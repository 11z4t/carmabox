"""CARMA Box — EV Charging Strategy.

Pure Python. No HA imports. Fully testable.

Core principle: SPREAD charging across the night at MINIMUM amps needed.
Never burst at 16A. Use price tiers to decide intensity:
  - Cheap hours: higher amps (up to max)
  - Normal hours: low amps (smyg-ladda)
  - Expensive hours: skip (battery drives house)

Always stay under Ellevio weighted target. Battery supports house
so grid budget goes to EV.
"""

from __future__ import annotations

from ..const import (
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_EV_EFFICIENCY,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_VOLTAGE,
    MAX_EV_CURRENT,
)

# Price tiers (öre/kWh)
PRICE_CHEAP = 30  # Below: charge at max amps
PRICE_NORMAL = 80  # Below: charge at min amps
# Above PRICE_NORMAL: skip (too expensive)


def calculate_ev_schedule(
    start_hour: int,
    num_hours: int,
    ev_soc_pct: float,
    ev_capacity_kwh: float,
    hourly_prices: list[float],
    hourly_loads: list[float],
    target_weighted_kw: float,
    morning_target_soc: float = 75.0,
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    days_since_full_charge: int = 0,
    full_charge_interval_days: int = 7,
    min_amps: int = DEFAULT_EV_MIN_AMPS,
    max_amps: int = MAX_EV_CURRENT,
    voltage: float = DEFAULT_VOLTAGE,
    battery_kwh_available: float = 0.0,
    battery_efficiency: float = DEFAULT_BATTERY_EFFICIENCY,
    pv_tomorrow_kwh: float = 0.0,
    daily_consumption_kwh: float = 15.0,
    night_start: int = DEFAULT_NIGHT_START,
    night_end: int = DEFAULT_NIGHT_END,
) -> list[float]:
    """Calculate per-hour EV charge power (kW).

    Strategy (PLAT-928):
    1. Find night hours (22-06) in planning window
    2. Sort by price — cheapest first
    3. Calculate battery support budget (can battery drive house?)
    4. For each hour, pick amps based on price tier:
       - Cheap (<30 öre): max amps that fit under Ellevio target
       - Normal (30-80 öre): min amps (smyg)
       - Expensive (>80 öre): 0 amps (skip)
    5. Always maximize SoC — don't stop at morning_target if cheap hours remain
    6. If minimum target can't be reached with cheap+normal: use expensive hours too

    Returns:
        List of EV charge power per hour (kW). Same length as num_hours.
    """
    schedule = [0.0] * num_hours

    # CARMA-P0-FIXES Task 1: Robustness — use default SoC if unavailable
    if ev_soc_pct < 0:
        ev_soc_pct = 50.0  # Assume mid-range if sensor unavailable

    if ev_capacity_kwh <= 0:
        return schedule

    # Determine target — force 100% if overdue for full charge
    effective_min_target = morning_target_soc
    if days_since_full_charge >= full_charge_interval_days - 1:
        effective_min_target = 100.0

    # Energy needed for minimum target
    energy_min_kwh = max(0, (effective_min_target - ev_soc_pct) / 100 * ev_capacity_kwh)
    # Energy needed for 100% (we always try to maximize)
    energy_max_kwh = max(0, (100.0 - ev_soc_pct) / 100 * ev_capacity_kwh)

    if energy_min_kwh < 0.5 and energy_max_kwh < 0.5:
        return schedule

    # Available charge rates
    min_kw = min_amps * voltage / 1000
    max_kw = max_amps * voltage / 1000

    # ── Find night hours ──────────────────────────────────
    night_slots: list[dict[str, int | float]] = []
    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        if _is_night_hour(abs_h, night_start, night_end):
            price = hourly_prices[i] if i < len(hourly_prices) else 100.0
            load = hourly_loads[i] if i < len(hourly_loads) else 2.0
            night_slots.append(
                {
                    "idx": i,
                    "hour": abs_h,
                    "price": price,
                    "load": load,
                }
            )

    if not night_slots:
        return schedule

    # ── Battery support budget ────────────────────────────
    # Battery drives the house → grid headroom goes to EV
    pv_surplus = max(0, pv_tomorrow_kwh - daily_consumption_kwh)
    if pv_surplus > 10:
        # Sunny tomorrow — batteries refill, use all for house support tonight
        battery_budget_kwh = battery_kwh_available
    elif pv_surplus > 0:
        battery_budget_kwh = min(battery_kwh_available, pv_surplus)
    else:
        # Cloudy — save battery for tomorrow, minimal support
        battery_budget_kwh = battery_kwh_available * 0.3

    battery_support_per_hour = battery_budget_kwh / len(night_slots) if night_slots else 0.0

    # ── Adjust price thresholds based on tomorrow's PV ─────
    # If sunny tomorrow → batteries refill for free → price doesn't matter
    # Charge EV regardless of cost tonight
    solar_refill = pv_tomorrow_kwh > battery_kwh_available / battery_efficiency
    effective_price_cheap = 999 if solar_refill else PRICE_CHEAP  # Everything is "cheap"
    effective_price_normal = 999 if solar_refill else PRICE_NORMAL

    # ── Sort by price (cheapest first) ────────────────────
    sorted_slots = sorted(night_slots, key=lambda s: s["price"])

    # ── Phase 1: Assign amps based on price tier ──────────
    # Goal: maximize SoC while staying under Ellevio target
    remaining_kwh = energy_max_kwh  # Try to fill as much as possible
    remaining_battery_kwh = battery_budget_kwh
    ev_efficiency = DEFAULT_EV_EFFICIENCY

    for slot in sorted_slots:
        if remaining_kwh <= 0.1:
            break

        price = slot["price"]
        load = slot["load"]
        abs_h = int(slot["hour"])
        w = night_weight if _is_night_hour(abs_h, night_start, night_end) else 1.0

        # Battery supports house load → more grid headroom for EV
        batt_kw = min(battery_support_per_hour, remaining_battery_kwh, load)
        effective_load = max(0, load - batt_kw)

        # Grid headroom: target/weight - effective_house_load
        grid_headroom_kw = (target_weighted_kw / w - effective_load) if w > 0 else max_kw
        grid_headroom_kw = max(0, grid_headroom_kw)

        # Pick amps based on price tier (adjusted for tomorrow's solar)
        if price < effective_price_cheap:
            # Cheap (or solar refill) — charge at max that fits under target
            desired_kw = min(max_kw, grid_headroom_kw)
        elif price < effective_price_normal:
            # Normal — smyg-ladda at min amps
            desired_kw = min(min_kw, grid_headroom_kw) if grid_headroom_kw >= min_kw else 0.0
        else:
            # Expensive — skip unless we MUST charge to reach minimum target
            desired_kw = 0.0

        if desired_kw < min_kw:
            desired_kw = 0.0  # Below min amps = don't charge

        # Snap to whole amps
        if desired_kw > 0:
            amps = int(desired_kw * 1000 / voltage)
            amps = max(min_amps, min(amps, max_amps))
            charge_kw = amps * voltage / 1000
        else:
            charge_kw = 0.0

        # Track battery usage
        if batt_kw > 0:
            remaining_battery_kwh -= batt_kw

        actual_kwh = min(charge_kw, remaining_kwh / ev_efficiency)
        schedule[int(slot["idx"])] = round(actual_kwh, 2)
        remaining_kwh -= actual_kwh * ev_efficiency

    # ── Phase 2: If minimum target not reached, use expensive hours ──
    achieved_kwh = sum(schedule) * ev_efficiency
    if achieved_kwh < energy_min_kwh:
        shortfall = energy_min_kwh - achieved_kwh
        # Find unused expensive night hours
        for slot in sorted_slots:
            if shortfall <= 0.1:
                break
            if schedule[int(slot["idx"])] > 0:
                continue  # Already scheduled

            abs_h = int(slot["hour"])
            w = night_weight if _is_night_hour(abs_h, night_start, night_end) else 1.0
            load = slot["load"]
            grid_headroom_kw = (target_weighted_kw / w - load) if w > 0 else max_kw
            grid_headroom_kw = max(0, grid_headroom_kw)

            # Only charge in expensive hours if there's reasonable headroom
            if grid_headroom_kw < min_kw * 0.5:
                continue  # Too tight — skip this hour
            charge_kw = min(min_kw, grid_headroom_kw) if grid_headroom_kw >= min_kw else min_kw
            amps = max(min_amps, min(int(charge_kw * 1000 / voltage), max_amps))
            charge_kw = amps * voltage / 1000

            actual_kwh = min(charge_kw, shortfall / ev_efficiency)
            schedule[int(slot["idx"])] = round(actual_kwh, 2)
            shortfall -= actual_kwh * ev_efficiency

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
        "tomorrow_kwh": (round(remaining_after_tonight, 1) if not can_reach_tonight else 0),
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


def _is_night_hour(
    hour: int,
    night_start: int = DEFAULT_NIGHT_START,
    night_end: int = DEFAULT_NIGHT_END,
) -> bool:
    """Check if hour is within night window."""
    if night_start > night_end:
        return hour >= night_start or hour < night_end
    return night_start <= hour < night_end
