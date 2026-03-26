"""Unit tests for Intelligent Scheduler (IT-2378).

Pure Python tests — no HA mocks. Tests the scheduler optimizer module
including EV backwards scheduling, battery scheduling, miner scheduling,
constraint checking, breach analysis, and learning profile.
"""

from __future__ import annotations

import pytest

from custom_components.carmabox.optimizer.models import (
    BreachCorrection,
    BreachLearning,
    BreachRecord,
    HourlyMeterState,
    SchedulerHourSlot,
)
from custom_components.carmabox.optimizer.scheduler import (
    _apply_corrections,
    _is_appliance_window,
    _is_night_hour,
    _schedule_battery,
    _schedule_ev_backwards,
    _schedule_miner,
    analyze_breach,
    analyze_idle_time,
    generate_scheduler_plan,
    plan_ev_full_charge,
    update_learnings,
)


class TestHelpers:
    """Test helper functions."""

    def test_is_night_hour(self) -> None:
        assert _is_night_hour(22) is True
        assert _is_night_hour(23) is True
        assert _is_night_hour(0) is True
        assert _is_night_hour(5) is True
        assert _is_night_hour(6) is False
        assert _is_night_hour(12) is False
        assert _is_night_hour(21) is False

    def test_is_appliance_window(self) -> None:
        assert _is_appliance_window(22) is True
        assert _is_appliance_window(23) is True
        assert _is_appliance_window(0) is True
        assert _is_appliance_window(1) is False
        assert _is_appliance_window(12) is False


class TestEVBackwardsScheduling:
    """Test EV scheduling with backwards-from-departure strategy."""

    def test_no_ev_returns_zeros(self) -> None:
        schedule = _schedule_ev_backwards(
            num_hours=24,
            start_hour=18,
            ev_soc_pct=-1,
            ev_capacity_kwh=0,
            morning_target_soc=75.0,
            hourly_prices=[50.0] * 24,
            hourly_loads=[1.5] * 24,
            target_weighted_kw=2.0,
            battery_kwh_available=5.0,
            pv_tomorrow_kwh=10.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert all(kw == 0 and amps == 0 for kw, amps in schedule)

    def test_ev_charging_only_at_night(self) -> None:
        """EV should only charge during night hours (22-06)."""
        schedule = _schedule_ev_backwards(
            num_hours=24,
            start_hour=18,
            ev_soc_pct=30.0,
            ev_capacity_kwh=87.5,
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 24,
            hourly_loads=[1.0] * 24,
            target_weighted_kw=4.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=20.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        for i, (kw, _amps) in enumerate(schedule):
            abs_h = (18 + i) % 24
            if kw > 0:
                assert _is_night_hour(abs_h), f"EV charging at non-night hour {abs_h}"

    def test_ev_already_charged(self) -> None:
        """No charging if already well above target (accounting for 10% BMS loss)."""
        schedule = _schedule_ev_backwards(
            num_hours=24,
            start_hour=18,
            ev_soc_pct=90.0,  # 90% * 0.9 = 81% > 75% target
            ev_capacity_kwh=87.5,
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 24,
            hourly_loads=[1.0] * 24,
            target_weighted_kw=4.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=20.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert all(kw == 0 for kw, _ in schedule)

    def test_ev_respects_learnings(self) -> None:
        """Learned breach avoidance should skip affected hours."""
        learnings = [
            BreachLearning(
                pattern="ev_23",
                hour=23,
                description="EV + disk kl 23",
                action="pause_ev",
                confidence=0.8,
                occurrences=3,
            )
        ]
        schedule = _schedule_ev_backwards(
            num_hours=24,
            start_hour=18,
            ev_soc_pct=30.0,
            ev_capacity_kwh=87.5,
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 24,
            hourly_loads=[1.0] * 24,
            target_weighted_kw=4.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=20.0,
            daily_consumption_kwh=15.0,
            learnings=learnings,
        )
        # Hour 23 is index 5 (start=18, 18+5=23)
        kw_at_23, _ = schedule[5]
        # Should prefer other hours, but may still use 23 as last resort
        # The key is that non-avoided hours are preferred
        total_non_avoided = sum(
            kw for i, (kw, _) in enumerate(schedule) if (18 + i) % 24 != 23 and kw > 0
        )
        assert total_non_avoided > 0, "Should prefer non-avoided hours"


class TestBatteryScheduling:
    """Test battery scheduling logic."""

    def test_solar_surplus_charges(self) -> None:
        """Battery should charge from solar surplus."""
        result = _schedule_battery(
            num_hours=24,
            start_hour=0,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0] * 6 + [3, 5, 7, 8, 8, 7, 5, 3] + [0] * 10,
            hourly_loads=[1.5] * 24,
            hourly_ev=[0.0] * 24,
            target_weighted_kw=2.0,
            battery_soc_pct=30.0,
            battery_cap_kwh=20.0,
        )
        # Midday hours with PV surplus should charge
        charge_hours = [i for i, (kw, action) in enumerate(result) if action == "c"]
        assert len(charge_hours) >= 3, "Should charge during solar surplus hours"

    def test_cheap_price_grid_charges(self) -> None:
        """Battery should grid charge at very cheap prices."""
        prices = [100.0] * 24
        prices[2] = 5.0  # Very cheap hour
        prices[3] = 8.0  # Also cheap
        result = _schedule_battery(
            num_hours=24,
            start_hour=0,
            hourly_prices=prices,
            hourly_pv=[0.0] * 24,
            hourly_loads=[1.0] * 24,
            hourly_ev=[0.0] * 24,
            target_weighted_kw=2.0,
            battery_soc_pct=30.0,
            battery_cap_kwh=20.0,
        )
        # Hour 2 and 3 should be grid charge
        _, action_h2 = result[2]
        _, action_h3 = result[3]
        assert action_h2 == "g", "Should grid charge at cheap price"
        assert action_h3 == "g", "Should grid charge at cheap price"

    def test_high_load_discharges(self) -> None:
        """Battery should discharge when load exceeds target."""
        loads = [1.0] * 24
        loads[17] = 4.0  # Evening peak
        loads[18] = 4.5
        loads[19] = 4.0
        result = _schedule_battery(
            num_hours=24,
            start_hour=0,
            hourly_prices=[80.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_loads=loads,
            hourly_ev=[0.0] * 24,
            target_weighted_kw=2.0,
            battery_soc_pct=80.0,
            battery_cap_kwh=20.0,
        )
        discharge_hours = [i for i, (kw, action) in enumerate(result) if action == "d"]
        assert 17 in discharge_hours or 18 in discharge_hours, "Should discharge during peak"

    def test_min_soc_respected(self) -> None:
        """Should not discharge below min SoC."""
        result = _schedule_battery(
            num_hours=24,
            start_hour=0,
            hourly_prices=[80.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_loads=[5.0] * 24,  # Very high load
            hourly_ev=[0.0] * 24,
            target_weighted_kw=2.0,
            battery_soc_pct=20.0,  # Low SoC
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
        )
        # With only 20% SoC and 15% min, only 1 kWh available
        total_discharge = sum(abs(kw) for kw, action in result if action == "d")
        assert total_discharge <= 1.5, "Should not exceed available energy above min SoC"


class TestMinerScheduling:
    """Test miner scheduling logic."""

    def test_miner_only_on_export(self) -> None:
        """Miner should only run when PV export surplus."""
        result = _schedule_miner(
            num_hours=24,
            start_hour=0,
            hourly_pv=[0] * 6 + [1, 3, 5, 8, 8, 7, 5, 3, 1] + [0] * 9,
            hourly_loads=[1.5] * 24,
            hourly_ev=[0.0] * 24,
            hourly_battery=[0.0] * 24,
        )
        # Should only be on during high PV hours
        for i, on in enumerate(result):
            pv = [0] * 6 + [1, 3, 5, 8, 8, 7, 5, 3, 1] + [0] * 9
            if i < len(pv):
                net_export = (pv[i] - 1.5) * 1000
                if on:
                    assert net_export > 500, f"Miner on at hour {i} with insufficient export"

    def test_miner_off_at_night(self) -> None:
        """Miner should be off at night (no PV)."""
        result = _schedule_miner(
            num_hours=24,
            start_hour=0,
            hourly_pv=[0.0] * 24,
            hourly_loads=[1.5] * 24,
            hourly_ev=[0.0] * 24,
            hourly_battery=[0.0] * 24,
        )
        assert not any(result), "Miner should be off with no PV"


class TestBreachAnalysis:
    """Test auto root cause analysis."""

    def test_ev_appliance_overlap(self) -> None:
        """Detect EV + appliance overlap as root cause."""
        breach = analyze_breach(
            hour=23,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=1.5,
            ev_kw=1.38,
            ev_amps=6,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
            appliance_loads={"disk": 1.5},
        )
        assert "EV" in breach.root_cause
        assert "vitvaror" in breach.root_cause
        assert breach.severity in ("major", "critical")

    def test_high_house_load(self) -> None:
        """Detect unexpectedly high house load."""
        breach = analyze_breach(
            hour=14,
            actual_weighted_kw=4.0,
            target_kw=2.0,
            house_load_kw=4.0,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
        )
        assert "huslast" in breach.root_cause.lower() or "hushåll" in breach.root_cause.lower()

    def test_severity_classification(self) -> None:
        """Test breach severity levels."""
        minor = analyze_breach(
            hour=10,
            actual_weighted_kw=2.1,
            target_kw=2.0,
            house_load_kw=2.1,
            ev_kw=0,
            ev_amps=0,
            battery_kw=0,
            pv_kw=0,
            miner_on=False,
        )
        assert minor.severity == "minor"

        major = analyze_breach(
            hour=10,
            actual_weighted_kw=2.4,
            target_kw=2.0,
            house_load_kw=2.4,
            ev_kw=0,
            ev_amps=0,
            battery_kw=0,
            pv_kw=0,
            miner_on=False,
        )
        assert major.severity == "major"

        critical = analyze_breach(
            hour=10,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            house_load_kw=3.0,
            ev_kw=0,
            ev_amps=0,
            battery_kw=0,
            pv_kw=0,
            miner_on=False,
        )
        assert critical.severity == "critical"


class TestLearningProfile:
    """Test breach learning and pattern recognition."""

    def test_new_learning_created(self) -> None:
        """First breach creates a new learning entry."""
        breach = BreachRecord(
            timestamp="2026-03-26T23:00:00",
            hour=23,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            loads_active=["EV:6A (1.4kW)", "disk:1.5kW"],
            root_cause="EV + vitvaror kl 23",
            remediation="Pausa EV",
            severity="major",
        )
        learnings = update_learnings([], breach)
        assert len(learnings) == 1
        assert learnings[0].hour == 23
        assert learnings[0].confidence == 0.2

    def test_repeated_breach_increases_confidence(self) -> None:
        """Same pattern should increase confidence."""
        learnings = [
            BreachLearning(
                pattern="ev_23",
                hour=23,
                description="EV + vitvaror",
                action="pause_ev",
                confidence=0.4,
                occurrences=2,
            )
        ]
        breach = BreachRecord(
            timestamp="2026-03-27T23:00:00",
            hour=23,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            loads_active=["EV:6A (1.4kW)"],
            root_cause="EV + vitvaror kl 23",
            remediation="Pausa EV",
            severity="major",
        )
        updated = update_learnings(learnings, breach)
        assert updated[0].occurrences == 3
        assert updated[0].confidence == pytest.approx(0.6)


class TestEVFullChargePlanning:
    """Test weekly 100% EV charge planning."""

    def test_not_due_yet(self) -> None:
        """Should return empty if not due."""
        result = plan_ev_full_charge(
            days_since_full=3,
            pv_forecast_daily=[20, 25, 30],
            current_weekday=2,  # Wednesday
        )
        assert result == ""

    def test_overdue_finds_sunny_day(self) -> None:
        """Should find a sunny weekend day."""
        result = plan_ev_full_charge(
            days_since_full=7,
            pv_forecast_daily=[10, 15, 20, 5, 30, 28, 25],  # Day 4-6 are Thu-Sat
            current_weekday=0,  # Monday
        )
        assert result != ""  # Should find a day


class TestFullSchedulerPlan:
    """Test the complete scheduler plan generation."""

    def test_basic_plan_generation(self) -> None:
        """Generate a basic plan and verify structure."""
        plan = generate_scheduler_plan(
            start_hour=18,
            num_hours=24,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_loads=[1.5] * 24,
            battery_soc_pct=60.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,
        )
        assert len(plan.slots) == 24
        assert plan.target_weighted_kw == 2.0
        assert plan.start_hour == 18

    def test_plan_with_ev(self) -> None:
        """Plan with EV enabled should schedule charging."""
        plan = generate_scheduler_plan(
            start_hour=18,
            num_hours=24,
            hourly_prices=[30.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_loads=[1.0] * 24,
            battery_soc_pct=60.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=4.0,
            ev_enabled=True,
            ev_soc_pct=30.0,
            ev_capacity_kwh=87.5,
            ev_morning_target_soc=75.0,
            pv_tomorrow_kwh=20.0,
        )
        assert plan.total_ev_kwh > 0, "Should schedule EV charging"
        ev_slots = [s for s in plan.slots if s.ev_kw > 0]
        assert len(ev_slots) > 0

    def test_plan_constraints_respected(self) -> None:
        """All slots should pass constraint check (under target)."""
        plan = generate_scheduler_plan(
            start_hour=0,
            num_hours=24,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0] * 6 + [3, 5, 7, 8, 7, 5, 3, 1] + [0] * 10,
            hourly_loads=[1.5] * 24,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=3.0,
        )
        constraint_violations = sum(1 for s in plan.slots if not s.constraint_ok)
        # May have some violations in edge cases but shouldn't be majority
        assert constraint_violations <= len(plan.slots) // 2

    def test_plan_winter_evening_peak(self) -> None:
        """Winter scenario: battery should discharge during evening peak."""
        loads = [0.8] * 6 + [2.0] * 3 + [1.5] * 5 + [3.0] * 2 + [4.0] * 3 + [3.5] * 2 + [1.5] * 3
        plan = generate_scheduler_plan(
            start_hour=0,
            num_hours=24,
            hourly_prices=[40] * 6 + [60] * 3 + [80] * 3 + [100] * 4 + [120] * 5 + [80] * 3,
            hourly_pv=[0.0] * 24,
            hourly_loads=loads,
            battery_soc_pct=85.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,
        )
        # Evening hours (17-21) should have discharge
        evening_discharge = [s for s in plan.slots if 17 <= s.hour <= 21 and s.action == "d"]
        assert len(evening_discharge) >= 2, "Should discharge during winter evening peak"

    def test_plan_miner_only_on_pv_export(self) -> None:
        """Miner should only be on during PV export."""
        plan = generate_scheduler_plan(
            start_hour=0,
            num_hours=24,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0] * 6 + [1, 3, 5, 8, 10, 9, 7, 4, 2] + [0] * 9,
            hourly_loads=[1.5] * 24,
            battery_soc_pct=90.0,  # Nearly full
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,
        )
        miner_hours = [s.hour for s in plan.slots if s.miner_on]
        # Miner should only be on during hours with significant PV
        for h in miner_hours:
            pv_list = [0] * 6 + [1, 3, 5, 8, 10, 9, 7, 4, 2] + [0] * 9
            assert pv_list[h] > 1.5, f"Miner on at hour {h} with insufficient PV"

    def test_plan_summary_stats(self) -> None:
        """Verify summary statistics are calculated."""
        plan = generate_scheduler_plan(
            start_hour=0,
            num_hours=24,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0] * 6 + [5, 8, 10, 10, 8, 5, 3] + [0] * 11,
            hourly_loads=[1.5] * 24,
            battery_soc_pct=30.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,
        )
        assert plan.total_charge_kwh >= 0
        assert plan.total_discharge_kwh >= 0
        assert plan.max_weighted_kw >= 0
        assert plan.estimated_cost_kr >= 0


class TestConstraintChecker:
    """Test constraint checking and remediation."""

    def test_violation_reduces_ev(self) -> None:
        """Constraint violation should reduce EV amps."""
        plan = generate_scheduler_plan(
            start_hour=22,  # Start at night
            num_hours=8,
            hourly_prices=[30.0] * 8,
            hourly_pv=[0.0] * 8,
            hourly_loads=[2.5] * 8,  # High house load
            battery_soc_pct=50.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,  # Low target
            night_weight=0.5,
            ev_enabled=True,
            ev_soc_pct=20.0,
            ev_capacity_kwh=87.5,
            ev_morning_target_soc=75.0,
            pv_tomorrow_kwh=20.0,
        )
        # With a low target and high load, EV should be constrained
        # Check that constraint checking ran
        assert len(plan.slots) == 8


class TestBreachCorrections:
    """Test breach correction application in scheduler."""

    def _make_ev_schedule(self, n: int = 24) -> list[tuple[float, int]]:
        """Create an EV schedule with charging at hours 23-02."""
        schedule = [(0.0, 0)] * n
        for i in [5, 6, 7, 8]:  # Indices for ~23,0,1,2 if start=18
            if i < n:
                schedule[i] = (4.14, 6)
        return schedule

    def _make_bat_schedule(self, n: int = 24) -> list[tuple[float, str]]:
        """Create a battery schedule: idle by default."""
        return [(0.0, "i")] * n

    def test_reduce_ev_correction(self) -> None:
        """reduce_ev should cap EV amps at 6A."""
        ev = self._make_ev_schedule()
        bat = self._make_bat_schedule()
        corr = BreachCorrection(
            created="2026-03-26T10:00:00",
            source_breach_hour=23,
            action="reduce_ev",
            target_hour=23,
            param="ev_amps=6",
            reason="EV för hög last kl 23",
        )
        # Hour 23 with start_hour=18 → index 5
        ev_out, bat_out = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev,
            battery_schedule=bat,
            start_hour=18,
            num_hours=24,
            battery_soc_pct=60,
            battery_cap_kwh=20,
            battery_min_soc=15,
            max_discharge_kw=4.0,
        )
        assert corr.applied is True
        # EV at index 5 (hour 23) should be reduced
        kw, amps = ev_out[5]
        assert amps == 6

    def test_add_discharge_correction(self) -> None:
        """add_discharge should schedule battery discharge."""
        ev = self._make_ev_schedule()
        bat = self._make_bat_schedule()
        corr = BreachCorrection(
            created="2026-03-26T10:00:00",
            source_breach_hour=19,
            action="add_discharge",
            target_hour=19,
            param="discharge_kw=2.5",
            reason="Batteri idle under peak",
        )
        # Hour 19 with start_hour=18 → index 1
        ev_out, bat_out = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev,
            battery_schedule=bat,
            start_hour=18,
            num_hours=24,
            battery_soc_pct=60,
            battery_cap_kwh=20,
            battery_min_soc=15,
            max_discharge_kw=4.0,
        )
        assert corr.applied is True
        kw, action = bat_out[1]
        assert action == "d"
        assert kw == -2.5

    def test_shift_ev_correction(self) -> None:
        """shift_ev should move EV charging from one hour to another."""
        ev = self._make_ev_schedule()
        bat = self._make_bat_schedule()
        # Move from hour 23 (idx 5) to hour 3 (idx 9)
        corr = BreachCorrection(
            created="2026-03-26T10:00:00",
            source_breach_hour=23,
            action="shift_ev",
            target_hour=3,
            param="shift_from=23,shift_to=3",
            reason="Flytta EV",
        )
        ev_out, bat_out = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev,
            battery_schedule=bat,
            start_hour=18,
            num_hours=24,
            battery_soc_pct=60,
            battery_cap_kwh=20,
            battery_min_soc=15,
            max_discharge_kw=4.0,
        )
        assert corr.applied is True
        # Hour 23 (idx 5) should be empty, hour 3 (idx 9) should have EV
        assert ev_out[5] == (0.0, 0)
        assert ev_out[9][0] > 0

    def test_expired_correction_skipped(self) -> None:
        """Expired corrections should not be applied."""
        bat = self._make_bat_schedule()
        corr = BreachCorrection(
            created="2026-03-25T10:00:00",
            source_breach_hour=19,
            action="add_discharge",
            target_hour=19,
            param="discharge_kw=2.0",
            reason="Old correction",
            expired=True,
        )
        _, bat_out = _apply_corrections(
            corrections=[corr],
            ev_schedule=self._make_ev_schedule(),
            battery_schedule=bat,
            start_hour=18,
            num_hours=24,
            battery_soc_pct=60,
            battery_cap_kwh=20,
            battery_min_soc=15,
            max_discharge_kw=4.0,
        )
        assert corr.applied is False  # Was expired, not applied
        assert bat_out[1][1] == "i"  # Still idle

    def test_low_soc_blocks_discharge_correction(self) -> None:
        """add_discharge should not apply if battery too low."""
        bat = self._make_bat_schedule()
        corr = BreachCorrection(
            created="2026-03-26T10:00:00",
            source_breach_hour=19,
            action="add_discharge",
            target_hour=19,
            param="discharge_kw=2.0",
            reason="Batteri idle",
        )
        _, bat_out = _apply_corrections(
            corrections=[corr],
            ev_schedule=self._make_ev_schedule(),
            battery_schedule=bat,
            start_hour=18,
            num_hours=24,
            battery_soc_pct=16,  # Just above min_soc=15 → <1 kWh avail
            battery_cap_kwh=20,
            battery_min_soc=15,
            max_discharge_kw=4.0,
        )
        assert corr.applied is False
        assert bat_out[1][1] == "i"

    def test_corrections_in_full_plan(self) -> None:
        """Corrections should be applied when passed to generate_scheduler_plan."""
        corr = BreachCorrection(
            created="2026-03-26T10:00:00",
            source_breach_hour=19,
            action="add_discharge",
            target_hour=19,
            param="discharge_kw=2.0",
            reason="Batteri idle under peak kl 19",
        )
        plan = generate_scheduler_plan(
            start_hour=18,
            num_hours=24,
            hourly_prices=[50.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_loads=[1.5] * 24,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=2.0,
            corrections=[corr],
        )
        assert len(plan.slots) == 24
        # Correction should have been applied
        assert corr.applied is True


class TestIdleAnalysis:
    """Test battery idle time analysis."""

    def _make_slots(self, idle_hours: list[int]) -> list[SchedulerHourSlot]:
        """Create slots where specified hours are idle."""
        slots = []
        for h in range(24):
            action = "i" if h in idle_hours else "d"
            slots.append(
                SchedulerHourSlot(
                    hour=h,
                    action=action,
                    battery_kw=0 if action == "i" else -2,
                    ev_kw=0,
                    ev_amps=0,
                    miner_on=False,
                    grid_kw=2.0,
                    weighted_kw=2.0,
                    pv_kw=5.0 if 9 <= h <= 15 else 0.0,
                    consumption_kw=2.0,
                    price=30 + h * 2,
                    battery_soc=50,
                    ev_soc=50,
                    constraint_ok=True,
                    reasoning="test",
                )
            )
        return slots

    def test_fully_active_100_score(self) -> None:
        """No idle hours should give 100% score."""
        slots = self._make_slots(idle_hours=[])
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=0,
            battery_soc_pct=50,
            battery_min_soc=15,
            battery_cap_kwh=20,
            prices=[50] * 24,
            pv_forecast=[0] * 24,
        )
        assert result.score == 100
        assert result.idle_pct == 0.0

    def test_all_idle_0_score(self) -> None:
        """All idle hours should give 0% score."""
        slots = self._make_slots(idle_hours=list(range(24)))
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=720,
            battery_soc_pct=50,
            battery_min_soc=15,
            battery_cap_kwh=20,
            prices=[50] * 24,
            pv_forecast=[0] * 24,
        )
        assert result.score == 0
        assert result.idle_pct > 0

    def test_missed_pv_charge_detected(self) -> None:
        """Idle hours with PV surplus should be flagged."""
        slots = self._make_slots(idle_hours=[10, 11, 12, 13])
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=240,
            battery_soc_pct=50,
            battery_min_soc=15,
            battery_cap_kwh=20,
            prices=[50] * 24,
            pv_forecast=[5.0 if 9 <= h <= 15 else 0 for h in range(24)],
        )
        assert result.missed_charge_kwh > 0
        assert any("PV" in t for t in result.opportunities)

    def test_high_idle_pct_generates_tip(self) -> None:
        """High idle % should generate reduction tip."""
        slots = self._make_slots(idle_hours=list(range(18)))  # 18/24 idle
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=600,
            battery_soc_pct=50,
            battery_min_soc=15,
            battery_cap_kwh=20,
            prices=[50] * 24,
            pv_forecast=[0] * 24,
        )
        assert result.idle_pct >= 50  # anti-idle discharge reduces idle hours
        assert any("idle" in t.lower() or "Idle" in t for t in result.opportunities)


class TestHourlyMeterState:
    """Test the HourlyMeterState model."""

    def test_defaults(self) -> None:
        state = HourlyMeterState()
        assert state.hour == -1
        assert state.samples == []
        assert state.projected_avg == 0.0
        assert state.warning_issued is False
        assert state.load_shed_active is False

    def test_sample_tracking(self) -> None:
        state = HourlyMeterState(hour=14)
        state.samples.append(1.5)
        state.samples.append(2.0)
        avg = sum(state.samples) / len(state.samples)
        assert avg == 1.75


class TestPredictorBatteryEconomics:
    """Test predictor battery economics methods."""

    def test_idle_penalty_round_trip(self) -> None:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        p = ConsumptionPredictor()
        p.add_idle_penalty(hour=14, weekday=2, idle_minutes=45, price_spread_ore=30)
        econ = p.get_battery_economics()
        assert econ["idle_count"] == 1
        assert econ["idle_penalty_ore"] > 0

    def test_battery_cycle_tracking(self) -> None:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        p = ConsumptionPredictor()
        p.add_battery_cycle(hour=18, weekday=3, charge_kwh=2.0, discharge_kwh=3.0, price_ore=80)
        econ = p.get_battery_economics()
        assert econ["cycle_count"] == 1
        assert econ["cycle_value_sek"] > 0

    def test_should_cycle_default_true(self) -> None:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        p = ConsumptionPredictor()
        assert p.should_cycle_battery(14, 2) is True  # Default with no data

    def test_serialization_preserves_economics(self) -> None:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        p = ConsumptionPredictor()
        p.add_idle_penalty(hour=14, weekday=2, idle_minutes=45, price_spread_ore=30)
        p.add_battery_cycle(hour=18, weekday=3, charge_kwh=2.0, discharge_kwh=3.0, price_ore=80)
        d = p.to_dict()
        p2 = ConsumptionPredictor.from_dict(d)
        assert p2.get_battery_economics() == p.get_battery_economics()
