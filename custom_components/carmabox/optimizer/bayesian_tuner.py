"""PLAT-1232: BayesianTuner -- automatic hyperparameter optimisation.

Pure Python + numpy. No HA imports. Fully testable.

Uses Gaussian Process (GP) Regression with an RBF kernel to model the
mapping from scheduler hyperparameter vectors to observed outcome scores.
Expected Improvement (EI) is the acquisition function used to suggest
the next hyperparameter configuration to try.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from ..const import (
    BAYES_AGGRESSIVE_MEDIAN_FACTOR_MAX,
    BAYES_AGGRESSIVE_MEDIAN_FACTOR_MIN,
    BAYES_BATTERY_BUDGET_LOW_RATIO_MAX,
    BAYES_BATTERY_BUDGET_LOW_RATIO_MIN,
    BAYES_CONSTRAINT_MARGIN_MAX,
    BAYES_CONSTRAINT_MARGIN_MIN,
    BAYES_DISCHARGE_MEDIAN_FACTOR_MAX,
    BAYES_DISCHARGE_MEDIAN_FACTOR_MIN,
    BAYES_EI_XI,
    BAYES_GP_KERNEL_SIGMA,
    BAYES_GP_LENGTH_SCALE,
    BAYES_GP_NOISE_SIGMA,
    BAYES_MAX_OBSERVATIONS,
    BAYES_N_INIT_SAMPLES,
    BAYES_RAND_CANDIDATES,
    SCHEDULER_AGGRESSIVE_MEDIAN_FACTOR,
    SCHEDULER_BATTERY_BUDGET_LOW_RATIO,
    SCHEDULER_CONSTRAINT_MARGIN,
    SCHEDULER_DISCHARGE_MEDIAN_FACTOR,
)

if TYPE_CHECKING:
    from ..optimizer.plan_feedback import FeedbackData

__all__ = ["BayesianTuner", "TunerParams"]

_LOGGER = logging.getLogger(__name__)

_SQRT_2: float = math.sqrt(2.0)
_SQRT_2PI: float = math.sqrt(2.0 * math.pi)

# Type alias: numpy float64 array (Any used for shape parameter -- numpy
# stubs do not provide a stable parametric form for mypy strict)
_FloatArray = Any


# ── Helpers ────────────────────────────────────────────────────────────────


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via erfc."""
    return 0.5 * math.erfc(-z / _SQRT_2)


def _normal_pdf(z: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * z * z) / _SQRT_2PI


def _uniform(lo: float, hi: float) -> float:
    """Return a random float uniformly sampled from [lo, hi]."""
    return lo + (hi - lo) * random.random()


# ── Data types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TunerParams:
    """Scheduler hyperparameters managed by BayesianTuner."""

    constraint_margin: float
    discharge_median_factor: float
    aggressive_median_factor: float
    battery_budget_low_ratio: float


def _default_params() -> TunerParams:
    """Return TunerParams initialised from scheduler defaults in const.py."""
    return TunerParams(
        constraint_margin=SCHEDULER_CONSTRAINT_MARGIN,
        discharge_median_factor=SCHEDULER_DISCHARGE_MEDIAN_FACTOR,
        aggressive_median_factor=SCHEDULER_AGGRESSIVE_MEDIAN_FACTOR,
        battery_budget_low_ratio=SCHEDULER_BATTERY_BUDGET_LOW_RATIO,
    )


def _params_to_array(params: TunerParams) -> _FloatArray:
    return np.array(
        [
            params.constraint_margin,
            params.discharge_median_factor,
            params.aggressive_median_factor,
            params.battery_budget_low_ratio,
        ],
        dtype=np.float64,
    )


def _array_to_params(arr: _FloatArray) -> TunerParams:
    return TunerParams(
        constraint_margin=float(arr[0]),
        discharge_median_factor=float(arr[1]),
        aggressive_median_factor=float(arr[2]),
        battery_budget_low_ratio=float(arr[3]),
    )


# ── GP Regression ──────────────────────────────────────────────────────────


class _GaussianProcess:
    """Minimal GP Regression with RBF (squared exponential) kernel.

    Uses numpy Cholesky for stable inversion of the kernel matrix.
    """

    def __init__(
        self,
        kernel_sigma: float,
        length_scale: float,
        noise_sigma: float,
    ) -> None:
        self._kernel_sigma = kernel_sigma
        self._length_scale = length_scale
        self._noise_sigma = noise_sigma
        self._x_train: _FloatArray | None = None
        self._chol: _FloatArray | None = None
        self._alpha: _FloatArray | None = None

    def _rbf(self, x_a: _FloatArray, x_b: _FloatArray) -> _FloatArray:
        """RBF kernel matrix K(x_a, x_b) with shape (|x_a|, |x_b|)."""
        diff: _FloatArray = x_a[:, np.newaxis, :] - x_b[np.newaxis, :, :]
        sq_dist: _FloatArray = np.sum(diff**2, axis=-1)
        return (self._kernel_sigma**2) * np.exp(-sq_dist / (2.0 * self._length_scale**2))

    def fit(self, x_train: _FloatArray, y: _FloatArray) -> None:
        """Fit GP to training inputs x_train (n x d) and targets y (n,)."""
        n: int = x_train.shape[0]
        k_mat: _FloatArray = self._rbf(x_train, x_train)
        k_noisy: _FloatArray = k_mat + (self._noise_sigma**2) * np.eye(n)
        self._x_train = x_train
        self._chol = np.linalg.cholesky(k_noisy)
        self._alpha = np.linalg.solve(self._chol.T, np.linalg.solve(self._chol, y))

    def predict(self, x_star: _FloatArray) -> tuple[_FloatArray, _FloatArray]:
        """Return (mean, std) arrays for test points x_star (m x d).

        Falls back to zero-mean / prior-std when the model has not been
        fitted yet.
        """
        m: int = x_star.shape[0]
        if self._x_train is None or self._alpha is None or self._chol is None:
            return np.zeros(m), np.full(m, self._kernel_sigma)

        k_star: _FloatArray = self._rbf(x_star, self._x_train)
        mu: _FloatArray = k_star @ self._alpha
        v: _FloatArray = np.linalg.solve(self._chol, k_star.T)
        k_diag: float = self._kernel_sigma**2
        var: _FloatArray = np.maximum(k_diag - np.sum(v**2, axis=0), 0.0)
        return mu, np.sqrt(var)


# ── Acquisition function ───────────────────────────────────────────────────


def _expected_improvement(
    mu: _FloatArray,
    sigma: _FloatArray,
    f_best: float,
    xi: float,
) -> _FloatArray:
    """Expected Improvement acquisition for an array of candidates."""
    improvement: _FloatArray = mu - f_best - xi
    ei: _FloatArray = np.zeros_like(mu)
    mask: _FloatArray = sigma > 0.0
    if not np.any(mask):
        return ei
    z_arr: _FloatArray = improvement[mask] / sigma[mask]
    z_list: list[float] = z_arr.tolist()
    cdf_vals: _FloatArray = np.array([_normal_cdf(z) for z in z_list], dtype=np.float64)
    pdf_vals: _FloatArray = np.array([_normal_pdf(z) for z in z_list], dtype=np.float64)
    ei[mask] = improvement[mask] * cdf_vals + sigma[mask] * pdf_vals
    return ei


# ── BayesianTuner ──────────────────────────────────────────────────────────


class BayesianTuner:
    """Tunes scheduler hyperparameters using Gaussian Process + EI.

    Usage::

        tuner = BayesianTuner()

        # After each planning cycle, record outcome:
        tuner.update(params, score)
        # or via FeedbackData:
        tuner.update_from_feedback(params, feedback_data)

        # Get the next config to try:
        next_params = tuner.tune()

        # Retrieve current best:
        best = tuner.get_params()
    """

    def __init__(self) -> None:
        self._observations: list[tuple[TunerParams, float]] = []
        self._best_params: TunerParams = _default_params()
        self._best_score: float = -math.inf
        self._gp = _GaussianProcess(
            kernel_sigma=BAYES_GP_KERNEL_SIGMA,
            length_scale=BAYES_GP_LENGTH_SCALE,
            noise_sigma=BAYES_GP_NOISE_SIGMA,
        )
        self._fitted: bool = False

    # ── Public API ─────────────────────────────────────────────────────────

    def update(self, params: TunerParams, score: float) -> None:
        """Record an observed (params, score) pair and refit the GP.

        The observations buffer is capped at BAYES_MAX_OBSERVATIONS; the
        oldest entry is discarded when the cap is exceeded.
        """
        self._observations.append((params, score))
        if len(self._observations) > BAYES_MAX_OBSERVATIONS:
            self._observations = self._observations[-BAYES_MAX_OBSERVATIONS:]
        if score > self._best_score:
            self._best_score = score
            self._best_params = params
        self._refit()

    def update_from_feedback(
        self,
        params: TunerParams,
        feedback: FeedbackData,
    ) -> None:
        """Derive a score from *feedback* and delegate to update().

        The score is plan_accuracy_pct / 100 so it lives in [0, 1].
        """
        score = feedback.plan_accuracy_pct / 100.0
        self.update(params, score)

    def tune(self) -> TunerParams:
        """Return the next hyperparameter configuration to try.

        During the initial exploration phase (fewer than BAYES_N_INIT_SAMPLES
        observations), a uniformly random configuration is returned.
        After that, Expected Improvement over BAYES_RAND_CANDIDATES random
        candidates balances exploration and exploitation.
        """
        if len(self._observations) < BAYES_N_INIT_SAMPLES or not self._fitted:
            return self._random_params()
        return self._maximise_ei()

    def get_params(self) -> TunerParams:
        """Return the best observed hyperparameter configuration so far."""
        return self._best_params

    # ── Private helpers ────────────────────────────────────────────────────

    def _refit(self) -> None:
        if len(self._observations) < 2:
            return
        x_data: _FloatArray = np.array(
            [_params_to_array(p) for p, _ in self._observations],
            dtype=np.float64,
        )
        y_data: _FloatArray = np.array([s for _, s in self._observations], dtype=np.float64)
        try:
            self._gp.fit(x_data, y_data)
            self._fitted = True
        except np.linalg.LinAlgError:
            _LOGGER.debug("GP Cholesky failed -- skipping fit cycle", exc_info=True)

    def _random_params(self) -> TunerParams:
        """Return a TunerParams sampled uniformly within parameter bounds."""
        return TunerParams(
            constraint_margin=_uniform(BAYES_CONSTRAINT_MARGIN_MIN, BAYES_CONSTRAINT_MARGIN_MAX),
            discharge_median_factor=_uniform(
                BAYES_DISCHARGE_MEDIAN_FACTOR_MIN, BAYES_DISCHARGE_MEDIAN_FACTOR_MAX
            ),
            aggressive_median_factor=_uniform(
                BAYES_AGGRESSIVE_MEDIAN_FACTOR_MIN, BAYES_AGGRESSIVE_MEDIAN_FACTOR_MAX
            ),
            battery_budget_low_ratio=_uniform(
                BAYES_BATTERY_BUDGET_LOW_RATIO_MIN, BAYES_BATTERY_BUDGET_LOW_RATIO_MAX
            ),
        )

    def _maximise_ei(self) -> TunerParams:
        """Pick the candidate with the highest Expected Improvement."""
        candidates: _FloatArray = np.array(
            [_params_to_array(self._random_params()) for _ in range(BAYES_RAND_CANDIDATES)],
            dtype=np.float64,
        )
        mu, sigma = self._gp.predict(candidates)
        ei = _expected_improvement(mu, sigma, self._best_score, BAYES_EI_XI)
        best_idx: int = int(np.argmax(ei))
        return _array_to_params(candidates[best_idx])
