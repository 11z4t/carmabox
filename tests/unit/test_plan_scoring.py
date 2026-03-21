"""Tests for CARMA Box plan scoring module (PLAT-963)."""

from custom_components.carmabox.optimizer.models import HourActual
from custom_components.carmabox.optimizer.plan_scoring import (
    DayScore,
    ScoreHistory,
    history_from_dict,
    history_to_dict,
    record_day_score,
    score_day,
    score_hour,
    summary,
    trend,
    worst_hours,
)


class TestScoreHour:
    """Test hourly scoring."""

    def test_perfect_match(self):
        actual = HourActual(
            hour=12,
            planned_action="d",
            actual_action="d",
            planned_grid_kw=2.0,
            actual_grid_kw=2.0,
            planned_weighted_kw=2.0,
            actual_weighted_kw=2.0,
            planned_battery_soc=50,
            actual_battery_soc=50,
        )
        hs = score_hour(actual)
        assert hs.score == 100.0
        assert hs.action_match is True
        assert hs.grid_error_kw == 0.0

    def test_wrong_action(self):
        actual = HourActual(
            hour=12,
            planned_action="d",
            actual_action="i",
            planned_grid_kw=2.0,
            actual_grid_kw=2.0,
            planned_battery_soc=50,
            actual_battery_soc=50,
        )
        hs = score_hour(actual)
        assert hs.score == 70.0  # Lost 30 points for action mismatch
        assert hs.action_match is False

    def test_grid_error_penalty(self):
        actual = HourActual(
            hour=12,
            planned_action="d",
            actual_action="d",
            planned_grid_kw=1.0,
            actual_grid_kw=4.0,  # 3 kW error = max penalty
            planned_battery_soc=50,
            actual_battery_soc=50,
        )
        hs = score_hour(actual)
        assert hs.grid_error_kw == 3.0
        assert hs.score < 70  # Lost grid points

    def test_soc_error_penalty(self):
        actual = HourActual(
            hour=12,
            planned_action="d",
            actual_action="d",
            planned_grid_kw=2.0,
            actual_grid_kw=2.0,
            planned_battery_soc=50,
            actual_battery_soc=30,  # 20% error = max penalty
        )
        hs = score_hour(actual)
        assert hs.soc_error_pct == -20.0
        assert hs.score < 80


class TestScoreDay:
    """Test daily scoring."""

    def test_empty_actuals(self):
        ds = score_day([], "2026-03-21")
        assert ds.overall_score == 0.0

    def test_full_day_perfect(self):
        actuals = [
            HourActual(
                hour=h,
                planned_action="i",
                actual_action="i",
                planned_grid_kw=2.0,
                actual_grid_kw=2.0,
                planned_weighted_kw=2.0,
                actual_weighted_kw=2.0,
                planned_battery_soc=50,
                actual_battery_soc=50,
            )
            for h in range(24)
        ]
        ds = score_day(actuals, "2026-03-21")
        assert ds.overall_score == 100.0
        assert ds.action_accuracy_pct == 100.0
        assert ds.grid_mae_kw == 0.0

    def test_mixed_accuracy(self):
        actuals = []
        for h in range(24):
            # Half hours match, half don't
            actual_action = "d" if h < 12 else "i"
            actuals.append(
                HourActual(
                    hour=h,
                    planned_action="d",
                    actual_action=actual_action,
                    planned_grid_kw=2.0,
                    actual_grid_kw=2.5,
                    planned_battery_soc=50,
                    actual_battery_soc=48,
                )
            )
        ds = score_day(actuals, "2026-03-21")
        assert ds.action_accuracy_pct == 50.0
        assert ds.overall_score > 0
        assert ds.hours_scored == 24


class TestScoreHistory:
    """Test score history tracking."""

    def test_record_day(self):
        history = ScoreHistory()
        ds = DayScore(date="2026-03-21", overall_score=75.0)
        record_day_score(history, ds)
        assert len(history.daily_scores) == 1

    def test_updates_ema(self):
        history = ScoreHistory(ema_score=50.0)
        ds = DayScore(date="2026-03-21", overall_score=90.0)
        record_day_score(history, ds)
        assert history.ema_score > 50.0

    def test_max_90_days(self):
        history = ScoreHistory()
        for i in range(100):
            ds = DayScore(date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", overall_score=70.0)
            record_day_score(history, ds)
        assert len(history.daily_scores) <= 90

    def test_trend_insufficient(self):
        history = ScoreHistory()
        assert trend(history) == "insufficient_data"

    def test_trend_improving(self):
        history = ScoreHistory()
        for i in range(7):
            ds = DayScore(date=f"2026-03-{i + 1:02d}", overall_score=50.0)
            record_day_score(history, ds)
        for i in range(7):
            ds = DayScore(date=f"2026-03-{i + 8:02d}", overall_score=80.0)
            record_day_score(history, ds)
        assert trend(history) == "improving"

    def test_trend_declining(self):
        history = ScoreHistory()
        for i in range(7):
            ds = DayScore(date=f"2026-03-{i + 1:02d}", overall_score=80.0)
            record_day_score(history, ds)
        for i in range(7):
            ds = DayScore(date=f"2026-03-{i + 8:02d}", overall_score=50.0)
            record_day_score(history, ds)
        assert trend(history) == "declining"


class TestWorstHours:
    """Test worst hours analysis."""

    def test_no_data(self):
        history = ScoreHistory()
        assert worst_hours(history) == []

    def test_identifies_problematic_hours(self):
        history = ScoreHistory()
        from custom_components.carmabox.optimizer.plan_scoring import HourScore

        for day in range(10):
            hour_scores = []
            for h in range(24):
                err = 5.0 if h == 18 else 0.5  # Hour 18 always bad
                hour_scores.append(
                    HourScore(
                        hour=h,
                        action_match=True,
                        grid_error_kw=err,
                        weighted_error_kw=err,
                        soc_error_pct=0,
                        score=50,
                    )
                )
            ds = DayScore(
                date=f"2026-03-{day + 1:02d}",
                overall_score=60.0,
                hour_scores=hour_scores,
            )
            record_day_score(history, ds)

        wh = worst_hours(history, top_n=3)
        assert len(wh) == 3
        assert wh[0]["hour"] == 18


class TestSummary:
    """Test summary function."""

    def test_empty(self):
        history = ScoreHistory()
        s = summary(history)
        assert s["current_score"] == 0
        assert s["trend"] == "insufficient_data"

    def test_with_data(self):
        history = ScoreHistory()
        for i in range(20):
            ds = DayScore(
                date=f"2026-03-{i + 1:02d}",
                overall_score=75.0,
                action_accuracy_pct=80.0,
                grid_mae_kw=0.5,
            )
            record_day_score(history, ds)
        s = summary(history)
        assert s["current_score"] > 0
        assert s["days_tracked"] == 20


class TestSerialization:
    """Test serialization."""

    def test_roundtrip(self):
        history = ScoreHistory()
        for i in range(5):
            ds = DayScore(
                date=f"2026-03-{i + 1:02d}",
                overall_score=70.0 + i,
                action_accuracy_pct=80.0,
                grid_mae_kw=0.5,
                weighted_mae_kw=0.4,
                soc_mae_pct=3.0,
                peak_error_kw=0.2,
                hours_scored=24,
            )
            record_day_score(history, ds)

        data = history_to_dict(history)
        h2 = history_from_dict(data)

        assert len(h2.daily_scores) == 5
        assert abs(h2.ema_score - history.ema_score) < 0.01

    def test_from_empty(self):
        h = history_from_dict({})
        assert len(h.daily_scores) == 0

    def test_from_none(self):
        h = history_from_dict(None)
        assert len(h.daily_scores) == 0
