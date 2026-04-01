"""ML EV usage prediction — predict_ev_usage tests.

Tests for the EV energy usage prediction method in ConsumptionPredictor.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor


class TestMLEvUsagePrediction:
    """predict_ev_usage: weekday-aware EV energy consumption forecasting."""

    def test_no_data_returns_zero(self) -> None:
        """Without EV usage history, prediction = 0.0."""
        pred = ConsumptionPredictor()
        assert pred.predict_ev_usage(0) == 0.0
        assert pred.predict_ev_usage(6) == 0.0

    def test_single_sample_returns_that_value(self) -> None:
        """One EV usage sample → prediction equals that value."""
        pred = ConsumptionPredictor()
        pred.add_ev_usage(weekday=1, soc_delta_pct=15.0, capacity_kwh=100.0)
        result = pred.predict_ev_usage(1)
        assert abs(result - 15.0) < 0.1

    def test_weekday_specific_prediction(self) -> None:
        """Monday commute (high) vs Sunday (low) → different predictions."""
        pred = ConsumptionPredictor()
        # Monday: 15 kWh (commute)
        for _ in range(5):
            pred.add_ev_usage(weekday=0, soc_delta_pct=15.0, capacity_kwh=100.0)
        # Sunday: 5 kWh (local)
        for _ in range(5):
            pred.add_ev_usage(weekday=6, soc_delta_pct=5.0, capacity_kwh=100.0)

        monday = pred.predict_ev_usage(0)
        sunday = pred.predict_ev_usage(6)
        assert monday > sunday
        assert abs(monday - 15.0) < 1.0
        assert abs(sunday - 5.0) < 1.0

    def test_recency_bias_favors_recent_samples(self) -> None:
        """Recent samples weigh more than old ones (exponential weighting)."""
        pred = ConsumptionPredictor()
        # Old samples: 10 kWh
        for _ in range(5):
            pred.add_ev_usage(weekday=0, soc_delta_pct=10.0, capacity_kwh=100.0)
        # Recent samples: 20 kWh
        for _ in range(5):
            pred.add_ev_usage(weekday=0, soc_delta_pct=20.0, capacity_kwh=100.0)

        result = pred.predict_ev_usage(0)
        # Should be closer to 20 than 10 (recent bias)
        assert result > 15.0

    def test_ev_usage_survives_round_trip(self) -> None:
        """EV usage history preserved through to_dict/from_dict."""
        pred = ConsumptionPredictor()
        for _ in range(3):
            pred.add_ev_usage(weekday=2, soc_delta_pct=12.0, capacity_kwh=80.0)

        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert abs(restored.predict_ev_usage(2) - pred.predict_ev_usage(2)) < 0.01

    def test_zero_soc_delta_records_zero_kwh(self) -> None:
        """soc_delta_pct=0 → 0 kWh stored (car not used)."""
        pred = ConsumptionPredictor()
        for _ in range(5):
            pred.add_ev_usage(weekday=3, soc_delta_pct=0.0, capacity_kwh=100.0)
        assert pred.predict_ev_usage(3) == 0.0

    def test_other_weekday_unaffected(self) -> None:
        """Adding EV usage for Monday doesn't affect Friday prediction."""
        pred = ConsumptionPredictor()
        for _ in range(5):
            pred.add_ev_usage(weekday=0, soc_delta_pct=20.0, capacity_kwh=100.0)
        assert pred.predict_ev_usage(4) == 0.0  # Friday: no data
