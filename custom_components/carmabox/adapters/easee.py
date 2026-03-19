"""CARMA Box — Easee EV charger adapter.

Reads charger state and controls charging via HA's easee integration.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from . import EVAdapter

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 5


class EaseeAdapter(EVAdapter):
    """Adapter for Easee EV charger via HA integration.

    Reads: status, current (A), power (W), cable state.
    Writes: enable/disable, dynamic charger limit (A).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str = "easee_home_12840",
        charger_id: str = "",
    ) -> None:
        """Initialize Easee adapter.

        Args:
            hass: Home Assistant instance.
            device_id: Easee device ID for service calls.
            entity_prefix: Entity prefix (e.g. 'easee_home_12840').
            charger_id: Easee charger serial (e.g. 'EH128405') for native service calls.
        """
        self.hass = hass
        self.device_id = device_id
        self.charger_id = charger_id
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

    async def _safe_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with error handling and 1 retry. Returns True on success."""
        entity_id = data.get("entity_id", "?")

        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN Easee: %s.%s → %s", domain, service, entity_id)
            return True

        for attempt in range(2):
            try:
                await self.hass.services.async_call(domain, service, data)
                return True
            except ServiceNotFound:
                _LOGGER.error(
                    "Easee: service not found %s.%s → %s",
                    domain,
                    service,
                    entity_id,
                )
                return False
            except HomeAssistantError as err:
                _LOGGER.error(
                    "Easee: HA error %s.%s → %s: %s (attempt %d/2)",
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                )
            except Exception as err:
                _LOGGER.exception(
                    "Easee: unexpected error %s.%s → %s: %s (attempt %d/2)",
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                )
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
        return False

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
        return self._state("power")  # Easee reports kW

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

    async def enable(self) -> bool:
        """Enable the charger."""
        _LOGGER.info("Easee: enable charger")
        return await self._safe_call(
            "switch",
            "turn_on",
            {"entity_id": f"switch.{self.prefix}_is_enabled"},
        )

    async def disable(self) -> bool:
        """Disable the charger."""
        _LOGGER.info("Easee: disable charger")
        return await self._safe_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{self.prefix}_is_enabled"},
        )

    async def set_current(self, amps: int) -> bool:
        """Set charger max limit (A).

        Uses easee.set_charger_max_limit (the only service that actually
        limits charging current — set_charger_dynamic_limit does NOT work).
        Default/minimum is 6A to avoid runaway 16A charging.
        """
        amps = max(6, min(32, amps))
        _LOGGER.info("Easee: set max limit → %dA", amps)

        if self.charger_id:
            return await self._safe_call(
                "easee",
                "set_charger_max_limit",
                {
                    "charger_id": self.charger_id,
                    "current": amps,
                },
            )

        # Fallback: number entity (may not exist in newer Easee versions)
        return await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.{self.prefix}_dynamic_charger_limit",
                "value": amps,
            },
        )

    async def reset_to_default(self) -> bool:
        """Reset charger to safe default (6A)."""
        return await self.set_current(6)
