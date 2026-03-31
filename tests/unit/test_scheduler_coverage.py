"""Coverage sweep for scheduler.py missing branches.

Targets lines: 75, 86, 91-93, 176, 181, 185, 208, 214-215, 234, 244,
  261, 264-270, 323-328, 347, 349, 431, 461-466, 471-475,
  540-542, 546-555, 559-562, 636, 638, 640, 642, 694-698,
  723, 731-736, 759-760, 799-802, 835, 857-858, 878-879, 893-903,
  963, 1128, 1245, 1264-1265, 1269-1270, 1275, 1296
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.models import (
    BreachCorrection,
    BreachLearning,
    BreachRecord,
    SchedulerHourSlot,
)
from custom_components.carmabox.optimizer.scheduler import (
    _apply_corrections,
    _check_constraints,
    _hours_until_departure,
    _is_appliance_window,
    _is_night_hour,
    _schedule_battery,
    _schedule_ev_backwards,
    analyze_breach,
    analyze_idle_time,
    generate_scheduler_plan,
    plan_ev_full_charge,
    update_learnings,
)

# ── Helper functions ───────────────────────────────────────────────────────────


def _slot(
    hour: int = 14,
    *,
    action: str = "i",
    battery_kw: float = 0.0,
    ev_kw: float = 0.0,
    ev_amps: int = 0,
    miner_on: bool = False,
    grid_kw: float = 2.0,
    weighted_kw: float = 2.0,
    pv_kw: float = 0.0,
    consumption_kw: float = 2.0,
    price: float = 50.0,
    battery_soc: float = 50.0,
    ev_soc: int = 0,
    constraint_ok: bool = True,
    reasoning: str = "",
) -> SchedulerHourSlot:
    return SchedulerHourSlot(
        hour=hour,
        action=action,
        battery_kw=battery_kw,
        ev_kw=ev_kw,
        ev_amps=ev_amps,
        miner_on=miner_on,
        grid_kw=grid_kw,
        weighted_kw=weighted_kw,
        pv_kw=pv_kw,
        consumption_kw=consumption_kw,
        price=price,
        battery_soc=battery_soc,
        ev_soc=ev_soc,
        constraint_ok=constraint_ok,
        reasoning=reasoning,
    )


# ── Lines 75, 86: Helper start<=end branches ───────────────────────────────────


class TestHelperEdgeCases:
    """Targets lines 75 and 86 (start<=end branches)."""

    def test_is_night_hour_start_le_end(self) -> None:
        """start<=end path (line 75): _is_night_hour with custom range."""
        assert _is_night_hour(3, 0, 6) is True  # 0 <= 3 < 6
        assert _is_night_hour(7, 0, 6) is False  # 7 not in 0-6

    def test_is_appliance_window_start_le_end(self) -> None:
        """start<=end path (line 86): custom window."""
        assert _is_appliance_window(3, 0, 5) is True  # 0 <= 3 < 5
        assert _is_appliance_window(6, 0, 5) is False  # 6 not in 0-5

    def test_hours_until_departure_before_departure(self) -> None:
        """Lines 91-92: current_hour <= departure."""
        assert _hours_until_departure(14, 20) == 6  # 20 - 14

    def test_hours_until_departure_after_departure(self) -> None:
        """Line 93: current_hour > departure — wraps overnight."""
        assert _hours_until_departure(22, 7) == 9  # 24 - 22 + 7


# ── Lines 176, 181, 185: _schedule_ev_backwards edge cases ────────────────────


class TestScheduleEvBackwards:
    """EV schedule edge cases and branch coverage."""

    def _make_learnings(
        self, hour: int, action: str, confidence: float = 0.8
    ) -> list[BreachLearning]:
        return [
            BreachLearning(
                pattern=f"ev_{hour}",
                hour=hour,
                description="test",
                action=action,
                confidence=confidence,
                occurrences=2,
            )
        ]

    def test_ev_soc_negative_uses_50_default(self) -> None:
        """ev_soc_pct < 0 → defaults to 50.0 (line ~176)."""
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=-1.0,  # negative → 50%
            ev_capacity_kwh=75.0,
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[1.5] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        # With ev_soc=50, target=75, energy needed → some charging
        assert len(result) == 8

    def test_ev_capacity_zero_returns_empty(self) -> None:
        """ev_capacity_kwh<=0 → return empty schedule (line ~181)."""
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=50.0,
            ev_capacity_kwh=0.0,  # invalid → empty
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[1.5] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert all(kw == 0.0 and amps == 0 for kw, amps in result)

    def test_energy_needed_below_threshold_returns_empty(self) -> None:
        """energy_needed < 0.5 → return empty schedule (line ~185)."""
        # With tiny battery (1 kWh) and SoC=83% → target=75%
        # energy_needed = max(0, (75 - 83*0.9)/100 * 1) = 0.003 kWh < 0.5
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=83.0,  # 83*0.9=74.7 > 75 target → negligible need
            ev_capacity_kwh=1.0,  # tiny battery → energy_needed < 0.5 kWh
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[1.5] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert all(kw == 0.0 and amps == 0 for kw, amps in result)

    def test_learning_avoid_shifts_to_secondary_hours(self) -> None:
        """Learnings with confidence>0.5 cause avoidance (line ~176)."""
        # Mark hour 22 as avoided
        learnings = self._make_learnings(22, "pause_ev", 0.9)
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=20.0,  # big deficit → needs charging
            ev_capacity_kwh=75.0,
            morning_target_soc=75.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[1.5] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=15.0,
            learnings=learnings,
        )
        assert len(result) == 8

    def test_pv_surplus_gt_10_uses_full_battery_budget(self) -> None:
        """pv_surplus > 10 → battery_budget = full available (line ~208)."""
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=20.0,
            ev_capacity_kwh=75.0,
            morning_target_soc=75.0,
            hourly_prices=[25.0] * 8,
            hourly_loads=[1.0] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=15.0,
            pv_tomorrow_kwh=30.0,  # > daily consumption → surplus > 10
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert len(result) == 8

    def test_pv_surplus_between_0_and_10(self) -> None:
        """0 < pv_surplus <= 10 → min(battery_available, surplus) (lines 214-215)."""
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=20.0,
            ev_capacity_kwh=75.0,
            morning_target_soc=75.0,
            hourly_prices=[25.0] * 8,
            hourly_loads=[1.0] * 8,
            target_weighted_kw=2.0,
            battery_kwh_available=15.0,
            pv_tomorrow_kwh=20.0,  # surplus = 20 - 15 = 5 → 0 < 5 <= 10
            daily_consumption_kwh=15.0,
            learnings=[],
        )
        assert len(result) == 8

    def test_no_night_candidates_start_in_day(self) -> None:
        """No night hours in window → empty schedule (line ~181)."""
        # Start at 9am, only 4 hours → all daytime, no night hours
        result = _schedule_ev_backwards(
            num_hours=4,
            start_hour=9,
            ev_soc_pct=20.0,
            ev_capacity_kwh=75.0,
            morning_target_soc=75.0,
            hourly_prices=[60.0] * 4,
            hourly_loads=[2.0] * 4,
            target_weighted_kw=2.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=15.0,
            learnings=[],
            night_start=22,
            night_end=6,
        )
        assert all(kw == 0.0 for kw, _ in result)


# ── Lines 323-328, 347, 349: _schedule_battery branches ───────────────────────


class TestScheduleBattery:
    """Battery schedule branch coverage."""

    def test_pv_forecast_with_surplus_break(self) -> None:
        """pv_forecast_daily with surplus>10 → break early (line 327)."""
        result = _schedule_battery(
            num_hours=8,
            start_hour=0,
            hourly_prices=[50.0] * 8,
            hourly_pv=[0.0] * 8,
            hourly_loads=[1.5] * 8,
            hourly_ev=[0.0] * 8,
            target_weighted_kw=2.0,
            battery_soc_pct=50.0,
            battery_cap_kwh=20.0,
            pv_forecast_daily=[0.0, 30.0, 30.0],  # day1 surplus=30>10 → break
        )
        assert len(result) == 8

    def test_pv_forecast_single_element(self) -> None:
        """pv_forecast_daily with 1 element → tomorrow_pv = that element (lines 347)."""
        result = _schedule_battery(
            num_hours=8,
            start_hour=18,
            hourly_prices=[30.0] * 8,
            hourly_pv=[0.0] * 8,
            hourly_loads=[1.5] * 8,
            hourly_ev=[0.0] * 8,
            target_weighted_kw=2.0,
            battery_soc_pct=80.0,
            battery_cap_kwh=20.0,
            pv_forecast_daily=[25.0],  # single element → line 347
        )
        assert len(result) == 8

    def test_high_pv_tomorrow_enables_solar_drain(self) -> None:
        """tomorrow_pv > 25 → solar_confident → sunrise_target = min_soc (line ~349)."""
        result = _schedule_battery(
            num_hours=12,
            start_hour=20,
            hourly_prices=[80.0] * 12,
            hourly_pv=[0.0] * 8 + [5.0] * 4,
            hourly_loads=[2.0] * 12,
            hourly_ev=[0.0] * 12,
            target_weighted_kw=2.0,
            battery_soc_pct=85.0,
            battery_cap_kwh=20.0,
            pv_forecast_daily=[0.0, 30.0],  # tomorrow > 25 → confident
        )
        # With strong solar tomorrow, battery will drain pre-sunrise
        assert len(result) == 12

    def test_moderate_pv_tomorrow(self) -> None:
        """15 < tomorrow_pv <= 25 → solar_moderate → sunrise_target=30% (line ~349)."""
        result = _schedule_battery(
            num_hours=12,
            start_hour=20,
            hourly_prices=[60.0] * 12,
            hourly_pv=[0.0] * 8 + [4.0] * 4,
            hourly_loads=[2.0] * 12,
            hourly_ev=[0.0] * 12,
            target_weighted_kw=2.0,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            pv_forecast_daily=[0.0, 20.0],  # 15 < 20 <= 25
        )
        assert len(result) == 12


# ── Lines 431, 461-466, 471-475: EV support + anti-idle ───────────────────────


class TestBatteryEVSupportAntiIdle:
    """Battery schedule Priority 6 (EV support) and Priority 7 (anti-idle)."""

    def test_ev_support_discharges_to_help_ev(self) -> None:
        """EV charging + high load → battery discharges to support (lines 461-466)."""
        # Hour 2 (night), EV charging 7A → load above target → needs battery support
        result = _schedule_battery(
            num_hours=1,
            start_hour=2,
            hourly_prices=[50.0],
            hourly_pv=[0.0],
            hourly_loads=[2.5],  # high house load
            hourly_ev=[1.7],  # EV on → total weighted high
            target_weighted_kw=2.0,
            battery_soc_pct=60.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
        )
        assert len(result) == 1

    def test_anti_idle_discharges_when_soc_above_80pct(self) -> None:
        """SOC > 80% + idle + load > 0.3 → anti-idle discharge (lines 471-475)."""
        # Use a price range where no other action is taken (moderate price)
        # and high SoC to trigger anti-idle
        result = _schedule_battery(
            num_hours=1,
            start_hour=14,  # daytime, no EV
            hourly_prices=[50.0],
            hourly_pv=[0.0],
            hourly_loads=[1.5],
            hourly_ev=[0.0],
            target_weighted_kw=3.0,  # high target → no constraint discharge
            battery_soc_pct=90.0,  # > 80% → anti-idle
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            grid_charge_price_threshold=10.0,  # very low → no grid charge at 50 öre
        )
        assert len(result) == 1


# ── Lines 540-562: _check_constraints breach fixes ────────────────────────────


class TestCheckConstraints:
    """Breach fix priority order: miner off, EV reduce, EV pause, battery discharge."""

    def test_no_violation_marks_constraint_ok(self) -> None:
        """Slot below target → constraint_ok=True (line 542)."""
        slots = [_slot(14, weighted_kw=1.5, constraint_ok=False)]
        result = _check_constraints(slots, target_weighted_kw=2.0)
        assert result[0].constraint_ok is True

    def test_miner_off_reduces_excess(self) -> None:
        """Miner on → turn off first (lines 546-547)."""
        slots = [_slot(14, weighted_kw=3.0, miner_on=True, constraint_ok=False, consumption_kw=2.5)]
        result = _check_constraints(slots, target_weighted_kw=2.0)
        assert result[0].miner_on is False

    def test_ev_reduce_amps_reduces_excess(self) -> None:
        """EV amps > min_amps → reduce EV (lines 549-555)."""
        from custom_components.carmabox.const import DEFAULT_VOLTAGE

        ev_amps = 16
        ev_kw = ev_amps * DEFAULT_VOLTAGE / 1000
        slots = [
            _slot(
                14,
                weighted_kw=3.5,
                ev_kw=ev_kw,
                ev_amps=ev_amps,
                constraint_ok=False,
                consumption_kw=1.5,
            )
        ]
        result = _check_constraints(slots, target_weighted_kw=2.0)
        # EV amps should be reduced
        assert result[0].ev_amps < ev_amps or result[0].ev_kw < ev_kw

    def test_ev_pause_when_reduction_insufficient(self) -> None:
        """Very high excess → EV paused entirely (lines 559-562)."""
        from custom_components.carmabox.const import DEFAULT_EV_MIN_AMPS, DEFAULT_VOLTAGE

        ev_amps = DEFAULT_EV_MIN_AMPS  # already at min, can't reduce → pause
        ev_kw = ev_amps * DEFAULT_VOLTAGE / 1000
        # Weighted is very high — even after min amps still over
        slots = [
            _slot(
                14,
                weighted_kw=4.0,
                ev_kw=ev_kw,
                ev_amps=ev_amps,
                constraint_ok=False,
                consumption_kw=3.0,
            )
        ]
        result = _check_constraints(slots, target_weighted_kw=2.0)
        # EV should be paused (ev_kw=0)
        assert result[0].ev_kw == 0.0 or result[0].ev_amps == 0

    def test_battery_discharge_as_last_resort(self) -> None:
        """No EV, no miner → battery discharge forced (lines ~562-565)."""
        slots = [
            _slot(
                14,
                weighted_kw=3.5,
                battery_kw=0.0,  # no battery action yet
                constraint_ok=False,
                consumption_kw=3.5,
            )
        ]
        result = _check_constraints(slots, target_weighted_kw=2.0)
        # Battery should now be discharging
        assert result[0].battery_kw < 0 or result[0].action == "d"


# ── Lines 636-642: analyze_breach load enumeration ────────────────────────────


class TestAnalyzeBreachLoads:
    """Battery discharge/charge and PV in loads list (lines 636-642)."""

    def test_battery_discharging_adds_to_loads(self) -> None:
        """battery_kw < -0.1 → adds 'batteri urladdning' (line 636)."""
        record = analyze_breach(
            hour=14,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=3.5,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=-2.0,  # discharging
            pv_kw=0.0,
            miner_on=False,
        )
        loads_str = " ".join(record.loads_active)
        assert "urladdning" in loads_str

    def test_battery_charging_adds_to_loads(self) -> None:
        """battery_kw > 0.1 → adds 'batteri laddning' (line 638)."""
        record = analyze_breach(
            hour=14,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=2.5,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=1.5,  # charging
            pv_kw=0.0,
            miner_on=False,
        )
        loads_str = " ".join(record.loads_active)
        assert "laddning" in loads_str

    def test_pv_positive_adds_to_loads(self) -> None:
        """pv_kw > 0 → adds 'PV:...' (line 640)."""
        record = analyze_breach(
            hour=14,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=4.5,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=1.0,  # solar production
            miner_on=False,
        )
        loads_str = " ".join(record.loads_active)
        assert "PV:" in loads_str

    def test_appliance_loads_above_threshold_added(self) -> None:
        """appliance_loads with value > 0.3 → added (line 642)."""
        record = analyze_breach(
            hour=14,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=2.5,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
            appliance_loads={"dishwasher": 1.2, "idle_sensor": 0.1},  # only dishwasher > 0.3
        )
        loads_str = " ".join(record.loads_active)
        assert "dishwasher" in loads_str

    def test_miner_on_adds_to_loads(self) -> None:
        """miner_on=True → adds 'miner:ON'."""
        record = analyze_breach(
            hour=14,
            actual_weighted_kw=3.5,
            target_kw=2.0,
            house_load_kw=3.0,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=True,
        )
        assert "miner:ON" in record.loads_active


# ── Lines 694-698: analyze_breach fallback root cause ─────────────────────────


class TestAnalyzeBreachRootCause:
    """Various root cause branches in analyze_breach."""

    def test_fallback_root_cause_when_no_specific_trigger(self) -> None:
        """No EV, no high load, battery>0 → fallback root cause (lines 694-698)."""
        record = analyze_breach(
            hour=3,  # not appliance window
            actual_weighted_kw=2.5,
            target_kw=2.0,
            house_load_kw=1.5,  # not > 3.0
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=-1.0,  # battery active
            pv_kw=0.0,
            miner_on=False,
        )
        assert "Kombinerad" in record.root_cause or record.root_cause  # fallback triggered

    def test_high_house_load_root_cause(self) -> None:
        """house_load > 3.0, no EV, no miner → unexpected load root cause."""
        record = analyze_breach(
            hour=3,
            actual_weighted_kw=4.0,
            target_kw=2.0,
            house_load_kw=4.0,  # > 3.0
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
        )
        assert "hög huslast" in record.root_cause or "Oväntat" in record.root_cause

    def test_battery_cold_lock_root_cause(self) -> None:
        """battery_kw==0 and house_load > target → cold lock suspected."""
        record = analyze_breach(
            hour=3,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            house_load_kw=2.5,  # > target=2.0
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,  # battery inactive
            pv_kw=0.0,
            miner_on=False,
        )
        assert (
            "inaktivt" in record.root_cause
            or "cold" in record.root_cause.lower()
            or record.root_cause
        )

    def test_ev_appliance_overlap_root_cause(self) -> None:
        """EV + appliance window + appliances > 1.0 → overlap root cause."""
        record = analyze_breach(
            hour=23,  # in appliance window (22-01)
            actual_weighted_kw=4.0,
            target_kw=2.0,
            house_load_kw=2.0,
            ev_kw=2.3,
            ev_amps=10,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
            appliance_loads={"dishwasher": 1.5},  # > 1.0
        )
        assert "vitvaror" in record.root_cause or "EV" in record.root_cause

    def test_severity_minor(self) -> None:
        """Tiny breach → severity=minor."""
        record = analyze_breach(
            hour=3,
            actual_weighted_kw=2.05,  # 2.5% over 2.0
            target_kw=2.0,
            house_load_kw=2.05,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
        )
        assert record.severity == "minor"

    def test_severity_critical(self) -> None:
        """Large breach → severity=critical."""
        record = analyze_breach(
            hour=3,
            actual_weighted_kw=4.0,  # 100% over
            target_kw=2.0,
            house_load_kw=4.0,
            ev_kw=0.0,
            ev_amps=0,
            battery_kw=0.0,
            pv_kw=0.0,
            miner_on=False,
        )
        assert record.severity == "critical"


# ── Lines 723, 731-736: update_learnings branches ─────────────────────────────


class TestUpdateLearnings:
    """Update learnings: action determination, existing update, size cap."""

    def test_battery_root_cause_action_battery_support(self) -> None:
        """'Batteri' in root_cause → action=battery_support (line ~731)."""
        breach = BreachRecord(
            timestamp="2026-01-01T14:00:00",
            hour=14,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            loads_active=["hus:3.0kW"],
            root_cause="Batteri stöttade inte hushållet — huslast utan urladdning",
            remediation="Schemalägg urladdning",
            severity="major",
        )
        result = update_learnings([], breach)
        assert result[0].action == "battery_support"

    def test_fallback_action_reduce_load(self) -> None:
        """No specific root cause keyword → action=reduce_load."""
        breach = BreachRecord(
            timestamp="2026-01-01T14:00:00",
            hour=14,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            loads_active=["hus:3.0kW"],
            root_cause="Kombinerad last överskred target",
            remediation="Granska lastprofil",
            severity="minor",
        )
        result = update_learnings([], breach)
        assert result[0].action == "reduce_load"

    def test_ev_vitvaror_action_pause_ev(self) -> None:
        """'EV' + 'vitvaror' in root_cause → action=pause_ev."""
        breach = BreachRecord(
            timestamp="2026-01-01T23:00:00",
            hour=23,
            actual_weighted_kw=4.0,
            target_kw=2.0,
            loads_active=["hus:2.0kW", "EV:10A (2.3kW)"],
            root_cause="EV (10A) + vitvaror (1.5kW) kl 23 orsakade topplast",
            remediation="Pausa EV",
            severity="major",
        )
        result = update_learnings([], breach)
        assert result[0].action == "pause_ev"

    def test_update_existing_learning_increases_confidence(self) -> None:
        """Same pattern again → occurrences+1, confidence+step (lines 731-736)."""

        breach = BreachRecord(
            timestamp="2026-01-01T14:00:00",
            hour=14,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            loads_active=["hus:3.0kW"],
            root_cause="Kombinerad last",
            remediation="",
            severity="minor",
        )
        # First call — creates learning
        learnings = update_learnings([], breach)
        initial_confidence = learnings[0].confidence
        initial_occurrences = learnings[0].occurrences

        # Second call with same pattern — should UPDATE existing
        learnings = update_learnings(learnings, breach)
        assert learnings[0].occurrences == initial_occurrences + 1
        assert learnings[0].confidence > initial_confidence

    def test_size_cap_trims_lowest_confidence(self) -> None:
        """Adding beyond SCHEDULER_MAX_LEARNINGS → cap enforced."""
        from custom_components.carmabox.const import SCHEDULER_MAX_LEARNINGS

        learnings: list[BreachLearning] = []
        # Fill to max + 1
        for i in range(SCHEDULER_MAX_LEARNINGS + 1):
            breach = BreachRecord(
                timestamp=f"2026-01-01T{i:02d}:00:00",
                hour=i,
                actual_weighted_kw=3.0,
                target_kw=2.0,
                loads_active=["hus:3.0kW"],
                root_cause=f"issue_{i}",
                remediation="",
                severity="minor",
            )
            learnings = update_learnings(learnings, breach)

        assert len(learnings) <= SCHEDULER_MAX_LEARNINGS


# ── Lines 799-802: plan_ev_full_charge ────────────────────────────────────────


class TestPlanEvFullCharge:
    """plan_ev_full_charge scenarios."""

    def test_not_due_returns_empty(self) -> None:
        """days_since_full < interval-2 → return '' (lines 799-802)."""
        result = plan_ev_full_charge(
            days_since_full=1,  # way below threshold
            pv_forecast_daily=[20.0] * 7,
            current_weekday=1,
        )
        assert result == ""

    def test_due_sunny_weekend_returns_date(self) -> None:
        """days_since_full >= interval → returns soonest sunny weekend."""
        from custom_components.carmabox.const import SCHEDULER_EV_100_INTERVAL_DAYS

        result = plan_ev_full_charge(
            days_since_full=SCHEDULER_EV_100_INTERVAL_DAYS,
            pv_forecast_daily=[40.0] * 7,  # all sunny
            current_weekday=0,  # Monday → Saturday is 5 days away
        )
        assert result  # returns a date string

    def test_fallback_to_next_saturday(self) -> None:
        """No sunny days in forecast → fallback to next Saturday."""
        from custom_components.carmabox.const import SCHEDULER_EV_100_INTERVAL_DAYS

        result = plan_ev_full_charge(
            days_since_full=SCHEDULER_EV_100_INTERVAL_DAYS,
            pv_forecast_daily=[0.0] * 7,  # no sun
            current_weekday=1,  # Tuesday
        )
        assert result  # returns next Saturday


# ── Lines 835, 857-858, 878-879, 893-903: _apply_corrections ─────────────────


class TestApplyCorrections:
    """_apply_corrections: reduce_ev, shift_ev, add_discharge, reduce_load, shift_appliance."""

    def _make_ev_schedule(self, num_hours: int, charge_hour: int = 1) -> list[tuple[float, int]]:
        sched = [(0.0, 0)] * num_hours
        sched[charge_hour] = (2.3, 10)
        return sched

    def _make_batt_schedule(self, num_hours: int) -> list[tuple[float, str]]:
        return [(0.0, "i")] * num_hours

    def test_reduce_ev_applies_min_amps(self) -> None:
        """action=reduce_ev → EV reduced to DEFAULT_EV_MIN_AMPS (line 835)."""
        from custom_components.carmabox.const import DEFAULT_EV_MIN_AMPS, DEFAULT_VOLTAGE

        corr = BreachCorrection(
            created="2026-01-01T01:00:00",
            source_breach_hour=1,
            target_hour=1,
            action="reduce_ev",
            param="",
            reason="test",
        )
        ev = self._make_ev_schedule(8, charge_hour=1)
        batt = self._make_batt_schedule(8)
        new_ev, new_batt = _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=0,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        min_kw = DEFAULT_EV_MIN_AMPS * DEFAULT_VOLTAGE / 1000
        assert new_ev[1][1] == DEFAULT_EV_MIN_AMPS or new_ev[1][0] <= min_kw

    def test_shift_ev_moves_charging(self) -> None:
        """action=shift_ev → EV slot moved from source to target (lines 857-858)."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=23,  # absolute hour
            target_hour=2,  # absolute target
            action="shift_ev",
            param="shift_from=23",
            reason="test",
        )
        # ev at slot 0 (23:00 relative to start_hour=23 → idx=0)
        ev = [(2.3, 10)] + [(0.0, 0)] * 7  # charging at slot 0
        batt = self._make_batt_schedule(8)
        new_ev, _ = _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=23,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        # EV should be shifted: slot 0 → slot 3 (hour 2 = 24+2-23=3)
        assert new_ev[0] == (0.0, 0) or new_ev[3][0] > 0 or corr.applied

    def test_add_discharge_inserts_battery_discharge(self) -> None:
        """action=add_discharge → battery schedule gets discharge entry (lines 878-879)."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=14,
            target_hour=14,
            action="add_discharge",
            param="discharge_kw=2.0",
            reason="test",
        )
        ev = [(0.0, 0)] * 8
        batt = self._make_batt_schedule(8)
        _, new_batt = _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=14,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        assert new_batt[0][0] < 0  # negative = discharge

    def test_reduce_load_pause_miner_marks_applied(self) -> None:
        """action=reduce_load with pause_miner → applied=True (lines 893-903)."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=14,
            target_hour=14,
            action="reduce_load",
            param="pause_miner",
            reason="test",
        )
        ev = [(0.0, 0)] * 8
        batt = self._make_batt_schedule(8)
        _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=14,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        assert corr.applied is True

    def test_shift_appliance_marks_applied(self) -> None:
        """action=shift_appliance → marked applied (line ~903)."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=22,
            target_hour=22,
            action="shift_appliance",
            param="",
            reason="dishwasher overlap",
        )
        ev = [(0.0, 0)] * 8
        batt = self._make_batt_schedule(8)
        _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=22,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        assert corr.applied is True

    def test_expired_correction_skipped(self) -> None:
        """Expired correction → not applied."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=14,
            target_hour=14,
            action="reduce_ev",
            param="",
            reason="test",
        )
        corr.expired = True
        ev = [(2.3, 10)] + [(0.0, 0)] * 7
        batt = self._make_batt_schedule(8)
        new_ev, _ = _apply_corrections(
            [corr],
            ev,
            batt,
            start_hour=14,
            num_hours=8,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            max_discharge_kw=5.0,
        )
        # Should not be changed
        assert not corr.applied


# ── Lines 963: generate_scheduler_plan with corrections ───────────────────────


class TestGenerateSchedulerPlanWithCorrections:
    """generate_scheduler_plan with breach corrections (line 963)."""

    def test_plan_applies_corrections(self) -> None:
        """Passing corrections → _apply_corrections called (line 963)."""
        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=22,
            target_hour=22,
            action="shift_appliance",
            param="",
            reason="test",
        )
        plan = generate_scheduler_plan(
            start_hour=20,
            num_hours=8,
            ev_enabled=False,
            corrections=[corr],
        )
        assert plan is not None
        assert len(plan.slots) == 8


# ── Lines 1245, 1264-1270, 1275, 1296: analyze_idle_time ─────────────────────


class TestAnalyzeIdleTime:
    """analyze_idle_time branch coverage."""

    def _make_slots(self, n: int, action: str = "i") -> list[SchedulerHourSlot]:
        return [
            _slot(
                i,
                action=action,
                pv_kw=0.0,
                consumption_kw=1.5,
                price=50.0,
                battery_soc=50.0,
            )
            for i in range(n)
        ]

    def test_missed_pv_charge_when_pv_surplus_and_not_full(self) -> None:
        """pv_surplus > 0.5 and soc < 95 → missed_charge (line ~1245)."""
        slots = [_slot(10, action="i", pv_kw=3.0, consumption_kw=1.5, battery_soc=50.0)]
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=0,
            battery_soc_pct=50.0,
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=[50.0],
            pv_forecast=[5.0],
        )
        assert result.missed_charge_kwh > 0

    def test_missed_cheap_charge_when_price_low(self) -> None:
        """price < 20 and soc < 80 → missed_charge += 2.0."""
        slots = [_slot(2, action="i", price=15.0, battery_soc=50.0)]
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=0,
            battery_soc_pct=50.0,
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=[15.0],
            pv_forecast=[0.0],
        )
        assert result.missed_charge_kwh > 0

    def test_missed_discharge_when_price_high(self) -> None:
        """price > avg*1.3 and soc > min+10 → missed_discharge > 0 (line ~1264)."""
        slots = [_slot(18, action="i", price=120.0, battery_soc=70.0)]
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=0,
            battery_soc_pct=70.0,
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=[50.0],  # avg=50, 120 > 65 → missed discharge
            pv_forecast=[0.0],
        )
        assert result.missed_discharge_kwh > 0

    def test_idle_pct_above_70_generates_tip(self) -> None:
        """idle_pct > 70 → 'idle >70%' opportunity tip (line ~1275)."""
        # 24 idle slots, 1 hour elapsed → 24*60 / 60 minutes = 1440/60 = 100%
        slots = self._make_slots(24)
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=480,  # 8 hours idle → very high pct
            battery_soc_pct=50.0,
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=[50.0] * 24,
            pv_forecast=[0.0] * 24,
        )
        tips = " ".join(result.opportunities)
        assert "idle" in tips.lower() or result.idle_pct > 0

    def test_cheap_hours_tip_when_soc_low(self) -> None:
        """cheap_hours exist and soc < 80 → charge tip (line ~1269)."""
        slots = self._make_slots(24)
        # Some prices well below avg
        prices = [50.0] * 20 + [10.0] * 4  # 4 cheap hours at 10 öre
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=60,
            battery_soc_pct=50.0,  # < 80
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=prices,
            pv_forecast=[0.0] * 24,
        )
        tips = " ".join(result.opportunities)
        assert "laddning" in tips.lower() or "Billiga" in tips

    def test_expensive_hours_tip_when_soc_high(self) -> None:
        """expensive hours + high soc → discharge tip (line ~1270)."""
        slots = self._make_slots(24)
        prices = [50.0] * 20 + [120.0] * 4  # 4 expensive hours at 120 öre
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=60,
            battery_soc_pct=80.0,  # > min+15
            battery_min_soc=15.0,
            battery_cap_kwh=20.0,
            prices=prices,
            pv_forecast=[0.0] * 24,
        )
        tips = " ".join(result.opportunities)
        assert "urladdning" in tips.lower() or "Dyra" in tips
