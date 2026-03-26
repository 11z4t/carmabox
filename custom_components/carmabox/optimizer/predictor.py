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
from typing import Any

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
    seasonal_factor: dict[int, float] = field(
        default_factory=lambda: {
            1: 1.4,
            2: 1.3,
            3: 1.1,
            4: 0.9,
            5: 0.8,
            6: 0.7,
            7: 0.7,
            8: 0.8,
            9: 0.9,
            10: 1.0,
            11: 1.2,
            12: 1.4,
        }
    )

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
        avg = sum(s * w for s, w in zip(samples, weights, strict=False)) / total_w

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

    def add_appliance_event(self, hour: int, weekday: int, appliance: str) -> None:
        """Track when appliances run for pattern learning."""
        key = f"app_{appliance}_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(1.0)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def predict_appliance_risk(self, hour: int, weekday: int) -> dict[str, float]:
        """Predict probability of each appliance running at given hour.
        
        Returns: {"disk": 0.8, "tvatt": 0.1, "tork": 0.05}
        """
        risks = {}
        for app in ("disk", "tvatt", "tork"):
            key = f"app_{app}_{weekday}_{hour}"
            samples = len(self.history.get(key, []))
            # Total nights tracked for this weekday
            total_key = f"app_total_{weekday}"
            total = len(self.history.get(total_key, [])) or 30
            risks[app] = min(1.0, samples / max(1, total))
        return risks

    def get_disk_typical_hours(self, weekday: int) -> list[int]:
        """Return hours where disk runs > 30% of the time."""
        result = []
        for h in range(24):
            risk = self.predict_appliance_risk(h, weekday)
            if risk.get("disk", 0) > 0.3:
                result.append(h)
        return result or [23, 0]  # Fallback

    def add_breach_event(self, hour: int, weekday: int, goal: str, excess_kw: float) -> None:
        """Learn from goal breaches to avoid them in future."""
        key = f"breach_{goal}_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(excess_kw)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def add_plan_feedback(self, hour: int, weekday: int,
                         planned_kw: float, actual_kw: float) -> None:
        """Learn from plan vs actual deviation.
        
        Over time, adjusts predictions to be more accurate.
        """
        key = f"feedback_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        # Store ratio: actual/planned (>1 = underestimated, <1 = overestimated)
        if planned_kw > 0.5:
            ratio = actual_kw / planned_kw
            self.history[key].append(ratio)
            if len(self.history[key]) > 30:
                self.history[key] = self.history[key][-30:]

    def get_correction_factor(self, hour: int, weekday: int) -> float:
        """Get learned correction factor for this hour/weekday.
        
        Returns multiplier: 1.0 = accurate, 1.2 = typically 20% higher than predicted.
        """
        key = f"feedback_{weekday}_{hour}"
        ratios = self.history.get(key, [])
        if len(ratios) < 5:
            return 1.0  # Not enough data
        # Weighted average of recent ratios (newer = heavier)
        weights = [math.exp(i * 0.15) for i in range(len(ratios))]
        total_w = sum(weights)
        return sum(r * w for r, w in zip(ratios, weights)) / total_w

    def add_temperature_sample(self, temp_c: float, consumption_kw: float, hour: int) -> None:
        """Learn temperature → consumption correlation."""
        # Bucket temperature in 5°C ranges
        bucket = int(temp_c / 5) * 5
        key = f"temp_{bucket}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(consumption_kw)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def get_temp_adjustment(self, temp_c: float, hour: int) -> float:
        """Get temperature-based consumption adjustment.
        
        Returns predicted kW adjustment (positive = more consumption).
        """
        bucket = int(temp_c / 5) * 5
        key = f"temp_{bucket}_{hour}"
        samples = self.history.get(key, [])
        if len(samples) < 3:
            return 0.0  # Not enough data
        avg_at_temp = sum(samples) / len(samples)
        # Compare to overall average at this hour
        overall_key = f"temp_10_{hour}"  # 10°C as baseline
        baseline = self.history.get(overall_key, [])
        if not baseline:
            return 0.0
        avg_baseline = sum(baseline) / len(baseline)
        return avg_at_temp - avg_baseline  # + = more consumption at this temp

    def add_ev_usage(self, weekday: int, soc_change_pct: float) -> None:
        """Learn daily EV usage patterns (SoC drop per day)."""
        key = f"ev_usage_{weekday}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(abs(soc_change_pct))
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def predict_ev_usage(self, weekday: int) -> float:
        """Predict how much EV SoC will drop today (%)."""
        key = f"ev_usage_{weekday}"
        samples = self.history.get(key, [])
        if len(samples) < 3:
            return 10.0  # Default 10% drop
        return sum(samples) / len(samples)

    def add_battery_cycle(self, charge_price_ore: float, discharge_price_ore: float, kwh: float) -> None:
        """Learn battery cycle economics.
        
        Tracks: at what price spread is cycling profitable?
        Over time: knows optimal charge/discharge thresholds.
        """
        spread = discharge_price_ore - charge_price_ore
        profit_kr = kwh * spread / 100
        key = "bat_cycles"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append({
            "spread": spread,
            "profit_kr": profit_kr,
            "kwh": kwh,
        })
        if len(self.history[key]) > 90:  # 90 days
            self.history[key] = self.history[key][-90:]

    def get_battery_economics(self) -> dict:
        """Get learned battery economics.
        
        Returns: avg profit/kWh, best hours to charge/discharge,
        minimum profitable spread.
        """
        cycles = self.history.get("bat_cycles", [])
        if len(cycles) < 7:
            return {"avg_profit_per_kwh_kr": 0.5, "min_spread_ore": 20, "learned": False}
        
        profitable = [c for c in cycles if c["profit_kr"] > 0]
        unprofitable = [c for c in cycles if c["profit_kr"] <= 0]
        
        avg_profit = sum(c["profit_kr"] for c in profitable) / max(1, len(profitable))
        avg_kwh = sum(c["kwh"] for c in profitable) / max(1, len(profitable))
        
        # Find minimum spread that was profitable
        spreads = sorted([c["spread"] for c in profitable])
        min_profitable_spread = spreads[len(spreads) // 5] if spreads else 20  # 20th percentile
        
        return {
            "avg_profit_per_kwh_kr": round(avg_profit / max(0.1, avg_kwh), 3),
            "min_spread_ore": round(max(5, min_profitable_spread), 1),
            "total_cycles": len(cycles),
            "profitable_pct": round(len(profitable) / max(1, len(cycles)) * 100, 1),
            "total_profit_kr": round(sum(c["profit_kr"] for c in cycles), 2),
            "learned": True,
        }

    def add_idle_penalty(self, hours_idle: int, missed_spread_ore: float) -> None:
        """Track cost of NOT cycling battery when spread existed.
        
        Teaches ML that idle batteries during high spread = lost money.
        """
        key = "bat_idle_cost"
        if key not in self.history:
            self.history[key] = []
        # Estimated lost profit: available_kwh × spread / 100
        self.history[key].append({
            "hours": hours_idle,
            "missed_spread": missed_spread_ore,
        })
        if len(self.history[key]) > 90:
            self.history[key] = self.history[key][-90:]

    def should_cycle_battery(self, current_spread_ore: float, available_kwh: float) -> dict:
        """ML recommendation: should we cycle battery now?
        
        Based on learned economics:
        - If spread > min_profitable_spread → YES, cycle
        - If spread < min → NO, wait for better opportunity
        - Confidence increases with more data
        """
        econ = self.get_battery_economics()
        min_spread = econ.get("min_spread_ore", 20)
        
        if current_spread_ore >= min_spread:
            expected_profit = available_kwh * current_spread_ore / 100
            return {
                "recommend": "cycle",
                "confidence": min(0.95, econ["total_cycles"] / 100),
                "expected_profit_kr": round(expected_profit, 2),
                "reason": f"spread {current_spread_ore:.0f} >= learned min {min_spread:.0f} ore",
            }
        else:
            return {
                "recommend": "wait",
                "confidence": min(0.95, econ["total_cycles"] / 100),
                "reason": f"spread {current_spread_ore:.0f} < learned min {min_spread:.0f} ore",
            }

    def get_breach_risk_hours(self, weekday: int, goal: str = "ellevio") -> list[int]:
        """Return hours with history of goal breaches."""
        result = []
        for h in range(24):
            key = f"breach_{goal}_{weekday}_{h}"
            if len(self.history.get(key, [])) >= 2:  # 2+ breaches = risky
                result.append(h)
        return result

        predictions = []
        for i in range(24):
            h = (start_hour + i) % 24
            # Weekday advances at midnight
            d = weekday if (start_hour + i) < 24 else (weekday + 1) % 7
            pred = self.predict_hour(d, h, month)
            predictions.append(pred)

        return predictions

    # ── Battery Economics (IT-2378) ──────────────────────────────

    def add_idle_penalty(
        self,
        hour: int,
        weekday: int,
        idle_minutes: int,
        price_spread_ore: float,
    ) -> None:
        """Record a battery idle penalty — batteries sat idle when spread was large."""
        key = f"idle_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        # Store as negative kW to distinguish from consumption samples
        penalty = -(idle_minutes * price_spread_ore / 60)  # öre wasted
        self.history[key].append(penalty)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def add_battery_cycle(
        self,
        hour: int,
        weekday: int,
        charge_kwh: float,
        discharge_kwh: float,
        price_ore: float,
    ) -> None:
        """Record a battery charge/discharge cycle for economics tracking."""
        key = f"cycle_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        # Store net value: discharge revenue - charge cost
        net = (discharge_kwh - charge_kwh) * price_ore / 100  # SEK
        self.history[key].append(net)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def should_cycle_battery(self, hour: int, weekday: int) -> bool:
        """Returns True if historical data suggests battery cycling is profitable at this hour."""
        key = f"cycle_{weekday}_{hour}"
        samples = self.history.get(key, [])
        if len(samples) < 5:
            return True  # Default: cycle if unsure
        recent = samples[-7:]
        return sum(recent) / len(recent) > 0

    def get_battery_economics(self) -> dict[str, float]:
        """Return summary of battery economics from learned data."""
        total_cycle_value = 0.0
        total_idle_penalty = 0.0
        cycle_count = 0
        idle_count = 0
        for key, vals in self.history.items():
            if key.startswith("cycle_"):
                total_cycle_value += sum(vals)
                cycle_count += len(vals)
            elif key.startswith("idle_"):
                total_idle_penalty += sum(vals)  # Already negative
                idle_count += len(vals)
        return {
            "cycle_value_sek": round(total_cycle_value, 2),
            "cycle_count": cycle_count,
            "idle_penalty_ore": round(abs(total_idle_penalty), 1),
            "idle_count": idle_count,
        }

    # ── IT-2380: Extended ML Learning Methods ─────────────────────

    def add_appliance_event(
        self,
        category: str,
        power_kw: float,
        hour: int,
        weekday: int,
    ) -> None:
        """Record an appliance event (dish/tvätt/tork) for pattern learning.

        Called when appliance power > 500W. Stores power per weekday×hour
        to learn when appliances typically run.
        """
        key = f"appl_{category}_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(power_kw)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def add_plan_feedback(
        self,
        hour: int,
        planned_kw: float,
        actual_kw: float,
    ) -> None:
        """Record planned vs actual consumption for correction factor learning.

        Called once per hour. Stores ratio actual/planned to build
        a per-hour correction profile.
        """
        if planned_kw <= 0.1:
            return
        ratio = actual_kw / planned_kw
        # Clamp to reasonable range to avoid outlier corruption
        ratio = max(0.3, min(3.0, ratio))
        key = f"plan_fb_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(ratio)
        if len(self.history[key]) > 60:
            self.history[key] = self.history[key][-60:]

    def add_temperature_sample(
        self,
        hour: int,
        outdoor_temp_c: float,
        consumption_kw: float,
    ) -> None:
        """Record outdoor temperature → consumption correlation.

        Groups by temperature bands (5°C buckets) per hour to learn
        how temperature affects consumption at different times of day.
        """
        band = int(outdoor_temp_c // 5) * 5  # e.g. -10, -5, 0, 5, 10, 15, 20, 25
        key = f"temp_{band}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(consumption_kw)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def add_ev_usage(
        self,
        weekday: int,
        soc_delta_pct: float,
        capacity_kwh: float,
    ) -> None:
        """Record daily EV energy usage (SoC change × capacity).

        Called once per day at midnight or when day changes.
        """
        kwh_used = abs(soc_delta_pct) / 100.0 * capacity_kwh
        key = f"ev_{weekday}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(kwh_used)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    def add_breach_event(
        self,
        hour: int,
        weekday: int,
        excess_kw: float,
    ) -> None:
        """Record an Ellevio target breach for risk pattern learning."""
        key = f"breach_{weekday}_{hour}"
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(excess_kw)
        if len(self.history[key]) > 30:
            self.history[key] = self.history[key][-30:]

    # ── IT-2380: Prediction Methods for Scheduler ─────────────────

    def predict_appliance_risk(self, hour: int, weekday: int | None = None) -> float:
        """Probability (0-1) that a major appliance runs at this hour.

        Checks all weekdays if weekday is None, else specific day.
        Returns 0.0 if no data.
        """
        total_events = 0
        total_days = 0
        for day in range(7) if weekday is None else [weekday]:
            for cat in ("disk", "tvatt", "tork"):
                key = f"appl_{cat}_{day}_{hour}"
                samples = self.history.get(key, [])
                total_events += len(samples)
            total_days += 1

        if total_days == 0:
            return 0.0
        # Normalize: assume 30 days of data per weekday
        max_possible = total_days * 30
        return min(1.0, total_events / max_possible) if max_possible > 0 else 0.0

    def get_correction_factor(self, hour: int) -> float:
        """Plan accuracy multiplier for this hour.

        Returns ratio > 1 if plans consistently underestimate,
        < 1 if overestimate. Returns 1.0 if insufficient data.
        """
        key = f"plan_fb_{hour}"
        samples = self.history.get(key, [])
        if len(samples) < 5:
            return 1.0
        # Weighted average of recent ratios
        recent = samples[-14:]  # Last 2 weeks
        weights = [math.exp(i * 0.15) for i in range(len(recent))]
        total_w = sum(weights)
        avg = sum(s * w for s, w in zip(recent, weights, strict=False)) / total_w
        return round(max(0.5, min(2.0, avg)), 3)

    def get_temp_adjustment(self, hour: int, outdoor_temp_c: float) -> float:
        """Consumption adjustment factor for current temperature.

        Compares consumption at current temp band vs baseline (15°C).
        Returns multiplier (e.g. 1.3 = 30% more consumption than baseline).
        """
        band = int(outdoor_temp_c // 5) * 5
        baseline_band = 15  # 15-20°C = baseline

        key_current = f"temp_{band}_{hour}"
        key_baseline = f"temp_{baseline_band}_{hour}"

        current_samples = self.history.get(key_current, [])
        baseline_samples = self.history.get(key_baseline, [])

        if len(current_samples) < 3 or len(baseline_samples) < 3:
            return 1.0

        avg_current = sum(current_samples[-10:]) / len(current_samples[-10:])
        avg_baseline = sum(baseline_samples[-10:]) / len(baseline_samples[-10:])

        if avg_baseline <= 0.1:
            return 1.0

        ratio = avg_current / avg_baseline
        return round(max(0.5, min(3.0, ratio)), 3)

    def predict_ev_usage(self, weekday: int) -> float:
        """Predicted daily EV energy usage (kWh) for this weekday.

        Returns 0.0 if no data.
        """
        key = f"ev_{weekday}"
        samples = self.history.get(key, [])
        if not samples:
            return 0.0
        # Weighted average favoring recent
        recent = samples[-10:]
        weights = [math.exp(i * 0.2) for i in range(len(recent))]
        total_w = sum(weights)
        return round(sum(s * w for s, w in zip(recent, weights, strict=False)) / total_w, 1)

    def get_breach_risk_hours(self, weekday: int | None = None) -> list[int]:
        """Hours with historically high breach risk (sorted by risk, highest first).

        Returns up to 6 hours that have had breaches.
        """
        risk: dict[int, float] = {}
        for key, vals in self.history.items():
            if not key.startswith("breach_"):
                continue
            parts = key.split("_")
            if len(parts) != 3:
                continue
            day, hour = int(parts[1]), int(parts[2])
            if weekday is not None and day != weekday:
                continue
            # Risk score = count × average excess
            avg_excess = sum(vals) / len(vals) if vals else 0
            risk[hour] = risk.get(hour, 0) + len(vals) * max(0.1, avg_excess)

        sorted_hours = sorted(risk.keys(), key=lambda h: risk[h], reverse=True)
        return sorted_hours[:6]

    def get_disk_typical_hours(self) -> list[int]:
        """Hours when dishwasher/appliances typically run (sorted by frequency).

        Returns list of hours with at least 3 recorded events.
        """
        hour_counts: dict[int, int] = {}
        for key, vals in self.history.items():
            if not key.startswith("appl_disk_"):
                continue
            parts = key.split("_")
            if len(parts) != 4:
                continue
            hour = int(parts[3])
            hour_counts[hour] = hour_counts.get(hour, 0) + len(vals)

        qualified = {h: c for h, c in hour_counts.items() if c >= 3}
        return sorted(qualified.keys(), key=lambda h: qualified[h], reverse=True)

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

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "history": self.history,
            "total_samples": self.total_samples,
            "seasonal_factor": self.seasonal_factor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionPredictor:
        """Deserialize from storage."""
        pred = cls()
        hist = data.get("history")
        if isinstance(hist, dict):
            pred.history = hist
        pred.total_samples = int(data.get("total_samples", 0))
        sf = data.get("seasonal_factor")
        if isinstance(sf, dict):
            pred.seasonal_factor = {int(k): float(v) for k, v in sf.items()}
        return pred
