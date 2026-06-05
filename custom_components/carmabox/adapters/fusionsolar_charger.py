"""CARMA Box — Huawei FusionSolar EV charger adapter.

Wraps the fusionsolarplus HACS integration for cloud-connected Huawei chargers.

Entity patterns (configurable via entity_prefix):
  - sensor.{prefix}_charger_state        → charger status string
  - sensor.{prefix}_charger_session_energy → session energy (kWh)
  - sensor.{prefix}_dynamic_charger_current → actual charging current (A)
  - number.{prefix}_dynamic_charger_current → write current setpoint
  - button.{prefix}_charger_pause        → pause charging
  - button.{prefix}_charger_resume       → resume charging

Cloud latency characteristics (FusionSolar v0.1 acceptance):
  - 5-30s normal latency for current changes to apply
  - 60s tolerance window before drift is flagged (needs_recovery)
  - >120s gap triggers HA notification alert (if notify_service configured)
  - 1A drift is acceptable (hard-invariant compromise per Wiklander RCA)
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
_MIN_CURRENT_A = 6
_CLOUD_TOLERANCE_S = 60  # Flag drift after 60s without convergence
_ALERT_THRESHOLD_S = 120  # Send HA notification after 120s unresolved drift
_DRIFT_MAX_A = 1  # Acceptable current deviation (v0.1 compromise)


class FusionSolarChargerAdapter(EVAdapter):
    """Adapter for Huawei FusionSolar EV charger via fusionsolarplus HACS integration.

    All write commands go via the FusionSolar cloud with 5-30s latency.
    The adapter tracks commanded vs actual current and surfaces drift via
    needs_recovery / try_recover for the coordinator to act on.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_prefix: str,
        notify_service: str = "",
    ) -> None:
        """Initialize FusionSolarChargerAdapter.

        Args:
            hass: Home Assistant instance.
            entity_prefix: Entity prefix matching the fusionsolarplus integration
                           (e.g. 'fusionsolarplus_ne132457623').
            notify_service: HA notify service name for >120s drift alerts
                            (e.g. 'notify.slack_carma'). Empty = no alert.
        """
        self.hass = hass
        self.prefix = entity_prefix
        self.notify_service = notify_service
        self._target_amps: int = 0
        self._target_set_at: float | None = None
        self._alert_sent_at: float | None = None

    def _state(self, entity_id: str, default: float = 0.0) -> float:
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
        entity_id = data.get("entity_id", "?")
        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN FusionSolar: %s.%s → %s", domain, service, entity_id)
            return True
        for attempt in range(2):
            try:
                await self.hass.services.async_call(domain, service, data)
                return True
            except ServiceNotFound:
                _LOGGER.error(
                    "FusionSolar %s: service not found %s.%s", self.prefix, domain, service
                )
                return False
            except (HomeAssistantError, Exception) as err:
                _LOGGER.error(
                    "FusionSolar %s: %s.%s error: %s (attempt %d/2)",
                    self.prefix,
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
        return self._str_state(f"sensor.{self.prefix}_charger_state")

    @property
    def current_a(self) -> float:
        return self._state(f"sensor.{self.prefix}_dynamic_charger_current")

    @property
    def session_energy_kwh(self) -> float:
        return self._state(f"sensor.{self.prefix}_charger_session_energy")

    @property
    def power_w(self) -> float:
        # FusionSolar typically reports 1-phase; use current x 230V as approximation.
        # Override with dedicated power sensor if available on this device.
        return self.current_a * 230.0

    @property
    def is_charging(self) -> bool:
        return self.status in ("charging", "Charging")

    @property
    def plug_connected(self) -> bool:
        return self.status not in ("", "disconnected", "Disconnected", "no_cable")

    @property
    def cable_locked(self) -> bool:
        return self.plug_connected

    @property
    def connection_state(self) -> str:
        if self.is_charging:
            return "charging"
        if self.plug_connected:
            return "connected"
        return "disconnected"

    @property
    def needs_recovery(self) -> bool:
        """True if commanded current has not converged within 60s tolerance."""
        if self._target_set_at is None or self._target_amps == 0:
            return False
        elapsed = time.monotonic() - self._target_set_at
        if elapsed < _CLOUD_TOLERANCE_S:
            return False
        drift = abs(self.current_a - self._target_amps)
        return drift > _DRIFT_MAX_A

    # ── Write ─────────────────────────────────────────────────

    async def enable(self) -> bool:
        _LOGGER.info("FusionSolar %s: resume charger", self.prefix)
        return await self._safe_call(
            "button",
            "press",
            {"entity_id": f"button.{self.prefix}_charger_resume"},
        )

    async def disable(self) -> bool:
        _LOGGER.info("FusionSolar %s: pause charger", self.prefix)
        ok = await self._safe_call(
            "button",
            "press",
            {"entity_id": f"button.{self.prefix}_charger_pause"},
        )
        if ok:
            self._target_amps = 0
            self._target_set_at = None
        return ok

    async def set_current(self, amps: int) -> bool:
        """Set dynamic charger current (A). Clamps to [6, MAX_EV_CURRENT]."""
        amps = max(_MIN_CURRENT_A, min(MAX_EV_CURRENT, amps))
        _LOGGER.info("FusionSolar %s: set_current → %dA", self.prefix, amps)
        ok = await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.{self.prefix}_dynamic_charger_current",
                "value": amps,
            },
        )
        if ok:
            self._target_amps = amps
            self._target_set_at = time.monotonic()
            self._alert_sent_at = None  # Reset alert timer on fresh command
        return ok

    async def reset_to_default(self) -> bool:
        return await self.set_current(_MIN_CURRENT_A)

    @property
    def charging_power_at_amps(self) -> float:
        return self._target_amps * 230.0

    async def try_recover(self) -> str | None:
        """Resend last current command. Sends HA alert if >120s unresolved."""
        if not self.needs_recovery:
            return None

        elapsed = time.monotonic() - (self._target_set_at or 0)
        drift = abs(self.current_a - self._target_amps)

        _LOGGER.warning(
            "FusionSolar %s: drift %.1fA after %.0fs (target=%dA actual=%.1fA) — resending",
            self.prefix,
            drift,
            elapsed,
            self._target_amps,
            self.current_a,
        )

        # Resend without resetting the timer (keep tracking convergence)
        await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.{self.prefix}_dynamic_charger_current",
                "value": self._target_amps,
            },
        )

        if (
            elapsed > _ALERT_THRESHOLD_S
            and self.notify_service
            and (self._alert_sent_at is None or (time.monotonic() - self._alert_sent_at) > 300)
        ):
            msg = (
                f"FusionSolar {self.prefix}: {elapsed:.0f}s drift "
                f"— target={self._target_amps}A actual={self.current_a:.1f}A"
            )
            _LOGGER.error("FusionSolar ALERT: %s", msg)
            try:
                await self.hass.services.async_call(
                    "notify",
                    self.notify_service.removeprefix("notify."),
                    {"message": f"⚠️ CARMA EV drift: {msg}"},
                )
                self._alert_sent_at = time.monotonic()
            except Exception as err:
                _LOGGER.error("FusionSolar: alert send failed: %s", err)

        return "fusionsolar_resend"
