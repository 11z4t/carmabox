"""B23 + INV-19 (revised): op_mode + ems_mode idempotency scenarios.

B23: op_mode=peak_shaving required for GoodWe EMS to engage (non-zero targets).
INV-19 (revised): op_mode written idempotently — once per transition, not every tick.
EPS glitch (2026-05-02) was caused by repeated op_mode writes; idempotent cache prevents that.
Sign encoded via ems_mode (charge_battery / discharge_battery / battery_standby).
Write order: ems_mode → op_mode → ems_power_limit.
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    BANKS,
    GOODWE_EMS_MODE_CHARGE,
    GOODWE_EMS_MODE_DISCHARGE,
    GOODWE_EMS_MODE_STANDBY,
    GOODWE_MODE_BATTERY_STANDBY,
    GOODWE_MODE_PEAK_SHAVING,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator


def _make_coordinator(calls: list) -> BatBalancerCoordinator:
    hass = MagicMock()

    async def mock_async_call(domain, service, data, *, blocking=False):
        calls.append((domain, service, data))

    hass.services.async_call = mock_async_call

    from custom_components.bat_balancer.models import BankConfig, BatBalancerState
    from custom_components.bat_balancer.sign_state_machine import SignStateMachine

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._bank_configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
    coord._state = BatBalancerState()
    coord._last_brain_write_ts = time.monotonic()
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)
    return coord


# ---------------------------------------------------------------------------
# B23: op_mode=peak_shaving written on non-zero target (required for EMS to engage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b23_op_mode_peak_shaving_on_discharge() -> None:
    """B23: discharge target → op_mode=peak_shaving written (required for GoodWe EMS to engage)."""
    calls = []
    coord = _make_coordinator(calls)
    await coord._write_power_limit("kontor", 3000.0)

    op_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "goodwe_inverter_operation_mode" in dat.get("entity_id", "")
    ]
    assert (
        len(op_calls) == 1
    ), f"B23: op_mode=peak_shaving must be written for EMS to engage, got {op_calls}"
    assert op_calls[0][2]["option"] == GOODWE_MODE_PEAK_SHAVING

    ems_mode_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    assert len(ems_mode_calls) == 1
    assert ems_mode_calls[0][2]["option"] == GOODWE_EMS_MODE_DISCHARGE


# ---------------------------------------------------------------------------
# Idempotency: same direction → ems_mode written only once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ems_mode_idempotent_same_direction() -> None:
    """Same sign two ticks → ems_mode written once, EMS limit written both ticks."""
    calls = []
    coord = _make_coordinator(calls)

    await coord._write_power_limit("kontor", 3000.0)
    await coord._write_power_limit("kontor", 2000.0)

    ems_mode_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    limit_calls = [(d, s, dat) for d, s, dat in calls if s == "set_value"]

    assert len(ems_mode_calls) == 1  # idempotent
    assert len(limit_calls) == 2
    assert limit_calls[0][2]["value"] == 3000.0
    assert limit_calls[1][2]["value"] == 2000.0


# ---------------------------------------------------------------------------
# Direction change: ems_mode updates on sign flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ems_mode_updates_on_direction_change() -> None:
    """charge → discharge → charge: ems_mode written 3 times.
    INV-19 (revised): op_mode written ONCE (peak_shaving on first non-zero, then idempotent).
    """
    calls = []
    coord = _make_coordinator(calls)

    await coord._write_power_limit("kontor", -1000.0)  # charge
    await coord._write_power_limit("kontor", 500.0)  # discharge
    await coord._write_power_limit("kontor", -800.0)  # charge again

    ems_mode_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    op_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "operation_mode" in dat.get("entity_id", "")
    ]

    assert len(ems_mode_calls) == 3
    assert ems_mode_calls[0][2]["option"] == GOODWE_EMS_MODE_CHARGE
    assert ems_mode_calls[1][2]["option"] == GOODWE_EMS_MODE_DISCHARGE
    assert ems_mode_calls[2][2]["option"] == GOODWE_EMS_MODE_CHARGE

    assert (
        len(op_calls) == 1
    ), f"INV-19: op_mode must be written ONCE (peak_shaving, then idempotent), got {op_calls}"
    assert op_calls[0][2]["option"] == GOODWE_MODE_PEAK_SHAVING


# ---------------------------------------------------------------------------
# Write order: ems_mode before op_mode before EMS limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_order_ems_mode_then_op_mode_then_limit() -> None:
    """Write order: ems_mode → op_mode → ems_power_limit (B23 + INV-19 revised)."""
    call_order = []

    hass = MagicMock()

    async def mock_call(domain, service, data, *, blocking=False):
        call_order.append((service, data.get("entity_id", "")))

    hass.services.async_call = mock_call

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)

    await coord._write_power_limit("kontor", 4000.0)

    assert len(call_order) == 3, f"Expected 3 calls (ems_mode + op_mode + limit), got {call_order}"
    services = [svc for svc, _ in call_order]
    assert services[0] == "select_option"  # ems_mode
    assert services[1] == "select_option"  # op_mode
    assert services[2] == "set_value"  # ems_power_limit

    entity_ids = [eid for _, eid in call_order]
    assert "ems_mode" in entity_ids[0]
    assert "operation_mode" in entity_ids[1]
    assert "ems_power_limit" in entity_ids[2]


# ---------------------------------------------------------------------------
# EMS always written even if both modes unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ems_written_even_if_ems_mode_unchanged() -> None:
    """EMS limit written every tick even when both ems_mode and op_mode are idempotent."""
    calls = []
    coord = _make_coordinator(calls)
    coord._last_goodwe_ems_modes["kontor"] = GOODWE_EMS_MODE_DISCHARGE
    coord._last_goodwe_modes["kontor"] = GOODWE_MODE_PEAK_SHAVING  # already written

    await coord._write_power_limit("kontor", 5000.0)

    select_calls = [(d, s, dat) for d, s, dat in calls if s == "select_option"]
    limit_calls = [(d, s, dat) for d, s, dat in calls if s == "set_value"]

    assert len(select_calls) == 0  # both ems_mode and op_mode idempotent
    assert len(limit_calls) == 1
    assert limit_calls[0][2]["value"] == 5000.0


# ---------------------------------------------------------------------------
# Charge — correct ems_mode + magnitude
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_sets_correct_ems_mode() -> None:
    """Negative target → charge_battery ems_mode, correct magnitude. INV-19: no op_mode write."""
    calls = []
    coord = _make_coordinator(calls)
    await coord._write_power_limit("forrad", -2000.0)

    ems_mode_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    limit_calls = [(d, s, dat) for d, s, dat in calls if s == "set_value"]

    assert ems_mode_calls[0][2]["option"] == GOODWE_EMS_MODE_CHARGE
    assert limit_calls[0][2]["value"] == 2000.0


# ---------------------------------------------------------------------------
# Idle — battery_standby ems_mode only (INV-19: op_mode never written)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_sets_standby_ems_mode() -> None:
    """Zero target → ems=battery_standby, op_mode=battery_standby (Q1 OPT-A)."""
    calls = []
    coord = _make_coordinator(calls)
    await coord._write_power_limit("kontor", 0.0)

    ems_mode_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    op_calls = [
        (d, s, dat)
        for d, s, dat in calls
        if s == "select_option" and "operation_mode" in dat.get("entity_id", "")
    ]

    assert ems_mode_calls[0][2]["option"] == GOODWE_EMS_MODE_STANDBY
    assert (
        len(op_calls) == 1
    ), f"Q1 OPT-A: op_mode=battery_standby must be written at idle, got {op_calls}"
    assert op_calls[0][2]["option"] == GOODWE_MODE_BATTERY_STANDBY


# ---------------------------------------------------------------------------
# B23-EQ: Both banks same ems_mode even when per-bank target=0 (equalization bias)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_banks_same_ems_mode_when_bias_zeroes_one_bank() -> None:
    """W2-SAFETY: global charge target, equalization gives 0W to förråd.

    W2 rule: per-bank 0W target → battery_standby regardless of global direction.
    Prevents charge_battery+0W (GoodWe interprets as uncapped → over-current risk).
    """
    calls = []
    coord = _make_coordinator(calls)

    await coord._write_power_limit("kontor", -2000.0, direction_w=-2000.0)
    await coord._write_power_limit("forrad", 0.0, direction_w=-2000.0)

    def ems_calls_for(bank: str) -> list:
        return [
            dat["option"]
            for d, s, dat in calls
            if s == "select_option"
            and "ems_mode" in dat.get("entity_id", "")
            and bank in dat.get("entity_id", "")
        ]

    assert ems_calls_for("kontor") == [GOODWE_EMS_MODE_CHARGE], "kontor must be charge_battery"
    assert ems_calls_for("forrad") == [
        GOODWE_EMS_MODE_STANDBY
    ], "W2: forrad must be battery_standby when per-bank target=0W (not charge_battery+0W)"

    forrad_limits = [
        dat["value"]
        for d, s, dat in calls
        if s == "set_value" and "forrad" in dat.get("entity_id", "")
    ]
    assert forrad_limits == [0.0], f"forrad ems_limit must be 0W, got {forrad_limits}"


@pytest.mark.asyncio
async def test_both_banks_same_ems_mode_discharge_bias() -> None:
    """W2-SAFETY: global discharge, equalization gives 0W to low-SoC bank.

    W2 rule: per-bank 0W → battery_standby (not discharge_battery+0W = fuse trip risk).
    """
    calls = []
    coord = _make_coordinator(calls)

    await coord._write_power_limit("kontor", 2000.0, direction_w=2000.0)
    await coord._write_power_limit("forrad", 0.0, direction_w=2000.0)

    def ems_calls_for(bank: str) -> list:
        return [
            dat["option"]
            for d, s, dat in calls
            if s == "select_option"
            and "ems_mode" in dat.get("entity_id", "")
            and bank in dat.get("entity_id", "")
        ]

    assert ems_calls_for("kontor") == [GOODWE_EMS_MODE_DISCHARGE]
    assert ems_calls_for("forrad") == [
        GOODWE_EMS_MODE_STANDBY
    ], "W2: forrad must be battery_standby when per-bank target=0W (not discharge_battery+0W)"
