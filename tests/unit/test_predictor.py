"""Tests for ConsumptionPredictor."""

from __future__ import annotations

import pytest

from custom_components.carmabox.optimizer.predictor import (
    MIN_TRAINING_SAMPLES,
    ConsumptionPredictor,
    HourSample,
)


def _make_trained_predictor(consumption_kw: float = 2.0) -> ConsumptionPredictor:
    """Create a predictor with enough samples to be trained."""
    p = ConsumptionPredictor()
    for wd in range(7):
        for h in range(24):
            for _ in range(2):  # 2x = 336 samples > 24 minimum
                p.add_sample(HourSample(weekday=wd, hour=h, month=9, consumption_kw=consumption_kw))
    return p


class TestHourSample:
    def test_basic_creation(self) -> None:
        s = HourSample(weekday=0, hour=8, month=3, consumption_kw=2.5, temperature_c=10.0)
        assert s.weekday == 0
        assert s.hour == 8
        assert s.month == 3
        assert s.consumption_kw == 2.5
        assert s.temperature_c == 10.0

    def test_temperature_optional(self) -> None:
        s = HourSample(weekday=1, hour=12, month=6, consumption_kw=1.5)
        assert s.temperature_c is None


class TestConsumptionPredictorInit:
    def test_initial_state(self) -> None:
        p = ConsumptionPredictor()
        assert p.total_samples == 0
        assert p.history == {}
        assert p.is_trained is False

    def test_min_training_samples_constant(self) -> None:
        assert MIN_TRAINING_SAMPLES == 24  # 1 day x 24 hours


class TestAddSample:
    def test_add_increments_total(self) -> None:
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=0, hour=8, month=3, consumption_kw=2.0))
        assert p.total_samples == 1

    def test_add_creates_correct_key(self) -> None:
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=3, hour=14, month=5, consumption_kw=2.0))
        assert "3_14" in p.history

    def test_add_stores_value(self) -> None:
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=0, hour=0, month=1, consumption_kw=3.5))
        assert p.history["0_0"] == [3.5]

    def test_add_multiple_same_slot(self) -> None:
        p = ConsumptionPredictor()
        for v in [1.0, 2.0, 3.0]:
            p.add_sample(HourSample(weekday=0, hour=0, month=1, consumption_kw=v))
        assert len(p.history["0_0"]) == 3
        assert p.total_samples == 3

    def test_slot_capped_at_30(self) -> None:
        p = ConsumptionPredictor()
        for i in range(35):
            p.add_sample(HourSample(weekday=0, hour=0, month=1, consumption_kw=float(i)))
        assert len(p.history["0_0"]) == 30

    def test_slot_keeps_latest_30(self) -> None:
        p = ConsumptionPredictor()
        for i in range(35):
            p.add_sample(HourSample(weekday=0, hour=0, month=1, consumption_kw=float(i)))
        # Should keep the last 30 (values 5..34)
        assert p.history["0_0"][0] == 5.0
        assert p.history["0_0"][-1] == 34.0


class TestIsTrainedAndAccuracy:
    def test_not_trained_below_threshold(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(MIN_TRAINING_SAMPLES - 1):
            p.add_sample(HourSample(weekday=0, hour=0, month=1, consumption_kw=2.0))
        assert p.is_trained is False

    def test_trained_at_threshold(self) -> None:
        p = ConsumptionPredictor()
        for i in range(MIN_TRAINING_SAMPLES):
            wd = i % 7
            h = (i // 7) % 24
            p.add_sample(HourSample(weekday=wd, hour=h, month=1, consumption_kw=2.0))
        assert p.is_trained is True

    def test_accuracy_empty(self) -> None:
        p = ConsumptionPredictor()
        assert p.accuracy_estimate == 0.0

    def test_accuracy_increases_with_data(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(3):
            p.add_sample(HourSample(weekday=0, hour=0, month=3, consumption_kw=2.0))
        assert p.accuracy_estimate > 0.0

    def test_accuracy_max_100_slots(self) -> None:
        """168 slots (7x24), each needs ≥3 samples for accuracy."""
        p = _make_trained_predictor()
        # We added 2 samples per slot, so accuracy < 100%
        # All slots filled but with only 2 samples each (< 3 required)
        assert 0.0 <= p.accuracy_estimate <= 100.0


class TestPredictHour:
    def test_returns_fallback_when_not_trained(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_hour(0, 8, 3, fallback_kw=2.5)
        assert result == 2.5

    def test_uses_default_fallback_when_not_trained(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_hour(0, 8, 3)
        assert result == 2.0  # default fallback

    def test_returns_prediction_when_trained(self) -> None:
        p = _make_trained_predictor(consumption_kw=3.0)
        result = p.predict_hour(0, 8, 9)
        assert result > 0

    def test_minimum_prediction_is_0_3(self) -> None:
        p = _make_trained_predictor(consumption_kw=0.01)
        result = p.predict_hour(0, 8, 9)
        assert result >= 0.3

    def test_seasonal_winter_higher_than_summer(self) -> None:
        p = _make_trained_predictor(consumption_kw=2.0)
        winter = p.predict_hour(0, 12, 1)  # January
        summer = p.predict_hour(0, 12, 7)  # July
        assert winter > summer

    def test_uses_adjacent_weekday_if_slot_empty(self) -> None:
        p = _make_trained_predictor(consumption_kw=2.0)
        # Remove data for weekday 2, hour 10
        del p.history["2_10"]
        # Should still return a prediction using adjacent weekday
        result = p.predict_hour(2, 10, 9)
        assert result > 0

    def test_fallback_when_all_adjacent_slots_empty(self) -> None:
        p = _make_trained_predictor(consumption_kw=2.0)
        # Remove all slots for hours 10 across all weekdays
        for wd in range(7):
            p.history.pop(f"{wd}_10", None)
        result = p.predict_hour(3, 10, 9, fallback_kw=5.0)
        assert result == 5.0


class TestPredict24h:
    def test_returns_fallback_list_when_not_trained(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_24h(0, 0, 3)
        assert result == [2.0] * 24

    def test_uses_provided_fallback_profile(self) -> None:
        p = ConsumptionPredictor()
        profile = list(range(24))  # [0, 1, 2, ..., 23]
        result = p.predict_24h(6, 0, 3, fallback_profile=profile)
        assert len(result) == 24
        assert result[0] == 6  # starts at hour 6
        assert result[-1] == 5  # wraps around

    def test_short_fallback_profile_uses_default(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_24h(0, 0, 3, fallback_profile=[1.0, 2.0])  # too short
        assert result == [2.0] * 24


class TestApplianceLearning:
    def test_add_appliance_event(self) -> None:
        p = ConsumptionPredictor()
        p.add_appliance_event("disk", 1.0, 22, 0)
        assert "appl_disk_0_22" in p.history
        assert p.history["appl_disk_0_22"] == [1.0]

    def test_appliance_event_capped_at_30(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(35):
            p.add_appliance_event("disk", 1.0, 22, 0)
        assert len(p.history["appl_disk_0_22"]) == 30

    def test_predict_appliance_risk_returns_float(self) -> None:
        p = ConsumptionPredictor()
        risk = p.predict_appliance_risk(22, 0)
        assert isinstance(risk, float)
        assert risk == 0.0

    def test_predict_appliance_risk_values_bounded(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(10):
            p.add_appliance_event("disk", 1.0, 22, 0)
        risk = p.predict_appliance_risk(22, 0)
        assert 0.0 <= risk <= 1.0

    def test_predict_appliance_risk_nonzero_after_events(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(10):
            p.add_appliance_event("disk", 1.0, 22, 0)
        risk = p.predict_appliance_risk(22, 0)
        assert risk > 0

    def test_get_disk_typical_hours_fallback_when_no_data(self) -> None:
        p = ConsumptionPredictor()
        hours = p.get_disk_typical_hours()
        assert isinstance(hours, list)

    def test_get_disk_typical_hours_with_events(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(15):
            p.add_appliance_event("disk", 1.0, 22, 0)
        hours = p.get_disk_typical_hours()
        assert isinstance(hours, list)


class TestBreachLearning:
    def test_add_breach_event(self) -> None:
        p = ConsumptionPredictor()
        p.add_breach_event(8, 0, 1.5)
        assert len(p.history) > 0

    def test_get_breach_risk_hours_empty(self) -> None:
        p = ConsumptionPredictor()
        assert p.get_breach_risk_hours(0) == []

    def test_get_breach_risk_hours_with_events(self) -> None:
        p = ConsumptionPredictor()
        p.add_breach_event(8, 0, 1.5)
        p.add_breach_event(8, 0, 2.0)
        result = p.get_breach_risk_hours(0)
        assert isinstance(result, list)


class TestPlanFeedback:
    def test_add_plan_feedback(self) -> None:
        p = ConsumptionPredictor()
        p.add_plan_feedback(8, 2.0, 2.5)
        assert len(p.history) > 0

    def test_get_correction_factor_insufficient_data(self) -> None:
        p = ConsumptionPredictor()
        assert p.get_correction_factor(8) == 1.0

    def test_get_correction_factor_with_samples(self) -> None:
        p = ConsumptionPredictor()
        for _ in range(10):
            p.add_plan_feedback(8, 2.0, 3.0)
        factor = p.get_correction_factor(8)
        assert isinstance(factor, float)


class TestTemperatureLearning:
    def test_add_temperature_sample(self) -> None:
        p = ConsumptionPredictor()
        p.add_temperature_sample(8, 10.0, 3.0)
        assert len(p.history) > 0

    def test_get_temp_adjustment_insufficient_data(self) -> None:
        p = ConsumptionPredictor()
        result = p.get_temp_adjustment(8, 10.0)
        assert isinstance(result, float)


class TestEvLearning:
    def test_add_ev_usage(self) -> None:
        p = ConsumptionPredictor()
        p.add_ev_usage(1, 15.0, 92.0)
        assert len(p.history) > 0

    def test_predict_ev_usage_default(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_ev_usage(1)
        assert isinstance(result, float)
        assert result >= 0


class TestBatteryEconomics:
    def test_add_battery_cycle(self) -> None:
        p = ConsumptionPredictor()
        p.add_battery_cycle(8, 0, 5.0, 4.5, 50.0)
        assert len(p.history) > 0

    def test_get_battery_economics_no_data(self) -> None:
        p = ConsumptionPredictor()
        econ = p.get_battery_economics()
        assert isinstance(econ, dict)

    def test_should_cycle_battery(self) -> None:
        p = ConsumptionPredictor()
        result = p.should_cycle_battery(8, 0)
        assert isinstance(result, bool)

    def test_add_idle_penalty(self) -> None:
        p = ConsumptionPredictor()
        p.add_idle_penalty(8, 0, 30, 50.0)
        assert len(p.history) > 0


class TestSerialization:
    def test_to_dict_keys(self) -> None:
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=0, hour=0, month=3, consumption_kw=2.0))
        d = p.to_dict()
        assert "history" in d
        assert "total_samples" in d
        assert "seasonal_factor" in d

    def test_to_dict_values(self) -> None:
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=0, hour=0, month=3, consumption_kw=2.0))
        d = p.to_dict()
        assert d["total_samples"] == 1

    def test_from_dict_empty(self) -> None:
        p = ConsumptionPredictor.from_dict({})
        assert p.total_samples == 0
        assert p.history == {}

    def test_roundtrip(self) -> None:
        p = _make_trained_predictor(2.0)
        d = p.to_dict()
        p2 = ConsumptionPredictor.from_dict(d)
        assert p2.total_samples == p.total_samples
        assert p2.history == p.history

    def test_from_dict_restores_seasonal_factors(self) -> None:
        p = ConsumptionPredictor()
        p.seasonal_factor[1] = 1.9  # modify
        d = p.to_dict()
        p2 = ConsumptionPredictor.from_dict(d)
        assert p2.seasonal_factor[1] == pytest.approx(1.9)

    def test_from_dict_seasonal_factor_keys_are_ints(self) -> None:
        p = ConsumptionPredictor()
        d = p.to_dict()
        p2 = ConsumptionPredictor.from_dict(d)
        for k in p2.seasonal_factor:
            assert isinstance(k, int)
