"""ML-03: Predictor training threshold — 24 samples (1 day) is enough."""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import (
    MIN_TRAINING_SAMPLES,
    TRAINING_THRESHOLD_SAMPLES,
    ConsumptionPredictor,
    HourSample,
)


def _add_n_samples(pred: ConsumptionPredictor, n: int) -> None:
    """Add n distinct hourly samples (cycling through 24 hours)."""
    for i in range(n):
        pred.add_sample(
            HourSample(weekday=0, hour=i % 24, month=6, consumption_kw=2.0 + i * 0.01)
        )


class TestTrainingThreshold:
    """ML-03 AC: is_trained = True after exactly 24 samples."""

    def test_is_trained_after_24_samples(self) -> None:
        pred = ConsumptionPredictor()
        _add_n_samples(pred, 24)
        assert pred.is_trained is True

    def test_not_trained_at_23_samples(self) -> None:
        pred = ConsumptionPredictor()
        _add_n_samples(pred, 23)
        assert pred.is_trained is False

    def test_training_threshold_constant_is_24(self) -> None:
        """ML-03 AC: TRAINING_THRESHOLD_SAMPLES named constant equals 24."""
        assert TRAINING_THRESHOLD_SAMPLES == 24

    def test_min_training_samples_equals_threshold(self) -> None:
        """TRAINING_THRESHOLD_SAMPLES and MIN_TRAINING_SAMPLES are consistent."""
        assert MIN_TRAINING_SAMPLES == TRAINING_THRESHOLD_SAMPLES

    def test_not_trained_with_zero_samples(self) -> None:
        pred = ConsumptionPredictor()
        assert pred.is_trained is False

    def test_trained_with_more_than_24(self) -> None:
        pred = ConsumptionPredictor()
        _add_n_samples(pred, 50)
        assert pred.is_trained is True

    def test_accuracy_estimate_grows_with_samples(self) -> None:
        """accuracy_estimate increases once slots have ≥3 samples each."""
        pred = ConsumptionPredictor()
        acc_before = pred.accuracy_estimate
        # accuracy_estimate counts slots with >= 3 samples — add 3 samples per hour
        for _ in range(3):
            _add_n_samples(pred, 24)
        assert pred.accuracy_estimate > acc_before
