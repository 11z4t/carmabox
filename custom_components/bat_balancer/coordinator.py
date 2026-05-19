"""BatBalancerCoordinator — 5s tick, distributes brain_target_bat_w to GoodWe banks."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BANKS,
    BRAIN_STALE_THRESHOLD_S,
    ENTITY_BAT_BATTERY_MODE,
    ENTITY_BAT_CHARGE_MAX_W,
    ENTITY_BAT_DISCHARGE_MAX_W,
    ENTITY_BAT_SOC,
    ENTITY_BRAIN_TARGET_BAT_W,
    ENTITY_GOODWE_OPERATION_MODE,
    ENTITY_GOODWE_POWER_LIMIT,
    ENTITY_HOUSE_GRID_POWER,
    ENTITY_MIN_SOC_BUFFER_PCT,
    ENTITY_SHADOW_MODE,
    ENTITY_SOC_EQ_MAX_BIAS_W,
    ENTITY_SOC_EQ_THRESHOLD_PCT,
    GOODWE_MODE_BATTERY_STANDBY,
    GOODWE_MODE_PEAK_SHAVING,
    HW_STALE_THRESHOLD_S,
    SOC_EQ_MAX_BIAS_DEFAULT_W,
    SOC_EQ_THRESHOLD_DEFAULT_PCT,
    BatBalancerStatus,
)
from .distribution_engine import distribute_target_to_banks
from .models import BankConfig, BankState, BatBalancerState, SensorSnapshot
from .sign_state_machine import SignStateMachine

_LOGGER = logging.getLogger(__name__)

_GOODWE_SOC_FALLBACK = "sensor.goodwe_battery_state_of_charge_{bank_id}"
_GOODWE_CHARGE_W_FALLBACK = "sensor.goodwe_battery_power_{bank_id}"


class BatBalancerCoordinator(DataUpdateCoordinator):
    """Reads brain target, distributes to banks via GoodWe EMS power limits."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="bat_balancer",
            update_interval=timedelta(seconds=5),
        )
        self._entry = entry
        self._bank_configs: dict[str, BankConfig] = {
            "kontor": BankConfig.default_kontor(),
            "forrad": BankConfig.default_forrad(),
        }
        self._sign_machines: dict[str, SignStateMachine] = {
            bid: SignStateMachine() for bid in BANKS
        }
        self._state = BatBalancerState()
        self._last_brain_write_ts: float = 0.0
        self._last_goodwe_modes: dict[str, str | None] = {bid: None for bid in BANKS}
        self._last_goodwe_modes: dict[str, str | None] = {bid: None for bid in BANKS}

    # ── Public API (read by sensor.py) ──────────────────────────────────────

    @property
    def capability(self) -> dict:
        """Return dict exposed as sensor.bat_balancer_capability attributes."""
        bank_socs = []
        for bid in BANKS:
            bs = self._read_bank_state(bid)
            bank_socs.append(bs.current_soc)

        avg_soc = sum(bank_socs) / len(bank_socs) if bank_socs else 0.0
        min_soc = min(bank_socs) if bank_socs else 0.0

        # can_engage: any bank online + min_soc > buffer
        min_soc_buffer = self._float_helper(ENTITY_MIN_SOC_BUFFER_PCT, 20.0)
        any_online = any(self._read_bank_state(bid).is_online for bid in BANKS)
        can_engage = any_online and min_soc > min_soc_buffer

        # max_w_now: sum of discharge headroom across online banks
        max_discharge = sum(
            self._effective_discharge_w(bid)
            for bid in BANKS
            if self._read_bank_state(bid).is_online
        )
        max_charge = sum(
            self._effective_charge_w(bid) for bid in BANKS if self._read_bank_state(bid).is_online
        )

        status = str(self._state.status)
        if not any_online:
            status = str(BatBalancerStatus.OFFLINE_BANK)
            can_engage = False

        return {
            "can_engage": can_engage,
            "max_w_now": max_discharge if can_engage else 0.0,
            "max_charge_w_now": max_charge,
            "avg_soc": round(avg_soc, 1),
            "min_soc": round(min_soc, 1),
            "status": status,
            "sign_flip_pending": any(sm.sign_flip_pending for sm in self._sign_machines.values()),
            "last_distribution": dict(self._state.last_distribution),
        }

    @property
    def avg_soc_pct(self) -> float:
        """Weighted average SoC across all banks (for sensor.bat_balancer_avg_soc_pct)."""
        total_kwh = sum(bc.capacity_kwh for bc in self._bank_configs.values())
        weighted = sum(
            self._read_bank_state(bid).current_soc * self._bank_configs[bid].capacity_kwh
            for bid in BANKS
        )
        return round(weighted / total_kwh if total_kwh > 0 else 0.0, 1)

    # ── DataUpdateCoordinator hook ───────────────────────────────────────────

    async def _async_update_data(self):
        """Called every 5s by DataUpdateCoordinator."""
        try:
            await self._tick()
        except Exception as exc:
            _LOGGER.error("bat_balancer tick error: %s", exc, exc_info=True)
            self._state.status = BatBalancerStatus.ERROR
            raise UpdateFailed(str(exc)) from exc
        return self._state

    async def _tick(self) -> None:
        shadow = self._bool_state(ENTITY_SHADOW_MODE, default=True)

        # --- read brain target ---
        brain_entity = self.hass.states.get(ENTITY_BRAIN_TARGET_BAT_W)
        brain_available = False
        brain_target_w = 0.0
        if brain_entity and brain_entity.state not in ("unavailable", "unknown", None):
            try:
                brain_target_w = float(brain_entity.state)
                brain_available = True
                self._last_brain_write_ts = time.monotonic()
            except (TypeError, ValueError):
                pass

        # stale brain target → zero
        if brain_available:
            age = time.monotonic() - self._last_brain_write_ts
            if age > BRAIN_STALE_THRESHOLD_S:
                brain_target_w = 0.0
                brain_available = False

        # --- read bank states ---
        bank_states: dict[str, BankState] = {bid: self._read_bank_state(bid) for bid in BANKS}

        # --- build snapshot ---
        snapshot = SensorSnapshot(
            brain_target_bat_w=brain_target_w,
            banks=bank_states,
            house_grid_w=self._float_state(ENTITY_HOUSE_GRID_POWER, float("nan")),
            soc_equalization_threshold_pct=self._float_helper(
                ENTITY_SOC_EQ_THRESHOLD_PCT, SOC_EQ_THRESHOLD_DEFAULT_PCT
            ),
            soc_equalization_max_bias_w=self._float_helper(
                ENTITY_SOC_EQ_MAX_BIAS_W, SOC_EQ_MAX_BIAS_DEFAULT_W
            ),
            shadow_mode=shadow,
            brain_target_available=brain_available,
        )

        if not brain_available:
            self._state.status = BatBalancerStatus.OK
            self._state.last_target_w = 0.0
            if not shadow:
                # write 0 to all banks (idle)
                for bid in BANKS:
                    ticked = self._sign_machines[bid].tick(0.0)
                    await self._write_power_limit(bid, ticked)
            return

        # --- distribute ---
        result = distribute_target_to_banks(
            brain_target_w,
            self._bank_configs,
            bank_states,
            snapshot,
        )

        self._state.status = result.status
        self._state.last_target_w = brain_target_w
        self._state.last_distribution = result.targets
        self._state.equalization_active = result.equalization_active

        if shadow:
            self._state.status = BatBalancerStatus.SHADOW_MODE
            _LOGGER.debug(
                "bat_balancer: SHADOW — brain=%.0fW dist=%s",
                brain_target_w,
                {k: round(v) for k, v in result.targets.items()},
            )
            return

        # --- apply sign machine + write ---
        for bid in BANKS:
            raw_target = result.targets.get(bid, 0.0)
            ticked = self._sign_machines[bid].tick(raw_target)
            await self._write_power_limit(bid, ticked)

        _LOGGER.debug(
            "bat_balancer: tick brain=%.0fW targets=%s status=%s",
            brain_target_w,
            {k: round(v) for k, v in result.targets.items()},
            result.status,
        )

    async def _set_goodwe_mode(self, bank_id: str, mode: str) -> None:
        """Write GoodWe inverter operation mode — idempotent, fire-and-forget."""
        if self._last_goodwe_modes.get(bank_id) == mode:
            return
        prev = self._last_goodwe_modes.get(bank_id)
        self._last_goodwe_modes[bank_id] = mode
        entity_id = ENTITY_GOODWE_OPERATION_MODE.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": mode},
                blocking=False,
            )
            _LOGGER.debug("bat_balancer: goodwe_mode %s -> %s", bank_id, mode)
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed to set mode %s=%s: %s", entity_id, mode, exc)
            self._last_goodwe_modes[bank_id] = prev

    async def _write_power_limit(self, bank_id: str, target_w: float) -> None:
        """Write signed target_w to GoodWe: set operation mode then EMS power limit.

        Discharge (target_w < 0): battery_standby mode + abs(target_w) EMS limit.
        Charge/idle (target_w >= 0): peak_shaving mode + abs(target_w) EMS limit.
        Mode written BEFORE EMS limit. Mode writes are idempotent.
        """
        mode = GOODWE_MODE_BATTERY_STANDBY if target_w < 0 else GOODWE_MODE_PEAK_SHAVING
        await self._set_goodwe_mode(bank_id, mode)

        entity_id = ENTITY_GOODWE_POWER_LIMIT.format(bank_id=bank_id)
        magnitude = abs(target_w)
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(magnitude, 0)},
                blocking=False,
            )
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed to write %s=%.0fW: %s", entity_id, magnitude, exc)

    def shutdown(self) -> None:
        """Called on unload — reset sign machines."""
        for sm in self._sign_machines.values():
            sm.reset()

    # ── State readers ────────────────────────────────────────────────────────

    def _read_bank_state(self, bank_id: str) -> BankState:
        """Read live sensor data for one bank."""
        # SoC: try filtered first, fall back to GoodWe raw
        soc_entity = ENTITY_BAT_SOC.format(bank_id=bank_id)
        soc_state = self.hass.states.get(soc_entity)
        sensor_stale = False

        if soc_state is None or soc_state.state in ("unavailable", "unknown"):
            # fallback
            fallback_id = _GOODWE_SOC_FALLBACK.format(bank_id=bank_id)
            soc_state = self.hass.states.get(fallback_id)

        soc = 50.0  # safe default
        is_online = False
        if soc_state and soc_state.state not in ("unavailable", "unknown", None):
            try:
                soc = float(soc_state.state)
                is_online = True
                # stale check
                age_s = (
                    self.hass.loop.time() - soc_state.last_changed.timestamp()
                    if hasattr(soc_state.last_changed, "timestamp")
                    else 0.0
                )
                if age_s > HW_STALE_THRESHOLD_S:
                    sensor_stale = True
            except (TypeError, ValueError):
                pass

        # BMS dynamic caps (optional sensors)
        bms_charge: float | None = self._optional_float(
            ENTITY_BAT_CHARGE_MAX_W.format(bank_id=bank_id)
        )
        bms_discharge: float | None = self._optional_float(
            ENTITY_BAT_DISCHARGE_MAX_W.format(bank_id=bank_id)
        )

        battery_mode = self._str_state(
            ENTITY_BAT_BATTERY_MODE.format(bank_id=bank_id), "battery_standby"
        )

        return BankState(
            bank_id=bank_id,
            current_soc=soc,
            is_online=is_online,
            battery_mode=battery_mode,
            bms_max_charge_w=bms_charge,
            bms_max_discharge_w=bms_discharge,
            sensor_stale=sensor_stale,
        )

    def _effective_discharge_w(self, bank_id: str) -> float:
        bs = self._read_bank_state(bank_id)
        bc = self._bank_configs[bank_id]
        if bs.bms_max_discharge_w is not None and not bs.sensor_stale:
            return bs.bms_max_discharge_w
        return bc.max_discharge_w

    def _effective_charge_w(self, bank_id: str) -> float:
        bs = self._read_bank_state(bank_id)
        bc = self._bank_configs[bank_id]
        if bs.bms_max_charge_w is not None and not bs.sensor_stale:
            return bs.bms_max_charge_w
        return bc.max_charge_w

    # ── HA state helpers ────────────────────────────────────────────────────

    def _float_state(self, entity_id: str, default: float) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return default
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return default

    def _float_helper(self, entity_id: str, default: float) -> float:
        return self._float_state(entity_id, default)

    def _bool_state(self, entity_id: str, default: bool) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return default
        return state.state == "on"

    def _str_state(self, entity_id: str, default: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return default
        return state.state

    def _optional_float(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None
