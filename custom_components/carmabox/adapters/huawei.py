"""CARMA Box — Huawei Solar adapter.

Reads battery state and sends commands via HA's huawei_solar HACS integration.

Entity patterns:
  - sensor.battery_state_of_capacity → SoC
  - sensor.battery_charge_discharge_power → battery power (W)
  - select.batteries_working_mode → EMS mode control
  - number.storage_maximum_charging_power → charge limit
  - number.storage_maximum_discharging_power → discharge limit

Working modes: maximise_self_consumption, time_of_use, fully_fed_to_grid
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from . import InverterAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 5
_RATE_LIMIT_S = 2.0  # Huawei Modbus TCP: max 1 call per 2s

# Map CARMA Box internal EMS modes → Huawei working modes
_CARMA_TO_HUAWEI: dict[str, str] = {
    "charge_pv": "maximise_self_consumption",
    "charge_battery": "time_of_use",
    "discharge_pv": "time_of_use",
    "discharge_battery": "time_of_use",
    "battery_standby": "maximise_self_consumption",
}

# Map Huawei working modes → CARMA Box internal EMS modes
_HUAWEI_TO_CARMA: dict[str, str] = {
    "maximise_self_consumption": "charge_pv",
    "time_of_use": "discharge_pv",
    "fully_fed_to_grid": "discharge_pv",
}


class HuaweiAdapter(InverterAdapter):
    """Adapter for Huawei FusionSolar inverter via huawei_solar HACS integration.

    Reads: SoC, battery power, temperature, working mode.
    Writes: working mode, discharge/charge power limits.

    Huawei uses Modbus TCP — only one connection at a time.
    Default polling: 30s (matches CARMA Box cycle).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str,
    ) -> None:
        """Initialize Huawei adapter.

        Args:
            hass: Home Assistant instance.
            device_id: Huawei device ID.
            entity_prefix: Entity prefix (e.g. '' for default single-inverter setup).
        """
        self.hass = hass
        self.device_id = device_id
        self.prefix = entity_prefix
        self._last_call_time: float = 0.0

    def _entity(self, base: str) -> str:
        """Build entity_id with optional prefix."""
        if self.prefix:
            return f"{base}_{self.prefix}"
        return base

    def _state(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _str_state(self, entity_id: str) -> str:
        """Read string state from HA entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return ""
        return state.state

    async def _safe_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with rate limiting and retry."""
        entity_id = data.get("entity_id", "?")

        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN Huawei %s: %s.%s → %s", self.prefix, domain, service, entity_id)
            return True

        import time

        now = time.monotonic()
        elapsed = now - self._last_call_time
        if elapsed < _RATE_LIMIT_S:
            await asyncio.sleep(_RATE_LIMIT_S - elapsed)

        for attempt in range(2):
            try:
                async with asyncio.timeout(10):
                    await self.hass.services.async_call(domain, service, data)
                self._last_call_time = time.monotonic()
                return True
            except ServiceNotFound:
                _LOGGER.error(
                    "Huawei %s: service not found %s.%s → %s",
                    self.prefix,
                    domain,
                    service,
                    entity_id,
                )
                return False
            except HomeAssistantError as err:
                _LOGGER.error(
                    "Huawei %s: HA error %s.%s → %s: %s (attempt %d/2)",
                    self.prefix,
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                )
            except Exception as err:
                _LOGGER.exception(
                    "Huawei %s: unexpected error %s.%s → %s: %s (attempt %d/2)",
                    self.prefix,
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                )
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
        self._last_call_time = time.monotonic()
        return False

    # ── Read ──────────────────────────────────────────────────

    @property
    def soc(self) -> float:
        """Battery SoC (0-100%). Returns -1 if unavailable."""
        raw = self._state(self._entity("sensor.battery_state_of_capacity"), default=-1.0)
        if raw < 0:
            return -1.0
        return max(0.0, min(100.0, raw))

    @property
    def power_w(self) -> float:
        """Battery power (W). Positive=discharge, negative=charge.

        Huawei reports positive=charging, negative=discharging — we invert
        to match CARMA Box convention (positive=discharge).
        """
        raw = self._state(self._entity("sensor.battery_charge_discharge_power"))
        return -raw  # Invert: Huawei positive=charge → CARMA positive=discharge

    @property
    def ems_mode(self) -> str:
        """Current EMS mode (mapped from Huawei working mode)."""
        huawei_mode = self._str_state(self._entity("select.batteries_working_mode"))
        return _HUAWEI_TO_CARMA.get(huawei_mode, "charge_pv")

    @property
    def temperature_c(self) -> float | None:
        """Battery temperature (°C) or None if unavailable."""
        val = self._state(self._entity("sensor.battery_temperature"), -999)
        return val if val > -999 else None

    # ── Write ─────────────────────────────────────────────────

    async def set_ems_mode(self, mode: str) -> bool:
        """Set EMS mode by mapping to Huawei working mode."""
        huawei_mode = _CARMA_TO_HUAWEI.get(mode)
        if not huawei_mode:
            _LOGGER.error("Huawei %s: REJECTED unknown EMS mode '%s'", self.prefix, mode)
            return False
        _LOGGER.info("Huawei %s: set working_mode -> %s (EMS: %s)", self.prefix, huawei_mode, mode)
        return await self._safe_call(
            "select",
            "select_option",
            {
                "entity_id": self._entity("select.batteries_working_mode"),
                "option": huawei_mode,
            },
        )

    async def set_discharge_limit(self, watts: int) -> bool:
        """Set maximum discharge power (W)."""
        watts = max(0, watts)
        _LOGGER.info("Huawei %s: discharge limit -> %dW", self.prefix, watts)
        return await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": self._entity("number.storage_maximum_discharging_power"),
                "value": watts,
            },
        )
