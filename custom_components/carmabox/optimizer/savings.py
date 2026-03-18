"""CARMA Box — Savings Calculator.

Pure Python. No HA imports. Fully testable.

Calculates estimated monthly savings from:
1. Peak reduction (Ellevio top-3 weighted peaks × kr/kW)
2. Price optimization (discharge at expensive hours, charge at cheap)

All values are accumulated over the current month and reset on month change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SavingsState:
    """Running savings state for the current month."""

    month: int = 0
    year: int = 0

    # Peak tracking — top-N weighted hourly peaks this month
    peak_samples: list[float] = field(default_factory=list)

    # Baseline peaks — what grid import would have been without battery
    baseline_peak_samples: list[float] = field(default_factory=list)

    # Accumulated price optimization (kr)
    discharge_savings_kr: float = 0.0
    grid_charge_savings_kr: float = 0.0

    # Counters
    total_discharge_kwh: float = 0.0
    total_grid_charge_kwh: float = 0.0


def reset_if_new_month(state: SavingsState, now: datetime) -> SavingsState:
    """Reset savings if a new month has started."""
    if state.month != now.month or state.year != now.year:
        return SavingsState(month=now.month, year=now.year)
    return state


def record_peak(
    state: SavingsState,
    weighted_kw: float,
    baseline_kw: float,
    top_n: int = 3,
) -> None:
    """Record a weighted peak sample.

    Args:
        state: Current savings state.
        weighted_kw: Actual weighted grid import (with CARMA Box).
        baseline_kw: What grid import would have been without battery.
        top_n: Number of top peaks to track (Ellevio = 3).
    """
    state.peak_samples.append(weighted_kw)
    state.peak_samples.sort(reverse=True)
    state.peak_samples = state.peak_samples[:top_n]

    state.baseline_peak_samples.append(baseline_kw)
    state.baseline_peak_samples.sort(reverse=True)
    state.baseline_peak_samples = state.baseline_peak_samples[:top_n]


def record_discharge(
    state: SavingsState,
    discharge_kwh: float,
    price_ore: float,
    avg_price_ore: float,
) -> None:
    """Record a discharge event (battery → house instead of grid).

    Savings = discharged energy × (current_price - average_price) / 100.
    The idea: we discharge at expensive hours and saved vs average cost.

    Args:
        state: Current savings state.
        discharge_kwh: Energy discharged this period (kWh).
        price_ore: Current electricity price (öre/kWh).
        avg_price_ore: Average daily price (öre/kWh).
    """
    if discharge_kwh > 0 and price_ore > avg_price_ore:
        savings = discharge_kwh * (price_ore - avg_price_ore) / 100  # öre → kr
        state.discharge_savings_kr += savings
        state.total_discharge_kwh += discharge_kwh


def record_grid_charge(
    state: SavingsState,
    charge_kwh: float,
    price_ore: float,
    avg_price_ore: float,
) -> None:
    """Record a grid charge event (cheap grid → battery for later).

    Savings = charged energy × (average_price - charge_price) / 100.

    Args:
        state: Current savings state.
        charge_kwh: Energy charged from grid (kWh).
        price_ore: Price during charging (öre/kWh).
        avg_price_ore: Average daily price (öre/kWh).
    """
    if charge_kwh > 0 and avg_price_ore > price_ore:
        savings = charge_kwh * (avg_price_ore - price_ore) / 100  # öre → kr
        state.grid_charge_savings_kr += savings
        state.total_grid_charge_kwh += charge_kwh


def calculate_peak_savings(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> float:
    """Calculate peak reduction savings (kr/month).

    Ellevio charges: mean(top-N peaks) × cost_per_kw per month.
    Savings = (baseline_mean - actual_mean) × cost_per_kw.

    Args:
        state: Current savings state.
        cost_per_kw: Grid operator peak cost (kr/kW/month).
        top_n: Number of top peaks (default 3 for Ellevio).

    Returns:
        Estimated savings in kr for this month.
    """
    if not state.peak_samples or not state.baseline_peak_samples:
        return 0.0

    actual_peaks = state.peak_samples[:top_n]
    baseline_peaks = state.baseline_peak_samples[:top_n]

    actual_mean = sum(actual_peaks) / len(actual_peaks)
    baseline_mean = sum(baseline_peaks) / len(baseline_peaks)

    reduction = max(0, baseline_mean - actual_mean)
    return round(reduction * cost_per_kw, 1)


def total_savings(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> float:
    """Total estimated savings this month (kr).

    Sum of peak reduction + discharge optimization + grid charge optimization.
    """
    peak = calculate_peak_savings(state, cost_per_kw, top_n)
    return round(peak + state.discharge_savings_kr + state.grid_charge_savings_kr, 1)


def savings_breakdown(
    state: SavingsState,
    cost_per_kw: float = 80.0,
    top_n: int = 3,
) -> dict[str, float]:
    """Detailed savings breakdown."""
    peak = calculate_peak_savings(state, cost_per_kw, top_n)
    return {
        "peak_reduction_kr": peak,
        "discharge_savings_kr": round(state.discharge_savings_kr, 1),
        "grid_charge_savings_kr": round(state.grid_charge_savings_kr, 1),
        "total_kr": round(peak + state.discharge_savings_kr + state.grid_charge_savings_kr, 1),
        "total_discharge_kwh": round(state.total_discharge_kwh, 1),
        "total_grid_charge_kwh": round(state.total_grid_charge_kwh, 1),
    }
