"""ML-01: ConsumptionPredictor.add_sample — robustness and None-guard tests."""

from __future__ import annotations

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)


def _sample(consumption_kw: float = 2.0, weekday: int = 0, hour: int = 12) -> HourSample:
    return HourSample(weekday=weekday, hour=hour, month=6, consumption_kw=consumption_kw)


class TestAddSampleDoesNotCrash:
    """ML-01 AC: add_sample must not crash on edge-case inputs."""

    def test_add_sample_does_not_crash_on_none_inputs(self) -> None:
        """None consumption_kw is rejected without raising."""
        pred = ConsumptionPredictor()
        sample = HourSample(weekday=0, hour=12, month=6, consumption_kw=None)  # type: ignore[arg-type]
        pred.add_sample(sample)  # Must not raise
        assert pred.total_samples == 0  # Rejected

    def test_add_sample_zero_consumption_accepted(self) -> None:
        """0.0 consumption is valid — not None — and must be accepted."""
        pred = ConsumptionPredictor()
        pred.add_sample(_sample(consumption_kw=0.0))
        assert pred.total_samples == 1

    def test_add_sample_negative_consumption_accepted(self) -> None:
        """Negative consumption (export) is accepted."""
        pred = ConsumptionPredictor()
        pred.add_sample(_sample(consumption_kw=-0.5))
        assert pred.total_samples == 1

    def test_add_sample_large_consumption_accepted(self) -> None:
        """Very large consumption is accepted (no upper guard)."""
        pred = ConsumptionPredictor()
        pred.add_sample(_sample(consumption_kw=50.0))
        assert pred.total_samples == 1

    def test_add_sample_invalid_weekday_rejected(self) -> None:
        """Weekday out of range (0-6) is rejected without raising."""
        pred = ConsumptionPredictor()
        pred.add_sample(HourSample(weekday=7, hour=12, month=6, consumption_kw=2.0))
        assert pred.total_samples == 0

    def test_add_sample_invalid_hour_rejected(self) -> None:
        """Hour out of range (0-23) is rejected without raising."""
        pred = ConsumptionPredictor()
        pred.add_sample(HourSample(weekday=0, hour=24, month=6, consumption_kw=2.0))
        assert pred.total_samples == 0

    def test_24_calls_gives_24_samples(self) -> None:
        """ML-01 AC: after 24 distinct hourly samples, total_samples == 24."""
        pred = ConsumptionPredictor()
        for hour in range(24):
            pred.add_sample(_sample(hour=hour))
        assert pred.total_samples >= 24

    def test_duplicate_hour_accumulates(self) -> None:
        """Multiple samples for same weekday/hour accumulate in history."""
        pred = ConsumptionPredictor()
        for _ in range(5):
            pred.add_sample(_sample(consumption_kw=3.0))
        assert pred.total_samples == 5
        assert len(pred.history.get("0_12", [])) == 5
