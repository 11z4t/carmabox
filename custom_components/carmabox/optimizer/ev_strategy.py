"""CARMA Box — EV Charging Strategy.

Pure Python. No HA imports. Fully testable.

Calculates per-hour EV charge schedule based on:
- Current EV SoC and battery capacity
- Electricity prices (cheapest hours first)
- Nightly target (e.g. 75% by 06:00)
- Full charge requirement (100% within N rolling days)
- Grid target constraint (EV charge must not push weighted import over target)
"""

from __future__ import annotations


def calculate_ev_schedule(
    start_hour: int,
    num_hours: int,
    ev_soc_pct: float,
    ev_capacity_kwh: float,
    hourly_prices: list[float],
    hourly_loads: list[float],
    target_weighted_kw: float,
    morning_target_soc: float = 75.0,
    night_weight: float = 0.5,
    days_since_full_charge: int = 0,
    full_charge_interval_days: int = 7,
    min_amps: int = 6,
    max_amps: int = 16,
    voltage: float = 230.0,
    battery_kwh_available: float = 0.0,
    pv_tomorrow_kwh: float = 0.0,
    daily_consumption_kwh: float = 15.0,
) -> list[float]:
    """Calculate per-hour EV charge power (kW).

    Strategy:
    1. Calculate energy needed to reach morning_target_soc by 06:00
    2. If days_since_full >= interval - 1, target 100% instead
    3. Calculate battery support budget (available - reserve for tomorrow)
    4. Sort available night hours by price (cheapest first)
    5. Fill hours with highest amperage that fits under grid target + battery support
    6. If not enough cheap hours, increase amperage on remaining

    Args:
        start_hour: Current hour (0-23).
        num_hours: Planning horizon.
        ev_soc_pct: Current EV SoC (0-100). Negative = no EV.
        ev_capacity_kwh: EV battery capacity.
        hourly_prices: Price per hour (öre/kWh).
        hourly_loads: Expected house load per hour (kW).
        target_weighted_kw: Grid import target (weighted kW).
        morning_target_soc: Target SoC by 06:00 (default 75%).
        night_weight: Ellevio night weight (default 0.5).
        days_since_full_charge: Days since last 100% charge.
        full_charge_interval_days: Max days between 100% charges.
        min_amps: Minimum charge current (default 6A).
        max_amps: Maximum charge current (default 16A).
        voltage: Grid voltage (default 230V).

    Returns:
        List of EV charge power per hour (kW). Same length as num_hours.
    """
    schedule = [0.0] * num_hours

    # No EV or already at target
    if ev_soc_pct < 0 or ev_capacity_kwh <= 0:
        return schedule

    # Determine target — force 100% if overdue
    effective_target = morning_target_soc
    if days_since_full_charge >= full_charge_interval_days - 1:
        effective_target = 100.0

    # Energy needed
    energy_needed_kwh = max(0, (effective_target - ev_soc_pct) / 100 * ev_capacity_kwh)
    if energy_needed_kwh < 0.5:
        return schedule

    # Available charge rates
    min_kw = min_amps * voltage / 1000
    max_kw = max_amps * voltage / 1000

    # Find night hours (22-06) in planning window
    night_slots: list[tuple[int, float, float]] = []  # (index, price, house_load)
    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        if abs_h >= 22 or abs_h < 6:
            price = hourly_prices[i] if i < len(hourly_prices) else 100.0
            load = hourly_loads[i] if i < len(hourly_loads) else 1.5
            night_slots.append((i, price, load))

    if not night_slots:
        return schedule

    # Sort by price (cheapest first)
    night_slots.sort(key=lambda x: x[1])

    # Calculate battery support budget
    # If sun tomorrow → batteries refill → use all available for EV tonight
    # If cloudy tomorrow → reserve battery for house → less EV support
    pv_surplus_tomorrow = max(0, pv_tomorrow_kwh - daily_consumption_kwh)
    if pv_surplus_tomorrow > 10:
        # Sunny tomorrow — batteries will refill, use all for EV
        battery_budget_kwh = battery_kwh_available
    elif pv_surplus_tomorrow > 0:
        # Partly cloudy — use surplus portion
        battery_budget_kwh = min(battery_kwh_available, pv_surplus_tomorrow)
    else:
        # Cloudy/winter — save battery for house, minimal EV support
        battery_budget_kwh = 0.0

    # Distribute battery support evenly across night hours
    num_night_hours = len(night_slots)
    battery_support_per_hour = battery_budget_kwh / num_night_hours if num_night_hours > 0 else 0.0

    # Fill cheapest hours first
    remaining_kwh = energy_needed_kwh
    remaining_battery_kwh = battery_budget_kwh
    for idx, _price, house_load in night_slots:
        if remaining_kwh <= 0:
            break

        abs_h = (start_hour + idx) % 24
        w = night_weight if (abs_h >= 22 or abs_h < 6) else 1.0

        # Max EV power that keeps weighted grid under target
        # Grid headroom: target / weight - house_load
        grid_headroom_kw = target_weighted_kw / w - house_load if w > 0 else max_kw
        grid_headroom_kw = max(0, grid_headroom_kw)

        # Battery can add support on top of grid headroom
        batt_support_kw = min(battery_support_per_hour, remaining_battery_kwh)
        total_headroom_kw = grid_headroom_kw + batt_support_kw

        # Pick amperage based on total headroom (grid + battery)
        if total_headroom_kw >= max_kw:
            charge_kw = max_kw
        elif total_headroom_kw >= min_kw:
            amps = int(total_headroom_kw * 1000 / voltage)
            amps = max(min_amps, min(amps, max_amps))
            charge_kw = amps * voltage / 1000
        else:
            # Even min amps exceeds headroom — charge at min anyway
            # (75% SoC target is safety requirement)
            charge_kw = min_kw

        # Track battery usage
        batt_used = max(0, charge_kw - grid_headroom_kw)
        remaining_battery_kwh -= batt_used

        actual_kwh = min(charge_kw, remaining_kwh)
        schedule[idx] = round(actual_kwh, 2)
        remaining_kwh -= actual_kwh

    return schedule


def calculate_ev_multinight_plan(
    ev_soc_pct: float,
    ev_capacity_kwh: float,
    target_soc: float,
    tonight_max_kwh: float,
    pv_tomorrow_kwh: float,
    daily_consumption_kwh: float,
    battery_cap_kwh: float,
) -> dict[str, object]:
    """Calculate multi-night EV charging plan.

    Returns a human-readable plan showing what happens tonight vs tomorrow.
    """
    energy_needed = max(0, (target_soc - ev_soc_pct) / 100 * ev_capacity_kwh)
    tonight_soc = min(100, ev_soc_pct + (tonight_max_kwh / ev_capacity_kwh * 100))
    remaining_after_tonight = max(0, energy_needed - tonight_max_kwh)

    # Tomorrow night: estimate battery support from PV
    pv_surplus = max(0, pv_tomorrow_kwh - daily_consumption_kwh)
    tomorrow_battery_kwh = min(battery_cap_kwh * 0.85, pv_surplus)
    tomorrow_max_kwh = 2.0 * 8 + tomorrow_battery_kwh
    tomorrow_soc = min(100, tonight_soc + (tomorrow_max_kwh / ev_capacity_kwh * 100))

    can_reach_tonight = tonight_soc >= target_soc

    return {
        "current_soc": round(ev_soc_pct, 0),
        "target_soc": target_soc,
        "tonight_soc": round(tonight_soc, 0),
        "tonight_kwh": round(tonight_max_kwh, 1),
        "tomorrow_soc": round(tomorrow_soc, 0) if not can_reach_tonight else None,
        "tomorrow_kwh": round(remaining_after_tonight, 1) if not can_reach_tonight else 0,
        "pv_tomorrow_kwh": round(pv_tomorrow_kwh, 0),
        "battery_support_kwh": round(tomorrow_battery_kwh, 1),
        "nights_needed": 1 if can_reach_tonight else 2,
        "plan_text": (
            f"EV {ev_soc_pct:.0f}% → {tonight_soc:.0f}% ikväll"
            + (
                f", {tomorrow_soc:.0f}% imorgon (sol {pv_tomorrow_kwh:.0f} kWh)"
                if not can_reach_tonight
                else ""
            )
        ),
    }


def ev_needs_charge(ev_soc_pct: float, morning_target_soc: float = 75.0) -> bool:
    """Check if EV needs charging tonight."""
    return 0 <= ev_soc_pct < morning_target_soc


def ev_needs_full_charge(days_since_full: int, full_charge_interval: int = 7) -> bool:
    """Check if EV is due for a 100% charge."""
    return days_since_full >= full_charge_interval - 1
