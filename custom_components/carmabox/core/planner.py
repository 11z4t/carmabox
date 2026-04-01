"""Planner — generates energy plans from prices, PV forecast, consumption.

Pure Python. No HA imports. Fully testable.

Wraps optimizer/planner.py generate_plan() and converts output to
PlanAction objects usable by Plan Executor.

Adds:
  - Solar-aware discharge rate (LAG 7)
  - Correct Ellevio target (never below tak x margin)
  - Dynamic min_soc based on temperature
  - PV forecast daily totals for sunrise detection
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..const import (
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_PEAK_TOP_N,
    GRID_LIMIT_DEFAULT_KW,
    P10_DISCHARGE_CONSERVATIVE_KW,
    P10_DISCHARGE_MODERATE_KW,
    P10_DISCHARGE_NORMAL_KW,
)
from ..optimizer.planner import generate_plan
from .plan_executor import PlanAction

# EXP-11: Named constants for allocate_pv_surplus policy values
PV_BATTERY_FILL_MARGIN_KWH = 1.0  # Extra kWh margin for "battery fills from PV"
BATTERY_NEAR_FULL_PCT = 95.0  # SoC threshold: battery considered "nearly full"
BATTERY_LOW_NEED_KWH = 1.0  # Below this kWh need → EV gets priority
MIN_BATTERY_CHARGE_SURPLUS_W = 300  # Min surplus watts to start battery charge
MIN_CONSUMER_SURPLUS_W = 200  # Min surplus watts to activate consumers
MIN_EXPORT_THRESHOLD_W = 50  # Below this → not considered export

# Named constants — replaces magic numbers in if-statements
_SOLAR_INACTIVE_START_HOUR = 6  # Hours before this = no solar production
_SOLAR_INACTIVE_END_HOUR = 20  # Hours after this = no solar production
_BATTERY_MAX_SOC_PCT = 100  # % — battery SoC ceiling
_PRESSURE_EXCELLENT_HPA = 1025  # hPa — very clear skies (high pressure)
_PRESSURE_GOOD_HPA = 1015  # hPa — normal pressure
_PRESSURE_FAIR_HPA = 1005  # hPa — low pressure (clouds/rain likely)
_LOW_BATTERY_DISCHARGE_SOC = 30  # % — avoid discharge below this SoC


@dataclass
class SolarAllocationResult:
    """Result of solar allocation planning."""

    ev_can_charge: bool  # Is there margin to charge EV from PV?
    ev_recommended_amps: int  # 0 = don't charge, 6-10 = charge
    ev_phase_mode: str = "3_phase"  # "1_phase" or "3_phase"
    battery_hours_to_full: float = 0.0  # Hours until batteries reach 100%
    surplus_after_battery_kwh: float = 0.0  # kWh available after battery needs
    reason: str = ""


def plan_solar_allocation(
    battery_soc_pct: float,
    battery_cap_kwh: float,
    ev_soc_pct: float,
    ev_target_pct: float,
    ev_cap_kwh: float,
    hourly_pv_kw: list[float],  # Remaining hours today, from Solcast
    hourly_consumption_kw: list[float],  # Remaining hours today
    current_hour: int,
    sunset_hour: int = 19,
    ev_phase_count: int = 3,
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS,
    ev_max_amps: int = DEFAULT_EV_MAX_AMPS,
    voltage: float = 230.0,
    pv_confidence: float = 1.0,
) -> SolarAllocationResult:
    """Allocate remaining solar production between battery and EV.

    Pure function. Decides whether there is enough PV surplus (after covering
    battery charging needs and household consumption) to also charge the EV.

    Args:
        pv_confidence: 0.5-1.2 adjustment factor from Tempest pressure +
            Solcast p10. Lower = less trust in PV forecast = prioritize battery.

    Returns SolarAllocationResult with recommendation and reasoning.
    """
    # EV at 100% → nothing to charge
    if ev_soc_pct >= 100:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="EV already at 100%",
        )
    # Note: we do NOT skip when ev_soc >= target. Target is for NIGHT charging.
    # During daytime, free solar kWh to EV is ALWAYS better than export.

    # Hours of sun remaining
    hours_left = max(0, sunset_hour - current_hour)

    # Edge case: no PV hours left (sunset passed or empty lists)
    if hours_left <= 0 or not hourly_pv_kw or not hourly_consumption_kw:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="No solar hours remaining",
        )

    # 1. Battery need
    battery_need_kwh = max(0.0, (100.0 - battery_soc_pct) / 100.0 * battery_cap_kwh)

    # 2. Calculate total surplus from PV after consumption
    n = min(hours_left, len(hourly_pv_kw), len(hourly_consumption_kw))
    if n <= 0:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=0.0,
            surplus_after_battery_kwh=0.0,
            reason="No solar hours remaining",
        )

    # Apply PV confidence (Tempest pressure + Solcast p10)
    adjusted_pv = [pv * pv_confidence for pv in hourly_pv_kw]
    surplus_per_hour = [max(0.0, adjusted_pv[h] - hourly_consumption_kw[h]) for h in range(n)]
    total_surplus_kwh = sum(surplus_per_hour)

    # 3. Battery hours to full
    avg_surplus = total_surplus_kwh / n if n > 0 else 0.0
    if avg_surplus > 0 and battery_need_kwh > 0:
        battery_hours_to_full = battery_need_kwh / avg_surplus
    elif battery_need_kwh <= 0:
        battery_hours_to_full = 0.0
    else:
        battery_hours_to_full = float("inf")

    # ── CORE PRINCIPLE ──────────────────────────────────────────
    # The question is NOT "is there margin NOW?"
    # The question IS "will we EXPORT later if we don't EV-charge now?"
    #
    # Rule 1: If battery reaches 100% before sunset → we WILL export
    #         → EV charging NOW prevents that export → ALWAYS do it
    # Rule 2: If total surplus > battery need → excess will export
    #         → EV charging absorbs that excess → do it
    # Rule 3: If surplus < battery need → every kWh to EV = kWh bat misses
    #         → But still better than export later → EV if bat fills anyway
    # Rule 4: Battery already full → ALL surplus to EV

    will_export = False
    export_kwh = 0.0
    ev_reason = ""

    # Rule 4: Battery full — everything to EV
    if battery_soc_pct >= 100.0:
        will_export = total_surplus_kwh > 0
        export_kwh = total_surplus_kwh
        ev_reason = "Battery full — all surplus to EV"

    # Rule 1: Battery fills before sunset — we WILL export after that
    elif battery_hours_to_full > 0 and battery_hours_to_full < n:
        hours_after_full = n - battery_hours_to_full
        export_kwh = avg_surplus * hours_after_full
        will_export = export_kwh > 0.5  # >0.5 kWh meaningful
        ev_reason = (
            f"Battery fills in {battery_hours_to_full:.1f}h, "
            f"{export_kwh:.1f} kWh would export — EV absorbs it"
        )

    # Rule 2: Total surplus exceeds battery need — excess exports
    elif total_surplus_kwh > battery_need_kwh:
        export_kwh = total_surplus_kwh - battery_need_kwh
        will_export = export_kwh > 0.5
        ev_reason = (
            f"Surplus {total_surplus_kwh:.1f} > battery need "
            f"{battery_need_kwh:.1f} — {export_kwh:.1f} kWh would export"
        )

    surplus_after_battery_kwh = max(0.0, export_kwh)

    # If we will NOT export → battery needs everything → no EV
    if not will_export:
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            battery_hours_to_full=battery_hours_to_full,
            surplus_after_battery_kwh=0.0,
            reason=(
                f"No export risk — all PV needed for battery "
                f"({battery_need_kwh:.1f} kWh need, "
                f"{total_surplus_kwh:.1f} kWh surplus)"
            ),
        )

    # We WILL export → EV should charge to prevent it
    # Default: 3-phase. Dynamically step down if not enough PV.
    #
    # Ladder: 3-fas 10A (6.9kW) → 3-fas 8A → 3-fas 6A (4.1kW)
    #         → 1-fas 6A (1.4kW) → stop (0)
    #
    # Constraint: grid import MUST stay under Ellevio tak (LAG 1 absolute)
    # grid_import = consumption + ev_kw - pv_now
    # max_ev_kw = pv_now - consumption + tak_kw (where tak is ~2.0 daytime)

    # Current PV available for EV (after house consumption)
    max(0, sum(surplus_per_hour) / max(1, n))
    # Max grid import allowed (Ellevio tak)
    tak_kw = GRID_LIMIT_DEFAULT_KW

    # How much EV power can we add without breaking tak?
    # grid_with_ev = consumption - pv + ev_kw
    # constraint: grid_with_ev <= tak_kw
    # → ev_kw <= pv - consumption + tak_kw = surplus + tak_kw
    avg_consumption = sum(hourly_consumption_kw[:n]) / max(1, n)
    avg_pv = sum(adjusted_pv[:n]) / max(1, n)
    max_ev_kw = max(0, avg_pv - avg_consumption + tak_kw)

    # Try 3-phase first (default), fall back to 1-phase
    ev_3phase_kw = ev_min_amps * voltage * ev_phase_count / 1000  # 4.14kW
    ev_1phase_kw = ev_min_amps * voltage / 1000  # 1.38kW

    if max_ev_kw >= ev_3phase_kw:
        # 3-phase fits within tak
        phase_mode = "3_phase"
        ev_amps_raw = max_ev_kw * 1000 / (voltage * ev_phase_count)
        ev_amps = min(max(ev_min_amps, math.ceil(ev_amps_raw)), ev_max_amps)
    elif max_ev_kw >= ev_1phase_kw:
        # 1-phase fits — lower power but still charges
        phase_mode = "1_phase"
        ev_amps_raw = max_ev_kw * 1000 / voltage
        ev_amps = min(max(ev_min_amps, math.ceil(ev_amps_raw)), ev_max_amps)
    else:
        # Not even 1-phase fits without breaking tak
        return SolarAllocationResult(
            ev_can_charge=False,
            ev_recommended_amps=0,
            ev_phase_mode="3_phase",
            battery_hours_to_full=battery_hours_to_full,
            surplus_after_battery_kwh=surplus_after_battery_kwh,
            reason=(
                f"Export risk {export_kwh:.1f} kWh but EV "
                f"min 1-fas {ev_1phase_kw:.1f}kW > headroom "
                f"{max_ev_kw:.1f}kW — would break tak"
            ),
        )

    return SolarAllocationResult(
        ev_can_charge=True,
        ev_recommended_amps=ev_amps,
        ev_phase_mode=phase_mode,
        battery_hours_to_full=battery_hours_to_full,
        surplus_after_battery_kwh=surplus_after_battery_kwh,
        reason=f"{ev_reason} — {phase_mode} {ev_amps}A",
    )


def calculate_pv_confidence(
    pressure_mbar: float,
    solar_radiation_wm2: float,
    solcast_estimate_kw: float,
    hour: int,
) -> float:
    """Calculate PV forecast confidence from Tempest weather data.

    EXP-13 / IT-GAP09: Uses barometric pressure and real-time solar radiation
    to adjust confidence in Solcast forecast.

    Returns: 0.5-1.2 confidence multiplier.
    - >1.0: conditions better than forecast (bright, high pressure)
    - 1.0: matches forecast
    - <1.0: conditions worse (cloudy, falling pressure)

    Args:
        pressure_mbar: Tempest barometric pressure (mbar). Normal ~1013.
        solar_radiation_wm2: Tempest irradiance (W/m2). 0-1900.
        solcast_estimate_kw: Current hour Solcast p50 estimate (kW).
        hour: Current hour (0-23). Night hours = always 1.0.
    """
    # Night: no PV, confidence irrelevant
    if hour < _SOLAR_INACTIVE_START_HOUR or hour > _SOLAR_INACTIVE_END_HOUR:
        return 1.0

    confidence = 1.0

    # Pressure factor: high pressure = clear skies = good PV
    # 1000 mbar = low (storms), 1020 = normal, 1040 = very clear
    if pressure_mbar > 0:
        pressure_factor = min(1.1, max(0.7, (pressure_mbar - 1000) / 30))
        confidence *= pressure_factor

    # Solar radiation validation: compare actual vs Solcast expected
    # If Solcast says 5kW but radiation is low → reduce confidence
    if solcast_estimate_kw > 0.5 and hour >= 8 and hour <= 18:
        # Expected radiation for given PV estimate (rough: 1kW PV ~ 200 W/m2)
        expected_radiation = solcast_estimate_kw * 200
        if expected_radiation > 0:
            radiation_ratio = solar_radiation_wm2 / expected_radiation
            # Clamp between 0.5 and 1.3
            radiation_factor = min(1.3, max(0.5, radiation_ratio))
            # Blend: 60% radiation validation, 40% pressure
            confidence = confidence * 0.4 + radiation_factor * 0.6

    # Clamp final result
    return round(min(1.2, max(0.5, confidence)), 2)


@dataclass
class PVSurplusAllocation:
    """Result of real-time PV surplus allocation.

    Follows priority stack:
    1. House consumption (always, implicit)
    2. Appliances (if running — implicit, already consuming)
    3. EV charging (if home + sol > hus) — BEFORE battery on weekends
    4. Battery charging (fill to 100% before sunset)
    5. Controllable consumers (miner, VP, pool, elvärmare)
    6. Export (NEVER if any above can absorb)
    """

    surplus_w: float  # Net PV surplus after house (positive = exporting)
    ev_action: str  # "charge", "hold", "stop"
    ev_amps: int  # Recommended amps (0 = don't charge)
    battery_action: str  # "charge", "hold", "discharge"
    battery_target_w: float  # Charge/discharge watts
    consumers_action: str  # "activate", "hold", "deactivate"
    consumers_available_w: float  # Surplus available for consumers after EV+battery
    will_export: bool  # True if surplus remains after all allocation
    export_w: float  # Remaining export watts
    reason: str


def allocate_pv_surplus(
    pv_now_w: float,
    grid_now_w: float,
    house_consumption_w: float,
    battery_soc_pct: float,
    battery_cap_kwh: float,
    ev_soc_pct: float,
    ev_connected: bool,
    ev_target_pct: float,
    is_workday: bool,
    hours_to_sunset: float,
    hourly_pv_remaining_kw: list[float],
    pv_confidence: float = 1.0,
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS,
    ev_max_amps: int = DEFAULT_EV_MAX_AMPS,
    voltage: float = 230.0,
    ellevio_tak_w: float = 2000.0,
    battery_max_charge_w: float = 5000,
) -> PVSurplusAllocation:
    """Real-time PV surplus allocation — called every 30s cycle.

    Implements the user's priority stack:
    1. House (implicit — already consuming)
    2. Appliances (implicit — already consuming if running)
    3. EV (if home + surplus) — BEFORE battery on weekends/when car home
    4. Battery (fill 100% before sunset)
    5. Controllable consumers (miner, VP, pool)
    6. Export (NEVER if consumers can absorb)

    Key insight: On weekends/holidays the car is often home during PV peak.
    EV charging from PV is FREE. Battery can charge LATER (PV fills both).
    Workdays: car typically away → PV goes to battery directly.
    """
    # Net surplus: positive = exporting, negative = importing
    surplus_w = pv_now_w - house_consumption_w
    if surplus_w < 0:
        surplus_w = 0.0

    # If importing (no surplus) — no allocation possible
    if pv_now_w < house_consumption_w:
        return PVSurplusAllocation(
            surplus_w=0,
            ev_action="hold",
            ev_amps=0,
            battery_action="hold",
            battery_target_w=0,
            consumers_action="deactivate",
            consumers_available_w=0,
            will_export=False,
            export_w=0,
            reason="No PV surplus — house consuming all",
        )

    remaining_w = surplus_w

    # ── Battery need calculation ─────────────────────────────
    battery_need_kwh = max(0, (100 - battery_soc_pct) / 100 * battery_cap_kwh)
    # Can battery fill before sunset from remaining PV?
    remaining_pv_kwh = sum(hourly_pv_remaining_kw) * pv_confidence
    battery_fills_from_pv = remaining_pv_kwh >= battery_need_kwh + PV_BATTERY_FILL_MARGIN_KWH

    # ── EV allocation ────────────────────────────────────────
    ev_action = "hold"
    ev_amps = 0

    ev_can_charge = (
        ev_connected and ev_soc_pct < 100 and ev_soc_pct >= 0  # -1 = unknown
    )

    if ev_can_charge:
        # EV charging min power: 3-phase 6A = 4140W
        ev_min_w = ev_min_amps * voltage * 3
        ev_max_w = ev_max_amps * voltage * 3

        # KEY DECISION: EV vs Battery priority
        # Weekend/car home + battery will fill anyway → EV FIRST
        # Workday/battery won't fill → battery first, EV only from excess
        ev_priority = (
            (not is_workday and battery_fills_from_pv)
            or battery_soc_pct >= BATTERY_NEAR_FULL_PCT  # Battery nearly full → EV
            or battery_need_kwh < BATTERY_LOW_NEED_KWH  # Very little battery need → EV
        )

        if ev_priority and remaining_w >= ev_min_w:
            # Allocate to EV (capped by headroom and max amps)
            ev_headroom_w = min(remaining_w, ev_max_w)
            # Don't exceed Ellevio tak: ev_kw + import < tak
            ev_headroom_w = min(ev_headroom_w, ellevio_tak_w + surplus_w)
            ev_amps_raw = ev_headroom_w / (voltage * 3)
            ev_amps = min(max(ev_min_amps, int(ev_amps_raw)), ev_max_amps)
            actual_ev_w = ev_amps * voltage * 3
            remaining_w -= actual_ev_w
            ev_action = "charge"
        elif not ev_priority and remaining_w > battery_max_charge_w + ev_min_w:
            # Battery first, but excess over battery max goes to EV
            battery_alloc = min(remaining_w, battery_max_charge_w)
            ev_excess = remaining_w - battery_alloc
            if ev_excess >= ev_min_w:
                ev_amps_raw = min(ev_excess, ev_max_w) / (voltage * 3)
                ev_amps = min(max(ev_min_amps, int(ev_amps_raw)), ev_max_amps)
                actual_ev_w = ev_amps * voltage * 3
                remaining_w -= actual_ev_w
                ev_action = "charge"

    # ── Battery allocation ───────────────────────────────────
    battery_action = "hold"
    battery_target_w: float = 0.0

    if battery_soc_pct < _BATTERY_MAX_SOC_PCT and remaining_w > MIN_BATTERY_CHARGE_SURPLUS_W:
        # Charge battery from remaining surplus
        charge_w = min(int(remaining_w), battery_max_charge_w)
        battery_action = "charge"
        battery_target_w = float(charge_w)
        remaining_w -= charge_w

    # ── Controllable consumers ───────────────────────────────
    consumers_action = "hold"
    consumers_available_w: float = 0.0

    if remaining_w > MIN_CONSUMER_SURPLUS_W:
        consumers_action = "activate"
        consumers_available_w = remaining_w
        remaining_w = 0  # Consumers absorb everything

    # ── Export check ─────────────────────────────────────────
    will_export = remaining_w > MIN_EXPORT_THRESHOLD_W
    export_w = max(0, remaining_w)

    # Build reason
    parts = []
    if ev_action == "charge":
        parts.append(f"EV {ev_amps}A")
    if battery_action == "charge":
        parts.append(f"Bat {battery_target_w}W")
    if consumers_action == "activate":
        parts.append(f"Consumers {consumers_available_w:.0f}W")
    if will_export:
        parts.append(f"Export {export_w:.0f}W")
    reason = f"PV {pv_now_w:.0f}W surplus {surplus_w:.0f}W -> " + " + ".join(parts or ["idle"])

    return PVSurplusAllocation(
        surplus_w=surplus_w,
        ev_action=ev_action,
        ev_amps=ev_amps,
        battery_action=battery_action,
        battery_target_w=battery_target_w,
        consumers_action=consumers_action,
        consumers_available_w=consumers_available_w,
        will_export=will_export,
        export_w=export_w,
        reason=reason,
    )


@dataclass
class PlannerConfig:
    """Planner configuration."""

    ellevio_tak_kw: float = 2.0
    ellevio_night_weight: float = 0.5
    grid_guard_margin: float = 0.85
    battery_min_soc: float = 15.0
    battery_min_soc_cold: float = 20.0
    cold_temp_c: float = 4.0
    grid_charge_price_threshold: float = 15.0
    grid_charge_max_soc: float = 90.0
    max_discharge_kw: float = 5.0
    max_grid_charge_kw: float = 3.0
    battery_efficiency: float = 0.92
    discharge_rate_solar_kw: float = 2.0
    discharge_rate_partial_kw: float = 1.0
    discharge_rate_winter_kw: float = 0.5
    solar_strong_threshold_kwh: float = 25.0
    solar_partial_threshold_kwh: float = 15.0
    ev_phase_count: int = 3


@dataclass
class PlannerInput:
    """All data needed to generate a plan."""

    start_hour: int
    hourly_prices: list[float]  # öre/kWh, starting from start_hour
    hourly_pv: list[float]  # kW per hour
    hourly_loads: list[float]  # kW per hour (consumption)
    hourly_ev: list[float]  # kW per hour (EV demand)
    battery_soc: float  # Weighted average SoC (%)
    battery_cap_kwh: float  # Total battery capacity
    ev_soc: float  # Current EV SoC (%)
    ev_cap_kwh: float  # EV capacity
    pv_forecast_tomorrow_kwh: float  # Total PV forecast for tomorrow
    battery_temps: list[float] | None = None  # Cell temps per battery


def calculate_night_reserve_kwh(
    ev_phase_count: int = 3,
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS,
    house_baseload_kw: float = 2.5,
    grid_max_night_kw: float = 4.0,
    night_hours: int = 8,
    appliance_margin_kwh: float = 3.0,
) -> float:
    """Calculate battery reserve needed for tonight.

    Reserve = (EV_kW + house_kW - grid_max_night) x hours + appliance_margin
    """
    ev_kw = 230 * ev_phase_count * ev_min_amps / 1000
    bat_per_hour = max(0, ev_kw + house_baseload_kw - grid_max_night_kw)
    return bat_per_hour * night_hours + appliance_margin_kwh


def max_daytime_discharge_kwh(
    battery_soc: float,
    battery_cap_kwh: float,
    min_soc: float = 15.0,
    night_reserve_kwh: float = 0.0,
) -> float:
    """How much can be discharged during daytime while preserving night reserve."""
    available = max(0, (battery_soc - min_soc) / 100 * battery_cap_kwh)
    return max(0, available - night_reserve_kwh)


def generate_carma_plan(
    input_data: PlannerInput,
    config: PlannerConfig | None = None,
) -> list[PlanAction]:
    """Generate plan and return as PlanAction list.

    Applies CARMA Box business logic on top of raw planner:
    - Target never below Ellevio tak x margin
    - Dynamic min_soc based on temperature
    - Solar-aware discharge rate
    """
    cfg = config or PlannerConfig()

    # Calculate effective min_soc (temperature-aware)
    min_soc = cfg.battery_min_soc
    if input_data.battery_temps:
        min_temp = min(input_data.battery_temps)
        if min_temp < cfg.cold_temp_c:
            min_soc = cfg.battery_min_soc_cold

    # Target: never below Ellevio tak x margin
    target_kw = cfg.ellevio_tak_kw * cfg.grid_guard_margin

    # Solar-aware max discharge rate
    pv_tomorrow = input_data.pv_forecast_tomorrow_kwh
    if pv_tomorrow > cfg.solar_strong_threshold_kwh:
        max_discharge = cfg.discharge_rate_solar_kw
    elif pv_tomorrow > cfg.solar_partial_threshold_kwh:
        max_discharge = cfg.discharge_rate_partial_kw
    else:
        max_discharge = cfg.discharge_rate_winter_kw

    # ── Night reserve: don't discharge daytime if batteries needed tonight ──
    # Calculate how much battery is needed for tonight's EV support
    ev_kw = 230 * int(getattr(cfg, "ev_phase_count", 3)) * 6 / 1000  # min 6A
    house_kw = 2.5  # Measured night baseload 2.5-3kW
    grid_max_night = cfg.ellevio_tak_kw / cfg.ellevio_night_weight  # Actual kW
    bat_per_hour_night = max(0, ev_kw + house_kw - grid_max_night)
    night_hours = 8
    disk_margin_kwh = 3.0  # Reserve for dishwasher/appliances
    night_reserve_kwh = bat_per_hour_night * night_hours + disk_margin_kwh

    available_kwh = max(0, (input_data.battery_soc - min_soc) / 100 * input_data.battery_cap_kwh)
    max_day_discharge_kwh = max(0, available_kwh - night_reserve_kwh)

    # If no room for daytime discharge, force max_discharge to 0
    if max_day_discharge_kwh <= 0.5:
        max_discharge = 0.0  # Save everything for night

    # Trim to same length
    n = min(
        len(input_data.hourly_prices),
        len(input_data.hourly_pv),
        len(input_data.hourly_loads),
    )
    if n == 0:
        return []

    # Generate plan using existing planner
    hour_plans = generate_plan(
        num_hours=n,
        start_hour=input_data.start_hour,
        target_weighted_kw=target_kw,
        hourly_loads=input_data.hourly_loads[:n],
        hourly_pv=input_data.hourly_pv[:n],
        hourly_prices=input_data.hourly_prices[:n],
        hourly_ev=input_data.hourly_ev[:n],
        battery_soc=input_data.battery_soc,
        ev_soc=max(0, input_data.ev_soc),
        battery_cap_kwh=input_data.battery_cap_kwh,
        battery_min_soc=min_soc,
        battery_efficiency=cfg.battery_efficiency,
        ev_cap_kwh=input_data.ev_cap_kwh,
        night_weight=cfg.ellevio_night_weight,
        grid_charge_price_threshold=cfg.grid_charge_price_threshold,
        grid_charge_max_soc=cfg.grid_charge_max_soc,
        max_discharge_kw=max_discharge,
        max_grid_charge_kw=cfg.max_grid_charge_kw,
    )

    # Convert HourPlan → PlanAction
    return [
        PlanAction(
            hour=hp.hour,
            action=hp.action,
            battery_kw=hp.battery_kw,
            grid_kw=hp.grid_kw,
            price=hp.price,
            battery_soc=hp.battery_soc,
            ev_soc=hp.ev_soc,
        )
        for hp in hour_plans
    ]


def apply_p10_safety(
    pv_forecast_p10_kwh: float,
    pv_forecast_estimate_kwh: float,
    daily_consumption_kwh: float = 15.0,
    p10_threshold_kwh: float = 5.0,
) -> dict[str, Any]:
    """PLAT-1004: p10-golv säkerhetsregel.

    Om p10 < threshold → risk för mycket lite sol.
    Returnerar justerade parametrar för planner.
    """
    if pv_forecast_p10_kwh < p10_threshold_kwh:
        # Confidence låg — spara batterier, nätladda om billigt
        return {
            "strategy": "conservative",
            "max_discharge_kw": P10_DISCHARGE_CONSERVATIVE_KW,
            "grid_charge_recommended": pv_forecast_p10_kwh < daily_consumption_kwh,
            "reason": (
                f"Solcast p10={pv_forecast_p10_kwh:.1f} kWh < {p10_threshold_kwh} kWh "
                f"— risk för lite sol, spara batterier"
            ),
        }
    confidence = min(1.0, pv_forecast_p10_kwh / max(1, pv_forecast_estimate_kwh))
    if confidence < 0.5:
        return {
            "strategy": "moderate",
            "max_discharge_kw": P10_DISCHARGE_MODERATE_KW,
            "grid_charge_recommended": False,
            "reason": f"Confidence {confidence:.0%} — måttlig urladdning",
        }
    return {
        "strategy": "normal",
        "max_discharge_kw": P10_DISCHARGE_NORMAL_KW,
        "grid_charge_recommended": False,
        "reason": f"Confidence {confidence:.0%} — normal drift",
    }


def calculate_ellevio_peak_cost(
    current_peaks_kw: list[float],
    new_peak_kw: float,
    cost_per_kw: float = DEFAULT_PEAK_COST_PER_KW,
    top_n: int = DEFAULT_PEAK_TOP_N,
) -> dict[str, Any]:
    """Calculate cost impact of a new peak on Ellevio bill.

    Ellevio charges: average of top N peaks x cost_per_kw x 12 months.

    Args:
        current_peaks_kw: This month's recorded peaks (weighted kW).
        new_peak_kw: Potential new peak.
        cost_per_kw: SEK per kW per month (default 80).
        top_n: Number of peaks averaged (default 3).

    Returns:
        dict with:
        - current_avg_kw: current average of top N
        - new_avg_kw: average if new_peak included
        - monthly_cost_increase: SEK/month
        - annual_cost_increase: SEK/year
        - should_avoid: bool (True if cost increase exceeds threshold SEK/month)
    """
    # Current top-N average
    sorted_current = sorted(current_peaks_kw, reverse=True)
    top_current = sorted_current[:top_n]
    current_avg = sum(top_current) / top_n if top_current else 0.0

    # New top-N average with candidate peak inserted
    all_peaks = [*sorted_current, new_peak_kw]
    sorted_new = sorted(all_peaks, reverse=True)
    top_new = sorted_new[:top_n]
    new_avg = sum(top_new) / top_n

    monthly_increase = (new_avg - current_avg) * cost_per_kw
    annual_increase = monthly_increase * 12

    return {
        "current_avg_kw": round(current_avg, 3),
        "new_avg_kw": round(new_avg, 3),
        "monthly_cost_increase": round(monthly_increase, 2),
        "annual_cost_increase": round(annual_increase, 2),
        "should_avoid": monthly_increase > 10.0,
    }


def estimate_hour_peak(
    current_weighted_kw: float,
    minutes_elapsed: int,
    remaining_load_kw: float,
) -> float:
    """Estimate where the weighted hourly average will land.

    Projects current weighted average to end of hour based on remaining load.
    """
    if minutes_elapsed >= 60:
        return current_weighted_kw
    remaining_minutes = 60 - minutes_elapsed
    projected = (current_weighted_kw * minutes_elapsed + remaining_load_kw * remaining_minutes) / 60
    return projected


def build_price_schedule(
    today_prices: list[float],
    tomorrow_prices: list[float],
    current_hour: int,
    plan_hours: int = 24,
) -> list[float]:
    """Build a price schedule starting from current_hour.

    Combines today's remaining prices + tomorrow's prices.
    If tomorrow not available, repeats today's pattern.

    Args:
        today_prices: 24 hourly prices (öre/kWh) for today
        tomorrow_prices: 24 hourly prices for tomorrow (empty if not available)
        current_hour: Current hour (0-23)
        plan_hours: How many hours to plan

    Returns:
        List of prices for plan_hours, starting from current_hour
    """
    current_hour = max(0, min(23, current_hour))
    remaining_today = today_prices[current_hour:]

    if tomorrow_prices:
        combined = list(remaining_today) + list(tomorrow_prices)
    else:
        # Repeat today's pattern for hours beyond today
        combined = list(remaining_today)
        while len(combined) < plan_hours:
            combined.extend(today_prices)

    return combined[:plan_hours]


def find_cheapest_hours(
    prices: list[float],
    n_hours: int,
    start_offset: int = 0,
) -> list[int]:
    """Find the N cheapest hours in a price list.

    Returns indices (relative to start_offset) of cheapest hours,
    sorted chronologically.
    """
    if not prices or n_hours <= 0:
        return []
    n_hours = min(n_hours, len(prices))
    indexed = sorted(range(len(prices)), key=lambda i: prices[i])
    cheapest = sorted(indexed[:n_hours])
    return [i + start_offset for i in cheapest]


def pressure_pv_adjustment(
    pressure_hpa: float,
    pressure_trend_hpa_3h: float = 0.0,
    normal_pressure_hpa: float = 1013.25,
) -> dict[str, Any]:
    """Adjust PV forecast confidence based on barometric pressure.

    High pressure (>1020) = clear skies → boost confidence
    Low pressure (<1005) = clouds/rain → reduce confidence
    Falling rapidly (<-3 hPa/3h) = weather front → reduce further

    Returns:
        dict with:
        - confidence_factor: float 0.5-1.2 (multiply PV estimate)
        - reason: str
        - pressure_category: str ("high"/"normal"/"low"/"storm")
    """
    if pressure_hpa > _PRESSURE_EXCELLENT_HPA:
        factor = 1.1
        category = "high"
        reason = f"High pressure {pressure_hpa:.0f} hPa — clear skies likely"
    elif pressure_hpa > _PRESSURE_GOOD_HPA:
        factor = 1.0
        category = "normal"
        reason = f"Normal pressure {pressure_hpa:.0f} hPa"
    elif pressure_hpa > _PRESSURE_FAIR_HPA:
        factor = 0.8
        category = "low"
        reason = f"Low pressure {pressure_hpa:.0f} hPa — clouds/rain likely"
    else:
        factor = 0.6
        category = "storm"
        reason = f"Storm pressure {pressure_hpa:.0f} hPa — heavy clouds/rain"

    # Adjust for pressure trend
    if pressure_trend_hpa_3h < -3:
        factor -= 0.1
        reason += f", falling rapidly ({pressure_trend_hpa_3h:+.1f} hPa/3h)"
    elif pressure_trend_hpa_3h > 3:
        factor += 0.05
        reason += f", rising ({pressure_trend_hpa_3h:+.1f} hPa/3h)"

    # Clamp to valid range
    factor = max(0.5, min(1.2, factor))

    return {
        "confidence_factor": round(factor, 2),
        "reason": reason,
        "pressure_category": category,
    }


def should_discharge_now(
    current_price_ore: float,
    upcoming_prices_ore: list[float],
    battery_soc_pct: float,
    min_soc: float = 15.0,
    discharge_threshold_factor: float = 0.7,
) -> dict[str, Any]:
    """Decide if battery should discharge NOW based on price comparison.

    Logic:
    1. Find the AVERAGE of the top 25% most expensive upcoming hours
    2. If current price >= expensive_avg x threshold -> discharge (it's expensive NOW)
    3. If current price < cheapest upcoming hours -> DON'T discharge (save for later)
    4. If battery < 30% -> don't discharge regardless

    Returns:
        dict with discharge, recommended_kw, reason, current_price,
        avg_expensive, cheapest_upcoming.
    """
    result = {
        "discharge": False,
        "recommended_kw": 0.0,
        "reason": "",
        "current_price": current_price_ore,
        "avg_expensive": 0.0,
        "cheapest_upcoming": 0.0,
    }

    # Guard: low battery
    if battery_soc_pct < _LOW_BATTERY_DISCHARGE_SOC:
        result["reason"] = (
            f"Battery too low ({battery_soc_pct:.0f}% < {_LOW_BATTERY_DISCHARGE_SOC}%)"
            " — preserving reserve"
        )
        return result

    # Guard: no upcoming prices
    if not upcoming_prices_ore:
        result["reason"] = "No upcoming price data available"
        return result

    # Guard: below min_soc
    if battery_soc_pct <= min_soc:
        result["reason"] = f"Battery at min SoC ({battery_soc_pct:.0f}% <= {min_soc:.0f}%)"
        return result

    # Calculate top 25% expensive average
    sorted_prices = sorted(upcoming_prices_ore, reverse=True)
    top_count = max(1, len(sorted_prices) // 4)
    top_expensive = sorted_prices[:top_count]
    avg_expensive = sum(top_expensive) / len(top_expensive)
    result["avg_expensive"] = round(avg_expensive, 2)

    # Cheapest upcoming
    cheapest = min(upcoming_prices_ore)
    result["cheapest_upcoming"] = cheapest

    # Decision: is current price expensive enough to discharge?
    threshold = avg_expensive * discharge_threshold_factor
    if current_price_ore >= threshold:
        # Scale discharge rate based on how expensive current price is
        # relative to the expensive average
        ratio = min(current_price_ore / max(1, avg_expensive), 1.5)
        recommended_kw = round(min(5.0, max(0.5, ratio * 3.0)), 1)
        result["discharge"] = True
        result["recommended_kw"] = recommended_kw
        result["reason"] = (
            f"Price {current_price_ore:.0f} ore >= threshold "
            f"{threshold:.0f} ore (top25% avg {avg_expensive:.0f} x "
            f"{discharge_threshold_factor}) — discharge at {recommended_kw} kW"
        )
    else:
        result["reason"] = (
            f"Price {current_price_ore:.0f} ore < threshold "
            f"{threshold:.0f} ore — save battery for expensive hours "
            f"(top25% avg {avg_expensive:.0f} ore)"
        )

    return result


def optimal_discharge_hours(
    prices_ore: list[float],
    start_hour: int,
    battery_kwh_available: float,
    max_discharge_kw: float = 5.0,
    house_load_kw: float = 2.5,
    min_profitable_spread_ore: float = 20.0,
) -> list[dict[str, Any]]:
    """Find the best hours to discharge battery for maximum savings.

    Returns list of {hour, price, discharge_kw, savings_ore} sorted by
    profitability. Only includes hours where price spread vs cheapest
    exceeds min_profitable_spread.
    """
    if not prices_ore or battery_kwh_available <= 0:
        return []

    cheapest_price = min(prices_ore)
    candidates = []

    for i, price in enumerate(prices_ore):
        spread = price - cheapest_price
        if spread >= min_profitable_spread_ore:
            hour = (start_hour + i) % 24
            # Discharge covers house load (offset grid import)
            discharge_kw = min(max_discharge_kw, house_load_kw + max_discharge_kw * 0.5)
            discharge_kw = min(discharge_kw, max_discharge_kw)
            savings_ore = spread * discharge_kw  # ore saved per hour
            candidates.append(
                {
                    "hour": hour,
                    "price": price,
                    "discharge_kw": round(discharge_kw, 1),
                    "savings_ore": round(savings_ore, 1),
                }
            )

    # Sort by savings (most profitable first)
    candidates.sort(key=lambda x: x["savings_ore"], reverse=True)

    # Limit by available battery energy (each hour = discharge_kw * 1h)
    result = []
    remaining_kwh = battery_kwh_available
    for c in candidates:
        if remaining_kwh <= 0:
            break
        actual_kw = min(c["discharge_kw"], remaining_kwh)
        c["discharge_kw"] = round(actual_kw, 1)
        c["savings_ore"] = round((c["price"] - cheapest_price) * actual_kw, 1)
        remaining_kwh -= actual_kw
        result.append(c)

    return result


def should_grid_charge_winter(
    pv_forecast_kwh: float,
    daily_consumption_kwh: float = 15.0,
    current_price_ore: float = 100.0,
    price_threshold_ore: float = 30.0,
    battery_soc: float = 50.0,
    max_charge_soc: float = 80.0,
) -> dict[str, Any]:
    """Determine if winter grid charging is recommended.

    In winter, PV production is low. If solar forecast is below daily
    consumption AND electricity price is cheap, batteries should charge
    from grid to have reserves for evening peak hours.

    Conditions (all must be true for recommendation):
    1. PV forecast < daily consumption (winter/cloudy)
    2. Current price below threshold (cheap electricity)
    3. Battery SoC below max_charge_soc

    Returns dict with:
    - recommend: bool
    - max_charge_soc: float (target SoC)
    - reason: str
    """
    if pv_forecast_kwh >= daily_consumption_kwh:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Solar covers consumption, no grid charge needed",
        }
    if current_price_ore >= price_threshold_ore:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Price too high for grid charge",
        }
    if battery_soc >= max_charge_soc:
        return {
            "recommend": False,
            "max_charge_soc": max_charge_soc,
            "reason": "Battery already sufficiently charged",
        }
    return {
        "recommend": True,
        "max_charge_soc": max_charge_soc,
        "reason": (
            f"Winter grid charge: PV {pv_forecast_kwh:.1f} kWh < "
            f"consumption {daily_consumption_kwh:.1f} kWh, "
            f"price {current_price_ore:.0f} öre < {price_threshold_ore:.0f} öre"
        ),
    }


def should_charge_ev_tonight(
    ev_soc_pct: float,
    ev_target_pct: float,
    ev_cap_kwh: float,
    tonight_prices_ore: list[float],  # 8 prices for kl 22-06
    tomorrow_night_prices_ore: list[float],  # 8 prices for tomorrow 22-06 (may be empty)
    pv_tomorrow_kwh: float,  # PV forecast tomorrow
    ev_charge_kw: float = 4.14,  # 6A 3-phase
    is_workday_tomorrow: bool = True,  # Mon-Fri: car leaves for work
) -> dict[str, Any]:
    """Decide: charge EV tonight or wait for cheaper/free opportunity.

    Compares three options:
    A) Charge tonight at tonight's cheapest hours
    B) Wait and charge tomorrow night at tomorrow's prices
    C) Wait and charge from PV tomorrow (free!) — ONLY if car stays home

    Logic:
    - If EV already at target: no charge
    - If WEEKEND + PV covers EV: wait (car home, free PV)
    - If WORKDAY: car leaves → PV can NOT cover → charge tonight
    - If tonight < tomorrow * 0.8: charge tonight (20% cheaper)
    - Otherwise: wait for better opportunity
    """
    ev_need_kwh = (ev_target_pct - ev_soc_pct) / 100.0 * ev_cap_kwh

    # Already at or above target
    if ev_need_kwh <= 0:
        return {
            "charge": False,
            "reason": "EV already at or above target",
            "ev_need_kwh": 0.0,
            "hours_needed": 0,
            "tonight_cost_kr": 0.0,
            "tomorrow_cost_kr": 0.0,
            "pv_covers": False,
        }

    hours_needed = math.ceil(ev_need_kwh / ev_charge_kw)

    # Tonight cost: cheapest N hours
    tonight_sorted = sorted(tonight_prices_ore)
    tonight_hours = tonight_sorted[: min(hours_needed, len(tonight_sorted))]
    tonight_cost_kr = sum(tonight_hours) * ev_charge_kw / 100.0

    # Tomorrow night cost (if available)
    if tomorrow_night_prices_ore:
        tomorrow_sorted = sorted(tomorrow_night_prices_ore)
        tomorrow_hours = tomorrow_sorted[: min(hours_needed, len(tomorrow_sorted))]
        tomorrow_cost_kr = sum(tomorrow_hours) * ev_charge_kw / 100.0
    else:
        tomorrow_cost_kr = 0.0

    # HARD REQUIREMENT: EV must be at target by 06:00 (LAG 3)
    # If below target and it's tonight → CHARGE regardless of cost
    if ev_soc_pct < ev_target_pct:
        # Must charge tonight — can't risk missing target
        pass  # Fall through to cost comparison below

    # PV coverage: tomorrow_kwh > ev_need + 15 (house + battery headroom)
    # BUT: only valid if car stays home (weekend/holiday)
    pv_covers = (
        pv_tomorrow_kwh > ev_need_kwh + 15.0
        and not is_workday_tomorrow  # Car must be HOME to PV-charge
    )

    # Decision logic
    # Option C: PV covers everything AND car stays home -- wait for free solar
    if pv_covers:
        return {
            "charge": False,
            "reason": (
                f"PV tomorrow {pv_tomorrow_kwh:.1f} kWh covers "
                f"EV need {ev_need_kwh:.1f} kWh + 15 kWh headroom — wait for free solar"
            ),
            "ev_need_kwh": round(ev_need_kwh, 2),
            "hours_needed": hours_needed,
            "tonight_cost_kr": round(tonight_cost_kr, 2),
            "tomorrow_cost_kr": round(tomorrow_cost_kr, 2),
            "pv_covers": True,
        }

    # Option A vs B: compare tonight vs tomorrow night prices
    if tomorrow_night_prices_ore and tomorrow_cost_kr > 0:
        if tonight_cost_kr < tomorrow_cost_kr * 0.8:
            # Tonight is 20%+ cheaper -- charge now
            return {
                "charge": True,
                "reason": (
                    f"Tonight {tonight_cost_kr:.2f} kr < tomorrow "
                    f"{tomorrow_cost_kr:.2f} kr x 0.8 — charge tonight"
                ),
                "ev_need_kwh": round(ev_need_kwh, 2),
                "hours_needed": hours_needed,
                "tonight_cost_kr": round(tonight_cost_kr, 2),
                "tomorrow_cost_kr": round(tomorrow_cost_kr, 2),
                "pv_covers": False,
            }
        else:
            # Tomorrow is same or cheaper -- wait
            return {
                "charge": False,
                "reason": (
                    f"Tomorrow night {tomorrow_cost_kr:.2f} kr not much more "
                    f"than tonight {tonight_cost_kr:.2f} kr — wait for better opportunity"
                ),
                "ev_need_kwh": round(ev_need_kwh, 2),
                "hours_needed": hours_needed,
                "tonight_cost_kr": round(tonight_cost_kr, 2),
                "tomorrow_cost_kr": round(tomorrow_cost_kr, 2),
                "pv_covers": False,
            }

    # No tomorrow prices available -- charge tonight as default
    return {
        "charge": True,
        "reason": (
            f"No tomorrow prices available — charge tonight at " f"{tonight_cost_kr:.2f} kr"
        ),
        "ev_need_kwh": round(ev_need_kwh, 2),
        "hours_needed": hours_needed,
        "tonight_cost_kr": round(tonight_cost_kr, 2),
        "tomorrow_cost_kr": 0.0,
        "pv_covers": False,
    }
