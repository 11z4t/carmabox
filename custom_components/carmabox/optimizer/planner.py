"""CARMA Box — Energy Planner.

Pure Python. No HA imports. Fully testable.

Core logic: given prices, PV forecast, consumption profile,
battery state and EV needs — calculate the optimal target
and per-hour plan that minimizes max(weighted_grid_import).

Philosophy:
  - Target is a FLAT line (e.g. 2.0 kW weighted)
  - Battery fills the GAP upward (never more)
  - Never discharge during export
  - Never drain batteries unnecessarily
  - Reserve for next day if solar forecast is low
"""
from __future__ import annotations

from .models import HourPlan


def ellevio_weight(hour: int, night_weight: float = 0.5) -> float:
    """Ellevio hourly weight: night ×0.5, day ×1.0."""
    return night_weight if (hour >= 22 or hour < 6) else 1.0


def calculate_target(
    battery_kwh_available: float,
    hours: int,
    hourly_loads: list[float],
    hourly_weights: list[float],
    pv_forecast_3d: list[float],
) -> float:
    """Calculate optimal flat target that uses battery optimally.

    Binary search for the target_weighted_kw that depletes the
    battery exactly when the next sunny day arrives.
    """
    lo, hi = 0.5, 5.0

    for _ in range(50):
        target = (lo + hi) / 2
        total_batt = 0.0
        for i in range(min(hours, len(hourly_loads))):
            w = hourly_weights[i] if i < len(hourly_weights) else 1.0
            load = hourly_loads[i]
            max_grid = target / w if w > 0 else load
            total_batt += max(0, load - max_grid)

        if total_batt > battery_kwh_available:
            lo = target  # Need higher target (less battery use)
        else:
            hi = target  # Can afford lower target

    return round(target, 2)


def generate_plan(
    num_hours: int,
    start_hour: int,
    target_weighted_kw: float,
    hourly_loads: list[float],
    hourly_pv: list[float],
    hourly_prices: list[float],
    hourly_ev: list[float],
    battery_soc: float,
    ev_soc: float,
    battery_cap_kwh: float = 25.0,
    battery_min_soc: float = 15.0,
    battery_efficiency: float = 0.90,
    ev_cap_kwh: float = 98.0,
    ev_efficiency: float = 0.92,
    night_weight: float = 0.5,
) -> list[HourPlan]:
    """Generate per-hour plan.

    For each hour:
    - If exporting (load < 0): charge battery
    - If weighted load > target: discharge battery
    - If weighted load < target: idle (grid handles it)
    """
    plan = []
    soc_kwh = battery_soc / 100 * battery_cap_kwh
    ev_soc_kwh = ev_soc / 100 * ev_cap_kwh
    min_soc_kwh = battery_min_soc / 100 * battery_cap_kwh

    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        w = ellevio_weight(abs_h, night_weight)
        load = hourly_loads[i] if i < len(hourly_loads) else 1.5
        pv = hourly_pv[i] if i < len(hourly_pv) else 0
        ev = hourly_ev[i] if i < len(hourly_ev) else 0
        price = hourly_prices[i] if i < len(hourly_prices) else 50

        net = load + ev - pv
        battery_kw = 0.0
        action = 'i'

        if net < -0.5:
            # Solar surplus — charge battery
            surplus = abs(net)
            charge = min(surplus, battery_cap_kwh - soc_kwh)
            if charge > 0.3:
                battery_kw = charge
                soc_kwh += charge * battery_efficiency
                action = 'c'

        elif net * w > target_weighted_kw:
            # Load above target — discharge battery
            need = (net * w - target_weighted_kw) / w
            available = soc_kwh - min_soc_kwh
            if available > 0.3:
                discharge = min(need, available, 5.0)  # Max 5kW
                battery_kw = -discharge
                soc_kwh -= discharge / battery_efficiency
                action = 'd'

        # EV SoC tracking
        ev_soc_kwh += ev * ev_efficiency
        ev_soc_pct = min(100, ev_soc_kwh / ev_cap_kwh * 100)

        grid = max(0, net + battery_kw)
        weighted = grid * w

        plan.append(HourPlan(
            hour=abs_h,
            action=action,
            battery_kw=round(battery_kw, 1),
            grid_kw=round(grid, 1),
            weighted_kw=round(weighted, 1),
            pv_kw=round(pv, 1),
            consumption_kw=round(load, 1),
            ev_kw=round(ev, 1),
            ev_soc=int(ev_soc_pct),
            battery_soc=int(soc_kwh / battery_cap_kwh * 100),
            price=round(price, 1),
        ))

    return plan
