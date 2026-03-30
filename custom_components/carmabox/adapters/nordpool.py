"""CARMA Box — Nordpool adapter.

Reads electricity prices via HA's nordpool integration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import PriceAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class NordpoolAdapter(PriceAdapter):
    """Adapter for Nordpool electricity prices via HA integration.

    Reads: today prices, tomorrow prices, current price.
    Handles: 96-entry (15 min) and 24-entry (hourly) formats.
    Fallback: configurable flat price if unavailable.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str, fallback_price: float = 100.0) -> None:
        """Initialize Nordpool adapter.

        Args:
            hass: Home Assistant instance.
            entity_id: Nordpool sensor entity (e.g. sensor.nordpool_kwh_se3_sek_3_10_025).
            fallback_price: Price (öre/kWh) to use when data is unavailable.
        """
        self.hass = hass
        self.entity_id = entity_id
        self.fallback_price = fallback_price

    def _attrs(self) -> dict[str, Any]:
        """Get entity attributes."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return {}
        return dict(state.attributes)

    @staticmethod
    def _to_hourly(raw: list[float]) -> list[float]:
        """Convert 96-entry (15min) to 24 hourly averages."""
        if not raw:
            return []
        if len(raw) <= 24:
            return [round(p, 2) for p in raw]

        step = len(raw) // 24
        hourly = []
        for h in range(24):
            chunk = raw[h * step : (h + 1) * step]
            avg = sum(chunk) / len(chunk) if chunk else 0
            hourly.append(round(avg, 2))
        return hourly

    @property
    def current_price(self) -> float:
        """Current electricity price (öre/kWh)."""
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return self.fallback_price
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return self.fallback_price

    @property
    def today_prices(self) -> list[float]:
        """Hourly prices for today (24 entries, öre/kWh).

        Returns fallback [50.0]*24 if unavailable.
        """
        attrs = self._attrs()
        raw = attrs.get("today", [])
        prices = self._to_hourly(raw)
        if not prices or len(prices) < 24:
            _LOGGER.debug("Nordpool today unavailable — using fallback")
            return [self.fallback_price] * 24
        return prices

    @property
    def tomorrow_prices(self) -> list[float] | None:
        """Hourly prices for tomorrow (24 entries) or None if not yet available."""
        attrs = self._attrs()
        if not attrs.get("tomorrow_valid", False):
            return None
        raw = attrs.get("tomorrow", [])
        prices = self._to_hourly(raw)
        if not prices or len(prices) < 24:
            return None
        return prices
