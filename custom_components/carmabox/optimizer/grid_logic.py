"""CARMA Box — Grid logic.

Target calculation, Ellevio weighting, reserve management.
Pure Python. No HA imports.
"""

from __future__ import annotations

from ..const import DEFAULT_NIGHT_END, DEFAULT_NIGHT_START


def ellevio_weight(hour: int, night_weight: float = 0.5) -> float:
    """Ellevio hourly weight: night (22-06) x night_weight, day x 1.0."""
    return night_weight if (hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END) else 1.0


def season_mode(pv_forecast_3d: list[float]) -> str:
    """Determine season mode from PV forecast.

    Args:
        pv_forecast_3d: Daily PV forecast (kWh) for next 3+ days.

    Returns:
        'summer' (>15 kWh avg), 'winter' (<5 kWh), or 'transition'.
    """
    if not pv_forecast_3d:
        return "winter"
    avg = sum(pv_forecast_3d) / len(pv_forecast_3d)
    if avg > 15:
        return "summer"
    if avg < 5:
        return "winter"
    return "transition"


def season_reserve_multiplier(mode: str) -> float:
    """Season-based reserve multiplier.

    Summer: batteries refill daily from PV → minimal reserve.
    Winter: no PV → maximum reserve.
    Transition: moderate.
    """
    if mode == "summer":
        return 0.5
    if mode == "winter":
        return 1.5
    return 1.0


def calculate_reserve(
    pv_forecast_daily: list[float],
    daily_consumption_kwh: float,
    daily_battery_need_kwh: float,
) -> float:
    """Calculate battery reserve needed for upcoming cloudy days.

    Looks ahead day-by-day until a sunny day (surplus >10 kWh) is found.
    Accumulates shortfall for each cloudy day.
    Applies season-based multiplier for extra safety in winter.

    Args:
        pv_forecast_daily: Daily PV forecast [today, tomorrow, day3, ...].
        daily_consumption_kwh: Typical daily house consumption.
        daily_battery_need_kwh: Battery energy needed per evening (above target).

    Returns:
        Reserve in kWh that should NOT be discharged.
    """
    mode = season_mode(pv_forecast_daily)
    multiplier = season_reserve_multiplier(mode)

    if not pv_forecast_daily:
        return daily_battery_need_kwh * 2 * multiplier

    reserve = 0.0
    for day_ahead, pv_kwh in enumerate(pv_forecast_daily[1:], 1):
        surplus = max(0, pv_kwh - daily_consumption_kwh)

        if surplus > 10:
            break  # Sunny day found — it will refill batteries

        shortfall = max(0, daily_battery_need_kwh - surplus)
        reserve += shortfall

        if day_ahead >= 7:
            break  # Max 7 day horizon

    return reserve * multiplier


def calculate_target(
    battery_kwh_available: float,
    hourly_loads: list[float],
    hourly_weights: list[float],
    reserve_kwh: float,
) -> float:
    """Calculate optimal flat target (weighted kW).

    Binary search for the target that depletes (available - reserve)
    battery energy exactly over the planning period.

    Args:
        battery_kwh_available: Total battery energy above min SoC.
        hourly_loads: Expected load per hour (kW).
        hourly_weights: Ellevio weight per hour (0.5 night, 1.0 day).
        reserve_kwh: Energy to reserve (not discharge).

    Returns:
        Optimal target in weighted kW.
    """
    usable = max(0, battery_kwh_available - reserve_kwh)
    hours = len(hourly_loads)

    if usable <= 0 or hours == 0:
        # No battery to discharge — target = max weighted load
        if not hourly_loads or not hourly_weights:
            return 5.0
        return max(load * w for load, w in zip(hourly_loads, hourly_weights, strict=False))

    lo, hi = 0.5, 10.0
    for _ in range(50):
        target = (lo + hi) / 2
        total_discharge = 0.0
        for i in range(hours):
            w = hourly_weights[i] if i < len(hourly_weights) else 1.0
            load = hourly_loads[i]
            max_grid = target / w if w > 0 else load
            total_discharge += max(0, load - max_grid)

        if total_discharge > usable:
            lo = target
        else:
            hi = target

    return round(target, 2)
