"""Tests for PLAT-1230: UncertaintyModel — probabilistic scenario inputs."""

from __future__ import annotations

import random

from custom_components.carmabox.optimizer.predictor import (
    ConsumptionPredictor,
    HourSample,
)
from custom_components.carmabox.optimizer.uncertainty_model import (
    ScenarioInputs,
    UncertaintyModel,
)

# ── Helpers ─────────────────────────────────────────────────────────────────

_RNG = random.Random(42)


def _model(
    p10: float = 50.0,
    p50: float = 100.0,
    p90: float = 150.0,
    pv: float = 1.0,
    load: float = 2.0,
    soc: float = 60.0,
    rng: random.Random | None = None,
) -> UncertaintyModel:
    return UncertaintyModel(
        price_p10=p10,
        price_p50=p50,
        price_p90=p90,
        pv_factor_central=pv,
        base_load_kw=load,
        base_soc_pct=soc,
        rng=rng if rng is not None else random.Random(1),
    )


def _trained_predictor(kw: float = 2.0) -> ConsumptionPredictor:
    p = ConsumptionPredictor()
    for wd in range(7):
        for h in range(24):
            for _ in range(5):
                p.add_sample(HourSample(weekday=wd, hour=h, month=9, consumption_kw=kw))
    return p


# ── ScenarioInputs dataclass ─────────────────────────────────────────────────


class TestScenarioInputs:
    def test_fields_exist(self) -> None:
        s = ScenarioInputs(price_ore=100.0, pv_factor=1.0, load_kw=2.0, soc_pct=60.0)
        assert s.price_ore == 100.0
        assert s.pv_factor == 1.0
        assert s.load_kw == 2.0
        assert s.soc_pct == 60.0

    def test_is_dataclass(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(ScenarioInputs)


# ── UncertaintyModel construction ────────────────────────────────────────────


class TestUncertaintyModelConstruct:
    def test_stores_price_quantiles(self) -> None:
        m = _model(p10=30.0, p50=80.0, p90=120.0)
        assert m.price_p10 == 30.0
        assert m.price_p50 == 80.0
        assert m.price_p90 == 120.0

    def test_stores_pv_factor(self) -> None:
        m = _model(pv=0.85)
        assert m.pv_factor_central == 0.85

    def test_stores_load_and_soc(self) -> None:
        m = _model(load=3.5, soc=75.0)
        assert m.base_load_kw == 3.5
        assert m.base_soc_pct == 75.0

    def test_default_rng_is_created(self) -> None:
        m = UncertaintyModel(50.0, 100.0, 150.0)
        assert m._rng is not None


# ── sample_inputs — count and types ──────────────────────────────────────────


class TestSampleInputsBasic:
    def test_returns_correct_count(self) -> None:
        m = _model()
        result = m.sample_inputs(10)
        assert len(result) == 10

    def test_returns_scenario_inputs_instances(self) -> None:
        m = _model()
        for s in m.sample_inputs(5):
            assert isinstance(s, ScenarioInputs)

    def test_n_scenarios_1_returns_one(self) -> None:
        m = _model()
        assert len(m.sample_inputs(1)) == 1

    def test_n_scenarios_0_returns_one(self) -> None:
        """0 is clamped to 1 — caller protection."""
        m = _model()
        assert len(m.sample_inputs(0)) == 1

    def test_large_n_scenarios(self) -> None:
        m = _model(rng=random.Random(7))
        assert len(m.sample_inputs(500)) == 500


# ── Price sampling ────────────────────────────────────────────────────────────


class TestPriceSampling:
    def test_price_non_negative(self) -> None:
        m = _model(p10=0.0, p50=10.0, p90=20.0, rng=random.Random(5))
        for s in m.sample_inputs(100):
            assert s.price_ore >= 0.0

    def test_price_spread_covers_below_p10(self) -> None:
        m = _model(p10=50.0, p50=100.0, p90=150.0, rng=random.Random(3))
        prices = [s.price_ore for s in m.sample_inputs(200)]
        assert min(prices) < 50.0, "Some prices should fall below P10"

    def test_price_spread_covers_above_p90(self) -> None:
        m = _model(p10=50.0, p50=100.0, p90=150.0, rng=random.Random(3))
        prices = [s.price_ore for s in m.sample_inputs(200)]
        assert max(prices) > 150.0, "Some prices should exceed P90"

    def test_median_price_near_p50(self) -> None:
        """Median of many samples should be close to P50."""
        m = _model(p10=50.0, p50=100.0, p90=150.0, rng=random.Random(9))
        prices = sorted(s.price_ore for s in m.sample_inputs(1000))
        median = prices[500]
        assert abs(median - 100.0) < 10.0, f"Median {median} too far from P50=100"

    def test_symmetric_quantiles_give_symmetric_distribution(self) -> None:
        """With P10=50, P50=100, P90=150 (symmetric spread), median ~ 100."""
        m = _model(p10=50.0, p50=100.0, p90=150.0, rng=random.Random(11))
        prices = sorted(s.price_ore for s in m.sample_inputs(1000))
        p10_actual = prices[100]
        p90_actual = prices[900]
        # Spread should be approximately symmetric around P50
        assert abs((100.0 - p10_actual) - (p90_actual - 100.0)) < 15.0


# ── PV factor sampling ────────────────────────────────────────────────────────


class TestPVFactorSampling:
    def test_pv_factor_non_negative(self) -> None:
        m = _model(pv=0.1, rng=random.Random(2))
        for s in m.sample_inputs(100):
            assert s.pv_factor >= 0.0

    def test_pv_factor_capped_at_max(self) -> None:
        m = _model(pv=1.9, rng=random.Random(2))
        for s in m.sample_inputs(200):
            assert s.pv_factor <= 2.0

    def test_pv_factor_central_is_mean(self) -> None:
        """Mean of Gaussian samples should be near the central value."""
        m = _model(pv=1.0, rng=random.Random(13))
        factors = [s.pv_factor for s in m.sample_inputs(1000)]
        mean_f = sum(factors) / len(factors)
        assert abs(mean_f - 1.0) < 0.05, f"Mean pv_factor {mean_f} too far from 1.0"


# ── Load sampling ─────────────────────────────────────────────────────────────


class TestLoadSampling:
    def test_load_within_pm20pct(self) -> None:
        m = _model(load=2.0, rng=random.Random(4))
        for s in m.sample_inputs(200):
            assert 2.0 * 0.80 <= s.load_kw <= 2.0 * 1.20

    def test_load_non_negative(self) -> None:
        m = _model(load=0.5, rng=random.Random(4))
        for s in m.sample_inputs(100):
            assert s.load_kw >= 0.0


# ── SoC sampling ──────────────────────────────────────────────────────────────


class TestSocSampling:
    def test_soc_within_pm2pct(self) -> None:
        m = _model(soc=60.0, rng=random.Random(6))
        for s in m.sample_inputs(200):
            assert 58.0 <= s.soc_pct <= 62.0

    def test_soc_clamped_to_0(self) -> None:
        """SoC near 0 should not go negative."""
        m = _model(soc=1.0, rng=random.Random(6))
        for s in m.sample_inputs(200):
            assert s.soc_pct >= 0.0

    def test_soc_clamped_to_100(self) -> None:
        """SoC near 100 should not exceed 100."""
        m = _model(soc=99.5, rng=random.Random(6))
        for s in m.sample_inputs(200):
            assert s.soc_pct <= 100.0


# ── Determinism with seeded RNG ───────────────────────────────────────────────


class TestDeterminism:
    def test_seeded_rng_gives_same_results(self) -> None:
        m1 = _model(rng=random.Random(42))
        m2 = _model(rng=random.Random(42))
        r1 = m1.sample_inputs(20)
        r2 = m2.sample_inputs(20)
        for a, b in zip(r1, r2, strict=True):
            assert a.price_ore == b.price_ore
            assert a.pv_factor == b.pv_factor
            assert a.load_kw == b.load_kw
            assert a.soc_pct == b.soc_pct


# ── from_predictor classmethod ────────────────────────────────────────────────


class TestFromPredictor:
    def test_from_predictor_returns_model(self) -> None:
        pred = _trained_predictor(kw=3.0)
        m = UncertaintyModel.from_predictor(
            predictor=pred,
            price_p10=50.0,
            price_p50=100.0,
            price_p90=150.0,
            base_soc_pct=70.0,
            start_hour=8,
            weekday=1,
            month=3,
            rng=random.Random(1),
        )
        assert isinstance(m, UncertaintyModel)
        result = m.sample_inputs(5)
        assert len(result) == 5

    def test_from_predictor_untrained_uses_default_load(self) -> None:
        pred = ConsumptionPredictor()  # untrained
        m = UncertaintyModel.from_predictor(
            predictor=pred,
            price_p10=50.0,
            price_p50=100.0,
            price_p90=150.0,
            base_soc_pct=50.0,
            start_hour=0,
            weekday=0,
            month=6,
            rng=random.Random(1),
        )
        # Untrained predictor returns fallback 2.0 — load must be positive
        assert m.base_load_kw > 0.0
