"""CARMA Box — Weather-aware consumption learning (PLAT-963).

Pure Python. No HA imports. Fully testable.

Learns correlation between outdoor temperature and household consumption.
Uses temperature bins (5°C wide) to build per-hour adjustment factors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Temperature bins: -20 to +35°C in 5°C steps → 11 bins
TEMP_BIN_WIDTH = 5
TEMP_BIN_MIN = -20
TEMP_BIN_MAX = 35
NUM_BINS = (TEMP_BIN_MAX - TEMP_BIN_MIN) // TEMP_BIN_WIDTH  # 11

# EMA alpha for temperature adjustment learning
TEMP_EMA_ALPHA = 0.08
MIN_SAMPLES_PER_BIN = 10


def _temp_to_bin(temp_c: float) -> int:
    """Convert temperature to bin index (0-10)."""
    clamped = max(TEMP_BIN_MIN, min(TEMP_BIN_MAX - 1, temp_c))
    return int((clamped - TEMP_BIN_MIN) // TEMP_BIN_WIDTH)


def _bin_to_label(bin_idx: int) -> str:
    """Convert bin index to human-readable label."""
    low = TEMP_BIN_MIN + bin_idx * TEMP_BIN_WIDTH
    high = low + TEMP_BIN_WIDTH
    return f"{low}..{high}°C"


@dataclass
class WeatherProfile:
    """Temperature-consumption correlation model.

    Stores adjustment factors per temperature bin per hour.
    Factor 1.0 = baseline (no adjustment).
    Factor 1.3 = 30% more consumption than baseline at this temp.
    """

    # [hour][bin] → adjustment factor (1.0 = baseline)
    factors: list[list[float]] = field(
        default_factory=lambda: [[1.0] * NUM_BINS for _ in range(24)]
    )

    # [hour][bin] → sample count
    counts: list[list[int]] = field(default_factory=lambda: [[0] * NUM_BINS for _ in range(24)])

    # Reference consumption per hour (learned baseline at ~15°C)
    baseline_kw: list[float] = field(default_factory=lambda: [2.0] * 24)
    baseline_samples: int = 0

    def update(
        self,
        hour: int,
        temp_c: float,
        consumption_kw: float,
        baseline_consumption_kw: float,
    ) -> None:
        """Update weather model with a new measurement.

        Args:
            hour: Hour of day (0-23).
            temp_c: Outdoor temperature (°C).
            consumption_kw: Actual house consumption (kW).
            baseline_consumption_kw: Expected consumption at baseline temp
                (from ConsumptionProfile).
        """
        if hour < 0 or hour > 23:
            return
        if baseline_consumption_kw < 0.1:
            return  # Avoid division by zero

        bin_idx = _temp_to_bin(temp_c)
        actual_factor = consumption_kw / baseline_consumption_kw

        # EMA update of the adjustment factor
        old = self.factors[hour][bin_idx]
        self.factors[hour][bin_idx] = TEMP_EMA_ALPHA * actual_factor + (1 - TEMP_EMA_ALPHA) * old
        self.counts[hour][bin_idx] += 1

    def get_adjustment(self, hour: int, temp_c: float) -> float:
        """Get consumption adjustment factor for given hour and temperature.

        Returns 1.0 if insufficient data for this bin.
        """
        if hour < 0 or hour > 23:
            return 1.0
        bin_idx = _temp_to_bin(temp_c)
        if self.counts[hour][bin_idx] < MIN_SAMPLES_PER_BIN:
            # Interpolate from nearest bins with enough data
            return self._interpolate(hour, bin_idx)
        return round(self.factors[hour][bin_idx], 3)

    def _interpolate(self, hour: int, target_bin: int) -> float:
        """Interpolate from nearest bins with sufficient data."""
        left = right = None
        for offset in range(1, NUM_BINS):
            if left is None and target_bin - offset >= 0:
                idx = target_bin - offset
                if self.counts[hour][idx] >= MIN_SAMPLES_PER_BIN:
                    left = (idx, self.factors[hour][idx])
            if right is None and target_bin + offset < NUM_BINS:
                idx = target_bin + offset
                if self.counts[hour][idx] >= MIN_SAMPLES_PER_BIN:
                    right = (idx, self.factors[hour][idx])
            if left and right:
                break

        if left and right:
            # Linear interpolation
            span = right[0] - left[0]
            if span > 0:
                t = (target_bin - left[0]) / span
                return round(left[1] + t * (right[1] - left[1]), 3)
        if left:
            return round(left[1], 3)
        if right:
            return round(right[1], 3)
        return 1.0

    def adjust_prediction(
        self,
        hour: int,
        base_consumption_kw: float,
        temp_c: float,
    ) -> float:
        """Adjust a consumption prediction based on temperature.

        Args:
            hour: Hour of day (0-23).
            base_consumption_kw: Base prediction (from ConsumptionProfile/Predictor).
            temp_c: Forecast outdoor temperature (°C).

        Returns:
            Temperature-adjusted consumption prediction (kW).
        """
        factor = self.get_adjustment(hour, temp_c)
        return round(max(0.3, base_consumption_kw * factor), 2)

    @property
    def total_samples(self) -> int:
        """Total samples across all bins."""
        return sum(sum(row) for row in self.counts)

    @property
    def coverage_pct(self) -> float:
        """Percentage of hourxbin slots with sufficient data."""
        filled = sum(1 for row in self.counts for c in row if c >= MIN_SAMPLES_PER_BIN)
        return round(filled / (24 * NUM_BINS) * 100, 1)

    def summary(self) -> dict[str, Any]:
        """Summary for diagnostics/dashboard."""
        # Average factor per bin across all hours
        avg_per_bin = {}
        for b in range(NUM_BINS):
            total_count = sum(self.counts[h][b] for h in range(24))
            if total_count >= MIN_SAMPLES_PER_BIN:
                total_factor = sum(self.factors[h][b] for h in range(24))
                avg_per_bin[_bin_to_label(b)] = round(total_factor / 24, 2)
        return {
            "total_samples": self.total_samples,
            "coverage_pct": self.coverage_pct,
            "avg_factor_per_temp": avg_per_bin,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "factors": self.factors,
            "counts": self.counts,
            "baseline_kw": self.baseline_kw,
            "baseline_samples": self.baseline_samples,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeatherProfile:
        """Deserialize from storage."""
        wp = cls()
        factors = data.get("factors")
        counts = data.get("counts")
        if isinstance(factors, list) and len(factors) == 24:
            wp.factors = [
                [float(v) for v in row[:NUM_BINS]] + [1.0] * max(0, NUM_BINS - len(row))
                for row in factors
            ]
        if isinstance(counts, list) and len(counts) == 24:
            wp.counts = [
                [int(v) for v in row[:NUM_BINS]] + [0] * max(0, NUM_BINS - len(row))
                for row in counts
            ]
        bkw = data.get("baseline_kw")
        if isinstance(bkw, list) and len(bkw) == 24:
            wp.baseline_kw = [float(v) for v in bkw]
        wp.baseline_samples = int(data.get("baseline_samples", 0))
        return wp
