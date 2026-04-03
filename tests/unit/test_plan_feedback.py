"""PLAT-1229: Unit tests for plan_feedback module."""

from __future__ import annotations

import json
import tempfile
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.carmabox.const import (
    EV_DAILY_ROLLING_DAYS,
    FEEDBACK_RETENTION_DAYS,
)
from custom_components.carmabox.optimizer.plan_feedback import (
    FeedbackData,
    HourRecord,
    PlanFeedback,
    _is_night_hour,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _ts(days_ago: float = 0.0) -> datetime:
    return _now() - timedelta(days=days_ago)


def _record(
    pf: PlanFeedback,
    *,
    hour: int = 10,
    device: str = "house",
    planned: float = 1.0,
    actual: float = 1.0,
) -> None:
    """Helper: append a record with a synthetic timestamp via public API."""
    pf.record_actual(hour=hour, device=device, planned_kwh=planned, actual_kwh=actual)


# ── _is_night_hour ────────────────────────────────────────────────────────────


class TestIsNightHour:
    def test_midnight_is_night(self) -> None:
        assert _is_night_hour(0) is True

    def test_early_morning_is_night(self) -> None:
        assert _is_night_hour(5) is True

    def test_morning_is_day(self) -> None:
        assert _is_night_hour(6) is False

    def test_afternoon_is_day(self) -> None:
        assert _is_night_hour(14) is False

    def test_late_evening_is_night(self) -> None:
        assert _is_night_hour(22) is True

    def test_23_is_night(self) -> None:
        assert _is_night_hour(23) is True


# ── record_actual ─────────────────────────────────────────────────────────────


class TestRecordActual:
    def test_appends_record(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=2.0, actual=1.8)
        assert len(pf._history) == 1

    def test_record_values_preserved(self) -> None:
        pf = PlanFeedback()
        pf.record_actual(hour=14, device="ev", planned_kwh=3.0, actual_kwh=2.5)
        r = pf._history[0]
        assert r.hour == 14
        assert r.device == "ev"
        assert r.planned_kwh == 3.0
        assert r.actual_kwh == 2.5

    def test_multiple_records_accumulate(self) -> None:
        pf = PlanFeedback()
        for i in range(5):
            _record(pf, planned=float(i), actual=float(i))
        assert len(pf._history) == 5

    def test_timestamp_is_recent(self) -> None:
        pf = PlanFeedback()
        before = _now()
        _record(pf)
        after = _now()
        ts = pf._history[0].timestamp
        assert before <= ts <= after


# ── prune_old ────────────────────────────────────────────────────────────────


class TestPruneOld:
    def test_fresh_record_kept(self) -> None:
        pf = PlanFeedback()
        _record(pf)
        pf.prune_old()
        assert len(pf._history) == 1

    def test_old_record_removed(self) -> None:
        pf = PlanFeedback()
        _record(pf)
        # Manually backdate the timestamp past retention window
        old_ts = _now() - timedelta(days=FEEDBACK_RETENTION_DAYS + 1)
        pf._history[0] = HourRecord(
            hour=pf._history[0].hour,
            device=pf._history[0].device,
            planned_kwh=pf._history[0].planned_kwh,
            actual_kwh=pf._history[0].actual_kwh,
            timestamp=old_ts,
        )
        pf.prune_old()
        assert len(pf._history) == 0

    def test_mixed_keeps_only_recent(self) -> None:
        pf = PlanFeedback()
        # Fresh record
        pf.record_actual(hour=10, device="house", planned_kwh=1.0, actual_kwh=1.0)
        # Backdate first record to 31 days ago
        old_ts = _now() - timedelta(days=FEEDBACK_RETENTION_DAYS + 1)
        pf._history[0] = HourRecord(
            hour=pf._history[0].hour,
            device=pf._history[0].device,
            planned_kwh=pf._history[0].planned_kwh,
            actual_kwh=pf._history[0].actual_kwh,
            timestamp=old_ts,
        )
        # Add a fresh one
        pf.record_actual(hour=11, device="house", planned_kwh=1.0, actual_kwh=1.0)
        pf.prune_old()
        assert len(pf._history) == 1
        assert pf._history[0].hour == 11


# ── compare_to_plan ───────────────────────────────────────────────────────────


class TestCompareToPlan:
    def test_empty_history(self) -> None:
        pf = PlanFeedback()
        result = pf.compare_to_plan()
        assert result["total_planned_kwh"] == 0.0
        assert result["total_actual_kwh"] == 0.0
        assert result["delta_kwh"] == 0.0

    def test_sum_is_correct(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=2.0, actual=1.5)
        _record(pf, planned=3.0, actual=3.0)
        result = pf.compare_to_plan()
        assert result["total_planned_kwh"] == pytest.approx(5.0)
        assert result["total_actual_kwh"] == pytest.approx(4.5)
        assert result["delta_kwh"] == pytest.approx(-0.5)

    def test_delta_positive_when_over(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=1.0, actual=2.0)
        assert pf.compare_to_plan()["delta_kwh"] == pytest.approx(1.0)


# ── get_feedback_data ─────────────────────────────────────────────────────────


class TestGetFeedbackData:
    def test_empty_returns_defaults(self) -> None:
        pf = PlanFeedback()
        fd = pf.get_feedback_data()
        assert fd.ev_daily_kwh_estimate == 0.0
        assert fd.house_baseload_day_kw == 0.0
        assert fd.house_baseload_night_kw == 0.0
        assert fd.plan_accuracy_pct == 0.0
        assert isinstance(fd.last_updated, datetime)

    def test_feedback_data_is_frozen(self) -> None:
        fd = FeedbackData(
            ev_daily_kwh_estimate=1.0,
            house_baseload_day_kw=2.0,
            house_baseload_night_kw=1.0,
            plan_accuracy_pct=80.0,
            last_updated=_now(),
        )
        with pytest.raises(FrozenInstanceError):
            fd.ev_daily_kwh_estimate = 99.0  # type: ignore[misc]

    def test_baseload_day_computed(self) -> None:
        pf = PlanFeedback()
        # Day hours
        _record(pf, hour=10, actual=2.0)
        _record(pf, hour=14, actual=4.0)
        fd = pf.get_feedback_data()
        assert fd.house_baseload_day_kw == pytest.approx(3.0)

    def test_baseload_night_computed(self) -> None:
        pf = PlanFeedback()
        _record(pf, hour=0, actual=1.0)
        _record(pf, hour=23, actual=3.0)
        fd = pf.get_feedback_data()
        assert fd.house_baseload_night_kw == pytest.approx(2.0)

    def test_baseload_day_and_night_separate(self) -> None:
        pf = PlanFeedback()
        _record(pf, hour=10, actual=4.0)  # day
        _record(pf, hour=2, actual=1.0)  # night
        fd = pf.get_feedback_data()
        assert fd.house_baseload_day_kw == pytest.approx(4.0)
        assert fd.house_baseload_night_kw == pytest.approx(1.0)


# ── plan_accuracy ─────────────────────────────────────────────────────────────


class TestPlanAccuracy:
    def test_all_accurate(self) -> None:
        pf = PlanFeedback()
        # actual within 20% of planned
        _record(pf, planned=1.0, actual=1.0)
        _record(pf, planned=1.0, actual=1.19)
        fd = pf.get_feedback_data()
        assert fd.plan_accuracy_pct == pytest.approx(100.0)

    def test_none_accurate(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=1.0, actual=2.0)  # 100% off
        _record(pf, planned=1.0, actual=0.0)  # 100% off
        fd = pf.get_feedback_data()
        assert fd.plan_accuracy_pct == pytest.approx(0.0)

    def test_partial_accuracy(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=1.0, actual=1.0)  # accurate
        _record(pf, planned=1.0, actual=1.0)  # accurate
        _record(pf, planned=1.0, actual=2.0)  # inaccurate
        _record(pf, planned=1.0, actual=2.0)  # inaccurate
        fd = pf.get_feedback_data()
        assert fd.plan_accuracy_pct == pytest.approx(50.0)

    def test_zero_planned_uses_floor(self) -> None:
        pf = PlanFeedback()
        # planned=0 → floor 0.1; actual=0.05 → |0-0.05|/0.1 = 0.5 > 0.20 → inaccurate
        _record(pf, planned=0.0, actual=0.05)
        fd = pf.get_feedback_data()
        assert fd.plan_accuracy_pct == pytest.approx(0.0)

    def test_zero_planned_small_actual_accurate(self) -> None:
        pf = PlanFeedback()
        # planned=0 → floor 0.1; actual=0.02 → |0-0.02|/0.1 = 0.2 = 0.20 → accurate
        _record(pf, planned=0.0, actual=0.02)
        fd = pf.get_feedback_data()
        assert fd.plan_accuracy_pct == pytest.approx(100.0)


# ── update_ev_daily ───────────────────────────────────────────────────────────


class TestUpdateEvDaily:
    def test_sample_added(self) -> None:
        pf = PlanFeedback()
        pf.update_ev_daily(10.0)
        assert pf._ev_daily_samples == [10.0]

    def test_rolling_window_enforced(self) -> None:
        pf = PlanFeedback()
        # Use identical values so std=0 and outlier filter never rejects
        for _ in range(EV_DAILY_ROLLING_DAYS + 3):
            pf.update_ev_daily(10.0)
        assert len(pf._ev_daily_samples) == EV_DAILY_ROLLING_DAYS

    def test_outlier_rejected(self) -> None:
        pf = PlanFeedback()
        # Build a stable baseline
        for _ in range(5):
            pf.update_ev_daily(10.0)
        # Extreme outlier: mean=10, std≈0, but add clear outlier
        # Use different values so std > 0
        pf._ev_daily_samples = [9.0, 10.0, 11.0, 10.0, 10.0]
        outlier = 100.0  # way above mean(~10) + 2*std(~0.7)
        pf.update_ev_daily(outlier)
        assert outlier not in pf._ev_daily_samples

    def test_normal_value_accepted(self) -> None:
        pf = PlanFeedback()
        pf._ev_daily_samples = [9.0, 10.0, 11.0, 10.0, 10.0]
        pf.update_ev_daily(10.5)
        assert 10.5 in pf._ev_daily_samples

    def test_ev_estimate_is_mean(self) -> None:
        pf = PlanFeedback()
        pf.update_ev_daily(8.0)
        pf.update_ev_daily(12.0)
        fd = pf.get_feedback_data()
        assert fd.ev_daily_kwh_estimate == pytest.approx(10.0)

    def test_single_sample_no_outlier_filter(self) -> None:
        """With fewer than 2 samples, outlier filtering is skipped."""
        pf = PlanFeedback()
        pf.update_ev_daily(999.0)
        assert pf._ev_daily_samples == [999.0]


# ── save / load round-trip ────────────────────────────────────────────────────


class TestSaveLoad:
    def test_round_trip_preserves_history(self) -> None:
        pf = PlanFeedback()
        pf.record_actual(hour=8, device="ev", planned_kwh=5.0, actual_kwh=4.8)
        pf.record_actual(hour=22, device="house", planned_kwh=1.0, actual_kwh=0.9)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            path = tmp.name

        pf.save(path)
        pf2 = PlanFeedback.load(path)

        assert len(pf2._history) == 2
        r = pf2._history[0]
        assert r.hour == 8
        assert r.device == "ev"
        assert r.planned_kwh == pytest.approx(5.0)
        assert r.actual_kwh == pytest.approx(4.8)

    def test_round_trip_preserves_ev_samples(self) -> None:
        pf = PlanFeedback()
        pf.update_ev_daily(10.0)
        pf.update_ev_daily(12.0)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            path = tmp.name

        pf.save(path)
        pf2 = PlanFeedback.load(path)
        assert pf2._ev_daily_samples == pytest.approx([10.0, 12.0])

    def test_load_empty_file(self) -> None:
        """Loading from a file with empty lists must not raise."""
        payload = {"history": [], "ev_daily_samples": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(payload, tmp)
            path = tmp.name

        pf = PlanFeedback.load(path)
        assert pf._history == []
        assert pf._ev_daily_samples == []

    def test_saved_file_is_valid_json(self) -> None:
        pf = PlanFeedback()
        _record(pf, planned=1.0, actual=0.9)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            path = tmp.name

        pf.save(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        assert "history" in data
        assert "ev_daily_samples" in data
        assert len(data["history"]) == 1


# ── HourRecord immutability ───────────────────────────────────────────────────


class TestHourRecord:
    def test_hour_record_is_frozen(self) -> None:
        r = HourRecord(
            hour=10,
            device="house",
            planned_kwh=1.0,
            actual_kwh=1.0,
            timestamp=_now(),
        )
        with pytest.raises(FrozenInstanceError):
            r.hour = 99  # type: ignore[misc]
