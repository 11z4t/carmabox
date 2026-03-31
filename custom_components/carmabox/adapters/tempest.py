"""CARMA Box — Tempest weather station adapter.

Reads outdoor weather data via HA's Tempest MQTT integration.
Provides temperature, illuminance, and wind data for optimizer decisions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import WeatherAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Entity IDs from Tempest WeatherFlow MQTT integration
_TEMPERATURE = "sensor.tempest_temperature"
_ILLUMINANCE = "sensor.tempest_illuminance"
_WIND_SPEED = "sensor.tempest_wind_speed"
_WIND_GUST = "sensor.tempest_wind_gust"
_PRESSURE = "sensor.tempest_pressure"
_SOLAR_RADIATION = "sensor.tempest_solar_radiation"


class TempestAdapter(WeatherAdapter):
    """Adapter for Tempest WeatherFlow station via HA MQTT integration.

    Reads: temperature (°C), illuminance (lux), wind speed (m/s), wind gust (m/s).
    Fallback: Safe defaults if sensor unavailable.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Tempest adapter."""
        self.hass = hass

    def _float_state(self, entity_id: str, fallback: float = 0.0) -> float:
        """Read float state, return fallback if unavailable."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return fallback
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return fallback

    @property
    def temperature_c(self) -> float:
        """Current outdoor temperature (°C).

        Fallback: 15.0°C if unavailable (moderate temperature assumption).
        """
        return self._float_state(_TEMPERATURE, fallback=15.0)

    @property
    def illuminance_lux(self) -> float:
        """Current solar illuminance (lux).

        Fallback: 0.0 lux if unavailable (assume night/no sun).
        """
        return self._float_state(_ILLUMINANCE, fallback=0.0)

    @property
    def wind_speed_ms(self) -> float:
        """Current wind speed (m/s).

        Fallback: 0.0 m/s if unavailable (calm conditions).
        """
        return self._float_state(_WIND_SPEED, fallback=0.0)

    @property
    def wind_gust_ms(self) -> float:
        """Maximum wind gust speed (m/s).

        Fallback: 0.0 m/s if unavailable (calm conditions).
        """
        return self._float_state(_WIND_GUST, fallback=0.0)

    @property
    def pressure_mbar(self) -> float:
        """Barometric pressure (mbar/hPa). BME280 MEMS, +/-1 mbar.

        Fallback: 1013.25 mbar (sea level standard) if unavailable.
        """
        return self._float_state(_PRESSURE, fallback=1013.25)

    @property
    def solar_radiation_wm2(self) -> float:
        """Solar radiation (W/m2). 0-1900 W/m2, +/-5%.

        Direct measurement of irradiance — can validate Solcast in real-time.
        Fallback: 0.0 if unavailable.
        """
        return self._float_state(_SOLAR_RADIATION, fallback=0.0)
