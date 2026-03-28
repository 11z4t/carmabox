"""Planner — generates energy plans from prices, PV forecast, consumption.

Pure Python. No HA imports. Fully testable.

Wraps optimizer/planner.py generate_plan() and converts output to
PlanAction objects usable by Plan Executor.

Adds:
  - Solar-aware discharge rate (LAG 7)
  - Correct Ellevio target (never below tak × margin)
  - Dynamic min_soc based on temperature
  - PV forecast daily totals for sunrise detection
"""

from __future__ import annotations

from dataclasses import dataclass

from ..optimizer.planner import generate_plan
from .plan_executor import PlanAction


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
    ellevio_night_weight: float = 0.5


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


def generate_carma_plan(
    input_data: PlannerInput,
    config: PlannerConfig | None = None,
) -> list[PlanAction]:
    """Generate plan and return as PlanAction list.

    Applies CARMA Box business logic on top of raw planner:
    - Target never below Ellevio tak × margin
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

    # Target: never below Ellevio tak × margin
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
    ev_kw = 230 * int(getattr(cfg, 'ev_phase_count', 3)) * 6 / 1000  # min 6A
    house_kw = 1.7  # Typical night consumption
    grid_max_night = cfg.ellevio_tak_kw / cfg.ellevio_night_weight  # Actual kW
    bat_per_hour_night = max(0, ev_kw + house_kw - grid_max_night)
    night_hours = 8
    disk_margin_kwh = 3.0  # Reserve for dishwasher/appliances
    night_reserve_kwh = bat_per_hour_night * night_hours + disk_margin_kwh

    available_kwh = max(0, (input_data.battery_soc - min_soc) / 100
                        * input_data.battery_cap_kwh)
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
