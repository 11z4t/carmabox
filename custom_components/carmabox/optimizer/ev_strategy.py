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
) -> list[float]:
    """Calculate per-hour EV charge power (kW).

    Strategy:
    1. Calculate energy needed to reach morning_target_soc by 06:00
    2. If days_since_full >= interval - 1, target 100% instead
    3. Sort available night hours by price (cheapest first)
    4. Fill hours with highest amperage that fits under grid target
    5. If not enough cheap hours, increase amperage on remaining

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

    # Fill cheapest hours first
    remaining_kwh = energy_needed_kwh
    for idx, _price, house_load in night_slots:
        if remaining_kwh <= 0:
            break

        abs_h = (start_hour + idx) % 24
        w = night_weight if (abs_h >= 22 or abs_h < 6) else 1.0

        # Max EV power that keeps weighted grid under target
        # weighted = (house_load + ev_kw) * w <= target_weighted_kw
        # ev_kw <= target_weighted_kw / w - house_load
        headroom_kw = target_weighted_kw / w - house_load if w > 0 else max_kw
        headroom_kw = max(0, headroom_kw)

        # Pick amperage: try max first, fall back to min
        if headroom_kw >= max_kw:
            charge_kw = max_kw
        elif headroom_kw >= min_kw:
            # Quantize to nearest valid amperage
            amps = int(headroom_kw * 1000 / voltage)
            amps = max(min_amps, min(amps, max_amps))
            charge_kw = amps * voltage / 1000
        else:
            # Even min amps exceeds target — charge anyway at min
            # (EV must reach target, safety is more important than peak)
            charge_kw = min_kw

        actual_kwh = min(charge_kw, remaining_kwh)
        schedule[idx] = round(actual_kwh, 2)
        remaining_kwh -= actual_kwh

    return schedule


def ev_needs_charge(ev_soc_pct: float, morning_target_soc: float = 75.0) -> bool:
    """Check if EV needs charging tonight."""
    return 0 <= ev_soc_pct < morning_target_soc


def ev_needs_full_charge(days_since_full: int, full_charge_interval: int = 7) -> bool:
    """Check if EV is due for a 100% charge."""
    return days_since_full >= full_charge_interval - 1
