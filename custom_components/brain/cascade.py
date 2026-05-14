"""Brain v0.3 cascade allocation — EV + bat target computation from PV surplus.

Pure module: no Home Assistant imports, no side effects.
All thresholds are injected via BrainInput — zero magic numbers here.

Design reference: V3-BRAIN-V03-DESIGN-R3 (outbox-905/V3-BRAIN-V03-DESIGN-R3.json)
QC PASS: outbox-901/V3-BRAIN-V03-R3-QC-PASS.json (2026-05-10T10:15)
v0.4: P3-BUFFER-GATE added between P2 and P3b. Gates bat-supplement (P3c) to
protect hard invariant: bat reaches 100% before sunset. P3b ANTI_EXPORT
(direct-sol >= ev_min) always passes — direct solar never jeopardises bat target.
v0.4.3: Bat-as-active-buffer — house load compensation independent of EV state.
"""

from __future__ import annotations

from .brain import BrainInput, BrainOutput
from .const import (
    BAT_EXPORT_DEADBAND_W,
    BAT_PHASE_BOOST_W,
    PHASES,
    STRATEGY_BAT_PRIORITY,
    STRATEGY_FORECAST_UNAVAILABLE,
    STRATEGY_NO_SUN,
    VOLTAGE_V,
)


def _bat_cannot_support_reason(inp: BrainInput, deficit_w: float) -> str:
    """Return a debug string explaining the first reason bat cannot supplement EV.

    Checked in priority order: can_engage gate, BMS-cap gate, UPS-marginal gate.
    Used only in the BAT_CANNOT_SUPPORT branch for operator diagnostics.

    Args:
        inp: Current brain input snapshot.
        deficit_w: How many watts bat would need to cover (ev_min_charge_w - surplus_w).

    Returns:
        Short diagnostic string embedded in the BAT_CANNOT_SUPPORT reason.
    """
    if not inp.bat_can_engage:
        return "can_engage=False"
    if inp.bat_max_discharge_w_now < deficit_w:
        return f"max_w={inp.bat_max_discharge_w_now:.0f}" f"<deficit={deficit_w:.0f}"
    return (
        f"soc={inp.bat_avg_soc_pct:.1f}%"
        f"<min={inp.ev_bat_support_min_soc:.0f}%"
        " (UPS-marginal)"
    )


def _compute_bat_grid_target(inp: BrainInput) -> float:
    """Compute bat target for grid-null objective (v0.4.3).

    Returns positive W (charge) when exporting PV surplus to absorb it,
    negative W (discharge) when importing beyond margin, 0.0 otherwise.
    Called BEFORE P1-gate so EV state never blocks grid balancing.

    Charge path (export absorption, P_PV_SURPLUS_ABSORB):
      grid_w < -BAT_EXPORT_DEADBAND_W AND bat_soc < charge_ceiling
      Returns: +min(export_excess, bat_max_charge_w_now)

    Discharge path (import reduction, P_HOUSE_LOAD_COMP):
      grid_w > house_load_comp_margin_w AND bat_can_engage AND soc > floor
      Returns: -min(import_excess, bat_max_discharge_w_now)
    """
    if not inp.bat_active_buffer_enabled:
        return 0.0
    if inp.in_night_window:
        return 0.0

    if inp.grid_w < -BAT_EXPORT_DEADBAND_W:
        if inp.bat_avg_soc_pct >= inp.bat_soc_charge_ceiling_pct:
            return 0.0  # bat full — binary consumers handle surplus
        export_excess_w = abs(inp.grid_w) - BAT_EXPORT_DEADBAND_W
        return min(export_excess_w, inp.bat_max_charge_w_now)

    if inp.grid_w > inp.house_load_comp_margin_w:
        if not inp.bat_can_engage:
            return 0.0
        floor_pct = inp.bat_floor_evening_pct if inp.in_evening_window else inp.bat_floor_day_pct
        if inp.bat_avg_soc_pct <= floor_pct:
            return 0.0
        import_excess_w = inp.grid_w - inp.house_load_comp_margin_w
        return -min(import_excess_w, inp.bat_max_discharge_w_now)

    return 0.0


def compute_targets(inp: BrainInput) -> BrainOutput:
    """Compute EV and bat allocation targets for one brain tick (v0.4.3 cascade).

    Implements the R3 priority cascade with v0.4 P3-BUFFER-GATE and v0.4.3
    bat-as-active-buffer (house load compensation).
    All thresholds read from inp — no constants used inside this function
    except PHASES, VOLTAGE_V (physics), STRATEGY_* (string literals), and
    BAT_PHASE_BOOST_W (v0.4.3 phase-boost constant).

    Priority order (first match wins):

      P0  Force override: operator takes full EV control; bat idle (0W).
      P1  EV gate: not connected OR SoC target reached → EV zero, bat = house_comp.
      P2  Night window: grid charges EV at ev_min_charge_w; bat idle (invariant).
      P3-BUFFER-GATE (v0.4): Determines bat_supplement_allowed flag.
            gate_enabled=False → supplement_allowed=True (v0.3.1 passthrough)
            strategy in (BUFFER_AVAILABLE, BAT_FULL) → supplement_allowed=True
            strategy in (BAT_PRIORITY, NO_SUN, FORECAST_UNAVAILABLE) → supplement_allowed=False
      P3a Bat-support disabled (master-switch off): anti-export only.
      P3b Sol surplus >= ev_min_charge_w: ANTI_EXPORT, bat = house_comp (usually 0).
      P3c Sol surplus <  ev_min_charge_w AND bat capable AND bat_supplement_allowed:
            BAT_SUPPLEMENT_BUFFER_OK. bat discharge covers max(EV deficit, house comp).
      P3d Sol surplus <  ev_min_charge_w AND (bat incapable OR gate blocked):
            bat = house_comp only.

    Args:
        inp: Immutable snapshot of all inputs for this tick.

    Returns:
        BrainOutput(target_ev_w, target_bat_w, reason).
    """
    # Pre-compute raw surplus (anti-export W available this tick; 0 if inside deadband).
    _raw_surplus_w: float = max(0.0, -inp.grid_w) if inp.grid_w <= -inp.deadband_w else 0.0

    # ★ v0.4.3: Pre-compute house bat target BEFORE P1-gate
    _house_bat_target_w: float = _compute_bat_grid_target(inp)

    # ★ v0.4.3: Phase-boost — computed once here so ALL early-return paths benefit
    # Boosts bat discharge when any phase approaches fuse limit (boost_a < fuse_a).
    # BMS cap (bat_max_discharge_w_now) always acts as safety ceiling.
    _max_phase_a: float = max(inp.l1_current_a, inp.l2_current_a, inp.l3_current_a)
    if inp.bat_active_buffer_enabled and _max_phase_a > inp.per_phase_boost_a:
        _boosted = _house_bat_target_w - BAT_PHASE_BOOST_W
        _house_bat_target_w = max(_boosted, -inp.bat_max_discharge_w_now)

    # ── P0: FORCE OVERRIDE ──────────────────────────────────────────────────
    if inp.force_active:
        target_ev_w = inp.force_a * PHASES * VOLTAGE_V
        return BrainOutput(
            target_ev_w=target_ev_w,
            target_bat_w=0.0,  # operator override — bat idle
            surplus_w=_raw_surplus_w,
            reason=f"FORCE_OVERRIDE amps={inp.force_a:.1f}",
        )

    # ── P1: EV-GATE ─────────────────────────────────────────────────────────
    if not inp.ev_connected:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★ bat discharges for house
            surplus_w=_raw_surplus_w,
            reason=f"EV_NOT_CONNECTED bat={_house_bat_target_w:.0f}W",
        )

    if inp.ev_soc >= inp.ev_target_soc:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★
            surplus_w=_raw_surplus_w,
            reason=(f"SOC_TARGET_REACHED soc={inp.ev_soc:.1f}%" f" bat={_house_bat_target_w:.0f}W"),
        )

    # ── P1b: EV PLUG-IN COOLDOWN (v0.4.1-B) ────────────────────────────────
    if inp.ev_plugin_cooldown_active:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★
            surplus_w=_raw_surplus_w,
            reason=(f"EV_PLUGIN_COOLDOWN" f" remaining_s={inp.ev_plugin_cooldown_remaining_s:.0f}"),
        )

    # ── P1.5: NO_CHARGE OPERATOR TOGGLE (v0.6a) ─────────────────────────────
    if inp.in_night_window and inp.skip_night_charge:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,  # night — bat idle (invariant)
            surplus_w=_raw_surplus_w,
            reason="NO_CHARGE_OPERATOR_TOGGLE ev_w=0",
        )

    # ── P2: NIGHT WINDOW — grid charges EV, bat IDLE ────────────────────────
    if inp.in_night_window:
        return BrainOutput(
            target_ev_w=inp.ev_min_charge_w,
            target_bat_w=0.0,  # night — bat idle (invariant)
            surplus_w=0.0,
            reason=f"NIGHT_CHARGE ev_w={inp.ev_min_charge_w:.0f}",
        )

    # ── P_PER_PHASE_FUSE: Per-fas strömskydd (v0.4.2-F / v0.4.3 updated) ───
    # Phase-boost already applied above; _house_bat_target_w reflects it.
    if _max_phase_a >= inp.per_phase_fuse_warning_a:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★ bat helps reduce phase current
            reason=(
                f"PER_PHASE_FUSE_PROTECT"
                f" max_a={_max_phase_a:.1f}A"
                f" limit={inp.per_phase_fuse_warning_a:.0f}A"
                f" bat={_house_bat_target_w:.0f}W"
            ),
            surplus_w=_raw_surplus_w,
        )

    # ── P_ELLEVIO: Ellevio-tak pre-gate (v0.4.1-A) ──────────────────────────
    if inp.ellevio_tak_w > 0.0:
        _pv_now_w = max(0.0, -inp.grid_w)
        _current_import_w = max(0.0, inp.grid_w)
        _max_grid_w = max(0.0, inp.ellevio_tak_w - inp.ps2_limit_w - inp.grid_safety_margin_w)
        _additional_headroom_w = max(0.0, _max_grid_w - _current_import_w)
        _available_for_ev_w = (
            _pv_now_w
            + inp.bat_max_discharge_w_now
            + _additional_headroom_w
            - inp.non_carma_anticipation_w
        )
        if _available_for_ev_w < inp.ev_min_charge_w:
            return BrainOutput(
                target_ev_w=0.0,
                target_bat_w=0.0,
                reason=(
                    f"ELLEVIO_TAK_NO_ROOM"
                    f" available={_available_for_ev_w:.0f}W"
                    f" ev_min={inp.ev_min_charge_w:.0f}W"
                    f" tak={inp.ellevio_tak_w:.0f}W"
                    f" import={_current_import_w:.0f}W"
                ),
                surplus_w=0.0,
            )

    # ── P3-BUFFER-GATE (v0.4) ───────────────────────────────────────────────
    if inp.ev_priority_operator:
        bat_supplement_allowed = True
        _ev_prio_prefix = "EV_PRIORITY_OPERATOR_"
    elif inp.buffer_gate_enabled and inp.buffer_strategy in (
        STRATEGY_BAT_PRIORITY,
        STRATEGY_NO_SUN,
        STRATEGY_FORECAST_UNAVAILABLE,
    ):
        bat_supplement_allowed = False
        _ev_prio_prefix = ""
    else:
        bat_supplement_allowed = True
        _ev_prio_prefix = ""

    surplus_w = _raw_surplus_w

    # ── P3: DAYTIME ─────────────────────────────────────────────────────────

    if not inp.bat_support_enabled:
        # P3a: anti-export only, no EV bat supplement.
        if surplus_w >= inp.ev_min_charge_w:
            return BrainOutput(
                target_ev_w=surplus_w,
                target_bat_w=_house_bat_target_w,  # ★ bat still helps house
                surplus_w=surplus_w,
                reason=f"ANTI_EXPORT_NO_BAT grid_w={inp.grid_w:.0f}W",
            )
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★
            surplus_w=surplus_w,
            reason=f"NO_SURPLUS_NO_BAT grid_w={inp.grid_w:.0f}W",
        )

    # P3b: Sol covers EV alone — anti-export, bat = house_comp (usually 0 when exporting).
    if surplus_w >= inp.ev_min_charge_w:
        return BrainOutput(
            target_ev_w=surplus_w,
            target_bat_w=_house_bat_target_w,  # ★ (0 when grid_w < 0 i.e. exporting)
            surplus_w=surplus_w,
            reason=f"ANTI_EXPORT grid_w={inp.grid_w:.0f}W",
        )

    # P3c / P3d: Sol insufficient — check bat capability AND buffer gate
    deficit_w = inp.ev_min_charge_w - surplus_w

    bat_ok = (
        bat_supplement_allowed
        and inp.bat_can_engage
        and inp.bat_max_discharge_w_now >= deficit_w
        and inp.bat_avg_soc_pct >= inp.ev_bat_support_min_soc
    )

    if bat_ok:
        # ★ P3c: Take the more aggressive of EV-deficit and house compensation
        ev_supplement_bat_w = -deficit_w
        target_bat_w = min(ev_supplement_bat_w, _house_bat_target_w)
        target_bat_w = max(target_bat_w, -inp.bat_max_discharge_w_now)
        return BrainOutput(
            target_ev_w=inp.ev_min_charge_w,
            target_bat_w=target_bat_w,
            surplus_w=surplus_w,
            reason=(
                f"{_ev_prio_prefix}BAT_SUPPLEMENT_BUFFER_OK"
                f" ev_w={inp.ev_min_charge_w:.0f}W"
                f" bat={target_bat_w:.0f}W"
                f" house_comp={_house_bat_target_w:.0f}W"
                f" ev_def={ev_supplement_bat_w:.0f}W"
                f" soc={inp.bat_avg_soc_pct:.1f}%"
            ),
        )

    # P3d: Cannot supplement EV — determine specific reason, house comp still active
    if not bat_supplement_allowed:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=_house_bat_target_w,  # ★ house comp still active
            surplus_w=surplus_w,
            reason=f"BAT_PRIORITY_BUFFER_BLOCK strategy={inp.buffer_strategy}",
        )

    reason = _bat_cannot_support_reason(inp, deficit_w)
    return BrainOutput(
        target_ev_w=0.0,
        target_bat_w=_house_bat_target_w,  # ★ house comp still active
        surplus_w=surplus_w,
        reason=f"{_ev_prio_prefix}BAT_CANNOT_SUPPORT {reason} house_comp={_house_bat_target_w:.0f}W",
    )
