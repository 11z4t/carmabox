"""Tests for PLAT-1231: predict_24h_with_uncertainty() on ConsumptionPredictor."""

from __future__ import annotations

import random

import pytest

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourlyPrediction,
    HourSample,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_trained(consumption_kw: float = 2.0) -> ConsumptionPredictor:
    """Create a predictor with full week coverage (336 samples)."""
    p = ConsumptionPredictor()
    for wd in range(7):
        for h in range(24):
            for _ in range(2):
                p.add_sample(HourSample(weekday=wd, hour=h, month=9, consumption_kw=consumption_kw))
    return p


def _make_rich(consumption_kw: float = 2.0) -> ConsumptionPredictor:
    """Create a predictor with 10 samples per slot (enough for bootstrap)."""
    p = ConsumptionPredictor()
    for wd in range(7):
        for h in range(24):
            for _ in range(10):
                p.add_sample(HourSample(weekday=wd, hour=h, month=9, consumption_kw=consumption_kw))
    return p


_RNG = random.Random(42)  # deterministic


# ── HourlyPrediction dataclass ───────────────────────────────────────────────


class TestHourlyPrediction:
    def test_fields_exist(self) -> None:
        hp = HourlyPrediction(hour=8, p10=1.5, p50=2.0, p90=2.5)
        assert hp.hour == 8
        assert hp.p10 == 1.5
        assert hp.p50 == 2.0
        assert hp.p90 == 2.5

    def test_p10_le_p50_le_p90(self) -> None:
        hp = HourlyPrediction(hour=12, p10=1.0, p50=2.0, p90=3.0)
        assert hp.p10 <= hp.p50 <= hp.p90


# ── predict_24h_with_uncertainty: basic contract ─────────────────────────────


class TestPredict24hWithUncertaintyBasic:
    def test_returns_24_items(self) -> None:
        p = _make_trained()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        assert len(result) == 24

    def test_items_are_hourly_prediction(self) -> None:
        p = _make_trained()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        for hp in result:
            assert isinstance(hp, HourlyPrediction)

    def test_hour_field_wraps_correctly(self) -> None:
        p = _make_trained()
        result = p.predict_24h_with_uncertainty(20, 0, 9, rng=_RNG)
        expected_hours = [(20 + i) % 24 for i in range(24)]
        assert [hp.hour for hp in result] == expected_hours

    def test_p10_le_p50_le_p90_all_hours(self) -> None:
        p = _make_rich()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        for hp in result:
            assert hp.p10 <= hp.p50 <= hp.p90, f"hour {hp.hour}: {hp.p10} {hp.p50} {hp.p90}"

    def test_all_values_positive(self) -> None:
        p = _make_rich()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        for hp in result:
            assert hp.p10 > 0.0
            assert hp.p50 > 0.0
            assert hp.p90 > 0.0

    def test_backward_compat_predict_24h_unchanged(self) -> None:
        """predict_24h() still works after adding predict_24h_with_uncertainty()."""
        p = _make_trained()
        result_old = p.predict_24h(0, 0, 9)
        assert len(result_old) == 24
        assert all(isinstance(v, float) for v in result_old)


# ── Fallback path (untrained predictor) ──────────────────────────────────────


class TestUncertaintyUntrained:
    def test_untrained_returns_24_items(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        assert len(result) == 24

    def test_untrained_uses_default_fallback(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        for hp in result:
            assert hp.p50 == pytest.approx(2.0, abs=0.01)

    def test_untrained_with_fallback_profile(self) -> None:
        p = ConsumptionPredictor()
        profile = [3.0] * 24
        result = p.predict_24h_with_uncertainty(0, 0, 9, fallback_profile=profile, rng=_RNG)
        assert result[0].p50 == pytest.approx(3.0, abs=0.01)

    def test_untrained_spread_gives_nonzero_width(self) -> None:
        p = ConsumptionPredictor()
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        # p90 should be > p10 due to fallback spread
        for hp in result:
            assert hp.p90 > hp.p10


# ── Bootstrap path (rich samples) ────────────────────────────────────────────


class TestUncertaintyBootstrap:
    def test_bootstrap_p50_close_to_point_estimate(self) -> None:
        p = _make_rich(consumption_kw=3.0)
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=_RNG)
        # p50 should be near the point prediction from predict_24h
        point_result = p.predict_24h(0, 0, 9)
        for i, hp in enumerate(result):
            assert (
                abs(hp.p50 - point_result[i]) < 0.5
            ), f"hour {hp.hour}: p50={hp.p50} vs point={point_result[i]}"

    def test_bootstrap_non_deterministic_without_rng(self) -> None:
        """Two calls with no rng may differ (randomness is present)."""
        p = _make_rich()
        r1 = p.predict_24h_with_uncertainty(0, 0, 9)
        r2 = p.predict_24h_with_uncertainty(0, 0, 9)
        # At least one value may differ — just verify they are valid
        assert len(r1) == 24
        assert len(r2) == 24

    def test_bootstrap_deterministic_with_seeded_rng(self) -> None:
        p = _make_rich()
        rng1 = random.Random(99)
        rng2 = random.Random(99)
        r1 = p.predict_24h_with_uncertainty(0, 0, 9, rng=rng1)
        r2 = p.predict_24h_with_uncertainty(0, 0, 9, rng=rng2)
        for a, b in zip(r1, r2, strict=False):
            assert a.p10 == b.p10
            assert a.p50 == b.p50
            assert a.p90 == b.p90

    def test_high_variance_data_gives_wide_interval(self) -> None:
        """When samples vary a lot, P90-P10 spread should be large."""
        p = ConsumptionPredictor()
        for wd in range(7):
            for h in range(24):
                for v in [0.5, 0.5, 0.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5]:
                    p.add_sample(HourSample(weekday=wd, hour=h, month=9, consumption_kw=v))
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=random.Random(7))
        spreads = [hp.p90 - hp.p10 for hp in result]
        assert max(spreads) > 0.5

    def test_uniform_data_gives_narrow_interval(self) -> None:
        """When all samples are identical, P10/P50/P90 should be very close."""
        p = _make_rich(consumption_kw=2.0)
        result = p.predict_24h_with_uncertainty(0, 0, 9, rng=random.Random(7))
        for hp in result:
            assert hp.p90 - hp.p10 < 0.05, f"hour {hp.hour}: spread={hp.p90 - hp.p10}"


# ── Seasonal and temperature adjustments ─────────────────────────────────────


class TestUncertaintyAdjustments:
    def test_winter_p50_higher_than_summer(self) -> None:
        p = _make_rich(consumption_kw=2.0)
        winter = p.predict_24h_with_uncertainty(0, 0, 1, rng=random.Random(1))  # January
        summer = p.predict_24h_with_uncertainty(0, 0, 7, rng=random.Random(1))  # July
        avg_winter = sum(hp.p50 for hp in winter) / 24
        avg_summer = sum(hp.p50 for hp in summer) / 24
        assert avg_winter > avg_summer
