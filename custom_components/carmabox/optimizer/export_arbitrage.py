"""CARMA Box — Export Arbitrage (EXPERIMENTAL, opt-in).

Pure Python. No HA imports. Fully testable.

============================================================================
 WHAT THIS MODULE IS
============================================================================
`planner.py` (see its module docstring, line 12) encodes a hard principle:
"Never discharge during export" — every discharge branch (P3/P4/P5/P7)
caps the requested discharge power at `net` (house load minus PV), so the
battery can only ever fill the gap up to zero grid import. It can never
push power OUT to the grid, even when the day's price would make that
clearly profitable.

Root-cause analysis (Forsmark vs. Jerström, 2026-07-19/21): on a shared
high-price evening (~140 öre/kWh, 18:00-21:00), Forsmark's SigenEnergy
inverter — not bound by this rule — discharged decisively (91%→23% SoC)
regardless of house load, and its Wolta score is 65.02%. Jerström's
CARMA-controlled battery, bound by the net-only cap, sat pinned near 100%
SoC through the same window and its Wolta `arbitrage` component measured
-322%: genuine net-export arbitrage is mathematically impossible under a
net-only cap, no matter how good the price forecast (EXP-06) is.

This module adds an OPT-IN extension: given a price signal, a percentile
ranking of today's remaining prices (reusing the same percentile engine as
`discharge_threshold.py`, EXP-06), the house's current net load, the
battery's SoC, and a site-specific `max_export_power_w` ceiling, it
computes a discharge power target that MAY exceed `net` — i.e. authorizes
genuine grid export — but only up to that ceiling, and only during the
top slice of the day's remaining prices.

============================================================================
 WHAT THIS MODULE IS NOT
============================================================================
- It is NOT a replacement for the "never discharge during export"
  principle. That remains the default behavior for every site. This
  module only takes effect when a site explicitly sets
  `enable_export_arbitrage=True` — the default is `False`, and with the
  default, `resolve_export_arbitrage_power()` returns EXACTLY the same
  discharge target the existing net-only logic would produce.
- It is NOT wired into `coordinator.py`, `battery_balancer.py`, or any
  live control loop. This module is a pure decision primitive only —
  wiring it into an actual site's control loop is a SEPARATE change that
  requires its own QC pass and explicit sign-off (see below).
- It does NOT remove or weaken any safety limit. The SoC floor
  (`min_soc_floor_pct`) and hardware discharge limit (`max_discharge_w`)
  are checked FIRST and unconditionally, exactly mirroring the pattern in
  `discharge_threshold.resolve_discharge_decision()` and
  `core/battery_balancer.effective_min_soc()`. Price and export intent are
  only ever permission to use energy that is already safely available.

============================================================================
 WHY THIS IS EXPERIMENTAL / BUSINESS-CRITICAL
============================================================================
Exporting battery-stored energy to the grid — as opposed to merely
avoiding grid import — can interact with a site's electricity contract,
grid tariff structure (nätavgifter), and any "sälja el till nätet"
agreement (or lack thereof) with the site's utility/retailer. Getting this
wrong can cost the customer money or violate their contract. Therefore:

    `enable_export_arbitrage` MUST NOT be turned on for any customer site
    without a separate QC pass AND explicit approval from the customer
    and/or the CARMA Box product owner (Börje). This commit adds the
    capability to CARMA Box's core code only — it does not enable it
    anywhere, and ships with `enable_export_arbitrage=False` and
    `max_export_power_w=0.0` as the library defaults.

============================================================================
 CYCLE-TIME CAVEAT (read before ever configuring max_export_power_w)
============================================================================
`resolve_export_arbitrage_power()` is a PER-CYCLE authorization primitive,
same pattern as `discharge_threshold.resolve_discharge_decision()`: it
gates on the SoC measured at the START of the current control cycle and
does not itself reason about how much energy remains above the floor for
the REST of that cycle. If a site's `max_export_power_w` is large relative
to (control-cycle length x battery capacity), SoC can overshoot below
`min_soc_floor_pct` between one cycle's authorization and the next SoC
reading, because nothing stops the full authorized power from being
applied for the whole cycle. Any wiring of this module into a live control
loop MUST size `max_export_power_w` (and/or the cycle length) so that a
single cycle at full authorized power cannot drain more than a small
fraction of the headroom above `min_soc_floor_pct` — or must additionally
bound the applied power by `battery_balancer.available_kwh()` /
cycle-length, the way `core/battery_balancer.py` already does for ordinary
discharge. This module deliberately does not take a cycle-length parameter
itself, to stay a pure, stateless decision primitive; the caller (the
live-wiring change that is explicitly OUT OF SCOPE here) is responsible
for closing this loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    DEFAULT_MAX_EXPORT_POWER_W,
    EXPORT_ARBITRAGE_FALLBACK_FLOOR_ORE,
    EXPORT_ARBITRAGE_MIN_PRICE_POINTS,
    EXPORT_ARBITRAGE_PERCENTILE,
    EXPORT_ARBITRAGE_SOC_FLOOR_PCT,
)
from .discharge_threshold import DynamicDischargeThreshold, calculate_dynamic_discharge_floor


@dataclass(frozen=True)
class ExportArbitrageDecision:
    """Result of an export-arbitrage evaluation for the current cycle.

    Attributes:
        should_export: True if any power beyond `net` (house load minus PV)
            was authorized this cycle.
        export_w: Discharge power (W) authorized ABOVE house net load —
            i.e. the portion that will actually flow out to the grid.
            Always 0 when `should_export` is False.
        allowed_discharge_w: TOTAL battery discharge power (W) authorized
            this cycle — house net load coverage plus `export_w`. This is
            the number a caller would actually command the battery to.
            Never exceeds `max_discharge_w`.
        reason: Human-readable explanation, for logging/diagnostics.
    """

    should_export: bool
    export_w: float
    allowed_discharge_w: float
    reason: str


def calculate_export_price_threshold(
    remaining_hours_prices: list[tuple[int, float]],
    percentile: float = EXPORT_ARBITRAGE_PERCENTILE,
    min_price_points: int = EXPORT_ARBITRAGE_MIN_PRICE_POINTS,
    fallback_floor_ore: float = EXPORT_ARBITRAGE_FALLBACK_FLOOR_ORE,
) -> DynamicDischargeThreshold:
    """Compute the price floor above which export arbitrage is permitted.

    Thin, intentional wrapper around
    `discharge_threshold.calculate_dynamic_discharge_floor` — reuses the
    exact same percentile engine (EXP-06) rather than re-implementing
    percentile math, but defaults to a STRICTER percentile than ordinary
    load-following discharge: selling stored energy to the grid should be
    reserved for the genuinely best remaining hours of the day, not merely
    "above median" or "above the ordinary discharge floor".

    Args:
        remaining_hours_prices: (hour, price_ore) pairs for the remaining
            hours of today. Same shape as the Nordpool adapter exposes and
            as `discharge_threshold` consumes.
        percentile: Fraction (0.0-1.0) of the remaining price distribution
            required before export is considered. Configurable per site —
            NOT hardcoded by callers; defaults to
            `EXPORT_ARBITRAGE_PERCENTILE` (0.95 = top ~5% of remaining
            hours), deliberately stricter than the ordinary discharge
            floor's default (0.85).
        min_price_points: Minimum remaining price points needed for a
            meaningful percentile; below this, fall back to a static
            floor. Same semantics as `discharge_threshold`.
        fallback_floor_ore: Static floor (öre/kWh) used when there is too
            little remaining price data to compute a percentile.

    Returns:
        A `DynamicDischargeThreshold` (reused type — the shape is
        identical: a floor plus diagnostics).
    """
    return calculate_dynamic_discharge_floor(
        remaining_hours_prices,
        percentile=percentile,
        min_price_points=min_price_points,
        fallback_floor_ore=fallback_floor_ore,
    )


def resolve_export_arbitrage_power(
    current_price_ore: float,
    threshold: DynamicDischargeThreshold,
    soc_pct: float,
    house_net_load_w: float,
    max_discharge_w: float,
    enable_export_arbitrage: bool = False,
    max_export_power_w: float = DEFAULT_MAX_EXPORT_POWER_W,
    min_soc_floor_pct: float = EXPORT_ARBITRAGE_SOC_FLOOR_PCT,
) -> ExportArbitrageDecision:
    """Decide the total battery discharge power for this cycle.

    Safety order (checked FIRST, unconditionally, regardless of the
    `enable_export_arbitrage` flag or price):
      1. `max_discharge_w` (hardware/BMS ceiling) is always respected.
      2. `min_soc_floor_pct` is always respected — at or below the floor,
         allowed discharge is 0 W, full stop, exactly mirroring
         `discharge_threshold.resolve_discharge_decision()`.
    Only once both hold does price/export logic get a say.

    Baseline behavior (`enable_export_arbitrage=False`, the default for
    every site unless explicitly overridden): this function returns
    EXACTLY the "never discharge during export" result — discharge is
    capped at `max(house_net_load_w, 0)`, i.e. never more than the house
    is currently drawing, so no dedicated export ever occurs. This matches
    `planner.py`'s existing P3/P4/P5/P7 branches. This is a regression
    guarantee: turning this module on in a site's dependency tree without
    flipping the flag changes NOTHING.

    Export-enabled behavior (`enable_export_arbitrage=True`): if, in
    addition to the safety checks above, the current price is at/above
    `threshold.floor_ore` (i.e. today's remaining prices rank this hour in
    the configured top percentile — see `calculate_export_price_threshold`
    / EXP-06), discharge is allowed to exceed `house_net_load_w` — up to
    `house_net_load_w + max_export_power_w`, still capped by
    `max_discharge_w`. The portion above `house_net_load_w` is reported
    separately as `export_w` (the power that will actually leave the site
    and flow to the grid).

    Args:
        current_price_ore: Current hour's Nordpool price (öre/kWh).
        threshold: Result of `calculate_export_price_threshold` (or
            `discharge_threshold.calculate_dynamic_discharge_floor`, same
            shape) for the remaining hours of today.
        soc_pct: Current battery SoC (%).
        house_net_load_w: House load minus PV production (W). May be
            negative when PV alone already exceeds house load (the house
            is already net-exporting via PV before any battery action).
        max_discharge_w: Hardware/BMS discharge power ceiling (W).
            ALWAYS respected, independent of export mode. Site-specific —
            e.g. `BatteryInfo.max_discharge_w` in `battery_balancer.py`.
        enable_export_arbitrage: Per-site opt-in flag. Defaults to
            `False`. MUST NOT be set `True` for a customer site without a
            separate QC pass and explicit customer/product-owner approval
            (see module docstring) — this function does not gate that
            approval itself, it only implements the mechanics once
            approved.
        max_export_power_w: Hard ceiling (W) on power exported ABOVE house
            net load. Site-specific — must reflect real contractual/
            hardware export limits. Defaults to `DEFAULT_MAX_EXPORT_POWER_W`
            (0.0 W) so that even if a site accidentally sets
            `enable_export_arbitrage=True` without also configuring an
            explicit export ceiling, no export occurs (belt-and-suspenders
            default, NOT a substitute for explicit per-site configuration).
        min_soc_floor_pct: Hard SoC floor (%) — discharge (of any kind,
            export or ordinary) is blocked at/below this regardless of
            price. Defaults to `EXPORT_ARBITRAGE_SOC_FLOOR_PCT` (20%),
            deliberately higher than the ordinary discharge floor's
            default (15%) as an extra margin for the more aggressive
            export use case. Site-specific — pass the site's real
            configured floor.

    Returns:
        `ExportArbitrageDecision` with the total discharge target, the
        export-only portion, and a human-readable reason.
    """
    max_discharge_w = max(0.0, max_discharge_w)
    max_export_power_w = max(0.0, max_export_power_w)
    net_only_w = max(0.0, house_net_load_w)

    # ── Safety gate 1: SoC floor — always checked first, wins over price ──
    if soc_pct <= min_soc_floor_pct:
        return ExportArbitrageDecision(
            should_export=False,
            export_w=0.0,
            allowed_discharge_w=0.0,
            reason=(
                f"SoC {soc_pct:.0f}% <= floor {min_soc_floor_pct:.0f}% — "
                "discharge blocked regardless of price or export mode (safety floor)"
            ),
        )

    # ── Baseline: never discharge during export (default, unless opted in) ──
    if not enable_export_arbitrage:
        allowed = min(net_only_w, max_discharge_w)
        return ExportArbitrageDecision(
            should_export=False,
            export_w=0.0,
            allowed_discharge_w=allowed,
            reason=(
                "export arbitrage disabled (enable_export_arbitrage=False, default) — "
                f"discharge capped at house net load ({allowed:.0f} W), "
                "identical to baseline 'never discharge during export' behavior"
            ),
        )

    # ── Export mode: still requires a genuinely favorable price ──
    if current_price_ore < threshold.floor_ore:
        allowed = min(net_only_w, max_discharge_w)
        return ExportArbitrageDecision(
            should_export=False,
            export_w=0.0,
            allowed_discharge_w=allowed,
            reason=(
                f"export arbitrage enabled but price {current_price_ore:.0f} öre < "
                f"export floor {threshold.floor_ore:.1f} öre ({threshold.reason}) — "
                f"falling back to house-net-load-only discharge ({allowed:.0f} W)"
            ),
        )

    if max_export_power_w <= 0:
        allowed = min(net_only_w, max_discharge_w)
        return ExportArbitrageDecision(
            should_export=False,
            export_w=0.0,
            allowed_discharge_w=allowed,
            reason=(
                "export arbitrage enabled and price is favorable, but "
                "max_export_power_w <= 0 for this site (not configured) — "
                f"falling back to house-net-load-only discharge ({allowed:.0f} W)"
            ),
        )

    uncapped_target_w = net_only_w + max_export_power_w
    allowed = min(uncapped_target_w, max_discharge_w)
    export_portion = max(0.0, allowed - net_only_w)

    return ExportArbitrageDecision(
        should_export=export_portion > 0,
        export_w=export_portion,
        allowed_discharge_w=allowed,
        reason=(
            f"price {current_price_ore:.0f} öre >= export floor {threshold.floor_ore:.1f} öre "
            f"({threshold.reason}) — exporting {export_portion:.0f} W above house net load "
            f"({net_only_w:.0f} W), capped by max_export_power_w={max_export_power_w:.0f} W "
            f"and max_discharge_w={max_discharge_w:.0f} W"
        ),
    )
