"""PLAT-1229: Plan Feedback — tracks planned vs actual energy usage."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.carmabox.const import (
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    EV_DAILY_ROLLING_DAYS,
    FEEDBACK_ACCURACY_TOLERANCE,
    FEEDBACK_PLANNED_FLOOR_KWH,
    FEEDBACK_RETENTION_DAYS,
    OUTLIER_STD_FACTOR,
)


@dataclass(frozen=True)
class FeedbackData:
    """Aggregated feedback metrics derived from recorded actuals."""

    ev_daily_kwh_estimate: float
    house_baseload_day_kw: float
    house_baseload_night_kw: float
    plan_accuracy_pct: float
    last_updated: datetime


@dataclass(frozen=True)
class HourRecord:
    """Single planned-vs-actual record for one hour and device."""

    hour: int
    device: str
    planned_kwh: float
    actual_kwh: float
    timestamp: datetime


def _is_night_hour(hour: int) -> bool:
    """Return True if *hour* falls in the configured night window."""
    return hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END


class PlanFeedback:
    """Accumulates HourRecord history and derives feedback metrics."""

    def __init__(self) -> None:
        self._history: list[HourRecord] = []
        self._ev_daily_samples: list[float] = []

    # ── Public write API ───────────────────────────────────────────────────

    def record_actual(
        self,
        hour: int,
        device: str,
        planned_kwh: float,
        actual_kwh: float,
    ) -> None:
        """Append a new HourRecord and prune stale entries."""
        record = HourRecord(
            hour=hour,
            device=device,
            planned_kwh=planned_kwh,
            actual_kwh=actual_kwh,
            timestamp=datetime.now(tz=UTC),
        )
        self._history.append(record)
        self.prune_old()

    def update_ev_daily(self, kwh: float) -> None:
        """Add an EV daily kWh sample; outliers and window overflow are rejected."""
        if len(self._ev_daily_samples) >= 2:
            mean = statistics.mean(self._ev_daily_samples)
            std = statistics.stdev(self._ev_daily_samples)
            if std > 0 and kwh > mean + OUTLIER_STD_FACTOR * std:
                return
        self._ev_daily_samples.append(kwh)
        if len(self._ev_daily_samples) > EV_DAILY_ROLLING_DAYS:
            self._ev_daily_samples = self._ev_daily_samples[-EV_DAILY_ROLLING_DAYS:]

    def prune_old(self) -> None:
        """Remove HourRecords older than FEEDBACK_RETENTION_DAYS."""
        cutoff = datetime.now(tz=UTC) - timedelta(days=FEEDBACK_RETENTION_DAYS)
        self._history = [r for r in self._history if r.timestamp >= cutoff]

    # ── Public read API ────────────────────────────────────────────────────

    def compare_to_plan(self) -> dict[str, float]:
        """Return a summary of planned vs actual totals across all records."""
        total_planned = sum(r.planned_kwh for r in self._history)
        total_actual = sum(r.actual_kwh for r in self._history)
        return {
            "total_planned_kwh": total_planned,
            "total_actual_kwh": total_actual,
            "delta_kwh": total_actual - total_planned,
        }

    def get_feedback_data(self) -> FeedbackData:
        """Compute and return aggregated FeedbackData from current history."""
        ev_estimate = self._ev_daily_estimate()

        day_kwh = [r.actual_kwh for r in self._history if not _is_night_hour(r.hour)]
        night_kwh = [r.actual_kwh for r in self._history if _is_night_hour(r.hour)]
        baseload_day = statistics.mean(day_kwh) if day_kwh else 0.0
        baseload_night = statistics.mean(night_kwh) if night_kwh else 0.0

        accuracy = self._plan_accuracy()

        return FeedbackData(
            ev_daily_kwh_estimate=ev_estimate,
            house_baseload_day_kw=baseload_day,
            house_baseload_night_kw=baseload_night,
            plan_accuracy_pct=accuracy,
            last_updated=datetime.now(tz=UTC),
        )

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Serialise state to a JSON file at *path*."""
        records: list[dict[str, Any]] = [
            {
                "hour": r.hour,
                "device": r.device,
                "planned_kwh": r.planned_kwh,
                "actual_kwh": r.actual_kwh,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in self._history
        ]
        payload: dict[str, Any] = {
            "history": records,
            "ev_daily_samples": self._ev_daily_samples,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> PlanFeedback:
        """Deserialise state from a JSON file at *path* and return a new instance."""
        with open(path, encoding="utf-8") as fh:
            raw: Any = json.load(fh)

        instance = cls()
        for item in raw.get("history", []):
            ts: datetime = datetime.fromisoformat(item["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            instance._history.append(
                HourRecord(
                    hour=int(item["hour"]),
                    device=str(item["device"]),
                    planned_kwh=float(item["planned_kwh"]),
                    actual_kwh=float(item["actual_kwh"]),
                    timestamp=ts,
                )
            )
        instance._ev_daily_samples = [float(v) for v in raw.get("ev_daily_samples", [])]
        return instance

    # ── Private helpers ────────────────────────────────────────────────────

    def _ev_daily_estimate(self) -> float:
        return statistics.mean(self._ev_daily_samples) if self._ev_daily_samples else 0.0

    def _plan_accuracy(self) -> float:
        if not self._history:
            return 0.0
        accurate = sum(
            1
            for r in self._history
            if abs(r.planned_kwh - r.actual_kwh) / max(r.planned_kwh, FEEDBACK_PLANNED_FLOOR_KWH)
            <= FEEDBACK_ACCURACY_TOLERANCE
        )
        return accurate / len(self._history) * 100.0
