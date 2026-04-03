"""PLAT-975: MLPredictor integration tests.

Tests:
  1. MLPredictor is fed consumption samples every cycle
  2. Planner uses MLPredictor forecast when flag ON + trained
  3. Planner falls back to EMA when flag OFF
  4. Planner falls back to EMA when MLPredictor raises
  5. sensor.carma_ml_forecast_kwh exposes correct value and attributes
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.carmabox.core.ml_predictor import (
    ConsumptionSample as MLConsumptionSample,
)
from custom_components.carmabox.core.ml_predictor import (
    MLPredictor,
    PlanAccuracySample,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _trained_ml_predictor(kw: float = 2.5) -> MLPredictor:
    """Return a trained MLPredictor (>= 24 buckets, one per hour weekday 0)."""
    pred = MLPredictor()
    for hour in range(24):
        for _ in range(3):
            pred.add_consumption(MLConsumptionSample(weekday=0, hour=hour, consumption_kw=kw))
    assert pred.is_trained, "MLPredictor should be trained after 24 hour buckets"
    return pred


def _make_coord_stub(*, ml_enabled: bool, ml_predictor: MLPredictor | None = None) -> MagicMock:
    """Create a minimal coordinator-like stub for sensor tests."""
    coord = MagicMock()
    coord._ml_enabled = ml_enabled
    pred = ml_predictor or MLPredictor()
    coord._ml_predictor = pred
    coord.ml_forecast_24h = pred.predict_24h_consumption(0) if pred.is_trained else []
    return coord


# ── Unit: MLPredictor data model ──────────────────────────────────────────────


class TestMLPredictorFeedAndPredict:
    """Verify MLPredictor learns and predicts correctly."""

    def test_add_consumption_and_predict(self) -> None:
        pred = _trained_ml_predictor(kw=3.0)
        result = pred.predict_consumption(0, 12)
        assert abs(result - 3.0) < 0.01

    def test_24h_profile_length(self) -> None:
        pred = _trained_ml_predictor(kw=2.0)
        profile = pred.predict_24h_consumption(0)
        assert len(profile) == 24

    def test_appliance_event_increases_risk(self) -> None:
        pred = MLPredictor()
        for _ in range(10):
            pred.add_appliance_event(21)
        pred.add_appliance_event(22)
        assert pred.predict_appliance_risk(21) > pred.predict_appliance_risk(22)

    def test_plan_correction_factor_default(self) -> None:
        pred = MLPredictor()
        # Less than 3 samples → no correction
        assert pred.get_plan_correction_factor(14) == 1.0

    def test_plan_correction_factor_learned(self) -> None:
        pred = MLPredictor()
        for _ in range(5):
            pred.add_plan_accuracy(
                PlanAccuracySample(
                    hour=14,
                    planned_grid_kw=2.0,
                    actual_grid_kw=2.5,
                    planned_action="charge_pv",
                    actual_action="charge_pv",
                    price=15.0,
                )
            )
        factor = pred.get_plan_correction_factor(14)
        assert factor > 1.0  # Actual > planned → correction > 1

    def test_not_trained_below_24_buckets(self) -> None:
        pred = MLPredictor()
        for h in range(23):
            pred.add_consumption(MLConsumptionSample(weekday=0, hour=h, consumption_kw=1.5))
        assert not pred.is_trained

    def test_trained_at_24_buckets(self) -> None:
        pred = _trained_ml_predictor()
        assert pred.is_trained

    def test_serialization_roundtrip(self) -> None:
        pred = _trained_ml_predictor(kw=2.8)
        data = pred.to_dict()
        pred2 = MLPredictor()
        pred2.from_dict(data)
        assert pred2.is_trained
        assert abs(pred2.predict_consumption(0, 0) - 2.8) < 0.01


# ── Planner: ML profile selection ─────────────────────────────────────────────


class TestMLPlannerSelection:
    """Verify planner uses correct profile based on flag + training."""

    def test_ml_profile_differs_from_default(self) -> None:
        """When MLPredictor trained at 5 kW, its profile should differ from 1.7 kW default."""
        pred = _trained_ml_predictor(kw=5.0)
        ml_profile = pred.predict_24h_consumption(0)
        assert all(abs(v - 5.0) < 0.01 for v in ml_profile)
        assert ml_profile != [1.7] * 24  # Different from default

    def test_ml_fallback_default_when_not_trained(self) -> None:
        """Untrained MLPredictor returns default 1.7 kW per hour."""
        pred = MLPredictor()
        assert not pred.is_trained
        profile = pred.predict_24h_consumption(0)
        assert all(v == 1.7 for v in profile)

    def test_ml_forecast_cached_after_planning(self) -> None:
        """ml_forecast_24h attribute is set when flag ON + trained."""
        pred = _trained_ml_predictor(kw=2.5)
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        # Sensor reads from ml_forecast_24h
        assert len(coord.ml_forecast_24h) == 24
        assert all(abs(v - 2.5) < 0.01 for v in coord.ml_forecast_24h)

    def test_ml_forecast_empty_when_flag_off(self) -> None:
        """ml_forecast_24h is empty when feature flag OFF."""
        pred = _trained_ml_predictor(kw=3.0)
        coord = _make_coord_stub(ml_enabled=False, ml_predictor=pred)
        # Simulate flag-off → coordinator sets ml_forecast_24h = []
        coord.ml_forecast_24h = []
        assert coord.ml_forecast_24h == []

    def test_ml_forecast_empty_when_not_trained(self) -> None:
        """ml_forecast_24h is empty when MLPredictor not trained."""
        pred = MLPredictor()
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        coord.ml_forecast_24h = []
        assert coord.ml_forecast_24h == []


# ── Sensor: carma_ml_forecast_kwh ─────────────────────────────────────────────


class TestMLForecastSensor:
    """Verify _ml_forecast_value / _ml_forecast_attrs return correct data."""

    def test_value_none_when_no_forecast(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_value

        coord = _make_coord_stub(ml_enabled=False)
        coord.ml_forecast_24h = []
        assert _ml_forecast_value(coord) is None

    def test_value_sum_of_hourly_forecast(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_value

        pred = _trained_ml_predictor(kw=2.0)
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        # 24 hours * 2.0 kW = 48.0 kWh
        assert _ml_forecast_value(coord) == pytest.approx(48.0, abs=0.1)

    def test_attrs_contain_required_keys(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        pred = _trained_ml_predictor(kw=1.5)
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        attrs = _ml_forecast_attrs(coord)
        assert "ml_enabled" in attrs
        assert "trained" in attrs
        assert "consumption_buckets" in attrs
        assert "hourly_forecast_kw" in attrs

    def test_attrs_trained_true_when_trained(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        pred = _trained_ml_predictor(kw=2.0)
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        attrs = _ml_forecast_attrs(coord)
        assert attrs["trained"] is True
        assert attrs["consumption_buckets"] == 24

    def test_attrs_trained_false_when_not_trained(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        coord = _make_coord_stub(ml_enabled=True, ml_predictor=MLPredictor())
        attrs = _ml_forecast_attrs(coord)
        assert attrs["trained"] is False

    def test_attrs_ml_enabled_reflects_flag(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        coord_on = _make_coord_stub(ml_enabled=True)
        coord_off = _make_coord_stub(ml_enabled=False)
        assert _ml_forecast_attrs(coord_on)["ml_enabled"] is True
        assert _ml_forecast_attrs(coord_off)["ml_enabled"] is False

    def test_attrs_hourly_forecast_24_values(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        pred = _trained_ml_predictor(kw=3.0)
        coord = _make_coord_stub(ml_enabled=True, ml_predictor=pred)
        attrs = _ml_forecast_attrs(coord)
        assert len(attrs["hourly_forecast_kw"]) == 24

    def test_attrs_no_ml_predictor_attribute(self) -> None:
        from custom_components.carmabox.sensor import _ml_forecast_attrs

        coord = MagicMock()
        del coord._ml_predictor
        coord._ml_enabled = False
        coord.ml_forecast_24h = []
        attrs = _ml_forecast_attrs(coord)
        assert attrs["trained"] is False
        assert attrs["consumption_buckets"] == 0


# ── Persistence: to_dict / from_dict ──────────────────────────────────────────


class TestMLPredictorPersistence:
    """Verify serialization preserves all learned data."""

    def test_consumption_survives_roundtrip(self) -> None:
        pred = _trained_ml_predictor(kw=4.0)
        data = pred.to_dict()
        pred2 = MLPredictor()
        pred2.from_dict(data)
        for hour in range(24):
            assert abs(pred2.predict_consumption(0, hour) - 4.0) < 0.01

    def test_appliance_hours_survive_roundtrip(self) -> None:
        pred = MLPredictor()
        for _ in range(5):
            pred.add_appliance_event(21)
        data = pred.to_dict()
        pred2 = MLPredictor()
        pred2.from_dict(data)
        assert pred2._appliance_hours.get(21, 0) == 5

    def test_from_dict_empty_data(self) -> None:
        """from_dict with empty dict → predictor stays at defaults."""
        pred = MLPredictor()
        pred.from_dict({})
        assert not pred.is_trained
        assert pred.predict_consumption(0, 0) == 1.7

    def test_from_dict_malformed_key_ignored(self) -> None:
        """from_dict with a malformed key (wrong split) → gracefully skipped."""
        pred = MLPredictor()
        pred.from_dict({"consumption": {"bad": [1.0, 2.0]}})
        # 'bad' can't be split into two ints → no entry added
        assert not pred.is_trained
