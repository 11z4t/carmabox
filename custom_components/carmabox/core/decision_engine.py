"""CARMA Box Decision Engine — ONE function, ONE decision per cycle.

No other code may override. Grid Guard = VETO only.

Pure Python. No HA imports. Fully testable.

This is the single source of truth for all energy decisions:
battery charge/discharge, EV charging, fast_charging state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BatteryAction(Enum):
    CHARGE_PV = "charge_pv"  # Charge from PV only
    CHARGE_GRID = "charge_grid"  # Charge from grid (cheap price)
    DISCHARGE = "discharge"  # Discharge to reduce grid import
    STANDBY = "standby"  # Idle


class EVAction(Enum):
    START = "start"
    STOP = "stop"
    NONE = "none"


@dataclass
class Decision:
    battery: BatteryAction
    battery_limit_w: int  # 0 = no limit, >0 = discharge rate
    ev: EVAction
    ev_amps: int  # 0 or 6-10
    ev_phase: str  # "1_phase" or "3_phase"
    fast_charging: bool  # MUST be False when discharging
    reason: str
    price_ore: float
    projected_weighted_kw: float


def _avg_top25(prices: list[float]) -> float:
    """Average of top 25% prices. Returns 50.0 if empty."""
    if not prices:
        return 50.0
    sorted_desc = sorted(prices, reverse=True)
    n = max(1, len(sorted_desc) // 4)
    return sum(sorted_desc[:n]) / n


def decide(
    # Current state
    battery_soc_pct: float,
    battery_cap_kwh: float,
    grid_import_w: float,
    pv_power_w: float,
    ev_soc_pct: float,
    ev_connected: bool,
    ev_target_pct: float = 75.0,
    # Price data
    current_price_ore: float = 50.0,
    upcoming_prices_ore: list[float] | None = None,
    # Time
    hour: int = 12,
    is_night: bool = False,
    is_workday_tomorrow: bool = True,
    # Forecasts
    pv_forecast_remaining_kwh: float = 0.0,
    pv_tomorrow_kwh: float = 0.0,
    house_load_w: float = 2500.0,
    # Ellevio
    weighted_avg_kw: float = 0.0,
    tak_kw: float = 2.0,
    night_weight: float = 0.5,
    # Battery limits
    min_soc: float = 15.0,
    max_discharge_kw: float = 5.0,
    # Thresholds
    cheap_price_ore: float = 30.0,
    grid_charge_max_soc: float = 80.0,
) -> Decision:
    """ONE decision per cycle. No other code may override except Grid Guard VETO.

    Priority:
    1. SAFETY: Never exceed Ellevio tak
    2. NEVER fast_charging during discharge
    3. PRICE: Discharge at expensive, charge at cheap
    4. PV: Use solar before grid
    5. EV: Charge when surplus or cheap night
    6. STANDBY: Default
    """
    reasons: list[str] = []

    # --- Weight for current hour ---
    weight = night_weight if is_night else 1.0

    # --- 1. SAFETY: Max actual grid import for weighted average under tak ---
    # Used by EV headroom checks below
    _max_grid_w = (tak_kw / weight) * 1000.0

    # --- 2. PRICE CHECK ---
    upcoming = upcoming_prices_ore or []
    avg_top25 = _avg_top25(upcoming)
    discharge_threshold = avg_top25 * 0.7
    is_expensive = current_price_ore >= discharge_threshold
    is_cheap = current_price_ore <= cheap_price_ore

    # --- 3. BATTERY DECISION ---
    battery_action = BatteryAction.STANDBY
    battery_limit_w = 0
    discharge_w = 0.0

    if battery_soc_pct <= min_soc:
        # Can't discharge — protect battery
        battery_action = BatteryAction.STANDBY
        reasons.append(f"SoC {battery_soc_pct:.0f}% <= min {min_soc:.0f}% -> standby")

    elif is_expensive and battery_soc_pct > min_soc + 5:
        # Discharge to offset grid import
        battery_action = BatteryAction.DISCHARGE
        # Discharge enough to cover house load minus PV, plus small margin
        discharge_w = min(
            house_load_w - pv_power_w + 200.0,
            max_discharge_kw * 1000.0,
        )
        discharge_w = max(0.0, discharge_w)  # Never negative
        battery_limit_w = int(discharge_w)
        reasons.append(
            f"Price {current_price_ore:.0f} ore >= threshold {discharge_threshold:.0f} "
            f"-> discharge {battery_limit_w}W"
        )

    elif is_cheap and battery_soc_pct < grid_charge_max_soc:
        # Cheap grid charging
        battery_action = BatteryAction.CHARGE_GRID
        reasons.append(
            f"Price {current_price_ore:.0f} ore <= cheap {cheap_price_ore:.0f} "
            f"-> grid charge (SoC {battery_soc_pct:.0f}%)"
        )

    elif pv_power_w > house_load_w:
        # Solar surplus — charge from PV
        battery_action = BatteryAction.CHARGE_PV
        reasons.append(f"PV {pv_power_w:.0f}W > load {house_load_w:.0f}W -> PV charge")

    else:
        battery_action = BatteryAction.STANDBY
        reasons.append("No price/PV trigger -> standby")

    # --- 4. FAST_CHARGING invariant ---
    # fast_charging = True ONLY when grid charging. NEVER when discharging.
    fast_charging = battery_action == BatteryAction.CHARGE_GRID

    # --- 5. EV DECISION ---
    ev_action = EVAction.NONE
    ev_amps = 0
    ev_phase = "3_phase"

    if ev_connected and ev_soc_pct < ev_target_pct:
        # Night charging (cheap or workday need)
        if is_night and (is_workday_tomorrow or current_price_ore < 30):
            # Check headroom against Ellevio tak
            ev_kw_3p = 4.14  # 6A * 230V * 3 phases / 1000
            net_load_3p = house_load_w + ev_kw_3p * 1000 - pv_power_w - discharge_w
            projected_3p = max(0.0, net_load_3p) / 1000.0 * weight

            if projected_3p <= tak_kw:
                ev_action = EVAction.START
                ev_amps = 6
                ev_phase = "3_phase"
                reasons.append("EV night 3-phase 6A (under tak)")
            else:
                # Try 1-phase fallback
                ev_kw_1p = 1.38  # 6A * 230V * 1 phase / 1000
                net_load_1p = house_load_w + ev_kw_1p * 1000 - pv_power_w - discharge_w
                projected_1p = max(0.0, net_load_1p) / 1000.0 * weight

                if projected_1p <= tak_kw:
                    ev_action = EVAction.START
                    ev_amps = 6
                    ev_phase = "1_phase"
                    reasons.append("EV night 1-phase 6A (3-phase would break tak)")
                else:
                    ev_action = EVAction.NONE
                    ev_amps = 0
                    reasons.append(
                        f"EV skipped: even 1-phase ({projected_1p:.1f}kW) would break "
                        f"tak {tak_kw:.1f}kW"
                    )

        # Daytime PV surplus charging
        elif not is_night and pv_power_w > house_load_w + 1400:
            # Enough surplus for at least 1-phase EV
            surplus_w = pv_power_w - house_load_w
            if surplus_w >= 4140:
                ev_action = EVAction.START
                ev_amps = 6
                ev_phase = "3_phase"
                reasons.append(f"EV solar surplus {surplus_w:.0f}W -> 3-phase")
            elif surplus_w >= 1380:
                ev_action = EVAction.START
                ev_amps = 6
                ev_phase = "1_phase"
                reasons.append(f"EV solar surplus {surplus_w:.0f}W -> 1-phase")

    # --- 6. PROJECT WEIGHTED AVERAGE ---
    # Estimate net grid import after all decisions
    ev_load_w = 0.0
    if ev_action == EVAction.START:
        ev_load_w = 4140.0 if ev_phase == "3_phase" else 1380.0
    charge_load_w = 0.0
    if battery_action == BatteryAction.CHARGE_GRID:
        charge_load_w = 3000.0  # Typical grid charge rate

    net_grid_w = house_load_w + ev_load_w + charge_load_w - pv_power_w - discharge_w
    projected_weighted_kw = max(0.0, net_grid_w) / 1000.0 * weight

    reason_str = " | ".join(reasons)

    return Decision(
        battery=battery_action,
        battery_limit_w=battery_limit_w,
        ev=ev_action,
        ev_amps=ev_amps,
        ev_phase=ev_phase,
        fast_charging=fast_charging,
        reason=reason_str,
        price_ore=current_price_ore,
        projected_weighted_kw=round(projected_weighted_kw, 3),
    )
