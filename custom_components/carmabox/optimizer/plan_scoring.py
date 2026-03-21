"""CARMA Box — Plan vs Actual Scoring (PLAT-963).

Pure Python. No HA imports. Fully testable.

Scores how well the plan matched reality. Tracks improvement over time.
Provides actionable insights on where plans deviate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import HourActual


@dataclass
class HourScore:
    """Score for a single hour."""

    hour: int
    action_match: bool  # Did planned action match actual?
    grid_error_kw: float  # actual_grid - planned_grid
    weighted_error_kw: float  # actual_weighted - planned_weighted
    soc_error_pct: float  # actual_soc - planned_soc
    score: float  # 0-100 composite score


@dataclass
class DayScore:
    """Aggregate score for one day."""

    date: str  # ISO date
    overall_score: float = 0.0  # 0-100
    action_accuracy_pct: float = 0.0  # % hours where action matched
    grid_mae_kw: float = 0.0  # Mean Absolute Error of grid import
    weighted_mae_kw: float = 0.0  # MAE of weighted grid import
    soc_mae_pct: float = 0.0  # MAE of SoC prediction
    peak_error_kw: float = 0.0  # Error at peak hour
    hours_scored: int = 0
    hour_scores: list[HourScore] = field(default_factory=list)


@dataclass
class ScoreHistory:
    """Historical scores for trend tracking."""

    daily_scores: list[DayScore] = field(default_factory=list)

    # EMA of overall score for smooth trend
    ema_score: float = 50.0
    ema_alpha: float = 0.1


def score_hour(actual: HourActual) -> HourScore:
    """Score a single hour's plan vs actual.

    Args:
        actual: HourActual with both planned and actual values filled.

    Returns:
        HourScore with composite score 0-100.
    """
    action_match = actual.planned_action == actual.actual_action

    grid_err = actual.actual_grid_kw - actual.planned_grid_kw
    weighted_err = actual.actual_weighted_kw - actual.planned_weighted_kw
    soc_err = actual.actual_battery_soc - actual.planned_battery_soc

    # Composite score (100 = perfect)
    # Action match: 30 points
    # Grid accuracy: 40 points (penalize proportionally)
    # SoC accuracy: 30 points

    action_pts = 30.0 if action_match else 0.0

    # Grid accuracy: full points at 0 error, 0 at 3+ kW error
    grid_penalty = min(1.0, abs(grid_err) / 3.0)
    grid_pts = 40.0 * (1 - grid_penalty)

    # SoC accuracy: full points at 0% error, 0 at 20%+ error
    soc_penalty = min(1.0, abs(soc_err) / 20.0)
    soc_pts = 30.0 * (1 - soc_penalty)

    score = round(action_pts + grid_pts + soc_pts, 1)

    return HourScore(
        hour=actual.hour,
        action_match=action_match,
        grid_error_kw=round(grid_err, 2),
        weighted_error_kw=round(weighted_err, 2),
        soc_error_pct=round(soc_err, 1),
        score=score,
    )


def score_day(actuals: list[HourActual], date_str: str) -> DayScore:
    """Score a full day's plan vs actual.

    Args:
        actuals: List of HourActual for each hour of the day.
        date_str: ISO date string.

    Returns:
        DayScore with aggregate metrics.
    """
    if not actuals:
        return DayScore(date=date_str)

    hour_scores = [score_hour(a) for a in actuals]

    # Aggregate
    action_matches = sum(1 for hs in hour_scores if hs.action_match)
    action_accuracy = action_matches / len(hour_scores) * 100

    grid_mae = sum(abs(hs.grid_error_kw) for hs in hour_scores) / len(hour_scores)
    weighted_mae = sum(abs(hs.weighted_error_kw) for hs in hour_scores) / len(hour_scores)
    soc_mae = sum(abs(hs.soc_error_pct) for hs in hour_scores) / len(hour_scores)

    # Peak hour error (highest planned weighted)
    peak_hour = max(actuals, key=lambda a: a.planned_weighted_kw)
    peak_err = peak_hour.actual_weighted_kw - peak_hour.planned_weighted_kw

    overall = sum(hs.score for hs in hour_scores) / len(hour_scores)

    return DayScore(
        date=date_str,
        overall_score=round(overall, 1),
        action_accuracy_pct=round(action_accuracy, 1),
        grid_mae_kw=round(grid_mae, 2),
        weighted_mae_kw=round(weighted_mae, 2),
        soc_mae_pct=round(soc_mae, 1),
        peak_error_kw=round(peak_err, 2),
        hours_scored=len(hour_scores),
        hour_scores=hour_scores,
    )


def record_day_score(history: ScoreHistory, day_score: DayScore) -> None:
    """Record a day's score in history.

    Updates EMA trend score and keeps max 90 days.
    """
    # Update or append
    for i, ds in enumerate(history.daily_scores):
        if ds.date == day_score.date:
            history.daily_scores[i] = day_score
            return

    history.daily_scores.append(day_score)
    if len(history.daily_scores) > 90:
        history.daily_scores = history.daily_scores[-90:]

    # Update EMA
    history.ema_score = (
        history.ema_alpha * day_score.overall_score + (1 - history.ema_alpha) * history.ema_score
    )


def trend(history: ScoreHistory) -> str:
    """Is plan accuracy improving, stable, or declining?

    Compares last 7 days vs previous 7 days.
    """
    scores = history.daily_scores
    if len(scores) < 14:
        return "insufficient_data"

    recent = [s.overall_score for s in scores[-7:]]
    previous = [s.overall_score for s in scores[-14:-7]]

    recent_avg = sum(recent) / len(recent)
    prev_avg = sum(previous) / len(previous)

    if recent_avg > prev_avg + 3:
        return "improving"
    if recent_avg < prev_avg - 3:
        return "declining"
    return "stable"


def worst_hours(history: ScoreHistory, top_n: int = 3) -> list[dict[str, Any]]:
    """Find the hours where plans consistently deviate most.

    Returns the top-N hours with highest average grid error.
    """
    if not history.daily_scores:
        return []

    # Aggregate errors per hour
    hour_errors: dict[int, list[float]] = {}
    for ds in history.daily_scores[-30:]:
        for hs in ds.hour_scores:
            hour_errors.setdefault(hs.hour, []).append(abs(hs.grid_error_kw))

    # Average error per hour
    avg_errors = []
    for h, errors in hour_errors.items():
        avg = sum(errors) / len(errors)
        avg_errors.append({"hour": h, "avg_grid_error_kw": round(avg, 2), "samples": len(errors)})

    avg_errors.sort(key=lambda x: x["avg_grid_error_kw"], reverse=True)
    return avg_errors[:top_n]


def summary(history: ScoreHistory) -> dict[str, Any]:
    """Full scoring summary for dashboard."""
    scores = history.daily_scores
    if not scores:
        return {
            "current_score": 0,
            "trend": "insufficient_data",
            "days_tracked": 0,
        }

    recent = scores[-7:] if len(scores) >= 7 else scores
    avg_score = sum(s.overall_score for s in recent) / len(recent)

    return {
        "current_score": round(avg_score, 1),
        "ema_score": round(history.ema_score, 1),
        "trend": trend(history),
        "days_tracked": len(scores),
        "avg_action_accuracy": round(sum(s.action_accuracy_pct for s in recent) / len(recent), 1),
        "avg_grid_mae_kw": round(sum(s.grid_mae_kw for s in recent) / len(recent), 2),
        "worst_hours": worst_hours(history),
    }


def history_to_dict(history: ScoreHistory) -> dict[str, Any]:
    """Serialize for persistent storage."""
    return {
        "ema_score": history.ema_score,
        "daily_scores": [
            {
                "date": ds.date,
                "overall_score": ds.overall_score,
                "action_accuracy_pct": ds.action_accuracy_pct,
                "grid_mae_kw": ds.grid_mae_kw,
                "weighted_mae_kw": ds.weighted_mae_kw,
                "soc_mae_pct": ds.soc_mae_pct,
                "peak_error_kw": ds.peak_error_kw,
                "hours_scored": ds.hours_scored,
            }
            for ds in history.daily_scores[-90:]
        ],
    }


def history_from_dict(data: dict[str, Any]) -> ScoreHistory:
    """Deserialize from storage."""
    if not data or not isinstance(data, dict):
        return ScoreHistory()
    try:
        daily = [
            DayScore(
                date=str(d["date"]),
                overall_score=float(d.get("overall_score", 0)),
                action_accuracy_pct=float(d.get("action_accuracy_pct", 0)),
                grid_mae_kw=float(d.get("grid_mae_kw", 0)),
                weighted_mae_kw=float(d.get("weighted_mae_kw", 0)),
                soc_mae_pct=float(d.get("soc_mae_pct", 0)),
                peak_error_kw=float(d.get("peak_error_kw", 0)),
                hours_scored=int(d.get("hours_scored", 0)),
            )
            for d in data.get("daily_scores", [])
            if isinstance(d, dict)
        ]
        return ScoreHistory(
            daily_scores=daily,
            ema_score=float(data.get("ema_score", 50.0)),
        )
    except (KeyError, ValueError, TypeError):
        return ScoreHistory()
