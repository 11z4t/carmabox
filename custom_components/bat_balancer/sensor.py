"""Sensor platform for bat_balancer — exposes capability + avg_soc to Brain."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BANKS, DOMAIN
from .coordinator import BatBalancerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BatBalancerCoordinator = hass.data[DOMAIN][entry.entry_id]

    # #42: migrate stale template-platform registry entries so Python sensors
    # can claim the canonical entity_id without getting a _2 suffix.
    registry = er.async_get(hass)
    for bank_id in BANKS:
        stale_eid = f"sensor.bat_balancer_distribution_{bank_id}_w"
        stale_entry = registry.async_get(stale_eid)
        if stale_entry and stale_entry.platform == "template":
            _LOGGER.info(
                "bat_balancer #42: removing stale template registry entry %s (unique_id=%s)",
                stale_eid,
                stale_entry.unique_id,
            )
            registry.async_remove(stale_entry.entity_id)

    async_add_entities(
        [
            BatBalancerCapabilitySensor(coordinator, entry),
            BatBalancerAvgSocSensor(coordinator, entry),
            *[BatBalancerDistributionSensor(coordinator, entry, bid) for bid in BANKS],
            BatBalancerActualTotalSensor(coordinator, entry),
            *[BatBalancerActualBankSensor(coordinator, entry, bid) for bid in BANKS],
            BatBalancerHwOvershootSensor(coordinator, entry),
            BatBalancerCorrectionActiveSensor(coordinator, entry),
            BatBalancerReasonSensor(coordinator, entry),
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


class BatBalancerDistributionSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_distribution_{bank_id}_w — mirrors exact ems_power_limit write.

    Sign: negative = charge, positive = discharge (same as brain_target convention).
    Replaces legacy Jinja template (S5 fix — template computed stale, independent algorithm).
    """

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:flash"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: BatBalancerCoordinator, entry: ConfigEntry, bank_id: str
    ) -> None:
        super().__init__(coordinator)
        self._bank_id = bank_id
        self._attr_name = f"Bat Balancer Distribution {bank_id.capitalize()} W"
        self._attr_unique_id = f"{entry.entry_id}_distribution_{bank_id}_w"
        self.entity_id = f"sensor.bat_balancer_distribution_{bank_id}_w"

    @property
    def native_value(self) -> float:
        return self.coordinator.distribution_w.get(self._bank_id, 0.0)


# ── W6: closed-loop HW-feedback sensors ─────────────────────────────────────


class BatBalancerActualTotalSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_actual_total_w — sum of HW-measured battery power (signed)."""

    _attr_name = "Bat Balancer Actual Total W"
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:flash-circle"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_actual_total_w"
        self.entity_id = "sensor.bat_balancer_actual_total_w"

    @property
    def native_value(self) -> float:
        return round(sum(self.coordinator.hw_actual_w.values()), 1)


class BatBalancerActualBankSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_actual_{bank_id}_w — per-bank HW-measured battery power."""

    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:flash-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: BatBalancerCoordinator, entry: ConfigEntry, bank_id: str
    ) -> None:
        super().__init__(coordinator)
        self._bank_id = bank_id
        self._attr_name = f"Bat Balancer Actual {bank_id.capitalize()} W"
        self._attr_unique_id = f"{entry.entry_id}_actual_{bank_id}_w"
        self.entity_id = f"sensor.bat_balancer_actual_{bank_id}_w"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.hw_actual_w.get(self._bank_id, 0.0), 1)


class BatBalancerHwOvershootSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_hw_overshoot_w — EMA-smoothed HW overshoot vs offer."""

    _attr_name = "Bat Balancer HW Overshoot W"
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_hw_overshoot_w"
        self.entity_id = "sensor.bat_balancer_hw_overshoot_w"

    @property
    def native_value(self) -> float:
        return self.coordinator.hw_overshoot_w


class BatBalancerCorrectionActiveSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_correction_active — True while W6 EMA clamp is reducing offer."""

    _attr_name = "Bat Balancer Correction Active"
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:tune"

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_correction_active"
        self.entity_id = "sensor.bat_balancer_correction_active"

    @property
    def native_value(self) -> str:
        return "on" if self.coordinator.hw_correction_active else "off"


class BatBalancerReasonSensor(CoordinatorEntity, SensorEntity):
    """sensor.bat_balancer_reason — why distributed_w may differ from offered_w.

    Cross-balancer taxonomy: ok | shadow | manual | overcurrent | hw_not_available |
    soc_floor | soc_ceil | offer_above_max | bms_cap | balancer_disabled | initializing
    Brain reads this for cascade-skip logic.
    """

    _attr_name = "Bat Balancer Reason"
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: BatBalancerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_reason"
        self.entity_id = "sensor.bat_balancer_reason"

    @property
    def native_value(self) -> str:
        return self.coordinator.reason

    @property
    def extra_state_attributes(self) -> dict:
        coord = self.coordinator
        dist = coord.distribution_w
        actual = coord.hw_actual_w
        return {
            "reason": coord.reason,
            "bank_reasons": coord.bank_reasons,
            "offer_w": round(coord._state.last_target_w, 1),
            "distributed_w": {k: round(v, 1) for k, v in dist.items()},
            "actual_w": {k: round(v, 1) for k, v in actual.items()},
            "hw_correction_active": coord.hw_correction_active,
        }
