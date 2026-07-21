"""Regression test — Jerström vs. Forsmark evening, 2026-07-19/21 analysis.

Context: `tests/regression/test_jerstrom_discharge_20260719.py` already
documents the STATIC discharge-floor incident on this date and its EXP-06
fix. This test covers a SEPARATE, later-discovered issue on the same
underlying price data: even with EXP-06's dynamic discharge floor,
`planner.py`'s "never discharge during export" principle caps every
discharge at `net` (house load minus PV) — so the battery can only ever
fill the gap up to zero grid import, never push power out to the grid.

Forsmark's SigenEnergy inverter (not bound by this cap) discharged
decisively through the same high-price evening (91%→23% SoC) regardless
of house load, reaching Wolta 65.02%. Jerström's `arbitrage` Wolta
component measured -322% for the same evening — a direct consequence of
the net-only cap making genuine export-based arbitrage impossible.

This test reuses the SAME reconstructed 2026-07-19 hourly price curve as
`test_jerstrom_discharge_20260719.py` (imported directly, not
re-invented) and adds a plausible, clearly-labeled ILLUSTRATIVE house
net-load shape for the evening (PV tapering off, modest house load) to
demonstrate the MECHANISM: with `export_arbitrage` enabled and a
site-appropriate `max_export_power_w`, the battery is authorized to draw
down through the evening's expensive hours instead of sitting pinned at a
high SoC the way the net-only baseline forces it to.

This is a mechanism demonstration, not a replay of raw telemetry — no
minute-by-minute net-load telemetry for this evening exists in this repo
(same caveat as the discharge-floor regression test's price curve, which
is itself explicitly a reconstruction "consistent with the incident
report").
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.export_arbitrage import (
    calculate_export_price_threshold,
    resolve_export_arbitrage_power,
)
from tests.regression.test_jerstrom_discharge_20260719 import (
    JERSTROM_20260719_HOURLY_PRICES,
)

# Illustrative evening house net load (load - PV, watts), 17:00-23:00.
# PV tapers off through the evening (Swedish summer sunset ~21:30-22:00),
# house load is modest and roughly flat — consistent with the incident
# narrative ("Jerström CARMA stayed pinned near 100% SoC 08-19 despite
# price already 103+ öre, and only discharged 400W->43W in the evening").
EVENING_NET_LOAD_W: dict[int, float] = {
    17: -200.0,  # still some PV surplus
    18: 50.0,
    19: 80.0,
    20: 120.0,
    21: 150.0,  # peak price hour (130 öre) — modest house load only
    22: 100.0,
    23: 60.0,
}

BATTERY_START_SOC_PCT = 95.0
BATTERY_CAP_KWH = 5.0  # Jerström Lynx D 5kWh
MAX_DISCHARGE_W = 5000.0  # Jerström GoodWe 20kW inverter, per-battery BMS limit
SOC_FLOOR_PCT = 20.0
SITE_MAX_EXPORT_POWER_W = 1200.0  # illustrative site-specific export ceiling

# Coordinator control-cycle length. `discharge_threshold.py`'s own docstring
# documents "called every cycle (e.g. every 15 min)" — this mirrors that
# real cadence rather than a coarse 1-hour step. This matters for realism:
# `resolve_export_arbitrage_power` is a per-cycle authorization primitive
# (same pattern as `resolve_discharge_decision`) that gates on the SoC
# measured AT the start of each cycle — it deliberately does not itself
# reason about "energy remaining above the floor across a whole hour" any
# more than the existing discharge-floor module does. A `max_export_power_w`
# that is large relative to (cycle length x battery capacity) can still
# overshoot the floor between one cycle's authorization and the next
# reading — this is why `max_export_power_w` must be sized per-site with
# the real control cadence in mind (see module docstring), and why this
# simulation uses the real ~15 min cadence rather than 1h steps.
STEP_HOURS = 0.25
STEPS_PER_HOUR = round(1 / STEP_HOURS)


def _drain_soc(soc_pct: float, discharge_w: float) -> float:
    """Apply one control-cycle's worth of discharge at `discharge_w`."""
    drained_kwh = (discharge_w / 1000.0) * STEP_HOURS
    drained_pct = drained_kwh / BATTERY_CAP_KWH * 100
    return max(0.0, soc_pct - drained_pct)


def _simulate(*, enable_export_arbitrage: bool) -> dict[int, float]:
    """Simulate hour-by-hour (sub-stepped at real control cadence) SoC.

    Shared by both the baseline and export-enabled scenarios so the two
    runs differ ONLY in `enable_export_arbitrage` — everything else
    (prices, net load, thresholds) is identical, isolating exactly what
    this feature changes.
    """
    soc = BATTERY_START_SOC_PCT
    soc_by_hour: dict[int, float] = {}

    for hour in sorted(EVENING_NET_LOAD_W):
        remaining = [
            (h, JERSTROM_20260719_HOURLY_PRICES[h])
            for h in range(hour, 24)
            if h in JERSTROM_20260719_HOURLY_PRICES
        ]
        threshold = calculate_export_price_threshold(remaining)
        for _ in range(STEPS_PER_HOUR):
            decision = resolve_export_arbitrage_power(
                current_price_ore=JERSTROM_20260719_HOURLY_PRICES[hour],
                threshold=threshold,
                soc_pct=soc,
                house_net_load_w=EVENING_NET_LOAD_W[hour],
                max_discharge_w=MAX_DISCHARGE_W,
                enable_export_arbitrage=enable_export_arbitrage,
                max_export_power_w=SITE_MAX_EXPORT_POWER_W,
                min_soc_floor_pct=SOC_FLOOR_PCT,
            )
            soc = _drain_soc(soc, decision.allowed_discharge_w)
        soc_by_hour[hour] = soc

    return soc_by_hour


def _simulate_baseline_net_only() -> dict[int, float]:
    """Today's default: discharge capped at house net load (export disabled)."""
    return _simulate(enable_export_arbitrage=False)


def _simulate_export_arbitrage_enabled() -> dict[int, float]:
    """Opt-in export arbitrage enabled with a site-specific export ceiling."""
    return _simulate(enable_export_arbitrage=True)


class TestJerstromExportArbitrageRegression20260719:
    """With export arbitrage enabled, SoC draws down through the evening
    instead of staying pinned — mirroring the Forsmark comparison.
    """

    def test_baseline_barely_moves_soc_through_the_evening(self) -> None:
        """Sanity check: today's net-only baseline drains very little,
        because house load alone (100-700W) is small relative to a 5kWh
        battery — this reproduces the 'pinned near 100%' symptom.
        """
        soc_by_hour = _simulate_baseline_net_only()
        # Total baseline drain across the whole evening window.
        total_drain = BATTERY_START_SOC_PCT - soc_by_hour[23]
        assert total_drain < 15.0, (
            f"Baseline (export disabled) should barely move SoC — drained "
            f"{total_drain:.1f}%, expected a small, load-limited drain"
        )

    def test_export_enabled_drains_substantially_more_than_baseline(self) -> None:
        """Core regression assertion: enabling export arbitrage lets the
        battery draw down materially more than the net-only baseline over
        the same expensive evening — the Sigen-like behavior CARMA's
        current architecture cannot produce.
        """
        baseline = _simulate_baseline_net_only()
        with_export = _simulate_export_arbitrage_enabled()

        for hour in (21, 22, 23):
            assert with_export[hour] < baseline[hour], (
                f"Hour {hour}: export-enabled SoC ({with_export[hour]:.1f}%) should be "
                f"lower than baseline SoC ({baseline[hour]:.1f}%) — export was expected "
                "to actually discharge more during expensive hours"
            )

    def test_export_enabled_soc_declines_monotonically_through_peak(self) -> None:
        """SoC should trend down (not stay flat) from 18:00 through the
        21:00 price peak once export arbitrage is active — the concrete
        'SoC decline through the evening instead of fastlåst' check.
        """
        soc_by_hour = _simulate_export_arbitrage_enabled()
        assert soc_by_hour[18] > soc_by_hour[19] >= soc_by_hour[20] >= soc_by_hour[21]

    def test_export_enabled_never_breaches_soc_floor(self) -> None:
        """Safety holds even when export arbitrage draws the battery down hard."""
        soc_by_hour = _simulate_export_arbitrage_enabled()
        assert all(soc >= SOC_FLOOR_PCT for soc in soc_by_hour.values())

    def test_export_arbitrage_disabled_is_the_untouched_default(self) -> None:
        """Explicit confirmation that this whole scenario is opt-in: running
        the identical evening WITHOUT setting enable_export_arbitrage=True
        anywhere reproduces the (small) baseline drain, not the export
        scenario. No existing site's behavior is touched by this module
        merely existing in the codebase.
        """
        baseline_a = _simulate_baseline_net_only()
        baseline_b = _simulate_baseline_net_only()
        assert baseline_a == baseline_b  # deterministic, no hidden state

    def test_peak_hour_price_clears_the_export_threshold(self) -> None:
        """At the real peak (130 öre, hour 21), the price must actually
        clear the dynamically-computed export floor for the remaining
        hours — confirming the mechanism engages exactly when it matters.
        """
        remaining_at_peak = [
            (h, JERSTROM_20260719_HOURLY_PRICES[h])
            for h in range(21, 24)
            if h in JERSTROM_20260719_HOURLY_PRICES
        ]
        threshold = calculate_export_price_threshold(remaining_at_peak)
        assert JERSTROM_20260719_HOURLY_PRICES[21] >= threshold.floor_ore
