"""Sensor platform for Binary Balancer — 5 sensors per asset."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BinaryBalancerCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Binary Balancer sensor entities from a config entry."""
    coordinator: BinaryBalancerCoordinator = entry.runtime_data
    asset_id: str = coordinator._config.asset_id

    entities: list[SensorEntity] = [
        BinaryBalancerStatusSensor(coordinator, asset_id),
        BinaryBalancerActualWSensor(coordinator, asset_id),
        BinaryBalancerFaultStateSensor(coordinator, asset_id),
        BinaryBalancerUptimeSensor(coordinator, asset_id),
        BinaryBalancerStaleCyclesSensor(coordinator, asset_id),
    ]
    async_add_entities(entities)


class _BinaryBalancerBaseSensor(CoordinatorEntity[BinaryBalancerCoordinator], SensorEntity):
    """Base class for Binary Balancer sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BinaryBalancerCoordinator,
        asset_id: str,
        key: str,
    ) -> None:
        """Initialise base sensor."""
        super().__init__(coordinator)
        self._asset_id = asset_id
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{asset_id}_{key}"
        self._attr_name = f"binary_balancer_{asset_id}_{key}"

    @property
    def data(self) -> dict[str, Any]:
        """Return coordinator data dict."""
        return self.coordinator.data or {}


class BinaryBalancerStatusSensor(_BinaryBalancerBaseSensor):
    """Overall status sensor: OK / PARTIAL / FAULT / OFFLINE / SAFE_STATE_ACTIVE."""

    def __init__(self, coordinator: BinaryBalancerCoordinator, asset_id: str) -> None:
        """Initialise status sensor."""
        super().__init__(coordinator, asset_id, "status")

    @property
    def native_value(self) -> str:
        """Return current status string."""
        return str(self.data.get("status", "OFFLINE"))


class BinaryBalancerActualWSensor(_BinaryBalancerBaseSensor):
    """Actual watts sensor."""

    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BinaryBalancerCoordinator, asset_id: str) -> None:
        """Initialise actual_w sensor."""
        super().__init__(coordinator, asset_id, "actual_w")

    @property
    def native_value(self) -> float:
        """Return actual watts."""
        return float(self.data.get("actual_w", 0.0))


class BinaryBalancerFaultStateSensor(_BinaryBalancerBaseSensor):
    """Fault state sensor string."""

    def __init__(self, coordinator: BinaryBalancerCoordinator, asset_id: str) -> None:
        """Initialise fault_state sensor."""
        super().__init__(coordinator, asset_id, "fault_state")

    @property
    def native_value(self) -> str:
        """Return fault state string."""
        return str(self.data.get("fault_state", "OFFLINE"))


class BinaryBalancerUptimeSensor(_BinaryBalancerBaseSensor):
    """Uptime sensor in seconds."""

    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: BinaryBalancerCoordinator, asset_id: str) -> None:
        """Initialise uptime_s sensor."""
        super().__init__(coordinator, asset_id, "uptime_s")

    @property
    def native_value(self) -> float:
        """Return uptime in seconds."""
        return float(self.data.get("uptime_s", 0.0))


class BinaryBalancerStaleCyclesSensor(_BinaryBalancerBaseSensor):
    """Count of consecutive stale cycles."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BinaryBalancerCoordinator, asset_id: str) -> None:
        """Initialise stale_cycles sensor."""
        super().__init__(coordinator, asset_id, "stale_cycles")

    @property
    def native_value(self) -> int:
        """Return stale cycle count."""
        return int(self.data.get("stale_cycles", 0))
