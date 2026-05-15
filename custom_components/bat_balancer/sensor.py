"""Sensor platform for bat_balancer — exposes capability + avg_soc to Brain."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BatBalancerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BatBalancerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BatBalancerCapabilitySensor(coordinator, entry),
            BatBalancerAvgSocSensor(coordinator, entry),
        ]
    )


class BatBalancerCapabilitySensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_capability — read by Brain for bat engagement decisions."""

    _attr_name = "Bat Balancer Capability"
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_capability"

    @property
    def native_value(self) -> str:
        cap = self.coordinator.capability
        return cap.get("status", "initializing")

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.capability


class BatBalancerAvgSocSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_avg_soc_pct — capacity-weighted average SoC across banks."""

    _attr_name = "Bat Balancer Avg SoC"
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:battery-50"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = "battery"

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_avg_soc_pct"
        self.entity_id = "sensor.bat_balancer_avg_soc_pct"

    @property
    def native_value(self) -> float:
        return self.coordinator.avg_soc_pct
