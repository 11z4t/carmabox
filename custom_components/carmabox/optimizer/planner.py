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
  - Grid charge at very cheap hours if battery needs it
"""

from __future__ import annotations

from ..const import (
    DEFAULT_BATTERY_CAP_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_EV_EFFICIENCY,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_MAX_GRID_CHARGE_KW,
    DEFAULT_NIGHT_WEIGHT,
)
from .grid_logic import ellevio_weight
from .models import HourPlan


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
    battery_cap_kwh: float = DEFAULT_BATTERY_CAP_KWH,
    battery_min_soc: float = DEFAULT_BATTERY_MIN_SOC,
    battery_efficiency: float = DEFAULT_BATTERY_EFFICIENCY,
    ev_cap_kwh: float = 0.0,
    ev_efficiency: float = DEFAULT_EV_EFFICIENCY,
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    grid_charge_price_threshold: float = DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    grid_charge_max_soc: float = DEFAULT_GRID_CHARGE_MAX_SOC,
    max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_KW,
    max_grid_charge_kw: float = DEFAULT_MAX_GRID_CHARGE_KW,
) -> list[HourPlan]:
    """Generate per-hour plan.

    Actions:
    - 'c' = charge from PV (solar surplus)
    - 'd' = discharge battery (load above target)
    - 'g' = charge from grid (cheap price)
    - 'i' = idle (grid handles it)

    For each hour:
    1. If price below threshold and battery not full → grid charge
    2. If exporting (load < 0): charge battery from PV
    3. If weighted load > target: discharge battery
    4. Otherwise: idle
    """
    plan = []
    soc_kwh = battery_soc / 100 * battery_cap_kwh
    ev_soc_kwh = ev_soc / 100 * ev_cap_kwh if ev_soc >= 0 else 0.0
    min_soc_kwh = battery_min_soc / 100 * battery_cap_kwh
    max_charge_kwh = grid_charge_max_soc / 100 * battery_cap_kwh

    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        w = ellevio_weight(abs_h, night_weight)
        load = hourly_loads[i] if i < len(hourly_loads) else 1.5
        pv = hourly_pv[i] if i < len(hourly_pv) else 0
        ev = hourly_ev[i] if i < len(hourly_ev) else 0
        price = hourly_prices[i] if i < len(hourly_prices) else 100

        net = load + ev - pv
        battery_kw = 0.0
        action = "i"

        if net < -0.5:
            # Solar surplus — charge battery from PV
            surplus = abs(net)
            charge = min(surplus, battery_cap_kwh - soc_kwh)
            if charge > 0.3:
                battery_kw = charge
                soc_kwh += charge * battery_efficiency
                action = "c"

        elif price <= grid_charge_price_threshold and soc_kwh < max_charge_kwh:
            # Very cheap price — charge from grid
            headroom = max_charge_kwh - soc_kwh
            charge_kw = min(max_grid_charge_kw, headroom)
            if charge_kw > 0.3:
                battery_kw = charge_kw
                soc_kwh += charge_kw * battery_efficiency
                action = "g"
                # Grid charge adds to net load
                net += charge_kw

        elif net * w > target_weighted_kw:
            # Load above target — discharge battery
            need = (net * w - target_weighted_kw) / w
            available = soc_kwh - min_soc_kwh
            if available > 0.3:
                discharge = min(need, available, max_discharge_kw)
                battery_kw = -discharge
                soc_kwh -= discharge
                action = "d"

        # Clamp SoC to valid range
        soc_kwh = max(0.0, min(soc_kwh, battery_cap_kwh))

        # EV SoC tracking
        if ev_soc >= 0:
            ev_soc_kwh += ev * ev_efficiency
            ev_soc_kwh = max(0.0, min(ev_soc_kwh, ev_cap_kwh))
        ev_soc_pct = max(0, min(100, ev_soc_kwh / ev_cap_kwh * 100)) if ev_cap_kwh > 0 else 0

        grid = max(0, net + battery_kw)
        weighted = grid * w

        batt_soc_pct = max(0, min(100, int(soc_kwh / battery_cap_kwh * 100)))

        plan.append(
            HourPlan(
                hour=abs_h,
                action=action,
                battery_kw=round(battery_kw, 1),
                grid_kw=round(grid, 1),
                weighted_kw=round(weighted, 1),
                pv_kw=round(pv, 1),
                consumption_kw=round(load, 1),
                ev_kw=round(ev, 1),
                ev_soc=int(ev_soc_pct),
                battery_soc=batt_soc_pct,
                price=round(price, 1),
            )
        )

    return plan
