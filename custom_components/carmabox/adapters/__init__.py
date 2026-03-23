"""CARMA Box adapters — HA-specific wrappers for data sources.

Abstract base classes define the contract each adapter must fulfill.
New hardware = new adapter implementing the right ABC.
Optimizer never imports adapters directly — coordinator wires them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class InverterAdapter(ABC):
    """Contract for battery inverter adapters (GoodWe, Huawei, SolarEdge)."""

    prefix: str = ""

    @property
    @abstractmethod
    def soc(self) -> float:
        """Battery SoC (0-100%)."""

    @property
    @abstractmethod
    def power_w(self) -> float:
        """Battery power (W). Positive=discharge, negative=charge."""

    @property
    @abstractmethod
    def ems_mode(self) -> str:
        """Current EMS mode string."""

    @property
    @abstractmethod
    def temperature_c(self) -> float | None:
        """Battery temperature (°C) or None."""

    @abstractmethod
    async def set_ems_mode(self, mode: str) -> bool:
        """Set EMS mode. Returns True on success."""

    @abstractmethod
    async def set_discharge_limit(self, watts: int) -> bool:
        """Set discharge power limit. Returns True on success."""


class EVAdapter(ABC):
    """Contract for EV charger adapters (Easee, Zaptec, Wallbox)."""

    @property
    @abstractmethod
    def status(self) -> str:
        """Charger status string."""

    @property
    @abstractmethod
    def current_a(self) -> float:
        """Charging current (A)."""

    @property
    @abstractmethod
    def power_w(self) -> float:
        """Charging power (W)."""

    @property
    @abstractmethod
    def is_charging(self) -> bool:
        """True if actively charging."""

    @property
    def cable_locked(self) -> bool:
        """True if cable is locked (car connected). Override per adapter."""
        return False

    @abstractmethod
    async def enable(self) -> bool:
        """Enable charger. Returns True on success."""

    @abstractmethod
    async def disable(self) -> bool:
        """Disable charger. Returns True on success."""

    @abstractmethod
    async def set_current(self, amps: int) -> bool:
        """Set charge current (A). Returns True on success."""

    async def reset_to_default(self) -> bool:
        """Reset charger to safe default. Override per adapter."""
        return await self.set_current(6)


class PriceAdapter(ABC):
    """Contract for price source adapters (Nordpool, Tibber, ENTSO-E)."""

    @property
    @abstractmethod
    def current_price(self) -> float:
        """Current price (öre/kWh)."""

    @property
    @abstractmethod
    def today_prices(self) -> list[float]:
        """24 hourly prices for today (öre/kWh)."""

    @property
    @abstractmethod
    def tomorrow_prices(self) -> list[float] | None:
        """24 hourly prices for tomorrow, or None if unavailable."""


class PVAdapter(ABC):
    """Contract for PV forecast adapters (Solcast, Forecast.Solar)."""

    @property
    @abstractmethod
    def today_kwh(self) -> float:
        """Total PV forecast for today (kWh)."""

    @property
    @abstractmethod
    def tomorrow_kwh(self) -> float:
        """Total PV forecast for tomorrow (kWh)."""

    @property
    @abstractmethod
    def forecast_daily_3d(self) -> list[float]:
        """Daily PV forecast for 3+ days (kWh)."""

    @property
    @abstractmethod
    def today_hourly_kw(self) -> list[float]:
        """24 hourly PV forecast for today (kW)."""

    @property
    @abstractmethod
    def tomorrow_hourly_kw(self) -> list[float]:
        """24 hourly PV forecast for tomorrow (kW)."""


class WeatherAdapter(ABC):
    """Contract for weather station adapters (Tempest, Netatmo, SMHI)."""

    @property
    @abstractmethod
    def temperature_c(self) -> float:
        """Current outdoor temperature (°C)."""

    @property
    @abstractmethod
    def illuminance_lux(self) -> float:
        """Current solar illuminance (lux)."""

    @property
    @abstractmethod
    def wind_speed_ms(self) -> float:
        """Current wind speed (m/s)."""

    @property
    @abstractmethod
    def wind_gust_ms(self) -> float:
        """Maximum wind gust speed (m/s)."""
