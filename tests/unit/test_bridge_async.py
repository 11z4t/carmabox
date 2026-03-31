"""Coverage tests for CoordinatorBridge async command methods.

Targets coordinator_bridge.py async methods:
  Lines 390-448  — _execute_battery_commands
  Lines 450-477  — _execute_ev_command
  Lines 479-509  — _enforce_ems_modes
  Lines 511-545  — _detect_and_fix_crosscharge
  Lines 547-576  — _execute_surplus_actions
  Lines 727-750  — _track_ellevio_sample
  Lines 836-879  — _async_restore_state (partial)
  Lines 1019-1134 — _async_update_data branches
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator_bridge import CoordinatorBridge
from custom_components.carmabox.core.coordinator_v2 import CycleResult, SystemState

# ── Factory ───────────────────────────────────────────────────────────────────


def _make_bridge(
    *,
    n_adapters: int = 1,
    ev_adapter: object | None = None,
    miner_entity: str = "",
    executor_enabled: bool = True,
) -> CoordinatorBridge:
    """Create CoordinatorBridge bypassing __init__."""
    bridge = object.__new__(CoordinatorBridge)

    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=None)
    bridge.hass = hass

    # Build mock adapters
    bridge.inverter_adapters = []
    for i in range(n_adapters):
        adapter = MagicMock()
        adapter.prefix = f"gw{i + 1}"
        adapter.device_id = f"dev{i + 1}"
        adapter.fast_charging_on = False
        adapter.max_discharge_w = 5000
        adapter.set_ems_mode = AsyncMock(return_value=True)
        adapter.set_fast_charging = AsyncMock(return_value=True)
        adapter.set_discharge_limit = AsyncMock(return_value=True)
        bridge.inverter_adapters.append(adapter)

    bridge.ev_adapter = ev_adapter
    bridge._miner_entity = miner_entity
    bridge.executor_enabled = executor_enabled

    return bridge


def _make_cycle_result(
    *,
    battery_commands: list[dict] | None = None,
    ev_command: dict | None = None,
    surplus_actions: list[dict] | None = None,
    plan_action: str = "standby",
    reason: str = "test",
) -> CycleResult:
    return CycleResult(
        battery_commands=battery_commands or [],
        ev_command=ev_command,
        surplus_actions=surplus_actions or [],
        grid_guard_status="ok",
        plan_action=plan_action,
        reason=reason,
        breaches=[],
        notifications=[],
    )


def _make_sys_state(**kwargs: object) -> SystemState:
    return SystemState(**kwargs)


# ── _execute_battery_commands ─────────────────────────────────────────────────


class TestExecuteBatteryCommands:
    @pytest.mark.asyncio
    async def test_discharge_pv_sets_mode_and_limit(self) -> None:
        """discharge_pv with power_limit → set_ems_mode + set_discharge_limit."""
        bridge = _make_bridge()
        adapter = bridge.inverter_adapters[0]

        commands = [{"id": 0, "mode": "discharge_pv", "power_limit": 2000, "fast_charging": False}]
        await bridge._execute_battery_commands(commands)

        adapter.set_ems_mode.assert_awaited_once_with("discharge_pv")
        adapter.set_discharge_limit.assert_awaited_once_with(2000)

    @pytest.mark.asyncio
    async def test_charge_pv_zeros_power_limit(self) -> None:
        """charge_pv → set_ems_mode + set_discharge_limit(0) for PLAT-1040."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge()
        # Replace adapter with a GoodWeAdapter spec mock
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.set_ems_mode = AsyncMock(return_value=True)
        adapter.set_fast_charging = AsyncMock(return_value=True)
        adapter.set_discharge_limit = AsyncMock(return_value=True)
        bridge.inverter_adapters = [adapter]

        commands = [{"id": 0, "mode": "charge_pv", "power_limit": 0, "fast_charging": False}]
        await bridge._execute_battery_commands(commands)

        adapter.set_ems_mode.assert_awaited_once_with("charge_pv")
        adapter.set_discharge_limit.assert_awaited_once_with(0)

    @pytest.mark.asyncio
    async def test_command_for_missing_adapter_logged(self) -> None:
        """Command references adapter id > available adapters — logged, not crashed."""
        bridge = _make_bridge(n_adapters=1)

        commands = [{"id": 1, "mode": "charge_pv", "power_limit": 0, "fast_charging": False}]
        # Should not raise
        await bridge._execute_battery_commands(commands)

    @pytest.mark.asyncio
    async def test_mode_set_failed_logs_error(self) -> None:
        """set_ems_mode returns False → error logged but continues."""
        bridge = _make_bridge()
        bridge.inverter_adapters[0].set_ems_mode = AsyncMock(return_value=False)

        commands = [{"id": 0, "mode": "discharge_pv", "power_limit": 1000, "fast_charging": False}]
        # Should not raise
        await bridge._execute_battery_commands(commands)

    @pytest.mark.asyncio
    async def test_empty_commands_no_ops(self) -> None:
        """Empty command list → no adapter calls."""
        bridge = _make_bridge()
        await bridge._execute_battery_commands([])
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_battery_standby_mode(self) -> None:
        """battery_standby → set_ems_mode called, no discharge limit."""
        bridge = _make_bridge()
        commands = [{"id": 0, "mode": "battery_standby", "power_limit": 0, "fast_charging": False}]
        await bridge._execute_battery_commands(commands)

        bridge.inverter_adapters[0].set_ems_mode.assert_awaited_once_with("battery_standby")
        bridge.inverter_adapters[0].set_discharge_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fast_charging_toggle(self) -> None:
        """fast_charging=True with GoodWeAdapter → set_fast_charging called."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.set_ems_mode = AsyncMock(return_value=True)
        adapter.set_fast_charging = AsyncMock(return_value=True)
        adapter.set_discharge_limit = AsyncMock(return_value=True)
        adapter.fast_charging_on = False
        bridge.inverter_adapters = [adapter]

        commands = [{"id": 0, "mode": "charge_pv", "power_limit": 0, "fast_charging": True}]
        await bridge._execute_battery_commands(commands)

        adapter.set_fast_charging.assert_awaited_once_with(on=True, authorized=True)


# ── _execute_ev_command ───────────────────────────────────────────────────────


class TestExecuteEvCommand:
    @pytest.mark.asyncio
    async def test_none_command_no_op(self) -> None:
        """ev_cmd=None → return immediately."""
        bridge = _make_bridge()
        await bridge._execute_ev_command(None)  # Should not raise

    @pytest.mark.asyncio
    async def test_no_ev_adapter_no_op(self) -> None:
        """ev_adapter=None → return immediately."""
        bridge = _make_bridge(ev_adapter=None)
        await bridge._execute_ev_command({"action": "start", "amps": 10})

    @pytest.mark.asyncio
    async def test_start_action_enables_and_sets_current(self) -> None:
        """action=start → ev_adapter.enable() + set_current(amps)."""
        ev = MagicMock()
        ev.enable = AsyncMock()
        ev.set_current = AsyncMock()
        bridge = _make_bridge(ev_adapter=ev)

        await bridge._execute_ev_command({"action": "start", "amps": 16})

        ev.enable.assert_awaited_once()
        ev.set_current.assert_awaited_once_with(16)

    @pytest.mark.asyncio
    async def test_stop_action_disables_ev(self) -> None:
        """action=stop → ev_adapter.disable()."""
        ev = MagicMock()
        ev.disable = AsyncMock()
        bridge = _make_bridge(ev_adapter=ev)

        await bridge._execute_ev_command({"action": "stop", "amps": 0})

        ev.disable.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_current_action(self) -> None:
        """action=set_current → ev_adapter.set_current(amps)."""
        ev = MagicMock()
        ev.set_current = AsyncMock()
        bridge = _make_bridge(ev_adapter=ev)

        await bridge._execute_ev_command({"action": "set_current", "amps": 12})

        ev.set_current.assert_awaited_once_with(12)

    @pytest.mark.asyncio
    async def test_phase_mode_set_for_easee(self) -> None:
        """phase_mode set → set_charger_phase_mode called for EaseeAdapter."""
        from custom_components.carmabox.adapters.easee import EaseeAdapter

        ev = MagicMock(spec=EaseeAdapter)
        ev.enable = AsyncMock()
        ev.set_current = AsyncMock()
        ev.set_charger_phase_mode = AsyncMock()
        bridge = _make_bridge(ev_adapter=ev)

        await bridge._execute_ev_command(
            {"action": "start", "amps": 10, "ev_phase_mode": "1_phase"}
        )

        ev.set_charger_phase_mode.assert_awaited_once_with("1_phase")

    @pytest.mark.asyncio
    async def test_ev_command_exception_handled(self) -> None:
        """EV command raises exception → logged, not re-raised."""
        ev = MagicMock()
        ev.enable = AsyncMock(side_effect=RuntimeError("adapter offline"))
        bridge = _make_bridge(ev_adapter=ev)

        # Should not raise
        await bridge._execute_ev_command({"action": "start", "amps": 10})


# ── _enforce_ems_modes ────────────────────────────────────────────────────────


class TestEnforceEmsModes:
    @pytest.mark.asyncio
    async def test_matching_mode_no_correction(self) -> None:
        """EMS mode matches target → no correction call."""
        bridge = _make_bridge(n_adapters=1)
        sys_state = _make_sys_state(ems_mode_1="charge_pv")
        result = _make_cycle_result(
            battery_commands=[{"id": 0, "mode": "charge_pv"}]
        )

        await bridge._enforce_ems_modes(sys_state, result)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drifted_mode_triggers_correction(self) -> None:
        """Actual mode != target → set_ems_mode called to correct."""
        bridge = _make_bridge(n_adapters=1)
        sys_state = _make_sys_state(ems_mode_1="battery_standby")
        result = _make_cycle_result(
            battery_commands=[{"id": 0, "mode": "charge_pv"}]
        )

        await bridge._enforce_ems_modes(sys_state, result)
        bridge.inverter_adapters[0].set_ems_mode.assert_awaited_once_with("charge_pv")

    @pytest.mark.asyncio
    async def test_correction_failed_logged(self) -> None:
        """Correction fails (returns False) → error logged, no exception."""
        bridge = _make_bridge(n_adapters=1)
        bridge.inverter_adapters[0].set_ems_mode = AsyncMock(return_value=False)
        sys_state = _make_sys_state(ems_mode_1="battery_standby")
        result = _make_cycle_result(
            battery_commands=[{"id": 0, "mode": "charge_pv"}]
        )

        await bridge._enforce_ems_modes(sys_state, result)
        bridge.inverter_adapters[0].set_ems_mode.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_command_for_missing_adapter_skipped(self) -> None:
        """Command references non-existent adapter → skipped gracefully."""
        bridge = _make_bridge(n_adapters=1)
        sys_state = _make_sys_state()
        result = _make_cycle_result(
            battery_commands=[{"id": 1, "mode": "charge_pv"}]
        )

        await bridge._enforce_ems_modes(sys_state, result)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adapter_2_ems_mode_checked(self) -> None:
        """Second adapter uses ems_mode_2 for comparison."""
        bridge = _make_bridge(n_adapters=2)
        sys_state = _make_sys_state(ems_mode_1="charge_pv", ems_mode_2="battery_standby")
        result = _make_cycle_result(
            battery_commands=[
                {"id": 0, "mode": "charge_pv"},  # matches → no correction
                {"id": 1, "mode": "discharge_pv"},  # mismatch → correction
            ]
        )

        await bridge._enforce_ems_modes(sys_state, result)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()
        bridge.inverter_adapters[1].set_ems_mode.assert_awaited_once_with("discharge_pv")


# ── _detect_and_fix_crosscharge ───────────────────────────────────────────────


class TestDetectAndFixCrosscharge:
    @pytest.mark.asyncio
    async def test_single_adapter_skips_check(self) -> None:
        """Only 1 adapter → crosscharge impossible, return early."""
        bridge = _make_bridge(n_adapters=1)
        sys_state = _make_sys_state(battery_power_1=-2000, battery_power_2=2000)

        await bridge._detect_and_fix_crosscharge(sys_state)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crosscharge_no_action(self) -> None:
        """Both batteries discharging → no crosscharge."""
        bridge = _make_bridge(n_adapters=2)
        sys_state = _make_sys_state(battery_power_1=-1500, battery_power_2=-500)

        await bridge._detect_and_fix_crosscharge(sys_state)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()
        bridge.inverter_adapters[1].set_ems_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_crosscharge_bat1_discharge_bat2_charge(self) -> None:
        """bat1 discharging + bat2 charging → force both to charge_pv."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge(n_adapters=2)
        # Make adapters GoodWeAdapter spec so set_discharge_limit is called
        for i in range(2):
            adapter = MagicMock(spec=GoodWeAdapter)
            adapter.set_ems_mode = AsyncMock(return_value=True)
            adapter.set_discharge_limit = AsyncMock(return_value=True)
            bridge.inverter_adapters[i] = adapter

        sys_state = _make_sys_state(battery_power_1=-2000, battery_power_2=2000)

        await bridge._detect_and_fix_crosscharge(sys_state)

        for adapter in bridge.inverter_adapters:
            adapter.set_ems_mode.assert_awaited_once_with("charge_pv")
            adapter.set_discharge_limit.assert_awaited_once_with(0)

    @pytest.mark.asyncio
    async def test_crosscharge_bat1_charge_bat2_discharge(self) -> None:
        """bat1 charging + bat2 discharging → also crosscharge."""
        bridge = _make_bridge(n_adapters=2)
        sys_state = _make_sys_state(battery_power_1=2000, battery_power_2=-2000)

        await bridge._detect_and_fix_crosscharge(sys_state)

        for adapter in bridge.inverter_adapters:
            adapter.set_ems_mode.assert_awaited_once_with("charge_pv")

    @pytest.mark.asyncio
    async def test_below_threshold_not_crosscharge(self) -> None:
        """Below 200W threshold → noise, not crosscharge."""
        bridge = _make_bridge(n_adapters=2)
        sys_state = _make_sys_state(battery_power_1=-100, battery_power_2=100)

        await bridge._detect_and_fix_crosscharge(sys_state)
        bridge.inverter_adapters[0].set_ems_mode.assert_not_awaited()


# ── _execute_surplus_actions ──────────────────────────────────────────────────


class TestExecuteSurplusActions:
    @pytest.mark.asyncio
    async def test_action_none_skipped(self) -> None:
        """action_type=none → no HA service call."""
        bridge = _make_bridge(miner_entity="switch.miner")
        actions = [{"id": "miner", "action": "none"}]
        await bridge._execute_surplus_actions(actions)
        bridge.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_miner_on_calls_turn_on(self) -> None:
        """action=on, id=miner → switch.turn_on called."""
        bridge = _make_bridge(miner_entity="switch.my_miner")
        actions = [{"id": "miner", "action": "on"}]
        await bridge._execute_surplus_actions(actions)
        bridge.hass.services.async_call.assert_awaited_once_with(
            "switch", "turn_on", {"entity_id": "switch.my_miner"}
        )

    @pytest.mark.asyncio
    async def test_miner_off_calls_turn_off(self) -> None:
        """action=off, id=miner → switch.turn_off called."""
        bridge = _make_bridge(miner_entity="switch.my_miner")
        actions = [{"id": "miner", "action": "off"}]
        await bridge._execute_surplus_actions(actions)
        bridge.hass.services.async_call.assert_awaited_once_with(
            "switch", "turn_off", {"entity_id": "switch.my_miner"}
        )

    @pytest.mark.asyncio
    async def test_no_entity_mapping_skipped(self) -> None:
        """Unknown id with no entity mapping → debug logged, no service call."""
        bridge = _make_bridge(miner_entity="")
        actions = [{"id": "miner", "action": "on"}]
        await bridge._execute_surplus_actions(actions)
        bridge.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_id_skipped(self) -> None:
        """Unknown surplus consumer id → no entity found, skipped."""
        bridge = _make_bridge(miner_entity="switch.miner")
        actions = [{"id": "unknown_consumer", "action": "on"}]
        await bridge._execute_surplus_actions(actions)
        bridge.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_exception_handled(self) -> None:
        """Service call raises → logged, does not propagate."""
        bridge = _make_bridge(miner_entity="switch.my_miner")
        bridge.hass.services.async_call = AsyncMock(side_effect=RuntimeError("HA down"))
        actions = [{"id": "miner", "action": "on"}]
        # Should not raise
        await bridge._execute_surplus_actions(actions)

    @pytest.mark.asyncio
    async def test_empty_actions_no_ops(self) -> None:
        """Empty actions list → no calls."""
        bridge = _make_bridge(miner_entity="switch.miner")
        await bridge._execute_surplus_actions([])
        bridge.hass.services.async_call.assert_not_awaited()


# ── _track_ellevio_sample ────────────────────────────────────────────────────


class TestTrackEllevioSample:
    def _make_track_bridge(self) -> CoordinatorBridge:
        bridge = _make_bridge()
        bridge._cfg = {"ellevio_night_weight": 0.5}
        bridge._ellevio_hour_samples: list[tuple[float, float]] = []
        bridge._ellevio_current_hour = -1
        bridge._ellevio_monthly_hourly_peaks: list[float] = []
        return bridge

    def test_first_sample_added_to_list(self) -> None:
        """First sample in hour → added to _ellevio_hour_samples."""
        from datetime import datetime as real_dt

        bridge = self._make_track_bridge()
        fake_now = real_dt(2026, 3, 31, 10, 0, 0)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            bridge._track_ellevio_sample(2000.0)

        assert len(bridge._ellevio_hour_samples) == 1
        kw, weight = bridge._ellevio_hour_samples[0]
        assert kw == pytest.approx(2.0)
        assert weight == 1.0  # Daytime

    def test_night_sample_uses_night_weight(self) -> None:
        """Night hour (22:00) → weight=0.5."""
        from datetime import datetime as real_dt

        bridge = self._make_track_bridge()
        fake_now = real_dt(2026, 3, 31, 22, 0, 0)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            bridge._track_ellevio_sample(1000.0)

        _, weight = bridge._ellevio_hour_samples[0]
        assert weight == 0.5

    def test_hour_rollover_flushes_samples(self) -> None:
        """New hour → old samples averaged into monthly peaks."""
        from datetime import datetime as real_dt

        bridge = self._make_track_bridge()
        bridge._ellevio_current_hour = 9
        bridge._ellevio_hour_samples = [(1.5, 1.0), (2.5, 1.0)]  # Avg 2.0 kW

        fake_now = real_dt(2026, 3, 31, 10, 0, 0)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            bridge._track_ellevio_sample(500.0)

        # Old hour flushed → 1 peak added
        assert len(bridge._ellevio_monthly_hourly_peaks) == 1
        assert bridge._ellevio_monthly_hourly_peaks[0] == pytest.approx(2.0)

    def test_negative_grid_power_clamped_to_zero(self) -> None:
        """Negative grid power (export) → clamped to 0.0 kW."""
        from datetime import datetime as real_dt

        bridge = self._make_track_bridge()
        fake_now = real_dt(2026, 3, 31, 10, 0, 0)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            bridge._track_ellevio_sample(-500.0)

        kw, _ = bridge._ellevio_hour_samples[0]
        assert kw == 0.0

    def test_peaks_list_truncated_at_800(self) -> None:
        """Monthly peaks list → truncated at 744 when >800 entries."""
        from datetime import datetime as real_dt

        bridge = self._make_track_bridge()
        bridge._ellevio_current_hour = 9
        bridge._ellevio_hour_samples = [(1.0, 1.0)]
        bridge._ellevio_monthly_hourly_peaks = [1.0] * 801  # Exceeds 800

        fake_now = real_dt(2026, 3, 31, 10, 0, 0)

        with patch("custom_components.carmabox.coordinator_bridge.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            bridge._track_ellevio_sample(0.0)

        # Should have truncated to 744
        assert len(bridge._ellevio_monthly_hourly_peaks) == 744
