"""CARMA Box — Dynamic Discharge Threshold.

Pure Python. No HA imports. Fully testable.

Root cause (Jerström, Stene Båtsmanstorp, 2026-07-19): the site ran a
STATIC discharge price floor (`input_number.brain_price_discharge_floor_ore`,
originally 50 öre/kWh) — discharge at full power as soon as the price
reached the threshold. Analysis of the real SoC history against Nordpool
prices showed the battery emptied completely at 19:50 (threshold reached
18:30, fully drained in 80 minutes) — almost 2 hours BEFORE the day's
actual price peak (130 öre at 21:45). A provisional bump to 90 öre/kWh
was applied as a stop-gap, but remains a FIXED threshold: too low on an
expensive day, too high on a cheap day, because it says nothing about
the SHAPE of that day's price curve.

This module replaces the fixed öre/kWh value with a threshold computed
fresh every cycle from the day's ACTUAL remaining Nordpool prices: the
floor is set at a configurable percentile of the remaining hours, so the
battery only discharges during the top X% most expensive hours still to
come today. On a flat day the floor tracks the flat price. On a day with
one late spike, the floor stays high until that spike actually arrives.

Generic by design — usable for any site with a Nordpool price array and
a battery discharge decision (Jerström, Sandgränd, Drottning, ...), not
hardcoded to one customer.

Safety: `resolve_discharge_decision` NEVER authorizes a discharge that
would take the battery below `min_soc_floor_pct`, regardless of price —
mirroring the min_soc / effective_min_soc pattern already enforced in
`core/battery_balancer.py`. Price is only ever a permission to discharge
when energy is available above the floor; SoC floor always wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    DYNAMIC_DISCHARGE_FALLBACK_FLOOR_ORE,
    DYNAMIC_DISCHARGE_MIN_PRICE_POINTS,
    DYNAMIC_DISCHARGE_PERCENTILE,
    DYNAMIC_DISCHARGE_SOC_FLOOR_PCT,
)


@dataclass(frozen=True)
class DynamicDischargeThreshold:
    """Computed discharge price floor for the remaining hours of today."""

    floor_ore: float
    percentile_used: float
    sample_count: int
    remaining_min_ore: float
    remaining_max_ore: float
    fallback_used: bool
    reason: str


@dataclass(frozen=True)
class DischargeDecision:
    """Whether discharge is permitted this hour, and why."""

    should_discharge: bool
    reason: str


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list.

    Args:
        sorted_values: Values sorted ascending (caller's responsibility).
        p: Percentile as a 0.0-1.0 fraction (e.g. 0.85 = 85th percentile).

    Returns:
        Interpolated value. Single-element lists return that element.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]

    p = max(0.0, min(1.0, p))
    idx = p * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def calculate_dynamic_discharge_floor(
    remaining_hours_prices: list[tuple[int, float]],
    percentile: float = DYNAMIC_DISCHARGE_PERCENTILE,
    min_price_points: int = DYNAMIC_DISCHARGE_MIN_PRICE_POINTS,
    fallback_floor_ore: float = DYNAMIC_DISCHARGE_FALLBACK_FLOOR_ORE,
) -> DynamicDischargeThreshold:
    """Compute a discharge price floor from the remaining today's prices.

    Called every cycle (e.g. every 15 min) with only the hours that are
    still ahead today — as the day progresses, the window shrinks and the
    floor re-adapts to what is actually still to come, not what already
    happened.

    Args:
        remaining_hours_prices: (hour, price_ore) pairs for the remaining
            hours of today, matching the shape the Nordpool adapter already
            exposes hourly prices in (see adapters/nordpool.py today_prices).
            Order does not matter; hour is carried through for diagnostics
            only. `None` prices are ignored.
        percentile: Fraction (0.0-1.0) of the remaining price distribution
            to use as the floor. 0.85 = "discharge only in the ~15% most
            expensive remaining hours". Configurable — NOT hardcoded by
            callers; defaults to the shared constant so all sites start
            from one tunable value.
        min_price_points: Minimum number of remaining price points needed
            to compute a meaningful percentile. Below this (e.g. the last
            hour of the day, or missing Nordpool data), fall back to a
            static floor rather than let a single price point produce a
            degenerate/noisy threshold.
        fallback_floor_ore: Static floor (öre/kWh) used when there are
            too few remaining price points to compute a percentile.

    Returns:
        DynamicDischargeThreshold with the computed floor and diagnostics.
    """
    prices = [price for _, price in remaining_hours_prices if price is not None]
    n = len(prices)

    if n == 0:
        return DynamicDischargeThreshold(
            floor_ore=fallback_floor_ore,
            percentile_used=percentile,
            sample_count=0,
            remaining_min_ore=0.0,
            remaining_max_ore=0.0,
            fallback_used=True,
            reason=f"no remaining price data — using static fallback {fallback_floor_ore:.0f} öre",
        )

    if n < min_price_points:
        return DynamicDischargeThreshold(
            floor_ore=fallback_floor_ore,
            percentile_used=percentile,
            sample_count=n,
            remaining_min_ore=round(min(prices), 2),
            remaining_max_ore=round(max(prices), 2),
            fallback_used=True,
            reason=(
                f"only {n} remaining price point(s) (< {min_price_points}) — "
                f"using static fallback {fallback_floor_ore:.0f} öre"
            ),
        )

    sorted_prices = sorted(prices)
    floor = _percentile(sorted_prices, percentile)

    return DynamicDischargeThreshold(
        floor_ore=round(floor, 2),
        percentile_used=percentile,
        sample_count=n,
        remaining_min_ore=round(sorted_prices[0], 2),
        remaining_max_ore=round(sorted_prices[-1], 2),
        fallback_used=False,
        reason=(
            f"P{percentile * 100:.0f} of {n} remaining hours "
            f"({sorted_prices[0]:.0f}-{sorted_prices[-1]:.0f} öre) = {floor:.1f} öre"
        ),
    )


def resolve_discharge_decision(
    current_price_ore: float,
    threshold: DynamicDischargeThreshold,
    soc_pct: float,
    min_soc_floor_pct: float = DYNAMIC_DISCHARGE_SOC_FLOOR_PCT,
) -> DischargeDecision:
    """Decide whether discharge is permitted this hour.

    Safety-first, same pattern as `core/battery_balancer.effective_min_soc`:
    the SoC floor is checked BEFORE price, and price can never override it.
    A battery at or below `min_soc_floor_pct` is never discharged, no
    matter how high `current_price_ore` is relative to the threshold.

    Args:
        current_price_ore: Current hour's Nordpool price (öre/kWh).
        threshold: Result of `calculate_dynamic_discharge_floor`.
        soc_pct: Current battery SoC (%).
        min_soc_floor_pct: Hard SoC floor — discharge is blocked at/below
            this regardless of price. Configurable per site/battery
            chemistry, matching `BatteryInfo.min_soc` in battery_balancer.

    Returns:
        DischargeDecision with should_discharge and a human-readable reason.
    """
    if soc_pct <= min_soc_floor_pct:
        return DischargeDecision(
            should_discharge=False,
            reason=(
                f"SoC {soc_pct:.0f}% <= floor {min_soc_floor_pct:.0f}% — "
                "discharge blocked regardless of price (safety floor)"
            ),
        )

    if current_price_ore >= threshold.floor_ore:
        return DischargeDecision(
            should_discharge=True,
            reason=f"price {current_price_ore:.0f} öre >= dynamic floor ({threshold.reason})",
        )

    return DischargeDecision(
        should_discharge=False,
        reason=(
            f"price {current_price_ore:.0f} öre < dynamic floor "
            f"{threshold.floor_ore:.1f} öre — waiting for a more expensive remaining hour"
        ),
    )
