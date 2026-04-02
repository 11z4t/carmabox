"""CARMA Box — Evening/Night Multi-Period Optimizer (IT-2381).

Pure Python. No HA imports. Fully testable.

Compares two strategies for the evening→night→tomorrow transition:
  A) Discharge battery during expensive evening hours, recharge from grid
     at cheap night prices.
  B) Save battery in evening (accept grid import at evening prices),
     use stored energy tomorrow to avoid peak import.

Factors:
  - Price spread: evening vs night vs tomorrow peak
  - Solar forecast: sunny tomorrow → battery refills from PV for free
  - EV needs: EV charging at night competes with battery recharging
  - Battery efficiency loss on recharge cycle (~10%)
  - Ellevio weighted peak impact
"""

from __future__ import annotations

import logging

from .models import MultiPeriodStrategy

_LOGGER = logging.getLogger(__name__)

# Evening = hours 17-22, Night = hours 22-06, Tomorrow peak = hours 07-20
EVENING_HOURS = list(range(17, 22))
NIGHT_HOURS = [22, 23, 0, 1, 2, 3, 4, 5]
TOMORROW_PEAK_HOURS = list(range(7, 20))


def _avg_price(prices_24h: list[float], hours: list[int]) -> float:
    """Average price for specified hours."""
    vals = [prices_24h[h] for h in hours if 0 <= h < len(prices_24h)]
    return sum(vals) / len(vals) if vals else 50.0


def _cheapest_n_hours(prices_24h: list[float], hours: list[int], n: int) -> list[int]:
    """Return the N cheapest hours from the given set."""
    candidates = [(h, prices_24h[h]) for h in hours if 0 <= h < len(prices_24h)]
    candidates.sort(key=lambda x: x[1])
    return [h for h, _ in candidates[:n]]


def _peak_price(prices_24h: list[float], hours: list[int]) -> float:
    """Highest price among specified hours."""
    vals = [prices_24h[h] for h in hours if 0 <= h < len(prices_24h)]
    return max(vals) if vals else 100.0


def evaluate_evening_strategy(
    # Battery state
    battery_kwh_available: float,
    battery_cap_kwh: float = 20.0,
    battery_efficiency: float = 0.90,
    battery_min_soc_pct: float = 15.0,
    # Prices
    prices_today_24h: list[float] | None = None,
    prices_tomorrow_24h: list[float] | None = None,
    # Forecasts
    pv_tomorrow_kwh: float = 0.0,
    daily_consumption_kwh: float = 15.0,
    hourly_consumption_evening: list[float] | None = None,
    # EV
    ev_need_kwh: float = 0.0,
    # Grid charge limits
    max_grid_charge_kw: float = 3.0,
) -> MultiPeriodStrategy:
    """Evaluate and choose the optimal evening/night strategy.

    Args:
        battery_kwh_available: Usable battery energy above min SoC (kWh).
        battery_cap_kwh: Total battery capacity.
        battery_efficiency: Round-trip efficiency (0-1).
        battery_min_soc_pct: Minimum SoC percentage.
        prices_today_24h: Today's 24h Nordpool prices (öre/kWh).
        prices_tomorrow_24h: Tomorrow's 24h prices (öre/kWh, may be None).
        pv_tomorrow_kwh: Solar forecast for tomorrow (kWh).
        daily_consumption_kwh: Typical daily consumption (kWh).
        hourly_consumption_evening: Expected consumption per evening hour (kW).
        ev_need_kwh: Energy needed for EV tonight (kWh).
        max_grid_charge_kw: Max grid charge rate (kW).

    Returns:
        MultiPeriodStrategy with chosen strategy and cost breakdown.
    """
    if prices_today_24h is None:
        prices_today_24h = [50.0] * 24
    if prices_tomorrow_24h is None:
        prices_tomorrow_24h = list(prices_today_24h)  # Assume similar if unknown
    if hourly_consumption_evening is None:
        hourly_consumption_evening = [2.0] * len(EVENING_HOURS)

    # Pad prices to 24h
    prices_today_24h = _pad_prices(prices_today_24h)
    prices_tomorrow_24h = _pad_prices(prices_tomorrow_24h)

    # ── Price analysis ──────────────────────────────────────────
    evening_avg = _avg_price(prices_today_24h, EVENING_HOURS)
    night_avg = _avg_price(prices_today_24h, NIGHT_HOURS)
    # Use cheapest night hours for recharge cost estimate
    night_cheapest = _cheapest_n_hours(prices_today_24h, NIGHT_HOURS, 4)
    night_cheap_avg = _avg_price(prices_today_24h, night_cheapest) if night_cheapest else night_avg
    tomorrow_peak_avg = _avg_price(prices_tomorrow_24h, TOMORROW_PEAK_HOURS)
    _tomorrow_peak_max = _peak_price(prices_tomorrow_24h, TOMORROW_PEAK_HOURS)

    # ── Evening consumption estimate ────────────────────────────
    evening_consumption_kwh = sum(hourly_consumption_evening[: len(EVENING_HOURS)])

    # How much battery can cover evening
    battery_for_evening = min(battery_kwh_available, evening_consumption_kwh)

    # ── Night capacity analysis ─────────────────────────────────
    # Total night hours available for charging
    night_hours_count = len(NIGHT_HOURS)
    night_charge_capacity_kwh = max_grid_charge_kw * night_hours_count

    # EV takes priority at night — remaining capacity for battery recharge
    night_for_battery_kwh = max(0, night_charge_capacity_kwh - ev_need_kwh)
    # Account for efficiency loss: need more grid energy to refill battery
    recharge_needed_kwh = battery_for_evening / battery_efficiency

    # Can we fully recharge at night?
    can_full_recharge = night_for_battery_kwh >= recharge_needed_kwh

    # ── Solar tomorrow analysis ─────────────────────────────────
    pv_surplus_tomorrow = max(0, pv_tomorrow_kwh - daily_consumption_kwh)
    # If PV surplus > battery capacity → battery refills for free tomorrow
    solar_refills_battery = pv_surplus_tomorrow > battery_cap_kwh * 0.5

    # ── Strategy A: Discharge evening + recharge night ──────────
    # Savings: avoid grid import during expensive evening
    a_evening_savings = battery_for_evening * evening_avg / 100  # kr

    # Cost: grid recharge at night (cheapest hours)
    actual_recharge = min(recharge_needed_kwh, night_for_battery_kwh)
    a_night_cost = actual_recharge * night_cheap_avg / 100  # kr

    # EV night cost (same in both strategies)
    ev_night_cost = ev_need_kwh * night_cheap_avg / 100  # kr

    a_total = (a_night_cost + ev_night_cost) - a_evening_savings
    # Lower = better (negative means net savings)

    # ── Strategy B: Save battery + use tomorrow ─────────────────
    # Cost: grid import during expensive evening (house needs power)
    b_evening_cost = battery_for_evening * evening_avg / 100  # kr

    # Savings: battery available tomorrow avoids peak-price import
    # But only if solar won't refill the battery anyway
    if solar_refills_battery:
        # PV refills battery for free → saved battery has no tomorrow value
        b_tomorrow_savings = 0.0
    else:
        # Battery value tomorrow = energy x peak price avoided
        b_tomorrow_savings = battery_for_evening * tomorrow_peak_avg / 100  # kr

    b_total = (b_evening_cost + ev_night_cost) - b_tomorrow_savings

    # ── Decision ────────────────────────────────────────────────
    # Strategy A is better when discharge+recharge costs less than saving
    spread_evening_night = evening_avg - night_cheap_avg
    _spread_evening_tomorrow = evening_avg - tomorrow_peak_avg

    reason_parts: list[str] = []

    if solar_refills_battery:
        # Sunny tomorrow → always discharge evening (battery refills free from PV)
        chosen = "A"
        confidence = 0.9
        reason_parts.append(f"Sol imorgon ({pv_tomorrow_kwh:.0f} kWh) fyller batteriet gratis")
        reason_parts.append("→ urladda kväll + PV-återfyllnad")
    elif a_total < b_total:
        chosen = "A"
        savings_diff = b_total - a_total
        confidence = min(0.95, 0.5 + savings_diff * 2)  # Higher diff → higher confidence
        reason_parts.append(
            f"Kväll→natt spread {spread_evening_night:.0f} öre — "
            f"lönsamt att urladda kväll + nätladda natt"
        )
        reason_parts.append(f"Besparing: {savings_diff:.2f} kr vs att spara batteriet")
    elif b_total < a_total:
        chosen = "B"
        savings_diff = a_total - b_total
        confidence = min(0.95, 0.5 + savings_diff * 2)
        reason_parts.append(
            f"Imorgon-toppris {tomorrow_peak_avg:.0f} öre > kväll {evening_avg:.0f} öre — "
            f"bättre spara batteriet"
        )
        reason_parts.append(f"Besparing: {savings_diff:.2f} kr vs att urladda ikväll")
    else:
        # Equal → prefer A (simpler, discharge evening is more certain)
        chosen = "A"
        confidence = 0.5
        reason_parts.append("Strategierna likvärdiga — urladda kväll som default")

    # Adjustments for edge cases
    if not can_full_recharge and chosen == "A":
        # Can't fully recharge → penalize strategy A
        shortfall_pct = (
            1.0 - (night_for_battery_kwh / recharge_needed_kwh) if recharge_needed_kwh > 0 else 0
        )
        if shortfall_pct > 0.3:
            # Significant shortfall — reconsider
            confidence *= 0.7
            reason_parts.append(
                f"OBS: Nattkapacitet räcker bara {(1 - shortfall_pct) * 100:.0f}% "
                f"(EV behöver {ev_need_kwh:.1f} kWh)"
            )

    if battery_kwh_available < 2.0:
        # Very little battery → doesn't matter much
        confidence = max(0.3, confidence * 0.5)
        reason_parts.append(f"Lite batteri ({battery_kwh_available:.1f} kWh) — liten påverkan")

    return MultiPeriodStrategy(
        chosen=chosen,
        confidence=round(confidence, 2),
        a_evening_savings_kr=round(a_evening_savings, 2),
        a_night_recharge_cost_kr=round(a_night_cost, 2),
        a_ev_night_cost_kr=round(ev_night_cost, 2),
        a_total_cost_kr=round(a_total, 2),
        b_evening_import_cost_kr=round(b_evening_cost, 2),
        b_tomorrow_savings_kr=round(b_tomorrow_savings, 2),
        b_ev_night_cost_kr=round(ev_night_cost, 2),
        b_total_cost_kr=round(b_total, 2),
        battery_kwh_available=round(battery_kwh_available, 1),
        evening_avg_price_ore=round(evening_avg, 1),
        night_avg_price_ore=round(night_cheap_avg, 1),
        tomorrow_peak_price_ore=round(tomorrow_peak_avg, 1),
        pv_tomorrow_kwh=round(pv_tomorrow_kwh, 1),
        ev_need_kwh=round(ev_need_kwh, 1),
        reasoning=". ".join(reason_parts),
    )


def apply_strategy_to_battery_schedule(
    strategy: MultiPeriodStrategy,
    battery_schedule: list[tuple[float, str]],
    start_hour: int,
    battery_kwh_available: float,
    max_discharge_kw: float = 5.0,
) -> list[tuple[float, str]]:
    """Modify battery schedule based on chosen multi-period strategy.

    Strategy A: ensure battery discharges during evening hours.
    Strategy B: suppress battery discharge during evening (save for tomorrow).

    Args:
        strategy: The chosen multi-period strategy.
        battery_schedule: Current (battery_kw, action) per slot from _schedule_battery.
        start_hour: Plan start hour.
        battery_kwh_available: Available battery energy (kWh).
        max_discharge_kw: Maximum discharge rate.

    Returns:
        Modified battery schedule.
    """
    if strategy.confidence < 0.3:
        return battery_schedule  # Too uncertain — don't override

    result = list(battery_schedule)
    num_hours = len(result)

    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        batt_kw, action = result[i]

        if abs_h in EVENING_HOURS:
            if strategy.chosen == "A":
                # Strategy A: ensure discharge during evening
                if action == "i" and battery_kwh_available > 1.0:
                    # Idle → force discharge to support house
                    discharge_kw = min(max_discharge_kw, battery_kwh_available)
                    result[i] = (round(-discharge_kw, 2), "d")
                    battery_kwh_available -= discharge_kw

            elif strategy.chosen == "B" and action == "d" and batt_kw < 0:
                # Strategy B: suppress discharge during evening (save battery)
                result[i] = (0.0, "i")
                battery_kwh_available += abs(batt_kw)  # Battery not used

    return result


def _pad_prices(prices: list[float]) -> list[float]:
    """Pad price list to 24 hours."""
    if len(prices) >= 24:
        return prices[:24]
    return prices + [50.0] * (24 - len(prices))
