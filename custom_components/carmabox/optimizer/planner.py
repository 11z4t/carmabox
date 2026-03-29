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
    # S1: Input validation — clamp num_hours to sane range
    num_hours = max(1, min(num_hours, 168))  # Max 7 days
    battery_cap_kwh = max(0.1, battery_cap_kwh)  # Prevent division by zero

    plan = []
    soc_kwh = battery_soc / 100 * battery_cap_kwh
    ev_soc_kwh = ev_soc / 100 * ev_cap_kwh if ev_soc >= 0 else 0.0
    min_soc_kwh = battery_min_soc / 100 * battery_cap_kwh
    max_charge_kwh = grid_charge_max_soc / 100 * battery_cap_kwh

    # ── Night reserve: don't discharge daytime if batteries needed tonight ──
    ev_kw_min = 230 * 3 * 6 / 1000  # 6A 3-phase = 4.14 kW
    house_kw = 2.5  # Measured night baseload (not 1.7 as assumed)
    grid_max_night = 4.0  # Ellevio 2kW viktat / 0.5 night weight
    bat_per_hour_night = max(0, ev_kw_min + house_kw - grid_max_night)
    night_reserve_kwh = bat_per_hour_night * 8 + 3.0  # 8h + disk margin
    available_kwh = max(0, soc_kwh - min_soc_kwh)
    max_day_discharge_kwh = max(0, available_kwh - night_reserve_kwh)
    # If daytime and no room for discharge → cap max_discharge_kw
    if max_day_discharge_kwh <= 0.5 and start_hour >= 6 and start_hour < 22:
        max_discharge_kw = 0.0  # Save everything for night

    # ── Price-aware arbitrage thresholds ─────────────────────────
    valid_prices = [p for p in hourly_prices[:num_hours] if p > 0]
    median_price = sorted(valid_prices)[len(valid_prices) // 2] if valid_prices else 50.0
    discharge_price_threshold = max(40.0, median_price * 0.9)

    # ── Solar refill: find sunrise (first hour with PV > 1kW) ────
    sunrise_slot = num_hours
    for si in range(num_hours):
        pv_si = hourly_pv[si] if si < len(hourly_pv) else 0.0
        if pv_si > 1.0:
            sunrise_slot = si
            break

    # ── Pre-sunrise drain target ─────────────────────────────────
    # Strong solar expected → drain to min_soc by sunrise
    total_pv_after_sunrise = sum(
        hourly_pv[j] if j < len(hourly_pv) else 0.0
        for j in range(sunrise_slot, num_hours)
    )
    solar_confident = total_pv_after_sunrise > 25.0
    solar_moderate = total_pv_after_sunrise > 15.0
    sunrise_target_pct = (
        battery_min_soc if solar_confident
        else 30.0 if solar_moderate
        else 50.0
    )
    sunrise_target_kwh = sunrise_target_pct / 100 * battery_cap_kwh

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
        available = soc_kwh - min_soc_kwh
        before_sunrise = i < sunrise_slot

        if net < -0.5:
            # P1: Solar surplus — charge battery from PV
            surplus = abs(net)
            charge = min(surplus, battery_cap_kwh - soc_kwh)
            if charge > 0.3:
                battery_kw = charge
                soc_kwh += charge * battery_efficiency
                action = "c"

        elif price <= grid_charge_price_threshold and soc_kwh < max_charge_kwh:
            # P2: Very cheap price — charge from grid
            headroom = max_charge_kwh - soc_kwh
            charge_kw = min(max_grid_charge_kw, headroom)
            if charge_kw > 0.3:
                battery_kw = charge_kw
                soc_kwh += charge_kw * battery_efficiency
                action = "g"

        elif w > 0 and net * w > target_weighted_kw:
            # P3: Ellevio constraint — discharge
            need = (net * w - target_weighted_kw) / w
            if available > 0.3:
                discharge = min(need, available, max_discharge_kw)
                battery_kw = -discharge
                soc_kwh -= discharge
                action = "d"

        elif net > 0.3 and available > 0.3 and price >= discharge_price_threshold:
            # P4: Price arbitrage — discharge to replace grid import
            discharge = min(net * 0.7, available, max_discharge_kw * 0.6)
            if discharge > 0.2:
                battery_kw = -discharge
                soc_kwh -= discharge
                action = "d"

        elif (before_sunrise and available > 0.3 and net > 0.1
              and soc_kwh > sunrise_target_kwh + 0.5
              and (abs_h >= 22 or abs_h < 8)):
            # P5: Pre-sunrise drain — ONLY at night (22-08)
            # Never drain batteries during daytime even if PV is low
            remaining_slots = max(1, sunrise_slot - i)
            remaining_drain = max(0, soc_kwh - sunrise_target_kwh)
            target_drain = remaining_drain / remaining_slots
            discharge = min(max(target_drain, net), available, max_discharge_kw)
            if discharge > 0.2:
                battery_kw = -discharge
                soc_kwh -= discharge
                action = "d"

        # P7: Anti-idle — discharge slowly at night if battery still high
        # ONLY at night (22-08) — daytime batteries may be needed for evening/night
        if (action == "i" and soc_kwh > battery_cap_kwh * 0.8 and net > 0.3
                and (abs_h >= 22 or abs_h < 8)):
            if available > 0.3:
                idle_discharge = min(net * 0.5, available, 1.5)
                if idle_discharge > 0.2:
                    battery_kw = -idle_discharge
                    soc_kwh -= idle_discharge
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
