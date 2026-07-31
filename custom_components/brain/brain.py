"""Brain v0.4 — PV-buffert-styrd cascading controller.

v0.4: Buffer snapshot computed before each cascade tick. BufferSnapshot gates
bat-supplement (P3c/P3d) to protect the hard invariant: bat reaches 100% before sunset.

v0.3.1 wiring: compute_targets() output is written to HA via service calls:
  - input_number.brain_target_ev_w   (EV charge target)
  - input_number.brain_target_bat_w  (bat discharge target; Brain sole writer)
  - input_text.brain_cascade_reason  (human-readable decision label)
  - input_select.pv2_manual_mode_*   (Charge/Discharge/Standby/Auto per bat target)

v0.4 additions:
  - input_number.brain_bat_need_kwh
  - input_number.brain_pv_surplus_remaining_kwh
  - input_number.brain_pv_buffer_kwh
  - input_text.brain_strategy

IT-3601: 0-vision STAY_ON gate — binary assets disengage when surplus drops below
  load_w - 100W (grid ≤ 100W invariant). Replaces STAY_ON_FACTOR=0.7 which allowed
  pool_elv to stay ON while importing up to 830W (3100×0.7=2170W → grid=830W FAIL).
  Pure helper _binary_cascade_on() extracted for unit testability.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time as _time_module
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

from homeassistant.util import dt as dt_util

from .cap_enforcer import CapEnforcer
from .const import (
    BAT_FORECAST_THRESHOLD_KWH,
    BAT_LOAN_MIN_SOC_PCT,
    BAT_MAX_DISCHARGE_FALLBACK_W,
    BAT_MIN_ABSOLUTE_PCT,
    BAT_NIGHT_TARGET_PCT,
    BINARY_ACTION_TMPL,
    BINARY_ASSET_IDS,
    BUFFER_WRITE_EPSILON_KWH,
    DEADBAND_W,
    DEFAULT_BAT_CAPACITY_KWH,
    DEFAULT_BAT_FLOOR_AFTER_SURPLUS_PCT,
    DEFAULT_BAT_FLOOR_DAY_PCT,
    DEFAULT_BAT_FLOOR_EVENING_PCT,
    DEFAULT_BAT_FLOOR_MORNING_PCT,
    DEFAULT_BAT_HARD_FLOOR_PCT,
    DEFAULT_BAT_SOC_CHARGE_CEILING_PCT,
    DEFAULT_DEADBAND_W,
    DEFAULT_EV_BAT_SUPPORT_MIN_SOC,
    DEFAULT_EV_MIN_CHARGE_W,
    DEFAULT_EV_PLUGIN_COOLDOWN_S,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_FORCE_EV_AMPS,
    DEFAULT_GRID_SAFETY_MARGIN_W,
    DEFAULT_HOUSE_BASELINE_KW,
    DEFAULT_HOUSE_LOAD_COMP_MARGIN_W,
    DEFAULT_NIGHT_END_HOUR,
    DEFAULT_NIGHT_START_HOUR,
    DEFAULT_NON_CARMA_ANTICIPATION_W,
    DEFAULT_PER_PHASE_BOOST_A,
    DEFAULT_PER_PHASE_WARNING_A,
    DEFAULT_SURPLUS_END_HOUR,
    DEFAULT_SURPLUS_START_HOUR,
    EASEE_CONNECTED_STATES,
    ENTITY_BAT_ACTIVE_BUFFER_ENABLED,
    ENTITY_BAT_AVG_SOC_PCT,
    ENTITY_BAT_BALANCER_CAPABILITY,
    ENTITY_BAT_BALANCER_MODE,
    ENTITY_BAT_BALANCER_TARGET_MANUAL_W,
    ENTITY_BAT_CAPACITY_FORRAD,
    ENTITY_BAT_CAPACITY_KONTOR,
    ENTITY_BAT_FLOOR_AFTER_SURPLUS_PCT,
    ENTITY_BAT_FLOOR_DAY_PCT,
    ENTITY_BAT_FLOOR_EVENING_PCT,
    ENTITY_BAT_FLOOR_MORNING_PCT,
    ENTITY_BAT_FORECAST_THRESHOLD_KWH,
    ENTITY_BAT_LOAN_MIN_SOC_PCT,
    ENTITY_BAT_MIN_ABSOLUTE_PCT,
    ENTITY_BAT_NIGHT_TARGET_PCT,
    ENTITY_BAT_OFFER_SOURCE,
    ENTITY_BAT_POWER_FORRAD,
    ENTITY_BAT_POWER_KONTOR,
    ENTITY_BAT_SUPPORT_ENABLED,
    ENTITY_BRAIN_BAT_HARD_FLOOR_PCT,
    ENTITY_BRAIN_BAT_NEED_KWH,
    ENTITY_BRAIN_GRID_1MIN_AVG,
    ENTITY_BRAIN_GRID_5MIN_AVG,
    ENTITY_BRAIN_GRID_SAFETY_MARGIN_W,
    ENTITY_BRAIN_NON_CARMA_ANTICIPATION_W,
    ENTITY_BRAIN_PV_BUFFER_KWH,
    ENTITY_BRAIN_PV_SURPLUS_REMAINING_KWH,
    ENTITY_BRAIN_SKIP_NIGHT_CHARGE,
    ENTITY_BRAIN_STRATEGY,
    ENTITY_BRAIN_TARGET_GRID_W,
    ENTITY_BRAIN_WRITE_FAIL_COUNT,
    ENTITY_BUFFER_GATE_ENABLED,
    ENTITY_CASCADE_REASON,
    ENTITY_DEADBAND_W,
    ENTITY_ELLEVIO_DYNAMISKT_TAK,
    ENTITY_EV_ACTUAL_W,
    ENTITY_EV_BAT_SUPPORT_MIN_SOC,
    ENTITY_EV_MIN_CHARGE_W,
    ENTITY_EV_PLUGIN_COOLDOWN_S,
    ENTITY_EV_PRIORITY,
    ENTITY_EV_SOC,
    ENTITY_EV_TARGET_SOC,
    ENTITY_EV_TARGET_STICKY,
    ENTITY_FORCE_EV_ACTIVE,
    ENTITY_FORCE_EV_AMPS,
    ENTITY_GRID_POWER,
    ENTITY_HOUSE_BASELINE_KW,
    ENTITY_HOUSE_L1_CURRENT_A,
    ENTITY_HOUSE_L2_CURRENT_A,
    ENTITY_HOUSE_L3_CURRENT_A,
    ENTITY_HOUSE_TOTAL_W,
    ENTITY_NIGHT_CHARGE_END,
    ENTITY_NIGHT_CHARGE_START,
    ENTITY_NORDPOOL_CURRENT_PRICE,
    ENTITY_PER_PHASE_FUSE_WARNING_A,
    ENTITY_PS2_PEAK_SHAVING_LIMIT,
    ENTITY_PV2_MANUAL_MODE_FORRAD,
    ENTITY_PV2_MANUAL_MODE_KONTOR,
    ENTITY_PV2_REASON_FORRAD,
    ENTITY_PV2_REASON_KONTOR,
    ENTITY_PV_FORECAST_TOMORROW_KWH,
    ENTITY_PV_POWER_FORRAD,
    ENTITY_PV_POWER_KONTOR,
    ENTITY_PV_REMAINING_KWH,
    ENTITY_SUN_SUN,
    ENTITY_SURPLUS_END_TIME,
    ENTITY_SURPLUS_START_TIME,
    ENTITY_TARGET_BAT_W,
    ENTITY_TARGET_EV_W,
    PHASES,
    PV2_MODE_AUTO,
    PV2_MODE_CHARGE,
    PV2_MODE_DEADBAND_W,
    PV2_MODE_DISCHARGE,
    PV_SPIKE_MAX_BIAS_W,
    PV_SPIKE_THRESHOLD_W,
    SCAN_INTERVAL,
    STRATEGY_BAT_PRIORITY,
    VOLTAGE_V,
)
from .pv_buffer import BufferSnapshot, compute_buffer

_LOGGER = logging.getLogger(__name__)
_MUTEX_FORCE_RELEASE_S: float = 30.0  # IT-4218 Bug#2: force-release zombie tick-mutex


@dataclass(frozen=True)
class BrainInput:
    """Immutable snapshot of all inputs for one brain tick.

    v0.1/v0.2 fields are required (no default) to maintain call-site safety.
    v0.3 fields all carry safe fail-safe defaults (conservative: bat unavailable
    defaults to can_engage=False, soc=0 → Brain gives up EV rather than risking
    over-discharge).
    v0.4 fields: buffer gate + snapshot — fail-safe defaults conservatively block
    bat-supplement (BAT_PRIORITY strategy = gate closed).
    """

    # ── v0.1/v0.2 fields (required) ─────────────────────────────────────────
    grid_w: float
    ev_connected: bool
    ev_soc: float
    ev_target_soc: float
    force_active: bool
    force_a: float
    in_night_window: bool = False

    # ── v0.3 fields (optional, conservative fail-safes) ─────────────────────
    ev_min_charge_w: float = DEFAULT_EV_MIN_CHARGE_W
    bat_support_enabled: bool = True
    bat_can_engage: bool = False  # conservative: assume bat unavailable
    bat_max_discharge_w_now: float = 0.0  # conservative: 0W → bat_ok=False
    bat_avg_soc_pct: float = 0.0  # conservative: 0% → UPS-marginal fails
    ev_bat_support_min_soc: float = DEFAULT_EV_BAT_SUPPORT_MIN_SOC
    deadband_w: float = DEFAULT_DEADBAND_W

    # ── v0.4 fields (optional, conservative fail-safes) ─────────────────────
    buffer_gate_enabled: bool = True
    buffer_strategy: str = STRATEGY_BAT_PRIORITY  # conservative: gate closed
    buffer_kwh: float = -1.0
    bat_need_kwh: float = 0.0
    pv_surplus_remaining_kwh: float = 0.0

    # ── v0.4.1 fields (optional, conservative fail-safes) ────────────────────
    # A: Ellevio-tak gate (0.0 = entity unavailable → gate disabled)
    ellevio_tak_w: float = 0.0
    ps2_limit_w: float = 0.0
    grid_safety_margin_w: float = DEFAULT_GRID_SAFETY_MARGIN_W
    # B: EV plug-in cooldown (False = no cooldown active)
    ev_plugin_cooldown_active: bool = False
    ev_plugin_cooldown_remaining_s: float = 0.0
    # C: Non-CARMA load (measured) + anticipation reserve (operator-set)
    non_carma_load_w: float = 0.0  # measured: house_total - ev_actual; 0=unknown
    non_carma_anticipation_w: float = DEFAULT_NON_CARMA_ANTICIPATION_W

    # ── v0.4.1 binary-asset + operator fields ────────────────────────────────
    ev_priority_operator: bool = False  # input_boolean.brain_ev_priority: bypass buffer gate

    # ── v0.6a: NO_CHARGE operator gate ──────────────────────────────────────────
    skip_night_charge: bool = (
        True  # conservative: block night grid-charge unless explicitly allowed
    )

    # ── v0.4.2 fields: per-phase fuse gate ────────────────────────────────────
    l1_current_a: float = 0.0
    l2_current_a: float = 0.0
    l3_current_a: float = 0.0
    per_phase_fuse_warning_a: float = 14.0

    # ── v0.4.3 fields: house load compensation (bat-as-active-buffer) ──────────
    bat_active_buffer_enabled: bool = True  # DEFAULT ON — Borje 2026-05-13
    bat_floor_day_pct: float = 50.0  # min SoC for bat discharge daytime
    bat_floor_evening_pct: float = 30.0  # min SoC for bat discharge evening (17-22)
    in_evening_window: bool = False  # True = 17:00-21:59 local time
    house_load_comp_margin_w: float = 500.0  # bat engages only when grid_import > this
    per_phase_boost_a: float = 13.0  # phase-boost threshold (A)
    bat_soc_charge_ceiling_pct: float = 95.0  # bat stops absorbing surplus above this SoC
    bat_max_charge_w_now: float = 0.0  # BMS charge cap (conservative fail-safe: 0)
    # IT-3599: 5min rolling avg grid (neg=export) — used in bat charge/discharge decision
    # to avoid transient reactions when binary loads (pool_elv, miner) switch on/off.
    # None = sensor unavailable → fallback to instantaneous grid_w.
    grid_5min_w: float | None = None
    # v0.4.5.3: 1-min avg for F2 feed-forward (faster convergence than 5min).
    # Binary cascade keeps 5min avg to suppress oscillation from binary load toggles.
    grid_1min_w: float | None = None
    # v0.4.5 F2.1: bat actual power (sum kontor+forrad, positive=discharge, negative=charge).
    # Required for feed-forward grid-null: target_bat_w = -(bat_actual + grid_5min - target).
    bat_actual_w: float = 0.0

    # ── v0.4.4 fields: period-aware cascade ──────────────────────────────────
    # Period windows — False = current period is not this type
    in_morning_window: bool = False  # after night_end, before surplus_start_time
    in_after_surplus_window: bool = False  # after surplus_end_time, before night_start
    bat_floor_morning_pct: float = DEFAULT_BAT_FLOOR_MORNING_PCT
    bat_floor_after_surplus_pct: float = DEFAULT_BAT_FLOOR_AFTER_SURPLUS_PCT
    # Night cascade: absolute bat floor (discharge-to) and grid-charge target
    bat_min_absolute_pct: float = BAT_MIN_ABSOLUTE_PCT
    bat_night_target_pct: float = BAT_NIGHT_TARGET_PCT
    # True when tomorrow's PV forecast ≥ bat_forecast_threshold_kwh
    pv_surplus_likely: bool = False

    # ── v0.4.4 F1/F2: closed-loop grid-null target ───────────────────────────
    # F1: operator-configured grid target (W). 0=grid_null, positive=accept import.
    brain_target_grid_w: float = 0.0
    # F2: safety-gated effective target:
    #   = brain_target_grid_w if (bat_soc_avg<=bat_min_soc OR spotpris<0) else 0.0
    # Pre-computed in _read_inputs so cascade.py stays pure.
    effective_target_grid_w: float = 0.0

    # ── IT-4218 T2: PV-spike feed-forward (Borje 2026-05-17 11:08) ───────────
    # Positive bias (W) added to raw bat target when PV ramps > PV_SPIKE_THRESHOLD_W
    # in ~15s — shifts bat toward charge before grid_1min catches up to new surplus.
    # 0.0 = no spike detected or outside surplus window.
    pv_spike_bias_w: float = 0.0

    # ── IT-4218 F2 incremental P-controller (Borje 2026-05-18) ──────────────
    # Previous cycle bat target written to HA. BrainController injects
    # self._last_target_bat_w so cascade.py stays stateless.
    target_old_bat_w: float = 0.0

    # ── IT-4218 Spec 1.6 Batch B Issue 2: absolute SoC hard floor ────────────
    # Discharge blocked when bat_avg_soc_pct <= bat_hard_floor_pct, regardless
    # of grid signal. Overrides soft-floor gates in _compute_bat_grid_target.
    bat_hard_floor_pct: float = 5.0


@dataclass(frozen=True)
class BrainOutput:
    """Immutable result of one brain compute cycle.

    target_ev_w: requested EV charge power in watts (≥ 0)
    target_bat_w: requested bat discharge power in watts (≤ 0 = discharge, 0 = idle)
    reason: short machine-readable decision label for logging and dashboard
    """

    target_ev_w: float
    target_bat_w: float  # negative = charge, positive = discharge, 0 = idle
    reason: str
    surplus_w: float = 0.0  # PV anti-export surplus available for binary assets


def _night_window_active(current: time, start: time, end: time) -> bool:
    """Return True if *current* falls inside the [start, end) night window.

    Handles cross-midnight windows correctly (e.g. 22:00–06:00).
    """
    if start > end:
        return current >= start or current < end
    return (
        start <= current < end
    )  # pragma: no cover — same-day window is unusual config; production uses cross-midnight (22:00-06:00)


def compute_target(inp: BrainInput) -> tuple[float, str]:
    """Pure brain logic — anti-export + night-window charging (v0.2 API).

    Priority order (first match wins):
      1. Operator force-override
      2. EV gate (not connected | SoC target reached)
      3. Night window: EV connected + SoC < target → flat ev_min_charge_w
      4. Anti-export with deadband (only outside night window)

    Returns:
        (target_ev_w, reason_string)
    """
    if inp.force_active:
        target_w = inp.force_a * PHASES * VOLTAGE_V
        return target_w, f"FORCE_OVERRIDE amps={inp.force_a:.1f}"

    if not inp.ev_connected:
        return 0.0, "EV_NOT_CONNECTED"

    if inp.ev_soc >= inp.ev_target_soc:
        return (
            0.0,
            f"SOC_TARGET_REACHED soc={inp.ev_soc:.1f} target={inp.ev_target_soc:.1f}",
        )

    if inp.in_night_window:
        return (
            inp.ev_min_charge_w,
            f"NIGHT_CHARGE soc={inp.ev_soc:.1f} target={inp.ev_target_soc:.1f}"
            f" W={inp.ev_min_charge_w:.0f}",
        )

    if inp.grid_w >= -DEADBAND_W:
        return (
            0.0,
            f"NO_EXPORT grid_w={inp.grid_w:.0f}W deadband={DEADBAND_W:.0f}W",
        )

    target_w = -inp.grid_w
    return target_w, f"ANTI_EXPORT grid_w={inp.grid_w:.0f}W target={target_w:.0f}W"


def _binary_cascade_on(
    surplus_after_ev: float,
    bat_loan_ok: bool,
    bat_loan_available_w: float,
    prev_on: bool,
    engage_w: float,
    stay_on_w: float,
) -> tuple[bool, str]:
    """Pure binary-cascade on/off decision for a single asset (IT-3601).

    engage_w   — threshold to turn ON from OFF (e.g. POOL_ELV_W + 100 = 3100W)
    stay_on_w  — threshold to remain ON (IT-3601: LOAD_W - 100W → grid ≤ 100W)

    Path labels: A=pure surplus, B=surplus+bat_loan, H=stay-on hysteresis, -=off.
    Bat_loan allowed for stay-on (path-B hysteresis) so bat-assisted sessions can
    persist while bat_soc>50%; stay_on_w threshold ensures 0-vision compliance.
    """
    path_a = surplus_after_ev >= engage_w
    path_b = bat_loan_ok and (surplus_after_ev + bat_loan_available_w) >= engage_w

    if prev_on:
        stay_direct = surplus_after_ev >= stay_on_w
        stay_bat = bat_loan_ok and (surplus_after_ev + bat_loan_available_w) >= stay_on_w
        on = stay_direct or stay_bat
        path = "A" if path_a else ("B" if path_b else ("H" if on else "-"))
        return on, path

    on = path_a or path_b
    return on, "A" if path_a else ("B" if path_b else "-")


class BrainController:
    """Reads HA state, runs compute_targets (v0.3), writes all outputs to HA.

    v0.3.1: _async_tick now calls compute_targets() and writes:
      - target_ev_w → input_number.brain_target_ev_w
      - target_bat_w → input_number.brain_target_bat_w
      - cascade_reason → input_text.brain_cascade_reason
      - pv2_manual_mode_{kontor,forrad} → mapped from target_bat_w + ev_connected

    v0.4: buffer snapshot computed before cascade, 4 buffer outputs published.
    target_ev_w and target_bat_w write every cycle (v0.4.2-P3 — no skip). Manual pv2 override is
    respected (Brain does not overwrite when sensor.pv2_reason contains 'Manuellt').
    """

    def __init__(
        self,
        hass: Any,
        ev_status_entity: str,
        decision_log_path: str,
    ) -> None:
        """Initialize the controller.

        Args:
            hass: HomeAssistant instance.
            ev_status_entity: Entity ID to use for EV cable-connected detection.
            decision_log_path: JSONL file path for decision audit trail.
        """
        self._hass = hass
        self._ev_status_entity = ev_status_entity
        self._decision_log_path = decision_log_path
        self._last_target_w: float | None = None
        self._last_bat_w: float | None = None
        self._last_bat_offer_source: str | None = None
        self._last_cascade_reason: str | None = None
        self._last_pv2_modes: dict[str, str | None] = {
            ENTITY_PV2_MANUAL_MODE_KONTOR: None,
            ENTITY_PV2_MANUAL_MODE_FORRAD: None,
        }
        # v0.4 buffer output cache (epsilon-idempotent)
        self._last_bat_need_kwh: float | None = None
        self._last_pv_surplus_kwh: float | None = None
        self._last_buffer_kwh: float | None = None
        self._last_strategy: str | None = None
        # v0.4.3.4: binary-action hysteresis (Frej 2026-05-14) — anti-oscillation
        self._last_binary_mode: str | None = None
        self._last_binary_target_w: float = 0.0
        # v0.4.1-B: EV plug-in cooldown state
        self._ev_plugin_detected_at: float | None = None
        self._prev_ev_connected: bool | None = None  # None = first tick (no history)
        self._cycle_id: int = 0  # monotonic counter for binary action cycle_id
        self._unsub: Any = None
        # IT-4218 Spec A §2.3: SOC≥99% hysteresis — prevents 0↔12W oscillation
        self._near_zero_target_since: float | None = None
        # IT-4218 Storm 901 RCA 2026-05-18 13:45 (Fas 1.5 P1): SKIP warm-start.
        # Warm-start från RestoreEntity gav stale-window: HA återställer
        # input_number.brain_target_bat_w till värdet före restart (e.g. 859W).
        # bat-balancer läser stale 859W → ems=859W → grid IMPORT ~700W sustained
        # ~30s tills Brain re-init klar och skriver target=0 vid PV-fall-detect.
        # = HUVUDKRAV 1 BROTT. Starta från 0 (35s ramp via P-controller acceptabel
        # — bat-balancer kommer också se 0 direkt och inte chargea från stale).
        self._last_target_bat_w: float = 0.0
        # IT-4218 Spec 1.6 Batch B Issue 4: consecutive write-fail counter for self-heal
        self._consecutive_write_fails: int = 0
        # IT-4218 T2: PV-spike feed-forward history (monotonic_ts, pv_w pairs)
        self._pv_samples: collections.deque[tuple[float, float]] = collections.deque()
        # IT-4237 §3.1: BCEL cap-enforcement layer
        self._cap_enforcer: CapEnforcer = CapEnforcer(hass)
        # IT-4237 §3.4: EV sticky anti-oscillation
        self._last_ev_nonzero_ts: float | None = None
        self._last_ev_nonzero_w: float = 0.0
        self._ev_sticky_active: bool = False
        # IT-4218 Spec 1.9 BULLETPROOF: tick mutex + heartbeat supervisor
        self._tick_running: bool = False
        self._tick_running_since: float = 0.0  # Bug#2: monotonic ts when mutex acquired
        self._stopping: bool = False
        self._supervisor_task: asyncio.Task | None = None

    def _apply_hysteresis(self, target_bat_w: float, soc_max: float, grid_w_now: float) -> float:
        """IT-4218 Spec A §2.3: 30s sticky bat target at SOC ≥ 99%.

        When bat is nearly full (≥99%) and the computed target is near-zero
        (<10W), hold the previous target for 30 s to prevent 0↔12W oscillation
        observed in live data (GoodWe EMS switching every 5 s).

        Bypass: if |grid_w_now| > 500W the situation is urgent — skip hysteresis
        so Brain responds immediately to a strong violation.
        """
        import time as _time

        now_ts = _time.monotonic()

        if abs(grid_w_now) > 500.0:
            # Strong grid violation — bypass hysteresis regardless of SOC
            self._near_zero_target_since = None
            self._last_target_bat_w = target_bat_w
            return target_bat_w

        if soc_max >= 99.0 and abs(target_bat_w) < 10.0:
            if self._near_zero_target_since is None:
                self._near_zero_target_since = now_ts
            elapsed = now_ts - self._near_zero_target_since
            if elapsed < 30.0:
                _LOGGER.debug(
                    "Brain hysteresis: SOC≥99 near-zero target %.0fW → sticky %.0fW (%.0fs/30s)",
                    target_bat_w,
                    self._last_target_bat_w,
                    elapsed,
                )
                return self._last_target_bat_w  # sticky
            # After 30 s, accept the near-zero target and reset timer
            self._near_zero_target_since = None
        else:
            self._near_zero_target_since = None

        self._last_target_bat_w = target_bat_w
        return target_bat_w

    def _compute_pv_spike_bias(self) -> float:
        """IT-4218 T2(c): return charge bias when PV ramps > 1 kW in ~15s.

        Adds feed-forward bias toward bat-charge before grid_1min catches up to
        new PV surplus after a cloud gap clears. 0.0 = no spike or no history.
        """
        now_ts = _time_module.monotonic()
        pv_now = self._state_float(ENTITY_PV_POWER_KONTOR, 0.0) + self._state_float(
            ENTITY_PV_POWER_FORRAD, 0.0
        )
        self._pv_samples.append((now_ts, pv_now))
        # Prune samples older than 20s
        while self._pv_samples and now_ts - self._pv_samples[0][0] > 20.0:
            self._pv_samples.popleft()

        if len(self._pv_samples) < 2:
            return 0.0
        oldest_ts, oldest_pv = self._pv_samples[0]
        if now_ts - oldest_ts < 10.0:
            return 0.0  # need at least 10s of history

        delta = pv_now - oldest_pv
        if delta < PV_SPIKE_THRESHOLD_W:
            return 0.0

        bias = min(delta / 3.0, PV_SPIKE_MAX_BIAS_W)
        _LOGGER.debug("Brain: PV spike +%.0fW/15s → feed-forward bias=%.0fW", delta, bias)
        return bias

    def _pre_validate_bat_target(self, target_bat_w: float, grid_w_5min: float) -> float:
        """IT-4218 T2(a): safety net — ensure bat charge never pushes import > +100W.

        Applied after hysteresis so we always write a grid-safe target.
        Discharge direction is not touched here (export-cap is in cascade).

        Uses 5-min rolling average (IT-4218 Bug #1) — fallback to instant grid_w
        if sensor unavailable (handled at call site).

        Borje 2026-05-18 12:05: cap gäller ENBART vid IMPORT (grid_w_5min > 0).
        Vid EXPORT (grid_w_5min <= 0) — Brain ska kunna charge upp till bat_max_charge
        för 0-vision. Formel `headroom = 100 - grid_w_5min` vid g=-640 ger 740W cap som
        klampar Brain trots full export-headroom. bat_max_charge_w_now-clamp finns
        redan i cascade-funktionen.
        """
        from .const import BAT_EXPORT_DEADBAND_W

        if target_bat_w < 0.0 and grid_w_5min > 0.0:  # charging mot import-mode
            headroom = BAT_EXPORT_DEADBAND_W - grid_w_5min
            if headroom <= 0.0:
                return 0.0
            return max(target_bat_w, -headroom)
        return target_bat_w

    def _apply_ev_sticky(self, target_ev_w: float) -> float:
        """IT-4237 §3.4: Anti-oscillation sticky for target_ev_w.

        When Brain computes target=0 but Easee is bouncing (awaiting_start) AND
        we offered >0 within the last 60s — hold the non-zero target to prevent
        spurious limit=0 → Easee default-reset cascades.
        """
        now = _time_module.monotonic()

        if target_ev_w > 0.0:
            self._last_ev_nonzero_ts = now
            self._last_ev_nonzero_w = target_ev_w
            return target_ev_w

        if self._last_ev_nonzero_ts is None:
            return 0.0

        if now - self._last_ev_nonzero_ts > 60.0:
            return 0.0

        ev_state = self._hass.states.get(self._ev_status_entity)
        if ev_state is None or ev_state.state != "awaiting_start":
            return 0.0

        _LOGGER.debug(
            "Brain EV sticky: target=0 suppressed → %.0fW (awaiting_start, %.0fs since last offer)",
            self._last_ev_nonzero_w,
            now - self._last_ev_nonzero_ts,
        )
        return self._last_ev_nonzero_w

    def start(self) -> None:  # pragma: no cover
        """Register 5-second polling loop with the HA event bus."""
        from homeassistant.helpers.event import (
            async_track_time_interval,
        )

        self._unsub = async_track_time_interval(self._hass, self._async_tick, SCAN_INTERVAL)
        _LOGGER.info(
            "Brain v0.4 started - interval=%ds ev_entity=%s",
            int(SCAN_INTERVAL.total_seconds()),
            self._ev_status_entity,
        )
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = self._hass.async_create_background_task(
                self._heartbeat_supervisor(), name="brain_heartbeat_supervisor"
            )

    def stop(self) -> None:  # pragma: no cover
        """Unregister the polling loop."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
            _LOGGER.info("Brain v0.4 stopped")
        self._stopping = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            self._supervisor_task = None

    async def _async_tick(self, _now: Any) -> None:
        """Entry point — tick mutex + outer timeout guard (IT-4218 Spec 1.9 BULLETPROOF).

        Lager 1: tick-mutex skyddar mot concurrent execution
        Lager 2: heartbeat-supervisor re-registrerar timer om frozen
        Lager 3: force-release zombie mutex >_MUTEX_FORCE_RELEASE_S (IT-4218 Bug#2)
        """
        if self._tick_running:
            age = _time_module.monotonic() - self._tick_running_since
            if age > _MUTEX_FORCE_RELEASE_S:
                _LOGGER.warning("Brain: zombie mutex %.0fs — force-releasing (P1-E-ZOMBIE)", age)
                self._tick_running = False
                self._tick_running_since = 0.0
                return  # skip this tick, next tick starts fresh in 5s
            _LOGGER.warning("Brain: tick skipped — previous tick still running (P1-E mutex)")
            return
        self._tick_running = True
        self._tick_running_since = _time_module.monotonic()  # Bug#2: stamp simultant med mutex
        try:
            await asyncio.wait_for(self._async_tick_inner(_now), timeout=4.5)
        except TimeoutError:
            _LOGGER.error(
                "Brain: tick exceeded 4.5s timeout — killed. Modbus/service starvation? (P1-D)"
            )
        except asyncio.CancelledError:
            _LOGGER.warning("Brain: tick cancelled")
            raise
        except Exception as err:
            _LOGGER.exception("Brain: unexpected error in _async_tick: %s", err)
        finally:
            self._tick_running = False
            self._tick_running_since = 0.0

    async def _async_tick_inner(self, _now: Any) -> None:
        """Execute one brain tick: buffer_snapshot → read → compute_targets → write all outputs."""
        from .cascade import compute_targets  # deferred: cascade imports from brain

        # v0.4: Compute buffer snapshot FIRST, then inject into cascade via BrainInput
        snapshot = self._compute_buffer_snapshot()
        inp = self._read_inputs(snapshot)
        out = compute_targets(inp)
        # IT-4218 Spec A §2.3: apply SOC≥99% hysteresis before writing bat target
        bat_target_w_after_hyst = self._apply_hysteresis(
            out.target_bat_w, inp.bat_avg_soc_pct, inp.grid_w
        )
        # IT-4218 T2(a): final import-cap safety net (after hysteresis)
        # IT-4218 Bug#1: use 5min-avg for PV-flicker robustness; fallback to grid_w if sensor unavailable
        bat_target_w = self._pre_validate_bat_target(
            bat_target_w_after_hyst,
            inp.grid_5min_w if inp.grid_5min_w is not None else inp.grid_w,
        )
        _period_str = (
            "morning"
            if inp.in_morning_window
            else "after_surplus"
            if inp.in_after_surplus_window
            else "night"
            if inp.in_night_window
            else "surplus"
        )
        _max_phase_a = max(inp.l1_current_a, inp.l2_current_a, inp.l3_current_a)
        _LOGGER.debug(
            "F2_TICK period=%s bae=%s target_before_hysteresis=%.0f"
            " target_after_hysteresis=%.0f target_after_prevalidate=%.0f"
            " max_phase_a=%.1f phase_boost_applied=%s",
            _period_str,
            inp.bat_active_buffer_enabled,
            out.target_bat_w,
            bat_target_w_after_hyst,
            bat_target_w,
            _max_phase_a,
            "YES" if _max_phase_a > inp.per_phase_boost_a else "NO",
        )
        # IT-4237 §3.4: EV sticky — prevent 0-flip when Easee bouncing awaiting_start
        ev_target_w = self._apply_ev_sticky(out.target_ev_w)
        sticky_now = ev_target_w > 0.0 and out.target_ev_w == 0.0
        if sticky_now != self._ev_sticky_active:
            self._ev_sticky_active = sticky_now
            await self._hass.services.async_call(
                "input_boolean",
                "turn_on" if sticky_now else "turn_off",
                {"entity_id": ENTITY_EV_TARGET_STICKY},
                blocking=False,
            )
        await self._async_write_target(max(0.0, ev_target_w), out.reason)
        bat_offer_w, bat_offer_source = self._bat_mode_routing(bat_target_w)
        await self._async_write_bat_target(bat_offer_w, out.reason)
        await self._async_write_cascade_reason(out.reason)
        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {
                    "entity_id": ENTITY_BRAIN_WRITE_FAIL_COUNT,
                    "value": float(self._consecutive_write_fails),
                },
                blocking=False,
            )
        except Exception as err:
            _LOGGER.warning("Brain: write_fail_count update failed: %s", err)
        await self._async_write_pv2_mode(bat_offer_w, inp.ev_connected)
        await self._async_write_bat_offer_source(bat_offer_source)
        await self._async_write_buffer_outputs(snapshot)
        self._cycle_id += 1
        await self._async_write_binary_actions(
            out.surplus_w, ev_target_w, out.reason, inp, snapshot
        )
        # IT-4237 §3.1: BCEL cap-enforcement — fire-and-forget, never blocks Brain tick
        self._hass.async_create_task(self._cap_enforcer.tick())
        await self._append_decision_log(out.target_ev_w, out.reason, out.target_bat_w, snapshot)

    async def _async_write_binary_actions(
        self, surplus_w: float, target_ev_w: float, reason: str, inp=None, snapshot=None
    ) -> None:
        """Publish Brain ACTION JSON to binary_balancer input_text helpers.

        IT-3601 (0-vision STAY_ON gate): pool_elv/miner disengage when grid > 100W.
        Replaces STAY_ON_FACTOR=0.7 (= 2170W) which allowed pool_elv to stay ON
        while importing up to 830W — violating the 0-vision invariant.

        Engage logic (path-A OR path-B, user-confirmed 2026-05-14):
          POOL_ELV: ON if surplus_after_ev >= 3100W (A) OR surplus+bat>=3100W AND soc>50% (B)
          MINER:    ON if remaining_after_pool >= 500W (A) OR remaining+bat>=500W AND ok (B)

        Stay-on (IT-3601): _binary_cascade_on() with stay_on_w = load_w - 100W:
          pool_elv stays ON while surplus_after_EV >= 2700W (OFF gate: < 2700W, v0.4.4 F6)
          miner stays ON while remaining >= 400W (OFF gate: < 400W, v0.4.4 F6)
        """
        # Binär-consumer fasta effekter + engage-thresholds (v0.4.4 F6 finalized):
        # pool_elv: ON≥3100W, OFF<2700W (400W hysteresis per spec)
        # miner:    ON≥600W, OFF<400W (200W hysteresis per spec)
        POOL_ELV_W = 3000.0  # fast effekt ~3kW
        POOL_ELV_ENGAGE_W = 3100.0  # ON gate: surplus_after_EV ≥ 3100W
        POOL_ELV_STAY_ON_W = 2700.0  # OFF gate: surplus_after_EV < 2700W (wider hysteresis v0.4.4)
        MINER_W = 500.0  # fast effekt ~500W (updated v0.4.4)
        MINER_ENGAGE_W = 600.0  # ON gate: surplus_after_pool ≥ 600W
        MINER_MIN_W = MINER_ENGAGE_W  # legacy alias
        MINER_STAY_ON_W = 400.0  # OFF gate: surplus_after_pool < 400W
        BAT_LOAN_MIN_SOC = self._state_float(ENTITY_BAT_LOAN_MIN_SOC_PCT, BAT_LOAN_MIN_SOC_PCT)
        _POOL_VP_TOO_OFFLINE = True  # pool_vp offline default (modbus not mapped via binary)

        ts = datetime.now(tz=UTC).isoformat(timespec="seconds")
        # IT-3599: anti-oscillation — reconstruct virtual surplus using 5min avg.
        # 5min avg already accounts for all live loads (EV, house, binary assets).
        # Add back prior-cycle binary load draw so the cascade sees "clean" surplus
        # before its own assets, preventing engage→spike→disengage oscillation.
        _prev_pool_on = getattr(self, "_last_pool_elv_mode", None) == "on"
        _prev_miner_on = getattr(self, "_last_miner_mode", None) == "on"
        _prior_binary_w = (POOL_ELV_W if _prev_pool_on else 0.0) + (
            MINER_W if _prev_miner_on else 0.0
        )
        grid_5min_w = self._state_float_or_none(ENTITY_BRAIN_GRID_5MIN_AVG)
        if grid_5min_w is not None:
            # EV priority correction: subtract uncaptured EV demand so pool_elv/miner
            # don't claim surplus that cascade earmarked for EV but ev_balancer hasn't
            # acted on yet (grid_5min lags 5 min behind actual EV ramp).
            _actual_ev_w = self._state_float_or_none(ENTITY_EV_ACTUAL_W) or 0.0
            _ev_correction_w = max(0.0, target_ev_w - _actual_ev_w)
            surplus_after_ev = max(0.0, -grid_5min_w + _prior_binary_w - _ev_correction_w)
        else:
            surplus_after_ev = max(0.0, surplus_w - target_ev_w)

        # ★ v0.4.4 AFTER_SURPLUS: binary consumers OFF — bat handles 0-vision, not assets
        if inp is not None and inp.in_after_surplus_window:
            self._last_pool_elv_mode = "off"
            self._last_miner_mode = "off"
            for asset_id in BINARY_ASSET_IDS:
                entity_id = BINARY_ACTION_TMPL.format(asset_id=asset_id)
                payload = json.dumps(
                    {
                        "a": asset_id,
                        "w": 0.0,
                        "m": "off",
                        "dl": 120,
                        "r": f"{reason[:40]} AFTER_SURPLUS_OFF",
                        "c": self._cycle_id,
                        "s": "brain",
                        "t": ts,
                    },
                    separators=(",", ":"),
                )
                try:
                    await self._hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"entity_id": entity_id, "value": payload},
                        blocking=False,
                    )
                except Exception as err:
                    _LOGGER.warning("Brain: failed to write %s - %s", entity_id, err)
            return

        # IT-3604 PER_PHASE_FUSE_PROTECT gate: force all binary consumers OFF immediately.
        # grid_5min_avg lags by up to 5 min — if pool_elv was OFF before triggering, the avg
        # still shows export surplus, which would re-engage pool_elv mid-fuse-event, preventing
        # phase current from dropping and causing oscillation (5545W import ↔ −1420W export).
        # IT-3605: Set 5-min cooldown so binary assets can't re-engage until grid_5min_avg settles.
        if "PER_PHASE_FUSE_PROTECT" in reason:
            self._last_pool_elv_mode = "off"
            self._last_miner_mode = "off"
            _now_ts = datetime.now(tz=UTC).timestamp()
            _cooldown_until = getattr(self, "_fuse_protect_cooldown_until", 0.0)
            if _now_ts > _cooldown_until:
                self._fuse_protect_cooldown_until = _now_ts + 300.0
            for asset_id in BINARY_ASSET_IDS:
                entity_id = BINARY_ACTION_TMPL.format(asset_id=asset_id)
                payload = json.dumps(
                    {
                        "a": asset_id,
                        "w": 0.0,
                        "m": "off",
                        "dl": 120,
                        "r": f"{reason[:40]} FUSE_PROTECT_OFF",
                        "c": self._cycle_id,
                        "s": "brain",
                        "t": ts,
                    },
                    separators=(",", ":"),
                )
                try:
                    await self._hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"entity_id": entity_id, "value": payload},
                        blocking=False,
                    )
                except Exception as err:
                    _LOGGER.warning("Brain: failed to write %s - %s", entity_id, err)
            return

        # IT-3605: Binary asset cooldown after PER_PHASE_FUSE_PROTECT — suppress re-engagement
        # until grid_5min_avg has had time (300s) to settle to the new load state.
        # Without this, the grid_5min_avg lag causes pool_elv to re-engage immediately after
        # fuse forces it off, creating a 30s oscillation loop.
        _now_ts = datetime.now(tz=UTC).timestamp()
        if _now_ts < getattr(self, "_fuse_protect_cooldown_until", 0.0):
            _remaining_s = self._fuse_protect_cooldown_until - _now_ts
            self._last_pool_elv_mode = "off"
            self._last_miner_mode = "off"
            for asset_id in BINARY_ASSET_IDS:
                entity_id = BINARY_ACTION_TMPL.format(asset_id=asset_id)
                payload = json.dumps(
                    {
                        "a": asset_id,
                        "w": 0.0,
                        "m": "off",
                        "dl": 120,
                        "r": f"FUSE_COOLDOWN {_remaining_s:.0f}s remaining",
                        "c": self._cycle_id,
                        "s": "brain",
                        "t": ts,
                    },
                    separators=(",", ":"),
                )
                try:
                    await self._hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"entity_id": entity_id, "value": payload},
                        blocking=False,
                    )
                except Exception as err:
                    _LOGGER.warning("Brain: failed to write %s - %s", entity_id, err)
            return

        # Bat-loan tillgänglighet (Path-B-villkor gemensamma)
        bat_loan_available_w = 0.0
        bat_loan_ok = False
        if inp is not None:
            bat_loan_ok = (
                inp.bat_soc_pct > BAT_LOAN_MIN_SOC
                if hasattr(inp, "bat_soc_pct")
                else inp.bat_avg_soc_pct > BAT_LOAN_MIN_SOC
            )
            buffer_ok = snapshot is None or snapshot.buffer_kwh >= 0
            bat_loan_ok = bat_loan_ok and buffer_ok
            if bat_loan_ok:
                bat_loan_available_w = max(0.0, inp.bat_max_discharge_w_now)

        # Per-asset decision: pool_elv (TIER 3 / TIER 4) via _binary_cascade_on (IT-3601)
        prev_mode_pool = self._last_pool_elv_mode if hasattr(self, "_last_pool_elv_mode") else None

        # IT-3605b: Block pool_elv if EV is actively charging. Pool_elv is a single-phase
        # load (~13A) sharing phase L2 with EV (~6A/phase). Combined = 19A >> 14A fuse limit.
        # EV has higher priority — force pool_elv off regardless of cascade result.
        _ev_active = target_ev_w > 0.0
        pool_elv_on, pool_elv_path = _binary_cascade_on(
            surplus_after_ev,
            bat_loan_ok,
            bat_loan_available_w,
            prev_mode_pool == "on",
            POOL_ELV_ENGAGE_W,
            POOL_ELV_STAY_ON_W,
        )
        if _ev_active:
            pool_elv_on = False
            pool_elv_path = "-"
        pool_elv_pathA = (not _ev_active) and (surplus_after_ev >= POOL_ELV_ENGAGE_W)
        _pool_elv_pathB = (
            (not _ev_active)
            and bat_loan_ok
            and (surplus_after_ev + bat_loan_available_w) >= POOL_ELV_ENGAGE_W
        )
        pool_elv_target_w = POOL_ELV_W if pool_elv_on else 0.0
        self._last_pool_elv_mode = "on" if pool_elv_on else "off"

        # bat_loan-andel som behövs för pool_elv (om path-B)
        bat_loan_consumed_w = (
            max(0.0, POOL_ELV_W - surplus_after_ev) if pool_elv_on and not pool_elv_pathA else 0.0
        )

        # Miner (TIER 5): efter pool_elv-allokering
        remaining_after_pool = max(0.0, surplus_after_ev - (POOL_ELV_W if pool_elv_on else 0.0))
        remaining_bat_loan = max(0.0, bat_loan_available_w - bat_loan_consumed_w)
        prev_mode_miner = self._last_miner_mode if hasattr(self, "_last_miner_mode") else None
        miner_pathA = remaining_after_pool >= MINER_MIN_W  # noqa: F841
        miner_pathB = bat_loan_ok and (remaining_after_pool + remaining_bat_loan) >= MINER_MIN_W  # noqa: F841
        miner_on, miner_path = _binary_cascade_on(
            remaining_after_pool,
            bat_loan_ok,
            remaining_bat_loan,
            prev_mode_miner == "on",
            MINER_MIN_W,
            MINER_STAY_ON_W,
        )
        miner_target_w = MINER_W if miner_on else 0.0
        self._last_miner_mode = "on" if miner_on else "off"

        # Build per-asset payloads
        per_asset = {
            "miner": (miner_target_w, "on" if miner_on else "off", f"pathr_{miner_path}"),
            "pool_vp": (0.0, "off", "modbus_only_via_binary_balancer"),
            "pool_elv": (
                pool_elv_target_w,
                "on" if pool_elv_on else "off",
                f"path_{pool_elv_path}",
            ),
        }
        # Legacy aggregate for back-compat tests
        self._last_binary_mode = "on" if (pool_elv_on or miner_on) else "off"
        self._last_binary_target_w = pool_elv_target_w + miner_target_w

        for asset_id in BINARY_ASSET_IDS:
            entity_id = BINARY_ACTION_TMPL.format(asset_id=asset_id)
            asset_w, asset_mode, asset_path = per_asset.get(asset_id, (0.0, "off", "-"))
            asset_reason = f"{reason[:40]} {asset_path}"
            payload = json.dumps(
                {
                    "a": asset_id,
                    "w": round(asset_w, 1),
                    "m": asset_mode,
                    "dl": 120,
                    "r": asset_reason[:60],
                    "c": self._cycle_id,
                    "s": "brain",
                    "t": ts,
                },
                separators=(",", ":"),
            )
            try:
                await self._hass.services.async_call(
                    "input_text",
                    "set_value",
                    {"entity_id": entity_id, "value": payload},
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.warning("Brain: failed to write %s - %s", entity_id, err)

    def _compute_buffer_snapshot(self) -> BufferSnapshot:
        """Compute PV-buffer snapshot from current HA state."""
        bat_avg_soc = self._state_float(ENTITY_BAT_AVG_SOC_PCT, 0.0)
        cap_kontor = self._state_float(ENTITY_BAT_CAPACITY_KONTOR, 0.0)
        cap_forrad = self._state_float(ENTITY_BAT_CAPACITY_FORRAD, 0.0)
        bat_capacity_kwh = (cap_kontor + cap_forrad) or DEFAULT_BAT_CAPACITY_KWH
        pv_remaining_kwh = self._state_float_or_none(ENTITY_PV_REMAINING_KWH)
        house_baseline_kw = self._state_float(ENTITY_HOUSE_BASELINE_KW, DEFAULT_HOUSE_BASELINE_KW)
        hours_to_sunset, sun_below_horizon = self._sun_state()

        return compute_buffer(
            bat_avg_soc_pct=bat_avg_soc,
            bat_capacity_kwh=bat_capacity_kwh,
            pv_remaining_kwh=pv_remaining_kwh,
            house_baseline_kw=house_baseline_kw,
            hours_to_sunset=hours_to_sunset,
            sun_below_horizon=sun_below_horizon,
        )

    def _sun_state(self) -> tuple[float, bool]:
        """Return (hours_to_sunset, sun_below_horizon) from sun.sun entity.

        Returns (0.0, True) as fail-safe when entity is unavailable.
        """
        state = self._hass.states.get(ENTITY_SUN_SUN)
        if state is None or state.state in ("unavailable", "unknown"):
            return 0.0, True

        sun_below_horizon = state.state == "below_horizon"

        try:
            next_setting_str = state.attributes.get("next_setting")
            if next_setting_str is None:
                return 0.0, sun_below_horizon

            # HA returns datetime object or ISO string depending on version
            if isinstance(next_setting_str, datetime):
                next_setting = next_setting_str
            else:
                next_setting = datetime.fromisoformat(str(next_setting_str))

            now = datetime.now(tz=UTC)
            if next_setting.tzinfo is None:
                next_setting = next_setting.replace(tzinfo=UTC)
            else:
                next_setting = next_setting.astimezone(UTC)

            hours = max(0.0, (next_setting - now).total_seconds() / 3600)
            return hours, sun_below_horizon
        except (ValueError, TypeError, AttributeError):
            return 0.0, sun_below_horizon

    def _state_float_or_none(self, entity_id: str) -> float | None:
        """Read float state from HA — return None if unavailable or non-numeric."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", "none", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _compute_effective_target_grid_w(self) -> float:
        """F1/F2 safety gate: compute effective_target_grid_w.

        effective = brain_target_grid_w if (bat_soc_avg <= bat_min_soc OR spotpris < 0) else 0.0

        Logic: prevent import acceptance while bat has capacity AND spotpris is positive.
        When bat is at floor OR spotpris is negative: allow configured target (accept import
        or maximise charge per Börje spec).
        """
        brain_target = self._state_float(ENTITY_BRAIN_TARGET_GRID_W, 0.0)
        if brain_target == 0.0:
            return 0.0  # fast path: no configured target, skip gate evaluation

        bat_soc = self._state_float(ENTITY_BAT_AVG_SOC_PCT, 100.0)
        bat_min_soc = self._state_float(ENTITY_BAT_MIN_ABSOLUTE_PCT, BAT_MIN_ABSOLUTE_PCT)
        bat_at_floor = bat_soc <= bat_min_soc

        spotpris = self._state_float_or_none(ENTITY_NORDPOOL_CURRENT_PRICE)
        spotpris_negative = spotpris is not None and spotpris < 0.0

        if bat_at_floor or spotpris_negative:
            return brain_target
        return 0.0

    def _read_inputs(self, snapshot: BufferSnapshot | None = None) -> BrainInput:
        """Read all required HA entities and return a BrainInput snapshot."""
        if snapshot is None:
            snapshot = compute_buffer(
                bat_avg_soc_pct=0.0,
                bat_capacity_kwh=DEFAULT_BAT_CAPACITY_KWH,
                pv_remaining_kwh=None,
                house_baseline_kw=DEFAULT_HOUSE_BASELINE_KW,
                hours_to_sunset=0.0,
                sun_below_horizon=True,
            )
        buffer_gate_enabled = self._state_bool(ENTITY_BUFFER_GATE_ENABLED, default=True)
        _ev_conn = self._ev_connected()
        _ev_cooldown = self._update_ev_cooldown(_ev_conn)

        return BrainInput(
            grid_w=self._state_float(ENTITY_GRID_POWER),
            ev_connected=_ev_conn,
            ev_soc=self._state_float(ENTITY_EV_SOC),
            ev_target_soc=self._state_float(ENTITY_EV_TARGET_SOC, DEFAULT_EV_TARGET_SOC),
            force_active=self._state_bool(ENTITY_FORCE_EV_ACTIVE),
            force_a=self._state_float(ENTITY_FORCE_EV_AMPS, DEFAULT_FORCE_EV_AMPS),
            in_night_window=self._in_night_window(),
            # ── v0.3 bat fields ─────────────────────────────────────────────
            ev_min_charge_w=self._state_float(ENTITY_EV_MIN_CHARGE_W, DEFAULT_EV_MIN_CHARGE_W),
            bat_support_enabled=self._state_bool(ENTITY_BAT_SUPPORT_ENABLED, default=True),
            bat_can_engage=self._bat_can_engage(),
            bat_max_discharge_w_now=self._bat_max_discharge_w_now(),
            bat_avg_soc_pct=self._state_float(ENTITY_BAT_AVG_SOC_PCT),
            ev_bat_support_min_soc=self._state_float(
                ENTITY_EV_BAT_SUPPORT_MIN_SOC, DEFAULT_EV_BAT_SUPPORT_MIN_SOC
            ),
            deadband_w=self._state_float(ENTITY_DEADBAND_W, DEFAULT_DEADBAND_W),
            # ── v0.4 buffer fields ──────────────────────────────────────────
            buffer_gate_enabled=buffer_gate_enabled,
            buffer_strategy=snapshot.strategy,
            buffer_kwh=snapshot.buffer_kwh,
            bat_need_kwh=snapshot.bat_need_kwh,
            pv_surplus_remaining_kwh=snapshot.pv_surplus_remaining_kwh,
            # ── v0.4.1 fields ───────────────────────────────────────────────
            non_carma_load_w=max(
                0.0,
                (self._state_float_or_none(ENTITY_HOUSE_TOTAL_W) or 0.0)
                - max(0.0, self._last_target_w or 0.0),
            ),
            ellevio_tak_w=(self._state_float_or_none(ENTITY_ELLEVIO_DYNAMISKT_TAK) or 0.0) * 1000.0,
            ps2_limit_w=self._state_float_or_none(ENTITY_PS2_PEAK_SHAVING_LIMIT) or 0.0,
            grid_safety_margin_w=self._state_float(
                ENTITY_BRAIN_GRID_SAFETY_MARGIN_W, DEFAULT_GRID_SAFETY_MARGIN_W
            ),
            ev_plugin_cooldown_active=_ev_cooldown[0],
            ev_plugin_cooldown_remaining_s=_ev_cooldown[1],
            non_carma_anticipation_w=self._state_float(
                ENTITY_BRAIN_NON_CARMA_ANTICIPATION_W, DEFAULT_NON_CARMA_ANTICIPATION_W
            ),
            ev_priority_operator=self._state_bool(ENTITY_EV_PRIORITY, default=False),
            # ── v0.6a NO_CHARGE operator gate ───────────────────────────────
            skip_night_charge=self._state_bool(ENTITY_BRAIN_SKIP_NIGHT_CHARGE, default=True),
            # ── v0.4.2 per-phase fuse gate ──────────────────────────────────
            l1_current_a=self._state_float(ENTITY_HOUSE_L1_CURRENT_A, 0.0),
            l2_current_a=self._state_float(ENTITY_HOUSE_L2_CURRENT_A, 0.0),
            l3_current_a=self._state_float(ENTITY_HOUSE_L3_CURRENT_A, 0.0),
            per_phase_fuse_warning_a=self._state_float(
                ENTITY_PER_PHASE_FUSE_WARNING_A, DEFAULT_PER_PHASE_WARNING_A
            ),
            # ── v0.4.3 bat-active-buffer ────────────────────────────────────
            bat_active_buffer_enabled=self._state_bool(
                ENTITY_BAT_ACTIVE_BUFFER_ENABLED, default=True
            ),
            bat_floor_day_pct=self._state_float(
                ENTITY_BAT_FLOOR_DAY_PCT, DEFAULT_BAT_FLOOR_DAY_PCT
            ),
            bat_floor_evening_pct=self._state_float(
                ENTITY_BAT_FLOOR_EVENING_PCT, DEFAULT_BAT_FLOOR_EVENING_PCT
            ),
            in_evening_window=self._in_evening_window(),
            house_load_comp_margin_w=self._state_float(
                ENTITY_BRAIN_GRID_SAFETY_MARGIN_W, DEFAULT_HOUSE_LOAD_COMP_MARGIN_W
            ),
            per_phase_boost_a=DEFAULT_PER_PHASE_BOOST_A,
            bat_soc_charge_ceiling_pct=DEFAULT_BAT_SOC_CHARGE_CEILING_PCT,
            bat_max_charge_w_now=self._bat_max_charge_w_now(),
            grid_5min_w=self._state_float_or_none(ENTITY_BRAIN_GRID_5MIN_AVG),
            grid_1min_w=self._state_float_or_none(ENTITY_BRAIN_GRID_1MIN_AVG),
            bat_actual_w=(
                self._state_float(ENTITY_BAT_POWER_KONTOR, 0.0)
                + self._state_float(ENTITY_BAT_POWER_FORRAD, 0.0)
            ),
            # ── v0.4.4 period-aware cascade ─────────────────────────────────
            in_morning_window=self._in_morning_window(),
            in_after_surplus_window=self._in_after_surplus_window(),
            bat_floor_morning_pct=self._state_float(
                ENTITY_BAT_FLOOR_MORNING_PCT, DEFAULT_BAT_FLOOR_MORNING_PCT
            ),
            bat_floor_after_surplus_pct=self._state_float(
                ENTITY_BAT_FLOOR_AFTER_SURPLUS_PCT, DEFAULT_BAT_FLOOR_AFTER_SURPLUS_PCT
            ),
            bat_min_absolute_pct=self._state_float(
                ENTITY_BAT_MIN_ABSOLUTE_PCT, BAT_MIN_ABSOLUTE_PCT
            ),
            bat_night_target_pct=self._state_float(
                ENTITY_BAT_NIGHT_TARGET_PCT, BAT_NIGHT_TARGET_PCT
            ),
            pv_surplus_likely=self._compute_pv_surplus_likely(),
            # ── v0.4.4 F1/F2: closed-loop grid-null ─────────────────────────
            brain_target_grid_w=self._state_float(ENTITY_BRAIN_TARGET_GRID_W, 0.0),
            effective_target_grid_w=self._compute_effective_target_grid_w(),
            # ── IT-4218 T2: PV-spike feed-forward ────────────────────────────
            pv_spike_bias_w=self._compute_pv_spike_bias(),
            # ── IT-4218 F2 incremental: previous bat target as P-controller base ─
            target_old_bat_w=self._last_target_bat_w,
            # ── IT-4218 Spec 1.6 Batch B Issue 2: absolute SoC hard floor ────────
            bat_hard_floor_pct=self._state_float(
                ENTITY_BRAIN_BAT_HARD_FLOOR_PCT, DEFAULT_BAT_HARD_FLOOR_PCT
            ),
        )

    async def _async_write_input_number(
        self,
        entity_id: str,
        value: float,
        last_ref: str,
    ) -> None:
        """Write a float value to an input_number entity (idempotent).

        Internal DRY helper. On service failure: logs error, resets cache for retry.
        last_ref is the name of the instance attribute holding the last-written value.
        """
        current = getattr(self, last_ref)
        if current == value:
            return

        prev = current
        setattr(self, last_ref, value)

        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%.4f - %s",
                entity_id,
                value,
                err,
            )
            setattr(self, last_ref, prev)

    async def _async_write_target(self, target_w: float, reason: str) -> None:
        """Write target_ev_w to HA helper every cycle (no skip — ensures HA reflects Brain).

        v0.4.2-P3: Always write regardless of previous value. HA state can be
        changed externally (manual set, restart) — Brain must re-assert each cycle.
        """
        prev = self._last_target_w
        self._last_target_w = target_w

        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": ENTITY_TARGET_EV_W, "value": target_w},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%.0fW - %s",
                ENTITY_TARGET_EV_W,
                target_w,
                err,
            )
            self._last_target_w = prev
            return

        _LOGGER.info("Brain -> target_ev_w=%.0fW  reason=%s", target_w, reason)

    async def _async_write_bat_target(self, target_bat_w: float, reason: str) -> None:
        """Write target_bat_w to HA helper every cycle (no skip — ensures HA reflects Brain).

        v0.4.2-P3: Always write regardless of previous value.
        IT-4218 Spec 1.6 Batch B: tracks consecutive write-fails; self-heals at 3.
        """
        prev = self._last_bat_w
        self._last_bat_w = target_bat_w

        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": ENTITY_TARGET_BAT_W, "value": target_bat_w},
                blocking=False,
            )
            self._consecutive_write_fails = 0
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%.0fW - %s",
                ENTITY_TARGET_BAT_W,
                target_bat_w,
                err,
            )
            self._last_bat_w = prev
            self._consecutive_write_fails += 1
            if self._consecutive_write_fails >= 3:
                _LOGGER.error(
                    "Brain: %d consecutive write-fails — resetting _last_target_bat_w to 0 (self-heal)",
                    self._consecutive_write_fails,
                )
                self._last_target_bat_w = 0.0
                self._consecutive_write_fails = 0
            return

        _LOGGER.info("Brain -> target_bat_w=%.0fW  reason=%s", target_bat_w, reason)

    async def _async_write_cascade_reason(self, reason: str) -> None:
        """Write cascade_reason to HA input_text every cycle — ensures last_changed updates."""
        prev = self._last_cascade_reason
        self._last_cascade_reason = reason

        try:
            await self._hass.services.async_call(
                "input_text",
                "set_value",
                {"entity_id": ENTITY_CASCADE_REASON, "value": reason},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%r - %s",
                ENTITY_CASCADE_REASON,
                reason,
                err,
            )
            self._last_cascade_reason = prev
            return

        _LOGGER.debug("Brain -> cascade_reason=%s", reason)

    async def _async_write_buffer_outputs(self, snapshot: BufferSnapshot) -> None:
        """Publish buffer snapshot values to HA helpers (epsilon-idempotent).

        Skips writes when value change < BUFFER_WRITE_EPSILON_KWH to avoid
        flooding the HA event bus on every 5s tick.
        """
        # bat_need_kwh
        if (
            self._last_bat_need_kwh is None
            or abs(snapshot.bat_need_kwh - self._last_bat_need_kwh) >= BUFFER_WRITE_EPSILON_KWH
        ):
            prev = self._last_bat_need_kwh
            self._last_bat_need_kwh = snapshot.bat_need_kwh
            try:
                await self._hass.services.async_call(
                    "input_number",
                    "set_value",
                    {
                        "entity_id": ENTITY_BRAIN_BAT_NEED_KWH,
                        "value": round(snapshot.bat_need_kwh, 2),
                    },
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.error("Brain: failed to write %s - %s", ENTITY_BRAIN_BAT_NEED_KWH, err)
                self._last_bat_need_kwh = prev

        # pv_surplus_remaining_kwh
        if (
            self._last_pv_surplus_kwh is None
            or abs(snapshot.pv_surplus_remaining_kwh - self._last_pv_surplus_kwh)
            >= BUFFER_WRITE_EPSILON_KWH
        ):
            prev = self._last_pv_surplus_kwh
            self._last_pv_surplus_kwh = snapshot.pv_surplus_remaining_kwh
            try:
                await self._hass.services.async_call(
                    "input_number",
                    "set_value",
                    {
                        "entity_id": ENTITY_BRAIN_PV_SURPLUS_REMAINING_KWH,
                        "value": round(snapshot.pv_surplus_remaining_kwh, 2),
                    },
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.error(
                    "Brain: failed to write %s - %s", ENTITY_BRAIN_PV_SURPLUS_REMAINING_KWH, err
                )
                self._last_pv_surplus_kwh = prev

        # buffer_kwh
        if (
            self._last_buffer_kwh is None
            or abs(snapshot.buffer_kwh - self._last_buffer_kwh) >= BUFFER_WRITE_EPSILON_KWH
        ):
            prev = self._last_buffer_kwh
            self._last_buffer_kwh = snapshot.buffer_kwh
            try:
                await self._hass.services.async_call(
                    "input_number",
                    "set_value",
                    {
                        "entity_id": ENTITY_BRAIN_PV_BUFFER_KWH,
                        "value": round(snapshot.buffer_kwh, 2),
                    },
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.error("Brain: failed to write %s - %s", ENTITY_BRAIN_PV_BUFFER_KWH, err)
                self._last_buffer_kwh = prev

        # strategy (string — always write on change)
        if self._last_strategy != snapshot.strategy:
            prev_s = self._last_strategy
            self._last_strategy = snapshot.strategy
            try:
                await self._hass.services.async_call(
                    "input_text",
                    "set_value",
                    {"entity_id": ENTITY_BRAIN_STRATEGY, "value": snapshot.strategy},
                    blocking=False,
                )
                _LOGGER.info(
                    "Brain -> strategy=%s buffer_kwh=%.2f", snapshot.strategy, snapshot.buffer_kwh
                )
            except Exception as err:
                _LOGGER.error("Brain: failed to write %s - %s", ENTITY_BRAIN_STRATEGY, err)
                self._last_strategy = prev_s

    def _bat_mode_routing(self, computed_bat_w: float) -> tuple[float, str]:
        """Route bat offer through MANUAL/SHADOW/AUTO mode. Brain = MASTER."""
        mode_state = self._hass.states.get(ENTITY_BAT_BALANCER_MODE)
        if mode_state and mode_state.state not in ("unknown", "unavailable"):
            mode = mode_state.state
        else:
            mode = "AUTO"
        if mode == "MANUAL":
            target_state = self._hass.states.get(ENTITY_BAT_BALANCER_TARGET_MANUAL_W)
            if target_state and target_state.state not in ("unknown", "unavailable"):
                try:
                    return float(target_state.state), "MANUAL"
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "brain: manual target entity state %r is not a valid float — falling back to 0.0",
                        target_state.state,
                    )
            return 0.0, "MANUAL"
        if mode == "SHADOW":
            return 0.0, "SHADOW"
        # AUTO: Borje 2026-05-22 16:20 — Feed-forward v5 (symmetrisk surplus + support).
        # new_offer = bat_actual_HW + grid_5min_avg
        # Symmetrisk: NEG offer = bat charges (PV-surplus), POS offer = bat dischargar (täcker import).
        # När solen går ner och vi importerar → Brain ber bat support huslast.
        # Deadband 100W: vid |5min_avg| < 100W HÅLLS offer (= 0±100W KPI).
        F2_OFFER_FLOOR_W = -10000.0  # max charge cap
        F2_OFFER_CEIL_W = 10000.0  # max discharge cap (= total HW bat)
        F2_DEADBAND_W = 100.0
        current_offer = 0.0
        offer_state = self._hass.states.get(ENTITY_TARGET_BAT_W)
        if offer_state and offer_state.state not in ("unknown", "unavailable"):
            try:
                current_offer = float(offer_state.state)
            except (TypeError, ValueError):
                current_offer = 0.0
        bat_actual = 0.0
        for entity in ("sensor.goodwe_battery_power_kontor", "sensor.goodwe_battery_power_forrad"):
            s = self._hass.states.get(entity)
            if s and s.state not in ("unknown", "unavailable"):
                with contextlib.suppress(TypeError, ValueError):
                    bat_actual += float(s.state)
        grid_state = self._hass.states.get("sensor.brain_grid_w_5min_avg")
        if grid_state and grid_state.state not in ("unknown", "unavailable"):
            try:
                grid_5min = float(grid_state.state)
                if abs(grid_5min) < F2_DEADBAND_W:
                    return current_offer, "AUTO"  # deadband: hold
                new_offer = bat_actual + grid_5min
                # Conservative fallback 0.0: if SoC unavailable, assume empty → block discharge.
                bat_soc = self._state_float(ENTITY_BAT_AVG_SOC_PCT, 0.0)
                # F2 discharge floor guard (mirrors cascade._compute_bat_grid_target:82).
                # Prevents discharge offer when at SoC floor — eliminates W2 false alarm.
                if new_offer > 0.0:
                    bat_floor = self._state_float(
                        ENTITY_BAT_FLOOR_DAY_PCT, DEFAULT_BAT_FLOOR_DAY_PCT
                    )
                    if bat_soc <= bat_floor:
                        _LOGGER.debug(
                            "F2-floor-block: soc=%.1f%% <= floor=%.1f%% → hold 0W",
                            bat_soc,
                            bat_floor,
                        )
                        new_offer = 0.0
                # 0-vision enforcement (Borje 2026-06-01): vid PV-export + bat ej full
                # → tvinga charge oavsett F2-cancellation (bat_discharge + grid_export ≈ 0).
                # F2_0VISION_ABSORB_MARGIN_W = 50W (definierat i const.py).
                if grid_5min < -F2_DEADBAND_W and bat_soc < DEFAULT_BAT_SOC_CHARGE_CEILING_PCT:
                    min_charge_offer = -(abs(grid_5min) + 50.0)
                    new_offer = min(new_offer, min_charge_offer)
                    charge_cap = self._bat_max_charge_w_now()
                    if charge_cap > 0.0:
                        new_offer = max(new_offer, -charge_cap)
                    _LOGGER.info(
                        "0-vision-enforce: grid=%.0fW bat=%.1f%% offer=%.0fW",
                        grid_5min,
                        bat_soc,
                        new_offer,
                    )
                # Symmetrisk clamp: tillåt både charge (neg) och discharge (pos)
                new_offer = max(F2_OFFER_FLOOR_W, min(F2_OFFER_CEIL_W, new_offer))
                return new_offer, "AUTO"
            except (TypeError, ValueError):
                _LOGGER.warning("brain: AUTO offer computation failed on invalid numeric input — falling back to 0.0")
        return 0.0, "AUTO"

    async def _async_write_bat_offer_source(self, source: str) -> None:
        """Write brain_bat_offer_source — idempotent, skips if unchanged."""
        if source == self._last_bat_offer_source:
            return
        try:
            await self._hass.services.async_call(
                "input_text",
                "set_value",
                {"entity_id": ENTITY_BAT_OFFER_SOURCE, "value": source},
                blocking=False,
            )
            self._last_bat_offer_source = source
        except Exception as err:
            _LOGGER.warning("Brain: bat_offer_source write failed: %s", err)

    async def _async_write_pv2_mode(self, target_bat_w: float, ev_connected: bool) -> None:
        """Map target_bat_w to pv2_manual_mode and write both inverters.

        Brain owns only Charge and Discharge; deadband → Auto lets GoodWe/v2.9 manage freely.
        Standby is never written — it triggers CARMA manual_override and blocks bat charging
        even when PV surplus is available (live incident 2026-05-11).
        Mapping (ZG-13 — threshold defined as PV2_MODE_DEADBAND_W = 100W):
          target_bat_w < -100W  → Charge
          target_bat_w > +100W  → Discharge
          otherwise             → Auto
        """
        if target_bat_w < -PV2_MODE_DEADBAND_W:
            mode = PV2_MODE_CHARGE
        elif target_bat_w > PV2_MODE_DEADBAND_W:
            mode = PV2_MODE_DISCHARGE
        else:
            mode = PV2_MODE_AUTO

        await self._async_set_pv2_mode(
            ENTITY_PV2_MANUAL_MODE_KONTOR, ENTITY_PV2_REASON_KONTOR, mode
        )
        await self._async_set_pv2_mode(
            ENTITY_PV2_MANUAL_MODE_FORRAD, ENTITY_PV2_REASON_FORRAD, mode
        )

    async def _async_set_pv2_mode(self, mode_entity: str, reason_entity: str, mode: str) -> None:
        """Write a single pv2_manual_mode inverter — idempotent, respects manual override."""
        reason_state = self._hass.states.get(reason_entity)
        if reason_state is not None and "Manuellt" in reason_state.state:
            _LOGGER.info(
                "Brain: %s skipped — manual override active (%s)",
                mode_entity,
                reason_state.state,
            )
            return

        if self._last_pv2_modes.get(mode_entity) == mode:
            return

        prev = self._last_pv2_modes.get(mode_entity)
        self._last_pv2_modes[mode_entity] = mode

        try:
            await self._hass.services.async_call(
                "input_select",
                "select_option",
                {"entity_id": mode_entity, "option": mode},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%s - %s",
                mode_entity,
                mode,
                err,
            )
            self._last_pv2_modes[mode_entity] = prev
            return

        _LOGGER.info("Brain -> %s=%s", mode_entity, mode)

    def _bat_can_engage(self) -> bool:
        """Read can_engage from bat_balancer_capability; fallback to SoC guard."""
        state = self._hass.states.get(ENTITY_BAT_BALANCER_CAPABILITY)
        if state is None or state.state in ("unavailable", "unknown"):
            # bat_balancer unavailable — use SoC as proxy (bat can engage if charged)
            soc = self._state_float(ENTITY_BAT_AVG_SOC_PCT, 0.0)
            loan_min_soc = self._state_float(ENTITY_BAT_LOAN_MIN_SOC_PCT, BAT_LOAN_MIN_SOC_PCT)
            return soc > loan_min_soc
        return bool(state.attributes.get("can_engage", False))

    def _bat_max_discharge_w_now(self) -> float:
        """Read max_w_now from bat_balancer_capability; fallback to SoC-gated constant."""
        state = self._hass.states.get(ENTITY_BAT_BALANCER_CAPABILITY)
        if state is None or state.state in ("unavailable", "unknown"):
            # bat_balancer unavailable — use constant when SoC allows discharge
            soc = self._state_float(ENTITY_BAT_AVG_SOC_PCT, 0.0)
            loan_min_soc = self._state_float(ENTITY_BAT_LOAN_MIN_SOC_PCT, BAT_LOAN_MIN_SOC_PCT)
            return BAT_MAX_DISCHARGE_FALLBACK_W if soc > loan_min_soc else 0.0
        try:
            return float(state.attributes.get("max_w_now", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _bat_max_charge_w_now(self) -> float:
        """Read max_charge_w_now from bat_balancer_capability; fallback to discharge cap."""
        state = self._hass.states.get(ENTITY_BAT_BALANCER_CAPABILITY)
        if state is None or state.state in ("unavailable", "unknown"):
            return 0.0
        try:
            val = state.attributes.get("max_charge_w_now")
            if val is not None:
                return float(val)
            return float(state.attributes.get("max_w_now", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _ev_connected(self) -> bool:
        """Return True if an EV cable is physically connected."""
        state = self._hass.states.get(self._ev_status_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "EV entity %s unavailable - assuming disconnected",
                self._ev_status_entity,
            )
            return False
        val = state.state.lower()
        if val in ("on", "true"):
            return True
        return val in EASEE_CONNECTED_STATES

    def _update_ev_cooldown(self, ev_connected: bool) -> tuple[bool, float]:
        """Track EV plug-in events and return (cooldown_active, remaining_s).

        Detects false→true transition on ev_connected to start cooldown timer.
        Cooldown prevents immediate EV allocation after plug-in (spike protection).
        Timer resets automatically when EV is unplugged.
        """
        import time as _time

        cooldown_s = self._state_float(ENTITY_EV_PLUGIN_COOLDOWN_S, DEFAULT_EV_PLUGIN_COOLDOWN_S)
        now_s = _time.monotonic()

        if self._prev_ev_connected is not None and not self._prev_ev_connected and ev_connected:
            self._ev_plugin_detected_at = now_s  # genuine plug-in transition: start timer
        elif not ev_connected:
            self._ev_plugin_detected_at = None  # unplugged: reset

        self._prev_ev_connected = ev_connected

        if self._ev_plugin_detected_at is not None:
            elapsed = now_s - self._ev_plugin_detected_at
            remaining = max(0.0, cooldown_s - elapsed)
            if remaining > 0.0:
                return True, remaining
        return False, 0.0

    def _in_evening_window(self) -> bool:
        """Return True if current local time is in evening window (17:00-21:59)."""
        return 17 <= dt_util.now().hour < 22

    def _in_morning_window(self) -> bool:
        """Return True if current time is after night_end and before surplus_start_time."""
        if self._in_night_window():
            return False
        now = datetime.now().time()
        surplus_start = self._parse_time_entity(
            ENTITY_SURPLUS_START_TIME, DEFAULT_SURPLUS_START_HOUR
        )
        return now < surplus_start

    def _in_after_surplus_window(self) -> bool:
        """Return True if current time is after surplus_end_time and before night_start."""
        if self._in_night_window():
            return False
        now = datetime.now().time()
        surplus_end = self._parse_time_entity(ENTITY_SURPLUS_END_TIME, DEFAULT_SURPLUS_END_HOUR)
        return now >= surplus_end

    def _compute_pv_surplus_likely(self) -> bool:
        """Return True if tomorrow's PV forecast meets or exceeds the threshold.

        Reads sensor.solcast_pv_forecast_forecast_tomorrow (kWh).
        Conservative fail-safe: returns False when entity unavailable (→ grid-charge).
        """
        forecast_kwh = self._state_float_or_none(ENTITY_PV_FORECAST_TOMORROW_KWH)
        if forecast_kwh is None:
            return False
        threshold = self._state_float(ENTITY_BAT_FORECAST_THRESHOLD_KWH, BAT_FORECAST_THRESHOLD_KWH)
        return forecast_kwh >= threshold

    def _in_night_window(self) -> bool:
        """Return True if the current wall-clock time falls inside the night window."""
        now = datetime.now().time()
        start = self._parse_time_entity(ENTITY_NIGHT_CHARGE_START, DEFAULT_NIGHT_START_HOUR)
        end = self._parse_time_entity(ENTITY_NIGHT_CHARGE_END, DEFAULT_NIGHT_END_HOUR)
        return _night_window_active(now, start, end)

    def _parse_time_entity(self, entity_id: str, default_hour: int) -> time:
        """Parse a time-only input_datetime entity state ("HH:MM:SS") to a time object."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", "none", ""):
            return time(default_hour, 0)
        try:
            parts = state.state.split(":")
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return time(default_hour, 0)

    def _state_float(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA — return default if unavailable or non-numeric."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", "none", ""):
            return default
        try:
            return float(state.state)
        except ValueError:
            return default

    def _state_bool(self, entity_id: str, default: bool = False) -> bool:
        """Read boolean state from HA — return default if entity missing."""
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return state.state.lower() in ("on", "true", "1")

    async def _append_decision_log(
        self,
        target_ev_w: float,
        reason: str,
        target_bat_w: float = 0.0,
        snapshot: BufferSnapshot | None = None,
    ) -> None:
        """Append a JSONL entry to the decision audit log (P1-C executor).

        File I/O runs in executor thread so NFS/slow-disk hangs cannot block
        the asyncio event loop. 3s timeout prevents tick delay on disk issues.
        """

        def _write_log_sync(path_str: str, entry_json: str) -> None:
            path = Path(path_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry_json + "\n")

        try:
            entry: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "target_ev_w": target_ev_w,
                "target_bat_w": target_bat_w,
                "reason": reason,
            }
            if snapshot is not None:
                entry["buffer_strategy"] = snapshot.strategy
                entry["buffer_kwh"] = round(snapshot.buffer_kwh, 3)
                entry["bat_need_kwh"] = round(snapshot.bat_need_kwh, 3)
                entry["pv_surplus_kwh"] = round(snapshot.pv_surplus_remaining_kwh, 3)
            await asyncio.wait_for(
                self._hass.async_add_executor_job(
                    _write_log_sync, self._decision_log_path, json.dumps(entry)
                ),
                timeout=3.0,
            )
        except (TimeoutError, OSError):
            pass  # Decision log is best-effort; control loop must never crash

    async def _heartbeat_supervisor(self) -> None:  # pragma: no cover
        """IT-4218 Spec 1.9 P2-F: Last-line-of-defense — re-register tick timer if frozen.

        Checks _cycle_id every 60s (two 30s sleeps). If it hasn't advanced,
        the tick loop is frozen and the timer is re-registered without creating
        a new supervisor task (avoids cascading duplicates).
        """
        from homeassistant.helpers.event import async_track_time_interval

        while not self._stopping:
            await asyncio.sleep(30)
            if self._stopping:
                break
            if self._unsub is None:
                _LOGGER.critical(
                    "Brain: heartbeat detected _unsub=None — re-registering tick timer"
                )
                self._unsub = async_track_time_interval(self._hass, self._async_tick, SCAN_INTERVAL)
                continue
            last_cycle = self._cycle_id
            await asyncio.sleep(30)
            if self._cycle_id == last_cycle:
                _LOGGER.critical(
                    "Brain: heartbeat detected frozen ticks (60s, cycle_id=%d unchanged)"
                    " — re-registering tick timer",
                    last_cycle,
                )
                if self._unsub is not None:
                    self._unsub()
                    self._unsub = None
                self._unsub = async_track_time_interval(self._hass, self._async_tick, SCAN_INTERVAL)
