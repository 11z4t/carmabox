"""CARMA Box — GoodWe adapter.

Reads battery state and sends commands via HA's goodwe integration entities.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from . import InverterAdapter

_LOGGER = logging.getLogger(__name__)


class GoodWeAdapter(InverterAdapter):
    """Adapter for GoodWe inverter via HA integration.

    Reads: SoC, battery power, temperature, EMS mode
    Writes: EMS mode, fast charging switch/power, peak shaving limit
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str,
    ) -> None:
        """Initialize GoodWe adapter.

        Args:
            hass: Home Assistant instance.
            device_id: GoodWe device ID for goodwe.set_parameter calls.
            entity_prefix: Entity prefix (e.g. 'kontor', 'forrad').
        """
        self.hass = hass
        self.device_id = device_id
        self.prefix = entity_prefix

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

    # ── Read ──────────────────────────────────────────────────

    @property
    def soc(self) -> float:
        """Battery SoC (0-100%)."""
        return self._state(f"sensor.pv_battery_soc_{self.prefix}")

    @property
    def power_w(self) -> float:
        """Battery power (W). Positive=discharge, negative=charge."""
        return self._state(f"sensor.goodwe_battery_power_{self.prefix}")

    @property
    def ems_mode(self) -> str:
        """Current EMS mode."""
        return self._str_state(f"select.goodwe_{self.prefix}_ems_mode")

    @property
    def fast_charging_on(self) -> bool:
        """Fast charging switch state."""
        return self._str_state(f"switch.goodwe_fast_charging_switch_{self.prefix}") == "on"

    @property
    def temperature_c(self) -> float | None:
        """Battery temperature (°C) or None if unavailable."""
        val = self._state(f"sensor.goodwe_battery_min_cell_temperature_{self.prefix}", -999)
        return val if val > -999 else None

    # ── Write ─────────────────────────────────────────────────

    async def set_ems_mode(self, mode: str) -> None:
        """Set EMS mode (charge_pv, charge_battery, discharge_battery, battery_standby)."""
        _LOGGER.info("GoodWe %s: set EMS → %s", self.prefix, mode)
        await self.hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": f"select.goodwe_{self.prefix}_ems_mode",
                "option": mode,
            },
        )

    async def set_fast_charging(
        self,
        on: bool,
        power_pct: int = 100,
        soc_target: int = 100,
    ) -> None:
        """Set fast charging switch + power + SoC target."""
        switch_entity = f"switch.goodwe_fast_charging_switch_{self.prefix}"
        service = "turn_on" if on else "turn_off"
        await self.hass.services.async_call(
            "switch",
            service,
            {
                "entity_id": switch_entity,
            },
        )
        if on:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.goodwe_fast_charging_power_{self.prefix}",
                    "value": power_pct,
                },
            )
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.goodwe_fast_charging_soc_{self.prefix}",
                    "value": soc_target,
                },
            )

    async def set_discharge_limit(self, watts: int) -> None:
        """Set peak shaving power limit (discharge rate)."""
        _LOGGER.info("GoodWe %s: discharge limit → %dW", self.prefix, watts)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.goodwe_{self.prefix}_ems_power_limit",
                "value": watts,
            },
        )
