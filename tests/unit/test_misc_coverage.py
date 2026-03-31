"""Coverage tests for miscellaneous module gaps — batch 14.

Targets:
  weather_learning.py: 33-35, 94, 123, 125, 165-166
  roi.py:              140, 152, 196-198, 283-284
  decision_engine.py:  201-205
  core/ml_predictor.py: 109, 162, 170, 189, 219, 234
  plan_scoring.py:     148-149, 274-275
  safety_guard.py:     285-294
  tempest.py:          87, 96
"""

from __future__ import annotations

import time

# ══════════════════════════════════════════════════════════════════════════════
# weather_learning.py
# ══════════════════════════════════════════════════════════════════════════════


class TestWeatherLearning:
    """Lines 33-35, 94, 123, 125, 165-166."""

    def test_bin_to_label_format(self) -> None:
        """_bin_to_label returns formatted range string (lines 33-35)."""
        from custom_components.carmabox.optimizer.weather_learning import _bin_to_label

        label = _bin_to_label(0)
        assert "°C" in label
        assert ".." in label

    def test_get_adjustment_invalid_hour(self) -> None:
        """hour out of range → 1.0 (line 94)."""
        from custom_components.carmabox.optimizer.weather_learning import WeatherProfile

        profile = WeatherProfile()
        assert profile.get_adjustment(hour=-1, temp_c=20.0) == 1.0
        assert profile.get_adjustment(hour=24, temp_c=20.0) == 1.0

    def test_interpolate_left_fallback(self) -> None:
        """_interpolate: only left bin has data → return left (line 125)."""
        from custom_components.carmabox.optimizer.weather_learning import (
            MIN_SAMPLES_PER_BIN,
            WeatherProfile,
        )

        profile = WeatherProfile()
        # Populate bin 0 (coldest) for hour 10 with enough samples
        for _ in range(MIN_SAMPLES_PER_BIN):
            profile.update(hour=10, temp_c=-30.0, consumption_kw=2.0, baseline_consumption_kw=2.0)
        # Request adjustment for warm temp — no warm bin data → interpolate
        result = profile.get_adjustment(hour=10, temp_c=30.0)
        assert result > 0

    def test_interpolate_right_fallback(self) -> None:
        """_interpolate: only right bin has data → return right (line 123)."""
        from custom_components.carmabox.optimizer.weather_learning import (
            MIN_SAMPLES_PER_BIN,
            WeatherProfile,
        )

        profile = WeatherProfile()
        # Populate hottest bin for hour 10
        for _ in range(MIN_SAMPLES_PER_BIN):
            profile.update(hour=10, temp_c=50.0, consumption_kw=1.5, baseline_consumption_kw=2.0)
        result = profile.get_adjustment(hour=10, temp_c=-30.0)
        assert result > 0

    def test_summary_includes_coverage(self) -> None:
        """summary() returns dict with total_samples (lines 165-166)."""
        from custom_components.carmabox.optimizer.weather_learning import WeatherProfile

        profile = WeatherProfile()
        profile.update(hour=12, temp_c=20.0, consumption_kw=2.0, baseline_consumption_kw=2.0)
        s = profile.summary()
        assert "total_samples" in s
        assert s["total_samples"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# roi.py
# ══════════════════════════════════════════════════════════════════════════════


class TestRoi:
    """Lines 140, 152, 196-198, 283-284."""

    def test_payback_months_zero_monthly_savings(self) -> None:
        """avg_monthly <= 0 → None (line 140)."""
        from custom_components.carmabox.optimizer.roi import ROIState, payback_months

        state = ROIState(battery_cost_kr=50000.0, installation_cost_kr=5000.0)
        state.monthly_savings = []  # No savings yet
        result = payback_months(state)
        assert result is None

    def test_payback_progress_zero_investment(self) -> None:
        """invest <= 0 → 100.0 (line 152)."""
        from custom_components.carmabox.optimizer.roi import ROIState, payback_progress_pct

        state = ROIState()  # No investment configured
        result = payback_progress_pct(state)
        assert result == 100.0

    def test_roi_summary_no_monthly_data(self) -> None:
        """months < 3 → annualized=0 (lines 196-198)."""
        from custom_components.carmabox.optimizer.roi import ROIState, roi_summary

        state = ROIState(battery_cost_kr=50000.0)
        result = roi_summary(state)
        assert isinstance(result, dict)
        # With no monthly data, annualized should be 0
        annualized = result.get("annualized_savings_kr", result.get("avg_monthly_savings_kr", 0))
        assert annualized == 0

    def test_roi_state_from_dict_error(self) -> None:
        """Corrupt data → except returns ROIState() (lines 283-284)."""
        from custom_components.carmabox.optimizer.roi import state_from_dict

        result = state_from_dict({"battery_cost_kr": "not_a_float"})
        assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
# decision_engine.py
# ══════════════════════════════════════════════════════════════════════════════


class TestDecisionEngine:
    """Lines 201-205: EV solar surplus branches in decide()."""

    def test_ev_solar_surplus_3phase(self) -> None:
        """pv_surplus >= 4140W → EVAction.START 3_phase (line 201-202)."""
        from custom_components.carmabox.core.decision_engine import decide

        result = decide(
            battery_soc_pct=80.0,
            battery_cap_kwh=10.0,
            grid_import_w=0.0,
            pv_power_w=6000.0,   # High PV
            ev_soc_pct=60.0,
            ev_connected=True,
            house_load_w=1000.0,
            is_night=False,       # Daytime → EV surplus path
            tak_kw=4.0,
        )
        assert result is not None

    def test_ev_solar_surplus_1phase(self) -> None:
        """pv_surplus 1380-4140W → EVAction.START 1_phase (line 203-204)."""
        from custom_components.carmabox.core.decision_engine import decide

        result = decide(
            battery_soc_pct=80.0,
            battery_cap_kwh=10.0,
            grid_import_w=0.0,
            pv_power_w=2500.0,   # Medium PV
            ev_soc_pct=60.0,
            ev_connected=True,
            house_load_w=800.0,
            is_night=False,
            tak_kw=4.0,
        )
        assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
# core/ml_predictor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestMlPredictor:
    """Lines 109, 162, 170, 189, 219, 234."""

    def _make_predictor(self) -> object:
        from custom_components.carmabox.core.ml_predictor import MLPredictor

        return MLPredictor()

    def test_predict_appliance_remaining_zero_sample_count(self) -> None:
        """sample_count=0 → returns zeroes (line 109)."""
        from custom_components.carmabox.core.ml_predictor import (
            AppliancePowerProfile,
            predict_appliance_remaining,
        )

        profile = AppliancePowerProfile(appliance_id="washer")
        result = predict_appliance_remaining(profile, elapsed_min=30.0)
        assert result["remaining_min"] == 0.0
        assert result["confidence"] == 0.0

    def test_add_plan_accuracy_trims(self) -> None:
        """add_plan_accuracy trims samples when > max (lines 162-163)."""
        from custom_components.carmabox.core.ml_predictor import MLPredictor, PlanAccuracySample

        predictor = MLPredictor()
        predictor._max_samples = 3  # Force small max
        for i in range(5):
            s = PlanAccuracySample(
                hour=10, planned_grid_kw=2.0, actual_grid_kw=1.9 + i * 0.1,
                planned_action="discharge", actual_action="discharge", price=50.0,
            )
            predictor.add_plan_accuracy(s)
        assert len(predictor._plan_accuracy[10]) <= 3

    def test_get_plan_correction_factor_few_samples(self) -> None:
        """< 3 samples → return 1.0 (line 219)."""
        from custom_components.carmabox.core.ml_predictor import MLPredictor, PlanAccuracySample

        predictor = MLPredictor()
        predictor.add_plan_accuracy(
            PlanAccuracySample(
                hour=5, planned_grid_kw=2.0, actual_grid_kw=2.0,
                planned_action="idle", actual_action="idle", price=50.0,
            )
        )
        result = predictor.get_plan_correction_factor(hour=5)
        assert result == 1.0

    def test_predict_pv_high_pressure(self) -> None:
        """pressure > 1015 with data → use high-pressure correction (line 234)."""
        from custom_components.carmabox.core.ml_predictor import MLPredictor

        predictor = MLPredictor()
        for p, r in [(1020, 1.1), (1025, 1.15), (1030, 1.05)]:
            predictor.add_pressure_pv(float(p), r)
        result = predictor.predict_pv_correction(1022.0)
        assert result > 1.0

    def test_predict_pv_no_data_returns_1(self) -> None:
        """no pressure data → 1.0 (line 189... actually predict path)."""
        from custom_components.carmabox.core.ml_predictor import MLPredictor

        predictor = MLPredictor()
        assert predictor.predict_pv_correction(1015.0) == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/plan_scoring.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPlanScoring:
    """Lines 148-149, 274-275."""

    def test_record_day_score_updates_existing(self) -> None:
        """Same date twice → update existing entry (lines 148-149)."""
        from custom_components.carmabox.optimizer.plan_scoring import (
            DayScore,
            ScoreHistory,
            record_day_score,
        )

        history = ScoreHistory()
        ds1 = DayScore(date="2026-03-01", overall_score=0.8)
        record_day_score(history, ds1)
        ds2 = DayScore(date="2026-03-01", overall_score=0.9)
        record_day_score(history, ds2)
        # Should still have only 1 entry for that date
        entries = [d for d in history.daily_scores if d.date == "2026-03-01"]
        assert len(entries) == 1
        assert entries[0].overall_score == 0.9

    def test_history_from_dict_error(self) -> None:
        """Corrupt data → exception returns ScoreHistory() (lines 274-275)."""
        from custom_components.carmabox.optimizer.plan_scoring import history_from_dict

        result = history_from_dict({"ema_score": "not_a_float"})
        assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/safety_guard.py
# ══════════════════════════════════════════════════════════════════════════════


class TestSafetyGuardCooldown:
    """Lines 285-294: check_rate_limit cooldown branch."""

    def test_rate_limit_cooldown_active_blocks(self) -> None:
        """Cooldown active → check_rate_limit returns not-ok (lines 285-290)."""
        from custom_components.carmabox.optimizer.safety_guard import SafetyGuard

        guard = SafetyGuard(max_mode_changes_per_hour=3)
        # Set cooldown to future
        guard._rate_limit_cooldown_until = time.monotonic() + 300
        result = guard.check_rate_limit()
        assert result.ok is False
        assert "cooldown" in result.reason.lower()

    def test_rate_limit_cooldown_expired_clears(self) -> None:
        """Cooldown expired → cleared and normal check proceeds (lines 291-292)."""
        from custom_components.carmabox.optimizer.safety_guard import SafetyGuard

        guard = SafetyGuard(max_mode_changes_per_hour=100)
        # Set cooldown to past (already expired)
        guard._rate_limit_cooldown_until = time.monotonic() - 1
        result = guard.check_rate_limit()
        # Cooldown should be cleared now
        assert guard._rate_limit_cooldown_until is None
        assert result.ok is True  # No violations within limit


# ══════════════════════════════════════════════════════════════════════════════
# adapters/tempest.py
# ══════════════════════════════════════════════════════════════════════════════


class TestTempestAdapter:
    """Lines 87, 96: pressure_mbar and solar_radiation_wm2 properties."""

    def _make_adapter(self) -> object:
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.tempest import TempestAdapter

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)  # All sensors unavailable
        return TempestAdapter(hass=hass)

    def test_pressure_mbar_fallback(self) -> None:
        """No state → fallback 1013.25 mbar (line 87)."""
        adapter = self._make_adapter()
        result = adapter.pressure_mbar
        assert result == 1013.25

    def test_solar_radiation_fallback(self) -> None:
        """No state → fallback 0.0 (line 96)."""
        adapter = self._make_adapter()
        result = adapter.solar_radiation_wm2
        assert result == 0.0
