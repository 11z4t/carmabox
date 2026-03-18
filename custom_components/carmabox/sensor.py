"""CARMA Box — Sensors.

Exposes optimizer state as HA sensors for dashboard + automations.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import BatteryCommand, CarmaboxCoordinator
from .optimizer.savings import savings_breakdown, total_savings


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CARMA Box sensors."""
    coordinator: CarmaboxCoordinator = entry.runtime_data
    async_add_entities(
        [
            CarmaboxPlanStatusSensor(coordinator, entry),
            CarmaboxTargetSensor(coordinator, entry),
            CarmaboxSavingsSensor(coordinator, entry),
            CarmaboxBatterySocSensor(coordinator, entry),
            CarmaboxGridImportSensor(coordinator, entry),
            CarmaboxEVSocSensor(coordinator, entry),
        ]
    )


class CarmaboxBaseSensor(CoordinatorEntity[CarmaboxCoordinator], SensorEntity):
    """Base sensor for CARMA Box."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CarmaboxCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name


class CarmaboxPlanStatusSensor(CarmaboxBaseSensor):
    """Current plan status (idle/charging/discharging)."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "plan_status", "Plan Status")
        self._attr_icon = "mdi:calendar-check"

    @property
    def native_value(self) -> str:
        """Return current action from plan."""
        if self.coordinator.data is None:
            return "unknown"
        state = self.coordinator.data
        if state.is_exporting:
            return "charging_pv"
        if state.all_batteries_full:
            return "standby"
        # Check what coordinator last commanded
        last = self.coordinator._last_command
        if last == BatteryCommand.DISCHARGE:
            return "discharging"
        if last == BatteryCommand.CHARGE_PV:
            return "charging"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Plan details as attributes."""
        if self.coordinator.data is None:
            return {}
        state = self.coordinator.data
        return {
            "target_weighted_kw": state.target_weighted_kw,
            "grid_power_w": state.grid_power_w,
            "battery_soc_1": state.battery_soc_1,
            "battery_soc_2": state.battery_soc_2 if state.has_battery_2 else None,
            "ev_soc": state.ev_soc if state.has_ev else None,
            "is_exporting": state.is_exporting,
            "plan_hours": len(state.plan),
        }


class CarmaboxTargetSensor(CarmaboxBaseSensor):
    """Current target weighted kW."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "target_kw", "Target")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_icon = "mdi:target"

    @property
    def native_value(self) -> float:
        return float(self.coordinator.target_kw)


class CarmaboxSavingsSensor(CarmaboxBaseSensor):
    """Estimated savings this month (kr)."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "savings_month", "Besparing Månad")
        self._attr_native_unit_of_measurement = "kr"
        self._attr_icon = "mdi:piggy-bank"
        self._entry = entry

    @property
    def native_value(self) -> float:
        cost_per_kw = float(self._entry.options.get("peak_cost_per_kw", 80.0))
        return total_savings(self.coordinator.savings, cost_per_kw)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Savings breakdown as attributes."""
        cost_per_kw = float(self._entry.options.get("peak_cost_per_kw", 80.0))
        return dict(savings_breakdown(self.coordinator.savings, cost_per_kw))


class CarmaboxBatterySocSensor(CarmaboxBaseSensor):
    """Average battery SoC."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "battery_soc", "Batteri SoC")
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_icon = "mdi:battery"

    @property
    def native_value(self) -> float:
        if self.coordinator.data is None:
            return 0
        return round(self.coordinator.data.total_battery_soc, 0)


class CarmaboxGridImportSensor(CarmaboxBaseSensor):
    """Current grid import (kW)."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "grid_import", "Grid Import")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_icon = "mdi:transmission-tower-import"

    @property
    def native_value(self) -> float:
        if self.coordinator.data is None:
            return 0
        return round(max(0, self.coordinator.data.grid_power_w) / 1000, 2)


class CarmaboxEVSocSensor(CarmaboxBaseSensor):
    """EV battery SoC."""

    def __init__(self, coordinator: CarmaboxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "ev_soc", "EV SoC")
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:car-electric"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None or not self.coordinator.data.has_ev:
            return None
        return round(self.coordinator.data.ev_soc, 0)
