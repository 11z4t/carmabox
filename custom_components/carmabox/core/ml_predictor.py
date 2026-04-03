"""ML Predictor — learns consumption patterns and optimizes decisions.

Pure Python. No HA imports. Fully testable.

Learns from historical data to improve planning:
  - House consumption per weekday/hour
  - Appliance patterns (when dishwasher typically runs)
  - Battery temperature → discharge capacity
  - Plan accuracy (planned vs actual per hour)
  - Atmospheric pressure → PV forecast correction

Stores rolling averages — no heavy ML frameworks needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from custom_components.carmabox.const import (
    DEFAULT_PLANNER_HOUSE_BASELOAD_KW,
    ML_CONFIDENCE_SATURATION_SAMPLES,
    ML_DEFAULT_APPLIANCE_RISK,
    ML_DEFAULT_TEMPERATURE_C,
    ML_EMA_ALPHA,
    ML_MAX_DECISION_OUTCOMES,
    ML_MAX_PRESSURE_SAMPLES,
    ML_MAX_SAMPLES_PER_BUCKET,
    ML_MIN_PLAN_CORRECTION_SAMPLES,
    ML_MIN_PLANNED_THRESHOLD_KW,
    ML_MIN_TRAINED_BUCKETS,
    ML_PRESSURE_HIGH_HPA,
    ML_PRESSURE_LOW_HPA,
    ML_SERIALIZE_LIMIT,
)


@dataclass
class ConsumptionSample:
    """One hour of consumption data."""

    weekday: int  # 0=Monday
    hour: int
    consumption_kw: float
    temperature_c: float = ML_DEFAULT_TEMPERATURE_C


@dataclass
class PlanAccuracySample:
    """Planned vs actual for one hour."""

    hour: int
    planned_grid_kw: float
    actual_grid_kw: float
    planned_action: str
    actual_action: str
    price: float


@dataclass
class PressureSample:
    """Atmospheric pressure for PV correlation."""

    pressure_hpa: float
    pv_actual_kwh: float
    pv_forecast_kwh: float


@dataclass
class AppliancePowerProfile:
    """Learned power profile for an appliance."""

    appliance_id: str
    typical_duration_min: float = 0.0
    peak_power_w: float = 0.0
    avg_power_w: float = 0.0
    total_energy_wh: float = 0.0
    sample_count: int = 0


def learn_appliance_cycle(
    profile: AppliancePowerProfile,
    cycle_duration_min: float,
    peak_w: float,
    avg_w: float,
    energy_wh: float,
) -> AppliancePowerProfile:
    """Update appliance profile with new cycle observation.

    Uses exponential moving average (EMA) with alpha=0.3.
    First observation sets values directly.
    """
    alpha = ML_EMA_ALPHA
    if profile.sample_count == 0:
        return AppliancePowerProfile(
            appliance_id=profile.appliance_id,
            typical_duration_min=cycle_duration_min,
            peak_power_w=peak_w,
            avg_power_w=avg_w,
            total_energy_wh=energy_wh,
            sample_count=1,
        )
    return AppliancePowerProfile(
        appliance_id=profile.appliance_id,
        typical_duration_min=(
            (1 - alpha) * profile.typical_duration_min + alpha * cycle_duration_min
        ),
        peak_power_w=(1 - alpha) * profile.peak_power_w + alpha * peak_w,
        avg_power_w=(1 - alpha) * profile.avg_power_w + alpha * avg_w,
        total_energy_wh=(1 - alpha) * profile.total_energy_wh + alpha * energy_wh,
        sample_count=profile.sample_count + 1,
    )


def predict_appliance_remaining(
    profile: AppliancePowerProfile,
    elapsed_min: float,
) -> dict[str, Any]:
    """Predict remaining time and energy for running appliance.

    Returns:
        - remaining_min: estimated minutes left (clamped to >= 0)
        - remaining_wh: estimated energy left (clamped to >= 0)
        - confidence: 0-1 based on sample_count (saturates around 10 samples)
    """
    if profile.sample_count == 0 or profile.typical_duration_min <= 0:
        return {"remaining_min": 0.0, "remaining_wh": 0.0, "confidence": 0.0}

    fraction_elapsed = min(elapsed_min / profile.typical_duration_min, 1.0)
    remaining_min = max(profile.typical_duration_min - elapsed_min, 0.0)
    remaining_wh = max(profile.total_energy_wh * (1.0 - fraction_elapsed), 0.0)
    confidence = min(profile.sample_count / ML_CONFIDENCE_SATURATION_SAMPLES, 1.0)

    return {
        "remaining_min": remaining_min,
        "remaining_wh": remaining_wh,
        "confidence": confidence,
    }


class MLPredictor:
    """Learns and predicts energy patterns."""

    def __init__(self) -> None:
        # Consumption: [weekday][hour] → rolling average
        self._consumption: dict[tuple[int, int], list[float]] = {}
        # Appliance: [hour] → count of appliance events
        self._appliance_hours: dict[int, int] = {}
        # Plan accuracy: [hour] → list of (planned, actual)
        self._plan_accuracy: dict[int, list[tuple[float, float]]] = {}
        # Pressure → PV ratio
        self._pressure_pv: list[tuple[float, float]] = []  # (pressure, pv_ratio)
        # Battery temp → effective capacity
        self._temp_capacity: list[tuple[float, float]] = []
        # Decision outcomes
        self._decision_outcomes: list[dict[str, Any]] = []
        self._max_samples = ML_MAX_SAMPLES_PER_BUCKET

    # ── Add samples ─────────────────────────────────────────────

    def add_consumption(self, sample: ConsumptionSample) -> None:
        """Record one hour of consumption."""
        key = (sample.weekday, sample.hour)
        if key not in self._consumption:
            self._consumption[key] = []
        self._consumption[key].append(sample.consumption_kw)
        if len(self._consumption[key]) > self._max_samples:
            self._consumption[key] = self._consumption[key][-self._max_samples :]

    def add_appliance_event(self, hour: int) -> None:
        """Record that an appliance ran at this hour."""
        self._appliance_hours[hour] = self._appliance_hours.get(hour, 0) + 1

    def add_plan_accuracy(self, sample: PlanAccuracySample) -> None:
        """Record planned vs actual for one hour."""
        if sample.hour not in self._plan_accuracy:
            self._plan_accuracy[sample.hour] = []
        self._plan_accuracy[sample.hour].append((sample.planned_grid_kw, sample.actual_grid_kw))
        if len(self._plan_accuracy[sample.hour]) > self._max_samples:
            self._plan_accuracy[sample.hour] = self._plan_accuracy[sample.hour][
                -self._max_samples :
            ]

    def add_pressure_pv(self, pressure_hpa: float, pv_ratio: float) -> None:
        """Record pressure → PV forecast accuracy."""
        self._pressure_pv.append((pressure_hpa, pv_ratio))
        if len(self._pressure_pv) > ML_MAX_PRESSURE_SAMPLES:
            self._pressure_pv = self._pressure_pv[-ML_MAX_PRESSURE_SAMPLES:]

    def add_decision_outcome(
        self,
        decision: str,
        context: dict[str, Any],
        outcome: str,
        laws_ok: bool,
    ) -> None:
        """Record decision + outcome for learning."""
        self._decision_outcomes.append(
            {
                "decision": decision,
                "context": context,
                "outcome": outcome,
                "laws_ok": laws_ok,
            }
        )
        if len(self._decision_outcomes) > ML_MAX_DECISION_OUTCOMES:
            self._decision_outcomes = self._decision_outcomes[-ML_MAX_DECISION_OUTCOMES:]

    # ── Predictions ─────────────────────────────────────────────

    def predict_consumption(self, weekday: int, hour: int) -> float:
        """Predicted consumption (kW) for this weekday+hour."""
        key = (weekday, hour)
        samples = self._consumption.get(key, [])
        if not samples:
            return DEFAULT_PLANNER_HOUSE_BASELOAD_KW
        return sum(samples) / len(samples)

    def predict_24h_consumption(self, weekday: int) -> list[float]:
        """Predicted 24h consumption profile."""
        return [self.predict_consumption(weekday, h) for h in range(24)]

    def predict_appliance_risk(self, hour: int) -> float:
        """Probability (0-1) that an appliance runs at this hour."""
        total = sum(self._appliance_hours.values())
        if total == 0:
            return ML_DEFAULT_APPLIANCE_RISK
        return self._appliance_hours.get(hour, 0) / total

    def get_plan_correction_factor(self, hour: int) -> float:
        """How much to adjust plan for this hour (1.0 = no correction)."""
        samples = self._plan_accuracy.get(hour, [])
        if len(samples) < ML_MIN_PLAN_CORRECTION_SAMPLES:
            return 1.0
        ratios = [
            actual / max(ML_MIN_PLANNED_THRESHOLD_KW, planned)
            for planned, actual in samples
            if planned > ML_MIN_PLANNED_THRESHOLD_KW
        ]
        if not ratios:
            return 1.0
        return sum(ratios) / len(ratios)

    def predict_pv_correction(self, pressure_hpa: float) -> float:
        """PV forecast correction factor based on pressure."""
        if not self._pressure_pv:
            return 1.0
        # Simple: high pressure → PV usually better than forecast
        # Low pressure → PV usually worse
        high = [r for p, r in self._pressure_pv if p > ML_PRESSURE_HIGH_HPA]
        low = [r for p, r in self._pressure_pv if p < ML_PRESSURE_LOW_HPA]
        if pressure_hpa > ML_PRESSURE_HIGH_HPA and high:
            return sum(high) / len(high)
        if pressure_hpa < ML_PRESSURE_LOW_HPA and low:
            return sum(low) / len(low)
        return 1.0

    def get_effective_decisions(self) -> dict[str, float]:
        """Which decisions keep laws intact? Returns effectiveness 0-1."""
        by_decision: dict[str, list[bool]] = {}
        for d in self._decision_outcomes:
            key = d["decision"]
            if key not in by_decision:
                by_decision[key] = []
            by_decision[key].append(d["laws_ok"])
        return {k: sum(v) / len(v) if v else 0.5 for k, v in by_decision.items()}

    # ── Serialization ───────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "consumption": {f"{k[0]}_{k[1]}": v for k, v in self._consumption.items()},
            "appliance_hours": self._appliance_hours,
            "pressure_pv": self._pressure_pv[-ML_SERIALIZE_LIMIT:],
            "decision_outcomes": self._decision_outcomes[-ML_SERIALIZE_LIMIT:],
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        """Restore from persistent storage."""
        for key_str, values in data.get("consumption", {}).items():
            parts = key_str.split("_")
            if len(parts) == 2:
                self._consumption[(int(parts[0]), int(parts[1]))] = values
        self._appliance_hours = data.get("appliance_hours", {})
        self._pressure_pv = data.get("pressure_pv", [])
        self._decision_outcomes = data.get("decision_outcomes", [])

    @property
    def is_trained(self) -> bool:
        """True if we have enough data to make predictions."""
        return len(self._consumption) >= ML_MIN_TRAINED_BUCKETS
