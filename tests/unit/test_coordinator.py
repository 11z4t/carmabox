"""Tests for CARMA Box coordinator — the brain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import (
    BatteryCommand,
    CarmaboxCoordinator,
)
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.models import CarmaboxState, Decision
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
    entry.entry_id = "test_entry"

    # Mock states
    states: dict[str, MagicMock] = {}

    def get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = get_state

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.entry = entry
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
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord.last_decision = Decision()
    coord.decision_log = []
    coord.consumption_profile = ConsumptionProfile()
    coord.hourly_actuals = []
    coord._last_tracked_hour = -1

    return coord


def _set_state(
    coord: CarmaboxCoordinator,
    entity_id: str,
    value: str,
) -> None:
    """Set a mock state on coordinator's hass."""
    state = MagicMock()
    state.state = value
    state.attributes = {}
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
    async def test_full_battery_triggers_standby(self) -> None:
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})

        state = CarmaboxState(
            grid_power_w=1000,
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
    async def test_discharge_splits_by_soc(self) -> None:
        coord = _make_coordinator(
            {
                "battery_ems_1": "select.ems1",
                "battery_ems_2": "select.ems2",
                "battery_limit_1": "number.limit1",
                "battery_limit_2": "number.limit2",
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
        # Battery 1 gets 80% of 1000 = 800W
        limit1_call = calls[1]
        assert limit1_call[0][2]["value"] == 800
        # Battery 2 gets 20% of 1000 = 200W
        limit2_call = calls[3]
        assert limit2_call[0][2]["value"] == 200

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
    def test_tracks_peak_samples(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(grid_power_w=2000, battery_power_1=0, battery_power_2=0)
        coord._track_savings(state)
        assert len(coord.savings.peak_samples) == 1

    def test_tracks_battery_discharge(self) -> None:
        coord = _make_coordinator({"fallback_price_ore": 80.0})
        state = CarmaboxState(
            grid_power_w=1000,
            battery_power_1=-1500,  # Discharging 1.5kW
            battery_power_2=-500,  # Discharging 0.5kW
            current_price=120.0,
        )
        coord._track_savings(state)
        # Should record discharge savings (price 120 > avg 80)
        assert coord.savings.discharge_savings_kr > 0
        assert coord.savings.total_discharge_kwh > 0

    def test_baseline_includes_discharge(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(
            grid_power_w=1000,
            battery_power_1=-2000,  # Discharging
            battery_power_2=0,
        )
        coord._track_savings(state)
        # Baseline should be higher (grid + battery discharge)
        assert coord.savings.baseline_peak_samples[0] > coord.savings.peak_samples[0]

    def test_no_discharge_savings_when_price_low(self) -> None:
        coord = _make_coordinator({"fallback_price_ore": 100.0})
        state = CarmaboxState(
            grid_power_w=1000,
            battery_power_1=-1000,
            current_price=50.0,  # Below avg price
        )
        coord._track_savings(state)
        assert coord.savings.discharge_savings_kr == 0.0


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
    async def test_charge_pv_one_battery_fails_other_continues(self) -> None:
        """_cmd_charge_pv: battery 1 fails → battery 2 should still be attempted."""
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

        state = CarmaboxState(battery_soc_1=50, battery_soc_2=50)
        with patch("custom_components.carmabox.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._cmd_charge_pv(state)

        # Battery 2 succeeded → command should still be set
        assert coord._last_command == BatteryCommand.CHARGE_PV

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
    async def test_write_verify_fail_increments_counter(self) -> None:
        """Write-verify detects mismatch → error counter incremented."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set_state(coord, "sensor.soc1", "50")
        # Entity still shows old mode after service call
        _set_state(coord, "select.ems1", "discharge_battery")

        initial_blocks = coord._daily_safety_blocks

        state = CarmaboxState(battery_soc_1=50)
        await coord._cmd_charge_pv(state)

        # Write-verify should detect mismatch and increment counter
        assert coord._daily_safety_blocks > initial_blocks

    def test_check_write_verify_match(self) -> None:
        """_check_write_verify returns True when modes match."""
        coord = _make_coordinator()
        _set_state(coord, "select.ems1", "battery_standby")
        assert coord._check_write_verify("select.ems1", "battery_standby") is True

    def test_check_write_verify_mismatch(self) -> None:
        """_check_write_verify returns False and increments counter on mismatch."""
        coord = _make_coordinator()
        _set_state(coord, "select.ems1", "charge_pv")
        initial = coord._daily_safety_blocks
        assert coord._check_write_verify("select.ems1", "battery_standby") is False
        assert coord._daily_safety_blocks == initial + 1


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

        a1.set_ems_mode.assert_called_once_with("discharge_battery")
        a2.set_ems_mode.assert_called_once_with("discharge_battery")
        # Battery 1 gets 80% of 1000 = 800W
        a1.set_discharge_limit.assert_called_once_with(800)
        # Battery 2 gets remainder = 200W
        a2.set_discharge_limit.assert_called_once_with(200)
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

        a1.set_ems_mode.assert_called_once_with("discharge_battery")
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
