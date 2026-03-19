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
        "reasoning": d.reasoning,
        "reasoning_chain": d.reasoning_chain,
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


def _battery_efficiency_value(coord: CarmaboxCoordinator) -> float | None:
    """Battery buy/sell ratio."""
    s = coord.savings
    if s.charge_from_grid_kwh < 0.01 or s.discharge_offset_kwh < 0.01:
        return None
    avg_buy = s.charge_from_grid_cost_ore / s.charge_from_grid_kwh
    avg_sell = s.discharge_offset_value_ore / s.discharge_offset_kwh
    return round(avg_sell / avg_buy, 1) if avg_buy > 0.01 else None


def _battery_efficiency_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Battery efficiency details."""
    s = coord.savings
    avg_buy = (
        round(s.charge_from_grid_cost_ore / s.charge_from_grid_kwh, 1)
        if s.charge_from_grid_kwh > 0.01
        else 0.0
    )
    avg_sell = (
        round(s.discharge_offset_value_ore / s.discharge_offset_kwh, 1)
        if s.discharge_offset_kwh > 0.01
        else 0.0
    )
    ratio = round(avg_sell / avg_buy, 1) if avg_buy > 0.01 else 0.0
    return {
        "avg_buy_price_ore": avg_buy,
        "avg_sell_price_ore": avg_sell,
        "ratio": ratio,
        "summary": f"Köpte {avg_buy:.0f} öre, sålde {avg_sell:.0f} öre = {ratio:.1f}x"
        if ratio > 0
        else "Ingen data",
        "charge_from_grid_kwh": round(s.charge_from_grid_kwh, 2),
        "discharge_offset_kwh": round(s.discharge_offset_kwh, 2),
    }


def _optimization_score_value(coord: CarmaboxCoordinator) -> float | None:
    """CARMA Box vs native peak shaving score."""
    s = coord.savings
    if len(s.baseline_peak_samples) < 3 or len(s.peak_samples) < 3:
        return None
    baseline = sorted(s.baseline_peak_samples, reverse=True)
    carma = sorted(s.peak_samples, reverse=True)
    base_avg = sum(baseline[:3]) / 3
    carma_avg = sum(carma[:3]) / 3
    if base_avg < 0.01:
        return None
    return round(max(0, (base_avg - carma_avg) / base_avg * 100), 0)


def _optimization_score_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Optimization score details."""
    s = coord.savings
    cost = float(coord._cfg.get("peak_cost_per_kw", 80.0))
    baseline = sorted(s.baseline_peak_samples, reverse=True)[:3]
    carma = sorted(s.peak_samples, reverse=True)[:3]
    base_avg = sum(baseline) / len(baseline) if baseline else 0.0
    carma_avg = sum(carma) / len(carma) if carma else 0.0
    return {
        "native_top3_avg_kw": round(base_avg, 2),
        "carma_top3_avg_kw": round(carma_avg, 2),
        "native_monthly_kr": round(base_avg * cost, 0),
        "carma_monthly_kr": round(carma_avg * cost, 0),
        "saved_kr": round((base_avg - carma_avg) * cost, 0),
    }


def _grid_charge_efficiency_value(coord: CarmaboxCoordinator) -> float | None:
    """How much cheaper we charge vs daily average."""
    s = coord.savings
    if s.charge_from_grid_kwh < 0.01:
        return None
    avg_buy = s.charge_from_grid_cost_ore / s.charge_from_grid_kwh
    avg_daily = coord._daily_avg_price
    if avg_daily < 0.01:
        return None
    return round(max(0, (1 - avg_buy / avg_daily) * 100), 0)


def _grid_charge_efficiency_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Grid charge efficiency details."""
    s = coord.savings
    avg_buy = (
        round(s.charge_from_grid_cost_ore / s.charge_from_grid_kwh, 1)
        if s.charge_from_grid_kwh > 0.01
        else 0.0
    )
    avg_daily = round(coord._daily_avg_price, 1)
    prices = s.grid_charge_prices
    return {
        "avg_charge_price_ore": avg_buy,
        "avg_daily_price_ore": avg_daily,
        "summary": f"Nätladdade vid {avg_buy:.0f} öre (snitt {avg_daily:.0f} öre)"
        if avg_buy > 0
        else "Ingen data",
        "total_grid_charge_kwh": round(s.charge_from_grid_kwh, 2),
        "price_min": round(min(prices), 1) if prices else 0.0,
        "price_max": round(max(prices), 1) if prices else 0.0,
    }


def _ellevio_realtime_value(coord: CarmaboxCoordinator) -> float | None:
    """Current hour rolling weighted average."""
    samples = coord._ellevio_hour_samples
    if not samples:
        return None
    total = sum(p * w for p, w in samples)
    wt = sum(w for _, w in samples)
    return round(total / wt, 2) if wt > 0.01 else None


def _ellevio_realtime_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Ellevio realtime: current hour + monthly top-3."""
    peaks = sorted(coord._ellevio_monthly_hourly_peaks, reverse=True)
    top3 = peaks[:3]
    top3_avg = round(sum(top3) / len(top3), 2) if top3 else 0.0
    cost = float(coord._cfg.get("peak_cost_per_kw", 80.0))
    return {
        "samples_this_hour": len(coord._ellevio_hour_samples),
        "top1_kw": round(top3[0], 2) if len(top3) >= 1 else 0.0,
        "top2_kw": round(top3[1], 2) if len(top3) >= 2 else 0.0,
        "top3_kw": round(top3[2], 2) if len(top3) >= 3 else 0.0,
        "top3_avg_kw": top3_avg,
        "estimated_monthly_cost_kr": round(top3_avg * cost, 0),
        "total_hours_tracked": len(peaks),
    }


def _shadow_value(coord: CarmaboxCoordinator) -> str:
    """Shadow comparison: agree or disagree."""
    s = coord.shadow
    if not s.timestamp:
        return "Ingen data"
    if s.agreement:
        return f"Eniga: {s.carma_action}"
    return s.reason if s.reason else f"CARMA: {s.carma_action}, v6: {s.actual_action}"


def _shadow_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Shadow mode details."""
    s = coord.shadow
    log = coord.shadow_log
    agree_count = sum(1 for x in log if x.agreement)
    total = len(log)
    agree_pct = round(agree_count / total * 100, 0) if total > 0 else 0

    return {
        "carma_action": s.carma_action,
        "actual_action": s.actual_action,
        "agreement": s.agreement,
        "agreement_pct_24h": agree_pct,
        "carma_weighted_kw": s.carma_weighted_kw,
        "actual_weighted_kw": s.actual_weighted_kw,
        "carma_better_kr": s.carma_better_kr,
        "cumulative_savings_kr": round(coord._shadow_savings_kr, 2),
        "price_ore": s.price_ore,
        "reason": s.reason,
        "disagreements_24h": [
            {
                "time": x.timestamp[11:16],
                "carma": x.carma_action,
                "v6": x.actual_action,
                "reason": x.reason[:100],
                "savings_kr": x.carma_better_kr,
            }
            for x in log
            if not x.agreement
        ][-12:],
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
        state_class=SensorStateClass.MEASUREMENT,
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
    CarmaboxSensorDescription(
        key="battery_efficiency",
        translation_key="battery_efficiency",
        icon="mdi:battery-arrow-up",
        native_unit_of_measurement="x",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_battery_efficiency_value,
        extra_attrs_fn=_battery_efficiency_attrs,
    ),
    CarmaboxSensorDescription(
        key="optimization_score",
        translation_key="optimization_score",
        icon="mdi:trophy",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_optimization_score_value,
        extra_attrs_fn=_optimization_score_attrs,
    ),
    CarmaboxSensorDescription(
        key="grid_charge_efficiency",
        translation_key="grid_charge_efficiency",
        icon="mdi:lightning-bolt-circle",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_grid_charge_efficiency_value,
        extra_attrs_fn=_grid_charge_efficiency_attrs,
    ),
    CarmaboxSensorDescription(
        key="ellevio_realtime",
        translation_key="ellevio_realtime",
        icon="mdi:gauge",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_ellevio_realtime_value,
        extra_attrs_fn=_ellevio_realtime_attrs,
    ),
    CarmaboxSensorDescription(
        key="shadow",
        translation_key="shadow",
        icon="mdi:compare-horizontal",
        value_fn=_shadow_value,
        extra_attrs_fn=_shadow_attrs,
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
