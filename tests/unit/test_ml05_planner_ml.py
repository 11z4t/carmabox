"""ML-05: Planner uses ML prediction when predictor is trained."""

from __future__ import annotations

from custom_components.carmabox.optimizer.planner import generate_plan
from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)


def _trained_predictor(base_kw: float = 3.0) -> ConsumptionPredictor:
    """Return a trained predictor (24 samples at uniform base_kw)."""
    pred = ConsumptionPredictor()
    for hour in range(24):
        pred.add_sample(HourSample(weekday=1, hour=hour, month=6, consumption_kw=base_kw))
    assert pred.is_trained
    return pred


class TestPlanUsesML:
    """ML-05 AC: generate_plan is called with ML profile when trained."""

    def test_plan_uses_ml_when_trained(self) -> None:
        """Trained predictor produces a different (ML) consumption profile than static fallback."""
        pred = _trained_predictor(base_kw=4.0)  # High consumption
        static_low = [1.0] * 24  # Low static profile

        # ML profile from predictor (should be ~4.0 kW per hour)
        ml_profile = pred.predict_24h(
            start_hour=0,
            weekday=1,
            month=6,
            fallback_profile=static_low,
        )
        # Confirm ML profile is higher than static fallback
        assert sum(ml_profile) > sum(static_low), (
            "ML profile should reflect trained 4kW, not static 1kW"
        )

    def test_plan_uses_static_when_not_trained(self) -> None:
        """Untrained predictor falls back to static profile."""
        pred = ConsumptionPredictor()
        assert not pred.is_trained

        fallback = [2.0] * 24
        result = pred.predict_24h(
            start_hour=0,
            weekday=1,
            month=6,
            fallback_profile=fallback,
        )
        # Untrained → returns fallback
        assert result == fallback

    def test_generate_plan_with_ml_profile_charges_on_surplus(self) -> None:
        """With high PV (5kW) and low ML consumption (2kW): action=charge at that hour."""
        pv = [5.0] + [0.0] * 23  # Surplus only at hour 0
        loads = [2.0] * 24       # Static 2kW consumption

        plan = generate_plan(
            num_hours=24,
            start_hour=0,
            target_weighted_kw=3.0,
            hourly_loads=loads,
            hourly_pv=pv,
            hourly_prices=[50.0] * 24,
            hourly_ev=[0.0] * 24,
            battery_soc=50.0,
            ev_soc=-1.0,
            battery_cap_kwh=20.0,
        )
        # Hour 0: pv=5, load=2, net=2-5=-3 → charge
        assert plan[0].action == "c"
