"""CARMA Box — Solcast PV forecast adapter.

Reads solar production forecast via HA's solcast_solar integration.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from . import PVAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Entity IDs used by solcast_solar integration
_TODAY = "sensor.solcast_pv_forecast_forecast_today"
_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
_DAY_PREFIX = "sensor.solcast_pv_forecast_forecast_day_"


class SolcastAdapter(PVAdapter):
    """Adapter for Solcast PV forecast via HA integration.

    Reads: today kWh, tomorrow kWh, 3-7 day daily forecast, hourly forecast.
    Fallback: 0.0 kWh if unavailable.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Solcast adapter."""
        self.hass = hass

    def _float_state(self, entity_id: str) -> float:
        """Read float state, return 0.0 if unavailable."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return 0.0
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return 0.0

    @property
    def today_kwh(self) -> float:
        """Total PV forecast for today (kWh)."""
        return self._float_state(_TODAY)

    @property
    def tomorrow_kwh(self) -> float:
        """Total PV forecast for tomorrow (kWh)."""
        return self._float_state(_TOMORROW)

    @property
    def forecast_daily_3d(self) -> list[float]:
        """Daily PV forecast for 3-7 days [today, tomorrow, day3, ...]."""
        result: list[float] = [
            self._float_state(_TODAY),
            self._float_state(_TOMORROW),
        ]

        for day in range(3, 8):
            val = self._float_state(f"{_DAY_PREFIX}{day}")
            if val > 0:
                result.append(val)

        return result

    def _parse_hourly(self, entity_id: str) -> list[float]:
        """Parse detailedHourly attribute into 24-entry kW list."""
        hourly: list[float] = [0.0] * 24

        state = self.hass.states.get(entity_id)
        if state is None:
            return hourly

        detailed = state.attributes.get("detailedHourly", [])
        if not detailed:
            return hourly

        for entry in detailed:
            period = entry.get("period_start", "")
            try:
                hour = datetime.fromisoformat(period).hour
            except (ValueError, TypeError):
                continue

            # Prefer conservative estimate (pv_estimate10)
            # Solcast HACS detailedHourly reports in kW (NOT watts)
            kw = entry.get("pv_estimate10", entry.get("pv_estimate", 0))
            hourly[hour] = round(kw, 2) if kw > 0 else 0.0

        return hourly

    @property
    def today_hourly_kw(self) -> list[float]:
        """Hourly PV forecast for today (kW per hour, 24 entries)."""
        return self._parse_hourly(_TODAY)

    @property
    def tomorrow_hourly_kw(self) -> list[float]:
        """Hourly PV forecast for tomorrow (kW per hour, 24 entries)."""
        return self._parse_hourly(_TOMORROW)
