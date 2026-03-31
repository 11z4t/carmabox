"""CARMA Box — Multi-Day Planner (PLAT-963).

Pure Python. No HA imports. Fully testable.

Extends the single-day planner to 3-7 day rolling plans.
Stitches together:
- Known Nordpool prices (today/tomorrow) + predicted prices (day 3+)
- PV forecast (Solcast 2-day) + corrected extrapolation (day 3+)
- Learned consumption profiles with weather adjustment
- Season-aware battery strategy
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .planner import generate_plan

if TYPE_CHECKING:
    from .models import HourPlan
    from .price_patterns import PriceProfile
    from .pv_correction import PVCorrectionProfile


@dataclass
class DayInputs:
    """Input data for one day in the multi-day plan."""

    date_str: str = ""  # ISO date
    weekday: int = 0  # 0=Monday
    month: int = 1
    prices: list[float] = field(default_factory=lambda: [50.0] * 24)
    pv_forecast: list[float] = field(default_factory=lambda: [0.0] * 24)
    consumption: list[float] = field(default_factory=lambda: [2.0] * 24)
    ev_schedule: list[float] = field(default_factory=lambda: [0.0] * 24)
    price_source: str = "predicted"  # "nordpool" or "predicted"
    pv_source: str = "predicted"  # "solcast" or "predicted"
    temp_forecast: list[float] | None = None  # Optional hourly temperature


@dataclass
class MultiDayPlan:
    """Result of a multi-day planning run."""

    days: int = 0
    start_hour: int = 0
    hourly_plan: list[HourPlan] = field(default_factory=list)
    day_summaries: list[dict[str, Any]] = field(default_factory=list)
    total_cost_estimate_kr: float = 0.0
    max_weighted_kw: float = 0.0
    data_quality: str = "mixed"  # "known", "predicted", "mixed"


def build_day_inputs(
    days: int,
    start_hour: int,
    start_weekday: int,
    start_month: int,
    known_prices_today: list[float] | None = None,
    known_prices_tomorrow: list[float] | None = None,
    known_pv_today: list[float] | None = None,
    known_pv_tomorrow: list[float] | None = None,
    consumption_profile_weekday: list[float] | None = None,
    consumption_profile_weekend: list[float] | None = None,
    price_model: PriceProfile | None = None,
    pv_correction: PVCorrectionProfile | None = None,
    pv_daily_estimate: float = 10.0,
    historical_mean_prices: list[float] | None = None,
    known_pv_daily: list[float] | None = None,
) -> list[DayInputs]:
    """Build inputs for each day of the multi-day plan.

    Fills in known data for today/tomorrow and uses learned models
    for subsequent days.

    Args:
        days: Number of days to plan (1-7).
        start_hour: Current hour (0-23).
        start_weekday: Current weekday (0=Monday).
        start_month: Current month (1-12).
        known_prices_today: Nordpool prices for today (24h).
        known_prices_tomorrow: Nordpool prices for tomorrow (24h, if available).
        known_pv_today: Solcast PV forecast for today (24h).
        known_pv_tomorrow: Solcast PV forecast for tomorrow (24h).
        consumption_profile_weekday: Learned weekday profile (24h kW).
        consumption_profile_weekend: Learned weekend profile (24h kW).
        price_model: Learned price patterns for prediction.
        pv_correction: PV forecast correction model.
        pv_daily_estimate: Estimated daily PV production (kWh) for days without forecast.
        historical_mean_prices: AC3 fallback — historical mean prices per hour (24h).
            Used when Nordpool unavailable (>48h) and no trained price model.
        known_pv_daily: Solcast daily totals [today, tomorrow, day3, ...] for extrapolation.

    Returns:
        List of DayInputs, one per day.
    """
    days = max(1, min(7, days))
    default_consumption = [2.0] * 24
    # AC3: Use historical mean prices as fallback when available
    has_prices = historical_mean_prices and len(historical_mean_prices) >= 24
    default_prices = (
        list(historical_mean_prices) if has_prices and historical_mean_prices else [50.0] * 24
    )

    result = []
    for d in range(days):
        weekday = (start_weekday + d) % 7
        is_weekend = weekday >= 5
        month = start_month  # Simplification: same month

        di = DayInputs(weekday=weekday, month=month)

        # Prices
        if d == 0 and known_prices_today and len(known_prices_today) >= 24:
            di.prices = list(known_prices_today)
            di.price_source = "nordpool"
        elif d == 1 and known_prices_tomorrow and len(known_prices_tomorrow) >= 24:
            di.prices = list(known_prices_tomorrow)
            di.price_source = "nordpool"
        elif price_model and price_model.has_sufficient_data:
            di.prices = price_model.predict_24h(month, is_weekend)
            di.price_source = "predicted"
        elif historical_mean_prices and len(historical_mean_prices) >= 24:
            di.prices = list(historical_mean_prices)
            di.price_source = "historical_mean"
        else:
            di.prices = list(default_prices)
            di.price_source = "default"

        # PV forecast
        if d == 0 and known_pv_today and len(known_pv_today) >= 24:
            di.pv_forecast = list(known_pv_today)
            di.pv_source = "solcast"
        elif d == 1 and known_pv_tomorrow and len(known_pv_tomorrow) >= 24:
            di.pv_forecast = list(known_pv_tomorrow)
            di.pv_source = "solcast"
        else:
            # Use Solcast daily estimate for this day if available
            day_kwh = pv_daily_estimate
            if known_pv_daily and d < len(known_pv_daily) and known_pv_daily[d] > 0:
                day_kwh = known_pv_daily[d]
            base_profile = _estimate_pv_profile(day_kwh, month)
            if pv_correction:
                di.pv_forecast = pv_correction.correct_profile(month, base_profile)
            else:
                di.pv_forecast = base_profile
            di.pv_source = "predicted"

        # Consumption
        if is_weekend and consumption_profile_weekend:
            di.consumption = list(consumption_profile_weekend)
        elif not is_weekend and consumption_profile_weekday:
            di.consumption = list(consumption_profile_weekday)
        else:
            di.consumption = list(default_consumption)

        result.append(di)

    return result


def _estimate_pv_profile(daily_kwh: float, month: int) -> list[float]:
    """Estimate a 24h PV profile from daily total.

    Uses a bell curve centered on solar noon, adjusted by month.
    """
    # Solar noon shifts slightly by month (simplified)
    # Daylight hours by month (approximate for Sweden SE3, ~59°N)
    daylight_hours = {
        1: 7,
        2: 9,
        3: 12,
        4: 14,
        5: 17,
        6: 18,
        7: 18,
        8: 16,
        9: 13,
        10: 10,
        11: 8,
        12: 6,
    }
    hours = daylight_hours.get(month, 12)

    # Sunrise/sunset approximation
    sunrise = max(4, 12 - hours // 2)
    sunset = min(22, sunrise + hours)

    profile = [0.0] * 24
    total = 0.0
    for h in range(sunrise, sunset):
        # Parabolic shape peaking at noon
        mid = (sunrise + sunset) / 2
        dist = abs(h - mid)
        half_span = (sunset - sunrise) / 2
        val = max(0, 1 - (dist / half_span) ** 2) if half_span > 0 else 0
        profile[h] = val
        total += val

    # Scale to match daily total
    if total > 0:
        scale = daily_kwh / total
        profile = [round(v * scale, 2) for v in profile]

    return profile


def generate_multiday_plan(
    day_inputs: list[DayInputs],
    start_hour: int,
    battery_soc: float,
    ev_soc: float = -1.0,
    battery_cap_kwh: float = 20.0,
    battery_min_soc: float = 15.0,
    battery_efficiency: float = 0.90,
    ev_cap_kwh: float = 0.0,
    night_weight: float = 0.5,
    grid_charge_price_threshold: float = 15.0,
    grid_charge_max_soc: float = 90.0,
    max_discharge_kw: float = 5.0,
    max_grid_charge_kw: float = 3.0,
) -> MultiDayPlan:
    """Generate a multi-day plan by stitching daily inputs.

    Concatenates hourly arrays and runs the single planner over the full horizon.

    Args:
        day_inputs: Per-day input data.
        start_hour: Current hour (0-23).
        battery_soc: Current battery SoC (%).
        ev_soc: Current EV SoC (%, -1 = no EV).
        battery_cap_kwh: Total battery capacity.
        battery_min_soc: Minimum SoC (%).
        battery_efficiency: Roundtrip efficiency.
        ev_cap_kwh: EV battery capacity.
        night_weight: Ellevio night weight.
        grid_charge_price_threshold: Max price for grid charging (öre).
        grid_charge_max_soc: Max SoC for grid charging (%).
        max_discharge_kw: Max discharge rate.
        max_grid_charge_kw: Max grid charge rate.

    Returns:
        MultiDayPlan with full hourly plan and per-day summaries.
    """
    if not day_inputs:
        return MultiDayPlan()

    # Stitch hourly arrays
    # Day 0: from start_hour to 24, then full days
    all_loads: list[float] = []
    all_pv: list[float] = []
    all_prices: list[float] = []
    all_ev: list[float] = []

    for i, di in enumerate(day_inputs):
        if i == 0:
            # First day: only remaining hours
            all_loads.extend(di.consumption[start_hour:])
            all_pv.extend(di.pv_forecast[start_hour:])
            all_prices.extend(di.prices[start_hour:])
            all_ev.extend(di.ev_schedule[start_hour:])
        else:
            all_loads.extend(di.consumption)
            all_pv.extend(di.pv_forecast)
            all_prices.extend(di.prices)
            all_ev.extend(di.ev_schedule)

    num_hours = len(all_loads)

    # Calculate target
    hourly_weights = [ellevio_weight((start_hour + i) % 24, night_weight) for i in range(num_hours)]
    soc_kwh = battery_soc / 100 * battery_cap_kwh
    min_kwh = battery_min_soc / 100 * battery_cap_kwh
    battery_available = max(0, soc_kwh - min_kwh)

    # PV daily estimates for reserve calculation
    pv_daily = []
    for di in day_inputs:
        pv_daily.append(sum(di.pv_forecast))
    daily_consumption = sum(all_loads[:24]) if len(all_loads) >= 24 else sum(all_loads)

    reserve = calculate_reserve(
        pv_daily,
        daily_consumption,
        daily_battery_need_kwh=5.0,
    )

    target = calculate_target(
        battery_available,
        all_loads,
        hourly_weights,
        reserve,
    )

    # Generate plan over full horizon
    hourly_plan = generate_plan(
        num_hours=num_hours,
        start_hour=start_hour,
        target_weighted_kw=target,
        hourly_loads=all_loads,
        hourly_pv=all_pv,
        hourly_prices=all_prices,
        hourly_ev=all_ev,
        battery_soc=battery_soc,
        ev_soc=ev_soc,
        battery_cap_kwh=battery_cap_kwh,
        battery_min_soc=battery_min_soc,
        battery_efficiency=battery_efficiency,
        ev_cap_kwh=ev_cap_kwh,
        night_weight=night_weight,
        grid_charge_price_threshold=grid_charge_price_threshold,
        grid_charge_max_soc=grid_charge_max_soc,
        max_discharge_kw=max_discharge_kw,
        max_grid_charge_kw=max_grid_charge_kw,
    )

    # Build per-day summaries
    day_summaries = []
    hour_offset = 0
    for i, di in enumerate(day_inputs):
        hours_in_day = (24 - start_hour) if i == 0 else 24
        day_plan = hourly_plan[hour_offset : hour_offset + hours_in_day]
        hour_offset += hours_in_day

        if day_plan:
            day_summaries.append(
                {
                    "day": i,
                    "weekday": di.weekday,
                    "price_source": di.price_source,
                    "pv_source": di.pv_source,
                    "max_weighted_kw": round(max(hp.weighted_kw for hp in day_plan), 1),
                    "total_charge_kwh": round(sum(max(0, hp.battery_kw) for hp in day_plan), 1),
                    "total_discharge_kwh": round(
                        sum(abs(min(0, hp.battery_kw)) for hp in day_plan), 1
                    ),
                    "avg_price": round(sum(hp.price for hp in day_plan) / len(day_plan), 1),
                    "end_soc": day_plan[-1].battery_soc,
                }
            )

    # Data quality assessment
    sources = set()
    for di in day_inputs:
        sources.add(di.price_source)
        sources.add(di.pv_source)
    if sources == {"nordpool", "solcast"}:
        quality = "known"
    elif "nordpool" in sources or "solcast" in sources:
        quality = "mixed"
    else:
        quality = "predicted"

    max_weighted = max((hp.weighted_kw for hp in hourly_plan), default=0)
    total_cost = sum(max(0, hp.grid_kw) * hp.price / 100 for hp in hourly_plan)

    return MultiDayPlan(
        days=len(day_inputs),
        start_hour=start_hour,
        hourly_plan=hourly_plan,
        day_summaries=day_summaries,
        total_cost_estimate_kr=round(total_cost, 1),
        max_weighted_kw=round(max_weighted, 1),
        data_quality=quality,
    )
