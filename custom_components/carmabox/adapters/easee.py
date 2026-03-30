"""CARMA Box — Easee EV charger adapter.

IT-1965/IT-1966: Rewritten for correct Easee control.

KEY INSIGHT (2026-03-23):
  - max_charger_limit = HARD CEILING. If set to 6A, Easee enters
    "waiting_in_fully" and BLOCKS all charging. NEVER set below 10A.
  - dynamic_charger_limit = ACTUAL current control. Use this for 6-10A.
  - On startup: set max=10A, dynamic=6A, disable smart_charging.
  - cable_locked=off is NORMAL when idle (car controls lock).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from ..const import DEFAULT_EV_MAX_AMPS
from . import EVAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 5
_MAX_LIMIT_FLOOR = 10  # PLAT-1032: 6A causes Easee "waiting_in_fully" block — 10A is safe minimum
_DYNAMIC_MIN = 6


class EaseeAdapter(EVAdapter):
    """Adapter for Easee EV charger via HA integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str = "easee_home_12840",
        charger_id: str = "",
    ) -> None:
        self.hass = hass
        self.device_id = device_id
        self.charger_id = charger_id
        self.prefix = entity_prefix
        self._initialized = False

    async def ensure_initialized(self, force: bool = False) -> None:
        """Setup: max_limit=10, smart_charging=off. Runs once unless force=True."""
        if self._initialized and not force:
            return
        self._initialized = True
        _LOGGER.info(
            "Easee: initializing — max_limit=%dA, dynamic=%dA, smart_charging=off",
            _MAX_LIMIT_FLOOR,
            _DYNAMIC_MIN,
        )
        # Set max_limit to safe floor + dynamic to minimum
        if self.charger_id:
            await self._safe_call(
                "easee",
                "set_charger_max_limit",
                {"charger_id": self.charger_id, "current": _MAX_LIMIT_FLOOR},
            )
            await self._safe_call(
                "easee",
                "set_charger_dynamic_limit",
                {"charger_id": self.charger_id, "current": _DYNAMIC_MIN},
            )
        # PLAT-1032: Set circuit dynamic limit as deep safety net
        if self.charger_id:
            await self._safe_call(
                "easee",
                "set_circuit_dynamic_limit",
                {"charger_id": self.charger_id, "current": _MAX_LIMIT_FLOOR},
            )
        # Disable smart charging (Easee cloud queue blocks us)
        await self._safe_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{self.prefix}_smart_charging"},
        )

    def _state(self, suffix: str, default: float = 0.0) -> float:
        entity_id = f"sensor.{self.prefix}_{suffix}"
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _str_state(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return ""
        return state.state

    async def _safe_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        entity_id = data.get("entity_id", data.get("charger_id", "?"))
        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN Easee: %s.%s → %s", domain, service, entity_id)
            return True
        for attempt in range(2):
            try:
                await self.hass.services.async_call(domain, service, data)
                return True
            except ServiceNotFound:
                _LOGGER.error("Easee: service not found %s.%s", domain, service)
                return False
            except (HomeAssistantError, Exception) as err:
                _LOGGER.error(
                    "Easee: %s.%s error: %s (attempt %d/2)",
                    domain,
                    service,
                    err,
                    attempt + 1,
                )
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
        return False

    # ── Read ──────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._str_state(f"sensor.{self.prefix}_status")

    @property
    def current_a(self) -> float:
        return self._state("current")

    @property
    def power_w(self) -> float:
        return self._state("power") * 1000

    @property
    def power_kw(self) -> float:
        return self._state("power")

    @property
    def is_enabled(self) -> bool:
        return self._str_state(f"switch.{self.prefix}_is_enabled") == "on"

    @property
    def is_charging(self) -> bool:
        return self.status == "charging"

    @property
    def cable_locked(self) -> bool:
        # cable_locked=off is NORMAL when idle — check plug instead
        plug = self._str_state(f"binary_sensor.{self.prefix}_plug")
        if plug == "on":
            return True  # plug connected = car present
        return self._str_state(f"binary_sensor.{self.prefix}_cable_locked") == "on"

    @property
    def plug_connected(self) -> bool:
        return self._str_state(f"binary_sensor.{self.prefix}_plug") == "on"

    @property
    def dynamic_limit_a(self) -> float:
        return self._state("dynamic_charger_limit")

    @property
    def reason_for_no_current(self) -> str:
        return self._str_state(f"sensor.{self.prefix}_reason_for_no_current")

    @property
    def phase_count(self) -> int:
        mode = self._str_state(f"sensor.{self.prefix}_phase_mode")
        return 3 if mode == "three" else 1

    @property
    def charging_power_at_amps(self) -> float:
        """Expected kW at current dynamic limit."""
        return self.dynamic_limit_a * 230 * self.phase_count / 1000

    # ── Write ─────────────────────────────────────────────────

    async def enable(self) -> bool:
        await self.ensure_initialized()
        _LOGGER.info("Easee: enable charger")
        ok = await self._safe_call(
            "switch",
            "turn_on",
            {"entity_id": f"switch.{self.prefix}_is_enabled"},
        )
        if ok and self.charger_id:
            # Resume in case Easee is in awaiting_start
            await self._safe_call(
                "easee",
                "action_command",
                {"charger_id": self.charger_id, "action_command": "resume"},
            )
        return ok

    async def disable(self) -> bool:
        _LOGGER.info("Easee: disable charger")
        return await self._safe_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{self.prefix}_is_enabled"},
        )

    async def set_current(self, amps: int) -> bool:
        """Set charging current via dynamic_charger_limit.

        Uses set_charger_dynamic_limit (NOT max_limit — max stays at 10A).
        Range: 6-DEFAULT_EV_MAX_AMPS hard capped (defense-in-depth).
        """
        await self.ensure_initialized()
        # SAFETY: Never exceed hardware limit regardless of caller
        amps = max(_DYNAMIC_MIN, min(DEFAULT_EV_MAX_AMPS, amps))
        # Raise max_limit if needed (max_limit must be >= dynamic_limit)
        if amps > _MAX_LIMIT_FLOOR and self.charger_id:
            await self._safe_call(
                "easee",
                "set_charger_max_limit",
                {"charger_id": self.charger_id, "current": amps},
            )
        _LOGGER.info("Easee: set dynamic limit → %dA", amps)

        if self.charger_id:
            return await self._safe_call(
                "easee",
                "set_charger_dynamic_limit",
                {"charger_id": self.charger_id, "current": amps},
            )
        # Fallback: number entity
        return await self._safe_call(
            "number",
            "set_value",
            {"entity_id": f"number.{self.prefix}_dynamic_charger_limit", "value": amps},
        )

    async def reset_to_default(self) -> bool:
        """Reset to safe default: dynamic=6A (max stays at 10A)."""
        return await self.set_current(_DYNAMIC_MIN)
