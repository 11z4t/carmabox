"""ML-02: Predictor persistent store — save/load round-trip.

Verifies that ConsumptionPredictor state survives serialization
(to_dict / from_dict) which maps to HA .storage save/load.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)


def _trained_predictor() -> ConsumptionPredictor:
    pred = ConsumptionPredictor()
    for h in range(24):
        pred.add_sample(HourSample(weekday=0, hour=h, month=4, consumption_kw=3.5))
    return pred


class TestML02PredictorStore:
    """ML-02: Predictor to_dict/from_dict round-trip (mirrors HA store)."""

    def test_empty_predictor_round_trips(self) -> None:
        """Fresh predictor serializes and deserializes cleanly."""
        pred = ConsumptionPredictor()
        data = pred.to_dict()
        restored = ConsumptionPredictor.from_dict(data)
        assert restored.total_samples == 0
        assert not restored.is_trained
        assert restored.history == {}

    def test_sample_count_survives_round_trip(self) -> None:
        """total_samples is preserved across save/load."""
        pred = _trained_predictor()
        assert pred.total_samples == 24
        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert restored.total_samples == 24

    def test_is_trained_survives_round_trip(self) -> None:
        """is_trained=True after 24 samples, still True after restore."""
        pred = _trained_predictor()
        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert restored.is_trained

    def test_history_data_survives_round_trip(self) -> None:
        """History dict is fully preserved."""
        pred = _trained_predictor()
        key = "0_12"
        original_samples = list(pred.history[key])

        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert key in restored.history
        assert restored.history[key] == original_samples

    def test_predictions_identical_after_restore(self) -> None:
        """predict_24h gives same values before and after round-trip."""
        pred = _trained_predictor()
        before = pred.predict_24h(start_hour=0, weekday=0, month=4)
        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        after = restored.predict_24h(start_hour=0, weekday=0, month=4)
        assert before == after

    def test_seasonal_factor_survives_round_trip(self) -> None:
        """Custom seasonal factors are preserved."""
        pred = ConsumptionPredictor()
        pred.seasonal_factor[1] = 2.0  # Modify January factor
        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert restored.seasonal_factor[1] == 2.0

    def test_corrupt_store_data_returns_fresh_predictor(self) -> None:
        """from_dict handles missing/corrupt keys gracefully."""
        restored = ConsumptionPredictor.from_dict({})
        assert restored.total_samples == 0
        assert not restored.is_trained

    def test_store_with_wrong_types_handled(self) -> None:
        """from_dict handles non-dict history gracefully."""
        data = {"history": "corrupted", "total_samples": 99}
        restored = ConsumptionPredictor.from_dict(data)
        # history was invalid → stays as provided (not crashed)
        # total_samples still restored
        assert restored.total_samples == 99

    def test_partial_restart_preserves_training_state(self) -> None:
        """Simulate 23-sample partial training: not trained before/after."""
        pred = ConsumptionPredictor()
        for h in range(23):
            pred.add_sample(HourSample(weekday=1, hour=h, month=4, consumption_kw=2.0))
        assert not pred.is_trained

        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        assert not restored.is_trained
        assert restored.total_samples == 23

        # One more sample → trained
        restored.add_sample(HourSample(weekday=1, hour=23, month=4, consumption_kw=2.0))
        assert restored.is_trained

    def test_appliance_history_survives_round_trip(self) -> None:
        """add_appliance_event data is preserved in store."""
        pred = ConsumptionPredictor()
        for _ in range(5):
            pred.add_appliance_event("disk", 1.8, 21, 0)

        restored = ConsumptionPredictor.from_dict(pred.to_dict())
        key = "appl_disk_0_21"
        assert key in restored.history
        assert len(restored.history[key]) == 5

    def test_store_key_consistency(self) -> None:
        """to_dict always produces same keys regardless of training state."""
        empty = ConsumptionPredictor()
        trained = _trained_predictor()
        for d in [empty.to_dict(), trained.to_dict()]:
            assert "history" in d
            assert "total_samples" in d
            assert "seasonal_factor" in d
