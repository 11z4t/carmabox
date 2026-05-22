"""B23 sole-writer: _write_power_limit encodes sign via ems_mode + op_mode per direction.

TC1: target_w=-3000 (charge)    → ems_mode=charge_battery,    op_mode=peak_shaving,    limit=3000
TC2: target_w=+1500 (discharge) → ems_mode=discharge_battery, op_mode=peak_shaving,    limit=1500
TC3: target_w=0 (idle)          → ems_mode=battery_standby,   op_mode=battery_standby, limit=0
INV-19 (revised): op_mode written idempotently — once per transition, not every tick.
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    BANKS,
    ENTITY_GOODWE_EMS_MODE,
    ENTITY_GOODWE_OPERATION_MODE,
    ENTITY_GOODWE_POWER_LIMIT,
    GOODWE_EMS_MODE_CHARGE,
    GOODWE_EMS_MODE_DISCHARGE,
    GOODWE_EMS_MODE_STANDBY,
    GOODWE_MODE_BATTERY_STANDBY,
    GOODWE_MODE_PEAK_SHAVING,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator


def _make_coord() -> BatBalancerCoordinator:
    from custom_components.bat_balancer.models import BankConfig, BatBalancerState
    from custom_components.bat_balancer.sign_state_machine import SignStateMachine

    hass = MagicMock()
    hass.data = {}
    hass.services.async_call = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test"

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._entry = entry
    coord._bank_configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
    coord._state = BatBalancerState()
    coord._last_brain_write_ts = time.monotonic()
    coord._bank_was_online = {bid: True for bid in BANKS}
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._last_written_ems_modes = {bid: None for bid in BANKS}
    coord._hw_mismatch_ticks = {bid: 0 for bid in BANKS}
    coord._full_bias_active = False
    coord._last_ems_limit_w = {bid: 0.0 for bid in BANKS}
    return coord


def _calls_for(hass_mock: MagicMock, domain: str, service: str, entity_id: str):
    """Extract all async_call invocations matching domain/service/entity_id."""
    return [
        c
        for c in hass_mock.services.async_call.call_args_list
        if c.args[0] == domain and c.args[1] == service and c.args[2].get("entity_id") == entity_id
    ]


# ---------------------------------------------------------------------------
# TC1: charge — ems_mode=charge_battery, op_mode=peak_shaving, limit=3000
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_writes_correct_ems_mode() -> None:
    """TC1: target_w=-3000 → ems_mode=charge_battery, op_mode=peak_shaving, limit=3000."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", -3000.0)

    ems_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_EMS_MODE.format(bank_id="kontor")
    )
    assert len(ems_calls) == 1
    assert ems_calls[0].args[2]["option"] == GOODWE_EMS_MODE_CHARGE

    op_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_OPERATION_MODE.format(bank_id="kontor")
    )
    assert len(op_calls) == 1
    assert op_calls[0].args[2]["option"] == GOODWE_MODE_PEAK_SHAVING

    limit_calls = _calls_for(
        coord.hass, "number", "set_value", ENTITY_GOODWE_POWER_LIMIT.format(bank_id="kontor")
    )
    assert len(limit_calls) == 1
    assert limit_calls[0].args[2]["value"] == 3000.0


# ---------------------------------------------------------------------------
# TC2: discharge — ems_mode=discharge_battery, op_mode=peak_shaving, limit=1500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discharge_writes_correct_ems_mode() -> None:
    """TC2: target_w=+1500 → ems_mode=discharge_battery, op_mode=peak_shaving, limit=1500."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", 1500.0)

    ems_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_EMS_MODE.format(bank_id="kontor")
    )
    assert len(ems_calls) == 1
    assert ems_calls[0].args[2]["option"] == GOODWE_EMS_MODE_DISCHARGE

    op_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_OPERATION_MODE.format(bank_id="kontor")
    )
    assert len(op_calls) == 1
    assert op_calls[0].args[2]["option"] == GOODWE_MODE_PEAK_SHAVING

    limit_calls = _calls_for(
        coord.hass, "number", "set_value", ENTITY_GOODWE_POWER_LIMIT.format(bank_id="kontor")
    )
    assert len(limit_calls) == 1
    assert limit_calls[0].args[2]["value"] == 1500.0


# ---------------------------------------------------------------------------
# TC3: idle — ems_mode=battery_standby, op_mode=battery_standby, limit=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_writes_standby_ems_mode() -> None:
    """TC3: target_w=0 → ems_mode=battery_standby, op_mode=battery_standby (Q1 OPT-A), limit=0."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", 0.0)

    ems_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_EMS_MODE.format(bank_id="kontor")
    )
    assert len(ems_calls) == 1
    assert ems_calls[0].args[2]["option"] == GOODWE_EMS_MODE_STANDBY

    op_calls = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_OPERATION_MODE.format(bank_id="kontor")
    )
    assert len(op_calls) == 1
    assert op_calls[0].args[2]["option"] == GOODWE_MODE_BATTERY_STANDBY

    limit_calls = _calls_for(
        coord.hass, "number", "set_value", ENTITY_GOODWE_POWER_LIMIT.format(bank_id="kontor")
    )
    assert len(limit_calls) == 1
    assert limit_calls[0].args[2]["value"] == 0.0


# ---------------------------------------------------------------------------
# TC-IDLE-FORCES-STANDBY: idle after active cycle must always write standby
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tc_idle_forces_standby_bypasses_idempotent_cache() -> None:
    """TC-IDLE-FORCES-STANDBY: target=0 always writes ems_mode=battery_standby every tick.
    op_mode=battery_standby written ONCE on transition (INV-19: idempotent cache, no repeated writes).

    Regression: GoodWe retains charge_battery + draws ~8W parasitic even at limit=0W.
    Fix: idle resets ems_mode cache so battery_standby is re-sent every tick.
    INV-19 protection: _last_goodwe_modes NOT reset — op_mode only on transition.
    """
    coord = _make_coord()

    # Simulate: forrad was actively charging (ems_mode=charge_battery, op_mode=peak_shaving cached)
    await coord._write_power_limit("forrad", -3000.0)
    coord.hass.services.async_call.reset_mock()

    # A3 kicks in: forrad gets 0W. First tick: cache flips → both written (standby transition).
    await coord._write_power_limit("forrad", 0.0)
    ems_first = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_EMS_MODE.format(bank_id="forrad")
    )
    op_first = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_OPERATION_MODE.format(bank_id="forrad")
    )
    assert len(ems_first) == 1
    assert ems_first[0].args[2]["option"] == GOODWE_EMS_MODE_STANDBY
    assert len(op_first) == 1
    assert op_first[0].args[2]["option"] == GOODWE_MODE_BATTERY_STANDBY
    coord.hass.services.async_call.reset_mock()

    # Second tick: ems_mode cache reset → battery_standby written again (prevents HW residual).
    # op_mode cache NOT reset → battery_standby NOT re-written (INV-19 idempotency preserved).
    await coord._write_power_limit("forrad", 0.0)
    ems_second = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_EMS_MODE.format(bank_id="forrad")
    )
    op_second = _calls_for(
        coord.hass, "select", "select_option", ENTITY_GOODWE_OPERATION_MODE.format(bank_id="forrad")
    )
    assert len(ems_second) == 1, (
        "TC-IDLE-FORCES-STANDBY: second idle tick must write battery_standby "
        "(ems_mode cache reset to prevent GoodWe hardware residual)"
    )
    assert ems_second[0].args[2]["option"] == GOODWE_EMS_MODE_STANDBY
    assert len(op_second) == 0, (
        "INV-19: op_mode must NOT be re-written on second idle tick "
        "(idempotent cache — EPS glitch protection)"
    )
