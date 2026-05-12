"""HA SensorEntity definitions for ev_balancer (spec §3 + §9a)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EvBalancerCoordinator
from .models import EvBalancerState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvBalancerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EvBalancerStatusSensor(coordinator),
            EvBalancerStatusReasonSensor(coordinator),
            EvBalancerActualWSensor(coordinator),
            EvBalancerActualASensor(coordinator),
            EvBalancerTargetASensor(coordinator),
            EvBalancerCapabilitySensor(coordinator),
            EvBalancerMetricsSensor(coordinator),
            EvBalancerLastHwWriteTsSensor(coordinator),
            EvBalancerDwellRemainingSensor(coordinator),
        ]
    )


class _EvBalancerBase(CoordinatorEntity[EvBalancerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EvBalancerCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        # Linking to a virtual device ensures HA generates entity_ids as
        # sensor.ev_balancer_{name_slug} instead of sensor.{name_slug}.
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="EV Balancer",
            manufacturer="Custom",
            model="ev_balancer v3",
        )

    @property
    def _state_obj(self) -> EvBalancerState:
        return self.coordinator.data


class EvBalancerStatusSensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_status"
    _attr_name = "Status"
    _attr_icon = "mdi:ev-station"

    @property
    def state(self) -> str:
        return self._state_obj.status.value if self._state_obj else "initializing"


class EvBalancerStatusReasonSensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_status_reason"
    _attr_name = "Status Reason"
    _attr_icon = "mdi:information-outline"

    @property
    def state(self) -> str:
        return self._state_obj.status_reason or "none"


class EvBalancerActualWSensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_actual_w"
    _attr_name = "Actual W"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    @property
    def native_value(self) -> float:
        s = self._state_obj
        if not s or s.last_dynamic_a == 0:
            return 0.0
        # Read actual from Easee (use coordinator method)
        return float(s.last_dynamic_a * 3 * 230)


class EvBalancerActualASensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_actual_a"
    _attr_name = "Actual A"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    @property
    def native_value(self) -> int:
        return self._state_obj.last_dynamic_a if self._state_obj else 0


class EvBalancerTargetASensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_target_a"
    _attr_name = "Target A"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:target"

    @property
    def native_value(self) -> int:
        return self._state_obj.last_dynamic_a if self._state_obj else 0


class EvBalancerCapabilitySensor(_EvBalancerBase, SensorEntity):
    """Reports capability window as JSON attributes — rejected_w excluded from Brain view."""

    _attr_unique_id = "ev_balancer_capability"
    _attr_name = "Capability"
    _attr_icon = "mdi:speedometer"

    @property
    def state(self) -> str:
        s = self._state_obj
        if not s:
            return "unknown"
        return f"{int(s.capability_max_w)}W"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._state_obj
        if not s:
            return {}
        return {
            "max_w": s.capability_max_w,
            "min_w": s.capability_min_w,
            "rejected_w": s.rejected_w,
        }


class EvBalancerMetricsSensor(_EvBalancerBase, SensorEntity):
    """Rolling 24h metrics."""

    _attr_unique_id = "ev_balancer_metrics"
    _attr_name = "Metrics"
    _attr_icon = "mdi:chart-bar"

    @property
    def state(self) -> int:
        return self._state_obj.amp_changes_24h if self._state_obj else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._state_obj
        if not s:
            return {}
        return {
            "amp_changes_24h": s.amp_changes_24h,
            "pause_resume_count_24h": s.pause_resume_count_24h,
            "fault_count_24h": s.fault_count_24h,
            "soft_fuse_engagements_24h": s.soft_fuse_engagements_24h,
            "safety_guard_corrections": s.safety_guard_corrections,
        }


class EvBalancerLastHwWriteTsSensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_last_hw_write_ts"
    _attr_name = "Last HW Write Ts"
    _attr_icon = "mdi:clock-check-outline"

    @property
    def state(self) -> float:
        return self._state_obj.last_hw_write_ts if self._state_obj else 0.0


class EvBalancerDwellRemainingSensor(_EvBalancerBase, SensorEntity):
    _attr_unique_id = "ev_balancer_dwell_remaining_s"
    _attr_name = "Dwell Remaining"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    @property
    def native_value(self) -> float:
        return self.coordinator.dwell_remaining_s

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "active_cooldowns": [
                {"type": k, "remaining_s": round(v, 1)}
                for k, v in self.coordinator.active_cooldowns
            ]
        }
