"""CARMA Box — ROI Calculator (PLAT-963).

Pure Python. No HA imports. Fully testable.

Calculates Return on Investment for the energy system:
- Total investment vs total savings
- Payback period estimation
- Monthly savings trend (12+ months)
- What-if comparison (with vs without CARMA Box)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MonthSavings:
    """One month's savings record."""

    year: int
    month: int
    peak_savings_kr: float = 0.0
    discharge_savings_kr: float = 0.0
    grid_charge_savings_kr: float = 0.0
    total_savings_kr: float = 0.0
    baseline_cost_kr: float = 0.0  # Cost without CARMA
    actual_cost_kr: float = 0.0  # Cost with CARMA


@dataclass
class ROIState:
    """ROI tracking state."""

    # Investment
    battery_cost_kr: float = 0.0
    solar_cost_kr: float = 0.0
    ev_charger_cost_kr: float = 0.0
    installation_cost_kr: float = 0.0
    other_cost_kr: float = 0.0

    # Monthly savings history
    monthly_savings: list[MonthSavings] = field(default_factory=list)

    # System start date
    start_year: int = 0
    start_month: int = 0


def total_investment(state: ROIState) -> float:
    """Total investment cost (kr)."""
    return (
        state.battery_cost_kr
        + state.solar_cost_kr
        + state.ev_charger_cost_kr
        + state.installation_cost_kr
        + state.other_cost_kr
    )


def total_savings(state: ROIState) -> float:
    """Total accumulated savings (kr)."""
    return sum(ms.total_savings_kr for ms in state.monthly_savings)


def total_baseline_cost(state: ROIState) -> float:
    """Total cost without CARMA Box (kr)."""
    return sum(ms.baseline_cost_kr for ms in state.monthly_savings)


def total_actual_cost(state: ROIState) -> float:
    """Total cost with CARMA Box (kr)."""
    return sum(ms.actual_cost_kr for ms in state.monthly_savings)


def record_month(
    state: ROIState,
    year: int,
    month: int,
    peak_savings_kr: float = 0.0,
    discharge_savings_kr: float = 0.0,
    grid_charge_savings_kr: float = 0.0,
    baseline_cost_kr: float = 0.0,
    actual_cost_kr: float = 0.0,
) -> None:
    """Record one month's savings.

    Updates existing month if already recorded, otherwise appends.
    """
    total = peak_savings_kr + discharge_savings_kr + grid_charge_savings_kr

    entry = MonthSavings(
        year=year,
        month=month,
        peak_savings_kr=round(peak_savings_kr, 1),
        discharge_savings_kr=round(discharge_savings_kr, 1),
        grid_charge_savings_kr=round(grid_charge_savings_kr, 1),
        total_savings_kr=round(total, 1),
        baseline_cost_kr=round(baseline_cost_kr, 0),
        actual_cost_kr=round(actual_cost_kr, 0),
    )

    # Update or append
    for i, ms in enumerate(state.monthly_savings):
        if ms.year == year and ms.month == month:
            state.monthly_savings[i] = entry
            return

    state.monthly_savings.append(entry)

    # Keep max 60 months (5 years)
    if len(state.monthly_savings) > 60:
        state.monthly_savings = state.monthly_savings[-60:]

    # Set start date if not set
    if state.start_year == 0:
        state.start_year = year
        state.start_month = month


def payback_months(state: ROIState) -> int | None:
    """Estimate payback period in months.

    Based on average monthly savings so far.
    Returns None if insufficient data or zero savings.
    """
    invest = total_investment(state)
    if invest <= 0:
        return 0  # No investment = instant payback

    savings = total_savings(state)
    months_tracked = len(state.monthly_savings)

    if months_tracked < 1 or savings <= 0:
        return None  # Can't estimate

    avg_monthly = savings / months_tracked
    if avg_monthly <= 0:
        return None

    remaining = max(0, invest - savings)
    months_left = remaining / avg_monthly

    return int(months_tracked + months_left)


def payback_progress_pct(state: ROIState) -> float:
    """How much of the investment has been paid back (0-100+%)."""
    invest = total_investment(state)
    if invest <= 0:
        return 100.0
    savings = total_savings(state)
    return round(savings / invest * 100, 1)


def monthly_trend(state: ROIState, last_n: int = 12) -> list[dict[str, Any]]:
    """Monthly savings trend for charting.

    Returns last N months with running total.
    """
    entries = state.monthly_savings[-last_n:]
    running_total = sum(ms.total_savings_kr for ms in state.monthly_savings if ms not in entries)

    result = []
    for ms in entries:
        running_total += ms.total_savings_kr
        result.append(
            {
                "year": ms.year,
                "month": ms.month,
                "savings_kr": ms.total_savings_kr,
                "peak_kr": ms.peak_savings_kr,
                "discharge_kr": ms.discharge_savings_kr,
                "grid_charge_kr": ms.grid_charge_savings_kr,
                "running_total_kr": round(running_total, 0),
                "baseline_cost_kr": ms.baseline_cost_kr,
                "actual_cost_kr": ms.actual_cost_kr,
            }
        )
    return result


def whatif_summary(state: ROIState) -> dict[str, Any]:
    """What-if comparison: life with vs without CARMA Box.

    Shows total cost difference and projected annual savings.
    """
    baseline = total_baseline_cost(state)
    actual = total_actual_cost(state)
    months = len(state.monthly_savings)

    # Annualized savings
    annualized = 0.0
    if months >= 3:
        recent = state.monthly_savings[-min(12, months) :]
        avg = sum(ms.total_savings_kr for ms in recent) / len(recent)
        annualized = avg * 12

    return {
        "total_without_carma_kr": round(baseline, 0),
        "total_with_carma_kr": round(actual, 0),
        "total_saved_kr": round(baseline - actual, 0),
        "months_tracked": months,
        "annualized_savings_kr": round(annualized, 0),
    }


def roi_summary(state: ROIState) -> dict[str, Any]:
    """Full ROI summary for dashboard."""
    invest = total_investment(state)
    savings = total_savings(state)
    pb = payback_months(state)

    return {
        "total_investment_kr": round(invest, 0),
        "total_savings_kr": round(savings, 0),
        "payback_progress_pct": payback_progress_pct(state),
        "estimated_payback_months": pb,
        "months_tracked": len(state.monthly_savings),
        "avg_monthly_savings_kr": (
            round(savings / len(state.monthly_savings), 0) if state.monthly_savings else 0
        ),
        "whatif": whatif_summary(state),
    }


def state_to_dict(state: ROIState) -> dict[str, Any]:
    """Serialize for persistent storage."""
    return {
        "battery_cost_kr": state.battery_cost_kr,
        "solar_cost_kr": state.solar_cost_kr,
        "ev_charger_cost_kr": state.ev_charger_cost_kr,
        "installation_cost_kr": state.installation_cost_kr,
        "other_cost_kr": state.other_cost_kr,
        "start_year": state.start_year,
        "start_month": state.start_month,
        "monthly_savings": [
            {
                "year": ms.year,
                "month": ms.month,
                "peak_savings_kr": ms.peak_savings_kr,
                "discharge_savings_kr": ms.discharge_savings_kr,
                "grid_charge_savings_kr": ms.grid_charge_savings_kr,
                "total_savings_kr": ms.total_savings_kr,
                "baseline_cost_kr": ms.baseline_cost_kr,
                "actual_cost_kr": ms.actual_cost_kr,
            }
            for ms in state.monthly_savings[-60:]
        ],
    }


def state_from_dict(data: dict[str, Any]) -> ROIState:
    """Deserialize from storage."""
    if not data or not isinstance(data, dict):
        return ROIState()
    try:
        monthly = [
            MonthSavings(
                year=int(d["year"]),
                month=int(d["month"]),
                peak_savings_kr=float(d.get("peak_savings_kr", 0)),
                discharge_savings_kr=float(d.get("discharge_savings_kr", 0)),
                grid_charge_savings_kr=float(d.get("grid_charge_savings_kr", 0)),
                total_savings_kr=float(d.get("total_savings_kr", 0)),
                baseline_cost_kr=float(d.get("baseline_cost_kr", 0)),
                actual_cost_kr=float(d.get("actual_cost_kr", 0)),
            )
            for d in data.get("monthly_savings", [])
            if isinstance(d, dict)
        ]
        return ROIState(
            battery_cost_kr=float(data.get("battery_cost_kr", 0)),
            solar_cost_kr=float(data.get("solar_cost_kr", 0)),
            ev_charger_cost_kr=float(data.get("ev_charger_cost_kr", 0)),
            installation_cost_kr=float(data.get("installation_cost_kr", 0)),
            other_cost_kr=float(data.get("other_cost_kr", 0)),
            start_year=int(data.get("start_year", 0)),
            start_month=int(data.get("start_month", 0)),
            monthly_savings=monthly,
        )
    except (KeyError, ValueError, TypeError):
        return ROIState()
