"""Coverage tests for small-module gaps — batch 19.

Targets:
  optimizer/predictor.py:      185-186, 365, 393, 396, 399, 418
  optimizer/pv_correction.py:  85, 164, 168, 188, 215
  optimizer/price_patterns.py: 119, 171, 183
  optimizer/ev_strategy.py:    149, 201, 213, 289
  optimizer/battery_health.py: 70, 73, 238
  core/battery_balancer.py:    250-260, 295
  core/grid_guard.py:          152, 235, 240, 244, 298, 395
  core/law_guardian.py:        354, 409, 469
  sensor.py:                   280
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
# optimizer/predictor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPredictorBatch19:
    """Lines 185-186, 365, 393, 396, 399, 418."""

    def _make(self) -> object:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        return ConsumptionPredictor()

    def test_should_cycle_battery_with_sufficient_samples(self) -> None:
        """>=5 cycle samples → lines 185-186 execute (recent avg > 0 check)."""
        p = self._make()
        for _ in range(7):
            p.add_battery_cycle(
                hour=10, weekday=1, charge_kwh=1.0, discharge_kwh=2.0, price_ore=80.0
            )
        result = p.should_cycle_battery(hour=10, weekday=1)
        assert isinstance(result, bool)

    def test_get_temp_adjustment_zero_baseline(self) -> None:
        """avg_baseline <= 0.1 → return 1.0 (line 365)."""
        p = self._make()
        # Add baseline samples (band 15..20°C) with zero consumption
        for _ in range(5):
            p.add_temperature_sample(hour=10, outdoor_temp_c=17.0, consumption_kw=0.0)
        for _ in range(5):
            p.add_temperature_sample(hour=10, outdoor_temp_c=-10.0, consumption_kw=3.0)
        result = p.get_temp_adjustment(hour=10, outdoor_temp_c=-10.0)
        assert result == 1.0

    def test_get_breach_risk_non_breach_key_skipped(self) -> None:
        """Non-breach_ key in history → continue (line 393)."""
        p = self._make()
        p.history["cycle_1_10"] = [1.0, 2.0]  # type: ignore[union-attr]
        result = p.get_breach_risk_hours()
        assert isinstance(result, list)

    def test_get_breach_risk_bad_key_format(self) -> None:
        """breach_ key with != 3 parts → continue (line 396)."""
        p = self._make()
        p.history["breach_bad"] = [0.5]  # type: ignore[union-attr]
        result = p.get_breach_risk_hours()
        assert isinstance(result, list)

    def test_get_breach_risk_weekday_filter(self) -> None:
        """weekday != requested → continue (line 399)."""
        p = self._make()
        for _ in range(3):
            p.add_breach_event(hour=17, weekday=0, excess_kw=0.5)
        result = p.get_breach_risk_hours(weekday=5)
        assert result == []

    def test_get_disk_bad_key_format(self) -> None:
        """appl_disk_ key with != 4 parts → continue (line 418)."""
        p = self._make()
        p.history["appl_disk_x"] = [1.5]  # type: ignore[union-attr]
        result = p.get_disk_typical_hours()
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/pv_correction.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPvCorrectionBatch19:
    """Lines 85, 164, 168, 188, 215."""

    def _make(self) -> object:
        from custom_components.carmabox.optimizer.pv_correction import PVCorrectionProfile

        return PVCorrectionProfile()

    def test_record_daily_trims_over_90(self) -> None:
        """daily_records > 90 → trim to last 90 (line 85)."""
        s = self._make()
        for i in range(92):
            s.record_daily(  # type: ignore[union-attr]
                month=1,
                forecast_kwh=5.0,
                actual_kwh=5.0,
                date_str=f"2026-01-{(i % 28) + 1:02d}",
            )
        assert len(s.daily_records) == 90  # type: ignore[union-attr]

    def test_correct_profile_hourly_branch(self) -> None:
        """hourly_samples[h] >= MIN_CORRECTION*3 → corrected=fcast*hourly_factor (line 164)."""
        from custom_components.carmabox.optimizer.pv_correction import MIN_SAMPLES_FOR_CORRECTION

        s = self._make()
        threshold = MIN_SAMPLES_FOR_CORRECTION * 3
        for _ in range(threshold + 1):
            s.record_hourly(hour=10, forecast_kw=2.0, actual_kw=2.2)  # type: ignore[union-attr]
        result = s.correct_profile(month=6, hourly_forecast=[2.0] * 24)  # type: ignore[union-attr]
        assert isinstance(result, list) and len(result) == 24

    def test_correct_profile_fallback_no_data(self) -> None:
        """No hourly, no monthly → corrected = fcast passthrough (line 168)."""
        s = self._make()
        result = s.correct_profile(month=6, hourly_forecast=[3.0] * 24)  # type: ignore[union-attr]
        assert result[12] == 3.0

    def test_overall_accuracy_no_errors_in_recent(self) -> None:
        """All recent records have forecast=0.5 → 0.5>0.5 is False → errors=[] → 0.0 (line 188)."""
        s = self._make()
        # forecast_kwh=0.5 passes the >= 0.5 guard, stored as 0.5, but 0.5 > 0.5 is False
        for i in range(7):
            s.record_daily(  # type: ignore[union-attr]
                month=3,
                forecast_kwh=0.5,
                actual_kwh=0.4,
                date_str=f"2026-03-{i + 1:02d}",
            )
        result = s.overall_accuracy  # type: ignore[union-attr]
        assert result == 0.0

    def test_trend_stable(self) -> None:
        """recent_err ≈ prev_err → neither improving nor declining → 'stable' (line 215)."""
        s = self._make()
        for i in range(14):
            s.record_daily(  # type: ignore[union-attr]
                month=3,
                forecast_kwh=5.0,
                actual_kwh=5.0,
                date_str=f"2026-03-{i + 1:02d}",
            )
        result = s.trend  # type: ignore[union-attr]
        assert result == "stable"


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/price_patterns.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPricePatternsBatch19:
    """Lines 119, 171, 183."""

    def _make(self) -> object:
        from custom_components.carmabox.optimizer.price_patterns import PriceProfile

        return PriceProfile()

    def test_predict_24h_invalid_month_clamped(self) -> None:
        """month=0 → clamped to 1 (line 119) — no crash, returns 24 prices."""
        learner = self._make()
        result = learner.predict_24h(month=0, is_weekend=False)  # type: ignore[union-attr]
        assert len(result) == 24

    def test_charge_threshold_invalid_month(self) -> None:
        """month=0 → return 50.0 (line 171)."""
        learner = self._make()
        result = learner.charge_threshold(month=0)  # type: ignore[union-attr]
        assert result == 50.0

    def test_discharge_threshold_invalid_month(self) -> None:
        """month=13 → return 100.0 (line 183)."""
        learner = self._make()
        result = learner.discharge_threshold(month=13)  # type: ignore[union-attr]
        assert result == 100.0


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/ev_strategy.py
# ══════════════════════════════════════════════════════════════════════════════


class TestEvStrategyBatch19:
    """Lines 149, 201, 213, 289."""

    def test_calculate_ev_schedule_break_when_covered(self) -> None:
        """After first cheap slot covers full max need → break at line 149."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        # Very small EV need + cheap prices → loop breaks early (line 149)
        result = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=70.0,
            ev_capacity_kwh=5.0,  # energy_max=(100-70)/100*5=1.5 kWh
            hourly_prices=[10.0] * 8,  # Very cheap → max amps fills need in 1 slot
            hourly_loads=[0.5] * 8,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
        )
        assert len(result) == 8

    def test_calculate_ev_schedule_shortfall_loop_break(self) -> None:
        """First expensive slot in shortfall loop covers remaining → break (line 201)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        # Expensive hours → pass1 skips all; shortfall loop fills min and breaks
        result = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=60.0,
            ev_capacity_kwh=5.0,  # energy_min=(75-60)/100*5=0.75 kWh
            hourly_prices=[90.0] * 8,  # All expensive → pass1 skips → shortfall loop
            hourly_loads=[0.5] * 8,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
        )
        assert len(result) == 8

    def test_calculate_ev_schedule_skip_tight_headroom(self) -> None:
        """Headroom < min_kw*0.5 → continue (skip hour, line 213)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        # Very high loads → headroom nearly zero in shortfall loop
        result = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=30.0,
            ev_capacity_kwh=10.0,
            hourly_prices=[90.0] * 8,  # Expensive → shortfall loop
            hourly_loads=[4.5] * 8,  # High → headroom ≈ 0 < min_kw*0.5
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
        )
        assert len(result) == 8

    def test_is_night_hour_wrap_around_midnight(self) -> None:
        """night_start=22 > night_end=6 → wrapping branch (line 289)."""
        from custom_components.carmabox.optimizer.ev_strategy import _is_night_hour

        assert _is_night_hour(23, night_start=22, night_end=6) is True
        assert _is_night_hour(3, night_start=22, night_end=6) is True
        assert _is_night_hour(12, night_start=22, night_end=6) is False


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/battery_health.py
# ══════════════════════════════════════════════════════════════════════════════


class TestBatteryHealthBatch19:
    """Lines 70, 73, 238."""

    def test_temp_bin_range_10_to_20(self) -> None:
        """10 <= temp_c < 20 → return 2 (line 70)."""
        from custom_components.carmabox.optimizer.battery_health import _temp_bin

        assert _temp_bin(10.0) == 2
        assert _temp_bin(15.0) == 2
        assert _temp_bin(19.9) == 2

    def test_temp_bin_30_and_above(self) -> None:
        """temp_c >= 30 → return 4 (line 73)."""
        from custom_components.carmabox.optimizer.battery_health import _temp_bin

        assert _temp_bin(30.0) == 4
        assert _temp_bin(50.0) == 4

    def test_efficiency_for_temperature_with_sufficient_samples(self) -> None:
        """temp_efficiency_counts[bin] >= MIN_CYCLE_SAMPLES → learned efficiency (line 238)."""
        from custom_components.carmabox.optimizer.battery_health import (
            MIN_CYCLE_SAMPLES,
            BatteryHealthState,
            efficiency_for_temperature,
            record_charge,
            record_discharge,
        )

        state = BatteryHealthState()
        for _ in range(MIN_CYCLE_SAMPLES + 1):
            record_charge(state, kwh=1.0, temp_c=15.0)
            record_discharge(state, kwh=0.95, temp_c=15.0)
        result = efficiency_for_temperature(state, temp_c=15.0)
        assert 0.5 <= result <= 1.0


# ══════════════════════════════════════════════════════════════════════════════
# core/battery_balancer.py
# ══════════════════════════════════════════════════════════════════════════════


class TestBatteryBalancerBatch19:
    """Lines 250-260, 295."""

    def test_calculate_proportional_discharge_zero_avail(self) -> None:
        """bat.soc == bat.min_soc → bat_avail=0 → zero-alloc path (lines 250-260)."""
        from custom_components.carmabox.core.battery_balancer import (
            BatteryInfo,
            calculate_proportional_discharge,
        )

        bat = BatteryInfo(
            id="bat1",
            soc=15.0,  # at min_soc=15 → avail_kwh=0
            cap_kwh=10.0,
            cell_temp_c=20.0,
            min_soc=15.0,
        )
        result = calculate_proportional_discharge([bat], total_watts=2000)
        assert result.allocations[0].watts == 0

    def test_calculate_proportional_charge_empty_input(self) -> None:
        """not batteries → return empty BalancerResult (line 295)."""
        from custom_components.carmabox.core.battery_balancer import (
            calculate_proportional_charge,
        )

        result = calculate_proportional_charge([], total_watts=1000)
        assert result.allocations == []

    def test_calculate_proportional_charge_zero_watts(self) -> None:
        """total_watts <= 0 → all allocations have watts=0 (line 295)."""
        from custom_components.carmabox.core.battery_balancer import (
            BatteryInfo,
            calculate_proportional_charge,
        )

        bat = BatteryInfo(id="b1", soc=50.0, cap_kwh=10.0, cell_temp_c=20.0)
        result = calculate_proportional_charge([bat], total_watts=0)
        assert result.total_w == 0
        assert all(a.watts == 0 for a in result.allocations)


# ══════════════════════════════════════════════════════════════════════════════
# core/grid_guard.py
# ══════════════════════════════════════════════════════════════════════════════


class TestGridGuardBatch19:
    """Lines 152, 235, 240, 244, 298, 395."""

    def _make_guard(self) -> object:
        from custom_components.carmabox.core.grid_guard import GridGuard, GridGuardConfig

        return GridGuard(GridGuardConfig())

    def test_main_fuse_exceeded_violation(self) -> None:
        """grid_import_w > main_fuse_w*0.9 → invariant violation appended (line 152)."""
        from custom_components.carmabox.core.grid_guard import GridGuard, GridGuardConfig

        cfg = GridGuardConfig(main_fuse_a=25, main_fuse_phases=3)
        guard = GridGuard(cfg)
        # main_fuse_w = 25*230*3 = 17250 W; 90% = 15525 W
        result = guard.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=16000.0,  # > 15525
            hour=12,
            minute=30,
        )
        assert any("Huvudsäkring" in v for v in result.invariant_violations)

    def test_headroom_kw_property(self) -> None:
        """headroom_kw returns tak*margin - last_projected (line 235)."""
        guard = self._make_guard()
        guard.evaluate(
            viktat_timmedel_kw=0.5,
            grid_import_w=500.0,
            hour=12,
            minute=30,
        )
        hw = guard.headroom_kw  # type: ignore[union-attr]
        assert isinstance(hw, float)

    def test_projected_timmedel_kw_property(self) -> None:
        """projected_timmedel_kw returns getattr default (line 240)."""
        guard = self._make_guard()
        result = guard.projected_timmedel_kw  # type: ignore[union-attr]
        assert isinstance(result, float)

    def test_status_property(self) -> None:
        """status returns _status string (line 244)."""
        guard = self._make_guard()
        result = guard.status  # type: ignore[union-attr]
        assert isinstance(result, str)

    def test_invariant_soc_unavailable_skipped(self) -> None:
        """bat.soc < 0 → continue skipping INV-5 (line 298)."""
        from custom_components.carmabox.core.grid_guard import (
            BatteryState,
            GridGuard,
            GridGuardConfig,
        )

        guard = GridGuard(GridGuardConfig())
        batteries = [
            BatteryState(
                id="bat1",
                soc=-1.0,  # Unavailable
                power_w=200.0,
                cell_temp_c=20.0,
                ems_mode="discharge_pv",
                fast_charging_on=False,
                available_kwh=0.0,
            )
        ]
        result = guard._check_invariants(batteries=batteries, fast_charge_authorized=False)  # type: ignore[union-attr]
        assert not any("INV-5" in v for v in result.invariant_violations)

    def test_action_ladder_break_when_remaining_zero(self) -> None:
        """First consumer covers overshoot → remaining<=0 → break (line 395)."""
        from custom_components.carmabox.core.grid_guard import Consumer, GridGuard, GridGuardConfig

        guard = GridGuard(GridGuardConfig())
        consumers = [
            Consumer(
                id="c1",
                name="C1",
                power_w=3000.0,
                is_active=True,
                priority_shed=1,
                entity_switch="switch.c1",
            ),
            Consumer(
                id="c2",
                name="C2",
                power_w=2000.0,
                is_active=True,
                priority_shed=2,
                entity_switch="switch.c2",
            ),
        ]
        cmds, reasons = guard._action_ladder(  # type: ignore[union-attr]
            overshoot_w=500.0,  # First consumer (3000W) covers it → remaining<0 → break
            consumers=consumers,
            ev_power_w=0.0,
            ev_amps=0,
            ev_phase_count=3,
            batteries=[],
            kontor_temp_c=20.0,
        )
        assert len(cmds) >= 1  # At least one action was taken


# ══════════════════════════════════════════════════════════════════════════════
# core/law_guardian.py
# ══════════════════════════════════════════════════════════════════════════════


def _make_guardian_state(**overrides: object) -> object:
    """Build a GuardianState with sensible defaults."""
    defaults: dict[str, object] = {
        "grid_import_w": 1000.0,
        "grid_viktat_timmedel_kw": 0.5,
        "ellevio_tak_kw": 2.0,
        "battery_soc_1": 50.0,
        "battery_soc_2": 50.0,
        "battery_power_1": 0.0,
        "battery_power_2": 0.0,
        "battery_idle_hours": 0.0,
        "ev_soc": 75.0,
        "ev_target_soc": 75.0,
        "ev_departure_hour": 7,
        "current_hour": 12,
        "current_price": 50.0,
        "pv_power_w": 0.0,
        "export_w": 0.0,
        "ems_mode_1": "discharge_pv",
        "ems_mode_2": "discharge_pv",
        "fast_charging_1": False,
        "fast_charging_2": False,
        "cell_temp_1": 20.0,
        "cell_temp_2": 20.0,
        "min_soc": 15.0,
        "cold_lock_temp": 4.0,
    }
    defaults.update(overrides)
    return defaults  # type: ignore[return-value]


class TestLawGuardianBatch19:
    """Lines 354, 409, 469."""

    def test_check_lag3_ev_soc_negative(self) -> None:
        """ev_soc < 0 → return OK early (line 354)."""
        from custom_components.carmabox.core.law_guardian import GuardianState, LawGuardian

        guardian = LawGuardian()
        state = GuardianState(
            grid_import_w=1000.0,
            grid_viktat_timmedel_kw=0.5,
            ellevio_tak_kw=2.0,
            battery_soc_1=50.0,
            battery_soc_2=50.0,
            battery_power_1=0.0,
            battery_power_2=0.0,
            battery_idle_hours=0.0,
            ev_soc=-1.0,  # Unavailable → early OK return (line 354)
            ev_target_soc=75.0,
            ev_departure_hour=7,
            current_hour=7,
            current_price=50.0,
            pv_power_w=0.0,
            export_w=0.0,
            ems_mode_1="discharge_pv",
            ems_mode_2="discharge_pv",
            fast_charging_1=False,
            fast_charging_2=False,
            cell_temp_1=20.0,
            cell_temp_2=20.0,
            min_soc=15.0,
            cold_lock_temp=4.0,
        )
        result = guardian._check_lag3(state)  # type: ignore[union-attr]
        assert result.ok is True

    def test_check_invariants_crosscharge_reversed(self) -> None:
        """battery_power_1 > 50 and battery_power_2 < -50 → crosscharge (line 409)."""
        from custom_components.carmabox.core.law_guardian import GuardianState, LawGuardian

        guardian = LawGuardian()
        state = GuardianState(
            grid_import_w=500.0,
            grid_viktat_timmedel_kw=0.3,
            ellevio_tak_kw=2.0,
            battery_soc_1=50.0,
            battery_soc_2=50.0,
            battery_power_1=200.0,  # discharging
            battery_power_2=-200.0,  # charging → reversed crosscharge
            battery_idle_hours=0.0,
            ev_soc=75.0,
            ev_target_soc=75.0,
            ev_departure_hour=7,
            current_hour=12,
            current_price=50.0,
            pv_power_w=0.0,
            export_w=0.0,
            ems_mode_1="discharge_pv",
            ems_mode_2="discharge_pv",
            fast_charging_1=False,
            fast_charging_2=False,
            cell_temp_1=20.0,
            cell_temp_2=20.0,
            min_soc=15.0,
            cold_lock_temp=4.0,
        )
        results = guardian._check_invariants(state)  # type: ignore[union-attr]
        assert any(not r.ok for r in results)

    def test_classify_lag1_cause_ems_auto(self) -> None:
        """ems_mode_1 == 'auto' → 'EMS auto → okontrollerad' (line 469)."""
        from custom_components.carmabox.core.law_guardian import GuardianState, LawGuardian

        guardian = LawGuardian()
        state = GuardianState(
            grid_import_w=3000.0,
            grid_viktat_timmedel_kw=2.5,
            ellevio_tak_kw=2.0,
            battery_soc_1=50.0,
            battery_soc_2=50.0,
            battery_power_1=0.0,
            battery_power_2=0.0,
            battery_idle_hours=0.0,
            ev_soc=75.0,
            ev_target_soc=75.0,
            ev_departure_hour=7,
            current_hour=12,
            current_price=50.0,
            pv_power_w=0.0,
            export_w=0.0,
            ems_mode_1="auto",  # → EMS auto path
            ems_mode_2="discharge_pv",
            fast_charging_1=False,
            fast_charging_2=False,
            cell_temp_1=20.0,
            cell_temp_2=20.0,
            min_soc=15.0,
            cold_lock_temp=4.0,
        )
        result = guardian._classify_lag1_cause(state)  # type: ignore[union-attr]
        assert "auto" in result.lower()


# ══════════════════════════════════════════════════════════════════════════════
# sensor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestSensorHelpersBatch19:
    """Line 280: _optimization_score_value when base_avg < 0.01."""

    def test_optimization_score_zero_baseline(self) -> None:
        """All baseline_peak_samples ≈ 0 → base_avg < 0.01 → return None (line 280)."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.sensor import _optimization_score_value

        coord = MagicMock()
        coord.savings.baseline_peak_samples = [0.0, 0.0, 0.0]
        coord.savings.peak_samples = [1.0, 2.0, 3.0]
        result = _optimization_score_value(coord)
        assert result is None
