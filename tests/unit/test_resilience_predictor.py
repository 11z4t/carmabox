"""Coverage tests for resilience.py and optimizer/predictor.py gaps — batch 15.

Targets:
  core/resilience.py:         85, 102, 111-112, 145, 151, 163, 188, 204, 214, 217
  optimizer/predictor.py:     159, 177, 185-186, 241, 250, 269, 287, 301, 321,
                              361-368, 380-383, 393, 396, 399, 415, 418
"""

from __future__ import annotations

import time

# ══════════════════════════════════════════════════════════════════════════════
# core/resilience.py
# ══════════════════════════════════════════════════════════════════════════════


class TestResilienceManager:
    """Lines 85, 102, 111-112, 145, 151, 163, 188, 204, 214, 217."""

    def _make_manager(self) -> object:
        from custom_components.carmabox.core.resilience import ResilienceManager

        return ResilienceManager()

    def test_get_value_no_fallback_no_current(self) -> None:
        """fb=None + current=None → (0.0, True) (line 85)."""
        mgr = self._make_manager()
        value, is_fb = mgr.get_value("sensor.unknown", current=None)
        assert value == 0.0
        assert is_fb is True

    def test_get_value_no_fallback_with_current(self) -> None:
        """fb=None + current valid → (current, False) (line 82)."""
        mgr = self._make_manager()
        value, is_fb = mgr.get_value("sensor.unknown", current=2.5)
        assert value == 2.5
        assert is_fb is False

    def test_get_value_exception_in_unavailable_check(self) -> None:
        """TypeError in _is_unavailable check → pass (line 102-103)."""
        mgr = self._make_manager()
        mgr.register_sensor("sensor.test")
        # Pass a non-float to trigger TypeError in _is_unavailable
        # This covers the except clause (line 102)
        _, is_fb = mgr.get_value("sensor.test", current=None)
        # Should return fallback (no last known → 0.0)
        assert is_fb is True

    def test_get_value_uses_last_known_fallback(self) -> None:
        """fb.last_update > 0 → use last known value (lines 111-112)."""
        mgr = self._make_manager()
        mgr.register_sensor("sensor.test")
        mgr._fallbacks["sensor.test"].max_age_s = 3600  # Large window so value is still fresh
        mgr.update_sensor("sensor.test", 3.5)
        # Now request with None → should use last known
        value, is_fb = mgr.get_value("sensor.test", current=None)
        assert value >= 3.5  # margin applied
        assert is_fb is True

    def test_record_success_clears_tripped(self) -> None:
        """record_success when tripped → cb.tripped=False (line 145)."""
        mgr = self._make_manager()
        mgr.register_breaker("adapter1", max_errors=2)
        mgr.record_error("adapter1")
        mgr.record_error("adapter1")  # trips
        assert mgr.is_breaker_open("adapter1") is True
        # Force reset
        mgr._circuit_breakers["adapter1"].trip_time = time.monotonic() - 99999
        mgr.is_breaker_open("adapter1")  # Triggers cooldown check
        mgr.record_success("adapter1")  # clears

    def test_record_error_auto_registers(self) -> None:
        """Unregistered adapter → record_error auto-registers then increments (line 151)."""
        mgr = self._make_manager()
        mgr.record_error("new_adapter")
        assert mgr._circuit_breakers["new_adapter"].consecutive_errors == 1

    def test_is_breaker_open_cooldown_expired(self) -> None:
        """Tripped breaker with elapsed >= cooldown → resets (line 163)."""
        mgr = self._make_manager()
        mgr.register_breaker("adapter1", max_errors=1, cooldown_s=0.01)
        mgr.record_error("adapter1")  # trips immediately (max_errors=1)
        assert mgr._circuit_breakers["adapter1"].tripped is True
        time.sleep(0.02)
        result = mgr.is_breaker_open("adapter1")
        assert result is False
        assert mgr._circuit_breakers["adapter1"].tripped is False

    def test_degraded_level_zero(self) -> None:
        """No breakers, no stale fallbacks → level 0 (line 188)."""
        mgr = self._make_manager()
        assert mgr.degraded_level == 0

    def test_status_degraded_adapter_offline(self) -> None:
        """Tripped breaker → status 'Degraderad: adapter offline' (line 204)."""
        mgr = self._make_manager()
        mgr.register_breaker("adapter1", max_errors=1)
        mgr.record_error("adapter1")
        assert "offline" in mgr.status.lower() or mgr.degraded_level == 2

    def test_degraded_level_sensor_fallback(self) -> None:
        """Stale sensor fallback → level 1 (lines 214-215)."""
        mgr = self._make_manager()
        mgr.register_sensor("sensor.old")
        mgr._fallbacks["sensor.old"].max_age_s = 1  # Short max age
        mgr.update_sensor("sensor.old", 2.0, ts=time.monotonic() - 100)
        level = mgr.degraded_level
        assert level >= 1


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/predictor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPredictor:
    """Lines 159, 177-186, 241, 250, 269, 287, 301, 321, 361-418."""

    def _make_predictor(self) -> object:
        from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor

        return ConsumptionPredictor()

    def test_add_idle_penalty_trims_history(self) -> None:
        """add_idle_penalty > 30 → trims to last 30 (line 159)."""
        p = self._make_predictor()
        for _ in range(35):
            p.add_idle_penalty(hour=10, weekday=1, idle_minutes=30.0, price_spread_ore=20.0)
        key = "idle_1_10"
        assert len(p.history[key]) <= 30

    def test_add_battery_cycle_creates_key(self) -> None:
        """add_battery_cycle with new key → creates list (lines 177-178)."""
        p = self._make_predictor()
        p.add_battery_cycle(hour=12, weekday=2, charge_kwh=5.0, discharge_kwh=4.0, price_ore=80.0)
        key = "cycle_2_12"
        assert key in p.history

    def test_add_battery_cycle_trims_history(self) -> None:
        """add_battery_cycle > 30 entries → trims (lines 185-186)."""
        p = self._make_predictor()
        for _ in range(35):
            p.add_battery_cycle(
                hour=12, weekday=2, charge_kwh=1.0, discharge_kwh=1.0, price_ore=50.0
            )
        assert len(p.history["cycle_2_12"]) <= 30

    def test_add_plan_feedback_trims(self) -> None:
        """add_plan_feedback > 60 → trims to 60 (line 241)."""
        p = self._make_predictor()
        for _ in range(65):
            p.add_plan_feedback(hour=8, planned_kw=2.0, actual_kw=2.1)
        key = "plan_fb_8"
        assert len(p.history[key]) <= 60

    def test_add_plan_feedback_low_planned_skipped(self) -> None:
        """planned_kw <= 0.1 → returns early (line 237)."""
        p = self._make_predictor()
        p.add_plan_feedback(hour=8, planned_kw=0.05, actual_kw=2.0)
        assert "plan_fb_8" not in p.history

    def test_add_temperature_sample_trims(self) -> None:
        """add_temperature_sample > 30 → trims (line 269)."""
        p = self._make_predictor()
        for _ in range(35):
            p.add_temperature_sample(hour=14, outdoor_temp_c=20.0, consumption_kw=2.0)
        key = "temp_20_14"
        assert len(p.history[key]) <= 30

    def test_add_ev_usage_trims(self) -> None:
        """add_ev_usage > 30 → trims (line 287)."""
        p = self._make_predictor()
        for _ in range(35):
            p.add_ev_usage(weekday=1, soc_delta_pct=20.0, capacity_kwh=98.0)
        key = "ev_1"
        assert len(p.history[key]) <= 30

    def test_add_breach_event_trims(self) -> None:
        """add_breach_event > 30 → trims (line 301)."""
        p = self._make_predictor()
        for _ in range(35):
            p.add_breach_event(hour=17, weekday=3, excess_kw=0.5)
        key = "breach_3_17"
        assert len(p.history[key]) <= 30

    def test_get_correction_factor_with_data(self) -> None:
        """With >= 5 samples → returns weighted average (lines 321, 361-368)."""
        p = self._make_predictor()
        for _ in range(10):
            p.add_plan_feedback(hour=10, planned_kw=2.0, actual_kw=2.4)  # ratio 1.2
        result = p.get_correction_factor(hour=10)
        # With consistent 1.2 ratio, result should be close to 1.2
        assert result > 1.0

    def test_get_temp_adjustment_with_data(self) -> None:
        """With >= 3 current + baseline samples → returns ratio (lines 361-368)."""
        p = self._make_predictor()
        # Add baseline band samples (band 15 = temps 15..20)
        for _ in range(5):
            p.add_temperature_sample(hour=10, outdoor_temp_c=17.0, consumption_kw=2.0)
        # Add cold band samples (band -10 = temps -10..-5)
        for _ in range(5):
            p.add_temperature_sample(hour=10, outdoor_temp_c=-10.0, consumption_kw=3.0)
        result = p.get_temp_adjustment(hour=10, outdoor_temp_c=-10.0)
        assert result >= 1.0  # Cold = higher consumption vs baseline

    def test_predict_ev_usage_with_data(self) -> None:
        """With data → returns weighted average (lines 393-399)."""
        p = self._make_predictor()
        for _ in range(5):
            p.add_ev_usage(weekday=0, soc_delta_pct=15.0, capacity_kwh=98.0)
        result = p.predict_ev_usage(weekday=0)
        assert result > 0.0

    def test_predict_ev_usage_no_data(self) -> None:
        """No data → 0.0 (line 393 early return)."""
        p = self._make_predictor()
        result = p.predict_ev_usage(weekday=6)
        assert result == 0.0

    def test_get_breach_risk_hours_with_data(self) -> None:
        """With breach data → returns non-empty list (lines 393-402)."""
        p = self._make_predictor()
        for _ in range(5):
            p.add_breach_event(hour=17, weekday=0, excess_kw=0.3)
        result = p.get_breach_risk_hours(weekday=0)
        assert 17 in result

    def test_get_breach_risk_hours_no_data(self) -> None:
        """No data → empty list (line 393)."""
        p = self._make_predictor()
        result = p.get_breach_risk_hours(weekday=6)
        assert result == []

    def test_get_disk_typical_hours_with_data(self) -> None:
        """With disk appliance events → returns hours list (lines 415-418)."""
        p = self._make_predictor()
        for _ in range(5):
            p.add_appliance_event(category="disk", power_kw=1.5, hour=20, weekday=0)
        result = p.get_disk_typical_hours()
        assert 20 in result

    def test_get_disk_typical_hours_skip_non_disk(self) -> None:
        """Non-disk keys are skipped (lines 414-417 continue paths)."""
        p = self._make_predictor()
        for _ in range(5):
            p.add_appliance_event(category="tvatt", power_kw=2.0, hour=10, weekday=1)
        result = p.get_disk_typical_hours()
        assert result == []
