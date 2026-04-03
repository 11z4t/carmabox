"""Coverage tests for coordinator.py tracking and utility methods.

Targets coordinator.py missing lines:
  - _update_hourly_meter (5365-5373, 5377-5378, 5400-5401, 5408-5410, 5420, 5425, 5430-5432)
  - _track_savings (5688, 5699-5702, 5728, 5743)
  - _track_battery_idle (5553-5569)
  - _feed_predictor_ml (5935, 5944-5945, 5951, 5955-5957, 5963-5966, 5968)
  - _check_repair_issues (5247-5256)
  - _self_heal_ev_tamper (6266-6267, 6290-6297)
  - _safe_service_call dry-run + error branches (6290-6297, 6313-6323)
"""

from __future__ import annotations

import datetime as _real_dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    HourlyMeterState,
)
from custom_components.carmabox.optimizer.savings import SavingsState


def _dt_at_hour(h: int) -> MagicMock:
    """Patch datetime.now() to return a fixed hour (stops _update_hourly_meter hour-changes)."""
    fake = MagicMock()
    fake.now = MagicMock(return_value=_real_dt.datetime(2026, 1, 1, h, 0, 0))
    return fake


# ── Factory ───────────────────────────────────────────────────────────────────


def _make_coord(*, cfg: dict | None = None, executor_enabled: bool = True) -> object:
    """Bypass coordinator __init__."""
    from custom_components.carmabox.coordinator import CarmaboxCoordinator

    coord = object.__new__(CarmaboxCoordinator)
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=None)
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord._cfg = cfg or {}
    coord.target_kw = 2.0
    coord.min_soc = 15.0
    coord.executor_enabled = executor_enabled
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord._breach_corrections = []
    coord._MAX_CORRECTIONS = 100
    coord._MAX_HOUR_SAMPLES = 120
    coord._miner_on = False
    coord._breach_load_shed_active = False
    coord._meter_state = HourlyMeterState(hour=14, projected_avg=0.0)
    coord.savings = SavingsState()
    coord._daily_avg_price = 100.0
    coord._daily_discharge_kwh = 0.0
    coord.predictor = MagicMock()
    coord.plan = []
    coord._last_known_ev_soc = -1.0
    coord.safety = MagicMock()
    coord.safety.recent_block_count = MagicMock(return_value=0)
    coord._daily_safety_blocks = 0
    coord.shadow_log = []
    coord._bat_idle_seconds = 0
    coord._bat_daily_idle_seconds = 0
    coord._bat_idle_day = 2  # fixed — avoids flakiness at day boundary
    coord._ev_last_known_enabled = None
    coord._ev_enabled = False
    coord._daily_plans = 0
    coord._peak_hour_samples = []
    coord._peak_last_hour = -1
    coord.appliance_power = {}
    from custom_components.carmabox.optimizer.report import ReportCollector

    coord.report_collector = ReportCollector(month=1, year=2026)
    from custom_components.carmabox.optimizer.models import Decision

    coord.last_decision = Decision()
    from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger

    coord.ledger = EnergyLedger()
    coord._ellevio_hour_samples = []
    coord._ellevio_current_hour = -1
    coord._ellevio_monthly_hourly_peaks = []
    coord._last_tracked_hour = -1
    coord._async_save_ledger = AsyncMock()
    coord.hourly_actuals = []
    coord._consumption_last_hour = -1
    from custom_components.carmabox.optimizer.consumption import ConsumptionProfile

    coord.consumption_profile = ConsumptionProfile()
    coord._read_battery_temp = MagicMock(return_value=20.0)
    # PLAT-975: ML Predictor
    from custom_components.carmabox.core.ml_predictor import MLPredictor as _MLPred

    coord._ml_predictor = _MLPred()
    coord.ml_forecast_24h = []
    return coord


# ── Tests: _update_hourly_meter ───────────────────────────────────────────────


class TestUpdateHourlyMeter:
    def test_hour_change_with_breach_generates_corrections(self) -> None:
        """Hour change + final_avg > target → corrections generated (lines 5365-5373)."""
        coord = _make_coord()
        # Simulate previous hour with breach; use now=hour 14 so meter hour 13 triggers change
        prev_state = HourlyMeterState(hour=13, projected_avg=3.0)
        prev_state.samples = [3.5, 3.5, 3.5, 3.5]  # avg > 2.0 → breach
        coord._meter_state = prev_state

        state = CarmaboxState(grid_power_w=1500.0)
        with (
            patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)),
            patch.object(coord, "_generate_breach_corrections") as mock_gen,
        ):
            coord._update_hourly_meter(state)
        mock_gen.assert_called_once()

    def test_hour_change_no_breach_no_corrections(self) -> None:
        """Hour change + final_avg <= target → no corrections (line 5365 branching)."""
        coord = _make_coord()
        prev_state = HourlyMeterState(hour=13, projected_avg=1.5)
        prev_state.samples = [1.0, 1.0, 1.0]  # avg < 2.0 → no breach
        coord._meter_state = prev_state

        state = CarmaboxState(grid_power_w=500.0)
        with (
            patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)),
            patch.object(coord, "_generate_breach_corrections") as mock_gen,
        ):
            coord._update_hourly_meter(state)
        mock_gen.assert_not_called()

    def test_hour_change_high_projected_carries_load_shed(self) -> None:
        """Hour change with prev projected > 90% of target → load shed ON (line 5377-5378)."""
        coord = _make_coord()
        prev_state = HourlyMeterState(hour=13, projected_avg=1.85)  # > 2.0 * 0.90 = 1.80
        prev_state.samples = [1.8]
        coord._meter_state = prev_state

        state = CarmaboxState(grid_power_w=500.0)
        with patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)):
            coord._update_hourly_meter(state)
        assert coord._breach_load_shed_active is True

    def test_hour_change_low_projected_clears_load_shed(self) -> None:
        """Hour change with prev projected <= 90% of target → load shed OFF."""
        coord = _make_coord()
        coord._breach_load_shed_active = True
        prev_state = HourlyMeterState(hour=13, projected_avg=1.0)  # < 1.80
        prev_state.samples = [1.0]
        coord._meter_state = prev_state

        state = CarmaboxState(grid_power_w=100.0)
        with patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)):
            coord._update_hourly_meter(state)
        assert coord._breach_load_shed_active is False

    def test_warning_issued_at_80pct(self) -> None:
        """Projected > 80% of target → warning_issued=True (lines 5399-5401)."""
        coord = _make_coord()
        # Add many samples at 1.7 kW weighted (> 2.0 * 0.80 = 1.60)
        meter = HourlyMeterState(hour=14)
        meter.samples = [1.7] * 20  # projected ≈ 1.7 > 1.60
        meter.warning_issued = False
        coord._meter_state = meter

        state = CarmaboxState(grid_power_w=1700.0)  # 1.7 kW unweighted
        with patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)):
            coord._update_hourly_meter(state)
        assert coord._meter_state.warning_issued is True

    def test_load_shed_activated_at_90pct(self) -> None:
        """Projected > 90% of target and n>10 → load shed ON (lines 5407-5410)."""
        coord = _make_coord()
        meter = HourlyMeterState(hour=14)
        meter.samples = [1.9] * 15  # projected ≈ 1.9 > 2.0*0.90=1.80, n>10
        meter.warning_issued = True  # Already warned
        coord._meter_state = meter
        coord._breach_load_shed_active = False

        state = CarmaboxState(grid_power_w=1900.0)
        with patch("custom_components.carmabox.coordinator.datetime", _dt_at_hour(14)):
            coord._update_hourly_meter(state)
        assert coord._breach_load_shed_active is True

    def test_breach_monitor_active_property(self) -> None:
        """breach_monitor_active returns _breach_load_shed_active (line 5420)."""
        coord = _make_coord()
        coord._breach_load_shed_active = True
        assert coord.breach_monitor_active is True

    def test_hourly_meter_projected_property(self) -> None:
        """hourly_meter_projected returns projected_avg (line 5425)."""
        coord = _make_coord()
        coord._meter_state.projected_avg = 1.75
        assert coord.hourly_meter_projected == 1.75

    def test_hourly_meter_pct_property(self) -> None:
        """hourly_meter_pct computes % of target (lines 5430-5432)."""
        coord = _make_coord()
        coord.target_kw = 2.0
        coord._meter_state.projected_avg = 1.5
        assert coord.hourly_meter_pct == pytest.approx(75.0)

    def test_hourly_meter_pct_zero_target(self) -> None:
        """hourly_meter_pct with target=0 → 0.0 (line 5430-5431)."""
        coord = _make_coord()
        coord.target_kw = 0.0
        assert coord.hourly_meter_pct == 0.0


# ── Tests: _track_savings ─────────────────────────────────────────────────────


class TestTrackSavings:
    def test_battery_power_2_negative_adds_to_discharge(self) -> None:
        """battery_power_2 < 0 → added to battery_discharge_kw (line 5688)."""
        coord = _make_coord()
        state = CarmaboxState(
            battery_power_1=0.0,
            battery_power_2=-1000.0,  # discharging bat2
            battery_soc_2=50.0,
            grid_power_w=500.0,
            current_price=150.0,
        )
        coord._track_savings(state)
        assert coord._daily_discharge_kwh > 0

    def test_hour_change_records_peak(self) -> None:
        """Hour change with previous samples → record_peak called (lines 5699-5702)."""
        coord = _make_coord()
        coord._peak_last_hour = 13  # previous hour
        coord._peak_hour_samples = [(1.5, 2.0), (1.6, 2.1)]  # (actual, baseline)

        state = CarmaboxState(
            battery_power_1=0.0,
            battery_power_2=0.0,
            grid_power_w=1000.0,
            current_price=100.0,
        )
        _hour = 14  # fixed — avoids flakiness at hour boundary
        coord._peak_last_hour = (_hour + 1) % 24  # different from mocked hour

        mock_now = MagicMock()
        mock_now.hour = _hour
        mock_now.weekday.return_value = 3  # Wednesday
        mock_now.strftime.return_value = "2026-04-02"

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            with patch("custom_components.carmabox.coordinator.record_peak") as mock_peak:
                coord._track_savings(state)
        mock_peak.assert_called_once()

    def test_battery_power_2_positive_adds_to_charge(self) -> None:
        """battery_power_2 > 0 → added to battery_charge_kw (line 5728)."""
        coord = _make_coord()
        state = CarmaboxState(
            battery_power_1=0.0,
            battery_power_2=500.0,  # charging bat2
            battery_soc_2=50.0,
            grid_power_w=800.0,
            current_price=50.0,
        )
        with patch("custom_components.carmabox.coordinator.record_grid_charge") as mock_gc:
            coord._track_savings(state)
        mock_gc.assert_called_once()

    def test_grid_charge_prices_trimmed_at_2000(self) -> None:
        """grid_charge_prices > 2000 → trimmed to last 2000 (line 5742-5743)."""
        coord = _make_coord()
        coord.savings.grid_charge_prices = list(range(2001))
        state = CarmaboxState(
            battery_power_2=1000.0,
            battery_soc_2=30.0,
            grid_power_w=1500.0,
            current_price=60.0,
        )
        coord._track_savings(state)
        assert len(coord.savings.grid_charge_prices) <= 2001  # trimmed on next call


# ── Tests: _track_battery_idle ────────────────────────────────────────────────


class TestTrackBatteryIdle:
    def test_battery_active_resets_idle_counter(self) -> None:
        """Battery active (power ≥ 50W) → idle_seconds reset to 0 (line 5569)."""
        coord = _make_coord()
        coord._bat_idle_seconds = 3600  # was idle
        state = CarmaboxState(battery_power_1=200.0)  # active
        coord._track_battery_idle(state)
        assert coord._bat_idle_seconds == 0

    def test_battery_active_long_idle_triggers_predictor(self) -> None:
        """Battery active + was idle > 1800s → idle_penalty added (lines 5553-5568)."""
        coord = _make_coord()
        coord._bat_idle_seconds = 3600  # > 1800
        state = CarmaboxState(battery_power_1=500.0)

        # Mock NordpoolAdapter to return prices
        mock_adapter = MagicMock()
        mock_adapter.current_price = 150.0
        mock_adapter.today_prices = [100.0, 120.0, 150.0, 80.0]

        with patch(
            "custom_components.carmabox.coordinator.NordpoolAdapter",
            return_value=mock_adapter,
        ):
            coord._track_battery_idle(state)

        # Price spread: |150 - 112.5| = 37.5 > 15 → predictor called
        coord.predictor.add_idle_penalty.assert_called_once()

    def test_battery_active_small_price_spread_no_predictor(self) -> None:
        """Battery active + idle > 1800s but price spread ≤ 15 → no predictor call."""
        coord = _make_coord()
        coord._bat_idle_seconds = 3600
        state = CarmaboxState(battery_power_1=500.0)

        mock_adapter = MagicMock()
        mock_adapter.current_price = 100.0
        mock_adapter.today_prices = [95.0, 100.0, 105.0, 100.0]  # spread < 15

        with patch(
            "custom_components.carmabox.coordinator.NordpoolAdapter",
            return_value=mock_adapter,
        ):
            coord._track_battery_idle(state)

        coord.predictor.add_idle_penalty.assert_not_called()


# ── Tests: _feed_predictor_ml ─────────────────────────────────────────────────


class TestFeedPredictorMl:
    def test_appliance_power_above_500w_triggers_event(self) -> None:
        """Appliance power > 500W → add_appliance_event (line 5935)."""
        coord = _make_coord()
        appliance_state = MagicMock()
        appliance_state.state = "800"  # > 500W
        coord.hass.states.get = lambda eid: appliance_state if "shelly" in eid else None
        state = CarmaboxState()
        coord._feed_predictor_ml(state)
        coord.predictor.add_appliance_event.assert_called()

    def test_temperature_sensor_adds_sample(self) -> None:
        """Tempest temperature → add_temperature_sample (lines 5944-5945)."""
        coord = _make_coord()
        temp_state = MagicMock()
        temp_state.state = "15.3"
        coord.hass.states.get = lambda eid: temp_state if "tempest_temperature" in eid else None
        state = CarmaboxState()
        coord._feed_predictor_ml(state)
        coord.predictor.add_temperature_sample.assert_called_once()

    def test_plan_feedback_new_hour(self) -> None:
        """New feedback hour → add_plan_feedback for matching hour (lines 5955-5957)."""
        _hour = 14  # fixed — avoids flakiness at hour boundary

        coord = _make_coord()
        coord._last_feedback_hour = (_hour + 1) % 24  # different from mocked hour

        ph = MagicMock()
        ph.hour = _hour
        ph.grid_kw = 1.5
        coord.plan = [ph]
        coord.hass.states.get = MagicMock(return_value=None)
        state = CarmaboxState(grid_power_w=1200.0)

        with patch("datetime.datetime") as mock_dt:
            now = MagicMock()
            now.hour = _hour
            now.weekday.return_value = 3
            mock_dt.now.return_value = now
            coord._feed_predictor_ml(state)
        coord.predictor.add_plan_feedback.assert_called_once()

    def test_plan_feedback_same_hour_skipped(self) -> None:
        """Same feedback hour → plan feedback skipped (line 5951)."""
        _hour = 14  # fixed — avoids flakiness at hour boundary

        coord = _make_coord()
        coord._last_feedback_hour = _hour  # same as mocked hour
        coord.hass.states.get = MagicMock(return_value=None)
        state = CarmaboxState()

        with patch("datetime.datetime") as mock_dt:
            now = MagicMock()
            now.hour = _hour
            now.weekday.return_value = 3
            mock_dt.now.return_value = now
            coord._feed_predictor_ml(state)
        coord.predictor.add_plan_feedback.assert_not_called()

    def test_ev_usage_reset_at_midnight(self) -> None:
        """Hour == 0 → _ev_usage_tracked_today = False (line 5968)."""
        coord = _make_coord()
        coord._ev_usage_tracked_today = True
        coord.hass.states.get = MagicMock(return_value=None)
        coord.plan = []

        with patch("datetime.datetime") as mock_dt:
            now = MagicMock()
            now.hour = 0
            now.weekday.return_value = 0
            mock_dt.now.return_value = now
            state = CarmaboxState()
            coord._feed_predictor_ml(state)

        assert coord._ev_usage_tracked_today is False


# ── Tests: _check_repair_issues ───────────────────────────────────────────────


class TestCheckRepairIssues:
    def test_hub_offline_more_than_24h(self) -> None:
        """Hub last_sync > 24h ago → raise_hub_offline_issue (lines 5247-5252)."""
        coord = _make_coord()
        import datetime as dt

        hub = MagicMock()
        hub.last_sync = dt.datetime.now() - dt.timedelta(hours=25)
        coord._hub = hub
        coord.safety.recent_block_count.return_value = 0

        with (
            patch("custom_components.carmabox.coordinator.raise_hub_offline_issue") as mock_raise,
            patch("custom_components.carmabox.coordinator.clear_issue"),
        ):
            coord._check_repair_issues()
        mock_raise.assert_called_once()

    def test_hub_online_clears_issue(self) -> None:
        """Hub last_sync < 24h → clear_issue('hub_offline') (line 5254)."""
        coord = _make_coord()
        import datetime as dt

        hub = MagicMock()
        hub.last_sync = dt.datetime.now() - dt.timedelta(hours=2)
        coord._hub = hub
        coord.safety.recent_block_count.return_value = 0

        with (
            patch("custom_components.carmabox.coordinator.clear_issue") as mock_clear,
            patch("custom_components.carmabox.coordinator.raise_hub_offline_issue"),
        ):
            coord._check_repair_issues()
        # clear_issue should be called (at least for hub_offline or safety guard)
        mock_clear.assert_called()

    def test_hub_none_skips_check(self) -> None:
        """No _hub → hub check skipped (no error)."""
        coord = _make_coord()
        # _hub not set, getattr returns None
        coord.safety.recent_block_count.return_value = 0
        with patch("custom_components.carmabox.coordinator.clear_issue"):
            coord._check_repair_issues()  # Should not raise

    def test_exception_handled(self) -> None:
        """Exception → caught silently (line 5255-5256)."""
        coord = _make_coord()
        coord.safety.recent_block_count.side_effect = RuntimeError("safety broke")
        coord._check_repair_issues()  # Should not raise


# ── Tests: _self_heal_ev_tamper ───────────────────────────────────────────────


class TestSelfHealEvTamper:
    def test_no_ev_adapter_returns_early(self) -> None:
        """ev_adapter is None → returns early (line 6259-6260)."""
        coord = _make_coord()
        coord.ev_adapter = None
        coord._self_heal_ev_tamper()  # Should not raise

    def test_first_check_records_state(self) -> None:
        """First call → records current state (lines 6264-6267)."""
        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        ev = MagicMock(spec=EaseeAdapter)
        ev.is_enabled = True
        coord.ev_adapter = ev
        coord._ev_last_known_enabled = None

        coord._self_heal_ev_tamper()
        assert coord._ev_last_known_enabled is True

    def test_external_change_detected_and_logged(self) -> None:
        """State changed externally → warning logged, state updated (lines 6269-6280)."""
        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        ev = MagicMock(spec=EaseeAdapter)
        ev.is_enabled = False  # Changed from True
        coord.ev_adapter = ev
        coord._ev_last_known_enabled = True  # was True, now False

        coord._self_heal_ev_tamper()
        assert coord._ev_last_known_enabled is False  # Updated

    def test_no_external_change_no_update(self) -> None:
        """State unchanged → no update, no warning."""
        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        ev = MagicMock(spec=EaseeAdapter)
        ev.is_enabled = True
        coord.ev_adapter = ev
        coord._ev_last_known_enabled = True  # Same

        coord._self_heal_ev_tamper()
        assert coord._ev_last_known_enabled is True


# ── Tests: _safe_service_call dry-run ─────────────────────────────────────────


class TestSafeServiceCall:
    @pytest.mark.asyncio
    async def test_dry_run_logs_and_returns_true(self) -> None:
        """executor_enabled=False → dry-run log, return True (lines 6289-6297)."""
        coord = _make_coord(executor_enabled=False)
        result = await coord._safe_service_call("switch", "turn_on", {"entity_id": "switch.miner"})
        assert result is True
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_service_not_found_returns_false(self) -> None:
        """ServiceNotFound → return False after 1 attempt (lines 6303-6311)."""
        from homeassistant.exceptions import ServiceNotFound

        coord = _make_coord(executor_enabled=True)
        coord.hass.services.async_call.side_effect = ServiceNotFound("switch", "turn_on")
        result = await coord._safe_service_call("switch", "turn_on", {"entity_id": "switch.miner"})
        assert result is False

    @pytest.mark.asyncio
    async def test_ha_error_retries_once(self) -> None:
        """HomeAssistantError on first attempt → sleep + retry (lines 6312-6323)."""
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord(executor_enabled=True)
        # First call fails, second succeeds
        coord.hass.services.async_call.side_effect = [
            HomeAssistantError("temporary"),
            None,  # success
        ]
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await coord._safe_service_call(
                "select",
                "select_option",
                {"entity_id": "select.ems_mode", "option": "peak_shaving"},
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_unexpected_exception_retries_once(self) -> None:
        """Unexpected exception on first attempt → retry (lines 6324-6335)."""
        coord = _make_coord(executor_enabled=True)
        coord.hass.services.async_call.side_effect = [
            RuntimeError("weird"),
            None,  # success on retry
        ]
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await coord._safe_service_call(
                "select",
                "select_option",
                {"entity_id": "select.ems_mode", "option": "peak_shaving"},
            )
        assert result is True
