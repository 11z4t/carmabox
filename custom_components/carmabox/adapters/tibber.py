"""CARMA Box — Tibber price adapter.

Reads electricity prices via HA's Tibber integration.

Entity pattern: sensor.electricity_price_{home_name}
Attributes: prices_today, prices_tomorrow (list of {start_time, price})

Tibber prices are in the configured currency (SEK/kWh for Sweden).
We convert to öre/kWh (x100) to match CARMA Box convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import PriceAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_SEK_TO_ORE = 100.0  # 1 SEK = 100 öre


class TibberAdapter(PriceAdapter):
    """Adapter for Tibber electricity prices via HA integration.

    Reads: current price, today prices, tomorrow prices.
    Handles: Tibber's dict-based price format (list of {start_time, price}).
    Converts: SEK/kWh → öre/kWh.
    Fallback: configurable flat price if unavailable.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str, fallback_price: float = 100.0) -> None:
        """Initialize Tibber adapter.

        Args:
            hass: Home Assistant instance.
            entity_id: Tibber price sensor entity (e.g. sensor.electricity_price_hem).
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
    def _extract_hourly(raw: list[dict[str, Any]] | None) -> list[float]:
        """Extract 24 hourly prices from Tibber format.

        Tibber stores prices as list of dicts: [{"start_time": "...", "price": 0.42}, ...]
        Price is in SEK/kWh — we convert to öre/kWh.
        """
        if not raw:
            return []
        prices: list[float] = []
        for entry in raw:
            price = entry.get("price", 0) if isinstance(entry, dict) else entry
            try:
                prices.append(round(float(price) * _SEK_TO_ORE, 2))
            except (ValueError, TypeError):
                prices.append(0.0)
        return prices

    @property
    def current_price(self) -> float:
        """Current electricity price (öre/kWh)."""
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return self.fallback_price
        try:
            return round(float(state.state) * _SEK_TO_ORE, 2)
        except (ValueError, TypeError):
            return self.fallback_price

    @property
    def today_prices(self) -> list[float]:
        """Hourly prices for today (24 entries, öre/kWh)."""
        attrs = self._attrs()
        raw = attrs.get("prices_today", [])
        prices = self._extract_hourly(raw)
        if not prices or len(prices) < 24:
            _LOGGER.debug("Tibber today unavailable — using fallback")
            return [self.fallback_price] * 24
        return prices[:24]

    @property
    def tomorrow_prices(self) -> list[float] | None:
        """Hourly prices for tomorrow (24 entries) or None if not yet available.

        Tibber typically publishes tomorrow's prices around 13:00 CET.
        """
        attrs = self._attrs()
        raw = attrs.get("prices_tomorrow", [])
        prices = self._extract_hourly(raw)
        if not prices or len(prices) < 24:
            return None
        return prices[:24]
