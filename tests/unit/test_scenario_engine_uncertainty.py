"""PLAT-1235: Tests for UncertaintyModel integration in ScenarioEngine."""

from __future__ import annotations

import random
from unittest.mock import MagicMock

from custom_components.carmabox.optimizer.cost_model import CostModel, ScenarioCost
from custom_components.carmabox.optimizer.device_profiles import Scenario, build_profiles
from custom_components.carmabox.optimizer.scenario_engine import ScenarioEngine
from custom_components.carmabox.optimizer.uncertainty_model import UncertaintyModel

# ── Helpers ────────────────────────────────────────────────────────────────


def _base_state() -> dict:  # type: ignore[type-arg]
    prices = [80.0] * 24
    for h in (22, 23, 0, 1, 2, 3, 4, 5):
        prices[h] = 20.0
    return {
        "battery_soc": 30.0,
        "ev_soc": 50.0,
        "ev_target_soc": 80.0,
        "battery_target_soc": 80.0,
        "hours": list(range(22, 24)) + list(range(0, 6)),
        "prices_ore": prices,
        "pv_tomorrow_kwh": 5.0,
    }


def _make_uncertainty(seed: int = 42) -> UncertaintyModel:
    return UncertaintyModel(
        price_p10=10.0,
        price_p50=50.0,
        price_p90=90.0,
        rng=random.Random(seed),
    )


def _engine(seed: int = 42) -> ScenarioEngine:
    return ScenarioEngine(
        profiles=build_profiles({}),
        cost_model=CostModel(),
        uncertainty_model=_make_uncertainty(seed),
    )


def _engine_no_uncertainty() -> ScenarioEngine:
    return ScenarioEngine(profiles=build_profiles({}), cost_model=CostModel())


# ── Construction ───────────────────────────────────────────────────────────


def test_uncertainty_model_injected_in_constructor() -> None:
    um = _make_uncertainty()
    eng = ScenarioEngine(
        profiles=build_profiles({}),
        cost_model=CostModel(),
        uncertainty_model=um,
    )
    assert eng.uncertainty_model is um


def test_uncertainty_model_defaults_to_none() -> None:
    eng = ScenarioEngine(profiles=build_profiles({}), cost_model=CostModel())
    assert eng.uncertainty_model is None


# ── Template fallback (no UncertaintyModel) ────────────────────────────────


def test_without_uncertainty_returns_template_scenarios() -> None:
    """No UncertaintyModel → existing template behaviour unchanged."""
    scenarios = _engine_no_uncertainty().generate_scenarios(_base_state())
    assert len(scenarios) >= 5  # SCENARIO_MIN_COUNT
    assert all(isinstance(s, Scenario) for s in scenarios)


def test_without_uncertainty_ignores_n_scenarios() -> None:
    """Template mode ignores the n_scenarios parameter."""
    eng = _engine_no_uncertainty()
    s1 = eng.generate_scenarios(_base_state(), n_scenarios=3)
    s2 = eng.generate_scenarios(_base_state(), n_scenarios=20)
    # Both should give the same template count (not 3 or 20)
    assert len(s1) == len(s2)


# ── n_scenarios edge cases ─────────────────────────────────────────────────


def test_n_zero_returns_empty_list() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=0)
    assert scenarios == []


def test_n_negative_returns_empty_list() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=-5)
    assert scenarios == []


def test_n_one_returns_one_scenario() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=1)
    assert len(scenarios) == 1


def test_default_n_is_10() -> None:
    """Calling generate_scenarios without n_scenarios should produce 10 items."""
    scenarios = _engine().generate_scenarios(_base_state())
    assert len(scenarios) == 10


# ── Return type and count ──────────────────────────────────────────────────


def test_returns_list_of_scenario_objects() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=5)
    assert isinstance(scenarios, list)
    assert all(isinstance(s, Scenario) for s in scenarios)


def test_returns_exactly_n_scenarios() -> None:
    for n in (2, 7, 15):
        scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=n)
        assert len(scenarios) == n, f"expected {n}, got {len(scenarios)}"


def test_scenario_names_prefixed_stochastic() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=4)
    for _i, sc in enumerate(sorted(scenarios, key=lambda s: int(s.name.split("_")[-1]))):
        assert sc.name.startswith("stochastic_")


# ── Cost ranking ───────────────────────────────────────────────────────────


def test_scenarios_sorted_by_cost_ascending() -> None:
    """Returned list must be sorted cheapest-first."""
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=8)
    costs = [s.total_cost_kr for s in scenarios]
    assert costs == sorted(costs)


def test_scenarios_have_non_negative_costs() -> None:
    scenarios = _engine().generate_scenarios(_base_state(), n_scenarios=5)
    assert all(s.total_cost_kr >= 0.0 for s in scenarios)


def test_cost_model_called_for_each_scenario() -> None:
    """CostModel.calculate_scenario_cost must be called once per scenario."""
    mock_cm = MagicMock()
    mock_cm.calculate_scenario_cost.return_value = ScenarioCost(
        grid_cost_kr=1.0, ellevio_penalty_kr=0.0, export_loss_kr=0.0, deferred_cost_kr=0.0
    )
    eng = ScenarioEngine(
        profiles=build_profiles({}),
        cost_model=mock_cm,
        uncertainty_model=_make_uncertainty(),
    )
    n = 6
    eng.generate_scenarios(_base_state(), n_scenarios=n)
    assert mock_cm.calculate_scenario_cost.call_count == n


# ── Determinism ────────────────────────────────────────────────────────────


def test_deterministic_with_same_seed() -> None:
    """Identical seeds must produce identical scenarios."""
    s1 = _engine(seed=7).generate_scenarios(_base_state(), n_scenarios=5)
    s2 = _engine(seed=7).generate_scenarios(_base_state(), n_scenarios=5)
    assert [sc.name for sc in s1] == [sc.name for sc in s2]
    assert [sc.total_cost_kr for sc in s1] == [sc.total_cost_kr for sc in s2]


def test_different_seeds_produce_different_costs() -> None:
    """Different seeds should (almost certainly) produce different cost sets."""
    s1 = _engine(seed=1).generate_scenarios(_base_state(), n_scenarios=10)
    s2 = _engine(seed=999).generate_scenarios(_base_state(), n_scenarios=10)
    costs1 = [s.total_cost_kr for s in s1]
    costs2 = [s.total_cost_kr for s in s2]
    # Different seeds → different price samples → different costs (with very high probability)
    assert costs1 != costs2
