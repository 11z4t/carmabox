"""Tests for monthly report data collector."""

from __future__ import annotations

from datetime import datetime

from custom_components.carmabox.optimizer.report import (
    DailySample,
    ReportCollector,
    generate_report,
    record_daily_sample,
    report_to_dict,
    reset_if_new_month,
)


class TestResetIfNewMonth:
    def test_same_month_keeps(self) -> None:
        c = ReportCollector(month=3, year=2026)
        c.samples.append(DailySample(date="2026-03-01"))
        result = reset_if_new_month(c, datetime(2026, 3, 15))
        assert len(result.samples) == 1

    def test_new_month_resets(self) -> None:
        c = ReportCollector(month=2, year=2026)
        c.samples.append(DailySample(date="2026-02-28"))
        result = reset_if_new_month(c, datetime(2026, 3, 1))
        assert len(result.samples) == 0
        assert result.month == 3


class TestRecordDailySample:
    def test_adds_new_date(self) -> None:
        c = ReportCollector(month=3, year=2026)
        record_daily_sample(c, DailySample(date="2026-03-01", peak_kw=3.0))
        assert len(c.samples) == 1

    def test_replaces_same_date(self) -> None:
        c = ReportCollector(month=3, year=2026)
        record_daily_sample(c, DailySample(date="2026-03-01", peak_kw=3.0))
        record_daily_sample(c, DailySample(date="2026-03-01", peak_kw=4.0))
        assert len(c.samples) == 1
        assert c.samples[0].peak_kw == 4.0

    def test_multiple_dates(self) -> None:
        c = ReportCollector(month=3, year=2026)
        record_daily_sample(c, DailySample(date="2026-03-01"))
        record_daily_sample(c, DailySample(date="2026-03-02"))
        assert len(c.samples) == 2


class TestGenerateReport:
    def test_empty_collector(self) -> None:
        c = ReportCollector(month=3, year=2026)
        report = generate_report(c)
        assert report.month == 3
        assert report.days_tracked == 0

    def test_peak_reduction(self) -> None:
        c = ReportCollector(month=3, year=2026)
        for day in range(1, 11):
            record_daily_sample(
                c,
                DailySample(
                    date=f"2026-03-{day:02d}",
                    peak_kw=2.0,
                    baseline_peak_kw=4.0,
                ),
            )
        report = generate_report(c, cost_per_kw=80.0)
        assert report.peak_actual_kw == 2.0
        assert report.peak_baseline_kw == 4.0
        assert report.peak_reduction_pct == 50.0
        assert report.peak_savings_kr == 160.0  # (4-2) x 80

    def test_ev_tracking(self) -> None:
        c = ReportCollector(month=3, year=2026)
        for day in range(1, 8):
            record_daily_sample(
                c,
                DailySample(
                    date=f"2026-03-{day:02d}",
                    ev_charged=True,
                    ev_target_reached=day <= 6,  # 6 out of 7 reached target
                    ev_kwh=15.0,
                ),
            )
        report = generate_report(c)
        assert report.ev_nights_charged == 7
        assert report.ev_nights_target_reached == 6
        assert report.ev_target_hit_pct == 85.7
        assert report.ev_total_kwh == 105.0

    def test_discharge_tracking(self) -> None:
        c = ReportCollector(month=3, year=2026)
        for day in range(1, 4):
            record_daily_sample(
                c,
                DailySample(
                    date=f"2026-03-{day:02d}",
                    discharge_kwh=5.0,
                    grid_charge_kwh=3.0,
                ),
            )
        report = generate_report(c)
        assert report.total_discharge_kwh == 15.0
        assert report.total_grid_charge_kwh == 9.0

    def test_system_health(self) -> None:
        c = ReportCollector(month=3, year=2026)
        record_daily_sample(
            c,
            DailySample(
                date="2026-03-01",
                safety_blocks=2,
                plans_generated=288,
            ),
        )
        report = generate_report(c)
        assert report.safety_guard_blocks == 2
        assert report.total_plans_generated == 288

    def test_battery_cycles(self) -> None:
        c = ReportCollector(month=3, year=2026)
        for day in range(1, 4):
            record_daily_sample(
                c,
                DailySample(
                    date=f"2026-03-{day:02d}",
                    battery_cycles=0.8,
                ),
            )
        report = generate_report(c)
        assert report.avg_daily_cycles == 0.8


class TestReportToDict:
    def test_contains_all_keys(self) -> None:
        c = ReportCollector(month=3, year=2026)
        record_daily_sample(c, DailySample(date="2026-03-01", peak_kw=2.0))
        report = generate_report(c)
        d = report_to_dict(report)
        assert "month" in d
        assert "peak_actual_kw" in d
        assert "total_savings_kr" in d
        assert "days_tracked" in d
        assert d["month"] == 3

    def test_serializable(self) -> None:
        """All values should be JSON-serializable primitives."""
        import json

        c = ReportCollector(month=3, year=2026)
        record_daily_sample(c, DailySample(date="2026-03-01", peak_kw=2.5, ev_charged=True))
        report = generate_report(c)
        d = report_to_dict(report)
        json_str = json.dumps(d)
        assert len(json_str) > 10
