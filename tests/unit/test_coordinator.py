"""Tests for CARMA Box coordinator — the brain."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import (
    BatteryCommand,
    CarmaboxCoordinator,
)
from custom_components.carmabox.optimizer.models import CarmaboxState


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
    coord.safety.check_discharge = MagicMock(
        return_value=MagicMock(ok=True, reason="")
    )
    coord.plan = []
    coord._plan_counter = 0
    coord._last_command = BatteryCommand.IDLE
    coord.target_kw = options.get("target_weighted_kw", 2.0) if options else 2.0
    coord.min_soc = options.get("min_soc", 15.0) if options else 15.0
    coord.logger = MagicMock()
    coord.name = "carmabox"
    coord._states = states

    return coord


def _set_state(
    coord: CarmaboxCoordinator, entity_id: str, value: str,
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


class TestExecute:
    @pytest.mark.asyncio
    async def test_export_triggers_charge_pv(self) -> None:
        coord = _make_coordinator({
            "battery_ems_1": "select.ems1",
            "battery_soc_1": "sensor.soc1",
        })
        _set_state(coord, "sensor.soc1", "50")
        _set_state(coord, "select.ems1", "battery_standby")

        state = CarmaboxState(grid_power_w=-1000, battery_soc_1=50)
        await coord._execute(state)
        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_full_battery_triggers_standby(self) -> None:
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})

        state = CarmaboxState(
            grid_power_w=1000, battery_soc_1=100, battery_soc_2=-1,
        )
        await coord._execute(state)
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_high_load_triggers_discharge(self) -> None:
        coord = _make_coordinator({
            "battery_ems_1": "select.ems1",
            "battery_limit_1": "number.limit1",
        })

        state = CarmaboxState(
            grid_power_w=5000, battery_soc_1=80, battery_soc_2=-1,
        )
        with patch(
            "custom_components.carmabox.coordinator.datetime"
        ) as mock_dt:
            mock_dt.now.return_value.hour = 18  # Daytime weight=1.0
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_safety_block_prevents_discharge(self) -> None:
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord.safety.check_discharge = MagicMock(
            return_value=MagicMock(ok=False, reason="min_soc")
        )

        state = CarmaboxState(
            grid_power_w=5000, battery_soc_1=10, battery_soc_2=-1,
        )
        with patch(
            "custom_components.carmabox.coordinator.datetime"
        ) as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)

        assert coord._last_command != BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_under_target_stays_idle(self) -> None:
        coord = _make_coordinator()
        state = CarmaboxState(
            grid_power_w=1000, battery_soc_1=50, battery_soc_2=-1,
        )
        with patch(
            "custom_components.carmabox.coordinator.datetime"
        ) as mock_dt:
            mock_dt.now.return_value.hour = 18
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_charge_pv_skips_full_battery(self) -> None:
        """Full battery should get standby, not charge_pv."""
        coord = _make_coordinator({
            "battery_ems_1": "select.ems1",
            "battery_soc_1": "sensor.soc1",
        })
        _set_state(coord, "sensor.soc1", "100")

        state = CarmaboxState(
            grid_power_w=-2000, battery_soc_1=100, battery_soc_2=-1,
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

    def test_no_duplicate_command(self) -> None:
        """Same command should not re-send."""
        coord = _make_coordinator({"battery_ems_1": "select.ems1"})
        coord._last_command = BatteryCommand.STANDBY

        # Calling standby again should be no-op
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            coord._cmd_standby(CarmaboxState())
        )
        coord.hass.services.async_call.assert_not_called()
