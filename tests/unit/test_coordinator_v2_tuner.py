"""PLAT-1234: Tests for BayesianTuner integration in CoordinatorV2."""

from __future__ import annotations

from custom_components.carmabox.const import (
    BAYES_CONSTRAINT_MARGIN_MAX,
    BAYES_CONSTRAINT_MARGIN_MIN,
    SCHEDULER_CONSTRAINT_MARGIN,
)
from custom_components.carmabox.core.coordinator_v2 import (
    CoordinatorConfig,
    CoordinatorV2,
    SystemState,
)
from custom_components.carmabox.optimizer.bayesian_tuner import TunerParams


def _state(**kw) -> SystemState:
    defaults = {
        "grid_import_w": 1500,
        "ellevio_viktat_kw": 1.5,
        "pv_power_w": 0,
        "battery_soc_1": 50,
        "battery_soc_2": 50,
        "battery_power_1": 0,
        "battery_power_2": 0,
        "battery_temp_1": 15,
        "battery_temp_2": 15,
        "ems_mode_1": "discharge_pv",
        "ems_mode_2": "discharge_pv",
        "fast_charging_1": False,
        "fast_charging_2": False,
        "ev_soc": 60,
        "ev_power_w": 0,
        "ev_connected": True,
        "ev_enabled": True,
        "current_price": 50,
        "disk_power_w": 0,
        "tvatt_power_w": 0,
        "miner_power_w": 0,
        "hour": 23,
        "minute": 0,
    }
    defaults.update(kw)
    return SystemState(**defaults)


def _confirmed(coord: CoordinatorV2) -> None:
    """Fast-forward through startup."""
    coord.cycle(_state())
    coord.cycle(_state())


# ── Construction ───────────────────────────────────────────────────────────


def test_tuner_instantiated_in_init() -> None:
    from custom_components.carmabox.optimizer.bayesian_tuner import BayesianTuner

    coord = CoordinatorV2()
    assert isinstance(coord._tuner, BayesianTuner)


def test_plan_feedback_instantiated_in_init() -> None:
    from custom_components.carmabox.optimizer.plan_feedback import PlanFeedback

    coord = CoordinatorV2()
    assert isinstance(coord._plan_feedback, PlanFeedback)


def test_tuner_params_default_to_scheduler_constants() -> None:
    coord = CoordinatorV2()
    assert coord._tuner_params.constraint_margin == SCHEDULER_CONSTRAINT_MARGIN


def test_runtime_dirty_false_on_init() -> None:
    coord = CoordinatorV2()
    assert coord._runtime_dirty is False


# ── tune() at night plan interval ─────────────────────────────────────────


def test_tune_called_at_night_plan_interval() -> None:
    """_tuner_params updated and _runtime_dirty set when night + plan_counter fires."""
    cfg = CoordinatorConfig(plan_interval_cycles=2)
    coord = CoordinatorV2(cfg)
    _confirmed(coord)
    coord._runtime_dirty = False
    # Advance plan counter to trigger plan_due at night
    coord.plan_counter = cfg.plan_interval_cycles - 1
    coord.cycle(_state(hour=23))
    assert coord._runtime_dirty is True


def test_tune_not_called_during_day() -> None:
    """During daytime (hour=12), tune() should not be invoked."""
    cfg = CoordinatorConfig(plan_interval_cycles=2)
    coord = CoordinatorV2(cfg)
    _confirmed(coord)
    coord._runtime_dirty = False
    coord.plan_counter = cfg.plan_interval_cycles - 1
    coord.cycle(_state(hour=12))
    assert coord._runtime_dirty is False


def test_tune_updates_tuner_params_object() -> None:
    """After night plan trigger, _tuner_params is a TunerParams instance."""
    cfg = CoordinatorConfig(plan_interval_cycles=2)
    coord = CoordinatorV2(cfg)
    _confirmed(coord)
    coord.plan_counter = cfg.plan_interval_cycles - 1
    coord.cycle(_state(hour=23))
    assert isinstance(coord._tuner_params, TunerParams)


def test_apply_tuner_params_propagates_to_grid_guard() -> None:
    """_apply_tuner_params() updates grid_guard.config.margin."""
    coord = CoordinatorV2()
    new_params = TunerParams(
        constraint_margin=0.75,
        discharge_median_factor=0.9,
        aggressive_median_factor=1.3,
        battery_budget_low_ratio=0.3,
    )
    coord._tuner_params = new_params
    coord._apply_tuner_params()
    assert coord.grid_guard.config.margin == 0.75
    assert coord.config.grid_guard_margin == 0.75


# ── update_from_feedback after execution ──────────────────────────────────


def test_feedback_recorded_after_plan_execution() -> None:
    """After a normal cycle with execution, plan_feedback has a record."""
    coord = CoordinatorV2()
    _confirmed(coord)
    initial_count = len(coord._plan_feedback._history)
    coord.cycle(_state())
    assert len(coord._plan_feedback._history) > initial_count


def test_tuner_observations_grow_after_cycles() -> None:
    """update_from_feedback feeds the tuner — observation count grows."""
    coord = CoordinatorV2()
    _confirmed(coord)
    initial_obs = len(coord._tuner._observations)
    for _ in range(3):
        coord.cycle(_state())
    assert len(coord._tuner._observations) > initial_obs


# ── Persistence ────────────────────────────────────────────────────────────


def test_tuner_params_in_persistent_state() -> None:
    """get_persistent_state() must include 'tuner_params' dict."""
    coord = CoordinatorV2()
    state = coord.get_persistent_state()
    assert "tuner_params" in state
    tp = state["tuner_params"]
    assert "constraint_margin" in tp
    assert "discharge_median_factor" in tp
    assert "aggressive_median_factor" in tp
    assert "battery_budget_low_ratio" in tp


def test_restore_tuner_state_updates_params() -> None:
    """restore_tuner_state() replaces _tuner_params from dict."""
    coord = CoordinatorV2()
    coord.restore_tuner_state(
        {
            "constraint_margin": 0.80,
            "discharge_median_factor": 0.85,
            "aggressive_median_factor": 1.20,
            "battery_budget_low_ratio": 0.25,
        }
    )
    assert coord._tuner_params.constraint_margin == 0.80
    assert coord._tuner_params.discharge_median_factor == 0.85


def test_restore_tuner_state_invalid_data_keeps_defaults() -> None:
    """restore_tuner_state() with bad data silently keeps current params."""
    coord = CoordinatorV2()
    original = coord._tuner_params
    coord.restore_tuner_state({"bad_key": "garbage"})
    assert coord._tuner_params == original


def test_persistent_state_roundtrip() -> None:
    """Save → restore → save produces same tuner_params."""
    coord1 = CoordinatorV2()
    coord1._tuner_params = TunerParams(
        constraint_margin=0.82,
        discharge_median_factor=0.88,
        aggressive_median_factor=1.25,
        battery_budget_low_ratio=0.28,
    )
    saved = coord1.get_persistent_state()

    coord2 = CoordinatorV2()
    coord2.restore_tuner_state(saved["tuner_params"])
    saved2 = coord2.get_persistent_state()

    assert saved["tuner_params"] == saved2["tuner_params"]


# ── Bounds ─────────────────────────────────────────────────────────────────


def test_tuner_params_within_bounds_after_night_tune() -> None:
    """After multiple night plan cycles, constraint_margin stays within bounds."""
    cfg = CoordinatorConfig(plan_interval_cycles=1)
    coord = CoordinatorV2(cfg)
    _confirmed(coord)
    for _ in range(10):
        coord.cycle(_state(hour=23))
    margin = coord._tuner_params.constraint_margin
    assert BAYES_CONSTRAINT_MARGIN_MIN <= margin <= BAYES_CONSTRAINT_MARGIN_MAX
