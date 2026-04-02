"""Integration tests for CoordinatorBridge.

Tests bridge logic: state collection, shadow mode, V2 cycle dispatch,
plan generation, and persistent state save/restore.
All HA dependencies are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hass():
    """Create a minimal mocked HomeAssistant object."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    # Default: all states return "0"
    hass.states.get = MagicMock(return_value=MagicMock(state="0"))
    return hass


def _make_entry(extra_data: dict | None = None):
    """Create a minimal mocked ConfigEntry."""
    entry = MagicMock()
    data = {
        "inverter_1_prefix": "kontor",
        "inverter_1_device_id": "dev1",
        "inverter_2_prefix": "forrad",
        "inverter_2_device_id": "dev2",
        "use_coordinator_v2": True,
        "executor_enabled": False,
        "ev_enabled": False,
        "weather_enabled": False,
    }
    if extra_data:
        data.update(extra_data)
    entry.data = data
    entry.options = {}
    entry.entry_id = "test_bridge"
    return entry


def _make_bridge(hass=None, entry=None):
    """Instantiate CoordinatorBridge with patched HA internals."""
    from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

    hass = hass or _make_hass()
    entry = entry or _make_entry()

    # Patch DataUpdateCoordinator.__init__ and adapters to avoid HA runtime deps
    with (
        patch(
            "custom_components.carmabox.coordinator_bridge.DataUpdateCoordinator.__init__",
            return_value=None,
        ),
        patch(
            "custom_components.carmabox.coordinator_bridge.GoodWeAdapter",
            return_value=MagicMock(
                soc=50.0,
                power_w=0.0,
                temperature_c=20.0,
                ems_mode="peak_shaving",
                fast_charging_on=False,
                prefix="mock",
                _analyze_only=False,
                set_ems_mode=AsyncMock(return_value=True),
                set_discharge_limit=AsyncMock(return_value=True),
                set_fast_charging=AsyncMock(return_value=True),
            ),
        ),
        patch(
            "custom_components.carmabox.coordinator_bridge.Store",
            return_value=MagicMock(
                async_save=AsyncMock(),
                async_load=AsyncMock(return_value=None),
            ),
        ),
    ):
        bridge = CoordinatorBridge(hass, entry)
        # Manually set hass since DataUpdateCoordinator.__init__ was patched
        bridge.hass = hass

    return bridge


# ---------------------------------------------------------------------------
# 1. Import test
# ---------------------------------------------------------------------------


class TestBridgeImport:
    def test_bridge_imports(self) -> None:
        """CoordinatorBridge can be imported without errors."""
        from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

        assert CoordinatorBridge is not None
        assert hasattr(CoordinatorBridge, "_async_update_data")
        assert hasattr(CoordinatorBridge, "_collect_system_state")
        assert hasattr(CoordinatorBridge, "_generate_plan")


# ---------------------------------------------------------------------------
# 2. State collection
# ---------------------------------------------------------------------------


class TestBridgeCollectSystemState:
    def test_collect_system_state_populates_fields(self) -> None:
        """_collect_system_state reads adapter values into SystemState."""
        bridge = _make_bridge()

        # Adapters have soc=50, power_w=0, temp=20, ems_mode=peak_shaving
        state = bridge._collect_system_state()

        assert state.battery_soc_1 == 50.0
        assert state.battery_soc_2 == 50.0
        assert state.battery_temp_1 == 20.0
        assert state.battery_temp_2 == 20.0
        assert state.ems_mode_1 == "peak_shaving"
        assert state.ems_mode_2 == "peak_shaving"
        assert state.fast_charging_1 is False
        assert state.fast_charging_2 is False
        # Grid / PV read from hass.states.get which returns "0"
        assert state.grid_import_w == 0.0
        assert state.pv_power_w == 0.0

    def test_collect_system_state_reads_price(self) -> None:
        """Price entity is read into current_price field."""
        hass = _make_hass()
        entry = _make_entry({"price_entity": "sensor.nordpool"})
        hass.states.get = MagicMock(return_value=MagicMock(state="85.5"))
        bridge = _make_bridge(hass, entry)

        state = bridge._collect_system_state()
        assert state.current_price == 85.5

    def test_collect_system_state_no_adapters(self) -> None:
        """When no inverter adapters exist, fallback to HA entity reads."""
        entry = _make_entry(
            {
                "inverter_1_prefix": "",
                "inverter_2_prefix": "",
            }
        )
        hass = _make_hass()
        bridge = _make_bridge(hass, entry)

        # Force empty adapter list (patch may have added mocks)
        bridge.inverter_adapters = []

        state = bridge._collect_system_state()
        # Should not crash, defaults used
        assert state.battery_soc_1 == 0.0
        assert state.battery_temp_1 == 15.0  # default fallback


# ---------------------------------------------------------------------------
# 3. Shadow mode — no execution
# ---------------------------------------------------------------------------


class TestBridgeShadowMode:
    @pytest.mark.asyncio
    async def test_shadow_mode_no_execution(self) -> None:
        """With executor_enabled=False, adapter commands are NOT called."""
        bridge = _make_bridge()
        assert bridge.executor_enabled is False

        # Mock V2 cycle to return commands
        from custom_components.carmabox.core.coordinator_v2 import CycleResult

        mock_result = CycleResult(
            battery_commands=[
                {"id": 0, "mode": "discharge_pv", "power_limit": 2000, "fast_charging": False}
            ],
            ev_command={"action": "start", "amps": 8},
            surplus_actions=[{"id": "miner", "action": "on"}],
            grid_guard_status="OK",
            plan_action="discharge",
            reason="test shadow",
            breaches=[],
            notifications=[],
        )
        bridge._v2.cycle = MagicMock(return_value=mock_result)
        bridge._v2.plan = []

        # Set up required state for _async_update_data
        bridge._state_restored = True
        bridge._startup_safety_confirmed = True
        bridge._last_plan_time = float("inf")  # skip plan generation
        bridge.data = None

        state = await bridge._async_update_data()

        # Verify NO adapter calls were made
        for adapter in bridge.inverter_adapters:
            adapter.set_ems_mode.assert_not_called()
            adapter.set_discharge_limit.assert_not_called()
            adapter.set_fast_charging.assert_not_called()

        # State should still be returned
        assert state is not None


# ---------------------------------------------------------------------------
# 4. V2 cycle runs
# ---------------------------------------------------------------------------


class TestBridgeV2Cycle:
    @pytest.mark.asyncio
    async def test_v2_cycle_runs(self) -> None:
        """When _use_v2=True, CoordinatorV2.cycle() is called."""
        bridge = _make_bridge()
        assert bridge._use_v2 is True

        from custom_components.carmabox.core.coordinator_v2 import CycleResult

        mock_result = CycleResult(
            battery_commands=[],
            ev_command=None,
            surplus_actions=[],
            grid_guard_status="OK",
            plan_action="idle",
            reason="test",
            breaches=[],
            notifications=[],
        )
        bridge._v2.cycle = MagicMock(return_value=mock_result)
        bridge._v2.plan = []

        bridge._state_restored = True
        bridge._startup_safety_confirmed = True
        bridge._last_plan_time = float("inf")
        bridge.data = None

        await bridge._async_update_data()

        bridge._v2.cycle.assert_called_once()
        # Verify it was called with a SystemState
        from custom_components.carmabox.core.coordinator_v2 import SystemState

        args = bridge._v2.cycle.call_args
        assert isinstance(args[0][0], SystemState)

    @pytest.mark.asyncio
    async def test_v2_disabled_skips_cycle(self) -> None:
        """When _use_v2=False, V2 cycle is NOT called."""
        bridge = _make_bridge()
        bridge._use_v2 = False

        bridge._v2.cycle = MagicMock()
        bridge._state_restored = True
        bridge._startup_safety_confirmed = True
        bridge._last_plan_time = float("inf")
        bridge.data = None

        await bridge._async_update_data()

        bridge._v2.cycle.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Plan generation
# ---------------------------------------------------------------------------


class TestBridgePlanGeneration:
    @pytest.mark.asyncio
    async def test_generate_plan_builds_planner_input(self) -> None:
        """_generate_plan builds PlannerInput from mocked Nordpool/Solcast data."""
        bridge = _make_bridge()

        # Mock NordpoolAdapter
        mock_nordpool = MagicMock()
        mock_nordpool.today_prices = [50.0] * 24
        mock_nordpool.tomorrow_prices = [60.0] * 24

        # Mock SolcastAdapter
        mock_solcast = MagicMock()
        mock_solcast.today_hourly_kw = [0.0] * 6 + [3.0] * 12 + [0.0] * 6
        mock_solcast.tomorrow_hourly_kw = [0.0] * 6 + [2.5] * 12 + [0.0] * 6
        mock_solcast.tomorrow_kwh = 30.0

        with (
            patch(
                "custom_components.carmabox.coordinator_bridge.NordpoolAdapter",
                return_value=mock_nordpool,
            ),
            patch(
                "custom_components.carmabox.coordinator_bridge.SolcastAdapter",
                return_value=mock_solcast,
            ),
            patch(
                "custom_components.carmabox.coordinator_bridge.generate_carma_plan",
                return_value=[],
            ) as mock_gen,
        ):
            await bridge._generate_plan()

        # generate_carma_plan should have been called
        mock_gen.assert_called_once()
        planner_input = mock_gen.call_args[0][0]

        from custom_components.carmabox.core.planner import PlannerInput

        assert isinstance(planner_input, PlannerInput)
        assert planner_input.battery_cap_kwh > 0
        assert len(planner_input.hourly_prices) > 0
        assert len(planner_input.hourly_pv) == len(planner_input.hourly_prices)
        assert len(planner_input.hourly_loads) == len(planner_input.hourly_prices)

    @pytest.mark.asyncio
    async def test_generate_plan_populates_plan_list(self) -> None:
        """After _generate_plan, self.plan is populated with HourPlan entries."""
        bridge = _make_bridge()

        from custom_components.carmabox.core.plan_executor import PlanAction

        mock_actions = [
            PlanAction(
                hour=h,
                action="idle",
                battery_kw=0.0,
                grid_kw=1.0,
                price=50.0,
                battery_soc=50,
                ev_soc=-1,
            )
            for h in range(24)
        ]

        mock_nordpool = MagicMock()
        mock_nordpool.today_prices = [50.0] * 24
        mock_nordpool.tomorrow_prices = []
        mock_solcast = MagicMock()
        mock_solcast.today_hourly_kw = [0.0] * 24
        mock_solcast.tomorrow_hourly_kw = [0.0] * 24
        mock_solcast.tomorrow_kwh = 0.0

        with (
            patch(
                "custom_components.carmabox.coordinator_bridge.NordpoolAdapter",
                return_value=mock_nordpool,
            ),
            patch(
                "custom_components.carmabox.coordinator_bridge.SolcastAdapter",
                return_value=mock_solcast,
            ),
            patch(
                "custom_components.carmabox.coordinator_bridge.generate_carma_plan",
                return_value=mock_actions,
            ),
        ):
            await bridge._generate_plan()

        assert len(bridge.plan) == 24
        assert bridge._plan_generated is True
        assert bridge.total_charge_kwh == 0.0
        # All grid_kw=1.0 positive => cost > 0
        assert bridge.estimated_cost_kr > 0


# ---------------------------------------------------------------------------
# 6. Persistence — save and restore
# ---------------------------------------------------------------------------


class TestBridgePersistence:
    @pytest.mark.asyncio
    async def test_save_state(self) -> None:
        """_async_save_state serializes plan + EV state to Store."""
        bridge = _make_bridge()

        from custom_components.carmabox.optimizer.models import HourPlan

        bridge.plan = [
            HourPlan(
                hour=10,
                action="discharge",
                battery_kw=-2.0,
                grid_kw=0.5,
                weighted_kw=0.0,
                pv_kw=3.0,
                consumption_kw=1.5,
                ev_kw=0.0,
                ev_soc=-1,
                battery_soc=60.0,
                price=80.0,
            ),
        ]
        bridge.night_ev_active = True
        from custom_components.carmabox.coordinator import BatteryCommand

        bridge._last_command = BatteryCommand.DISCHARGE
        bridge._ev_enabled = True
        bridge._ev_current_amps = 10

        # Force save by resetting timing
        bridge._last_save_time = 0.0

        await bridge._async_save_state()

        bridge._store.async_save.assert_called_once()
        saved = bridge._store.async_save.call_args[0][0]

        assert saved["night_ev_active"] is True
        assert saved["last_command"] == "discharge"
        assert saved["ev_enabled"] is True
        assert saved["ev_current_amps"] == 10
        assert len(saved["plan"]) == 1
        assert saved["plan"][0]["hour"] == 10
        assert saved["plan"][0]["action"] == "discharge"
        assert saved["plan"][0]["battery_kw"] == -2.0

    @pytest.mark.asyncio
    async def test_restore_state(self) -> None:
        """_async_restore_state loads plan + EV state from Store."""
        bridge = _make_bridge()
        bridge._state_restored = False

        stored_data = {
            "plan": [
                {
                    "hour": 14,
                    "action": "charge",
                    "battery_kw": 3.0,
                    "grid_kw": 1.0,
                    "weighted_kw": 0.0,
                    "pv_kw": 5.0,
                    "consumption_kw": 2.0,
                    "ev_kw": 0.0,
                    "ev_soc": 40,
                    "battery_soc": 30,
                    "price": 25.0,
                },
            ],
            "night_ev_active": True,
            "last_command": "charge_pv",
            "ev_enabled": True,
            "ev_current_amps": 8,
            "saved_at": "2026-03-29T12:00:00",
        }
        bridge._store.async_load = AsyncMock(return_value=stored_data)

        await bridge._async_restore_state()

        assert bridge._state_restored is True
        assert bridge.night_ev_active is True
        from custom_components.carmabox.coordinator import BatteryCommand

        assert bridge._last_command == BatteryCommand.CHARGE_PV
        assert bridge._ev_enabled is True
        assert bridge._ev_current_amps == 8
        assert len(bridge.plan) == 1
        assert bridge.plan[0].hour == 14
        assert bridge.plan[0].action == "charge"
        assert bridge.plan[0].battery_kw == 3.0

    @pytest.mark.asyncio
    async def test_restore_no_data(self) -> None:
        """_async_restore_state handles missing data gracefully."""
        bridge = _make_bridge()
        bridge._state_restored = False

        bridge._store.async_load = AsyncMock(return_value=None)

        await bridge._async_restore_state()

        assert bridge._state_restored is True
        assert bridge.plan == []  # unchanged from init
        from custom_components.carmabox.coordinator import BatteryCommand

        assert bridge._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_restore_skips_if_already_restored(self) -> None:
        """_async_restore_state is idempotent — skips if already restored."""
        bridge = _make_bridge()
        bridge._state_restored = True

        bridge._store.async_load = AsyncMock()

        await bridge._async_restore_state()

        bridge._store.async_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_rate_limited(self) -> None:
        """_async_save_state skips if called too frequently."""
        import time

        bridge = _make_bridge()
        bridge._last_save_time = time.monotonic()  # just saved

        await bridge._async_save_state()

        bridge._store.async_save.assert_not_called()


# ---------------------------------------------------------------------------
# PLAT-1095: Ellevio hour samples persistence
# ---------------------------------------------------------------------------


class TestEllevioSamplesPersistence:
    @pytest.mark.asyncio
    async def test_ellevio_samples_persist_across_restart(self) -> None:
        """Save state → create new bridge → verify samples restored."""

        bridge = _make_bridge()
        bridge._last_save_time = 0.0

        # Populate 3 samples
        _HOUR = 14  # fixed — avoids flakiness at hour boundary
        bridge._ellevio_hour_samples = [(1.5, 1.0), (1.2, 0.5), (1.8, 1.0)]
        bridge._ellevio_current_hour = _HOUR

        await bridge._async_save_state()
        bridge._store.async_save.assert_called_once()
        saved = bridge._store.async_save.call_args[0][0]

        # Verify samples were serialized
        assert len(saved["ellevio_hour_samples"]) == 3
        assert saved["ellevio_current_hour"] == _HOUR
        assert saved["ellevio_saved_at"] > 0

        # Create new bridge and restore from saved state
        bridge2 = _make_bridge()
        bridge2._state_restored = False
        bridge2._store.async_load = AsyncMock(return_value=saved)

        # Patch datetime so restored hour matches saved hour (avoids hour-boundary flakiness)
        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value.hour = _HOUR
            await bridge2._async_restore_state()

        assert len(bridge2._ellevio_hour_samples) == 3
        assert bridge2._ellevio_hour_samples[0] == (1.5, 1.0)
        assert bridge2._ellevio_hour_samples[1] == (1.2, 0.5)
        assert bridge2._ellevio_hour_samples[2] == (1.8, 1.0)
        assert bridge2._ellevio_current_hour == _HOUR

    @pytest.mark.asyncio
    async def test_stale_ellevio_samples_discarded(self) -> None:
        """Samples from >1 hour ago are discarded on restore."""
        import time

        bridge = _make_bridge()
        bridge._state_restored = False

        stored_data = {
            "plan": [],
            "night_ev_active": False,
            "last_command": "STANDBY",
            "ev_enabled": False,
            "ev_current_amps": 6,
            "saved_at": "2026-03-29T10:00:00",
            "ellevio_hour_samples": [[1.5, 1.0], [1.2, 0.5]],
            "ellevio_current_hour": 10,
            # Saved 90 minutes ago
            "ellevio_saved_at": time.time() - 5400,
        }
        bridge._store.async_load = AsyncMock(return_value=stored_data)

        await bridge._async_restore_state()

        # Stale samples (>1h old) should be discarded
        assert bridge._ellevio_hour_samples == []
        assert bridge._ellevio_current_hour == -1  # unchanged from init

    @pytest.mark.asyncio
    async def test_grid_guard_correct_projection_after_restart(self) -> None:
        """After restore with valid samples, weighted average is correct."""
        import time

        _HOUR = 14  # fixed — avoids flakiness at hour boundary
        bridge = _make_bridge()
        bridge._state_restored = False

        stored_data = {
            "plan": [],
            "night_ev_active": False,
            "last_command": "STANDBY",
            "ev_enabled": False,
            "ev_current_amps": 6,
            "saved_at": "2026-04-02T14:00:00",
            "ellevio_hour_samples": [[2.0, 1.0], [1.0, 1.0], [3.0, 1.0]],
            "ellevio_current_hour": _HOUR,
            "ellevio_saved_at": time.time() - 30,  # 30s ago — fresh
        }
        bridge._store.async_load = AsyncMock(return_value=stored_data)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value.hour = _HOUR
            await bridge._async_restore_state()

        # Samples restored — verify weighted average computation
        samples = bridge._ellevio_hour_samples
        assert len(samples) == 3
        total = sum(p * w for p, w in samples)
        wt = sum(w for _, w in samples)
        weighted_avg = total / wt
        # (2*1 + 1*1 + 3*1) / (1+1+1) = 6/3 = 2.0
        assert abs(weighted_avg - 2.0) < 0.01
