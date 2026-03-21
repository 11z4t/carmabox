"""CARMA Box — Consumption Predictor (Level 2 AI).

Local ML model that predicts hourly house consumption based on:
- Day of week (weekday/weekend patterns)
- Hour of day (morning/afternoon/evening/night)
- Season (summer low, winter high)
- Recent consumption history (last 7 days)

Uses simple linear regression (no external dependencies beyond numpy).
Trains locally on HA data — no cloud dependency.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Minimum samples before prediction is trusted
MIN_TRAINING_SAMPLES = 168  # 7 days × 24 hours


@dataclass
class HourSample:
    """One hour's consumption data for training."""

    weekday: int  # 0=Monday, 6=Sunday
    hour: int  # 0-23
    month: int  # 1-12
    consumption_kw: float
    temperature_c: float | None = None


@dataclass
class ConsumptionPredictor:
    """Predicts hourly consumption using weighted historical averages.

    No sklearn dependency — uses simple weighted averaging with
    day-of-week and seasonal adjustments.
    """

    # Historical data: [weekday][hour] → list of consumption values
    history: dict[str, list[float]] = field(default_factory=dict)
    total_samples: int = 0

    # Seasonal multipliers (learned)
    seasonal_factor: dict[int, float] = field(default_factory=lambda: {
        1: 1.4, 2: 1.3, 3: 1.1, 4: 0.9, 5: 0.8, 6: 0.7,
        7: 0.7, 8: 0.8, 9: 0.9, 10: 1.0, 11: 1.2, 12: 1.4,
    })

    def add_sample(self, sample: HourSample) -> None:
        """Add a consumption sample for training."""
        key = f"{sample.weekday}_{sample.hour}"
        if key not in self.history:
            self.history[key] = []

        self.history[key].append(sample.consumption_kw)

        # Keep last 30 samples per slot (30 days of data per hour)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

        self.total_samples += 1

    def predict_hour(
        self,
        weekday: int,
        hour: int,
        month: int,
        fallback_kw: float = 2.0,
    ) -> float:
        """Predict consumption for a specific hour.

        Returns predicted kW, or fallback if insufficient data.
        """
        if self.total_samples < MIN_TRAINING_SAMPLES:
            return fallback_kw

        key = f"{weekday}_{hour}"
        samples = self.history.get(key, [])

        if not samples:
            # Try similar days (adjacent weekday)
            for offset in (1, -1, 2, -2):
                alt_key = f"{(weekday + offset) % 7}_{hour}"
                alt = self.history.get(alt_key, [])
                if alt:
                    samples = alt
                    break

        if not samples:
            return fallback_kw

        # Weighted average: recent samples weigh more
        weights = [math.exp(i * 0.1) for i in range(len(samples))]
        total_w = sum(weights)
        avg = sum(s * w for s, w in zip(samples, weights)) / total_w

        # Apply seasonal adjustment
        base_month = 9  # September = baseline (factor 0.9)
        base_factor = self.seasonal_factor.get(base_month, 1.0)
        current_factor = self.seasonal_factor.get(month, 1.0)
        seasonal_adj = current_factor / base_factor if base_factor > 0 else 1.0

        predicted = avg * seasonal_adj
        return round(max(0.3, predicted), 2)

    def predict_24h(
        self,
        start_hour: int,
        weekday: int,
        month: int,
        fallback_profile: list[float] | None = None,
    ) -> list[float]:
        """Predict 24 hours of consumption starting from start_hour.

        Returns list of 24 predicted kW values.
        """
        if self.total_samples < MIN_TRAINING_SAMPLES:
            if fallback_profile and len(fallback_profile) >= 24:
                return fallback_profile[start_hour:] + fallback_profile[:start_hour]
            return [2.0] * 24

        predictions = []
        for i in range(24):
            h = (start_hour + i) % 24
            # Weekday advances at midnight
            d = weekday if (start_hour + i) < 24 else (weekday + 1) % 7
            pred = self.predict_hour(d, h, month)
            predictions.append(pred)

        return predictions

    @property
    def is_trained(self) -> bool:
        """True if enough data for reliable predictions."""
        return self.total_samples >= MIN_TRAINING_SAMPLES

    @property
    def accuracy_estimate(self) -> float:
        """Rough accuracy estimate based on data coverage.

        Returns 0-100%. 100% = all 168 weekday×hour slots have data.
        """
        filled = sum(1 for v in self.history.values() if len(v) >= 3)
        total_slots = 7 * 24  # 168
        return round(filled / total_slots * 100, 0)

    def to_dict(self) -> dict:
        """Serialize for persistent storage."""
        return {
            "history": self.history,
            "total_samples": self.total_samples,
            "seasonal_factor": self.seasonal_factor,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConsumptionPredictor:
        """Deserialize from storage."""
        pred = cls()
        pred.history = data.get("history", {})
        pred.total_samples = data.get("total_samples", 0)
        sf = data.get("seasonal_factor", {})
        if sf:
            pred.seasonal_factor = {int(k): float(v) for k, v in sf.items()}
        return pred
