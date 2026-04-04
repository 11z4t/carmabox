"""CARMA Box — Zaptec EV charger adapter.

Reads charger state and sends commands via HA's zaptec HACS integration.

Entity patterns:
  - sensor.{prefix}_charger_mode → status
  - sensor.{prefix}_charger_current → charging current
  - sensor.{prefix}_energy_meter → energy (kWh)
  - switch.{prefix}_charging → enable/disable
  - number.{installation}_available_current → current limit

Charger modes: connected_charging, connected_requesting, connected_finished,
               disconnected, unknown

NOTE: Zaptec recommends NOT changing current more than once per 15 minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from ..const import MAX_EV_CURRENT
from . import EVAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 5
_DYNAMIC_MIN = 6
_CURRENT_CHANGE_COOLDOWN_S = 900  # 15 min between current changes (Zaptec best practice)

# Map Zaptec charger_mode → human-readable status for CARMA Box
_MODE_TO_STATUS: dict[str, str] = {
    "connected_charging": "charging",
    "connected_requesting": "awaiting_start",
    "connected_finished": "completed",
    "disconnected": "disconnected",
    "unknown": "unknown",
}


class ZaptecAdapter(EVAdapter):
    """Adapter for Zaptec EV charger via HA HACS integration.

    Reads: charger mode/status, current, power.
    Writes: enable/disable charging, set available current.

    Current control is via installation-level available_current entity
    which sets all three phases simultaneously. Per-phase control available
    via zaptec.limit_current service.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str,
        installation_prefix: str = "",
    ) -> None:
        """Initialize Zaptec adapter.

        Args:
            hass: Home Assistant instance.
            device_id: Zaptec device ID.
            entity_prefix: Charger entity prefix (e.g. 'zaptec_charger_hallway').
            installation_prefix: Installation entity prefix for current control.
        """
        self.hass = hass
        self.device_id = device_id
        self.prefix = entity_prefix
        self.installation_prefix = installation_prefix or entity_prefix
        self._last_current_change: float = -_CURRENT_CHANGE_COOLDOWN_S

    def _state(self, suffix: str, default: float = 0.0) -> float:
        """Read float state from sensor.{prefix}_{suffix}."""
        entity_id = f"sensor.{self.prefix}_{suffix}"
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _str_state(self, entity_id: str) -> str:
        """Read string state from full entity_id."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return ""
        return state.state

    async def _safe_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with retry."""
        entity_id = data.get("entity_id", data.get("charger_id", "?"))
        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN Zaptec: %s.%s → %s", domain, service, entity_id)
            return True
        for attempt in range(2):
            try:
                await self.hass.services.async_call(domain, service, data)
                return True
            except ServiceNotFound:
                _LOGGER.error("Zaptec: service not found %s.%s", domain, service)
                return False
            except (HomeAssistantError, Exception) as err:
                _LOGGER.error(
                    "Zaptec: %s.%s error: %s (attempt %d/2)",
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
        """Charger status (normalized to CARMA Box conventions)."""
        raw = self._str_state(f"sensor.{self.prefix}_charger_mode")
        return _MODE_TO_STATUS.get(raw, raw or "unknown")

    @property
    def current_a(self) -> float:
        """Charging current (A)."""
        return self._state("charger_current")

    @property
    def power_w(self) -> float:
        """Charging power (W). Estimated from current x voltage x phases."""
        # Zaptec doesn't expose a direct power sensor — estimate from current
        current = self.current_a
        if current < 0.5:
            return 0.0
        phases = self.phase_count
        return current * 230 * phases

    @property
    def is_charging(self) -> bool:
        """True if actively charging."""
        return self.status == "charging"

    @property
    def cable_locked(self) -> bool:
        """True if cable is locked (car connected)."""
        return self._str_state(f"switch.{self.prefix}_permanent_cable_lock") == "on"

    @property
    def plug_connected(self) -> bool:
        """True if car is connected (any connected_* mode)."""
        raw = self._str_state(f"sensor.{self.prefix}_charger_mode")
        return raw.startswith("connected_")

    def _state_by_id(self, entity_id: str, default: float = 0.0) -> float:
        """Read float from full entity_id (not suffix-based)."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    @property
    def phase_count(self) -> int:
        """Number of charging phases (from phase switch setting)."""
        phase_switch = self._state_by_id(
            f"number.{self.installation_prefix}_3_to_1_phase_switch_current",
            default=-1,
        )
        if phase_switch == 32:
            return 1  # Forced 1-phase
        if phase_switch == 0:
            return 3  # Forced 3-phase
        return 3  # Default 3-phase

    @property
    def available_current_a(self) -> float:
        """Current available current setting (A)."""
        entity_id = f"number.{self.installation_prefix}_available_current"
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return 0.0
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return 0.0

    @property
    def charging_power_at_amps(self) -> float:
        """Expected kW at current available current setting."""
        return self.available_current_a * 230 * self.phase_count / 1000

    # ── Write ─────────────────────────────────────────────────

    async def enable(self) -> bool:
        """Enable charger (turn on charging switch + resume)."""
        _LOGGER.info("Zaptec: enable charger")
        ok = await self._safe_call(
            "switch",
            "turn_on",
            {"entity_id": f"switch.{self.prefix}_charging"},
        )
        if ok:
            # Also press resume button in case charger is in paused state
            await self._safe_call(
                "button",
                "press",
                {"entity_id": f"button.{self.prefix}_resume_charging"},
            )
        return ok

    async def disable(self) -> bool:
        """Disable charger (stop charging)."""
        _LOGGER.info("Zaptec: disable charger")
        return await self._safe_call(
            "switch",
            "turn_off",
            {"entity_id": f"switch.{self.prefix}_charging"},
        )

    async def set_current(self, amps: int) -> bool:
        """Set charging current via available_current.

        Respects Zaptec 15-minute cooldown between current changes.
        Range: 6-MAX_EV_CURRENT hard capped.
        """
        amps = max(_DYNAMIC_MIN, min(MAX_EV_CURRENT, amps))

        # Enforce 15-min cooldown (Zaptec best practice)
        now = time.monotonic()
        elapsed = now - self._last_current_change
        if elapsed < _CURRENT_CHANGE_COOLDOWN_S:
            _LOGGER.debug(
                "Zaptec: current change skipped — %.0fs since last change (cooldown %ds)",
                elapsed,
                _CURRENT_CHANGE_COOLDOWN_S,
            )
            return True  # Not an error, just rate-limited

        _LOGGER.info("Zaptec: set available current → %dA", amps)
        ok = await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.{self.installation_prefix}_available_current",
                "value": amps,
            },
        )
        if ok:
            self._last_current_change = now
        return ok

    async def reset_to_default(self) -> bool:
        """Reset charger to safe default: 6A."""
        return await self.set_current(_DYNAMIC_MIN)
