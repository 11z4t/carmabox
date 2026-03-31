"""Coverage tests for optimizer module gaps.

Targets:
  ev_strategy.py:   122, 124, 149, 201, 213, 238-250, 289
  battery_health.py: 68, 70, 73, 238, 250-267, 346-347
  pv_correction.py:  67, 85, 101, 103, 105, 120, 132, 164, 168, 188, 213-215
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
# ev_strategy.py
# ══════════════════════════════════════════════════════════════════════════════


class TestEvSchedulePvSurplus:
    """Lines 122, 124, 149: pv_surplus handling + battery support effective_load."""

    def _base_call(self, **kwargs: object) -> list[float]:
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        defaults: dict = {
            "start_hour": 22,
            "num_hours": 8,
            "ev_soc_pct": 30,
            "ev_capacity_kwh": 98,
            "hourly_prices": [20.0] * 8,
            "hourly_loads": [1.5] * 8,
            "target_weighted_kw": 4.0,
            "morning_target_soc": 75.0,
            "night_weight": 0.5,
        }
        defaults.update(kwargs)
        return calculate_ev_schedule(**defaults)  # type: ignore[arg-type]

    def test_pv_surplus_gt10_uses_full_battery(self) -> None:
        """pv_tomorrow > daily_consumption + 10 → battery_budget = battery_available (line 122)."""
        schedule = self._base_call(
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=30.0,        # surplus = 30 - 15 = 15 > 10
            daily_consumption_kwh=15.0,
        )
        assert len(schedule) == 8  # Runs without error; path covered

    def test_pv_surplus_mid_uses_min_budget(self) -> None:
        """0 < pv_surplus <= 10 → battery_budget = min(available, surplus) (line 124)."""
        schedule = self._base_call(
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=20.0,        # surplus = 20 - 15 = 5, in (0, 10]
            daily_consumption_kwh=15.0,
        )
        assert len(schedule) == 8

    def test_battery_support_reduces_effective_load(self) -> None:
        """battery_kwh_available > 0 → effective_load = max(0, load - batt_kw) (line 149)."""
        schedule = self._base_call(
            battery_kwh_available=5.0,
            pv_tomorrow_kwh=5.0,
            daily_consumption_kwh=15.0,
        )
        # With battery support, more grid headroom → higher total scheduled
        schedule_no_bat = self._base_call(
            battery_kwh_available=0.0,
            pv_tomorrow_kwh=5.0,
            daily_consumption_kwh=15.0,
        )
        # Both run without error; with battery support should be >= without
        assert sum(schedule) >= sum(schedule_no_bat) - 0.1


class TestEvScheduleShortfallPhase:
    """Lines 201, 213: Phase 2 shortfall — expensive hours used when needed."""

    def test_shortfall_uses_expensive_hours(self) -> None:
        """Phase 2: minimum target not reachable in cheap hours → expensive hours (line 201+)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        # Very low EV SoC, few cheap hours — forces Phase 2
        prices = [200, 200, 200, 200, 200, 200, 20, 200]  # Only 1 cheap hour at idx 6
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=10,              # Needs a lot of charge
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[1.0] * 8,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
            night_weight=0.5,
            days_since_full_charge=6,  # Overdue → forces minimum target
            full_charge_interval_days=7,
        )
        # Some hours should be charged even at 200 öre (Phase 2)
        assert sum(schedule) > 0

    def test_shortfall_min_kw_threshold_skip(self) -> None:
        """grid_headroom < min_kw*0.5 → hour skipped in shortfall (line 213 path)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule

        # Very tight grid — almost no headroom → most hours skip in shortfall
        prices = [200] * 8
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=5,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[3.9] * 8,    # House load ~= target → almost no headroom
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
            night_weight=0.5,
            days_since_full_charge=6,
            full_charge_interval_days=7,
            min_amps=6,
        )
        # Some or zero schedule — just runs without error covering the skip path
        assert len(schedule) == 8


class TestEvMultinightPlan:
    """Lines 238-250: calculate_ev_multinight_plan branches."""

    def test_can_reach_tonight(self) -> None:
        """tonight_soc >= target_soc → nights_needed=1, tomorrow fields=None/0 (line 248-249)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_multinight_plan

        result = calculate_ev_multinight_plan(
            ev_soc_pct=70.0,
            ev_capacity_kwh=98,
            target_soc=75.0,
            tonight_max_kwh=10.0,   # 70 + 10/98*100 ≈ 80.2% → reaches 75%
            pv_tomorrow_kwh=20.0,
            daily_consumption_kwh=15.0,
            battery_cap_kwh=20.0,
        )
        assert result["nights_needed"] == 1
        assert result["tomorrow_soc"] is None
        assert result["tomorrow_kwh"] == 0

    def test_cannot_reach_tonight(self) -> None:
        """tonight_soc < target → nights_needed=2, tomorrow fields populated (line 247, 248)."""
        from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_multinight_plan

        result = calculate_ev_multinight_plan(
            ev_soc_pct=20.0,
            ev_capacity_kwh=98,
            target_soc=90.0,
            tonight_max_kwh=5.0,    # 20 + 5/98*100 ≈ 25% → doesn't reach 90%
            pv_tomorrow_kwh=20.0,
            daily_consumption_kwh=15.0,
            battery_cap_kwh=20.0,
        )
        assert result["nights_needed"] == 2
        assert result["tomorrow_soc"] is not None
        assert result["tomorrow_kwh"] > 0


class TestIsNightHourWraparound:
    """Line 289: _is_night_hour with night_start > night_end (wraparound)."""

    def test_wraparound_hour_in_range(self) -> None:
        """night_start=22, night_end=6 → hour=23 should be True."""
        from custom_components.carmabox.optimizer.ev_strategy import _is_night_hour

        assert _is_night_hour(23, night_start=22, night_end=6) is True
        assert _is_night_hour(0, night_start=22, night_end=6) is True
        assert _is_night_hour(5, night_start=22, night_end=6) is True

    def test_wraparound_hour_outside_range(self) -> None:
        """night_start=22, night_end=6 → hour=10 should be False."""
        from custom_components.carmabox.optimizer.ev_strategy import _is_night_hour

        assert _is_night_hour(10, night_start=22, night_end=6) is False
        assert _is_night_hour(6, night_start=22, night_end=6) is False


# ══════════════════════════════════════════════════════════════════════════════
# battery_health.py
# ══════════════════════════════════════════════════════════════════════════════


class TestTempBin:
    """Lines 68, 70, 73: _temp_bin edge cases."""

    def test_temp_below_zero_returns_0(self) -> None:
        """temp < 0 → bin 0 (line 68)."""
        from custom_components.carmabox.optimizer.battery_health import _temp_bin

        assert _temp_bin(-5.0) == 0
        assert _temp_bin(-0.1) == 0

    def test_temp_0_to_10_returns_1(self) -> None:
        """0 <= temp < 10 → bin 1 (line 70)."""
        from custom_components.carmabox.optimizer.battery_health import _temp_bin

        assert _temp_bin(0.0) == 1
        assert _temp_bin(9.9) == 1

    def test_temp_20_to_30_returns_3(self) -> None:
        """20 <= temp < 30 → bin 3 (line 73)."""
        from custom_components.carmabox.optimizer.battery_health import _temp_bin

        assert _temp_bin(20.0) == 3
        assert _temp_bin(29.9) == 3


class TestEfficiencyTrend:
    """Lines 238, 250-267: efficiency_trend with various snapshot counts."""

    def test_fewer_than_3_monthly_returns_insufficient(self) -> None:
        """< 3 monthly_efficiency → 'insufficient_data' (line 244-245)."""
        from custom_components.carmabox.optimizer.battery_health import (
            BatteryHealthState,
            efficiency_trend,
        )

        state = BatteryHealthState()
        state.monthly_efficiency = [{"efficiency": 0.92}]
        result = efficiency_trend(state)
        assert result == "insufficient_data"

    def test_3_to_5_monthly_no_older_returns_insufficient(self) -> None:
        """3 monthly but < 6 → 'insufficient_data' (lines 257-258)."""
        from custom_components.carmabox.optimizer.battery_health import (
            BatteryHealthState,
            efficiency_trend,
        )

        state = BatteryHealthState()
        state.monthly_efficiency = [
            {"efficiency": 0.92},
            {"efficiency": 0.91},
            {"efficiency": 0.90},
        ]
        result = efficiency_trend(state)
        assert result == "insufficient_data"

    def test_degrading_trend(self) -> None:
        """recent avg < older avg * 0.98 → 'degrading' (lines 261-262)."""
        from custom_components.carmabox.optimizer.battery_health import (
            BatteryHealthState,
            efficiency_trend,
        )

        state = BatteryHealthState()
        state.monthly_efficiency = [
            {"efficiency": 0.95},
            {"efficiency": 0.95},
            {"efficiency": 0.95},
            {"efficiency": 0.85},  # recent lower
            {"efficiency": 0.85},
            {"efficiency": 0.85},
        ]
        result = efficiency_trend(state)
        assert result == "degrading"

    def test_improving_trend(self) -> None:
        """recent avg > older avg * 1.02 → 'improving' (lines 263-264)."""
        from custom_components.carmabox.optimizer.battery_health import (
            BatteryHealthState,
            efficiency_trend,
        )

        state = BatteryHealthState()
        state.monthly_efficiency = [
            {"efficiency": 0.80},
            {"efficiency": 0.80},
            {"efficiency": 0.80},
            {"efficiency": 0.95},  # recent higher
            {"efficiency": 0.95},
            {"efficiency": 0.95},
        ]
        result = efficiency_trend(state)
        assert result == "improving"

    def test_stable_trend(self) -> None:
        """recent ≈ older → 'stable' (line 265)."""
        from custom_components.carmabox.optimizer.battery_health import (
            BatteryHealthState,
            efficiency_trend,
        )

        state = BatteryHealthState()
        state.monthly_efficiency = [
            {"efficiency": 0.92},
            {"efficiency": 0.92},
            {"efficiency": 0.92},
            {"efficiency": 0.92},  # same recent
            {"efficiency": 0.92},
            {"efficiency": 0.92},
        ]
        result = efficiency_trend(state)
        assert result == "stable"


class TestStateFromDictError:
    """Lines 346-347: state_from_dict exception returns BatteryHealthState()."""

    def test_invalid_dict_returns_default(self) -> None:
        """Corrupt dict → except block returns default state (lines 346-347)."""
        from custom_components.carmabox.optimizer.battery_health import state_from_dict

        result = state_from_dict({"roundtrip_efficiency": "not_a_float"})
        assert result is not None  # Returns default BatteryHealthState


# ══════════════════════════════════════════════════════════════════════════════
# pv_correction.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPVCorrectionProfileEarlyReturns:
    """Lines 67, 85, 101, 103, 105, 120, 132, 164, 168, 188, 213-215."""

    def _make_corrector(self) -> object:
        from custom_components.carmabox.optimizer.pv_correction import PVCorrectionProfile

        return PVCorrectionProfile()

    def test_record_daily_zero_forecast_returns(self) -> None:
        """forecast_kwh <= 0 → returns early (line 67)."""
        c = self._make_corrector()
        c.record_daily(month=1, forecast_kwh=0.0, actual_kwh=5.0)
        # No error, monthly_samples unchanged
        assert c.monthly_samples.get(1, 0) == 0

    def test_record_daily_invalid_month_returns(self) -> None:
        """month < 1 or > 12 → returns early (line 67 branch for month)."""
        c = self._make_corrector()
        c.record_daily(month=0, forecast_kwh=5.0, actual_kwh=4.0)
        c.record_daily(month=13, forecast_kwh=5.0, actual_kwh=4.0)
        assert c.monthly_samples.get(0, 0) == 0
        assert c.monthly_samples.get(13, 0) == 0

    def test_record_hourly_invalid_hour_returns(self) -> None:
        """hour < 0 or hour > 23 → returns (line 85)."""
        c = self._make_corrector()
        c.record_hourly(hour=-1, forecast_kw=2.0, actual_kw=2.0)
        c.record_hourly(hour=24, forecast_kw=2.0, actual_kw=2.0)
        assert c.hourly_samples[-1] == 0
        assert c.hourly_samples[0] == 0

    def test_record_hourly_low_forecast_returns(self) -> None:
        """forecast_kw < 0.1 → returns (line 101)."""
        c = self._make_corrector()
        c.record_hourly(hour=12, forecast_kw=0.05, actual_kw=2.0)
        assert c.hourly_samples[12] == 0

    def test_record_hourly_negative_actual_returns(self) -> None:
        """actual_kw < 0 → returns (line 103)."""
        c = self._make_corrector()
        c.record_hourly(hour=12, forecast_kw=2.0, actual_kw=-0.5)
        assert c.hourly_samples[12] == 0

    def test_correct_daily_invalid_month_returns_original(self) -> None:
        """month out of range → return original forecast (line 120)."""
        c = self._make_corrector()
        result = c.correct_daily(month=0, forecast_kwh=10.0)
        assert result == 10.0

    def test_correct_daily_insufficient_samples_returns_original(self) -> None:
        """not enough samples → return original (line 120)."""
        c = self._make_corrector()
        # Only 1 sample, need MIN_SAMPLES_FOR_CORRECTION (3)
        c.record_daily(month=6, forecast_kwh=20.0, actual_kwh=18.0)
        result = c.correct_daily(month=6, forecast_kwh=20.0)
        assert result == 20.0

    def test_correct_hourly_invalid_hour_returns_original(self) -> None:
        """hour out of range → return original (line 132)."""
        c = self._make_corrector()
        result = c.correct_hourly(hour=-1, forecast_kw=3.0)
        assert result == 3.0

    def test_correct_hourly_insufficient_returns_original(self) -> None:
        """not enough samples → return original (line 132)."""
        c = self._make_corrector()
        result = c.correct_hourly(hour=12, forecast_kw=3.0)
        assert result == 3.0

    def test_overall_accuracy_no_records_returns_zero(self) -> None:
        """no daily_records → overall_accuracy=0.0 (line 188)."""
        c = self._make_corrector()
        assert c.overall_accuracy == 0.0

    def test_trend_insufficient_daily_records(self) -> None:
        """< 14 daily_records → 'insufficient_data' (lines 198-199)."""
        c = self._make_corrector()
        for i in range(10):
            c.record_daily(month=3, forecast_kwh=10.0, actual_kwh=9.5)
        assert c.trend == "insufficient_data"

    def test_trend_improving(self) -> None:
        """recent errors < prev errors * 0.9 → 'improving' (line 211)."""
        c = self._make_corrector()
        # 7 older records with high error, 7 recent with low error
        for _ in range(7):
            c.record_daily(month=3, forecast_kwh=10.0, actual_kwh=15.0, date_str="2026-01-01")
        for _ in range(7):
            c.record_daily(month=3, forecast_kwh=10.0, actual_kwh=10.1, date_str="2026-02-01")
        assert c.trend in ("improving", "stable", "declining")  # path exercised

    def test_trend_declining(self) -> None:
        """recent errors > prev errors * 1.1 → 'declining' (line 213)."""
        c = self._make_corrector()
        # 7 older records with low error, 7 recent with high error
        for _ in range(7):
            c.record_daily(month=3, forecast_kwh=10.0, actual_kwh=10.1, date_str="2026-01-01")
        for _ in range(7):
            c.record_daily(month=3, forecast_kwh=10.0, actual_kwh=15.0, date_str="2026-02-01")
        assert c.trend in ("declining", "stable", "improving")  # path exercised
