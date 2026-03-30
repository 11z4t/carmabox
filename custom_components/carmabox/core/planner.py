"""Planner — generates energy plans from prices, PV forecast, consumption.

Pure Python. No HA imports. Fully testable.

Wraps optimizer/planner.py generate_plan() and converts output to
PlanAction objects usable by Plan Executor.

Adds:
  - Solar-aware discharge rate (LAG 7)
  - Correct Ellevio target (never below tak × margin)
  - Dynamic min_soc based on temperature
  - PV forecast daily totals for sunrise detection
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..const import (
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_PEAK_TOP_N,
    P10_DISCHARGE_CONSERVATIVE_KW,
    P10_DISCHARGE_MODERATE_KW,
    P10_DISCHARGE_NORMAL_KW,
)
from ..optimizer.planner import generate_plan
from .plan_executor import PlanAction


@dataclass
class SolarAllocationResult:
    """Result of solar allocation planning."""

    ev_can_charge: bool  # Is there margin to charge EV from PV?
    ev_recommended_amps: int  # 0 = don't charge, 6-10 = charge
    battery_hours_to_full: float  # Hours until batteries reach 100%
    surplus_after_battery_kwh: float  # kWh available for EV after battery needs
    reason: str


def plan_solar_allocation(
    battery_soc_pct: float,
    battery_cap_kwh: float,
    ev_soc_pct: float,
    ev_target_pct: float,
    ev_cap_kwh: float,
    hourly_pv_kw: list[float],  # Remaining hours today, from Solcast
    hourly_consumption_kw: list[float],  # Remaining hours today
    current_hour: int,
    sunset_hour: int = 19,
    ev_phase_count: int = 3,
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS,
    ev_max_amps: int = DEFAULT_EV_MAX_AMPS,
    voltage: float = 230.0,
    pv_confidence: float = 1.0,
) -> SolarAllocationResult:
    """Allocate remaining solar production between battery and EV.

    Pure function. Decides whether there is enough PV surplus (after covering
    battery charging needs and household consumption) to also charge the EV.

    Args:
        pv_confidence: 0.5-1.2 adjustment factor from Tempest pressure +
            Solcast p10. Lower = less trust in PV forecast = prioritize battery.

    Returns SolarAllocationResult with recommendation and reasoning.
    """
    # EV at 100% → nothing to charge
    if ev_soc_pct >= 100:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="EV already at 100%",
        )
    # Note: we do NOT skip when ev_soc >= target. Target is for NIGHT charging.
    # During daytime, free solar kWh to EV is ALWAYS better than export.

    # Hours of sun remaining
    hours_left = max(0, sunset_hour - current_hour)

    # Edge case: no PV hours left (sunset passed or empty lists)
    if hours_left <= 0 or not hourly_pv_kw or not hourly_consumption_kw:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="No solar hours remaining",
        )

    # 1. Battery need
    battery_need_kwh = max(0.0, (100.0 - battery_soc_pct) / 100.0 * battery_cap_kwh)

    # 2. Calculate total surplus from PV after consumption
    n = min(hours_left, len(hourly_pv_kw), len(hourly_consumption_kw))
    if n <= 0:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="No solar hours remaining",
        )

    # Apply PV confidence (Tempest pressure + Solcast p10)
    adjusted_pv = [pv * pv_confidence for pv in hourly_pv_kw]
    surplus_per_hour = [max(0.0, adjusted_pv[h] - hourly_consumption_kw[h]) for h in range(n)]
    total_surplus_kwh = sum(surplus_per_hour)

    # 3. Battery hours to full
    avg_surplus = total_surplus_kwh / n if n > 0 else 0.0
    if avg_surplus > 0 and battery_need_kwh > 0:
        battery_hours_to_full = battery_need_kwh / avg_surplus
    elif battery_need_kwh <= 0:
        battery_hours_to_full = 0.0
    else:
        battery_hours_to_full = float("inf")

    # ── CORE PRINCIPLE ──────────────────────────────────────────
    # The question is NOT "is there margin NOW?"
    # The question IS "will we EXPORT later if we don't EV-charge now?"
    #
    # Rule 1: If battery reaches 100% before sunset → we WILL export
    #         → EV charging NOW prevents that export → ALWAYS do it
    # Rule 2: If total surplus > battery need → excess will export
    #         → EV charging absorbs that excess → do it
    # Rule 3: If surplus < battery need → every kWh to EV = kWh bat misses
    #         → But still better than export later → EV if bat fills anyway
    # Rule 4: Battery already full → ALL surplus to EV

    will_export = False
    export_kwh = 0.0
    ev_reason = ""

    # Rule 4: Battery full — everything to EV
    if battery_soc_pct >= 100.0:
        will_export = total_surplus_kwh > 0
        export_kwh = total_surplus_kwh
        ev_reason = "Battery full — all surplus to EV"

    # Rule 1: Battery fills before sunset — we WILL export after that
    elif battery_hours_to_full > 0 and battery_hours_to_full < n:
        hours_after_full = n - battery_hours_to_full
        export_kwh = avg_surplus * hours_after_full
        will_export = export_kwh > 0.5  # >0.5 kWh meaningful
        ev_reason = (
            f"Battery fills in {battery_hours_to_full:.1f}h, "
            f"{export_kwh:.1f} kWh would export — EV absorbs it"
        )

    # Rule 2: Total surplus exceeds battery need — excess exports
    elif total_surplus_kwh > battery_need_kwh:
        export_kwh = total_surplus_kwh - battery_need_kwh
        will_export = export_kwh > 0.5
        ev_reason = (
            f"Surplus {total_surplus_kwh:.1f} > battery need "
            f"{battery_need_kwh:.1f} — {export_kwh:.1f} kWh would export"
        )

    surplus_after_battery_kwh = max(0.0, export_kwh)

    # If we will NOT export → battery needs everything → no EV
    if not will_export:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=battery_hours_to_full,
            surplus_after_battery_kwh=0.0,
            reason=(
                f"No export risk — all PV needed for battery "
                f"({battery_need_kwh:.1f} kWh need, "
                f"{total_surplus_kwh:.1f} kWh surplus)"
            ),
        )

    # We WILL export → EV should charge to prevent it
    # Calculate amps: use peak surplus hour (EV charges during peak)
    peak_surplus = max(surplus_per_hour) if surplus_per_hour else 0
    ev_kw_available = min(peak_surplus, export_kwh / max(1, n))
    # Start at min_amps — Grid Guard protects Ellevio tak
    # We know export is coming, so EV charging is always justified
    ev_amps_raw = ev_kw_available * 1000.0 / (voltage * ev_phase_count)
    ev_amps = min(max(0, math.ceil(ev_amps_raw)), ev_max_amps)

    # If calculated amps < min_amps but export IS coming:
    # Start at min_amps anyway — Grid Guard protects Ellevio tak
    # Battery charging rate auto-adjusts (GoodWe peak shaving)
    # It's better to use some grid import now than export later
    if ev_amps < ev_min_amps:
        ev_amps = ev_min_amps  # Force minimum — export prevention

    return SolarAllocationResult(
        ev_can_charge=True,
        ev_recommended_amps=ev_amps,
        battery_hours_to_full=battery_hours_to_full,
        surplus_after_battery_kwh=surplus_after_battery_kwh,
        reason=ev_reason,
    )


@dataclass
class PlannerConfig:
    """Planner configuration."""

    ellevio_tak_kw: float = 2.0
    ellevio_night_weight: float = 0.5
    grid_guard_margin: float = 0.85
    battery_min_soc: float = 15.0
    battery_min_soc_cold: float = 20.0
    cold_temp_c: float = 4.0
    grid_charge_price_threshold: float = 15.0
    grid_charge_max_soc: float = 90.0
    max_discharge_kw: float = 5.0
    max_grid_charge_kw: float = 3.0
    battery_efficiency: float = 0.92
    discharge_rate_solar_kw: float = 2.0
    discharge_rate_partial_kw: float = 1.0
    discharge_rate_winter_kw: float = 0.5
    solar_strong_threshold_kwh: float = 25.0
    solar_partial_threshold_kwh: float = 15.0
    ev_phase_count: int = 3
    ellevio_night_weight: float = 0.5


@dataclass
class PlannerInput:
    """All data needed to generate a plan."""

    start_hour: int
    hourly_prices: list[float]  # öre/kWh, starting from start_hour
    hourly_pv: list[float]  # kW per hour
    hourly_loads: list[float]  # kW per hour (consumption)
    hourly_ev: list[float]  # kW per hour (EV demand)
    battery_soc: float  # Weighted average SoC (%)
    battery_cap_kwh: float  # Total battery capacity
    ev_soc: float  # Current EV SoC (%)
    ev_cap_kwh: float  # EV capacity
    pv_forecast_tomorrow_kwh: float  # Total PV forecast for tomorrow
    battery_temps: list[float] | None = None  # Cell temps per battery


def calculate_night_reserve_kwh(
    ev_phase_count: int = 3,
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS,
    house_baseload_kw: float = 2.5,
    grid_max_night_kw: float = 4.0,
    night_hours: int = 8,
    appliance_margin_kwh: float = 3.0,
) -> float:
    """Calculate battery reserve needed for tonight.

    Reserve = (EV_kW + house_kW - grid_max_night) × hours + appliance_margin
    """
    ev_kw = 230 * ev_phase_count * ev_min_amps / 1000
    bat_per_hour = max(0, ev_kw + house_baseload_kw - grid_max_night_kw)
    return bat_per_hour * night_hours + appliance_margin_kwh


def max_daytime_discharge_kwh(
    battery_soc: float,
    battery_cap_kwh: float,
    min_soc: float = 15.0,
    night_reserve_kwh: float = 0.0,
) -> float:
    """How much can be discharged during daytime while preserving night reserve."""
    available = max(0, (battery_soc - min_soc) / 100 * battery_cap_kwh)
    return max(0, available - night_reserve_kwh)


def generate_carma_plan(
    input_data: PlannerInput,
    config: PlannerConfig | None = None,
) -> list[PlanAction]:
    """Generate plan and return as PlanAction list.

    Applies CARMA Box business logic on top of raw planner:
    - Target never below Ellevio tak × margin
    - Dynamic min_soc based on temperature
    - Solar-aware discharge rate
    """
    cfg = config or PlannerConfig()

    # Calculate effective min_soc (temperature-aware)
    min_soc = cfg.battery_min_soc
    if input_data.battery_temps:
        min_temp = min(input_data.battery_temps)
        if min_temp < cfg.cold_temp_c:
            min_soc = cfg.battery_min_soc_cold

    # Target: never below Ellevio tak × margin
    target_kw = cfg.ellevio_tak_kw * cfg.grid_guard_margin

    # Solar-aware max discharge rate
    pv_tomorrow = input_data.pv_forecast_tomorrow_kwh
    if pv_tomorrow > cfg.solar_strong_threshold_kwh:
        max_discharge = cfg.discharge_rate_solar_kw
    elif pv_tomorrow > cfg.solar_partial_threshold_kwh:
        max_discharge = cfg.discharge_rate_partial_kw
    else:
        max_discharge = cfg.discharge_rate_winter_kw

    # ── Night reserve: don't discharge daytime if batteries needed tonight ──
    # Calculate how much battery is needed for tonight's EV support
    ev_kw = 230 * int(getattr(cfg, "ev_phase_count", 3)) * 6 / 1000  # min 6A
    house_kw = 2.5  # Measured night baseload 2.5-3kW
    grid_max_night = cfg.ellevio_tak_kw / cfg.ellevio_night_weight  # Actual kW
    bat_per_hour_night = max(0, ev_kw + house_kw - grid_max_night)
    night_hours = 8
    disk_margin_kwh = 3.0  # Reserve for dishwasher/appliances
    night_reserve_kwh = bat_per_hour_night * night_hours + disk_margin_kwh

    available_kwh = max(0, (input_data.battery_soc - min_soc) / 100 * input_data.battery_cap_kwh)
    max_day_discharge_kwh = max(0, available_kwh - night_reserve_kwh)

    # If no room for daytime discharge, force max_discharge to 0
    if max_day_discharge_kwh <= 0.5:
        max_discharge = 0.0  # Save everything for night

    # Trim to same length
    n = min(
        len(input_data.hourly_prices),
        len(input_data.hourly_pv),
        len(input_data.hourly_loads),
    )
    if n == 0:
        return []

    # Generate plan using existing planner
    hour_plans = generate_plan(
        num_hours=n,
        start_hour=input_data.start_hour,
        target_weighted_kw=target_kw,
        hourly_loads=input_data.hourly_loads[:n],
        hourly_pv=input_data.hourly_pv[:n],
        hourly_prices=input_data.hourly_prices[:n],
        hourly_ev=input_data.hourly_ev[:n],
        battery_soc=input_data.battery_soc,
        ev_soc=max(0, input_data.ev_soc),
        battery_cap_kwh=input_data.battery_cap_kwh,
        battery_min_soc=min_soc,
        battery_efficiency=cfg.battery_efficiency,
        ev_cap_kwh=input_data.ev_cap_kwh,
        night_weight=cfg.ellevio_night_weight,
        grid_charge_price_threshold=cfg.grid_charge_price_threshold,
        grid_charge_max_soc=cfg.grid_charge_max_soc,
        max_discharge_kw=max_discharge,
        max_grid_charge_kw=cfg.max_grid_charge_kw,
    )

    # Convert HourPlan → PlanAction
    return [
        PlanAction(
            hour=hp.hour,
            action=hp.action,
            battery_kw=hp.battery_kw,
            grid_kw=hp.grid_kw,
            price=hp.price,
            battery_soc=hp.battery_soc,
            ev_soc=hp.ev_soc,
        )
        for hp in hour_plans
    ]


def apply_p10_safety(
    pv_forecast_p10_kwh: float,
    pv_forecast_estimate_kwh: float,
    daily_consumption_kwh: float = 15.0,
    p10_threshold_kwh: float = 5.0,
) -> dict:
    """PLAT-1004: p10-golv säkerhetsregel.

    Om p10 < threshold → risk för mycket lite sol.
    Returnerar justerade parametrar för planner.
    """
    if pv_forecast_p10_kwh < p10_threshold_kwh:
        # Confidence låg — spara batterier, nätladda om billigt
        return {
            "strategy": "conservative",
            "max_discharge_kw": P10_DISCHARGE_CONSERVATIVE_KW,
            "grid_charge_recommended": pv_forecast_p10_kwh < daily_consumption_kwh,
            "reason": (
                f"Solcast p10={pv_forecast_p10_kwh:.1f} kWh < {p10_threshold_kwh} kWh "
                f"— risk för lite sol, spara batterier"
            ),
        }
    confidence = min(1.0, pv_forecast_p10_kwh / max(1, pv_forecast_estimate_kwh))
    if confidence < 0.5:
        return {
            "strategy": "moderate",
            "max_discharge_kw": P10_DISCHARGE_MODERATE_KW,
            "grid_charge_recommended": False,
            "reason": f"Confidence {confidence:.0%} — måttlig urladdning",
        }
    return {
        "strategy": "normal",
        "max_discharge_kw": P10_DISCHARGE_NORMAL_KW,
        "grid_charge_recommended": False,
        "reason": f"Confidence {confidence:.0%} — normal drift",
    }


def calculate_ellevio_peak_cost(
    current_peaks_kw: list[float],
    new_peak_kw: float,
    cost_per_kw: float = DEFAULT_PEAK_COST_PER_KW,
    top_n: int = DEFAULT_PEAK_TOP_N,
) -> dict:
    """Calculate cost impact of a new peak on Ellevio bill.

    Ellevio charges: average of top N peaks x cost_per_kw x 12 months.

    Args:
        current_peaks_kw: This month's recorded peaks (weighted kW).
        new_peak_kw: Potential new peak.
        cost_per_kw: SEK per kW per month (default 80).
        top_n: Number of peaks averaged (default 3).

    Returns:
        dict with:
        - current_avg_kw: current average of top N
        - new_avg_kw: average if new_peak included
        - monthly_cost_increase: SEK/month
        - annual_cost_increase: SEK/year
        - should_avoid: bool (True if cost increase > 10 SEK/month)
    """
    # Current top-N average
    sorted_current = sorted(current_peaks_kw, reverse=True)
    top_current = sorted_current[:top_n]
    current_avg = sum(top_current) / top_n if top_current else 0.0

    # New top-N average with candidate peak inserted
    all_peaks = sorted_current + [new_peak_kw]
    sorted_new = sorted(all_peaks, reverse=True)
    top_new = sorted_new[:top_n]
    new_avg = sum(top_new) / top_n

    monthly_increase = (new_avg - current_avg) * cost_per_kw
    annual_increase = monthly_increase * 12

    return {
        "current_avg_kw": round(current_avg, 3),
        "new_avg_kw": round(new_avg, 3),
        "monthly_cost_increase": round(monthly_increase, 2),
        "annual_cost_increase": round(annual_increase, 2),
        "should_avoid": monthly_increase > 10.0,
    }


def estimate_hour_peak(
    current_weighted_kw: float,
    minutes_elapsed: int,
    remaining_load_kw: float,
) -> float:
    """Estimate where the weighted hourly average will land.

    Projects current weighted average to end of hour based on remaining load.
    """
    if minutes_elapsed >= 60:
        return current_weighted_kw
    remaining_minutes = 60 - minutes_elapsed
    projected = (current_weighted_kw * minutes_elapsed + remaining_load_kw * remaining_minutes) / 60
    return projected


def build_price_schedule(
    today_prices: list[float],
    tomorrow_prices: list[float],
    current_hour: int,
    plan_hours: int = 24,
) -> list[float]:
    """Build a price schedule starting from current_hour.

    Combines today's remaining prices + tomorrow's prices.
    If tomorrow not available, repeats today's pattern.

    Args:
        today_prices: 24 hourly prices (öre/kWh) for today
        tomorrow_prices: 24 hourly prices for tomorrow (empty if not available)
        current_hour: Current hour (0-23)
        plan_hours: How many hours to plan

    Returns:
        List of prices for plan_hours, starting from current_hour
    """
    current_hour = max(0, min(23, current_hour))
    remaining_today = today_prices[current_hour:]

    if tomorrow_prices:
        combined = list(remaining_today) + list(tomorrow_prices)
    else:
        # Repeat today's pattern for hours beyond today
        combined = list(remaining_today)
        while len(combined) < plan_hours:
            combined.extend(today_prices)

    return combined[:plan_hours]


def find_cheapest_hours(
    prices: list[float],
    n_hours: int,
    start_offset: int = 0,
) -> list[int]:
    """Find the N cheapest hours in a price list.

    Returns indices (relative to start_offset) of cheapest hours,
    sorted chronologically.
    """
    if not prices or n_hours <= 0:
        return []
    n_hours = min(n_hours, len(prices))
    indexed = sorted(range(len(prices)), key=lambda i: prices[i])
    cheapest = sorted(indexed[:n_hours])
    return [i + start_offset for i in cheapest]


def pressure_pv_adjustment(
    pressure_hpa: float,
    pressure_trend_hpa_3h: float = 0.0,
    normal_pressure_hpa: float = 1013.25,
) -> dict:
    """Adjust PV forecast confidence based on barometric pressure.

    High pressure (>1020) = clear skies → boost confidence
    Low pressure (<1005) = clouds/rain → reduce confidence
    Falling rapidly (<-3 hPa/3h) = weather front → reduce further

    Returns:
        dict with:
        - confidence_factor: float 0.5-1.2 (multiply PV estimate)
        - reason: str
        - pressure_category: str ("high"/"normal"/"low"/"storm")
    """
    if pressure_hpa > 1025:
        factor = 1.1
        category = "high"
        reason = f"High pressure {pressure_hpa:.0f} hPa — clear skies likely"
    elif pressure_hpa > 1015:
        factor = 1.0
        category = "normal"
        reason = f"Normal pressure {pressure_hpa:.0f} hPa"
    elif pressure_hpa > 1005:
        factor = 0.8
        category = "low"
        reason = f"Low pressure {pressure_hpa:.0f} hPa — clouds/rain likely"
    else:
        factor = 0.6
        category = "storm"
        reason = f"Storm pressure {pressure_hpa:.0f} hPa — heavy clouds/rain"

    # Adjust for pressure trend
    if pressure_trend_hpa_3h < -3:
        factor -= 0.1
        reason += f", falling rapidly ({pressure_trend_hpa_3h:+.1f} hPa/3h)"
    elif pressure_trend_hpa_3h > 3:
        factor += 0.05
        reason += f", rising ({pressure_trend_hpa_3h:+.1f} hPa/3h)"

    # Clamp to valid range
    factor = max(0.5, min(1.2, factor))

    return {
        "confidence_factor": round(factor, 2),
        "reason": reason,
        "pressure_category": category,
    }


def should_grid_charge_winter(
    pv_forecast_kwh: float,
    daily_consumption_kwh: float = 15.0,
    current_price_ore: float = 100.0,
    price_threshold_ore: float = 30.0,
    battery_soc: float = 50.0,
    max_charge_soc: float = 80.0,
) -> dict:
    """Determine if winter grid charging is recommended.

    In winter, PV production is low. If solar forecast is below daily
    consumption AND electricity price is cheap, batteries should charge
    from grid to have reserves for evening peak hours.

    Conditions (all must be true for recommendation):
    1. PV forecast < daily consumption (winter/cloudy)
    2. Current price below threshold (cheap electricity)
    3. Battery SoC below max_charge_soc

    Returns dict with:
    - recommend: bool
    - max_charge_soc: float (target SoC)
    - reason: str
    """
    if pv_forecast_kwh >= daily_consumption_kwh:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Solar covers consumption, no grid charge needed",
        }
    if current_price_ore >= price_threshold_ore:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Price too high for grid charge",
        }
    if battery_soc >= max_charge_soc:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Battery already sufficiently charged",
        }
    return {
        "recommend": True,
        "max_charge_soc": max_charge_soc,
        "reason": (
            f"Winter grid charge: PV {pv_forecast_kwh:.1f} kWh < "
            f"consumption {daily_consumption_kwh:.1f} kWh, "
            f"price {current_price_ore:.0f} öre < {price_threshold_ore:.0f} öre"
        ),
    }
