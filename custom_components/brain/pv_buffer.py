"""Brain v0.4 — PV-prognos vs bat-need buffer calculation.

Pure module: no HA imports. Imported by brain.py (for sensor publishing)
and indirectly by cascade.py (via BrainInput.buffer_kwh).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BufferSnapshot:
    bat_need_kwh: float
    pv_remaining_kwh: float
    house_remaining_kwh: float
    pv_surplus_remaining_kwh: float
    buffer_kwh: float
    strategy: str  # BAT_FULL / BUFFER_AVAILABLE / BAT_PRIORITY / NO_SUN / FORECAST_UNAVAILABLE
    hours_to_sunset: float
    forecast_available: bool


def compute_buffer(
    *,
    bat_avg_soc_pct: float,
    bat_capacity_kwh: float,
    pv_remaining_kwh: float | None,
    house_baseline_kw: float,
    hours_to_sunset: float,
    sun_below_horizon: bool,
) -> BufferSnapshot:
    """Compute PV-buffer snapshot for one brain tick.

    Args:
        bat_avg_soc_pct: Current average battery SoC in percent (0-100).
        bat_capacity_kwh: Total nominal battery capacity in kWh.
        pv_remaining_kwh: Forecast remaining PV production today (None = unavailable).
        house_baseline_kw: Estimated house load baseline in kW.
        hours_to_sunset: Hours until sunset (0 = already past sunset).
        sun_below_horizon: True if sun is currently below horizon.

    Returns:
        BufferSnapshot with computed values and strategy.
    """
    soc_pct = max(0.0, min(100.0, bat_avg_soc_pct))
    bat_need_kwh = (100.0 - soc_pct) / 100.0 * bat_capacity_kwh
    house_remaining_kwh = max(0.0, hours_to_sunset) * house_baseline_kw

    if pv_remaining_kwh is None:
        return BufferSnapshot(
            bat_need_kwh=bat_need_kwh,
            pv_remaining_kwh=0.0,
            house_remaining_kwh=house_remaining_kwh,
            pv_surplus_remaining_kwh=0.0,
            buffer_kwh=-bat_need_kwh,
            strategy="FORECAST_UNAVAILABLE",
            hours_to_sunset=hours_to_sunset,
            forecast_available=False,
        )

    pv_surplus_remaining_kwh = max(0.0, pv_remaining_kwh - house_remaining_kwh)
    buffer_kwh = pv_surplus_remaining_kwh - bat_need_kwh

    if bat_need_kwh <= 0.01:
        strategy = "BAT_FULL"
    elif sun_below_horizon or hours_to_sunset <= 0.01:
        strategy = "NO_SUN"
    elif buffer_kwh >= 0.0:
        strategy = "BUFFER_AVAILABLE"
    else:
        strategy = "BAT_PRIORITY"

    return BufferSnapshot(
        bat_need_kwh=bat_need_kwh,
        pv_remaining_kwh=pv_remaining_kwh,
        house_remaining_kwh=house_remaining_kwh,
        pv_surplus_remaining_kwh=pv_surplus_remaining_kwh,
        buffer_kwh=buffer_kwh,
        strategy=strategy,
        hours_to_sunset=hours_to_sunset,
        forecast_available=True,
    )
