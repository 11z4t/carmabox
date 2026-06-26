"""Coordinator for Binary Balancer."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACTION_HELPER_TMPL,
    CONF_ASSET_ID,
    CONF_HYSTERESIS_OFF_PCT,
    CONF_HYSTERESIS_ON_PCT,
    CONF_MIN_DWELL_S,
    CONF_SEASON_ACTIVE_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_TYPICAL_DRAG_W,
    COORDINATOR_INTERVAL_S,
    DECISION_LOG_MAX_BYTES,
    DECISION_LOG_PATH_TMPL,
    DEFAULT_HYSTERESIS_OFF_PCT,
    DEFAULT_HYSTERESIS_ON_PCT,
    DEFAULT_MIN_DWELL_S,
    DOMAIN,
    FEEDBACK_HELPER_TMPL,
    OFFLINE_CYCLES_THRESHOLD,
    OVERRIDE_MODE_HELPER_TMPL,
)
from .models import (
    ActionMessage,
    AssetConfig,
    BalancerState,
    FaultState,
    FeedbackMessage,
    SensorSnapshot,
    SwitchAction,
)
from .translator import parse_action_message, translate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class BinaryBalancerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls Brain ACTION and drives a binary switch asset."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise coordinator from config entry."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.data[CONF_ASSET_ID]}",
            update_interval=timedelta(seconds=COORDINATOR_INTERVAL_S),
        )
        self._entry = entry
        self._config = self._build_config(entry)
        self._state = BalancerState()
        self._last_fault_state: FaultState = FaultState.OK
        self._last_action: SwitchAction = SwitchAction.HOLD
        self._uptime_start_ts: float | None = None
        self._log_path = DECISION_LOG_PATH_TMPL.format(asset_id=self._config.asset_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_config(entry: ConfigEntry) -> AssetConfig:
        """Build AssetConfig from a ConfigEntry."""
        data = entry.data
        return AssetConfig(
            asset_id=data[CONF_ASSET_ID],
            typical_drag_w=float(data[CONF_TYPICAL_DRAG_W]),
            switch_entity=data[CONF_SWITCH_ENTITY],
            season_active_entity=data.get(CONF_SEASON_ACTIVE_ENTITY),
            min_dwell_s=int(data.get(CONF_MIN_DWELL_S, DEFAULT_MIN_DWELL_S)),
            hysteresis_on_pct=float(data.get(CONF_HYSTERESIS_ON_PCT, DEFAULT_HYSTERESIS_ON_PCT)),
            hysteresis_off_pct=float(data.get(CONF_HYSTERESIS_OFF_PCT, DEFAULT_HYSTERESIS_OFF_PCT)),
        )

    def _read_input_text(self, entity_id: str) -> str:
        """Read state of an input_text helper, returning empty string if unavailable."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return ""
        return state.state

    def _read_switch_state(self, entity_id: str) -> tuple[bool, bool]:
        """Return (is_on, is_unavailable) for a switch entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return False, True
        return state.state == "on", False

    def _read_season_active(self) -> bool:
        """Return True if season is active (no entity = always active)."""
        entity_id = self._config.season_active_entity
        if entity_id is None:
            return True
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state == "on"

    @staticmethod
    def _read_guardian_safe_state() -> bool:
        """Return True if global guardian safe state is active.

        This stub always returns False — override in production with
        a real guardian entity read when integrated.
        """
        return False

    def _compute_action_age_s(self, action: ActionMessage | None) -> float:
        """Return seconds since ACTION was published based on ts field."""
        if action is None:
            return float("inf")
        if not action.ts:
            return 0.0
        try:
            published = datetime.fromisoformat(action.ts)
            now = datetime.now(tz=UTC)
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            age = (now - published).total_seconds()
            return max(0.0, age)
        except ValueError:
            return 0.0

    def _build_snapshot(self, action: ActionMessage | None) -> SensorSnapshot:
        """Build SensorSnapshot from current HA state."""
        switch_on, switch_unavailable = self._read_switch_state(self._config.switch_entity)
        return SensorSnapshot(
            switch_is_on=switch_on,
            switch_unavailable=switch_unavailable,
            season_active=self._read_season_active(),
            guardian_safe_state=self._read_guardian_safe_state(),
            action_age_s=self._compute_action_age_s(action),
        )

    def _update_state(
        self, result_action: SwitchAction, result_fault: FaultState, now_ts: float
    ) -> BalancerState:
        """Return updated BalancerState after applying translate result."""
        new_switch_on = self._state.switch_on
        new_last_change_ts = self._state.last_change_ts
        new_offline_cycles = self._state.offline_cycles
        new_stale_cycles = self._state.stale_cycles

        if result_action == SwitchAction.TURN_ON:
            new_switch_on = True
            new_last_change_ts = now_ts
            self._uptime_start_ts = now_ts
        elif result_action == SwitchAction.TURN_OFF:
            new_switch_on = False
            new_last_change_ts = now_ts
            self._uptime_start_ts = None

        if result_fault == FaultState.OFFLINE:
            new_offline_cycles = self._state.offline_cycles + 1
        else:
            new_offline_cycles = 0

        # stale_cycles increments when HOLD due to stale action
        if result_fault == FaultState.OK and not new_switch_on:
            new_stale_cycles = self._state.stale_cycles + 1
        else:
            new_stale_cycles = 0

        uptime_s = 0.0
        if new_switch_on and self._uptime_start_ts is not None:
            uptime_s = now_ts - self._uptime_start_ts

        return BalancerState(
            switch_on=new_switch_on,
            last_change_ts=new_last_change_ts,
            last_cycle_id=self._state.last_cycle_id,
            offline_cycles=new_offline_cycles,
            uptime_s=uptime_s,
            stale_cycles=new_stale_cycles,
        )

    def _read_override_mode(self) -> str:
        """Return override mode from input_select helper — defaults to AUTO."""
        entity_id = OVERRIDE_MODE_HELPER_TMPL.format(asset_id=self._config.asset_id)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return "AUTO"
        return state.state.upper()

    async def _apply_switch_action(self, action: SwitchAction) -> None:
        """Call HA switch service if action is not HOLD."""
        if action == SwitchAction.HOLD:
            return
        # PRIO 1 (Borje 2026-06-26): MANUAL-mode = operator äger switchen. Balancer rör ALDRIG.
        if self._read_override_mode() == "MANUAL":
            _LOGGER.debug(
                "binary_balancer %s: MANUAL-mode → skip switch.%s (operator äger switchen)",
                self._config.asset_id,
                action.value,
            )
            return
        service = action.value  # "turn_on" or "turn_off"
        entity_id = self._config.switch_entity
        _LOGGER.debug("Binary balancer calling switch.%s on %s", service, entity_id)
        await self.hass.services.async_call(
            "switch",
            service,
            {"entity_id": entity_id},
            blocking=False,
        )

    async def _write_feedback(self, feedback: FeedbackMessage) -> None:
        """Write FEEDBACK JSON to HA input_text helper."""
        entity_id = FEEDBACK_HELPER_TMPL.format(asset_id=self._config.asset_id)
        json_str = feedback.to_json()
        if len(json_str) > 255:
            _LOGGER.warning("Feedback JSON exceeds 255 chars (%d): %s", len(json_str), json_str)
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.debug("Feedback entity %s not found - skipping write", entity_id)
            return
        await self.hass.services.async_call(
            "input_text",
            "set_value",
            {"entity_id": entity_id, "value": json_str},
            blocking=False,
        )

    def _write_jsonl_log(self, entry_dict: dict[str, Any]) -> None:
        """Append a JSONL decision log entry (with rotation)."""
        try:
            log_dir = os.path.dirname(self._log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            # Rotate if file exceeds max size
            file_exists = os.path.exists(self._log_path)
            if file_exists and os.path.getsize(self._log_path) >= DECISION_LOG_MAX_BYTES:
                rotated = self._log_path + ".1"
                os.replace(self._log_path, rotated)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry_dict) + "\n")
        except OSError as err:
            _LOGGER.warning("Failed to write JSONL log: %s", err)

    def _should_log(self, result_action: SwitchAction, fault_state: FaultState) -> bool:
        """Return True if this cycle should be logged (delta-based)."""
        if result_action != SwitchAction.HOLD:
            return True
        return fault_state != self._last_fault_state

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch Brain ACTION, translate, actuate, write FEEDBACK."""
        import time

        now_ts = time.monotonic()
        asset_id = self._config.asset_id

        # 1. Read ACTION from input_text helper
        action_entity = ACTION_HELPER_TMPL.format(asset_id=asset_id)
        raw_action = self._read_input_text(action_entity)
        action: ActionMessage | None = None
        if raw_action:
            action = parse_action_message(raw_action, asset_id)
            if action is None:
                _LOGGER.debug("Could not parse ACTION for %s: %s", asset_id, raw_action[:80])

        # 2. Build SensorSnapshot
        sensors = self._build_snapshot(action)

        # 3. Translate
        result = translate(
            action=action,
            config=self._config,
            sensors=sensors,
            state=self._state,
            now_ts=now_ts,
        )

        # 4. Apply switch action
        try:
            await self._apply_switch_action(result.action)
        except Exception as err:
            _LOGGER.error("Failed to apply switch action for %s: %s", asset_id, err)
            raise UpdateFailed(f"Switch action failed: {err}") from err

        # 5. Update state
        self._state = self._update_state(result.action, result.fault_state, now_ts)

        # 6. Write FEEDBACK
        now_iso = datetime.now(tz=UTC).isoformat()
        feedback = FeedbackMessage(
            asset_id=asset_id,
            actual_w=result.actual_w,
            fault_state=result.fault_state.value,
            fault_detail=result.fault_detail,
            cap_engage=result.cap_engage,
            cap_max_w=self._config.typical_drag_w,
            cap_min_w=0.0,
            rejected_w=result.rejected_w,
            rejected_reason=result.rejected_reason,
            stale_action=result.stale_action,
            ts=now_iso,
        )
        await self._write_feedback(feedback)

        # 7. Write JSONL log (delta-based)
        if self._should_log(result.action, result.fault_state):
            log_entry: dict[str, Any] = {
                "ts": now_iso,
                "cycle_id": self._state.last_cycle_id,
                "asset_id": asset_id,
                "input": {
                    "target_w": action.target_w if action else None,
                    "mode": action.mode if action else None,
                    "action_age_s": round(sensors.action_age_s, 2),
                },
                "output": {
                    "action": result.action.value,
                    "reason": result.reason,
                },
                "fault_state": result.fault_state.value,
                "actual_w": result.actual_w,
                "rejected_w": result.rejected_w,
                "stale_action": result.stale_action,
                "switch_on": self._state.switch_on,
            }
            self._write_jsonl_log(log_entry)

        # 8. Track last values for delta logging
        self._last_fault_state = result.fault_state
        self._last_action = result.action

        # 9. Return data dict for sensor entities
        offline_escalated = self._state.offline_cycles >= OFFLINE_CYCLES_THRESHOLD
        effective_fault = FaultState.OFFLINE if offline_escalated else result.fault_state
        return {
            "asset_id": asset_id,
            "status": effective_fault.value,
            "actual_w": result.actual_w,
            "fault_state": result.fault_state.value,
            "uptime_s": self._state.uptime_s,
            "stale_cycles": self._state.stale_cycles,
            "switch_on": self._state.switch_on,
            "reason": result.reason,
        }
