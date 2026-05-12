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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

from .const import (
    BUFFER_WRITE_EPSILON_KWH,
    DEADBAND_W,
    DEFAULT_BAT_CAPACITY_KWH,
    DEFAULT_DEADBAND_W,
    DEFAULT_EV_BAT_SUPPORT_MIN_SOC,
    DEFAULT_EV_MIN_CHARGE_W,
    DEFAULT_EV_PLUGIN_COOLDOWN_S,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_FORCE_EV_AMPS,
    DEFAULT_GRID_SAFETY_MARGIN_W,
    DEFAULT_HOUSE_BASELINE_KW,
    DEFAULT_NIGHT_END_HOUR,
    DEFAULT_NIGHT_START_HOUR,
    DEFAULT_NON_CARMA_ANTICIPATION_W,
    EASEE_CONNECTED_STATES,
    ENTITY_BAT_AVG_SOC_PCT,
    ENTITY_BAT_BALANCER_CAPABILITY,
    ENTITY_BAT_CAPACITY_FORRAD,
    ENTITY_BAT_CAPACITY_KONTOR,
    ENTITY_BAT_SUPPORT_ENABLED,
    ENTITY_BRAIN_BAT_NEED_KWH,
    ENTITY_BRAIN_GRID_SAFETY_MARGIN_W,
    ENTITY_BRAIN_NON_CARMA_ANTICIPATION_W,
    ENTITY_BRAIN_PV_BUFFER_KWH,
    ENTITY_BRAIN_PV_SURPLUS_REMAINING_KWH,
    ENTITY_BRAIN_STRATEGY,
    ENTITY_BUFFER_GATE_ENABLED,
    ENTITY_CASCADE_REASON,
    ENTITY_DEADBAND_W,
    ENTITY_ELLEVIO_DYNAMISKT_TAK,
    ENTITY_EV_BAT_SUPPORT_MIN_SOC,
    ENTITY_EV_MIN_CHARGE_W,
    ENTITY_EV_PLUGIN_COOLDOWN_S,
    ENTITY_EV_SOC,
    ENTITY_EV_TARGET_SOC,
    ENTITY_FORCE_EV_ACTIVE,
    ENTITY_FORCE_EV_AMPS,
    ENTITY_GRID_POWER,
    ENTITY_HOUSE_BASELINE_KW,
    ENTITY_HOUSE_TOTAL_W,
    ENTITY_NIGHT_CHARGE_END,
    ENTITY_NIGHT_CHARGE_START,
    ENTITY_PS2_PEAK_SHAVING_LIMIT,
    ENTITY_PV2_MANUAL_MODE_FORRAD,
    ENTITY_PV2_MANUAL_MODE_KONTOR,
    ENTITY_PV2_REASON_FORRAD,
    ENTITY_PV2_REASON_KONTOR,
    ENTITY_PV_REMAINING_KWH,
    ENTITY_SUN_SUN,
    ENTITY_TARGET_BAT_W,
    ENTITY_TARGET_EV_W,
    PHASES,
    PV2_MODE_AUTO,
    PV2_MODE_CHARGE,
    PV2_MODE_DEADBAND_W,
    PV2_MODE_DISCHARGE,
    SCAN_INTERVAL,
    STRATEGY_BAT_PRIORITY,
    VOLTAGE_V,
)
from .pv_buffer import BufferSnapshot, compute_buffer

_LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class BrainOutput:
    """Immutable result of one brain compute cycle.

    target_ev_w: requested EV charge power in watts (≥ 0)
    target_bat_w: requested bat discharge power in watts (≤ 0 = discharge, 0 = idle)
    reason: short machine-readable decision label for logging and dashboard
    """

    target_ev_w: float
    target_bat_w: float
    reason: str


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


class BrainController:
    """Reads HA state, runs compute_targets (v0.3), writes all outputs to HA.

    v0.3.1: _async_tick now calls compute_targets() and writes:
      - target_ev_w → input_number.brain_target_ev_w
      - target_bat_w → input_number.brain_target_bat_w
      - cascade_reason → input_text.brain_cascade_reason
      - pv2_manual_mode_{kontor,forrad} → mapped from target_bat_w + ev_connected

    v0.4: buffer snapshot computed before cascade, 4 buffer outputs published.
    All writes are idempotent (skip if value unchanged). Manual pv2 override is
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
        # v0.4.1-B: EV plug-in cooldown state
        self._ev_plugin_detected_at: float | None = None
        self._prev_ev_connected: bool | None = None  # None = first tick (no history)
        self._unsub: Any = None

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

    def stop(self) -> None:  # pragma: no cover
        """Unregister the polling loop."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
            _LOGGER.info("Brain v0.4 stopped")

    async def _async_tick(self, _now: Any) -> None:
        """Execute one brain tick: buffer_snapshot → read → compute_targets → write all outputs."""
        from .cascade import compute_targets  # deferred: cascade imports from brain

        # v0.4: Compute buffer snapshot FIRST, then inject into cascade via BrainInput
        snapshot = self._compute_buffer_snapshot()
        inp = self._read_inputs(snapshot)
        out = compute_targets(inp)
        await self._async_write_target(out.target_ev_w, out.reason)
        await self._async_write_bat_target(out.target_bat_w, out.reason)
        await self._async_write_cascade_reason(out.reason)
        await self._async_write_pv2_mode(out.target_bat_w, inp.ev_connected)
        await self._async_write_buffer_outputs(snapshot)
        self._append_decision_log(out.target_ev_w, out.reason, out.target_bat_w, snapshot)

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
            ellevio_tak_w=self._state_float_or_none(ENTITY_ELLEVIO_DYNAMISKT_TAK) or 0.0,
            ps2_limit_w=self._state_float_or_none(ENTITY_PS2_PEAK_SHAVING_LIMIT) or 0.0,
            grid_safety_margin_w=self._state_float(
                ENTITY_BRAIN_GRID_SAFETY_MARGIN_W, DEFAULT_GRID_SAFETY_MARGIN_W
            ),
            ev_plugin_cooldown_active=_ev_cooldown[0],
            ev_plugin_cooldown_remaining_s=_ev_cooldown[1],
            non_carma_anticipation_w=self._state_float(
                ENTITY_BRAIN_NON_CARMA_ANTICIPATION_W, DEFAULT_NON_CARMA_ANTICIPATION_W
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
                blocking=True,
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
        """Write target_ev_w to HA helper — skips if identical (idempotent)."""
        if self._last_target_w == target_w:
            return

        prev = self._last_target_w
        self._last_target_w = target_w

        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": ENTITY_TARGET_EV_W, "value": target_w},
                blocking=True,
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
        """Write target_bat_w to HA helper — skips if identical (idempotent)."""
        if self._last_bat_w == target_bat_w:
            return

        prev = self._last_bat_w
        self._last_bat_w = target_bat_w

        try:
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": ENTITY_TARGET_BAT_W, "value": target_bat_w},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error(
                "Brain: failed to write %s=%.0fW - %s",
                ENTITY_TARGET_BAT_W,
                target_bat_w,
                err,
            )
            self._last_bat_w = prev
            return

        _LOGGER.info("Brain -> target_bat_w=%.0fW  reason=%s", target_bat_w, reason)

    async def _async_write_cascade_reason(self, reason: str) -> None:
        """Write cascade_reason to HA input_text — skips if identical (idempotent)."""
        if self._last_cascade_reason == reason:
            return

        prev = self._last_cascade_reason
        self._last_cascade_reason = reason

        try:
            await self._hass.services.async_call(
                "input_text",
                "set_value",
                {"entity_id": ENTITY_CASCADE_REASON, "value": reason},
                blocking=True,
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
                    blocking=True,
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
                    blocking=True,
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
                    blocking=True,
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
                    blocking=True,
                )
                _LOGGER.info(
                    "Brain -> strategy=%s buffer_kwh=%.2f", snapshot.strategy, snapshot.buffer_kwh
                )
            except Exception as err:
                _LOGGER.error("Brain: failed to write %s - %s", ENTITY_BRAIN_STRATEGY, err)
                self._last_strategy = prev_s

    async def _async_write_pv2_mode(self, target_bat_w: float, ev_connected: bool) -> None:
        """Map target_bat_w to pv2_manual_mode and write both inverters.

        Brain owns only Charge and Discharge; deadband → Auto lets GoodWe/v2.9 manage freely.
        Standby is never written — it triggers CARMA manual_override and blocks bat charging
        even when PV surplus is available (live incident 2026-05-11).
        Mapping (ZG-13 — threshold defined as PV2_MODE_DEADBAND_W = 100W):
          target_bat_w > +100W  → Charge
          target_bat_w < -100W  → Discharge
          otherwise             → Auto
        """
        if target_bat_w > PV2_MODE_DEADBAND_W:
            mode = PV2_MODE_CHARGE
        elif target_bat_w < -PV2_MODE_DEADBAND_W:
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
                blocking=True,
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
        """Read can_engage attribute from bat_balancer_capability sensor."""
        state = self._hass.states.get(ENTITY_BAT_BALANCER_CAPABILITY)
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        return bool(state.attributes.get("can_engage", False))

    def _bat_max_discharge_w_now(self) -> float:
        """Read max_w_now attribute from bat_balancer_capability sensor."""
        state = self._hass.states.get(ENTITY_BAT_BALANCER_CAPABILITY)
        if state is None or state.state in ("unavailable", "unknown"):
            return 0.0
        try:
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

    def _append_decision_log(
        self,
        target_ev_w: float,
        reason: str,
        target_bat_w: float = 0.0,
        snapshot: BufferSnapshot | None = None,
    ) -> None:
        """Append a JSONL entry to the decision audit log.

        Best-effort: OSError is silently ignored so log failures never
        interrupt the control loop.
        """
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
            log_path = Path(self._decision_log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # Decision log is best-effort; control loop must never crash
