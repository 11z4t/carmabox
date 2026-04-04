"""CARMA Box — Uncertainty Model (PLAT-1230).

Generates stochastic scenario inputs (P10/P50/P90) for price, solar,
load and battery state-of-charge. Used by ScenarioEngine to produce
a distribution of realistic planning scenarios.

Imports predict_24h_with_uncertainty() from ConsumptionPredictor (PLAT-1231).

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..const import (
    UNCERTAINTY_LOAD_VARIATION_PCT,
    UNCERTAINTY_PRICE_P10_QUANTILE,
    UNCERTAINTY_PRICE_P90_QUANTILE,
    UNCERTAINTY_PV_FACTOR_MAX,
    UNCERTAINTY_PV_FACTOR_MIN,
    UNCERTAINTY_PV_FACTOR_SPREAD,
    UNCERTAINTY_SOC_MAX_PCT,
    UNCERTAINTY_SOC_MIN_PCT,
    UNCERTAINTY_SOC_VARIATION_PCT,
)

if TYPE_CHECKING:
    from ..optimizer.predictor import ConsumptionPredictor

__all__ = ["ScenarioInputs", "UncertaintyModel"]


@dataclass
class ScenarioInputs:
    """Stochastic inputs for one planning scenario (PLAT-1230).

    All fields are sampled from their respective uncertainty distributions
    by UncertaintyModel.sample_inputs().
    """

    price_ore: float  # Spot price in öre/kWh
    pv_factor: float  # PV forecast correction factor (0.0-2.0)
    load_kw: float  # House load estimate (kW)
    soc_pct: float  # Battery state of charge (0-100 %)


class UncertaintyModel:
    """Generates stochastic scenario inputs for energy planning.

    Takes reference values (price P10/P50/P90, PV factor, load baseline,
    SoC) and produces N ScenarioInputs samples for Monte-Carlo planning.

    Price distribution: piecewise-linear CDF fitted to P10/P50/P90.
    PV factor distribution: Gaussian(pv_factor_central, sigma), clamped.
    Load distribution: Uniform(base * (1 - 20%), base * (1 + 20%)).
    SoC distribution: Uniform(base ± 2%), clamped to [0, 100].
    """

    def __init__(
        self,
        price_p10: float,
        price_p50: float,
        price_p90: float,
        pv_factor_central: float = 1.0,
        base_load_kw: float = 2.0,
        base_soc_pct: float = 50.0,
        rng: random.Random | None = None,
    ) -> None:
        """Initialise the uncertainty model.

        Args:
            price_p10: 10th percentile spot price (öre/kWh).
            price_p50: 50th percentile spot price (öre/kWh).
            price_p90: 90th percentile spot price (öre/kWh).
            pv_factor_central: Central PV correction factor from MLPredictor.
            base_load_kw: Baseline house load (kW).
            base_soc_pct: Current battery SoC (%).
            rng: Optional seeded Random instance for deterministic tests.
        """
        self.price_p10 = price_p10
        self.price_p50 = price_p50
        self.price_p90 = price_p90
        self.pv_factor_central = pv_factor_central
        self.base_load_kw = base_load_kw
        self.base_soc_pct = base_soc_pct
        self._rng = rng if rng is not None else random.Random()

    # ── Public API ──────────────────────────────────────────────────────────

    def sample_inputs(self, n_scenarios: int) -> list[ScenarioInputs]:
        """Generate n_scenarios stochastic ScenarioInputs.

        Each scenario is independently sampled from the uncertainty
        distributions defined at construction time.

        Args:
            n_scenarios: Number of scenarios to generate (>= 1).

        Returns:
            List of ScenarioInputs, length == n_scenarios.
        """
        return [self._sample_one() for _ in range(max(1, n_scenarios))]

    @classmethod
    def from_predictor(
        cls,
        predictor: ConsumptionPredictor,
        price_p10: float,
        price_p50: float,
        price_p90: float,
        base_soc_pct: float,
        start_hour: int,
        weekday: int,
        month: int,
        rng: random.Random | None = None,
    ) -> UncertaintyModel:
        """Construct an UncertaintyModel using ConsumptionPredictor for load baseline.

        Calls predict_24h_with_uncertainty() to obtain a P50 load estimate
        for start_hour, then uses that as base_load_kw.

        Args:
            predictor: Trained ConsumptionPredictor (PLAT-1231).
            price_p10: 10th percentile price (öre/kWh).
            price_p50: 50th percentile price (öre/kWh).
            price_p90: 90th percentile price (öre/kWh).
            base_soc_pct: Current battery SoC (%).
            start_hour: Hour to use as load baseline (0-23).
            weekday: Weekday (0=Monday).
            month: Month (1-12).
            rng: Optional seeded Random instance.

        Returns:
            Configured UncertaintyModel.
        """
        predictions = predictor.predict_24h_with_uncertainty(
            start_hour, weekday, month, rng=rng
        )
        base_load_kw = predictions[0].p50 if predictions else 2.0
        return cls(
            price_p10=price_p10,
            price_p50=price_p50,
            price_p90=price_p90,
            pv_factor_central=1.0,
            base_load_kw=base_load_kw,
            base_soc_pct=base_soc_pct,
            rng=rng,
        )

    # ── Sampling helpers ─────────────────────────────────────────────────────

    def _sample_one(self) -> ScenarioInputs:
        """Sample a single ScenarioInputs."""
        return ScenarioInputs(
            price_ore=self._sample_price(),
            pv_factor=self._sample_pv_factor(),
            load_kw=self._sample_load(),
            soc_pct=self._sample_soc(),
        )

    def _sample_price(self) -> float:
        """Sample spot price using piecewise-linear CDF through P10/P50/P90."""
        u = self._rng.random()
        p10, p50, p90 = self.price_p10, self.price_p50, self.price_p90

        # Slopes for each interval (price per unit of CDF)
        slope_left = (p50 - p10) / (0.50 - UNCERTAINTY_PRICE_P10_QUANTILE)
        slope_mid = (p90 - p50) / (UNCERTAINTY_PRICE_P90_QUANTILE - 0.50)

        if u <= UNCERTAINTY_PRICE_P10_QUANTILE:
            # Left tail: extrapolate using mid slope below P10
            price = p10 - slope_left * (UNCERTAINTY_PRICE_P10_QUANTILE - u)
        elif u <= 0.50:
            price = p10 + slope_left * (u - UNCERTAINTY_PRICE_P10_QUANTILE)
        elif u <= UNCERTAINTY_PRICE_P90_QUANTILE:
            price = p50 + slope_mid * (u - 0.50)
        else:
            # Right tail: extrapolate using mid slope above P90
            price = p90 + slope_mid * (u - UNCERTAINTY_PRICE_P90_QUANTILE)

        return round(max(0.0, price), 2)

    def _sample_pv_factor(self) -> float:
        """Sample PV correction factor from Gaussian distribution."""
        raw = self._rng.gauss(self.pv_factor_central, UNCERTAINTY_PV_FACTOR_SPREAD)
        return round(
            max(UNCERTAINTY_PV_FACTOR_MIN, min(UNCERTAINTY_PV_FACTOR_MAX, raw)), 3
        )

    def _sample_load(self) -> float:
        """Sample house load from Uniform(base * (1 - variation), base * (1 + variation))."""
        lo = self.base_load_kw * (1.0 - UNCERTAINTY_LOAD_VARIATION_PCT)
        hi = self.base_load_kw * (1.0 + UNCERTAINTY_LOAD_VARIATION_PCT)
        return round(max(0.0, self._rng.uniform(lo, hi)), 2)

    def _sample_soc(self) -> float:
        """Sample battery SoC from Uniform(base ± variation), clamped to [0, 100]."""
        lo = self.base_soc_pct - UNCERTAINTY_SOC_VARIATION_PCT
        hi = self.base_soc_pct + UNCERTAINTY_SOC_VARIATION_PCT
        raw = self._rng.uniform(lo, hi)
        return round(max(UNCERTAINTY_SOC_MIN_PCT, min(UNCERTAINTY_SOC_MAX_PCT, raw)), 2)
