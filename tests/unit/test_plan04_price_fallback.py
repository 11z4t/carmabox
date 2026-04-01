"""PLAN-04: Day 3+ historical price fallback in multiday planner.

Tests that when Nordpool prices are unavailable for day 3+,
historical mean prices are used as fallback.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.multiday_planner import (
    build_day_inputs,
)

HISTORICAL_MEAN = [
    10.0, 9.0, 8.5, 8.0, 8.5, 10.0,   # 00-05
    15.0, 25.0, 35.0, 38.0, 36.0, 34.0,  # 06-11
    32.0, 30.0, 28.0, 30.0, 35.0, 45.0,  # 12-17
    55.0, 60.0, 58.0, 50.0, 35.0, 20.0,  # 18-23
]


class TestPlan04PriceFallback:
    """PLAN-04: Day 3+ uses historical mean prices."""

    def test_day3_uses_historical_mean_when_no_nordpool(self) -> None:
        """Without price_model, day 3 falls back to historical mean prices."""
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        assert len(inputs) == 3
        # Day 2 (index 2) = day 3 — no Nordpool, no model
        assert inputs[2].price_source == "historical_mean"
        assert inputs[2].prices == HISTORICAL_MEAN

    def test_day1_uses_nordpool_when_available(self) -> None:
        """Known Nordpool prices for today take priority over historical."""
        today = [20.0] * 24
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            known_prices_today=today,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        assert inputs[0].price_source == "nordpool"
        assert inputs[0].prices == today

    def test_day2_uses_nordpool_when_available(self) -> None:
        """Known Nordpool prices for tomorrow used on day 2."""
        tomorrow = [30.0] * 24
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            known_prices_tomorrow=tomorrow,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        assert inputs[1].price_source == "nordpool"
        assert inputs[1].prices == tomorrow

    def test_no_historical_fallback_uses_default_50(self) -> None:
        """Without any price source, default is 50 öre/kWh."""
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=4,
        )
        assert inputs[2].price_source == "default"
        assert inputs[2].prices == [50.0] * 24

    def test_historical_fallback_used_for_all_unpredicted_days(self) -> None:
        """Days 3-7 all use historical mean when no model."""
        inputs = build_day_inputs(
            days=7,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        for i in range(2, 7):
            assert inputs[i].price_source == "historical_mean", f"Day {i} wrong source"
            assert inputs[i].prices == HISTORICAL_MEAN

    def test_price_model_overrides_historical_for_day3(self) -> None:
        """If a price model exists, it takes priority over historical mean."""
        from unittest.mock import MagicMock

        model = MagicMock()
        model.has_sufficient_data = True
        model.predict_24h.return_value = [42.0] * 24

        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            price_model=model,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        assert inputs[2].price_source == "predicted"
        assert inputs[2].prices == [42.0] * 24

    def test_historical_prices_not_mutated(self) -> None:
        """build_day_inputs does not mutate the historical_mean_prices list."""
        original = list(HISTORICAL_MEAN)
        inputs = build_day_inputs(
            days=5,
            start_hour=0,
            start_weekday=0,
            start_month=4,
            historical_mean_prices=HISTORICAL_MEAN,
        )
        # Modify returned prices — original should be unchanged
        inputs[2].prices[0] = 999.0
        assert HISTORICAL_MEAN[0] == original[0]
