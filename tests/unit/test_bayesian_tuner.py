"""PLAT-1232: Tests for optimizer/bayesian_tuner.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from custom_components.carmabox.const import (
    BAYES_AGGRESSIVE_MEDIAN_FACTOR_MAX,
    BAYES_AGGRESSIVE_MEDIAN_FACTOR_MIN,
    BAYES_BATTERY_BUDGET_LOW_RATIO_MAX,
    BAYES_BATTERY_BUDGET_LOW_RATIO_MIN,
    BAYES_CONSTRAINT_MARGIN_MAX,
    BAYES_CONSTRAINT_MARGIN_MIN,
    BAYES_DISCHARGE_MEDIAN_FACTOR_MAX,
    BAYES_DISCHARGE_MEDIAN_FACTOR_MIN,
    BAYES_N_INIT_SAMPLES,
    SCHEDULER_AGGRESSIVE_MEDIAN_FACTOR,
    SCHEDULER_BATTERY_BUDGET_LOW_RATIO,
    SCHEDULER_CONSTRAINT_MARGIN,
    SCHEDULER_DISCHARGE_MEDIAN_FACTOR,
)
from custom_components.carmabox.optimizer.bayesian_tuner import (
    BayesianTuner,
    TunerParams,
    _array_to_params,
    _expected_improvement,
    _GaussianProcess,
    _params_to_array,
)


def _make_params(
    cm: float = 0.85,
    dmf: float = 0.9,
    amf: float = 1.3,
    bbl: float = 0.3,
) -> TunerParams:
    return TunerParams(
        constraint_margin=cm,
        discharge_median_factor=dmf,
        aggressive_median_factor=amf,
        battery_budget_low_ratio=bbl,
    )


def _populate(tuner: BayesianTuner, n: int, score: float = 0.5) -> None:
    for i in range(n):
        p = _make_params(cm=0.70 + 0.01 * i)
        tuner.update(p, score)


# ── TunerParams & defaults ─────────────────────────────────────────────────


def test_default_params_reflect_scheduler_constants() -> None:
    """get_params() before any update returns scheduler defaults from const.py."""
    tuner = BayesianTuner()
    params = tuner.get_params()
    assert params.constraint_margin == SCHEDULER_CONSTRAINT_MARGIN
    assert params.discharge_median_factor == SCHEDULER_DISCHARGE_MEDIAN_FACTOR
    assert params.aggressive_median_factor == SCHEDULER_AGGRESSIVE_MEDIAN_FACTOR
    assert params.battery_budget_low_ratio == SCHEDULER_BATTERY_BUDGET_LOW_RATIO


def test_tuner_params_is_frozen() -> None:
    """TunerParams is a frozen dataclass — mutation raises AttributeError."""
    p = _make_params()
    with pytest.raises(AttributeError):
        p.constraint_margin = 0.99  # type: ignore[misc]


# ── update() ──────────────────────────────────────────────────────────────


def test_update_grows_observation_count() -> None:
    tuner = BayesianTuner()
    assert len(tuner._observations) == 0
    tuner.update(_make_params(), 0.7)
    assert len(tuner._observations) == 1
    tuner.update(_make_params(cm=0.8), 0.8)
    assert len(tuner._observations) == 2


def test_update_tracks_best_score_and_params() -> None:
    tuner = BayesianTuner()
    p_low = _make_params(cm=0.72)
    p_high = _make_params(cm=0.88)
    tuner.update(p_low, 0.5)
    tuner.update(p_high, 0.9)
    assert tuner.get_params() == p_high
    assert tuner._best_score == pytest.approx(0.9)


def test_update_does_not_regress_best_on_lower_score() -> None:
    tuner = BayesianTuner()
    p_best = _make_params(cm=0.90)
    tuner.update(p_best, 0.95)
    tuner.update(_make_params(cm=0.75), 0.4)
    assert tuner.get_params() == p_best


def test_update_caps_observations_at_max() -> None:
    from custom_components.carmabox.const import BAYES_MAX_OBSERVATIONS

    tuner = BayesianTuner()
    for i in range(BAYES_MAX_OBSERVATIONS + 5):
        tuner.update(_make_params(cm=0.70 + 0.001 * i), 0.5)
    assert len(tuner._observations) == BAYES_MAX_OBSERVATIONS


# ── update_from_feedback() ────────────────────────────────────────────────


def test_update_from_feedback_derives_score_from_accuracy() -> None:
    tuner = BayesianTuner()
    feedback = MagicMock()
    feedback.plan_accuracy_pct = 80.0
    params = _make_params()
    tuner.update_from_feedback(params, feedback)
    _, recorded_score = tuner._observations[0]
    assert recorded_score == pytest.approx(0.80)


def test_update_from_feedback_100_pct_gives_score_1() -> None:
    tuner = BayesianTuner()
    feedback = MagicMock()
    feedback.plan_accuracy_pct = 100.0
    tuner.update_from_feedback(_make_params(), feedback)
    _, score = tuner._observations[0]
    assert score == pytest.approx(1.0)


# ── tune() ────────────────────────────────────────────────────────────────


def test_tune_before_n_init_returns_tuner_params_type() -> None:
    """tune() always returns a TunerParams, even during exploration phase."""
    tuner = BayesianTuner()
    result = tuner.tune()
    assert isinstance(result, TunerParams)


def test_tune_after_n_init_uses_gp() -> None:
    """After BAYES_N_INIT_SAMPLES observations, _fitted becomes True."""
    tuner = BayesianTuner()
    _populate(tuner, BAYES_N_INIT_SAMPLES + 1)
    assert tuner._fitted is True
    result = tuner.tune()
    assert isinstance(result, TunerParams)


def test_tune_params_within_bounds() -> None:
    """Every parameter returned by tune() respects the declared bounds."""
    tuner = BayesianTuner()
    _populate(tuner, BAYES_N_INIT_SAMPLES + 2, score=0.6)
    for _ in range(20):
        p = tuner.tune()
        assert BAYES_CONSTRAINT_MARGIN_MIN <= p.constraint_margin <= BAYES_CONSTRAINT_MARGIN_MAX
        assert (
            BAYES_DISCHARGE_MEDIAN_FACTOR_MIN
            <= p.discharge_median_factor
            <= BAYES_DISCHARGE_MEDIAN_FACTOR_MAX
        )
        assert (
            BAYES_AGGRESSIVE_MEDIAN_FACTOR_MIN
            <= p.aggressive_median_factor
            <= BAYES_AGGRESSIVE_MEDIAN_FACTOR_MAX
        )
        assert (
            BAYES_BATTERY_BUDGET_LOW_RATIO_MIN
            <= p.battery_budget_low_ratio
            <= BAYES_BATTERY_BUDGET_LOW_RATIO_MAX
        )


# ── _GaussianProcess ──────────────────────────────────────────────────────


def test_gp_predict_before_fit_returns_prior_std() -> None:
    from custom_components.carmabox.const import BAYES_GP_KERNEL_SIGMA

    gp = _GaussianProcess(
        kernel_sigma=BAYES_GP_KERNEL_SIGMA,
        length_scale=0.3,
        noise_sigma=0.1,
    )
    x_star = np.array([[0.1, 0.2, 0.3, 0.4]], dtype=np.float64)
    mu, std = gp.predict(x_star)
    assert mu[0] == pytest.approx(0.0)
    assert std[0] == pytest.approx(BAYES_GP_KERNEL_SIGMA)


def test_gp_fit_predict_output_shapes() -> None:
    gp = _GaussianProcess(kernel_sigma=1.0, length_scale=0.3, noise_sigma=0.1)
    x_train = np.random.default_rng(42).random((8, 4))
    y = np.random.default_rng(42).random(8)
    gp.fit(x_train, y)
    x_star = np.random.default_rng(0).random((5, 4))
    mu, std = gp.predict(x_star)
    assert mu.shape == (5,)
    assert std.shape == (5,)
    assert np.all(std >= 0.0)


def test_gp_predict_std_near_training_point_is_low() -> None:
    """Posterior std should be small near a training point."""
    gp = _GaussianProcess(kernel_sigma=1.0, length_scale=0.5, noise_sigma=0.01)
    x_train = np.array([[0.5, 0.5, 0.5, 0.5]], dtype=np.float64)
    y = np.array([1.0], dtype=np.float64)
    gp.fit(x_train, y)
    _mu, std = gp.predict(x_train)
    assert std[0] < 0.2


# ── _expected_improvement ─────────────────────────────────────────────────


def test_ei_is_nonnegative() -> None:
    mu = np.array([0.3, 0.7, 1.2], dtype=np.float64)
    sigma = np.array([0.1, 0.2, 0.3], dtype=np.float64)
    ei = _expected_improvement(mu, sigma, f_best=0.5, xi=0.01)
    assert np.all(ei >= 0.0)


def test_ei_higher_where_improvement_possible() -> None:
    """EI should be higher for a point well above f_best than one below."""
    mu = np.array([1.5, 0.1], dtype=np.float64)
    sigma = np.array([0.2, 0.2], dtype=np.float64)
    ei = _expected_improvement(mu, sigma, f_best=0.5, xi=0.01)
    assert ei[0] > ei[1]


def test_ei_zero_when_sigma_zero() -> None:
    mu = np.array([2.0, 3.0], dtype=np.float64)
    sigma = np.zeros(2, dtype=np.float64)
    ei = _expected_improvement(mu, sigma, f_best=0.5, xi=0.01)
    assert np.all(ei == 0.0)


# ── array ↔ params roundtrip ──────────────────────────────────────────────


def test_params_to_array_roundtrip() -> None:
    p = _make_params(cm=0.82, dmf=0.95, amf=1.25, bbl=0.35)
    arr = _params_to_array(p)
    p2 = _array_to_params(arr)
    assert p2.constraint_margin == pytest.approx(p.constraint_margin)
    assert p2.discharge_median_factor == pytest.approx(p.discharge_median_factor)
    assert p2.aggressive_median_factor == pytest.approx(p.aggressive_median_factor)
    assert p2.battery_budget_low_ratio == pytest.approx(p.battery_budget_low_ratio)
