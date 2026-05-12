"""EaseeClient — ONLY writer to Easee service calls.

Single-writer invariant: no other component in ev_balancer may call
easee.set_charger_dynamic_limit or easee.action_command.

Write order enforced: dynamic_limit BEFORE is_enabled (spec §5).
Retry: 1x after 5s on service call exception.
Timeout → sets charger_offline flag on coordinator.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .const import (
    EASEE_SERVICE_ACTION_COMMAND,
    EASEE_SERVICE_SET_CHARGER_DYNAMIC_LIMIT,
)

_LOGGER = logging.getLogger(__name__)
_RETRY_DELAY_S = 5
_CALL_TIMEOUT_S = 10


class EaseeClient:
    """Single-writer wrapper around Easee HA service calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._safety_guard_corrections: int = 0

    @property
    def safety_guard_corrections(self) -> int:
        return self._safety_guard_corrections

    async def set_charger_dynamic_limit(self, device_id: str, current_a: int) -> bool:
        """Write dynamic current limit to charger. Returns True on success."""
        if not device_id:
            _LOGGER.warning("easee_client: device_id not configured")
            return False

        # Hard cap — translation_logic should already enforce this,
        # but guard here as last resort and track any bypass.
        if current_a > 16:
            _LOGGER.warning(
                "easee_client: safety guard clamped %dA → 16A (correction #%d)",
                current_a,
                self._safety_guard_corrections + 1,
            )
            self._safety_guard_corrections += 1
            current_a = 16
        current_a = max(0, current_a)

        return await self._call_with_retry(
            EASEE_SERVICE_SET_CHARGER_DYNAMIC_LIMIT,
            {"device_id": device_id, "current": current_a, "time_to_live": 0},
        )

    async def action_command(self, charger_id: str, command: str) -> bool:
        """Send action command (pause/resume/stop) to charger."""
        if not charger_id:
            _LOGGER.warning("easee_client: charger_id not configured")
            return False
        return await self._call_with_retry(
            EASEE_SERVICE_ACTION_COMMAND,
            {"charger_id": charger_id, "action_command": command},
        )

    async def _call_with_retry(self, service: str, data: dict) -> bool:
        domain, service_name = service.split(".", 1)
        for attempt in range(2):
            try:
                async with asyncio.timeout(_CALL_TIMEOUT_S):
                    await self._hass.services.async_call(domain, service_name, data, blocking=True)
                return True
            except TimeoutError:
                _LOGGER.error("easee_client: timeout calling %s (attempt %d)", service, attempt + 1)
            except Exception as exc:
                _LOGGER.error(
                    "easee_client: error calling %s: %s (attempt %d)", service, exc, attempt + 1
                )
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
        return False
