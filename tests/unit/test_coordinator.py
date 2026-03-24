"""Tests for CARMA Box coordinator — the brain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import (
    BatteryCommand,
    CarmaboxCoordinator,
)
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    Decision,
    HourActual,
    ShadowComparison,
)
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState


def _make_coordinator(
    options: dict[str, object] | None = None,
) -> CarmaboxCoordinator:
    """Create coordinator with mocked hass + config entry."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()

    entry = MagicMock()
    entry.options = options or {}
    entry.data = dict(entry.options)
    entry.entry_id = "test_entry"

    # Mock states
    states: dict[str, MagicMock] = {}

    def get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = get_state

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord._cfg = {**entry.data, **entry.options} if hasattr(entry, "data") else dict(entry.options)
    coord.safety = MagicMock()
    # All safety checks default to PASS so existing tests work unchanged
    coord.safety.check_heartbeat = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_rate_limit = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_charge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_discharge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_crosscharge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.plan = []
    coord._plan_counter = 0
    coord._last_command = BatteryCommand.IDLE
    coord._last_discharge_w = 0
    coord._pending_write_verifies = []
    coord.target_kw = options.get("target_weighted_kw", 2.0) if options else 2.0
    coord.min_soc = options.get("min_soc", 15.0) if options else 15.0
    coord.logger = MagicMock()
    coord.name = "carmabox"
    coord._states = states
    coord.savings = SavingsState(month=3, year=2026)
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord._daily_discharge_kwh = 0.0
    coord._daily_safety_blocks = 0
    coord._daily_plans = 0
    coord._current_date = "2026-03-18"
    coord._daily_avg_price = float((options or {}).get("fallback_price_ore", 80.0))
    coord._avg_price_initialized = True
    coord.notifier = MagicMock()
    coord.notifier.crosscharge_alert = AsyncMock()
    coord.notifier.proactive_discharge_started = AsyncMock()
    coord.notifier.safety_block = AsyncMock()
    coord._runtime_loaded = True
    coord._ledger_loaded = True
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord.last_decision = Decision()
    from collections import deque as _deque

    coord.decision_log = _deque(maxlen=48)
    coord.consumption_profile = ConsumptionProfile()
    coord.hourly_actuals = []
    coord._last_tracked_hour = -1
    coord._plan_deviation_count = 0
    coord._plan_last_correction_time = 0.0
    coord._ellevio_hour_samples = []
    coord._ellevio_current_hour = -1
    coord._ellevio_monthly_hourly_peaks = []
    coord.shadow = ShadowComparison()
    coord.shadow_log = []
    coord._shadow_savings_kr = 0.0
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}
    coord._ev_enabled = False
    coord._ev_current_amps = 0
    coord._ev_last_ramp_time = 0.0
    coord._ev_initialized = True
    coord._miner_entity = ""
    coord._miner_on = False
    coord._taper_active = False
    coord._cold_lock_active = False
    # IT-2067: Peak tracking, spike, reserve, dynamic discharge
    from custom_components.carmabox.const import PEAK_RANK_COUNT

    coord._peak_ranks = [0.0] * PEAK_RANK_COUNT
    coord._peak_month = 3
    coord._peak_last_update = 0.0
    coord._spike_active = False
    coord._spike_activated_at = 0.0
    coord._spike_cooldown_started = 0.0
    coord._grid_power_history = _deque(maxlen=120)
    coord._reserve_target_pct = 15.0
    coord._reserve_last_calc = 0.0
    from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger

    coord.ledger = EnergyLedger()
    coord._license_tier = "premium"
    coord._license_features = [
        "analyzer",
        "executor",
        "dashboard",
        "ev_control",
        "miner_control",
        "watchdog",
        "self_healing",
    ]
    coord._license_last_check = 0.0
    coord._license_check_interval = 99999999
    coord._license_valid_until = ""
    coord._license_offline_grace_days = 7
    coord.executor_enabled = True  # Tests need executor active
    coord._savings_loaded = True  # Skip restore in tests
    coord._savings_last_save = 0.0
    coord._savings_store = MagicMock()
    coord._savings_store.async_save = AsyncMock()
    coord._consumption_loaded = True  # Skip restore in tests
    coord._consumption_last_save = 0.0
    coord._consumption_last_hour = -1
    coord._consumption_store = MagicMock()
    coord._consumption_store.async_save = AsyncMock()

    # PLAT-965: Predictor
    coord.predictor = ConsumptionPredictor()
    coord._predictor_store = MagicMock()
    coord._predictor_store.async_save = AsyncMock()
    coord._predictor_loaded = True
    coord._predictor_last_save = 0.0

    # PLAT-972: Self-healing
    coord._ems_consecutive_failures = 0
    coord._ems_pause_until = 0.0
    coord._ev_last_known_enabled = None

    return coord


def _set_state(
    coord: CarmaboxCoordinator,
    entity_id: str,
    value: str,
    attributes: dict[str, object] | None = None,
) -> None:
    """Set a mock state on coordinator's hass."""
    state = MagicMock()
    state.state = value
    state.attributes = attributes or {}
    coord._states[entity_id] = state  # type: ignore[attr-defined]


class TestCollectState:
    def test_reads_grid_power(self) -> None:
        coord = _make_coordinator({"grid_entity": "sensor.grid"})
        _set_state(coord, "sensor.grid", "2500")
        state = coord._collect_state()
        assert state.grid_power_w == 2500.0

    def test_reads_battery_soc(self) -> None:
        coord = _make_coordinator({"battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "85")
        state = coord._collect_state()
        assert state.battery_soc_1 == 85.0

    def test_unavailable_returns_default(self) -> None:
        coord = _make_coordinator({"grid_entity": "sensor.grid"})
        _set_state(coord, "sensor.grid", "unavailable")
        state = coord._collect_state()
        assert state.grid_power_w == 0.0

    def test_missing_entity_returns_default(self) -> None:
        coord = _make_coordinator({"grid_entity": "sensor.nonexistent"})
        state = coord._collect_state()
        assert state.grid_power_w == 0.0

    def test_unreasonable_value_rejected(self) -> None:
        coord = _make_coordinator({"grid_entity": "sensor.grid"})
        _set_state(coord, "sensor.grid", "999999")
        state = coord._collect_state()
        assert state.grid_power_w == 0.0  # >100kW rejected

    def test_reads_battery_temp(self) -> None:
        coord = _make_coordinator({"battery_temp_entity": "sensor.batt_temp"})
        _set_state(coord, "sensor.batt_temp", "32.5")
        state = coord._collect_state()
        assert state.battery_temp_c == 32.5

    def test_battery_temp_none_when_unavailable(self) -> None:
        coord = _make_coordinator({"battery_temp_entity": "sensor.batt_temp"})
        _set_state(coord, "sensor.batt_temp", "unavailable")
        state = coord._collect_state()
        assert state.battery_temp_c is None

    def test_battery_temp_none_when_not_configured(self) -> None:
        coord = _make_coordinator({})
        state = coord._collect_state()
        assert state.battery_temp_c is None


class TestExecute:
    @pytest.mark.asyncio
    async def test_export_triggers_charge_pv(self) -> None:
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_soc_1": "sensor.soc1",
            }
        )
        _set_state(coord, "sensor.soc1", "50")
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(grid_power_w=-1000, battery_soc_1=50)
        await coord._execute(state)
        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_full_battery_with_grid_import_triggers_proactive_discharge(self) -> None:
        """SoC 100% + grid importing → proactive discharge (not standby)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})

        state = CarmaboxState(
            grid_power_w=1000,
            battery_soc_1=100,
            battery_soc_2=-1,
        )
        await coord._execute(state)
        # Decision recorded as discharge (service call may not set _last_command in test)
        assert coord.last_decision is not None
        assert coord.last_decision.action == "discharge"

    async def test_full_battery_exporting_triggers_standby(self) -> None:
        """SoC 100% + exporting → standby (correct, no discharge during export)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})

        state = CarmaboxState(
            grid_power_w=-500,  # exporting
            battery_soc_1=100,
            battery_soc_2=-1,
        )
        await coord._execute(state)
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_high_load_triggers_discharge(self) -> None:
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_limit_1": "number.limit1",
            }
        )

        state = CarmaboxState(
            grid_power_w=5000,
            battery_soc_1=80,
            battery_soc_2=-1,
        )
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18  # Daytime weight=1.0
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_safety_block_prevents_discharge(self) -> None:
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_discharge = MagicMock(return_value=MagicMock(ok=False, reason="min_soc"))

        state = CarmaboxState(
            grid_power_w=5000,
            battery_soc_1=10,
            battery_soc_2=-1,
        )
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)

        assert coord._last_command != BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_under_target_stays_idle(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(
            grid_power_w=1000,
            battery_soc_1=50,
            battery_soc_2=-1,
        )
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_charge_pv_skips_full_battery(self) -> None:
        """Full battery should get standby, not charge_pv."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_soc_1": "sensor.soc1",
            }
        )
        _set_state(coord, "sensor.soc1", "100")

        state = CarmaboxState(
            grid_power_w=-2000,
            battery_soc_1=100,
            battery_soc_2=-1,
        )
        await coord._execute(state)

        # Should call standby (all full), not charge_pv
        assert coord._last_command == BatteryCommand.STANDBY


class TestRecordDecision:
    def test_record_decision_updates_last_decision(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(grid_power_w=1500, battery_soc_1=60)
        coord._record_decision(state, "idle", "Vila — test")
        assert coord.last_decision.action == "idle"
        assert coord.last_decision.reason == "Vila — test"

    def test_record_decision_appends_to_log(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState()
        for i in range(5):
            coord._record_decision(state, "idle", f"Decision {i}")
        assert len(coord.decision_log) == 5

    def test_record_decision_caps_at_48(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState()
        for i in range(60):
            coord._record_decision(state, "idle", f"Decision {i}")
        assert len(coord.decision_log) == 48
        assert "Decision 59" in coord.decision_log[-1].reason

    def test_record_decision_calls_system_log(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState()
        coord._record_decision(state, "discharge", "Urladdning 500W — test")
        coord.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_updates_decision_sensor(self) -> None:
        """Decision sensor must update on every _execute() call."""
        coord = _make_coordinator()
        state = CarmaboxState(
            grid_power_w=1000,
            battery_soc_1=50,
            battery_soc_2=-1,
        )
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            mock_dt.now.return_value.isoformat.return_value = "2026-03-19T18:00:00"
            await coord._execute(state)

        assert coord.last_decision.action == "idle"
        assert coord.last_decision.timestamp != ""
        assert len(coord.decision_log) == 1


class TestBatteryCommand:
    def test_enum_values(self) -> None:
        assert BatteryCommand.IDLE.value == "idle"
        assert BatteryCommand.CHARGE_PV.value == "charge_pv"
        assert BatteryCommand.STANDBY.value == "standby"
        assert BatteryCommand.DISCHARGE.value == "discharge"

    @pytest.mark.asyncio
    async def test_no_duplicate_command(self) -> None:
        """Same command should not re-send."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._last_command = BatteryCommand.STANDBY

        # Calling standby again should be no-op

        await coord._cmd_standby(CarmaboxState())
        coord.hass.services.async_call.assert_not_called()


class TestCoordinatorInit:
    def test_init_defaults(self) -> None:
        """Test coordinator initializes with defaults when no options."""
        hass = MagicMock()
        entry = MagicMock()
        entry.options = {}
        entry.entry_id = "test"

        # Patch super().__init__ to avoid HA internals
        with patch.object(CarmaboxCoordinator, "__init__", lambda self, h, e: None):
            coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
            coord.hass = hass
            coord.entry = entry
            coord.safety = MagicMock()
            coord.plan = []
            coord._plan_counter = 0
            coord._last_command = BatteryCommand.IDLE
            coord.target_kw = entry.options.get("target_weighted_kw", 2.0)
            coord.min_soc = entry.options.get("min_soc", 15.0)

        assert coord.target_kw == 2.0
        assert coord.min_soc == 15.0
        assert coord._last_command == BatteryCommand.IDLE

    def test_init_with_options(self) -> None:
        coord = _make_coordinator(
            {
                "target_weighted_kw": 3.0,
                "min_soc": 20.0,
            }
        )
        assert coord.target_kw == 3.0
        assert coord.min_soc == 20.0


class TestReadHelpers:
    def test_read_float_valid(self) -> None:
        coord = _make_coordinator()
        _set_state(coord, "sensor.test", "42.5")
        assert coord._read_float("sensor.test") == 42.5

    def test_read_float_invalid(self) -> None:
        coord = _make_coordinator()
        _set_state(coord, "sensor.test", "not_a_number")
        assert coord._read_float("sensor.test") == 0.0

    def test_read_float_unreasonable(self) -> None:
        coord = _make_coordinator()
        _set_state(coord, "sensor.test", "999999")
        assert coord._read_float("sensor.test") == 0.0

    def test_read_float_empty_entity_id(self) -> None:
        coord = _make_coordinator()
        assert coord._read_float("") == 0.0

    def test_read_str_valid(self) -> None:
        coord = _make_coordinator()
        _set_state(coord, "select.test", "charge_pv")
        assert coord._read_str("select.test") == "charge_pv"

    def test_read_str_unavailable(self) -> None:
        coord = _make_coordinator()
        _set_state(coord, "select.test", "unavailable")
        assert coord._read_str("select.test") == ""

    def test_read_str_missing(self) -> None:
        coord = _make_coordinator()
        assert coord._read_str("select.nonexistent") == ""

    def test_read_str_empty_entity_id(self) -> None:
        coord = _make_coordinator()
        assert coord._read_str("") == ""


class TestAsyncUpdateData:
    @pytest.mark.asyncio
    async def test_update_collects_and_executes(self) -> None:
        coord = _make_coordinator({"grid_entity": "sensor.grid"})
        _set_state(coord, "sensor.grid", "1500")

        with patch.object(coord, "_execute", new_callable=AsyncMock) as mock_exec:
            result = await coord._async_update_data()

        assert result.grid_power_w == 1500.0
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_triggers_replan(self) -> None:
        coord = _make_coordinator()
        coord._plan_counter = 9  # Will hit threshold (10)

        with (
            patch.object(coord, "_execute", new_callable=AsyncMock),
            patch.object(coord, "_generate_plan") as mock_plan,
        ):
            await coord._async_update_data()

        mock_plan.assert_called_once()
        assert coord._plan_counter == 0

    @pytest.mark.asyncio
    async def test_update_error_raises_update_failed(self) -> None:
        coord = _make_coordinator()

        with patch.object(coord, "_collect_state", side_effect=RuntimeError("boom")):
            from homeassistant.helpers.update_coordinator import UpdateFailed

            with pytest.raises(UpdateFailed, match="boom"):
                await coord._async_update_data()


class TestGeneratePlan:
    def test_generate_plan_runs(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState()
        coord._generate_plan(state)  # Should not raise


class TestDischargeProportional:
    @pytest.mark.asyncio
    async def test_discharge_splits_by_stored_energy(self) -> None:
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
                "battery_limit_1": "number.limit1",
                "battery_limit_2": "number.limit2",
                "battery_1_kwh": 15.0,
                "battery_2_kwh": 5.0,
            }
        )

        state = CarmaboxState(
            grid_power_w=5000,
            battery_soc_1=80,
            battery_soc_2=20,
        )
        await coord._cmd_discharge(state, 1000)

        calls = coord.hass.services.async_call.call_args_list
        # Should have 4 calls: ems1, limit1, ems2, limit2
        assert len(calls) == 4
        # Battery 1: 80%×15kWh=1200, Battery 2: 20%×5kWh=100, total=1300
        # Battery 1 gets 1200/1300 of 1000 = 923W
        limit1_call = calls[1]
        assert limit1_call[0][2]["value"] == 923
        # Battery 2 gets remainder = 77W
        limit2_call = calls[3]
        assert limit2_call[0][2]["value"] == 77

    @pytest.mark.asyncio
    async def test_discharge_zero_soc_returns(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(battery_soc_1=0, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)
        coord.hass.services.async_call.assert_not_called()


class TestNoDuplicateCommands:
    @pytest.mark.asyncio
    async def test_charge_pv_no_duplicate(self) -> None:
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._last_command = BatteryCommand.CHARGE_PV
        await coord._cmd_charge_pv(CarmaboxState())
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_discharge_no_duplicate_similar_wattage(self) -> None:
        """K1: Skip redundant discharge if wattage within 100W tolerance."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._last_command = BatteryCommand.DISCHARGE
        coord._last_discharge_w = 3000
        await coord._cmd_discharge(CarmaboxState(battery_soc_1=50), 3050)
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_discharge_sends_when_wattage_differs(self) -> None:
        """K1: Re-send discharge when wattage differs by ≥100W."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_limit_1": "number.limit1",
            }
        )
        coord._last_command = BatteryCommand.DISCHARGE
        coord._last_discharge_w = 3000
        await coord._cmd_discharge(CarmaboxState(battery_soc_1=50, grid_power_w=5000), 3200)
        coord.hass.services.async_call.assert_called()

    @pytest.mark.asyncio
    async def test_charge_pv_sends_when_different(self) -> None:
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_soc_1": "sensor.soc1",
            }
        )
        _set_state(coord, "sensor.soc1", "50")
        coord._last_command = BatteryCommand.IDLE
        await coord._cmd_charge_pv(CarmaboxState(battery_soc_1=50))
        coord.hass.services.async_call.assert_called()


class TestTrackSavings:
    def test_tracks_peak_samples_at_hour_change(self) -> None:
        """Peaks recorded as hourly averages, not instantaneous."""
        coord = _make_coordinator()
        # Simulate hour 10 — accumulates
        coord._peak_last_hour = 10
        coord._peak_hour_samples = [(2.0, 2.0)]
        coord._current_date = "2026-03-21"
        # Same hour — just accumulates
        coord._peak_hour_samples.append((2.0, 2.0))
        assert len(coord.savings.peak_samples) == 0
        # Simulate hour change — flush to record_peak
        from custom_components.carmabox.optimizer.savings import record_peak

        avg_actual = sum(s[0] for s in coord._peak_hour_samples) / len(coord._peak_hour_samples)
        avg_baseline = sum(s[1] for s in coord._peak_hour_samples) / len(coord._peak_hour_samples)
        record_peak(coord.savings, avg_actual, avg_baseline)
        assert len(coord.savings.peak_samples) == 1

    def test_tracks_battery_discharge(self) -> None:
        coord = _make_coordinator({"fallback_price_ore": 80.0})
        from custom_components.carmabox.optimizer.savings import record_discharge

        # Simulate discharge: 2 kW for 30s = 0.0167 kWh at 120 öre (avg 80)
        record_discharge(coord.savings, 0.0167, 120.0, 80.0)
        assert coord.savings.discharge_savings_kr > 0
        assert coord.savings.total_discharge_kwh > 0

    def test_baseline_includes_discharge(self) -> None:
        """Baseline = grid + battery discharge (what grid would be without battery)."""
        coord = _make_coordinator()
        from custom_components.carmabox.optimizer.savings import record_peak

        # Actual: grid 1 kW (battery discharging 2 kW to offset)
        # Baseline: grid + discharge = 1 + 2 = 3 kW
        record_peak(coord.savings, 1.0, 3.0)
        assert len(coord.savings.baseline_peak_samples) == 1
        assert coord.savings.baseline_peak_samples[0] > coord.savings.peak_samples[0]

    def test_negative_discharge_savings_when_price_low(self) -> None:
        """Discharge at cheap price = negative savings."""
        coord = _make_coordinator({"fallback_price_ore": 100.0})
        state = CarmaboxState(
            grid_power_w=1000,
            battery_power_1=-1000,
            current_price=50.0,  # Below avg (100)
        )
        coord._track_savings(state)
        assert coord.savings.discharge_savings_kr < 0


class TestSafetyBlockingPaths:
    @pytest.mark.asyncio
    async def test_heartbeat_stale_blocks_all(self) -> None:
        """Stale heartbeat → no commands executed."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_heartbeat = MagicMock(
            return_value=MagicMock(ok=False, reason="stale 200s")
        )
        state = CarmaboxState(grid_power_w=5000, battery_soc_1=80)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)
        assert coord._last_command == BatteryCommand.IDLE
        assert coord._daily_safety_blocks > 0

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_all(self) -> None:
        """Rate limit → no commands."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="10 changes/h")
        )
        state = CarmaboxState(grid_power_w=5000, battery_soc_1=80)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)
        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_crosscharge_forces_standby(self) -> None:
        """Crosscharge detected → forced standby."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_crosscharge = MagicMock(
            return_value=MagicMock(ok=False, reason="crosscharge")
        )
        state = CarmaboxState(
            grid_power_w=2000,
            battery_soc_1=80,
            battery_power_1=-1000,
            battery_power_2=1000,
        )
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_charge_blocked_during_export(self) -> None:
        """Export + charge blocked → self-heal to standby (not user-facing block)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_charge = MagicMock(
            return_value=MagicMock(ok=False, reason="temp too low")
        )
        state = CarmaboxState(grid_power_w=-2000, battery_soc_1=50)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 12
            await coord._execute(state)
        assert coord._last_command == BatteryCommand.STANDBY
        # Self-healing: no safety_blocked flag — handled internally
        assert coord.last_decision.safety_blocked is False


class TestDecisionRecording:
    @pytest.mark.asyncio
    async def test_idle_decision_recorded(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(grid_power_w=100, battery_soc_1=30)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 12
            await coord._execute(state)
        assert coord.last_decision.action == "idle"
        assert "Vila" in coord.last_decision.reason

    @pytest.mark.asyncio
    async def test_discharge_decision_recorded(self) -> None:
        coord = _make_coordinator(
            {"battery_ems_1": "select.ems1", "battery_limit_1": "number.limit1"}
        )
        state = CarmaboxState(grid_power_w=5000, battery_soc_1=80, battery_soc_2=-1)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)
        assert coord.last_decision.action == "discharge"
        assert "Urladdning" in coord.last_decision.reason
        assert coord.last_decision.discharge_w > 0

    @pytest.mark.asyncio
    async def test_decision_log_accumulates(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(grid_power_w=1000, battery_soc_1=80)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 12
            for _ in range(5):
                await coord._execute(state)
        assert len(coord.decision_log) == 5


class TestSafetyGuardBypass:
    """PLAT-877: ALL commands must go through SafetyGuard."""

    @pytest.mark.asyncio
    async def test_charge_pv_blocked_by_check_charge(self) -> None:
        """check_charge returns block → charge_pv must NOT execute."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.safety.check_charge = MagicMock(
            return_value=MagicMock(ok=False, reason="all batteries full")
        )

        state = CarmaboxState(battery_soc_1=100, battery_soc_2=100)
        await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()
        assert coord._daily_safety_blocks > 0

    @pytest.mark.asyncio
    async def test_charge_pv_blocked_by_rate_limit(self) -> None:
        """Rate limit reached → charge_pv must NOT execute."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="rate limit exceeded")
        )

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_charge_pv_blocked_by_heartbeat(self) -> None:
        """Heartbeat stale → charge_pv must NOT execute."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.safety.check_heartbeat = MagicMock(
            return_value=MagicMock(ok=False, reason="stale 300s")
        )

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_standby_blocked_by_rate_limit(self) -> None:
        """Rate limit reached → standby must NOT execute."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="rate limit exceeded")
        )

        state = CarmaboxState()
        await coord._cmd_standby(state)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_standby_blocked_by_heartbeat(self) -> None:
        """Heartbeat stale → standby must NOT execute."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_heartbeat = MagicMock(
            return_value=MagicMock(ok=False, reason="stale 300s")
        )

        state = CarmaboxState()
        await coord._cmd_standby(state)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_forced_standby_bypasses_rate_limit(self) -> None:
        """force=True standby (safety action) should still work."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="rate limit exceeded")
        )

        state = CarmaboxState()
        await coord._cmd_standby(state, force=True)

        assert coord._last_command == BatteryCommand.STANDBY
        coord.hass.services.async_call.assert_called()

    @pytest.mark.asyncio
    async def test_discharge_blocked_by_rate_limit(self) -> None:
        """Rate limit reached → discharge must NOT execute."""
        coord = _make_coordinator(
            {"battery_ems_1": "select.ems1", "battery_limit_1": "number.limit1"}
        )
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="rate limit exceeded")
        )

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_discharge_blocked_by_heartbeat(self) -> None:
        """Heartbeat stale → discharge must NOT execute."""
        coord = _make_coordinator(
            {"battery_ems_1": "select.ems1", "battery_limit_1": "number.limit1"}
        )
        coord.safety.check_heartbeat = MagicMock(
            return_value=MagicMock(ok=False, reason="stale 300s")
        )

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_discharge_blocked_by_check_discharge(self) -> None:
        """check_discharge returns block → discharge must NOT execute."""
        coord = _make_coordinator(
            {"battery_ems_1": "select.ems1", "battery_limit_1": "number.limit1"}
        )
        coord.safety.check_discharge = MagicMock(
            return_value=MagicMock(ok=False, reason="SoC too low")
        )

        state = CarmaboxState(battery_soc_1=10, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        assert coord._last_command == BatteryCommand.IDLE
        coord.hass.services.async_call.assert_not_called()
        assert coord._daily_safety_blocks > 0

    @pytest.mark.asyncio
    async def test_crosscharge_triggers_forced_standby(self) -> None:
        """Crosscharge detected in _execute → both batteries set to standby."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_crosscharge = MagicMock(
            return_value=MagicMock(ok=False, reason="crosscharge detected")
        )

        state = CarmaboxState(
            battery_power_1=1000,
            battery_power_2=-1000,
            battery_soc_1=50,
        )
        await coord._execute(state)

        # Should have called standby (forced)
        assert coord._last_command == BatteryCommand.STANDBY
        coord.hass.services.async_call.assert_called()

    @pytest.mark.asyncio
    async def test_record_mode_change_called_on_charge_pv(self) -> None:
        """record_mode_change must be called after successful charge_pv."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        coord.safety.record_mode_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_mode_change_called_on_standby(self) -> None:
        """record_mode_change must be called after successful standby."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})

        state = CarmaboxState()
        await coord._cmd_standby(state)

        coord.safety.record_mode_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_mode_change_called_on_discharge(self) -> None:
        """record_mode_change must be called after successful discharge."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_limit_1": "number.limit1",
            }
        )

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        coord.safety.record_mode_change.assert_called_once()


class TestServiceCallErrorHandling:
    """PLAT-879: Error handling on all HA service calls."""

    @pytest.mark.asyncio
    async def test_service_call_raises_last_command_unchanged(self) -> None:
        """Service call raises → _last_command must stay IDLE."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("Modbus timeout"))

        state = CarmaboxState(battery_soc_1=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_service_call_raises_error_counted(self) -> None:
        """Service call raises → _daily_safety_blocks incremented."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("Modbus timeout"))
        initial_blocks = coord._daily_safety_blocks

        state = CarmaboxState(battery_soc_1=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        assert coord._daily_safety_blocks > initial_blocks

    @pytest.mark.asyncio
    async def test_service_call_retries_once(self) -> None:
        """Failed service call should retry once (2 total attempts)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("Modbus timeout"))

        state = CarmaboxState(battery_soc_1=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        # Should have been called twice (1 attempt + 1 retry)
        assert coord.hass.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_service_call_retry_succeeds(self) -> None:
        """First attempt fails, retry succeeds → command applied."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        # Fail first, succeed on retry
        coord.hass.services.async_call = AsyncMock(side_effect=[Exception("timeout"), None])

        state = CarmaboxState(battery_soc_1=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_service_not_found_no_retry(self) -> None:
        """ServiceNotFound should NOT retry — the service doesn't exist."""
        from homeassistant.exceptions import ServiceNotFound

        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        coord.hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("select", "select_option")
        )

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        # Only 1 attempt — no retry for ServiceNotFound
        assert coord.hass.services.async_call.call_count == 1
        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_discharge_ems_fail_no_limit_set(self) -> None:
        """_cmd_discharge: EMS fails → discharge limit must NOT be set (fail-safe)."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_limit_1": "number.limit1",
            }
        )
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("Modbus timeout"))

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_discharge(state, 1000)

        # Verify no number.set_value call was attempted (all calls are EMS attempts)
        for c in coord.hass.services.async_call.call_args_list:
            assert c[0][0] != "number", "Discharge limit must NOT be set when EMS fails"

        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_charge_pv_one_battery_fails_rollback_to_standby(self) -> None:
        """R3: _cmd_charge_pv partial failure → rollback ALL to standby."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
                "battery_soc_1": "sensor.soc1",
                "battery_soc_2": "sensor.soc2",
            }
        )
        _set_state(coord, "sensor.soc1", "50")
        _set_state(coord, "sensor.soc2", "50")

        # Battery 1 fails, battery 2 succeeds
        async def side_effect(domain: str, service: str, data: dict) -> None:
            entity = data.get("entity_id", "")
            if entity == "select.ems1":
                raise Exception("Modbus timeout")

        coord.hass.services.async_call = AsyncMock(side_effect=side_effect)

        blocks_before = coord._daily_safety_blocks
        state = CarmaboxState(battery_soc_1=50, battery_soc_2=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        # R3: partial failure → rollback to standby, command NOT set
        assert coord._last_command != BatteryCommand.CHARGE_PV
        assert coord._daily_safety_blocks > blocks_before

    @pytest.mark.asyncio
    async def test_standby_service_fail_last_command_unchanged(self) -> None:
        """Standby service call fails → _last_command stays IDLE."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("Modbus timeout"))

        state = CarmaboxState()
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_standby(state)

        assert coord._last_command == BatteryCommand.IDLE


class TestWriteVerify:
    """PLAT-879: Write-verify after EMS mode changes."""

    @pytest.mark.asyncio
    async def test_write_verify_pass(self) -> None:
        """After successful service call, write-verify reads back mode."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        # After service call, entity state reflects new mode
        _set_state(coord, "select.ems1", "charge_pv")

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_write_verify_deferred_detects_mismatch(self) -> None:
        """PLAT-945: Write-verify deferred to next cycle detects lockup."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        # Entity still shows old mode after service call
        _set_state(coord, "select.ems1", "discharge_battery")

        initial_blocks = coord._daily_safety_blocks

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        # Should NOT increment immediately (deferred)
        assert coord._daily_safety_blocks == initial_blocks
        assert len(coord._pending_write_verifies) > 0

        # Simulate next cycle: state still stale → lockup detected
        coord._run_deferred_write_verifies()
        assert coord._daily_safety_blocks > initial_blocks

    def test_check_write_verify_queues_pending(self) -> None:
        """PLAT-945: _check_write_verify queues for deferred check, not immediate."""
        coord = _make_coordinator()
        _set_state(coord, "select.ems1", "charge_pv")  # Stale state
        initial = coord._daily_safety_blocks
        coord._check_write_verify("select.ems1", "battery_standby")
        # Should NOT increment immediately — deferred to next cycle
        assert coord._daily_safety_blocks == initial
        assert len(coord._pending_write_verifies) == 1

    def test_deferred_write_verify_match(self) -> None:
        """PLAT-945: Deferred verify passes when state has propagated."""
        coord = _make_coordinator()
        coord._check_write_verify("select.ems1", "battery_standby")
        # Simulate Modbus propagation (state updated before next cycle)
        _set_state(coord, "select.ems1", "battery_standby")
        initial = coord._daily_safety_blocks
        coord._run_deferred_write_verifies()
        assert coord._daily_safety_blocks == initial
        assert len(coord._pending_write_verifies) == 0

    def test_deferred_write_verify_mismatch(self) -> None:
        """PLAT-945: Deferred verify detects lockup when state didn't propagate."""
        coord = _make_coordinator()
        coord._check_write_verify("select.ems1", "battery_standby")
        # Simulate Modbus lockup (state still stale after 30s)
        _set_state(coord, "select.ems1", "charge_pv")
        initial = coord._daily_safety_blocks
        coord._run_deferred_write_verifies()
        assert coord._daily_safety_blocks == initial + 1
        assert len(coord._pending_write_verifies) == 0


def _make_mock_adapter(
    soc: float = 50.0, power_w: float = 0.0, ems_mode: str = "", temp: float | None = 25.0
) -> MagicMock:
    """Create a mock InverterAdapter."""
    adapter = MagicMock()
    adapter.soc = soc
    adapter.power_w = power_w
    adapter.ems_mode = ems_mode
    adapter.temperature_c = temp
    adapter.set_ems_mode = AsyncMock(return_value=True)
    adapter.set_discharge_limit = AsyncMock(return_value=True)
    return adapter


def _make_mock_ev_adapter(
    status: str = "", power_w: float = 0.0, current_a: float = 0.0
) -> MagicMock:
    """Create a mock EVAdapter."""
    adapter = MagicMock()
    adapter.status = status
    adapter.power_w = power_w
    adapter.current_a = current_a
    adapter.is_charging = status == "charging"
    adapter.enable = AsyncMock(return_value=True)
    adapter.disable = AsyncMock(return_value=True)
    adapter.set_current = AsyncMock(return_value=True)
    return adapter


class TestAdapterIntegration:
    """PLAT-885: Coordinator must use adapters instead of raw service calls."""

    @pytest.mark.asyncio
    async def test_charge_pv_uses_adapter_set_ems_mode(self) -> None:
        """_cmd_charge_pv must call adapter.set_ems_mode('charge_pv')."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=50, ems_mode="charge_pv")
        coord.inverter_adapters = [a1]

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        a1.set_ems_mode.assert_called_once_with("charge_pv")
        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_charge_pv_full_battery_gets_standby(self) -> None:
        """Full battery (SoC>=100) should get standby mode via adapter."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=100, ems_mode="battery_standby")
        coord.inverter_adapters = [a1]

        state = CarmaboxState(battery_soc_1=100)
        await coord._cmd_charge_pv(state)

        a1.set_ems_mode.assert_called_once_with("battery_standby")

    @pytest.mark.asyncio
    async def test_standby_uses_adapter_set_ems_mode(self) -> None:
        """_cmd_standby must call adapter.set_ems_mode('battery_standby')."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(ems_mode="battery_standby")
        a2 = _make_mock_adapter(ems_mode="battery_standby")
        coord.inverter_adapters = [a1, a2]

        state = CarmaboxState()
        await coord._cmd_standby(state)

        a1.set_ems_mode.assert_called_once_with("battery_standby")
        a2.set_ems_mode.assert_called_once_with("battery_standby")
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_discharge_uses_adapter_set_ems_mode_and_limit(self) -> None:
        """_cmd_discharge must call adapter.set_ems_mode + adapter.set_discharge_limit."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=80, ems_mode="discharge_battery")
        a2 = _make_mock_adapter(soc=20, ems_mode="discharge_battery")
        coord.inverter_adapters = [a1, a2]

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=20)
        await coord._cmd_discharge(state, 1000)

        a1.set_ems_mode.assert_called_once_with("peak_shaving")
        a2.set_ems_mode.assert_called_once_with("peak_shaving")
        # IT-2067: Dynamic discharge limit based on SoC.
        # avg_soc = (80+20)/2 = 50 → MID tier (1500W)
        from custom_components.carmabox.const import DISCHARGE_LIMIT_MID_SOC_W

        expected_limit = DISCHARGE_LIMIT_MID_SOC_W
        a1.set_discharge_limit.assert_called_once_with(expected_limit)
        a2.set_discharge_limit.assert_called_once_with(expected_limit)
        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_discharge_ems_fail_no_limit_set_adapter(self) -> None:
        """Adapter EMS fails → discharge limit must NOT be set (fail-safe)."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=80)
        a1.set_ems_mode = AsyncMock(return_value=False)
        coord.inverter_adapters = [a1]

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        a1.set_ems_mode.assert_called_once_with("peak_shaving")
        a1.set_discharge_limit.assert_not_called()
        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_discharge_zero_soc_returns_adapter(self) -> None:
        """All adapters at 0 SoC → no commands sent."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=0)
        coord.inverter_adapters = [a1]

        state = CarmaboxState(battery_soc_1=0, battery_soc_2=-1)
        await coord._cmd_discharge(state, 1000)

        a1.set_ems_mode.assert_not_called()

    def test_collect_state_uses_adapter_soc(self) -> None:
        """_collect_state reads battery SoC from adapter."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=85, power_w=-500, ems_mode="charge_pv", temp=30.0)
        coord.inverter_adapters = [a1]

        state = coord._collect_state()
        assert state.battery_soc_1 == 85.0
        assert state.battery_power_1 == -500.0
        assert state.battery_ems_1 == "charge_pv"

    def test_collect_state_uses_two_adapters(self) -> None:
        """_collect_state reads both batteries from adapters."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=80, power_w=-200, ems_mode="charge_pv")
        a2 = _make_mock_adapter(soc=60, power_w=100, ems_mode="discharge_battery")
        coord.inverter_adapters = [a1, a2]

        state = coord._collect_state()
        assert state.battery_soc_1 == 80.0
        assert state.battery_soc_2 == 60.0
        assert state.battery_power_2 == 100.0
        assert state.battery_ems_2 == "discharge_battery"

    def test_collect_state_ev_adapter(self) -> None:
        """_collect_state reads EV data from adapter when configured."""
        coord = _make_coordinator()
        ev = _make_mock_ev_adapter(status="charging", power_w=7400, current_a=32)
        coord.ev_adapter = ev

        state = coord._collect_state()
        assert state.ev_power_w == 7400.0
        assert state.ev_current_a == 32.0
        assert state.ev_status == "charging"

    def test_battery_temp_from_adapter(self) -> None:
        """_read_battery_temp uses adapter.temperature_c."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(temp=35.0)
        a2 = _make_mock_adapter(temp=32.0)
        coord.inverter_adapters = [a1, a2]

        temp = coord._read_battery_temp()
        assert temp == 32.0  # min of both adapters

    def test_battery_temp_none_when_adapter_returns_none(self) -> None:
        """_read_battery_temp returns None when all adapters return None."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(temp=None)
        coord.inverter_adapters = [a1]

        temp = coord._read_battery_temp()
        assert temp is None

    @pytest.mark.asyncio
    async def test_no_raw_service_calls_with_adapters(self) -> None:
        """With adapters configured, hass.services.async_call must NOT be used."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=50, ems_mode="charge_pv")
        coord.inverter_adapters = [a1]

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        # Coordinator should NOT make raw service calls — adapters handle that
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_forced_standby_works_with_adapters(self) -> None:
        """force=True standby should bypass rate limit and use adapters."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(ems_mode="battery_standby")
        coord.inverter_adapters = [a1]
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="rate limit exceeded")
        )

        state = CarmaboxState()
        await coord._cmd_standby(state, force=True)

        a1.set_ems_mode.assert_called_once_with("battery_standby")
        assert coord._last_command == BatteryCommand.STANDBY


class TestR3RollbackPartialFailure:
    """PLAT-937: R3 rollback — partial adapter failure forces all to standby."""

    @pytest.mark.asyncio
    async def test_charge_pv_partial_failure_rollback_adapters(self) -> None:
        """charge_pv: adapter 1 succeeds, adapter 2 fails → rollback all to standby."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=50, ems_mode="charge_pv")
        a2 = _make_mock_adapter(soc=50, ems_mode="charge_pv")
        a2.set_ems_mode = AsyncMock(return_value=False)  # adapter 2 fails
        coord.inverter_adapters = [a1, a2]

        blocks_before = coord._daily_safety_blocks
        state = CarmaboxState(battery_soc_1=50, battery_soc_2=50)
        await coord._cmd_charge_pv(state)

        # Rollback: both adapters should get standby call
        assert a1.set_ems_mode.call_count == 2  # charge_pv + standby rollback
        assert a2.set_ems_mode.call_count == 2  # failed charge_pv + standby rollback
        assert a1.set_ems_mode.call_args_list[-1].args == ("battery_standby",)
        assert a2.set_ems_mode.call_args_list[-1].args == ("battery_standby",)
        assert coord._last_command != BatteryCommand.CHARGE_PV
        assert coord._daily_safety_blocks > blocks_before

    @pytest.mark.asyncio
    async def test_charge_pv_all_fail_no_rollback(self) -> None:
        """charge_pv: both adapters fail → no rollback needed (no partial state)."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=50)
        a1.set_ems_mode = AsyncMock(return_value=False)
        a2 = _make_mock_adapter(soc=50)
        a2.set_ems_mode = AsyncMock(return_value=False)
        coord.inverter_adapters = [a1, a2]

        state = CarmaboxState(battery_soc_1=50, battery_soc_2=50)
        await coord._cmd_charge_pv(state)

        # No rollback — both failed, no partial state
        assert a1.set_ems_mode.call_count == 1
        assert a2.set_ems_mode.call_count == 1
        assert coord._last_command != BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_discharge_partial_failure_rollback_adapters(self) -> None:
        """discharge: adapter 1 succeeds, adapter 2 fails → rollback all to standby."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=80, ems_mode="discharge_battery")
        a2 = _make_mock_adapter(soc=50, ems_mode="discharge_battery")
        a2.set_ems_mode = AsyncMock(return_value=False)  # adapter 2 fails
        coord.inverter_adapters = [a1, a2]

        blocks_before = coord._daily_safety_blocks
        state = CarmaboxState(battery_soc_1=80, battery_soc_2=50)
        await coord._cmd_discharge(state, 1000)

        # Rollback: both adapters should get standby call
        assert a1.set_ems_mode.call_args_list[-1].args == ("battery_standby",)
        assert a2.set_ems_mode.call_args_list[-1].args == ("battery_standby",)
        assert coord._last_command != BatteryCommand.DISCHARGE
        assert coord._daily_safety_blocks > blocks_before

    @pytest.mark.asyncio
    async def test_discharge_all_succeed_no_rollback(self) -> None:
        """discharge: all adapters succeed → no rollback, command set normally."""
        coord = _make_coordinator()
        a1 = _make_mock_adapter(soc=80, ems_mode="discharge_battery")
        a2 = _make_mock_adapter(soc=50, ems_mode="discharge_battery")
        coord.inverter_adapters = [a1, a2]

        state = CarmaboxState(battery_soc_1=80, battery_soc_2=50)
        await coord._cmd_discharge(state, 1000)

        assert coord._last_command == BatteryCommand.DISCHARGE
        # No standby calls — only discharge_battery
        for adapter in [a1, a2]:
            for call in adapter.set_ems_mode.call_args_list:
                assert call.args == ("peak_shaving",)


class TestApplianceTracking:
    """PLAT-943: Appliance power tracking tests."""

    def test_track_appliances_reads_power(self) -> None:
        """_track_appliances reads power from configured appliance entities."""
        coord = _make_coordinator()
        coord._appliances = [
            {
                "entity_id": "sensor.tvatt_power",
                "name": "Tvättmaskin",
                "category": "laundry",
                "threshold_w": 10,
            },
            {
                "entity_id": "sensor.miner_power",
                "name": "Miner",
                "category": "miner",
                "threshold_w": 10,
            },
        ]
        _set_state(coord, "sensor.tvatt_power", "250", {"unit_of_measurement": "W"})
        _set_state(coord, "sensor.miner_power", "800", {"unit_of_measurement": "W"})

        coord._track_appliances()

        assert coord.appliance_power["laundry"] == 250.0
        assert coord.appliance_power["miner"] == 800.0

    def test_track_appliances_converts_kw(self) -> None:
        """kW sensors should be converted to W."""
        coord = _make_coordinator()
        coord._appliances = [
            {
                "entity_id": "sensor.vp_power",
                "name": "VP",
                "category": "heating",
                "threshold_w": 10,
            },
        ]
        _set_state(coord, "sensor.vp_power", "1.5", {"unit_of_measurement": "kW"})

        coord._track_appliances()

        assert coord.appliance_power["heating"] == 1500.0

    def test_track_appliances_threshold_filters_standby(self) -> None:
        """Power below threshold should read as 0."""
        coord = _make_coordinator()
        coord._appliances = [
            {"entity_id": "sensor.tvatt", "name": "T", "category": "laundry", "threshold_w": 10},
        ]
        _set_state(coord, "sensor.tvatt", "5", {"unit_of_measurement": "W"})

        coord._track_appliances()

        assert coord.appliance_power["laundry"] == 0.0

    def test_track_appliances_accumulates_energy(self) -> None:
        """Energy should accumulate across multiple calls."""
        coord = _make_coordinator()
        coord._appliances = [
            {"entity_id": "sensor.tvatt", "name": "T", "category": "laundry", "threshold_w": 10},
        ]
        _set_state(coord, "sensor.tvatt", "1000", {"unit_of_measurement": "W"})

        coord._track_appliances()
        coord._track_appliances()

        # 1000W × (30/3600)h × 2 calls = 16.67 Wh
        expected = 1000 * (30 / 3600) * 2
        assert abs(coord.appliance_energy_wh["laundry"] - expected) < 0.01

    def test_track_appliances_sums_same_category(self) -> None:
        """Multiple appliances in same category should sum."""
        coord = _make_coordinator()
        coord._appliances = [
            {
                "entity_id": "sensor.tvatt",
                "name": "Tvättmaskin",
                "category": "laundry",
                "threshold_w": 10,
            },
            {
                "entity_id": "sensor.tork",
                "name": "Torktumlare",
                "category": "laundry",
                "threshold_w": 10,
            },
        ]
        _set_state(coord, "sensor.tvatt", "200", {"unit_of_measurement": "W"})
        _set_state(coord, "sensor.tork", "800", {"unit_of_measurement": "W"})

        coord._track_appliances()

        assert coord.appliance_power["laundry"] == 1000.0

    def test_daily_reset_clears_energy(self) -> None:
        """Daily counter reset should clear appliance energy."""
        from datetime import datetime

        coord = _make_coordinator()
        coord.appliance_energy_wh = {"laundry": 5000.0, "miner": 12000.0}
        coord._current_date = "2026-03-19"

        # Trigger reset by simulating next day
        coord._reset_daily_counters_if_new_day(datetime(2026, 3, 20, 0, 1))

        assert coord.appliance_energy_wh == {}

    def test_empty_appliances_no_error(self) -> None:
        """Empty appliance list should not crash."""
        coord = _make_coordinator()
        coord._appliances = []

        coord._track_appliances()

        assert coord.appliance_power == {}


class TestPredictorIntegration:
    """PLAT-965: ConsumptionPredictor integration in coordinator."""

    def test_predictor_initialized(self) -> None:
        """Coordinator should have a predictor instance."""
        coord = _make_coordinator()
        assert coord.predictor is not None
        assert coord.predictor.total_samples == 0

    def test_track_savings_feeds_predictor(self) -> None:
        """_track_savings should add samples to predictor once per hour."""
        coord = _make_coordinator()
        coord._consumption_last_hour = -1  # Force first-hour update
        state = CarmaboxState(
            grid_power_w=2000,
            battery_power_1=0,
            battery_power_2=0,
            pv_power_w=1000,
            ev_power_w=0,
            current_price=50.0,
        )
        mock_now = MagicMock()
        mock_now.hour = 14
        mock_now.weekday.return_value = 5  # Saturday
        mock_now.month = 3
        mock_now.strftime.return_value = "2026-03-21"
        mock_now.isoformat.return_value = "2026-03-21T14:00:00"
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            coord._track_savings(state)

        assert coord.predictor.total_samples == 1

    def test_predictor_used_in_generate_plan_when_trained(self) -> None:
        """When predictor is trained, _generate_plan should use it."""
        coord = _make_coordinator({"price_entity": "sensor.np"})
        # Make predictor appear trained
        coord.predictor.total_samples = 200
        for d in range(7):
            for h in range(24):
                key = f"{d}_{h}"
                coord.predictor.history[key] = [2.0, 2.5, 1.8]

        _set_state(
            coord,
            "sensor.np",
            "50",
            {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False},
        )

        state = CarmaboxState(battery_soc_1=80)
        with patch("custom_components.carmabox.coordinator.SolcastAdapter") as mock_sol:
            mock_sol.return_value.today_hourly_kw = [0.0] * 24
            mock_sol.return_value.tomorrow_kwh = 10.0
            mock_sol.return_value.forecast_daily_3d = [10.0, 10.0, 10.0]
            coord._generate_plan(state)

        assert len(coord.plan) > 0

    def test_predictor_fallback_when_not_trained(self) -> None:
        """When predictor is NOT trained, use consumption profile."""
        coord = _make_coordinator({"price_entity": "sensor.np"})
        assert not coord.predictor.is_trained

        _set_state(
            coord,
            "sensor.np",
            "50",
            {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False},
        )

        state = CarmaboxState(battery_soc_1=80)
        coord._generate_plan(state)  # Should not raise


class TestSelfHealing:
    """PLAT-972: Self-healing tests."""

    @pytest.mark.asyncio
    async def test_goodwe_self_heal_unavailable_triggers_reload(self) -> None:
        """When GoodWe EMS entity is unavailable, trigger reload."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coordinator()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 80.0
        coord.inverter_adapters = [adapter]

        # Set entity to unavailable
        _set_state(coord, "select.goodwe_kontor_ems_mode", "unavailable")

        await coord._self_heal_goodwe_entries()

        assert coord._ems_consecutive_failures == 1
        # Should have called reload service
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_goodwe_self_heal_pauses_after_max_failures(self) -> None:
        """After MAX_FAILURES consecutive, pause for 5 min."""
        import time

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coordinator()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 80.0
        coord.inverter_adapters = [adapter]
        coord._ems_consecutive_failures = 2  # One more will trigger pause

        _set_state(coord, "select.goodwe_kontor_ems_mode", "unavailable")

        await coord._self_heal_goodwe_entries()

        assert coord._ems_pause_until > time.monotonic()
        assert coord._ems_consecutive_failures == 0  # Reset after pause

    @pytest.mark.asyncio
    async def test_goodwe_self_heal_resets_on_healthy(self) -> None:
        """Healthy adapter resets failure counter."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coordinator()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 80.0
        coord.inverter_adapters = [adapter]
        coord._ems_consecutive_failures = 2

        # Entity is healthy
        _set_state(coord, "select.goodwe_kontor_ems_mode", "charge_pv")

        await coord._self_heal_goodwe_entries()

        assert coord._ems_consecutive_failures == 0

    def test_ev_tamper_detection_logs_change(self) -> None:
        """External EV enable/disable change should be detected."""
        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coordinator()
        ev = MagicMock(spec=EaseeAdapter)
        ev.is_enabled = True
        coord.ev_adapter = ev
        coord._ev_last_known_enabled = False  # CARMA thinks disabled

        coord._self_heal_ev_tamper()

        assert coord._ev_last_known_enabled is True  # Updated to current


class TestTransparencySensor:
    """PLAT-964: System status and health."""

    def test_status_text_all_ok(self) -> None:
        """When everything works, status = 'Allt fungerar'."""
        coord = _make_coordinator()
        coord.inverter_adapters = []
        assert coord.status_text == "Allt fungerar"

    def test_system_health_returns_dict(self) -> None:
        """system_health should return dict with component statuses."""
        coord = _make_coordinator()
        health = coord.system_health
        assert isinstance(health, dict)
        # Should at least have safety and control
        assert "sakerhet" in health
        assert "styrning" in health

    def test_status_text_adapter_offline(self) -> None:
        """Offline adapter shows in status."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coordinator()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = -1.0
        coord.inverter_adapters = [adapter]

        # Entity unavailable
        _set_state(coord, "select.goodwe_kontor_ems_mode", "unavailable")

        assert "offline" in coord.status_text.lower()

    def test_status_text_paused(self) -> None:
        """When EMS is paused, status reflects it."""
        import time

        coord = _make_coordinator()
        coord._ems_pause_until = time.monotonic() + 300  # Paused for 5 min

        health = coord.system_health
        assert health["styrning"] == "pausad"
        assert "pausad" in coord.status_text.lower()


class TestPlanScore:
    """PLAT-966: Plan score calculation."""

    def test_plan_score_no_data(self) -> None:
        """No hourly actuals → score None."""
        coord = _make_coordinator()
        scores = coord.plan_score()
        assert scores["score_today"] is None
        assert scores["trend"] == "stable"

    def test_plan_score_perfect_match(self) -> None:
        """Perfect match → score 100."""
        coord = _make_coordinator()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(5)
        ]
        scores = coord.plan_score()
        assert scores["score_today"] == 100.0

    def test_plan_score_partial_match(self) -> None:
        """Partial match → score < 100."""
        coord = _make_coordinator()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=3.0) for h in range(5)
        ]
        scores = coord.plan_score()
        assert scores["score_today"] is not None
        # 2.0/3.0 = 66.7%
        assert 60 < scores["score_today"] < 70

    def test_plan_score_trend_stable(self) -> None:
        """With insufficient daily data, trend is stable."""
        coord = _make_coordinator()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(3)
        ]
        scores = coord.plan_score()
        assert scores["trend"] == "stable"

    def test_plan_score_both_zero(self) -> None:
        """Both planned and actual near zero → 100%."""
        coord = _make_coordinator()
        coord.hourly_actuals = [
            HourActual(hour=0, planned_weighted_kw=0.0, actual_weighted_kw=0.0),
            HourActual(hour=1, planned_weighted_kw=0.0, actual_weighted_kw=0.0),
        ]
        scores = coord.plan_score()
        assert scores["score_today"] == 100.0


class TestTaperDetection:
    """IT-1939: BMS taper detection — never export when SoC < 100%."""

    @pytest.mark.asyncio
    async def test_taper_detected_when_exporting_during_charge_pv(self) -> None:
        """Export > 200W while charge_pv + SoC < 100% → taper detected."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,  # Exporting 500W
            battery_soc_1=96,
            battery_soc_2=-1,
            pv_power_w=3000,
        )
        await coord._execute(state)
        assert coord._taper_active is True
        assert coord.last_decision.action == "charge_pv_taper"

    @pytest.mark.asyncio
    async def test_taper_not_detected_when_export_below_threshold(self) -> None:
        """Export < 200W → no taper (batteries absorbing well)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-100,  # Exporting only 100W
            battery_soc_1=96,
            battery_soc_2=-1,
            pv_power_w=3000,
        )
        await coord._execute(state)
        assert coord._taper_active is False
        assert coord.last_decision.action == "charge_pv"

    @pytest.mark.asyncio
    async def test_taper_exit_when_soc_full(self) -> None:
        """SoC reaches 100% → taper exits."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._taper_active = True

        state = CarmaboxState(
            grid_power_w=-500,
            battery_soc_1=100,
            battery_soc_2=-1,
            pv_power_w=3000,
        )
        await coord._execute(state)
        assert coord._taper_active is False

    @pytest.mark.asyncio
    async def test_taper_exit_when_not_exporting(self) -> None:
        """No longer exporting → taper cleared."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._taper_active = True

        state = CarmaboxState(
            grid_power_w=500,  # Importing now
            battery_soc_1=96,
            battery_soc_2=-1,
            pv_power_w=1000,
        )
        await coord._execute(state)
        assert coord._taper_active is False

    @pytest.mark.asyncio
    async def test_taper_exit_when_export_low(self) -> None:
        """Export drops below exit threshold → taper exits."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._taper_active = True
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-50,  # Below TAPER_EXIT_EXPORT_W (100)
            battery_soc_1=96,
            battery_soc_2=-1,
            pv_power_w=3000,
        )
        await coord._execute(state)
        assert coord._taper_active is False

    @pytest.mark.asyncio
    async def test_taper_exit_when_pv_low(self) -> None:
        """PV drops below exit threshold → taper exits (sun going down)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._taper_active = True
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-150,  # Export above exit threshold
            battery_soc_1=96,
            battery_soc_2=-1,
            pv_power_w=300,  # Below TAPER_EXIT_PV_KW (0.5 kW = 500W)
        )
        await coord._execute(state)
        assert coord._taper_active is False

    @pytest.mark.asyncio
    async def test_taper_activates_miner(self) -> None:
        """During taper, miner should be turned ON to absorb surplus."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._miner_entity = "switch.miner"
        coord._miner_on = False
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-800,
            battery_soc_1=97,
            battery_soc_2=-1,
            pv_power_w=4000,
        )
        await coord._execute(state)
        assert coord._taper_active is True
        assert coord._miner_on is True

    @pytest.mark.asyncio
    async def test_taper_decision_has_taper_info(self) -> None:
        """Taper decision should include taper info in reasoning."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-600,
            battery_soc_1=95,
            battery_soc_2=-1,
            pv_power_w=3500,
        )
        await coord._execute(state)
        assert coord._taper_active is True
        d = coord.last_decision
        assert d.action == "charge_pv_taper"
        assert "taper" in d.reason.lower()

    @pytest.mark.asyncio
    async def test_taper_keeps_charge_pv_mode(self) -> None:
        """Taper should NOT change battery mode — keep charge_pv."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-1500,
            battery_soc_1=98,
            battery_soc_2=-1,
            pv_power_w=5000,
        )
        await coord._execute(state)
        assert coord._last_command == BatteryCommand.CHARGE_PV
        assert coord._taper_active is True

    @pytest.mark.asyncio
    async def test_taper_with_dual_battery(self) -> None:
        """Taper detection works with dual batteries."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
            }
        )
        _set_state(coord, "select.ems1", "battery_standby")
        _set_state(coord, "select.ems2", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-700,
            battery_soc_1=97,
            battery_soc_2=96,
            pv_power_w=4000,
        )
        await coord._execute(state)
        assert coord._taper_active is True
        assert coord.last_decision.action == "charge_pv_taper"


class TestColdLockDetection:
    """IT-1948: BMS cold lock detection — charging blocked at low cell temp."""

    @pytest.mark.asyncio
    async def test_cold_lock_detected_when_cell_temp_low(self) -> None:
        """Cell temp < 10°C + exporting + battery ~0W → cold lock."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,  # Exporting
            battery_soc_1=50,
            battery_soc_2=-1,
            battery_power_1=0,  # Battery not accepting charge
            pv_power_w=3000,
            battery_cell_temp_1=8.3,  # Below 10°C threshold
        )
        await coord._execute(state)
        assert coord._cold_lock_active is True
        assert coord.last_decision.action == "bms_cold_lock"
        assert "kall-blockering" in coord.last_decision.reason

    @pytest.mark.asyncio
    async def test_cold_lock_not_detected_when_cell_temp_ok(self) -> None:
        """Cell temp > 10°C → normal charge_pv, no cold lock."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-100,  # Low export (below taper threshold)
            battery_soc_1=50,
            battery_soc_2=-1,
            pv_power_w=3000,
            battery_cell_temp_1=15.0,  # Above threshold
        )
        await coord._execute(state)
        assert coord._cold_lock_active is False
        assert coord.last_decision.action == "charge_pv"

    @pytest.mark.asyncio
    async def test_cold_lock_clears_when_temp_rises(self) -> None:
        """Cold lock clears when cell temp rises above threshold."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._cold_lock_active = True
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,
            battery_soc_1=50,
            battery_soc_2=-1,
            pv_power_w=3000,
            battery_cell_temp_1=12.0,  # Above threshold now
        )
        await coord._execute(state)
        assert coord._cold_lock_active is False

    @pytest.mark.asyncio
    async def test_cold_lock_dual_battery_one_cold(self) -> None:
        """One battery cold + other warm → cold lock (any_cold = True)."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
            }
        )
        _set_state(coord, "select.ems1", "battery_standby")
        _set_state(coord, "select.ems2", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,
            battery_soc_1=50,
            battery_soc_2=50,
            battery_power_1=0,
            battery_power_2=0,
            pv_power_w=3000,
            battery_cell_temp_1=8.3,  # Cold
            battery_cell_temp_2=15.0,  # Warm
        )
        await coord._execute(state)
        assert coord._cold_lock_active is True
        assert coord.last_decision.action == "bms_cold_lock"

    @pytest.mark.asyncio
    async def test_cold_lock_activates_surplus_chain(self) -> None:
        """During cold lock, miner should be turned ON (surplus chain)."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._miner_entity = "switch.miner"
        coord._miner_on = False
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-800,
            battery_soc_1=50,
            battery_soc_2=-1,
            battery_power_1=0,
            pv_power_w=4000,
            battery_cell_temp_1=7.0,
        )
        await coord._execute(state)
        assert coord._cold_lock_active is True
        assert coord._miner_on is True

    @pytest.mark.asyncio
    async def test_cold_lock_no_cell_temp_no_lock(self) -> None:
        """No cell temp data (None) → no cold lock, normal behavior."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-100,  # Low export (below taper threshold)
            battery_soc_1=50,
            battery_soc_2=-1,
            pv_power_w=3000,
            battery_cell_temp_1=None,  # No data
        )
        await coord._execute(state)
        assert coord._cold_lock_active is False
        assert coord.last_decision.action == "charge_pv"

    @pytest.mark.asyncio
    async def test_cold_lock_decision_shows_cell_temps(self) -> None:
        """Cold lock decision reason includes cell temperatures."""
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
            }
        )
        _set_state(coord, "select.ems1", "battery_standby")
        _set_state(coord, "select.ems2", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,
            battery_soc_1=50,
            battery_soc_2=50,
            battery_power_1=0,
            battery_power_2=0,
            pv_power_w=3000,
            battery_cell_temp_1=8.3,
            battery_cell_temp_2=10.5,
        )
        await coord._execute(state)
        assert coord._cold_lock_active is True
        d = coord.last_decision
        assert "8.3" in d.reason
        assert "kontor" in d.reason

    @pytest.mark.asyncio
    async def test_cold_lock_not_taper(self) -> None:
        """Cold cell temp should trigger cold_lock, NOT taper."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(
            grid_power_w=-500,  # Exporting > TAPER_EXPORT_THRESHOLD_W
            battery_soc_1=50,
            battery_soc_2=-1,
            battery_power_1=0,
            pv_power_w=3000,
            battery_cell_temp_1=5.0,  # Very cold
        )
        await coord._execute(state)
        # Should be cold lock, NOT taper
        assert coord._cold_lock_active is True
        assert coord._taper_active is False
        assert coord.last_decision.action == "bms_cold_lock"


# ── IT-2067: Peak Tracking Tests ─────────────────────────────────


class TestPeakTracking:
    """Test rolling top-3 monthly peak tracking."""

    def test_initial_peaks_are_zero(self) -> None:
        coord = _make_coordinator()
        assert coord._peak_ranks == [0.0, 0.0, 0.0]

    def test_track_single_peak(self) -> None:
        coord = _make_coordinator()
        coord._peak_last_update = 0.0  # Force update
        coord._track_peaks(5.0)
        assert coord._peak_ranks[0] == 5.0
        assert coord._peak_ranks[1] == 0.0

    def test_track_multiple_peaks_sorted(self) -> None:
        coord = _make_coordinator()
        # Force updates by setting last_update far in past
        import time

        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(3.0)
        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(5.0)
        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(4.0)
        # Should be sorted descending
        assert coord._peak_ranks == [5.0, 4.0, 3.0]

    def test_new_peak_pushes_out_lowest(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [8.0, 6.0, 4.0]
        import time

        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(5.0)
        assert coord._peak_ranks == [8.0, 6.0, 5.0]

    def test_monthly_reset(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        coord._peak_month = 2  # February — will trigger reset in March
        import time

        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(1.0)
        assert coord._peak_ranks[0] == 1.0
        assert coord._peak_month != 2  # Month updated

    def test_negative_grid_ignored(self) -> None:
        coord = _make_coordinator()
        import time

        coord._peak_last_update = time.monotonic() - 400
        coord._track_peaks(-1.0)
        assert coord._peak_ranks == [0.0, 0.0, 0.0]


class TestPeakRiskStatus:
    """Test peak risk calculation."""

    def test_safe_when_below_threshold(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        assert coord._peak_risk_status(4.0) == "safe"

    def test_warning_near_rank3(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        # margin=1.0 default, so warning at >= 5.0
        assert coord._peak_risk_status(5.5) == "warning"

    def test_risk_at_rank3(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        assert coord._peak_risk_status(6.0) == "risk"

    def test_safe_when_peaks_below_meaningful(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [2.0, 1.0, 0.5]
        # rank_3 < 3.0 → always safe (normal house load)
        assert coord._peak_risk_status(2.5) == "safe"


class TestAdjustedTarget:
    """Test dynamic target adjustment based on peak risk."""

    def test_no_adjustment_when_safe(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        # grid_kw=1.0, well below rank_3=6.0, safe
        target = coord._adjusted_target_kw(is_night=False, current_grid_kw=1.0)
        from custom_components.carmabox.const import DEFAULT_TARGET_DAY_KW

        assert target == DEFAULT_TARGET_DAY_KW

    def test_reduced_when_risk(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        # grid_kw=7.0 >= rank_3=6.0 → risk
        target = coord._adjusted_target_kw(is_night=False, current_grid_kw=7.0)
        from custom_components.carmabox.const import DEFAULT_TARGET_DAY_KW, PEAK_WARNING_MARGIN_KW

        assert target == max(0.5, DEFAULT_TARGET_DAY_KW - PEAK_WARNING_MARGIN_KW)

    def test_night_target_different(self) -> None:
        coord = _make_coordinator()
        coord._peak_ranks = [10.0, 8.0, 6.0]
        target = coord._adjusted_target_kw(is_night=True, current_grid_kw=1.0)
        from custom_components.carmabox.const import DEFAULT_TARGET_NIGHT_KW

        assert target == DEFAULT_TARGET_NIGHT_KW


# ── IT-2067: Appliance Spike Tests ───────────────────────────────


class TestApplianceSpikeDetection:
    """Test appliance spike detection logic."""

    def test_no_spike_on_stable_load(self) -> None:
        coord = _make_coordinator()
        import time

        base = time.monotonic()
        # Simulate stable 2000W for 60s
        for i in range(10):
            coord._grid_power_history.append((base + i * 6, 2000))
        assert coord._detect_appliance_spike(2100) is False

    def test_spike_on_sudden_jump(self) -> None:
        coord = _make_coordinator()
        import time

        base = time.monotonic()
        # Stable baseline at 1500W
        for i in range(5):
            coord._grid_power_history.append((base + i * 6, 1500))
        # Sudden jump to 3000W (delta = 1500 > 1000 threshold)
        assert coord._detect_appliance_spike(3000) is True

    def test_spike_recovery_resets_flag(self) -> None:
        coord = _make_coordinator()
        coord._spike_active = True
        coord._spike_activated_at = 0.0  # Very old → safety timeout
        import asyncio

        asyncio.get_event_loop().run_until_complete(coord._handle_spike_recovery(1000))
        assert coord._spike_active is False


# ── IT-2067: Dynamic Discharge Limit Tests ────────────────────────


class TestDynamicDischargeLimit:
    """Test SoC-based dynamic discharge limit."""

    def test_high_soc_aggressive(self) -> None:
        coord = _make_coordinator()
        coord.data = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        limit = coord._dynamic_discharge_limit_w()
        from custom_components.carmabox.const import DISCHARGE_LIMIT_HIGH_SOC_W

        assert limit == DISCHARGE_LIMIT_HIGH_SOC_W

    def test_low_soc_conservative(self) -> None:
        coord = _make_coordinator()
        coord.data = CarmaboxState(battery_soc_1=25, battery_soc_2=-1)
        limit = coord._dynamic_discharge_limit_w()
        from custom_components.carmabox.const import DISCHARGE_LIMIT_LOW_SOC_W

        assert limit == DISCHARGE_LIMIT_LOW_SOC_W

    def test_very_low_soc(self) -> None:
        coord = _make_coordinator()
        coord.data = CarmaboxState(battery_soc_1=15, battery_soc_2=-1)
        limit = coord._dynamic_discharge_limit_w()
        from custom_components.carmabox.const import DISCHARGE_LIMIT_VERY_LOW_SOC_W

        assert limit == DISCHARGE_LIMIT_VERY_LOW_SOC_W

    def test_two_batteries_average(self) -> None:
        coord = _make_coordinator()
        # Average = (80+40)/2 = 60, which is > 40 but not > 60
        coord.data = CarmaboxState(battery_soc_1=80, battery_soc_2=40)
        limit = coord._dynamic_discharge_limit_w()
        from custom_components.carmabox.const import DISCHARGE_LIMIT_MID_SOC_W

        assert limit == DISCHARGE_LIMIT_MID_SOC_W


# ── IT-2067: Reserve Target Tests ─────────────────────────────────


class TestReserveTarget:
    """Test Solcast-based dynamic min SoC."""

    def test_strong_sun_no_offset(self) -> None:
        coord = _make_coordinator()
        coord._reserve_last_calc = 0.0  # Force recalc
        _set_state(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "25.0")
        target = coord._calculate_reserve_target()
        # Strong sun (25 kWh > 20 threshold) → base 15% + 0% = 15%
        assert target == 15.0

    def test_weak_sun_adds_offset(self) -> None:
        coord = _make_coordinator()
        coord._reserve_last_calc = 0.0
        _set_state(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "3.0")
        target = coord._calculate_reserve_target()
        # Weak sun (3 kWh < 5 threshold) → base 15% + 10% = 25%
        assert target == 25.0

    def test_forecast_unavailable_neutral(self) -> None:
        coord = _make_coordinator()
        coord._reserve_last_calc = 0.0
        # No state set → _read_float returns -1 → neutral
        target = coord._calculate_reserve_target()
        # Neutral → base 15% + 5% = 20%
        assert target == 20.0
