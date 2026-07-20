"""Tests for CARMA Box dynamic discharge threshold module.

Root cause: Jerström 2026-07-19 — a static öre/kWh discharge floor
emptied the battery ~2h before the day's actual price peak. These tests
cover the percentile-based replacement across different price shapes and
edge cases, independent of any single site.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.discharge_threshold import (
    DischargeDecision,
    DynamicDischargeThreshold,
    calculate_dynamic_discharge_floor,
    resolve_discharge_decision,
)


def _pairs(prices: list[float], start_hour: int = 0) -> list[tuple[int, float]]:
    """Helper: build (hour, price) pairs from a flat price list."""
    return [(start_hour + i, p) for i, p in enumerate(prices)]


class TestCalculateDynamicDischargeFloor:
    """Threshold computation across different price shapes."""

    def test_flat_day_floor_equals_flat_price(self) -> None:
        """All hours the same price → floor should equal that price."""
        prices = _pairs([50.0] * 10)
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert result.floor_ore == 50.0
        assert not result.fallback_used

    def test_two_peak_day_floor_sits_between_peaks_and_troughs(self) -> None:
        """Two-peak day: floor should be above the troughs, below the peaks."""
        # Morning peak, midday dip, evening peak
        prices = _pairs([30, 90, 95, 40, 25, 20, 30, 60, 100, 110, 105, 50])
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert min(p for _, p in prices) < result.floor_ore < max(p for _, p in prices)

    def test_single_spike_day_floor_isolates_the_spike(self) -> None:
        """One expensive hour among many cheap ones — high percentile isolates it.

        The floor should sit above the entire cheap baseline (max 23 öre),
        so that only the spike hour (200 öre) would actually be permitted
        to discharge — the baseline hours never qualify.
        """
        prices = _pairs([20, 22, 21, 23, 20, 200, 22, 21])
        baseline_max = max(p for h, p in prices if p != 200)
        result = calculate_dynamic_discharge_floor(prices, percentile=0.9)
        assert baseline_max < result.floor_ore < 200.0

        spike_decision = resolve_discharge_decision(200.0, result, soc_pct=50.0)
        baseline_decision = resolve_discharge_decision(23.0, result, soc_pct=50.0)
        assert spike_decision.should_discharge is True
        assert baseline_decision.should_discharge is False

    def test_rising_trend_floor_above_median(self) -> None:
        """Strictly rising prices — floor should be well above the midpoint."""
        prices = _pairs([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert result.floor_ore > 55.0  # above midpoint (55)

    def test_falling_trend_floor_reflects_remaining_hours(self) -> None:
        """Strictly falling prices — floor still reflects the (falling) distribution."""
        prices = _pairs([100, 90, 80, 70, 60, 50, 40, 30, 20, 10])
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert result.floor_ore < 100.0
        assert result.floor_ore > 10.0

    def test_percentile_is_configurable_not_hardcoded(self) -> None:
        """Different percentile parameters must yield different floors."""
        prices = _pairs([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        low = calculate_dynamic_discharge_floor(prices, percentile=0.5)
        high = calculate_dynamic_discharge_floor(prices, percentile=0.95)
        assert high.floor_ore > low.floor_ore
        assert low.percentile_used == 0.5
        assert high.percentile_used == 0.95

    def test_empty_price_array_uses_fallback(self) -> None:
        """No price data at all → static fallback floor, not a crash."""
        result = calculate_dynamic_discharge_floor([], percentile=0.85, fallback_floor_ore=42.0)
        assert result.fallback_used
        assert result.floor_ore == 42.0
        assert result.sample_count == 0

    def test_too_few_price_points_uses_fallback(self) -> None:
        """Fewer points than min_price_points → static fallback, not a noisy percentile."""
        prices = _pairs([130.0])  # single remaining hour (e.g. 23:00)
        result = calculate_dynamic_discharge_floor(
            prices, percentile=0.85, min_price_points=2, fallback_floor_ore=50.0
        )
        assert result.fallback_used
        assert result.floor_ore == 50.0
        assert result.sample_count == 1

    def test_all_prices_equal_no_fallback_needed(self) -> None:
        """All-equal prices with enough points should NOT trigger fallback."""
        prices = _pairs([75.0, 75.0, 75.0, 75.0])
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert not result.fallback_used
        assert result.floor_ore == 75.0

    def test_none_prices_are_ignored(self) -> None:
        """None entries (e.g. missing hourly data) are filtered, not crashing."""
        prices: list[tuple[int, float]] = [(10, 50.0), (11, None), (12, 60.0), (13, 70.0)]  # type: ignore[list-item]
        result = calculate_dynamic_discharge_floor(prices, percentile=0.5, min_price_points=2)
        assert result.sample_count == 3
        assert not result.fallback_used

    def test_percentile_clamped_to_valid_range(self) -> None:
        """Out-of-range percentiles are clamped, not crashing or extrapolating."""
        prices = _pairs([10, 20, 30, 40, 50])
        below = calculate_dynamic_discharge_floor(prices, percentile=-0.5)
        above = calculate_dynamic_discharge_floor(prices, percentile=1.5)
        assert below.floor_ore == 10.0
        assert above.floor_ore == 50.0

    def test_reason_mentions_sample_count(self) -> None:
        """Diagnostics string should be informative (sample count present)."""
        prices = _pairs([10, 20, 30, 40, 50])
        result = calculate_dynamic_discharge_floor(prices, percentile=0.85)
        assert "5" in result.reason

    def test_shrinking_window_as_day_progresses(self) -> None:
        """Recomputing with a shrinking remaining-hours window changes the floor.

        This mirrors calling the function every cycle: early in the day the
        window is wide (includes cheap hours), later it narrows toward the
        expensive tail — the floor should generally rise as cheap hours drop
        out of the window.
        """
        full_day = _pairs([20, 22, 25, 30, 40, 130], start_hour=18)
        late_day = _pairs([40, 130], start_hour=22)

        early = calculate_dynamic_discharge_floor(full_day, percentile=0.85)
        late = calculate_dynamic_discharge_floor(late_day, percentile=0.85)
        assert late.floor_ore >= early.floor_ore


class TestResolveDischargeDecision:
    """Discharge permission logic — SoC floor must always win over price."""

    def test_soc_floor_blocks_even_at_extreme_price(self) -> None:
        """No price is high enough to authorize discharge below the SoC floor."""
        threshold = DynamicDischargeThreshold(
            floor_ore=10.0,
            percentile_used=0.85,
            sample_count=5,
            remaining_min_ore=10.0,
            remaining_max_ore=500.0,
            fallback_used=False,
            reason="test",
        )
        decision = resolve_discharge_decision(
            current_price_ore=999.0,
            threshold=threshold,
            soc_pct=14.0,
            min_soc_floor_pct=15.0,
        )
        assert isinstance(decision, DischargeDecision)
        assert decision.should_discharge is False
        assert "floor" in decision.reason.lower()

    def test_soc_exactly_at_floor_blocks(self) -> None:
        """SoC exactly AT the floor still blocks (boundary is inclusive of block)."""
        threshold = DynamicDischargeThreshold(
            floor_ore=50.0,
            percentile_used=0.85,
            sample_count=5,
            remaining_min_ore=20.0,
            remaining_max_ore=100.0,
            fallback_used=False,
            reason="test",
        )
        decision = resolve_discharge_decision(
            current_price_ore=200.0,
            threshold=threshold,
            soc_pct=15.0,
            min_soc_floor_pct=15.0,
        )
        assert decision.should_discharge is False

    def test_price_above_floor_and_soc_ok_discharges(self) -> None:
        """Above SoC floor and above price floor → discharge permitted."""
        threshold = DynamicDischargeThreshold(
            floor_ore=90.0,
            percentile_used=0.85,
            sample_count=5,
            remaining_min_ore=20.0,
            remaining_max_ore=130.0,
            fallback_used=False,
            reason="test",
        )
        decision = resolve_discharge_decision(
            current_price_ore=130.0,
            threshold=threshold,
            soc_pct=60.0,
            min_soc_floor_pct=15.0,
        )
        assert decision.should_discharge is True

    def test_price_below_floor_blocks_even_with_healthy_soc(self) -> None:
        """Healthy SoC does not force discharge when price is below the dynamic floor."""
        threshold = DynamicDischargeThreshold(
            floor_ore=90.0,
            percentile_used=0.85,
            sample_count=5,
            remaining_min_ore=20.0,
            remaining_max_ore=130.0,
            fallback_used=False,
            reason="test",
        )
        decision = resolve_discharge_decision(
            current_price_ore=50.0,
            threshold=threshold,
            soc_pct=80.0,
            min_soc_floor_pct=15.0,
        )
        assert decision.should_discharge is False

    def test_price_exactly_at_floor_discharges(self) -> None:
        """Price exactly equal to floor is treated as a permitted discharge (>=)."""
        threshold = DynamicDischargeThreshold(
            floor_ore=90.0,
            percentile_used=0.85,
            sample_count=5,
            remaining_min_ore=20.0,
            remaining_max_ore=130.0,
            fallback_used=False,
            reason="test",
        )
        decision = resolve_discharge_decision(
            current_price_ore=90.0,
            threshold=threshold,
            soc_pct=50.0,
            min_soc_floor_pct=15.0,
        )
        assert decision.should_discharge is True
