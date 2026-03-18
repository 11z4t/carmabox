"""CARMA Box — Easee EV charger adapter.

Reads charger state and controls charging via HA's easee integration.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class EaseeAdapter:
    """Adapter for Easee EV charger via HA integration.

    Reads: status, current (A), power (W), cable state.
    Writes: enable/disable, dynamic charger limit (A).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str = "easee_home_12840",
    ) -> None:
        """Initialize Easee adapter.

        Args:
            hass: Home Assistant instance.
            device_id: Easee device ID for service calls.
            entity_prefix: Entity prefix (e.g. 'easee_home_12840').
        """
        self.hass = hass
        self.device_id = device_id
        self.prefix = entity_prefix

    def _state(self, suffix: str, default: float = 0.0) -> float:
        """Read float state."""
        entity_id = f"sensor.{self.prefix}_{suffix}"
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _str_state(self, entity_id: str) -> str:
        """Read string state."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return ""
        return state.state

    # ── Read ──────────────────────────────────────────────────

    @property
    def status(self) -> str:
        """Charger status (awaiting_start, charging, paused, etc.)."""
        return self._str_state(f"sensor.{self.prefix}_status")

    @property
    def current_a(self) -> float:
        """Current charging amperage."""
        return self._state("current")

    @property
    def power_w(self) -> float:
        """Current charging power (W)."""
        return self._state("power") * 1000  # Easee reports kW

    @property
    def is_enabled(self) -> bool:
        """Charger enabled."""
        return self._str_state(f"switch.{self.prefix}_is_enabled") == "on"

    @property
    def is_charging(self) -> bool:
        """True if actively charging."""
        return self.status == "charging"

    @property
    def cable_locked(self) -> bool:
        """True if cable is locked (car connected)."""
        return self._str_state(f"binary_sensor.{self.prefix}_cable_locked") == "on"

    @property
    def dynamic_limit_a(self) -> float:
        """Current dynamic charger limit (A)."""
        return self._state("dynamic_charger_limit")

    # ── Write ─────────────────────────────────────────────────

    async def enable(self) -> None:
        """Enable the charger."""
        _LOGGER.info("Easee: enable charger")
        await self.hass.services.async_call("switch", "turn_on", {
            "entity_id": f"switch.{self.prefix}_is_enabled",
        })

    async def disable(self) -> None:
        """Disable the charger."""
        _LOGGER.info("Easee: disable charger")
        await self.hass.services.async_call("switch", "turn_off", {
            "entity_id": f"switch.{self.prefix}_is_enabled",
        })

    async def set_current(self, amps: int) -> None:
        """Set dynamic charger limit (A). Min 6, max 16 (1-phase).

        Uses number.set_value (reliable) instead of easee.set_charger_dynamic_limit
        (returns 500 errors frequently).
        """
        amps = max(0, min(32, amps))
        _LOGGER.info("Easee: set current → %dA", amps)
        await self.hass.services.async_call("number", "set_value", {
            "entity_id": f"number.{self.prefix}_dynamic_charger_limit",
            "value": amps,
        })
