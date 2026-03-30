"""Tests for Law Guardian — law monitoring + breach records + RCA."""

from __future__ import annotations

import time

from custom_components.carmabox.core.law_guardian import (
    BreachRecord,
    GuardianState,
    LawGuardian,
    LawId,
    Severity,
)


def _state(**kwargs) -> GuardianState:
    defaults = dict(
        grid_import_w=1500,
        grid_viktat_timmedel_kw=1.5,
        ellevio_tak_kw=2.0,
        battery_soc_1=50,
        battery_soc_2=50,
        battery_power_1=1000,
        battery_power_2=500,
        battery_idle_hours=0,
        ev_soc=80,
        ev_target_soc=75,
        ev_departure_hour=6,
        current_hour=14,
        current_price=50,
        pv_power_w=0,
        export_w=0,
        ems_mode_1="discharge_pv",
        ems_mode_2="discharge_pv",
        fast_charging_1=False,
        fast_charging_2=False,
        cell_temp_1=15,
        cell_temp_2=15,
        min_soc=15,
        cold_lock_temp=4,
    )
    defaults.update(kwargs)
    return GuardianState(**defaults)


class TestLag1Grid:
    def test_under_tak_ok(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=1.0))
        lag1 = [c for c in r.checks if c.law == LawId.LAG_1_GRID]
        assert all(c.ok for c in lag1)
        assert len(r.breaches) == 0

    def test_over_tak_breach(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=2.5))
        lag1 = [c for c in r.checks if c.law == LawId.LAG_1_GRID]
        assert any(not c.ok for c in lag1)
        assert any(b.law == LawId.LAG_1_GRID for b in r.breaches)
        assert r.replan_needed

    def test_breach_has_root_cause(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=2.5, fast_charging_1=True))
        breach = next(b for b in r.breaches if b.law == LawId.LAG_1_GRID)
        assert "fast_charging" in breach.root_cause

    def test_3_breaches_triggers_notification(self):
        g = LawGuardian()
        for _ in range(3):
            r = g.evaluate(_state(grid_viktat_timmedel_kw=2.5, current_hour=14))
        assert any(n["channel"] == "slack" for n in r.notifications)

    def test_near_tak_warning(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=1.8))
        lag1 = [c for c in r.checks if c.law == LawId.LAG_1_GRID]
        assert lag1[0].ok  # Not breached yet
        assert lag1[0].severity == Severity.WARNING


class TestLag2Idle:
    def test_active_batteries_ok(self):
        g = LawGuardian()
        r = g.evaluate(_state(battery_power_1=1000, battery_power_2=500))
        lag2 = [c for c in r.checks if c.law == LawId.LAG_2_IDLE]
        assert all(c.ok for c in lag2)

    def test_idle_with_capacity_warning(self):
        g = LawGuardian()
        # Simulate many idle cycles (>4h = 480 cycles at 30s)
        for _ in range(490):
            r = g.evaluate(
                _state(
                    battery_power_1=0,
                    battery_power_2=0,
                    battery_soc_1=50,
                    battery_soc_2=50,
                )
            )
        lag2 = [c for c in r.checks if c.law == LawId.LAG_2_IDLE]
        assert any(not c.ok for c in lag2)


class TestLag3Ev:
    def test_ev_at_target_ok(self):
        g = LawGuardian()
        r = g.evaluate(_state(ev_soc=80, ev_target_soc=75, current_hour=6, ev_departure_hour=6))
        lag3 = [c for c in r.checks if c.law == LawId.LAG_3_EV]
        assert all(c.ok for c in lag3)

    def test_ev_under_target_at_departure(self):
        g = LawGuardian()
        r = g.evaluate(_state(ev_soc=60, ev_target_soc=75, current_hour=6, ev_departure_hour=6))
        lag3 = [c for c in r.checks if c.law == LawId.LAG_3_EV]
        assert any(not c.ok for c in lag3)
        assert any(n["severity"] == "critical" for n in r.notifications)


class TestLag4Export:
    def test_no_export_ok(self):
        g = LawGuardian()
        r = g.evaluate(_state(export_w=0))
        lag4 = [c for c in r.checks if c.law == LawId.LAG_4_EXPORT]
        assert all(c.ok for c in lag4)

    def test_high_export_warning(self):
        g = LawGuardian()
        r = g.evaluate(_state(export_w=1000))
        lag4 = [c for c in r.checks if c.law == LawId.LAG_4_EXPORT]
        assert any(not c.ok for c in lag4)


class TestInvariants:
    def test_ems_auto_breach(self):
        g = LawGuardian()
        r = g.evaluate(_state(ems_mode_1="auto"))
        assert any(b.law == LawId.INV_1_EMS_AUTO for b in r.breaches)

    def test_crosscharge_breach(self):
        g = LawGuardian()
        r = g.evaluate(_state(battery_power_1=-2000, battery_power_2=1000))
        assert any(b.law == LawId.INV_2_CROSSCHARGE for b in r.breaches)

    def test_fast_charging_breach(self):
        g = LawGuardian()
        r = g.evaluate(_state(fast_charging_1=True))
        assert any(b.law == LawId.INV_3_FAST_CHARGE for b in r.breaches)

    def test_min_soc_breach(self):
        g = LawGuardian()
        r = g.evaluate(_state(battery_soc_1=12, battery_power_1=500))
        assert any(b.law == LawId.INV_5_MIN_SOC for b in r.breaches)

    def test_cold_min_soc_higher(self):
        g = LawGuardian()
        r = g.evaluate(_state(battery_soc_1=18, battery_power_1=500, cell_temp_1=3))
        assert any(b.law == LawId.INV_5_MIN_SOC for b in r.breaches)

    def test_all_ok(self):
        g = LawGuardian()
        r = g.evaluate(_state())
        inv_breaches = [b for b in r.breaches if b.law.value.startswith("INV")]
        assert len(inv_breaches) == 0


class TestBreachHistory:
    def test_breaches_stored(self):
        g = LawGuardian()
        g.evaluate(_state(grid_viktat_timmedel_kw=3.0))
        assert len(g.breach_history) > 0

    def test_history_capped(self):
        g = LawGuardian(max_breach_history=10)
        for _ in range(20):
            g.evaluate(_state(grid_viktat_timmedel_kw=3.0))
        assert len(g.breach_history) <= 10


class TestSummaries:
    def test_daily_summary(self):
        g = LawGuardian()
        g.evaluate(_state(grid_viktat_timmedel_kw=3.0))
        s = g.daily_summary()
        assert s["total_breaches"] > 0
        assert s["lag1_count"] > 0

    def test_hourly_summary(self):
        g = LawGuardian()
        g.evaluate(_state(grid_viktat_timmedel_kw=3.0))
        s = g.hourly_summary()
        assert s["breach_count"] >= 0


class TestRootCause:
    def test_classify_fast_charging(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=3.0, fast_charging_1=True))
        b = next(b for b in r.breaches if b.law == LawId.LAG_1_GRID)
        assert "fast_charging" in b.root_cause

    def test_classify_idle(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=3.0, battery_power_1=0, battery_power_2=0))
        b = next(b for b in r.breaches if b.law == LawId.LAG_1_GRID)
        assert "idle" in b.root_cause.lower()

    def test_classify_charging(self):
        g = LawGuardian()
        r = g.evaluate(_state(grid_viktat_timmedel_kw=3.0, battery_power_1=-2000))
        b = next(b for b in r.breaches if b.law == LawId.LAG_1_GRID)
        assert "LADDAR" in b.root_cause


class TestShouldNotifySlack:
    def test_should_notify_no_breaches(self):
        g = LawGuardian()
        notify, msg = g.should_notify_slack()
        assert notify is False
        assert msg == ""

    def test_should_notify_below_threshold(self):
        g = LawGuardian()
        # Generate 2 LAG_1 breaches (threshold default = 3)
        for _ in range(2):
            g.evaluate(_state(grid_viktat_timmedel_kw=2.5, current_hour=14))
        notify, msg = g.should_notify_slack()
        assert notify is False

    def test_should_notify_above_threshold(self):
        g = LawGuardian()
        # Generate 3 LAG_1 breaches within same hour
        for _ in range(3):
            g.evaluate(_state(grid_viktat_timmedel_kw=2.5, current_hour=14))
        notify, msg = g.should_notify_slack()
        assert notify is True
        assert "LAG_1" in msg
        assert "x3" in msg

    def test_should_notify_old_breaches_excluded(self):
        g = LawGuardian()
        now = time.monotonic()
        # Inject 3 breaches with monotonic timestamps >60 min ago
        for _ in range(3):
            br = BreachRecord(
                timestamp="2026-03-29T10:00:00",
                law=LawId.LAG_1_GRID,
                severity=Severity.CRITICAL,
                actual_value=2.5,
                limit_value=2.0,
            )
            # Override _mono to be 70 minutes ago
            br._mono = now - 70 * 60
            g.breach_history.append(br)
        notify, msg = g.should_notify_slack(window_minutes=60)
        assert notify is False
        assert msg == ""
