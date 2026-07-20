"""Regression test — Jerström battery-drain incident, 2026-07-19.

Root cause: `custom_components/bat_balancer` on the Jerström site
(GoodWe 20kW + Lynx D 5kWh) used a STATIC discharge price floor
(`input_number.brain_price_discharge_floor_ore`, 50 öre/kWh at the time).
Discharge started at full power the moment price crossed the threshold.

Real SoC history vs. Nordpool prices on 2026-07-19 showed:
  - Threshold (50 öre) reached ~18:30
  - Battery fully drained by ~19:50 (80 minutes later)
  - The day's ACTUAL price peak was 130 öre at ~21:45 — reached ~2 hours
    AFTER the battery was already empty.

A provisional bump to 90 öre/kWh was applied as a stop-gap, but remains a
fixed value that doesn't adapt to a given day's price shape.

This test reconstructs an hourly price curve consistent with the incident
report (crossing ~50 öre around 18:00-19:00, peaking at 130 öre around
21:00-22:00) and simulates, hour by hour, what each discharge strategy
would have done:

  1. OLD static-floor strategy (50 öre) — expected to drain the battery to
     its SoC floor before the day's actual peak hour.
  2. NEW dynamic percentile strategy (this module) — expected to hold back
     discharge until later in the evening, so the battery still has energy
     available AT the peak hour.

Both simulations use the same battery state and a fast discharge rate so
that "days" resolve in a handful of hourly steps — the point under test is
WHEN each strategy releases the energy, not the exact minute-level curve
(the production system runs on hourly Nordpool granularity).
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.discharge_threshold import (
    calculate_dynamic_discharge_floor,
    resolve_discharge_decision,
)

# Reconstructed 2026-07-19 hourly price curve (öre/kWh), consistent with
# the incident report: crosses 50 öre in the 18:00 hour, peaks at 130 öre
# in the 21:00 hour (real peak ~21:45).
JERSTROM_20260719_HOURLY_PRICES: dict[int, float] = {
    0: 35, 1: 30, 2: 28, 3: 27, 4: 30, 5: 38,
    6: 55, 7: 62, 8: 58, 9: 45, 10: 35, 11: 28,
    12: 22, 13: 20, 14: 22, 15: 28, 16: 38, 17: 45,
    18: 52, 19: 68, 20: 95, 21: 130, 22: 108, 23: 75,
}  # fmt: skip

OLD_STATIC_FLOOR_ORE = 50.0  # The original, pre-incident static threshold
PEAK_HOUR = 21  # Real peak ~21:45 falls within the 21:00 hour
START_HOUR = 17  # Simulation starts a bit before the threshold is first crossed
END_HOUR = 24
BATTERY_MIN_SOC_PCT = 15.0
BATTERY_START_SOC_PCT = 80.0
# Fast discharge so the ~80-minute real-world drain resolves within the
# hourly simulation grid: (80 - 15)% drained in ~1.33h → ~49%/hour.
OLD_STRATEGY_DRAIN_PCT_PER_HOUR = 49.0
# New strategy: modest, realistic discharge rate once actually authorized.
NEW_STRATEGY_DRAIN_PCT_PER_HOUR = 25.0


def _simulate_old_static_strategy() -> dict[int, float]:
    """Simulate the OLD static-floor discharge strategy hour by hour.

    Returns {hour: soc_after_hour}.
    """
    soc = BATTERY_START_SOC_PCT
    soc_by_hour: dict[int, float] = {}

    for hour in range(START_HOUR, END_HOUR):
        price = JERSTROM_20260719_HOURLY_PRICES[hour]
        if price >= OLD_STATIC_FLOOR_ORE and soc > BATTERY_MIN_SOC_PCT:
            soc = max(BATTERY_MIN_SOC_PCT, soc - OLD_STRATEGY_DRAIN_PCT_PER_HOUR)
        soc_by_hour[hour] = soc

    return soc_by_hour


def _simulate_new_dynamic_strategy(percentile: float = 0.85) -> dict[int, float]:
    """Simulate the NEW dynamic percentile discharge strategy hour by hour.

    The threshold is recomputed every hour from ONLY the remaining hours of
    the day (mirrors calling `calculate_dynamic_discharge_floor` every
    coordinator cycle with the live Nordpool `today` array sliced from the
    current hour onward).

    Returns {hour: soc_after_hour}.
    """
    soc = BATTERY_START_SOC_PCT
    soc_by_hour: dict[int, float] = {}

    for hour in range(START_HOUR, END_HOUR):
        remaining = [(h, JERSTROM_20260719_HOURLY_PRICES[h]) for h in range(hour, END_HOUR)]
        threshold = calculate_dynamic_discharge_floor(remaining, percentile=percentile)
        decision = resolve_discharge_decision(
            current_price_ore=JERSTROM_20260719_HOURLY_PRICES[hour],
            threshold=threshold,
            soc_pct=soc,
            min_soc_floor_pct=BATTERY_MIN_SOC_PCT,
        )
        if decision.should_discharge:
            soc = max(BATTERY_MIN_SOC_PCT, soc - NEW_STRATEGY_DRAIN_PCT_PER_HOUR)
        soc_by_hour[hour] = soc

    return soc_by_hour


class TestJerstromIncident20260719Regression:
    """Old static-floor strategy drains before the peak; new one doesn't."""

    def test_old_static_strategy_reproduces_the_incident(self) -> None:
        """Sanity check: the OLD strategy really does empty the battery pre-peak.

        This documents the bug being fixed — the battery reaches the SoC
        floor at/around hour 19 (18:30-19:50 in the real incident), well
        before the actual peak hour (21).
        """
        soc_by_hour = _simulate_old_static_strategy()
        assert soc_by_hour[19] <= BATTERY_MIN_SOC_PCT + 1.0, (
            "OLD strategy should have drained the battery to its floor by "
            f"hour 19 (incident: fully drained ~19:50), got SoC={soc_by_hour[19]}"
        )
        # And it stays empty through the actual peak hour.
        assert soc_by_hour[PEAK_HOUR] <= BATTERY_MIN_SOC_PCT + 1.0

    def test_new_dynamic_strategy_withholds_discharge_before_20(self) -> None:
        """NEW strategy must NOT drain the battery during 18:00-19:59.

        This is the core regression assertion requested for this fix: the
        dynamic percentile threshold should recognize that 52-68 öre is
        cheap relative to what the rest of the day still holds (up to 130
        öre), and wait — unlike the old fixed 50 öre floor which fired
        immediately.
        """
        soc_by_hour = _simulate_new_dynamic_strategy()
        assert soc_by_hour[18] == BATTERY_START_SOC_PCT, (
            "NEW strategy discharged during hour 18 — should have waited "
            f"for a relatively more expensive hour, got SoC={soc_by_hour[18]}"
        )
        assert soc_by_hour[19] == BATTERY_START_SOC_PCT, (
            "NEW strategy discharged during hour 19 — should have waited, "
            f"got SoC={soc_by_hour[19]}"
        )

    def test_new_dynamic_strategy_has_energy_available_at_the_peak(self) -> None:
        """NEW strategy must still have usable SoC AT the real peak hour (21).

        This is the concrete "would this fix have helped" check: at the
        moment the price actually peaks (130 öre, hour 21), the dynamically
        managed battery must be above its safety floor — i.e. actually able
        to discharge and capture the peak, unlike the old strategy which had
        already emptied itself ~2 hours earlier.
        """
        soc_by_hour = _simulate_new_dynamic_strategy()
        soc_entering_peak_hour = soc_by_hour.get(PEAK_HOUR - 1, BATTERY_START_SOC_PCT)
        assert soc_entering_peak_hour > BATTERY_MIN_SOC_PCT, (
            "NEW strategy had no energy left entering the peak hour — "
            f"SoC={soc_entering_peak_hour}, floor={BATTERY_MIN_SOC_PCT}"
        )

    def test_new_dynamic_strategy_actually_discharges_at_the_peak(self) -> None:
        """The dynamic floor at the peak hour must be low enough to trigger discharge.

        Confirms the threshold computed from the remaining hours (just the
        peak + trailing hours) is at/below the peak price itself, so the
        battery is put to use exactly when it matters most.
        """
        remaining_at_peak = [
            (h, JERSTROM_20260719_HOURLY_PRICES[h]) for h in range(PEAK_HOUR, END_HOUR)
        ]
        threshold = calculate_dynamic_discharge_floor(remaining_at_peak, percentile=0.85)
        decision = resolve_discharge_decision(
            current_price_ore=JERSTROM_20260719_HOURLY_PRICES[PEAK_HOUR],
            threshold=threshold,
            soc_pct=BATTERY_START_SOC_PCT,
            min_soc_floor_pct=BATTERY_MIN_SOC_PCT,
        )
        assert decision.should_discharge is True, (
            f"Expected discharge to trigger at the peak hour, got: {decision.reason}"
        )

    def test_new_strategy_saves_more_soc_than_old_strategy_pre_peak(self) -> None:
        """Direct comparison: NEW strategy preserves materially more SoC before the peak."""
        old_soc_by_hour = _simulate_old_static_strategy()
        new_soc_by_hour = _simulate_new_dynamic_strategy()

        hour_before_peak = PEAK_HOUR - 1
        assert new_soc_by_hour[hour_before_peak] > old_soc_by_hour[hour_before_peak], (
            "NEW strategy should retain more SoC than OLD strategy just before "
            f"the peak hour: old={old_soc_by_hour[hour_before_peak]}, "
            f"new={new_soc_by_hour[hour_before_peak]}"
        )

    def test_safety_floor_still_respected_under_new_strategy(self) -> None:
        """Even under the new strategy, SoC never drops below the configured floor."""
        soc_by_hour = _simulate_new_dynamic_strategy()
        assert all(soc >= BATTERY_MIN_SOC_PCT for soc in soc_by_hour.values())
