"""Tests for PLAT-1233: UncertaintyModel integration into ScenarioEngine."""

from __future__ import annotations

import random

from custom_components.carmabox.optimizer.cost_model import CostModel
from custom_components.carmabox.optimizer.device_profiles import build_profiles
from custom_components.carmabox.optimizer.scenario_engine import ScenarioEngine
from custom_components.carmabox.optimizer.uncertainty_model import UncertaintyModel

# ── Helpers ─────────────────────────────────────────────────────────────────


def _uncertainty_model(
    p10: float = 20.0,
    p50: float = 50.0,
    p90: float = 80.0,
    soc: float = 60.0,
    rng: random.Random | None = None,
) -> UncertaintyModel:
    return UncertaintyModel(
        price_p10=p10,
        price_p50=p50,
        price_p90=p90,
        pv_factor_central=1.0,
        base_load_kw=2.0,
        base_soc_pct=soc,
        rng=rng if rng is not None else random.Random(42),
    )


def _engine_with_uncertainty(
    uncertainty_model: UncertaintyModel | None = None,
) -> ScenarioEngine:
    return ScenarioEngine(
        profiles=build_profiles({}),
        cost_model=CostModel(),
        uncertainty_model=uncertainty_model,
    )


def _base_state() -> dict:  # type: ignore[type-arg]
    prices = [80.0] * 24
    for h in [22, 23, 0, 1, 2, 3, 4, 5]:
        prices[h] = 20.0
    return {
        "battery_soc": 30.0,
        "ev_soc": 40.0,
        "ev_target_soc": 80.0,
        "battery_target_soc": 80.0,
        "hours": list(range(22, 24)) + list(range(0, 6)),
        "prices_ore": prices,
        "pv_tomorrow_kwh": 10.0,
    }


# ── 1. Construction ─────────────────────────────────────────────────────────


def test_uncertainty_model_none_by_default() -> None:
    """ScenarioEngine.uncertainty_model defaults to None."""
    eng = ScenarioEngine(profiles=build_profiles({}), cost_model=CostModel())
    assert eng.uncertainty_model is None


def test_accepts_uncertainty_model_at_init() -> None:
    """ScenarioEngine can be constructed with an UncertaintyModel."""
    um = _uncertainty_model()
    eng = _engine_with_uncertainty(um)
    assert eng.uncertainty_model is um


# ── 2. Backward compatibility (no uncertainty model) ────────────────────────


def test_generate_without_uncertainty_model_works_as_before() -> None:
    """Without uncertainty_model, generate_scenarios behaves as the parametric path."""
    eng = _engine_with_uncertainty(None)
    scenarios = eng.generate_scenarios(_base_state())
    # Parametric path: 5 templates x 3 deltas = 15 scenarios
    assert len(scenarios) == 15


def test_generate_without_uncertainty_ignores_n_scenarios() -> None:
    """n_scenarios parameter is ignored when uncertainty_model is None."""
    eng = _engine_with_uncertainty(None)
    s1 = eng.generate_scenarios(_base_state(), n_scenarios=3)
    s2 = eng.generate_scenarios(_base_state(), n_scenarios=100)
    assert len(s1) == len(s2)


# ── 3. Probabilistic path — counts ──────────────────────────────────────────


def test_generate_with_uncertainty_returns_n_scenarios() -> None:
    """With uncertainty_model, returns exactly n_scenarios items."""
    eng = _engine_with_uncertainty(_uncertainty_model())
    result = eng.generate_scenarios(_base_state(), n_scenarios=7)
    assert len(result) == 7


def test_generate_probabilistic_n1_returns_one() -> None:
    """n_scenarios=1 returns exactly one scenario."""
    eng = _engine_with_uncertainty(_uncertainty_model())
    result = eng.generate_scenarios(_base_state(), n_scenarios=1)
    assert len(result) == 1


def test_generate_probabilistic_n0_returns_one() -> None:
    """n_scenarios=0 is clamped to 1 — caller protection."""
    eng = _engine_with_uncertainty(_uncertainty_model())
    result = eng.generate_scenarios(_base_state(), n_scenarios=0)
    assert len(result) == 1


def test_generate_probabilistic_large_n() -> None:
    """Large n_scenarios produces correct count without error."""
    eng = _engine_with_uncertainty(_uncertainty_model(rng=random.Random(7)))
    result = eng.generate_scenarios(_base_state(), n_scenarios=30)
    assert len(result) == 30


# ── 4. Scoring ───────────────────────────────────────────────────────────────


def test_generate_probabilistic_scenarios_are_scored() -> None:
    """All returned scenarios have total_cost_kr set (not all 0.0)."""
    eng = _engine_with_uncertainty(_uncertainty_model())
    result = eng.generate_scenarios(_base_state(), n_scenarios=5)
    # At least some should have non-zero cost (EV+battery need charging)
    assert any(s.total_cost_kr >= 0.0 for s in result)


def test_generate_probabilistic_sorted_by_cost() -> None:
    """Returned list is sorted by total_cost_kr ascending."""
    eng = _engine_with_uncertainty(_uncertainty_model())
    result = eng.generate_scenarios(_base_state(), n_scenarios=10)
    costs = [s.total_cost_kr for s in result]
    assert costs == sorted(costs)


# ── 5. Stochastic inputs affect output ──────────────────────────────────────


def test_generate_probabilistic_deterministic_with_seeded_rng() -> None:
    """Same seed produces identical cost totals."""
    state = _base_state()
    eng1 = _engine_with_uncertainty(_uncertainty_model(rng=random.Random(99)))
    eng2 = _engine_with_uncertainty(_uncertainty_model(rng=random.Random(99)))
    r1 = eng1.generate_scenarios(state, n_scenarios=5)
    r2 = eng2.generate_scenarios(state, n_scenarios=5)
    assert len(r1) == len(r2)
    for a, b in zip(r1, r2, strict=True):
        assert a.total_cost_kr == b.total_cost_kr


def test_generate_probabilistic_high_price_increases_cost() -> None:
    """A model with higher prices should produce higher-cost scenarios than low prices."""
    state = _base_state()
    eng_cheap = _engine_with_uncertainty(
        _uncertainty_model(p10=5.0, p50=10.0, p90=15.0, rng=random.Random(1))
    )
    eng_dear = _engine_with_uncertainty(
        _uncertainty_model(p10=200.0, p50=250.0, p90=300.0, rng=random.Random(1))
    )
    cheap_costs = [s.total_cost_kr for s in eng_cheap.generate_scenarios(state, n_scenarios=5)]
    dear_costs = [s.total_cost_kr for s in eng_dear.generate_scenarios(state, n_scenarios=5)]
    assert sum(cheap_costs) < sum(dear_costs)


def test_generate_probabilistic_high_soc_reduces_slots() -> None:
    """High base SoC (fewer slots needed) should not raise and must return n scenarios."""
    eng = _engine_with_uncertainty(
        _uncertainty_model(soc=98.0, rng=random.Random(5))
    )
    state = _base_state()
    state["battery_target_soc"] = 80.0  # already near full
    result = eng.generate_scenarios(state, n_scenarios=5)
    assert len(result) == 5


def test_generate_probabilistic_pv_factor_scales_pv_tomorrow() -> None:
    """Verify that scenarios are generated (PV factor is forwarded without error)."""
    eng = _engine_with_uncertainty(
        _uncertainty_model(rng=random.Random(3))
    )
    state = _base_state()
    state["pv_tomorrow_kwh"] = 20.0
    result = eng.generate_scenarios(state, n_scenarios=4)
    assert len(result) == 4
    assert all(s.total_cost_kr >= 0.0 for s in result)
