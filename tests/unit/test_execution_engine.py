"""Tests for core/execution_engine.py — ExecutionEngine.

TDD: tests written BEFORE implementation (PLAT-1141 COORD-02).
Covers:
  - Instantiation
  - enforce_ems_modes (EMS drift, crosscharge, PLAT-1040 power limit)
  - cmd_miner (on/off, no entity)
  - cmd_ev_start (clamp, idempotent, adapter missing, set_current fail)
  - cmd_ev_stop (adapter missing, happy path)
  - cmd_ev_adjust (no adapter, not enabled, same amps, ramp up/down)
  - execute_surplus_allocations (all allocation types)
  - cmd_charge_pv (safety block, partial failure rollback, happy path)
  - cmd_grid_charge (safety block, happy path)
  - cmd_standby (force=True, idempotent, happy path)
  - cmd_discharge (K1 skip, safety block, no stored energy, happy path)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.const import DEFAULT_EV_MAX_AMPS, DEFAULT_EV_MIN_AMPS

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_safety(
    *,
    heartbeat_ok: bool = True,
    rate_ok: bool = True,
    charge_ok: bool = True,
    discharge_ok: bool = True,
) -> MagicMock:
    """Build a mock SafetyGuard."""
    safety = MagicMock()
    safety.check_heartbeat = MagicMock(return_value=MagicMock(ok=heartbeat_ok, reason="test"))
    safety.check_rate_limit = MagicMock(return_value=MagicMock(ok=rate_ok, reason="test"))
    safety.check_charge = MagicMock(return_value=MagicMock(ok=charge_ok, reason="test"))
    safety.check_discharge = MagicMock(return_value=MagicMock(ok=discharge_ok, reason="test"))
    safety.record_mode_change = MagicMock()
    return safety


def _make_adapter(
    *, prefix: str = "kontor", soc: float = 60.0, ems_mode: str = "charge_pv"
) -> MagicMock:
    """Build a mock inverter adapter."""
    adp = MagicMock()
    adp.prefix = prefix
    adp.soc = soc
    adp.ems_mode = ems_mode
    adp.set_ems_mode = AsyncMock(return_value=True)
    adp.set_fast_charging = AsyncMock(return_value=True)
    adp.set_discharge_limit = AsyncMock(return_value=True)
    adp.max_discharge_w = 5000
    adp.max_charge_w = 5000
    return adp


def _make_ev_adapter(*, prefix: str = "easee_home_test") -> MagicMock:
    """Build a mock EV adapter."""
    ev = MagicMock()
    ev.prefix = prefix
    ev.enable = AsyncMock(return_value=True)
    ev.disable = AsyncMock(return_value=True)
    ev.set_current = AsyncMock(return_value=True)
    ev.reset_to_default = AsyncMock(return_value=True)
    ev.cable_locked = True
    return ev


def _make_coord(
    *,
    last_battery_action: str = "charge_pv",
    ev_enabled: bool = False,
    ev_amps: int = 0,
    inverter_adapters: list | None = None,
    ev_adapter: MagicMock | None = None,
    safety: MagicMock | None = None,
    last_command_name: str = "IDLE",
    last_discharge_w: int = 0,
    miner_entity: str = "switch.miner_test",
    executor_enabled: bool = True,
    cfg: dict | None = None,
) -> MagicMock:
    """Build minimal coordinator mock for ExecutionEngine tests."""
    from custom_components.carmabox.coordinator import BatteryCommand

    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.hass.states.get = MagicMock(return_value=None)

    coord._cfg = cfg or {}
    coord._last_battery_action = last_battery_action
    coord._ev_enabled = ev_enabled
    coord._ev_current_amps = ev_amps
    coord._last_command = BatteryCommand[last_command_name]
    coord._last_discharge_w = last_discharge_w
    coord._daily_safety_blocks = 0
    coord.inverter_adapters = inverter_adapters if inverter_adapters is not None else []
    coord.ev_adapter = ev_adapter
    coord.executor_enabled = executor_enabled
    coord._miner_entity = miner_entity
    coord._miner_on = False
    coord._async_save_runtime = AsyncMock()
    coord.safety = safety or _make_safety()
    coord.min_soc = 15.0
    coord.target_kw = 2.0
    coord._read_battery_temp = MagicMock(return_value=20.0)
    coord._read_cell_temp = MagicMock(return_value=20.0)
    coord._get_entity = MagicMock(return_value=None)
    coord._safe_service_call = AsyncMock(return_value=True)
    coord._check_write_verify = MagicMock()
    return coord


def _make_carmabox_state(**kwargs: float) -> MagicMock:
    """Build a minimal CarmaboxState-like mock."""
    state = MagicMock()
    state.battery_soc_1 = kwargs.get("battery_soc_1", 60.0)
    state.battery_soc_2 = kwargs.get("battery_soc_2", 60.0)
    state.battery_power_1 = kwargs.get("battery_power_1", 0.0)
    state.battery_power_2 = kwargs.get("battery_power_2", 0.0)
    state.grid_power_w = kwargs.get("grid_power_w", 0.0)
    state.pv_power_w = kwargs.get("pv_power_w", 0.0)
    state.ev_power_w = kwargs.get("ev_power_w", 0.0)
    state.ev_soc = kwargs.get("ev_soc", -1.0)
    state.total_battery_soc = kwargs.get("total_battery_soc", 60.0)
    return state


# ── Import after helpers ──────────────────────────────────────────────────────


@pytest.fixture()
def engine_no_adapters() -> object:
    """ExecutionEngine with empty coordinator (no adapters)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    return ExecutionEngine(_make_coord())


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Instantiation
# ═══════════════════════════════════════════════════════════════════════════════


def test_instantiation() -> None:
    """ExecutionEngine can be created with a coordinator."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord()
    engine = ExecutionEngine(coord)
    assert engine._coord is coord


# ═══════════════════════════════════════════════════════════════════════════════
# 2. enforce_ems_modes
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enforce_ems_modes_no_adapters(engine_no_adapters: object) -> None:
    """Returns early when no inverter adapters configured."""
    # Should not raise
    await engine_no_adapters.enforce_ems_modes()


@pytest.mark.asyncio
async def test_enforce_ems_modes_charge_pv_resets_power_limit() -> None:
    """PLAT-1040: power limit reset to 0 when desired_ems=charge_pv."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(prefix="kontor", ems_mode="charge_pv")
    coord = _make_coord(last_battery_action="charge_pv", inverter_adapters=[adp])

    # Simulate non-zero power limit on entity
    limit_state = MagicMock()
    limit_state.state = "500"
    coord.hass.states.get = MagicMock(return_value=limit_state)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    coord.hass.services.async_call.assert_called_once_with(
        "number",
        "set_value",
        {"entity_id": "number.goodwe_kontor_ems_power_limit", "value": 0},
    )


@pytest.mark.asyncio
async def test_enforce_ems_modes_power_limit_zero_no_call() -> None:
    """No service call when power limit already 0."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(prefix="kontor", ems_mode="charge_pv")
    coord = _make_coord(last_battery_action="charge_pv", inverter_adapters=[adp])

    limit_state = MagicMock()
    limit_state.state = "0"
    coord.hass.states.get = MagicMock(return_value=limit_state)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_enforce_ems_modes_drift_correction() -> None:
    """Drift detected → re-apply desired EMS mode."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    # Adapter reports discharge_pv but action is charge_pv → drift
    adp = _make_adapter(prefix="kontor", ems_mode="discharge_pv")
    coord = _make_coord(last_battery_action="charge_pv", inverter_adapters=[adp])

    limit_state = MagicMock()
    limit_state.state = "0"
    coord.hass.states.get = MagicMock(return_value=limit_state)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    adp.set_ems_mode.assert_called_with("charge_pv")


@pytest.mark.asyncio
async def test_enforce_ems_modes_crosscharge_prevention() -> None:
    """Drift correction prevents crosscharge: drifted adapter corrected to desired mode.

    desired=discharge_pv (last_battery_action=discharge):
    - kontor=discharge_pv (correct) → no change
    - forrad=charge_pv (drifted) → corrected to discharge_pv
    INV-2 does not fire because enforced modes are consistent (both discharge_pv).
    """
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp1 = _make_adapter(prefix="kontor", ems_mode="discharge_pv")
    adp2 = _make_adapter(prefix="forrad", ems_mode="charge_pv")
    coord = _make_coord(last_battery_action="discharge", inverter_adapters=[adp1, adp2])

    # No limit entity state
    coord.hass.states.get = MagicMock(return_value=None)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    # Kontor already correct — no change
    adp1.set_ems_mode.assert_not_called()
    # Forrad drifted to charge_pv → corrected to discharge_pv
    adp2.set_ems_mode.assert_called_with("discharge_pv")


@pytest.mark.asyncio
async def test_enforce_ems_modes_discharge_standby_skip() -> None:
    """During discharge, battery_standby on individual adapter is allowed (alloc=0W)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    # adapter 1: discharge_pv, adapter 2: battery_standby (zero allocation)
    adp1 = _make_adapter(prefix="kontor", ems_mode="discharge_pv")
    adp2 = _make_adapter(prefix="forrad", ems_mode="battery_standby")
    coord = _make_coord(last_battery_action="discharge", inverter_adapters=[adp1, adp2])
    coord.hass.states.get = MagicMock(return_value=None)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    # adp2 should NOT be forced to discharge_pv (standby is legitimate)
    adp2.set_ems_mode.assert_not_called()


@pytest.mark.asyncio
async def test_enforce_ems_modes_fast_charging_off_during_discharge() -> None:
    """INV-3: fast_charging must be OFF when discharging."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(prefix="kontor", ems_mode="discharge_pv")
    coord = _make_coord(last_battery_action="discharge", inverter_adapters=[adp])

    # fast_charging is ON
    fc_state = MagicMock()
    fc_state.state = "on"
    coord.hass.states.get = MagicMock(return_value=fc_state)

    engine = ExecutionEngine(coord)
    await engine.enforce_ems_modes()

    adp.set_fast_charging.assert_any_call(on=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. cmd_miner
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_miner_no_entity() -> None:
    """Returns early when no miner entity configured."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(miner_entity="")
    engine = ExecutionEngine(coord)
    await engine.cmd_miner(on=True)

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_miner_turn_on() -> None:
    """cmd_miner(on=True) calls switch.turn_on."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(miner_entity="switch.miner_test")
    engine = ExecutionEngine(coord)
    await engine.cmd_miner(on=True)

    coord.hass.services.async_call.assert_called_once_with(
        "switch", "turn_on", {"entity_id": "switch.miner_test"}
    )
    assert coord._miner_on is True
    coord._async_save_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_miner_turn_off() -> None:
    """cmd_miner(on=False) calls switch.turn_off."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(miner_entity="switch.miner_test")
    engine = ExecutionEngine(coord)
    await engine.cmd_miner(on=False)

    coord.hass.services.async_call.assert_called_once_with(
        "switch", "turn_off", {"entity_id": "switch.miner_test"}
    )
    assert coord._miner_on is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. cmd_ev_start
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_ev_start_no_adapter() -> None:
    """Returns early when no EV adapter."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(ev_adapter=None)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(DEFAULT_EV_MIN_AMPS)

    coord._async_save_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_ev_start_idempotent() -> None:
    """Returns early when already enabled at same amps."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=True, ev_amps=DEFAULT_EV_MIN_AMPS, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(DEFAULT_EV_MIN_AMPS)

    ev.set_current.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ev_start_clamps_below_min() -> None:
    """Amps below DEFAULT_EV_MIN_AMPS clamped to DEFAULT_EV_MIN_AMPS."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(1)  # Way below min

    ev.set_current.assert_called_once_with(DEFAULT_EV_MIN_AMPS)


@pytest.mark.asyncio
async def test_cmd_ev_start_clamps_above_max() -> None:
    """Amps above DEFAULT_EV_MAX_AMPS clamped to DEFAULT_EV_MAX_AMPS."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(99)  # Way above max

    ev.set_current.assert_called_once_with(DEFAULT_EV_MAX_AMPS)


@pytest.mark.asyncio
async def test_cmd_ev_start_set_current_fails() -> None:
    """When set_current fails, EV not enabled, runtime not saved."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    ev.set_current = AsyncMock(return_value=False)
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(DEFAULT_EV_MIN_AMPS)

    ev.enable.assert_not_called()
    coord._async_save_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_ev_start_happy_path() -> None:
    """Happy path: set_current OK → enable → state updated."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(DEFAULT_EV_MIN_AMPS)

    ev.set_current.assert_called_once_with(DEFAULT_EV_MIN_AMPS)
    ev.enable.assert_called_once()
    assert coord._ev_enabled is True
    assert coord._ev_current_amps == DEFAULT_EV_MIN_AMPS
    coord._async_save_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_ev_start_enable_fails_disables() -> None:
    """When enable fails, disable is called and state not updated."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    ev.enable = AsyncMock(return_value=False)
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_start(DEFAULT_EV_MIN_AMPS)

    ev.disable.assert_called_once()
    assert coord._ev_enabled is False
    coord._async_save_runtime.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. cmd_ev_stop
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_ev_stop_no_adapter() -> None:
    """Returns early when no EV adapter."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(ev_adapter=None)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_stop()

    coord._async_save_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_ev_stop_happy_path() -> None:
    """Stops EV: disable + reset + state cleared."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=True, ev_amps=DEFAULT_EV_MIN_AMPS, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_stop()

    ev.disable.assert_called_once()
    ev.reset_to_default.assert_called_once()
    assert coord._ev_enabled is False
    assert coord._ev_current_amps == 0
    coord._async_save_runtime.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. cmd_ev_adjust
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_ev_adjust_no_adapter() -> None:
    """Returns early when no EV adapter."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(ev_adapter=None)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(DEFAULT_EV_MIN_AMPS)

    coord._async_save_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_ev_adjust_not_enabled() -> None:
    """Returns early when EV not currently enabled."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=False, ev_amps=0, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(10)

    ev.set_current.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ev_adjust_same_amps_no_op() -> None:
    """Returns early when amps unchanged."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=True, ev_amps=10, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(10)

    ev.set_current.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ev_adjust_ramp_up_one_step() -> None:
    """EXP-04: ramp UP goes one step at a time via EV_RAMP_STEPS."""
    from custom_components.carmabox.const import EV_RAMP_STEPS
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    # Currently at 6A, target 16A → should only go to first step above 6
    current = DEFAULT_EV_MIN_AMPS  # 6A
    first_step_above = next(s for s in EV_RAMP_STEPS if s > current)
    coord = _make_coord(ev_enabled=True, ev_amps=current, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(DEFAULT_EV_MAX_AMPS)  # Target max

    ev.set_current.assert_called_once_with(min(first_step_above, DEFAULT_EV_MAX_AMPS))


@pytest.mark.asyncio
async def test_cmd_ev_adjust_ramp_down_direct() -> None:
    """Ramp DOWN goes directly to target (safe, no surge risk)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=True, ev_amps=DEFAULT_EV_MAX_AMPS, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(DEFAULT_EV_MIN_AMPS)

    ev.set_current.assert_called_once_with(DEFAULT_EV_MIN_AMPS)
    assert coord._ev_current_amps == DEFAULT_EV_MIN_AMPS


@pytest.mark.asyncio
async def test_cmd_ev_adjust_set_current_ok_updates_state() -> None:
    """State updated when set_current succeeds."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_enabled=True, ev_amps=DEFAULT_EV_MAX_AMPS, ev_adapter=ev)
    engine = ExecutionEngine(coord)
    await engine.cmd_ev_adjust(8)

    assert coord._ev_current_amps == 8
    coord._async_save_runtime.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. execute_surplus_allocations
# ═══════════════════════════════════════════════════════════════════════════════


def _alloc(alloc_id: str, action: str, target_w: int = 0) -> MagicMock:
    """Create a surplus allocation mock."""
    a = MagicMock()
    a.id = alloc_id
    a.action = action
    a.target_w = target_w
    return a


@pytest.mark.asyncio
async def test_execute_surplus_allocations_none_action_skipped() -> None:
    """Allocations with action='none' are skipped."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord()
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("miner", "none")])

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_execute_surplus_allocations_miner_start() -> None:
    """Miner start calls switch.turn_on."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord()
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("miner", "start")])

    coord.hass.services.async_call.assert_called_once_with(
        "switch", "turn_on", {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"}
    )


@pytest.mark.asyncio
async def test_execute_surplus_allocations_miner_stop() -> None:
    """Miner stop calls switch.turn_off."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord()
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("miner", "stop")])

    coord.hass.services.async_call.assert_called_once_with(
        "switch", "turn_off", {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"}
    )


@pytest.mark.asyncio
async def test_execute_surplus_allocations_ev_start() -> None:
    """EV start with sufficient wattage calls cmd_ev_start."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_adapter=ev, ev_enabled=False, ev_amps=0)
    engine = ExecutionEngine(coord)
    # 3-phase 6A = 4140W minimum
    await engine.execute_surplus_allocations([_alloc("ev", "start", target_w=5000)])

    ev.set_current.assert_called_once()
    assert coord._ev_enabled is True


@pytest.mark.asyncio
async def test_execute_surplus_allocations_ev_start_insufficient_power() -> None:
    """EV start skipped when target_w < 4140W (3-phase 6A minimum)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_adapter=ev, ev_enabled=False, ev_amps=0)
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("ev", "start", target_w=3000)])

    ev.set_current.assert_not_called()


@pytest.mark.asyncio
async def test_execute_surplus_allocations_ev_stop() -> None:
    """EV stop calls cmd_ev_stop."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    ev = _make_ev_adapter()
    coord = _make_coord(ev_adapter=ev, ev_enabled=True, ev_amps=DEFAULT_EV_MIN_AMPS)
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("ev", "stop")])

    ev.disable.assert_called_once()
    assert coord._ev_enabled is False


@pytest.mark.asyncio
async def test_execute_surplus_allocations_battery_charge() -> None:
    """Battery start/increase calls set_ems_mode + set_fast_charging."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter()
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("battery", "start", target_w=3000)])

    adp.set_ems_mode.assert_called_with("charge_pv")
    adp.set_fast_charging.assert_called_once()


@pytest.mark.asyncio
async def test_execute_surplus_allocations_battery_stop() -> None:
    """Battery stop/decrease calls set_fast_charging(on=False)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter()
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    await engine.execute_surplus_allocations([_alloc("battery", "stop")])

    adp.set_fast_charging.assert_called_with(on=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. cmd_charge_pv
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_charge_pv_idempotent() -> None:
    """Returns early if already in CHARGE_PV or CHARGE_PV_TAPER state."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(last_command_name="CHARGE_PV")
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_charge_pv(state)

    coord.safety.check_heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_charge_pv_safety_heartbeat_blocked() -> None:
    """Safety heartbeat block prevents charge_pv."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    safety = _make_safety(heartbeat_ok=False)
    coord = _make_coord(safety=safety)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_charge_pv(state)

    assert coord._daily_safety_blocks == 1


@pytest.mark.asyncio
async def test_cmd_charge_pv_safety_rate_blocked() -> None:
    """Safety rate limit block increments daily blocks counter."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    safety = _make_safety(rate_ok=False)
    coord = _make_coord(safety=safety)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_charge_pv(state)

    assert coord._daily_safety_blocks == 1


@pytest.mark.asyncio
async def test_cmd_charge_pv_with_adapter_happy_path() -> None:
    """charge_pv with adapter → set_ems_mode called."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=60.0)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_charge_pv(state)

    adp.set_ems_mode.assert_called_with("charge_pv")
    assert coord._last_command == BatteryCommand.CHARGE_PV
    coord._async_save_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_charge_pv_full_battery_sets_standby() -> None:
    """Battery at 100% SoC gets battery_standby instead of charge_pv."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=100.0)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_charge_pv(state)

    adp.set_ems_mode.assert_called_with("battery_standby")


# ═══════════════════════════════════════════════════════════════════════════════
# 9. cmd_standby
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_standby_idempotent_without_force() -> None:
    """Returns early if already STANDBY and force=False."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(last_command_name="STANDBY")
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_standby(state, force=False)

    coord.safety.check_heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_standby_force_bypasses_safety() -> None:
    """force=True skips safety gate and always proceeds."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    # Even if already STANDBY, force=True re-executes
    adp = _make_adapter()
    coord = _make_coord(last_command_name="STANDBY", inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_standby(state, force=True)

    adp.set_ems_mode.assert_called_with("battery_standby")


@pytest.mark.asyncio
async def test_cmd_standby_happy_path() -> None:
    """Standby with adapter → battery_standby + STANDBY command."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter()
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_standby(state)

    adp.set_ems_mode.assert_called_with("battery_standby")
    assert coord._last_command == BatteryCommand.STANDBY
    coord._async_save_runtime.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 10. cmd_discharge
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_discharge_k1_skip_redundant() -> None:
    """K1: skip discharge when already at similar wattage (±100W)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord = _make_coord(last_command_name="DISCHARGE", last_discharge_w=2000)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    # 2050W is within 100W of 2000W → skip
    await engine.cmd_discharge(state, 2050)

    coord.safety.check_heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_discharge_safety_heartbeat_blocked() -> None:
    """Safety heartbeat block prevents discharge."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    safety = _make_safety(heartbeat_ok=False)
    coord = _make_coord(safety=safety)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_discharge(state, 2000)

    assert coord._daily_safety_blocks == 1


@pytest.mark.asyncio
async def test_cmd_discharge_safety_discharge_check_blocked() -> None:
    """SafetyGuard discharge check block increments daily blocks."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    safety = _make_safety(discharge_ok=False)
    coord = _make_coord(safety=safety)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_discharge(state, 2000)

    assert coord._daily_safety_blocks == 1


@pytest.mark.asyncio
async def test_cmd_discharge_no_stored_energy_returns_early() -> None:
    """Returns early when no stored energy (both SoC effectively 0)."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=0.0)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state(battery_soc_1=0.0, battery_soc_2=0.0)
    await engine.cmd_discharge(state, 2000)

    adp.set_ems_mode.assert_not_called()
    assert coord._last_command == BatteryCommand.IDLE


@pytest.mark.asyncio
async def test_cmd_discharge_happy_path() -> None:
    """Happy path: discharge sets EMS mode and updates state."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=60.0)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_discharge(state, 2000)

    adp.set_ems_mode.assert_called_with("auto")
    adp.set_discharge_limit.assert_called_with(0)
    assert coord._last_command == BatteryCommand.DISCHARGE
    assert coord._last_discharge_w == 2000


@pytest.mark.asyncio
async def test_cmd_discharge_limit_fail_rolls_back() -> None:
    """K2: discharge limit failure rolls back to standby."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=60.0)
    adp.set_discharge_limit = AsyncMock(return_value=False)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_discharge(state, 2000)

    # Rolled back to standby
    adp.set_ems_mode.assert_any_call("battery_standby")
    assert coord._last_command == BatteryCommand.IDLE


# ═══════════════════════════════════════════════════════════════════════════════
# 11. cmd_grid_charge
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cmd_grid_charge_safety_heartbeat_blocked() -> None:
    """Safety heartbeat block prevents grid_charge (unless already charging)."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    safety = _make_safety(heartbeat_ok=False)
    coord = _make_coord(safety=safety)
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_grid_charge(state)

    assert coord._daily_safety_blocks == 1


@pytest.mark.asyncio
async def test_cmd_grid_charge_happy_path() -> None:
    """Grid charge enables fast_charging on adapters."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    adp = _make_adapter(soc=60.0)
    coord = _make_coord(inverter_adapters=[adp])
    engine = ExecutionEngine(coord)
    state = _make_carmabox_state()
    await engine.cmd_grid_charge(state)

    adp.set_ems_mode.assert_called_with("charge_pv")
    adp.set_fast_charging.assert_called_once()
    assert coord._last_command == BatteryCommand.CHARGE_PV


# ═══════════════════════════════════════════════════════════════════════════════
# 12. execute_v2 — smoke test (complex method, integration-style)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_v2_smoke_no_plan() -> None:
    """execute_v2 runs without crashing when no plan and minimal state."""
    from custom_components.carmabox.core.execution_engine import ExecutionEngine
    from custom_components.carmabox.optimizer.models import CarmaboxState

    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.hass.states.get = MagicMock(return_value=None)
    coord._cfg = {"ev_phase_count": 3}
    coord._last_battery_action = "charge_pv"
    coord._ev_enabled = False
    coord._ev_current_amps = 0
    coord._night_ev_active = False
    coord._fast_charge_authorized = False
    coord._price_discharge_active = False
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord.plan = []
    coord.target_kw = 2.0
    coord._grid_guard_result = None
    coord._grid_guard = MagicMock()
    coord._grid_guard.headroom_kw = 1.0
    coord._read_float = MagicMock(return_value=50.0)
    coord._record_decision = AsyncMock()
    coord._check_discharge_drift = MagicMock()
    coord._build_surplus_consumers = MagicMock(return_value=[])
    coord._async_save_runtime = AsyncMock()
    coord.weather_adapter = None
    coord._pv_allocation = {}

    # Provide _surplus_hysteresis as pre-set to avoid __new__ issues
    from custom_components.carmabox.core.surplus_chain import HysteresisState

    coord._surplus_hysteresis = HysteresisState()

    engine = ExecutionEngine(coord)

    state = MagicMock(spec=CarmaboxState)
    state.grid_power_w = 500.0
    state.pv_power_w = 0.0
    state.battery_soc_1 = 60.0
    state.battery_soc_2 = 60.0
    state.battery_power_1 = 0.0
    state.battery_power_2 = 0.0
    state.ev_power_w = 0.0
    state.ev_soc = -1.0
    state.total_battery_soc = 60.0
    state.battery_min_cell_temp_1 = 20.0
    state.battery_min_cell_temp_2 = 20.0

    with patch("custom_components.carmabox.core.execution_engine.datetime") as mock_dt:
        mock_dt.now.return_value = MagicMock(hour=14, weekday=MagicMock(return_value=1))
        await engine.execute_v2(state)

    # Core: record_decision was called
    coord._record_decision.assert_awaited_once()
