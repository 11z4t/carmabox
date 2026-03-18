"""CARMA Box adapters — HA-specific wrappers for data sources.

Abstract base classes define the contract each adapter must fulfill.
New hardware = new adapter implementing the right ABC.
Optimizer never imports adapters directly — coordinator wires them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class InverterAdapter(ABC):
    """Contract for battery inverter adapters (GoodWe, Huawei, SolarEdge)."""

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
    async def set_ems_mode(self, mode: str) -> None:
        """Set EMS mode."""

    @abstractmethod
    async def set_discharge_limit(self, watts: int) -> None:
        """Set discharge power limit."""


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

    @abstractmethod
    async def enable(self) -> None:
        """Enable charger."""

    @abstractmethod
    async def disable(self) -> None:
        """Disable charger."""

    @abstractmethod
    async def set_current(self, amps: int) -> None:
        """Set charge current (A)."""


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
