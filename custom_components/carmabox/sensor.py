"""CARMA Box — Sensors.

Exposes optimizer state as HA sensors for dashboard + automations.
Uses SensorEntityDescription pattern (Shelly-standard).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import APPLIANCE_CATEGORIES, DOMAIN
from .coordinator import CarmaboxCoordinator
from .optimizer.models import BatteryCommand
from .optimizer.savings import (
    daily_trend,
    peak_comparison,
    savings_breakdown,
    savings_whatif,
    total_savings,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


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
    if last == BatteryCommand.BMS_COLD_LOCK:
        return "cold_lock"  # IT-1948: BMS cold lock (cell temp < 10°C)
    if last == BatteryCommand.CHARGE_PV_TAPER:
        return "charging_taper"  # IT-1939: BMS taper state
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
        "taper_active": coord._taper_active,
        "cold_lock_active": coord._cold_lock_active,
        "cell_temp_kontor": (
            coord.data.battery_min_cell_temp_1
            if coord.data and hasattr(coord.data, "battery_min_cell_temp_1")
            else None
        ),
        "cell_temp_forrad": (
            coord.data.battery_min_cell_temp_2
            if coord.data and hasattr(coord.data, "battery_min_cell_temp_2")
            else None
        ),
    }
    # PV allocation plan (per-hour table for dashboard)
    if hasattr(coord, "_pv_allocation") and coord._pv_allocation:
        attrs["pv_allocation"] = coord._pv_allocation

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
        for e in list(coord.decision_log)
    ]
    return attrs


def _plan_accuracy_value(coord: CarmaboxCoordinator) -> float | None:
    """Plan accuracy: how close actual grid matched plan (min/max ratio, weighted average).

    Formula per hour: min(planned, actual) / max(planned, actual) x 100
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
        "summary": (
            f"Köpte {avg_buy:.0f} öre, sålde {avg_sell:.0f} öre = {ratio:.1f}x"
            if ratio > 0
            else "Ingen data"
        ),
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
        "summary": (
            f"Nätladdade vid {avg_buy:.0f} öre (snitt {avg_daily:.0f} öre)"
            if avg_buy > 0
            else "Ingen data"
        ),
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


def _status_value(coord: CarmaboxCoordinator) -> str:
    """PLAT-964: Transparency sensor — user-friendly status."""
    return coord.status_text


def _status_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """PLAT-964: System health per component."""
    return {"system_health": coord.system_health}


def _plan_score_value(coord: CarmaboxCoordinator) -> float | None:
    """PLAT-966: Plan score — how well plan matched reality."""
    scores = coord.plan_score()
    return scores.get("score_today")


def _plan_score_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """PLAT-966: Plan score details with trend."""
    return coord.plan_score()


def _energy_ledger_value(coord: CarmaboxCoordinator) -> str:
    """Energy ledger summary — today's battery saving."""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    summary = coord.ledger.daily_summary(today)
    saving = summary.get("battery_net_saving_kr", 0)
    cost = summary.get("total_cost_kr", 0)
    hours = summary.get("hours", 0)
    return f"{saving:.1f} kr sparat, {cost:.1f} kr total ({hours}h)"


def _energy_ledger_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Energy ledger full data — hourly table + daily summary."""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    return coord.ledger.daily_summary(today)


def _daily_insight_value(coord: CarmaboxCoordinator) -> str:
    """Daily insight summary — one-liner for the sensor state."""
    insight = coord.daily_insight
    if insight.get("status") == "collecting":
        return "Samlar data"
    recs = insight.get("recommendation_count", 0)
    max_kw = insight.get("ellevio_max_kw", 0)
    cost = insight.get("total_cost_kr", 0)
    return f"Max {max_kw:.1f} kW, kostnad {cost:.0f} kr, {recs} rekommendationer"


def _daily_insight_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Daily insight full data — Ellevio + Nordpool + recommendations."""
    return coord.daily_insight


def _household_insights_value(coord: CarmaboxCoordinator) -> str:
    """PLAT-962: Monthly household insight — comparison vs similar households."""
    bench = coord.benchmark_data
    if not bench or bench.get("similar_households", 0) < 10:
        return "Samlar data"
    diff = bench.get("diff_pct", 0.0)
    if diff < -5:
        return f"{abs(diff):.0f}% under snittet"
    if diff > 5:
        return f"{diff:.0f}% över snittet"
    return "Nära snittet"


def _household_insights_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """PLAT-962: Household insights details — benchmarking, tips, ROI."""
    bench = coord.benchmark_data
    if not bench:
        return {
            "status": "waiting",
            "message": "Samlar in data. Benchmarking kräver minst 10 liknande hushåll.",
        }
    return {
        "similar_households": bench.get("similar_households", 0),
        "comparison_group": bench.get("comparison_group", ""),
        "your_monthly_kwh": bench.get("your_monthly_kwh", 0),
        "avg_monthly_kwh": bench.get("avg_monthly_kwh", 0),
        "diff_pct": bench.get("diff_pct", 0),
        "trend_3m": bench.get("trend_3m", ""),
        "your_savings_kr": bench.get("your_savings_kr", 0),
        "avg_savings_kr": bench.get("avg_savings_kr", 0),
        "savings_rank_pct": bench.get("savings_rank_pct", 0),
        "tips": bench.get("tips", []),
        "battery_roi_months": bench.get("battery_roi_months", 0),
        "solar_roi_months": bench.get("solar_roi_months", 0),
        "updated": bench.get("updated", ""),
    }


def _rules_value(coord: CarmaboxCoordinator) -> str:
    """IT-1937: Current active rule name."""
    active_rule_id = getattr(coord, "_active_rule_id", None)
    if not active_rule_id:
        return "Ingen aktiv regel"

    # Map rule_id to human-readable name
    rule_names = {
        "RULE_0": "Safety guard",
        "RULE_0_5": "Solar charge",
        "RULE_1": "Export guard",
        "RULE_1_5": "Cheap grid charge",
        "RULE_1_8": "Proactive discharge",
        "RULE_2": "Peak shaving",
        "RULE_4": "Idle / standby",
    }
    return str(rule_names.get(active_rule_id, active_rule_id))


def _rules_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """IT-1937: Rules table with all rules, active state, parameters, and history."""

    if not coord.data:
        return {"status": "no_data"}

    # Define all rules with metadata
    all_rules = [
        {
            "id": "RULE_0",
            "name": "Safety guard",
            "priority": 1,
            "condition": f"SoC < {coord.min_soc}% → nödstopp",
            "parameters": {
                "min_soc": coord.min_soc,
            },
        },
        {
            "id": "RULE_0_5",
            "name": "Solar charge",
            "priority": 2,
            "condition": "PV > 500W + batteri ej fullt + export → charge_pv",
            "parameters": {
                "min_pv_kw": 0.5,
            },
        },
        {
            "id": "RULE_1",
            "name": "Export guard",
            "priority": 3,
            "condition": "Exporterar (grid < 0) → charge_pv / standby",
            "parameters": {},
        },
        {
            "id": "RULE_1_5",
            "name": "Cheap grid charge",
            "priority": 4,
            "condition": (
                f"Pris < {coord._cfg.get('grid_charge_price_threshold', 15):.0f}"
                f" öre + SoC < 90% → grid_charge"
            ),
            "parameters": {
                "price_threshold_ore": float(coord._cfg.get("grid_charge_price_threshold", 15)),
                "max_soc": float(coord._cfg.get("grid_charge_max_soc", 90)),
            },
        },
        {
            "id": "RULE_1_8",
            "name": "Proactive discharge",
            "priority": 5,
            "condition": "SoC hög + grid import + dagtid → eliminera import",
            "parameters": {
                "min_soc_threshold": "40-90% (beroende på väder)",
                "min_grid_w": "50-300W (beroende på sol/natt)",
            },
        },
        {
            "id": "RULE_2",
            "name": "Peak shaving",
            "priority": 6,
            "condition": f"Grid viktat > {coord.target_kw:.1f} kW → discharge",
            "parameters": {
                "target_kw": coord.target_kw,
                "hysteresis": 0.9,
            },
        },
        {
            "id": "RULE_4",
            "name": "Idle / standby",
            "priority": 7,
            "condition": f"Grid viktat < {coord.target_kw:.1f} kW → vila",
            "parameters": {
                "target_kw": coord.target_kw,
            },
        },
    ]

    # Mark active rule
    active_rule_id = getattr(coord, "_active_rule_id", None)
    rule_triggers = getattr(coord, "_rule_triggers", {})

    for rule in all_rules:
        rule["active"] = rule["id"] == active_rule_id
        trigger_data = rule_triggers.get(rule["id"], {})
        rule["last_triggered"] = trigger_data.get("timestamp", None)
        rule["result"] = trigger_data.get("result", None)

    return {
        "rules": all_rules,
        "active_rule": active_rule_id,
        "rule_count": len(all_rules),
    }


# ── IT-2378: Intelligent Scheduler sensors ────────────────────────


def _scheduler_last_breach_value(coord: CarmaboxCoordinator) -> str:
    """Last breach description."""
    breaches = coord.scheduler_plan.breaches
    if not breaches:
        return "Inga överträdelser"
    return str(breaches[-1].root_cause)[:200]


def _scheduler_last_breach_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Last breach details."""
    breaches = coord.scheduler_plan.breaches
    if not breaches:
        return {"breach_count_month": coord.scheduler_plan.breach_count_month}
    b = breaches[-1]
    return {
        "timestamp": b.timestamp,
        "hour": b.hour,
        "actual_weighted_kw": b.actual_weighted_kw,
        "target_kw": b.target_kw,
        "loads_active": b.loads_active,
        "root_cause": b.root_cause,
        "remediation": b.remediation,
        "severity": b.severity,
        "breach_count_month": coord.scheduler_plan.breach_count_month,
        "learnings": [
            {"pattern": lr.pattern, "action": lr.action, "confidence": lr.confidence}
            for lr in (coord.scheduler_plan.learnings or [])[:10]
        ],
    }


def _scheduler_breach_count_value(coord: CarmaboxCoordinator) -> int:
    """Monthly breach count."""
    return coord.scheduler_plan.breach_count_month


def _scheduler_24h_plan_value(coord: CarmaboxCoordinator) -> str:
    """24h plan status summary."""
    plan = coord.scheduler_plan
    if not plan.slots:
        return "Ingen plan"
    violations = sum(1 for s in plan.slots if not s.constraint_ok)
    if violations:
        return f"Plan aktiv — {violations} varning(ar)"
    return "Plan aktiv — alla timmar OK"


def _scheduler_24h_plan_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Full 24h scheduler plan for dashboard."""
    plan = coord.scheduler_plan
    slots_data = []
    for s in plan.slots:
        slots_data.append(
            {
                "h": s.hour,
                "a": s.action,
                "bat_kw": s.battery_kw,
                "ev_kw": s.ev_kw,
                "ev_a": s.ev_amps,
                "miner": s.miner_on,
                "grid": s.grid_kw,
                "w_kw": s.weighted_kw,
                "pv": s.pv_kw,
                "load": s.consumption_kw,
                "price": s.price,
                "soc": s.battery_soc,
                "ev_soc": s.ev_soc,
                "ok": s.constraint_ok,
                "reason": s.reasoning,
            }
        )
    return {
        "target_weighted_kw": plan.target_weighted_kw,
        "max_weighted_kw": plan.max_weighted_kw,
        "total_ev_kwh": plan.total_ev_kwh,
        "ev_soc_at_06": plan.ev_soc_at_06,
        "total_charge_kwh": plan.total_charge_kwh,
        "total_discharge_kwh": plan.total_discharge_kwh,
        "estimated_cost_kr": plan.estimated_cost_kr,
        "slots": slots_data,
    }


def _scheduler_ev_full_charge_value(coord: CarmaboxCoordinator) -> str:
    """Next planned EV 100% charge date."""
    return coord.scheduler_plan.ev_next_full_charge_date or "Ej planerad"


def _idle_analysis_value(coord: CarmaboxCoordinator) -> int:
    """Battery utilization score (0 if no idle analysis)."""
    ia = coord.scheduler_plan.idle_analysis
    if ia is None:
        return 0
    return ia.score


def _idle_analysis_attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
    """Idle analysis extra attributes."""
    ia = coord.scheduler_plan.idle_analysis
    if ia is None:
        return {}
    return {
        "idle_hours_today": ia.idle_hours_today,
        "idle_pct": ia.idle_pct,
        "missed_charge_kwh": ia.missed_charge_kwh,
        "missed_discharge_kwh": ia.missed_discharge_kwh,
        "missed_savings_kr": ia.missed_savings_kr,
        "opportunities": ia.opportunities,
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
        key="rules",
        translation_key="rules",
        icon="mdi:table-settings",
        value_fn=_rules_value,
        extra_attrs_fn=_rules_attrs,
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
        value_fn=lambda coord: (round(coord.data.total_battery_soc, 0) if coord.data else 0),
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
    CarmaboxSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:heart-pulse",
        value_fn=_status_value,
        extra_attrs_fn=_status_attrs,
    ),
    CarmaboxSensorDescription(
        key="plan_score",
        translation_key="plan_score",
        icon="mdi:chart-line",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_plan_score_value,
        extra_attrs_fn=_plan_score_attrs,
    ),
    CarmaboxSensorDescription(
        key="household_insights",
        translation_key="household_insights",
        icon="mdi:home-analytics",
        value_fn=_household_insights_value,
        extra_attrs_fn=_household_insights_attrs,
    ),
    CarmaboxSensorDescription(
        key="daily_insight",
        translation_key="daily_insight",
        icon="mdi:lightbulb-on",
        value_fn=_daily_insight_value,
        extra_attrs_fn=_daily_insight_attrs,
    ),
    CarmaboxSensorDescription(
        key="rule_flow",
        translation_key="rule_flow",
        icon="mdi:sitemap",
        value_fn=lambda c: c.rule_flow.get("active_rule", "idle"),
        extra_attrs_fn=lambda c: c.rule_flow,
    ),
    CarmaboxSensorDescription(
        key="energy_ledger",
        translation_key="energy_ledger",
        icon="mdi:book-open-page-variant",
        value_fn=_energy_ledger_value,
        extra_attrs_fn=_energy_ledger_attrs,
    ),
    # ── IT-2378: Intelligent Scheduler ────────────────────────
    CarmaboxSensorDescription(
        key="scheduler_last_breach",
        translation_key="scheduler_last_breach",
        icon="mdi:alert-circle",
        value_fn=_scheduler_last_breach_value,
        extra_attrs_fn=_scheduler_last_breach_attrs,
    ),
    CarmaboxSensorDescription(
        key="scheduler_breach_count_month",
        translation_key="scheduler_breach_count_month",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL,
        value_fn=_scheduler_breach_count_value,
    ),
    CarmaboxSensorDescription(
        key="scheduler_24h_plan",
        translation_key="scheduler_24h_plan",
        icon="mdi:calendar-clock",
        value_fn=_scheduler_24h_plan_value,
        extra_attrs_fn=_scheduler_24h_plan_attrs,
    ),
    CarmaboxSensorDescription(
        key="scheduler_ev_next_full_charge",
        translation_key="scheduler_ev_next_full_charge",
        icon="mdi:car-electric",
        value_fn=_scheduler_ev_full_charge_value,
    ),
    CarmaboxSensorDescription(
        key="battery_idle_today",
        translation_key="battery_idle_today",
        icon="mdi:battery-clock-outline",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coord: coord._bat_daily_idle_seconds // 60,
    ),
    CarmaboxSensorDescription(
        key="battery_utilization_score",
        translation_key="battery_utilization_score",
        icon="mdi:battery-charging-high",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_idle_analysis_value,
        extra_attrs_fn=_idle_analysis_attrs,
    ),
    CarmaboxSensorDescription(
        key="breach_monitor_projected",
        translation_key="breach_monitor_projected",
        icon="mdi:chart-timeline-variant",
        native_unit_of_measurement="kW",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coord: coord.hourly_meter_projected,
    ),
    CarmaboxSensorDescription(
        key="breach_monitor_pct",
        translation_key="breach_monitor_pct",
        icon="mdi:gauge",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coord: coord.hourly_meter_pct,
        extra_attrs_fn=lambda coord: {
            "load_shed_active": coord.breach_monitor_active,
            "samples_this_hour": len(coord._meter_state.samples),
            "peak_sample_kw": coord._meter_state.peak_sample,
            "warning_issued": coord._meter_state.warning_issued,
            "active_corrections": len(coord.get_active_corrections()),
        },
    ),
)


def _appliance_value_factory(category: str) -> Callable[[CarmaboxCoordinator], float]:
    """Create a value function for a specific appliance category."""

    def _value(coord: CarmaboxCoordinator) -> float:
        return round(coord.appliance_power.get(category, 0.0), 1)

    return _value


def _appliance_attrs_factory(
    category: str,
) -> Callable[[CarmaboxCoordinator], dict[str, Any]]:
    """Create an attrs function for a specific appliance category."""

    def _attrs(coord: CarmaboxCoordinator) -> dict[str, Any]:
        energy_wh = coord.appliance_energy_wh.get(category, 0.0)
        # List individual appliances in this category
        members = [app for app in coord._appliances if app.get("category") == category]
        return {
            "energy_today_kwh": round(energy_wh / 1000, 2),
            "appliances": [{"entity_id": m["entity_id"], "name": m["name"]} for m in members],
        }

    return _attrs


def _build_appliance_descriptions(
    appliances: list[dict[str, Any]],
) -> list[CarmaboxSensorDescription]:
    """Build sensor descriptions for each appliance category found in config."""
    categories_in_use: set[str] = set()
    for app in appliances:
        cat = app.get("category", "other")
        categories_in_use.add(cat)

    descriptions: list[CarmaboxSensorDescription] = []
    for cat in sorted(categories_in_use):
        label = APPLIANCE_CATEGORIES.get(cat, cat)
        descriptions.append(
            CarmaboxSensorDescription(
                key=f"appliance_{cat}",
                translation_key=f"appliance_{cat}",
                icon="mdi:lightning-bolt",
                name=f"Förbrukning {label}",
                native_unit_of_measurement="W",
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
                value_fn=_appliance_value_factory(cat),
                extra_attrs_fn=_appliance_attrs_factory(cat),
            )
        )
    return descriptions


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CARMA Box sensors from EntityDescription."""
    coordinator: CarmaboxCoordinator = entry.runtime_data
    entities = [CarmaboxSensor(coordinator, entry, desc) for desc in SENSOR_DESCRIPTIONS]

    # PLAT-943: Add per-category appliance sensors
    appliances = list(entry.options.get("appliances") or entry.data.get("appliances") or [])
    for desc in _build_appliance_descriptions(appliances):
        entities.append(CarmaboxSensor(coordinator, entry, desc))

    async_add_entities(entities)


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
