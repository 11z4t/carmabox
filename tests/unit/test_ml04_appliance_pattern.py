"""ML-04: Appliance pattern integration in predict_24h.

Tests that predict_24h adds appliance contribution and applies
plan feedback correction factors.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)


def _trained_predictor() -> ConsumptionPredictor:
    """Return a predictor trained with 24 basic samples (one per hour)."""
    pred = ConsumptionPredictor()
    for h in range(24):
        for _ in range(3):  # 3 samples per slot for stability
            pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=2.0))
    return pred


class TestML04AppliancePattern:
    """ML-04: Appliance patterns affect predict_24h."""

    def test_no_appliance_data_returns_base_prediction(self) -> None:
        """Without appliance history, prediction = base only."""
        pred = _trained_predictor()
        result = pred.predict_24h(start_hour=0, weekday=0, month=4)
        assert len(result) == 24
        # No appliance contribution → value should be close to 2.0
        assert all(0.3 <= v <= 5.0 for v in result)

    def test_appliance_events_increase_prediction_at_target_hour(self) -> None:
        """Appliance running at h=21 → prediction at h=21 higher."""
        pred = _trained_predictor()
        baseline_21 = pred.predict_24h(start_hour=0, weekday=0, month=4)[21]

        # Add dishwasher events at h=21 (3+ needed to trigger)
        for _ in range(5):
            pred.add_appliance_event("disk", 1.8, 21, 0)

        boosted_21 = pred.predict_24h(start_hour=0, weekday=0, month=4)[21]
        assert (
            boosted_21 > baseline_21
        ), f"Prediction should be higher with appliance history: {boosted_21} vs {baseline_21}"

    def test_appliance_contribution_only_at_recorded_hour(self) -> None:
        """Appliance at h=21 should NOT boost prediction at h=10."""
        pred = _trained_predictor()
        for _ in range(5):
            pred.add_appliance_event("disk", 2.0, 21, 0)

        result = pred.predict_24h(start_hour=0, weekday=0, month=4)
        # h=10 (index 10) should be unaffected
        # h=21 (index 21) should be boosted vs h=10
        assert result[21] >= result[10]

    def test_fewer_than_3_appliance_events_no_contribution(self) -> None:
        """Less than 3 appliance events → _estimate_appliance_kw returns 0."""
        pred = _trained_predictor()
        baseline = pred.predict_24h(start_hour=0, weekday=0, month=4)[21]

        pred.add_appliance_event("disk", 2.0, 21, 0)
        pred.add_appliance_event("disk", 2.0, 21, 0)  # only 2 samples

        result_21 = pred.predict_24h(start_hour=0, weekday=0, month=4)[21]
        assert result_21 == baseline  # no change — below threshold

    def test_estimate_appliance_kw_sums_multiple_categories(self) -> None:
        """disk + tvatt both running at same hour → combined contribution."""
        pred = _trained_predictor()
        # Add 3 events each for disk and tvatt at h=22
        for _ in range(3):
            pred.add_appliance_event("disk", 1.5, 22, 1)
        for _ in range(3):
            pred.add_appliance_event("tvatt", 2.0, 22, 1)

        single_cat = pred._estimate_appliance_kw(22, 0)  # weekday=0 → no data
        two_cats = pred._estimate_appliance_kw(22, 1)  # weekday=1 → both categories
        assert two_cats > single_cat

    def test_estimate_appliance_kw_uses_recent_average(self) -> None:
        """_estimate_appliance_kw averages last 10 events, not all."""
        pred = ConsumptionPredictor()
        # Add old low-power events first
        for _ in range(5):
            pred.add_appliance_event("disk", 1.0, 10, 0)
        # Then recent high-power events
        for _ in range(5):
            pred.add_appliance_event("disk", 3.0, 10, 0)

        result = pred._estimate_appliance_kw(10, 0)
        # Average of last 10 (all) = (5*1.0 + 5*3.0) / 10 = 2.0
        assert abs(result - 2.0) < 0.01

    def test_correction_factor_applied_in_predict_24h(self) -> None:
        """Plan feedback correction factor modifies prediction."""
        pred = _trained_predictor()
        baseline = pred.predict_24h(start_hour=0, weekday=0, month=4)[8]

        # Add plan feedback suggesting we underestimate at h=8 (ratio=1.5)
        for _ in range(6):
            pred.add_plan_feedback(8, 2.0, 3.0)  # actual 50% higher than planned

        corrected = pred.predict_24h(start_hour=0, weekday=0, month=4)[8]
        # Correction factor > 1 → prediction should be higher
        assert corrected > baseline

    def test_predict_24h_returns_24_values_with_appliance_data(self) -> None:
        """predict_24h always returns exactly 24 values regardless of appliance data."""
        pred = _trained_predictor()
        for h in range(24):
            for _ in range(4):
                pred.add_appliance_event("disk", 1.5, h, 0)
        result = pred.predict_24h(start_hour=6, weekday=0, month=4)
        assert len(result) == 24

    def test_untrained_predictor_ignores_appliance_data(self) -> None:
        """When not trained, predict_24h returns fallback regardless of appliance data."""
        pred = ConsumptionPredictor()
        # Only 10 samples — not trained
        for h in range(10):
            pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=2.0))
        # Add lots of appliance events
        for _ in range(10):
            pred.add_appliance_event("disk", 5.0, 21, 0)

        result = pred.predict_24h(start_hour=0, weekday=0, month=4)
        # Should be fallback (2.0), not boosted
        assert all(v == 2.0 for v in result)
