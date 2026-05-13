"""Brain v0.3 cascade allocation — EV + bat target computation from PV surplus.

Pure module: no Home Assistant imports, no side effects.
All thresholds are injected via BrainInput — zero magic numbers here.

Design reference: V3-BRAIN-V03-DESIGN-R3 (outbox-905/V3-BRAIN-V03-DESIGN-R3.json)
QC PASS: outbox-901/V3-BRAIN-V03-R3-QC-PASS.json (2026-05-10T10:15)
v0.4: P3-BUFFER-GATE added between P2 and P3b. Gates bat-supplement (P3c) to
protect hard invariant: bat reaches 100% before sunset. P3b ANTI_EXPORT
(direct-sol >= ev_min) always passes — direct solar never jeopardises bat target.
"""

from __future__ import annotations

from .brain import BrainInput, BrainOutput
from .const import (
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


def compute_targets(inp: BrainInput) -> BrainOutput:
    """Compute EV and bat allocation targets for one brain tick (v0.4 cascade).

    Implements the R3 priority cascade with v0.4 P3-BUFFER-GATE.
    All thresholds read from inp — no constants used inside this function
    except PHASES and VOLTAGE_V (physics) and STRATEGY_* (string literals).

    Priority order (first match wins):

      P0  Force override: operator takes full EV control; bat stays idle (0W).
      P1  EV gate: not connected OR SoC target reached → both outputs zero.
      P2  Night window: grid charges EV at ev_min_charge_w; bat is idle (0W).
      P3-BUFFER-GATE (v0.4): Determines bat_supplement_allowed flag.
            gate_enabled=False → supplement_allowed=True (v0.3.1 passthrough)
            strategy in (BUFFER_AVAILABLE, BAT_FULL) → supplement_allowed=True
            strategy in (BAT_PRIORITY, NO_SUN, FORECAST_UNAVAILABLE) → supplement_allowed=False
      P3a Bat-support disabled (master-switch off): anti-export only.
            surplus >= ev_min_charge_w → ANTI_EXPORT_NO_BAT
            surplus <  ev_min_charge_w → NO_SURPLUS_NO_BAT (EV waits — 6A invariant)
      P3b Sol surplus >= ev_min_charge_w: ANTI_EXPORT, bat idle.
            (direct-sol — always passes, buffer gate does not apply here)
      P3c Sol surplus <  ev_min_charge_w AND bat capable AND bat_supplement_allowed:
            BAT_SUPPLEMENT_BUFFER_OK. bat discharge covers exact deficit.
      P3d Sol surplus <  ev_min_charge_w AND (bat incapable OR gate blocked):
            BAT_PRIORITY_BUFFER_BLOCK (gate blocked) or BAT_CANNOT_SUPPORT (bat incapable).
            EV waits — no grid-import fallback (per SCOPE-LOCK).

    Args:
        inp: Immutable snapshot of all inputs for this tick.

    Returns:
        BrainOutput(target_ev_w, target_bat_w, reason).
    """
    # Pre-compute raw surplus (anti-export W available this tick; 0 if inside deadband).
    # Used in early returns so binary assets always get their share of leftover PV.
    _raw_surplus_w: float = max(0.0, -inp.grid_w) if inp.grid_w <= -inp.deadband_w else 0.0

    # ── P0: FORCE OVERRIDE ──────────────────────────────────────────────────
    if inp.force_active:
        target_ev_w = inp.force_a * PHASES * VOLTAGE_V
        return BrainOutput(
            target_ev_w=target_ev_w,
            target_bat_w=0.0,
            surplus_w=_raw_surplus_w,
            reason=f"FORCE_OVERRIDE amps={inp.force_a:.1f}",
        )

    # ── P1: EV-GATE ─────────────────────────────────────────────────────────
    if not inp.ev_connected:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=_raw_surplus_w,
            reason="EV_NOT_CONNECTED",
        )

    if inp.ev_soc >= inp.ev_target_soc:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=_raw_surplus_w,
            reason=f"SOC_TARGET_REACHED soc={inp.ev_soc:.1f}%",
        )

    # ── P1b: EV PLUG-IN COOLDOWN (v0.4.1-B) ────────────────────────────────
    # Absorbs the plug-in current spike — EV waits until the house load settles.
    if inp.ev_plugin_cooldown_active:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=_raw_surplus_w,
            reason=f"EV_PLUGIN_COOLDOWN remaining_s={inp.ev_plugin_cooldown_remaining_s:.0f}",
        )

    # ── P1.5: NO_CHARGE OPERATOR TOGGLE (v0.6a) ─────────────────────────────
    # Operator blocks night grid-charging without force-override.
    # Bypasses P2 NIGHT_CHARGE when skip_night_charge=on + in_night_window.
    # Daytime surplus (P3) is NOT blocked — gate is night-only.
    if inp.in_night_window and inp.skip_night_charge:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=_raw_surplus_w,
            reason="NO_CHARGE_OPERATOR_TOGGLE ev_w=0",
        )

    # ── P2: NIGHT WINDOW — grid charges EV, bat IDLE ────────────────────────
    if inp.in_night_window:
        return BrainOutput(
            target_ev_w=inp.ev_min_charge_w,
            target_bat_w=0.0,
            surplus_w=0.0,
            reason=f"NIGHT_CHARGE ev_w={inp.ev_min_charge_w:.0f}",
        )

    # ── P_PER_PHASE_FUSE: Per-fas strömskydd (v0.4.2-F) ─────────────────────
    # Protects against tripping 16A fuse on any single phase.
    # Live incident 2026-05-12 18:08: Brain had no per-phase guard.
    # Threshold configurable (default 14A = 2A below 16A fuse).
    _max_phase_a = max(inp.l1_current_a, inp.l2_current_a, inp.l3_current_a)
    if _max_phase_a >= inp.per_phase_fuse_warning_a:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            reason=(
                f"PER_PHASE_FUSE_PROTECT"
                f" max_a={_max_phase_a:.1f}A"
                f" limit={inp.per_phase_fuse_warning_a:.0f}A"
                f" L1={inp.l1_current_a:.1f}"
                f" L2={inp.l2_current_a:.1f}"
                f" L3={inp.l3_current_a:.1f}"
            ),
            surplus_w=_raw_surplus_w,
        )

    # ── P_ELLEVIO: Ellevio-tak pre-gate (v0.4.1-A) ──────────────────────────
    # Guard: pv_now + bat_room + additional_grid_headroom - anticipation >= ev_min.
    # Only active when ellevio_tak_w > 0 (entity is configured and available).
    # grid_w encodes current import/export: positive = importing, negative = exporting.
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
    # Determines whether bat-supplement (P3c) is allowed this tick.
    # P3b ANTI_EXPORT (direct-sol >= ev_min) ALWAYS passes — direct solar
    # power never competes with bat charge target.
    # ev_priority_operator (v0.4.1-E): operator-override bypasses buffer gate.
    # Ellevio gate (above) still applies and cannot be overridden.
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
        bat_supplement_allowed = True  # BUFFER_AVAILABLE, BAT_FULL, or gate disabled
        _ev_prio_prefix = ""

    # Pre-compute daytime surplus (moved here from P3 body for reuse in all P3 returns)
    surplus_w = _raw_surplus_w

    # ── P3: DAYTIME ─────────────────────────────────────────────────────────
    # surplus_w computed above (shared between gate and P3 allocations)

    if not inp.bat_support_enabled:
        # P3a: v0.1 fallback — anti-export only, no bat supplement.
        # ev_min_charge_w gate always applies: Brain never sends below 6A-equivalent
        # to ev-balancer (HARD invariant — balancer would discard or cause grid import).
        if surplus_w >= inp.ev_min_charge_w:
            return BrainOutput(
                target_ev_w=surplus_w,
                target_bat_w=0.0,
                surplus_w=surplus_w,
                reason=f"ANTI_EXPORT_NO_BAT grid_w={inp.grid_w:.0f}W",
            )
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=surplus_w,
            reason=f"NO_SURPLUS_NO_BAT grid_w={inp.grid_w:.0f}W",
        )

    # P3b: Sol covers EV alone — anti-export, bat idle.
    # Buffer gate does NOT apply here: direct solar → EV never risks bat target.
    if surplus_w >= inp.ev_min_charge_w:
        return BrainOutput(
            target_ev_w=surplus_w,
            target_bat_w=0.0,
            surplus_w=surplus_w,
            reason=f"ANTI_EXPORT grid_w={inp.grid_w:.0f}W",
        )

    # P3c / P3d: Sol insufficient — check bat capability AND buffer gate
    deficit_w = inp.ev_min_charge_w - surplus_w

    bat_ok = (
        bat_supplement_allowed  # v0.4 buffer gate (first check)
        and inp.bat_can_engage  # balancer signals ready
        and inp.bat_max_discharge_w_now >= deficit_w  # BMS cap covers deficit
        and inp.bat_avg_soc_pct >= inp.ev_bat_support_min_soc  # UPS-marginal OK
    )

    if bat_ok:
        # P3c: Bat-supplement with buffer OK — bat covers exact deficit
        target_bat_w = -deficit_w
        return BrainOutput(
            target_ev_w=inp.ev_min_charge_w,
            target_bat_w=target_bat_w,
            surplus_w=surplus_w,
            reason=(
                f"{_ev_prio_prefix}BAT_SUPPLEMENT_BUFFER_OK"
                f" ev_w={inp.ev_min_charge_w:.0f}W"
                f" sun={surplus_w:.0f}W"
                f" bat={target_bat_w:.0f}W"
                f" soc={inp.bat_avg_soc_pct:.1f}%"
            ),
        )

    # P3d: Cannot supplement — determine specific reason
    if not bat_supplement_allowed:
        return BrainOutput(
            target_ev_w=0.0,
            target_bat_w=0.0,
            surplus_w=surplus_w,
            reason=f"BAT_PRIORITY_BUFFER_BLOCK strategy={inp.buffer_strategy}",
        )

    reason = _bat_cannot_support_reason(inp, deficit_w)
    return BrainOutput(
        target_ev_w=0.0,
        target_bat_w=0.0,
        surplus_w=surplus_w,
        reason=f"{_ev_prio_prefix}BAT_CANNOT_SUPPORT {reason}",
    )
