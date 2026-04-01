"""ML Quality improvements — temperature-aware prediction, seasonal calibration, MAE.

Tests for:
- predict_24h with outdoor_temp_c (temperature-aware)
- update_seasonal_factor (auto-calibration)
- mean_absolute_error property
- data_coverage_pct property
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)


def _trained_predictor(base_kw: float = 2.0) -> ConsumptionPredictor:
    """Return trained predictor with uniform consumption samples."""
    pred = ConsumptionPredictor()
    for wd in range(7):
        for h in range(24):
            for _ in range(4):
                pred.add_sample(HourSample(weekday=wd, hour=h, month=4, consumption_kw=base_kw))
    return pred


class TestTemperatureAwarePrediction:
    """Temperature-adjusted predict_24h."""

    def test_predict_24h_accepts_outdoor_temp_c(self) -> None:
        """predict_24h does not crash when outdoor_temp_c is provided."""
        pred = _trained_predictor()
        result = pred.predict_24h(start_hour=0, weekday=0, month=4, outdoor_temp_c=5.0)
        assert len(result) == 24

    def test_no_temp_data_returns_1_0_adjustment(self) -> None:
        """Without temperature history, adjustment = 1.0 (no change)."""
        pred = _trained_predictor()
        # No temp samples recorded → get_temp_adjustment returns 1.0
        adj = pred.get_temp_adjustment(12, 5.0)
        assert adj == 1.0

    def test_cold_temp_increases_prediction(self) -> None:
        """Cold weather with learned data → higher prediction than warm weather."""
        pred = _trained_predictor(base_kw=2.0)
        # Seed temperature→consumption correlation
        # 15°C band (baseline): 2.0 kW
        for _ in range(5):
            pred.add_temperature_sample(12, 15.0, 2.0)
        # -10°C band: 3.5 kW
        for _ in range(5):
            pred.add_temperature_sample(12, -10.0, 3.5)

        warm = pred.predict_24h(start_hour=0, weekday=0, month=1, outdoor_temp_c=15.0)
        cold = pred.predict_24h(start_hour=0, weekday=0, month=1, outdoor_temp_c=-10.0)
        # Cold predictions should be higher than warm at hour 12 (index 12)
        assert cold[12] > warm[12]

    def test_none_temp_same_as_no_temp(self) -> None:
        """outdoor_temp_c=None gives same result as not passing it."""
        pred = _trained_predictor()
        without = pred.predict_24h(start_hour=0, weekday=0, month=4)
        with_none = pred.predict_24h(start_hour=0, weekday=0, month=4, outdoor_temp_c=None)
        assert without == with_none

    def test_untrained_predictor_ignores_temp(self) -> None:
        """When not trained, temperature has no effect (returns fallback)."""
        pred = ConsumptionPredictor()
        for h in range(10):
            pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=3.0))
        result = pred.predict_24h(start_hour=0, weekday=0, month=4, outdoor_temp_c=-20.0)
        assert all(v == 2.0 for v in result)  # default fallback


class TestSeasonalFactorCalibration:
    """update_seasonal_factor auto-calibration."""

    def test_update_raises_factor_when_actual_greater(self) -> None:
        """If actual > predicted, seasonal factor for that month increases."""
        pred = ConsumptionPredictor()
        initial = pred.seasonal_factor[4]
        # Seed so predictor is trained and a real prediction exists
        for h in range(24):
            pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=2.0))
        # actual 20% higher than planned
        pred.update_seasonal_factor(month=4, actual_avg_kw=2.4, predicted_avg_kw=2.0)
        assert pred.seasonal_factor[4] > initial

    def test_update_lowers_factor_when_actual_smaller(self) -> None:
        """If actual < predicted, seasonal factor decreases."""
        pred = ConsumptionPredictor()
        initial = pred.seasonal_factor[4]
        for h in range(24):
            pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=2.0))
        # actual 20% lower than planned
        pred.update_seasonal_factor(month=4, actual_avg_kw=1.6, predicted_avg_kw=2.0)
        assert pred.seasonal_factor[4] < initial

    def test_small_error_no_update(self) -> None:
        """Ratio within 5% noise floor → seasonal factor unchanged."""
        pred = ConsumptionPredictor()
        initial = pred.seasonal_factor[4]
        # 3% error — within noise floor
        pred.update_seasonal_factor(month=4, actual_avg_kw=2.03, predicted_avg_kw=2.0)
        assert pred.seasonal_factor[4] == initial

    def test_zero_predicted_no_crash(self) -> None:
        """predicted_avg_kw=0 → no update (no crash)."""
        pred = ConsumptionPredictor()
        initial = pred.seasonal_factor[4]
        pred.update_seasonal_factor(month=4, actual_avg_kw=2.0, predicted_avg_kw=0.0)
        assert pred.seasonal_factor[4] == initial

    def test_factor_clamped_to_reasonable_range(self) -> None:
        """Even extreme ratios keep seasonal factor within [0.4, 2.5]."""
        pred = ConsumptionPredictor()
        # Many extreme updates pushing factor up
        for _ in range(200):
            pred.update_seasonal_factor(month=4, actual_avg_kw=5.0, predicted_avg_kw=1.0)
        assert pred.seasonal_factor[4] <= 2.5
        # Many extreme updates pushing factor down
        for _ in range(200):
            pred.update_seasonal_factor(month=4, actual_avg_kw=0.1, predicted_avg_kw=5.0)
        assert pred.seasonal_factor[4] >= 0.4

    def test_seasonal_factor_survives_round_trip_after_update(self) -> None:
        """Updated seasonal factor is preserved through to_dict/from_dict."""
        pred = ConsumptionPredictor()
        pred.seasonal_factor[4] = 1.25  # Direct set for test
        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert abs(restored.seasonal_factor[4] - 1.25) < 0.001


class TestMeanAbsoluteError:
    """mean_absolute_error property."""

    def test_no_feedback_returns_minus_one(self) -> None:
        """Without plan feedback, MAE = -1.0 (no data)."""
        pred = ConsumptionPredictor()
        assert pred.mean_absolute_error == -1.0

    def test_perfect_predictions_give_zero_mae(self) -> None:
        """actual == planned → MAE = 0.0."""
        pred = ConsumptionPredictor()
        for _ in range(6):
            pred.add_plan_feedback(12, 2.0, 2.0)
        mae = pred.mean_absolute_error
        assert mae == 0.0

    def test_consistent_20pct_overestimate_gives_02_mae(self) -> None:
        """Actual consistently 20% below planned → MAE ≈ 0.2."""
        pred = ConsumptionPredictor()
        for h in range(6):
            for _ in range(6):
                pred.add_plan_feedback(h, 2.0, 1.6)  # actual 80% of planned
        mae = pred.mean_absolute_error
        assert abs(mae - 0.2) < 0.05

    def test_mae_uses_all_hours(self) -> None:
        """MAE aggregates feedback from all hours, not just one."""
        pred = ConsumptionPredictor()
        for h in range(24):
            for _ in range(6):
                pred.add_plan_feedback(h, 2.0, 2.5)  # 25% over
        assert pred.mean_absolute_error > 0.0

    def test_fewer_than_5_samples_returns_minus_one(self) -> None:
        """Less than 5 total feedback samples → -1.0."""
        pred = ConsumptionPredictor()
        pred.add_plan_feedback(0, 2.0, 2.0)
        pred.add_plan_feedback(1, 2.0, 2.0)
        assert pred.mean_absolute_error == -1.0


class TestDataCoveragePct:
    """data_coverage_pct property."""

    def test_empty_predictor_zero_coverage(self) -> None:
        pred = ConsumptionPredictor()
        assert pred.data_coverage_pct == 0.0

    def test_full_week_coverage(self) -> None:
        """All 168 slots with >=3 samples → 100% coverage."""
        pred = ConsumptionPredictor()
        for wd in range(7):
            for h in range(24):
                for _ in range(3):
                    pred.add_sample(HourSample(weekday=wd, hour=h, month=4, consumption_kw=2.0))
        assert pred.data_coverage_pct == 100.0

    def test_one_day_coverage_is_roughly_14pct(self) -> None:
        """24 slots filled out of 168 → ~14% coverage."""
        pred = ConsumptionPredictor()
        for h in range(24):
            for _ in range(3):
                pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=2.0))
        # 24/168 = 14.3%
        assert 13.0 <= pred.data_coverage_pct <= 15.0

    def test_non_consumption_keys_excluded(self) -> None:
        """plan_fb, temp, appl, etc. keys don't count toward coverage."""
        pred = ConsumptionPredictor()
        # Only add non-consumption entries
        for h in range(24):
            for _ in range(5):
                pred.add_plan_feedback(h, 2.0, 2.0)
                pred.add_appliance_event("disk", 1.5, h, 0)
        assert pred.data_coverage_pct == 0.0  # no consumption slots filled
