"""CARMA Box — Sensors.

Exposes optimizer state as HA sensors for dashboard + automations.
Uses SensorEntityDescription pattern (Shelly-standard).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BatteryCommand, CarmaboxCoordinator
from .optimizer.savings import (
    daily_trend,
    peak_comparison,
    savings_breakdown,
    savings_whatif,
    total_savings,
)


@dataclass(frozen=True, kw_only=True)
class CarmaboxSensorDescription(SensorEntityDescription):
    """Describes a CARMA Box sensor."""

    value_fn: Callable[[CarmaboxCoordinator], Any] = lambda _: None
    extra_attrs_fn: Callable[[CarmaboxCoordinator], dict[str, Any]] | None = None


def _plan_status_value(coord: CarmaboxCoordinator) -> str:
    """Current plan status."""
    if coord.data is None:
        return "unknown"
    state = coord.data
    if state.is_exporting:
        return "charging_pv"
    if state.all_batteries_full:
        return "standby"
    last = coord._last_command
    if last == BatteryCommand.DISCHARGE:
        return "discharging"
    if last == BatteryCommand.CHARGE_PV:
        return "charging"
    return "idle"


def _plan_status_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Plan status extra attributes including full plan data for dashboard card."""
    if coord.data is None:
        return {}
    state = coord.data
    # Serialize plan for the Lovelace card
    plan_data = []
    for hp in state.plan:
        plan_data.append(
            {
                "h": hp.hour,
                "a": hp.action,
                "p": round(hp.price, 1),
                "soc": hp.battery_soc,
                "grid": round(hp.grid_kw, 2),
                "bat": round(hp.battery_kw, 2),
                "ev_soc": hp.ev_soc,
            }
        )
    return {
        "target_weighted_kw": state.target_weighted_kw,
        "grid_power_w": state.grid_power_w,
        "battery_soc_1": state.battery_soc_1,
        "battery_soc_2": state.battery_soc_2 if state.has_battery_2 else None,
        "ev_soc": state.ev_soc if state.has_ev else None,
        "is_exporting": state.is_exporting,
        "plan_hours": len(state.plan),
        "plan": plan_data,
    }


def _savings_value(coord: CarmaboxCoordinator) -> float:
    """Current month savings."""
    cost = float(coord.entry.options.get("peak_cost_per_kw", 80.0))
    return total_savings(coord.savings, cost)


def _savings_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Savings breakdown with what-if, trend, and peak comparison."""
    cost = float(coord.entry.options.get("peak_cost_per_kw", 80.0))
    attrs: dict[str, Any] = dict(savings_breakdown(coord.savings, cost))
    attrs["whatif"] = savings_whatif(coord.savings, cost)
    attrs["trend"] = daily_trend(coord.savings)
    attrs["peaks"] = peak_comparison(coord.savings)
    return attrs


def _decision_value(coord: CarmaboxCoordinator) -> str:
    """Current decision reason."""
    d = coord.last_decision
    return d.reason if d.reason else "Ingen data"


def _decision_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Decision details + log."""
    d = coord.last_decision
    attrs: dict[str, Any] = {
        "action": d.action,
        "reason_text": d.reason,
        "target_kw": d.target_kw,
        "grid_kw": d.grid_kw,
        "weighted_kw": d.weighted_kw,
        "price_ore": d.price_ore,
        "battery_soc": d.battery_soc,
        "ev_soc": d.ev_soc,
        "pv_kw": d.pv_kw,
        "discharge_w": d.discharge_w,
        "safety_blocked": d.safety_blocked,
        "timestamp": d.timestamp,
        "analyze_only": not coord.executor_enabled,
    }
    # Last 24h decisions as compact list (max 48 entries)
    attrs["decisions_24h"] = [
        {
            "timestamp": e.timestamp,
            "action": e.action,
            "reason_text": e.reason[:120],
            "target_kw": e.target_kw,
            "grid_kw": e.grid_kw,
            "weighted_kw": e.weighted_kw,
            "price_ore": e.price_ore,
            "battery_soc": e.battery_soc,
            "ev_soc": e.ev_soc,
            "pv_kw": e.pv_kw,
        }
        for e in coord.decision_log[-48:]
    ]
    return attrs


def _plan_accuracy_value(coord: CarmaboxCoordinator) -> float | None:
    """Plan accuracy: how close actual grid matched plan (min/max ratio, weighted average).

    Formula per hour: min(planned, actual) / max(planned, actual) × 100
    Example: plan=2.0, actual=2.3 → 2.0/2.3 = 87%
    Hours where both are near-zero count as 100% (nothing to compare).
    """
    actuals = coord.hourly_actuals
    if len(actuals) < 2:
        return None
    total_accuracy = 0.0
    counted = 0
    for a in actuals:
        p = abs(a.planned_weighted_kw)
        r = abs(a.actual_weighted_kw)
        if p < 0.01 and r < 0.01:
            total_accuracy += 100.0  # Both near-zero = perfect match
        else:
            lo, hi = min(p, r), max(p, r)
            total_accuracy += (lo / hi) * 100
        counted += 1
    if counted == 0:
        return None
    return round(total_accuracy / counted, 0)


def _plan_accuracy_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Plan accuracy details with 24h history and goal tracking."""
    actuals = coord.hourly_actuals
    accuracy = _plan_accuracy_value(coord)
    history = [
        {
            "h": a.hour,
            "plan_grid_kw": a.planned_grid_kw,
            "actual_grid_kw": a.actual_grid_kw,
            "plan_kw": a.planned_weighted_kw,
            "actual_kw": a.actual_weighted_kw,
            "plan_action": a.planned_action,
            "actual_action": a.actual_action,
            "bat_plan": a.planned_battery_soc,
            "bat_actual": a.actual_battery_soc,
            "ev_plan": a.planned_ev_soc,
            "ev_actual": a.actual_ev_soc,
            "price": a.price,
        }
        for a in actuals[-24:]
    ]
    return {
        "hours_tracked": len(actuals),
        "goal_pct": 70,
        "goal_met": accuracy is not None and accuracy >= 70,
        "history": history,
    }


SENSOR_DESCRIPTIONS: tuple[CarmaboxSensorDescription, ...] = (
    CarmaboxSensorDescription(
        key="plan_accuracy",
        translation_key="plan_accuracy",
        icon="mdi:bullseye-arrow",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_plan_accuracy_value,
        extra_attrs_fn=_plan_accuracy_attrs,
    ),
    CarmaboxSensorDescription(
        key="decision",
        translation_key="decision",
        icon="mdi:head-lightbulb",
        value_fn=_decision_value,
        extra_attrs_fn=_decision_attrs,
    ),
    CarmaboxSensorDescription(
        key="plan_status",
        translation_key="plan_status",
        icon="mdi:calendar-check",
        value_fn=_plan_status_value,
        extra_attrs_fn=_plan_status_attrs,
    ),
    CarmaboxSensorDescription(
        key="target_kw",
        translation_key="target_kw",
        icon="mdi:target",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda coord: float(coord.target_kw),
    ),
    CarmaboxSensorDescription(
        key="savings_month",
        translation_key="savings_month",
        icon="mdi:piggy-bank",
        native_unit_of_measurement="kr",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=_savings_value,
        extra_attrs_fn=_savings_attrs,
    ),
    CarmaboxSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        icon="mdi:battery",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda coord: round(coord.data.total_battery_soc, 0) if coord.data else 0,
    ),
    CarmaboxSensorDescription(
        key="grid_import",
        translation_key="grid_import",
        icon="mdi:transmission-tower-import",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda coord: (
            round(max(0, coord.data.grid_power_w) / 1000, 2) if coord.data else 0
        ),
    ),
    CarmaboxSensorDescription(
        key="ev_soc",
        translation_key="ev_soc",
        icon="mdi:car-electric",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda coord: (
            round(coord.data.ev_soc, 0) if coord.data and coord.data.has_ev else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CARMA Box sensors from EntityDescription."""
    coordinator: CarmaboxCoordinator = entry.runtime_data
    async_add_entities(CarmaboxSensor(coordinator, entry, desc) for desc in SENSOR_DESCRIPTIONS)


class CarmaboxSensor(CoordinatorEntity[CarmaboxCoordinator], SensorEntity):
    """Generic CARMA Box sensor driven by EntityDescription."""

    entity_description: CarmaboxSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CarmaboxCoordinator,
        entry: ConfigEntry,
        description: CarmaboxSensorDescription,
    ) -> None:
        """Initialize sensor from description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"carmabox_{description.key}"
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for CARMA Box."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="CARMA Box",
            manufacturer="CARMA Box",
            model="Energy Optimizer",
            sw_version="1.0.0",
        )

    @property
    def native_value(self) -> Any:
        """Return sensor value via description function."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes if defined."""
        if self.entity_description.extra_attrs_fn:
            return self.entity_description.extra_attrs_fn(self.coordinator)
        return None
