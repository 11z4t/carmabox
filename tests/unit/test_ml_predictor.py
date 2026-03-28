"""Tests for ML Predictor."""

from __future__ import annotations

from custom_components.carmabox.core.ml_predictor import (
    ConsumptionSample,
    MLPredictor,
    PlanAccuracySample,
)


class TestConsumptionPrediction:
    def test_default_consumption(self):
        p = MLPredictor()
        assert p.predict_consumption(0, 14) == 1.7

    def test_learned_consumption(self):
        p = MLPredictor()
        for _ in range(5):
            p.add_consumption(ConsumptionSample(weekday=0, hour=14, consumption_kw=2.5))
        assert abs(p.predict_consumption(0, 14) - 2.5) < 0.01

    def test_24h_profile(self):
        p = MLPredictor()
        profile = p.predict_24h_consumption(0)
        assert len(profile) == 24
        assert all(v == 1.7 for v in profile)  # Default

    def test_samples_capped(self):
        p = MLPredictor()
        for i in range(50):
            p.add_consumption(ConsumptionSample(weekday=0, hour=0, consumption_kw=float(i)))
        assert len(p._consumption[(0, 0)]) == 30


class TestApplianceRisk:
    def test_default_risk(self):
        p = MLPredictor()
        assert p.predict_appliance_risk(21) == 0.1

    def test_learned_risk(self):
        p = MLPredictor()
        for _ in range(10):
            p.add_appliance_event(21)
        p.add_appliance_event(22)
        risk_21 = p.predict_appliance_risk(21)
        risk_22 = p.predict_appliance_risk(22)
        assert risk_21 > risk_22


class TestPlanAccuracy:
    def test_default_correction(self):
        p = MLPredictor()
        assert p.get_plan_correction_factor(14) == 1.0

    def test_learned_correction(self):
        p = MLPredictor()
        for _ in range(5):
            p.add_plan_accuracy(PlanAccuracySample(
                hour=14, planned_grid_kw=1.0, actual_grid_kw=1.5,
                planned_action="i", actual_action="i", price=50,
            ))
        factor = p.get_plan_correction_factor(14)
        assert factor > 1.0  # Actual > planned → correction > 1


class TestPressurePV:
    def test_default_correction(self):
        p = MLPredictor()
        assert p.predict_pv_correction(1010) == 1.0

    def test_high_pressure_correction(self):
        p = MLPredictor()
        for _ in range(5):
            p.add_pressure_pv(1020, 1.2)  # High pressure → PV better
        assert p.predict_pv_correction(1020) > 1.0

    def test_low_pressure_correction(self):
        p = MLPredictor()
        for _ in range(5):
            p.add_pressure_pv(1000, 0.6)  # Low pressure → PV worse
        assert p.predict_pv_correction(1000) < 1.0


class TestDecisionOutcomes:
    def test_effective_decisions(self):
        p = MLPredictor()
        p.add_decision_outcome("discharge_2kw", {}, "ok", True)
        p.add_decision_outcome("discharge_2kw", {}, "ok", True)
        p.add_decision_outcome("discharge_2kw", {}, "breach", False)
        eff = p.get_effective_decisions()
        assert 0.6 < eff["discharge_2kw"] < 0.7  # 2/3


class TestSerialization:
    def test_round_trip(self):
        p = MLPredictor()
        p.add_consumption(ConsumptionSample(0, 14, 2.5))
        p.add_appliance_event(21)
        data = p.to_dict()
        p2 = MLPredictor()
        p2.from_dict(data)
        assert abs(p2.predict_consumption(0, 14) - 2.5) < 0.01

    def test_is_trained(self):
        p = MLPredictor()
        assert not p.is_trained
        for h in range(24):
            p.add_consumption(ConsumptionSample(0, h, 1.5))
        assert p.is_trained
