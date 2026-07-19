"""AC-W1..W5: W1 current watchdog + W2 forbid-discharge-zero safety rules.

TC-W1-TRIP:         L1 > trip_a → emergency stop, both banks standby, iPhone + PN notify
TC-W1-BELOW_TRIP:   all phases < trip_a → no emergency
TC-W1-CLEARS:       emergency + sustained below reset_a → clear after 60s
TC-W1-NO_DOUBLE:    emergency already active → no double-notify on second over-current tick
TC-W1-RESET_BLOCKS: emergency active, peak between reset and trip → sustain timer resets
TC-W2-DISCHARGE_0:  discharge_battery + target=0 → clamped to standby (IT-2102 override)
TC-W2-CHARGE_0:     charge_battery + target=0 → clamped to standby
TC-W2-NON_ZERO:     discharge + target=500W → discharge_battery (not clamped)
TC-W4-INCIDENT:     simulate 2026-05-22 incident: direction=discharge, bias-excluded bank=0 → W2 prevents over-current
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
    CURRENT_RESET_DEFAULT_A,
    CURRENT_RESET_SUSTAIN_S,
    CURRENT_TRIP_DEFAULT_A,
    ENTITY_BAT_ALLOW_HW_AUTONOMY,
    ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE,
    ENTITY_BAT_CURRENT_RESET_A,
    ENTITY_BAT_CURRENT_TRIP_A,
    ENTITY_BAT_ECO_MODE_DISABLE_SOC,
    ENTITY_GOODWE_BATTERY_POWER,
    ENTITY_GOODWE_DEVICE_ID,
    ENTITY_GOODWE_DOD_HOLDING,
    ENTITY_GOODWE_ECO_MODE_SOC,
    ENTITY_GOODWE_FAST_CHARGING,
    ENTITY_HOUSE_L1_CURRENT_A,
    ENTITY_HOUSE_L2_CURRENT_A,
    ENTITY_HOUSE_L3_CURRENT_A,
    ENTITY_HW_MISMATCH_THRESHOLD_W,
    GOODWE_EMS_MODE_DISCHARGE,
    GOODWE_EMS_MODE_STANDBY,
    GOODWE_MODE_BATTERY_STANDBY,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator
from custom_components.bat_balancer.models import BankConfig, BatBalancerState
from custom_components.bat_balancer.sign_state_machine import SignStateMachine


def _make_states(**extra: str) -> MagicMock:
    defaults: dict[str, str] = {
        ENTITY_BAT_ALLOW_HW_AUTONOMY: "off",
        ENTITY_BAT_ECO_MODE_DISABLE_SOC: "0",
        ENTITY_HW_MISMATCH_THRESHOLD_W: "200",
        ENTITY_BAT_CURRENT_TRIP_A: str(CURRENT_TRIP_DEFAULT_A),
        ENTITY_BAT_CURRENT_RESET_A: str(CURRENT_RESET_DEFAULT_A),
        ENTITY_HOUSE_L1_CURRENT_A: "5",
        ENTITY_HOUSE_L2_CURRENT_A: "5",
        ENTITY_HOUSE_L3_CURRENT_A: "5",
    }
    for bid in BANKS:
        defaults[ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id=bid)] = "0"
        defaults[ENTITY_GOODWE_DOD_HOLDING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_FAST_CHARGING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_DEVICE_ID.format(bank_id=bid)] = "abc123"
        defaults[ENTITY_GOODWE_BATTERY_POWER.format(bank_id=bid)] = "0"
    defaults.update(extra)

    def _get(entity_id: str):
        val = defaults.get(entity_id)
        if val is None:
            return None
        s = MagicMock()
        s.state = val
        return s

    mock = MagicMock()
    mock.get = _get
    return mock


def _make_coord(
    states_mock: MagicMock | None = None,
    emergency: bool = False,
    below_reset_since: float | None = None,
) -> BatBalancerCoordinator:
    hass = MagicMock()
    hass.data = {}
    hass.services.async_call = AsyncMock()
    hass.states = states_mock or _make_states()

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
    coord._bank_was_online = dict.fromkeys(BANKS, True)
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._hw_autonomy_initialized = False
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._current_emergency_active = emergency
    coord._current_below_reset_since = below_reset_since
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)
    return coord


def _notify_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "notify"
    ]


def _pn_create_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "persistent_notification" and call.args[1] == "create"
    ]


def _bool_calls(coord: BatBalancerCoordinator, service: str) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "input_boolean" and call.args[1] == service
    ]


def _select_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "select" and call.args[1] == "select_option"
    ]


# ─────────────────────────────────────────────────────────────────────────────
# TC-W1-TRIP
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W1_TRIP_activates_emergency():
    """AC-W1: L1 > trip_a (14A) → emergency=True, input_boolean.turn_on, iPhone, PN."""
    states = _make_states(**{ENTITY_HOUSE_L1_CURRENT_A: "15"})
    coord = _make_coord(states)

    result = await coord._check_current_safety()

    assert result is True
    assert coord._current_emergency_active is True

    bool_on = _bool_calls(coord, "turn_on")
    assert any(c["entity_id"] == ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE for c in bool_on)

    notifies = _notify_calls(coord)
    assert len(notifies) == 1
    assert "15" in notifies[0]["message"] or "15.0" in notifies[0]["message"]

    pns = _pn_create_calls(coord)
    assert len(pns) == 1
    assert pns[0]["notification_id"] == "bat_balancer_current_emergency"


@pytest.mark.asyncio
async def test_TC_W1_TRIP_uses_max_phase():
    """Emergency triggers on the highest phase, not sum."""
    states = _make_states(
        **{
            ENTITY_HOUSE_L1_CURRENT_A: "5",
            ENTITY_HOUSE_L2_CURRENT_A: "16",
            ENTITY_HOUSE_L3_CURRENT_A: "3",
        }
    )
    coord = _make_coord(states)

    result = await coord._check_current_safety()
    assert result is True
    assert coord._current_emergency_active is True


@pytest.mark.asyncio
async def test_TC_W1_TRIP_custom_threshold():
    """Trip threshold read from helper — 10A trip, L2=11A → emergency."""
    states = _make_states(
        **{
            ENTITY_BAT_CURRENT_TRIP_A: "10",
            ENTITY_HOUSE_L2_CURRENT_A: "11",
        }
    )
    coord = _make_coord(states)

    result = await coord._check_current_safety()
    assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-W1-BELOW_TRIP
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W1_BELOW_TRIP_no_emergency():
    """All phases 5A < 14A → no emergency, no notifications."""
    coord = _make_coord()  # defaults all 5A

    result = await coord._check_current_safety()

    assert result is False
    assert coord._current_emergency_active is False
    assert _notify_calls(coord) == []
    assert _pn_create_calls(coord) == []


# ─────────────────────────────────────────────────────────────────────────────
# TC-W1-NO_DOUBLE: emergency already active → no re-notify
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W1_NO_DOUBLE_fire():
    """Second over-current tick with emergency already active → no additional notifications."""
    states = _make_states(**{ENTITY_HOUSE_L1_CURRENT_A: "16"})
    coord = _make_coord(states, emergency=True)

    await coord._check_current_safety()

    # No new notifications (already active)
    assert _notify_calls(coord) == []
    assert _pn_create_calls(coord) == []
    assert coord._current_emergency_active is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-W1-CLEARS: sustained below reset → clear after 60s
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W1_CLEARS_after_sustain():
    """Emergency + peak < reset_a sustained 60s → cleared."""
    # Simulate: already below reset for 61s
    states = _make_states(
        **{
            ENTITY_HOUSE_L1_CURRENT_A: "8",  # 8A < 12A reset
            ENTITY_HOUSE_L2_CURRENT_A: "8",
            ENTITY_HOUSE_L3_CURRENT_A: "8",
        }
    )
    past = time.monotonic() - (CURRENT_RESET_SUSTAIN_S + 1)
    coord = _make_coord(states, emergency=True, below_reset_since=past)

    result = await coord._check_current_safety()

    assert result is False
    assert coord._current_emergency_active is False

    bool_off = _bool_calls(coord, "turn_off")
    assert any(c["entity_id"] == ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE for c in bool_off)


@pytest.mark.asyncio
async def test_TC_W1_CLEARS_not_before_sustain():
    """Emergency + peak < reset but only 10s → still active."""
    states = _make_states(
        **{
            ENTITY_HOUSE_L1_CURRENT_A: "8",
        }
    )
    recent = time.monotonic() - 10
    coord = _make_coord(states, emergency=True, below_reset_since=recent)

    result = await coord._check_current_safety()

    assert result is True
    assert coord._current_emergency_active is True


# ─────────────────────────────────────────────────────────────────────────────
# TC-W1-RESET_BLOCKS: peak between reset_a and trip_a resets sustain timer
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W1_RESET_BLOCKS_sustain_timer_reset():
    """Emergency + peak 13A (between reset=12 and trip=14) → sustain timer resets to None."""
    states = _make_states(
        **{
            ENTITY_HOUSE_L1_CURRENT_A: "13",  # > reset(12) but < trip(14)
        }
    )
    past = time.monotonic() - 30
    coord = _make_coord(states, emergency=True, below_reset_since=past)

    result = await coord._check_current_safety()

    assert result is True
    assert coord._current_emergency_active is True
    assert coord._current_below_reset_since is None


# ─────────────────────────────────────────────────────────────────────────────
# TC-W2: forbid discharge/charge + 0W
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W2_DISCHARGE_0_clamped_to_standby():
    """AC-W3: global discharge direction + per-bank target=0 → battery_standby (not discharge+0)."""
    calls: list = []
    hass = MagicMock()

    async def _call(domain, service, data, *, blocking=False):
        calls.append((domain, service, data))

    hass.services.async_call = _call

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)

    # global direction=+2000 (discharge), but per-bank target=0 (equalization bias-excluded)
    await coord._write_power_limit("forrad", 0.0, direction_w=2000.0)

    ems_calls = [
        dat["option"]
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    op_calls = [
        dat["option"]
        for d, s, dat in calls
        if s == "select_option" and "operation_mode" in dat.get("entity_id", "")
    ]

    assert ems_calls == [GOODWE_EMS_MODE_STANDBY], f"W2: must be battery_standby, got {ems_calls}"
    assert op_calls == [
        GOODWE_MODE_BATTERY_STANDBY
    ], f"Q1 OPT-A: op_mode=battery_standby must be written at W2 clamp, got {op_calls}"


@pytest.mark.asyncio
async def test_TC_W2_CHARGE_0_clamped_to_standby():
    """AC-W3: global charge direction + per-bank target=0 → battery_standby."""
    calls: list = []
    hass = MagicMock()

    async def _call(domain, service, data, *, blocking=False):
        calls.append((domain, service, data))

    hass.services.async_call = _call

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)

    await coord._write_power_limit("forrad", 0.0, direction_w=-2000.0)

    ems_calls = [
        dat["option"]
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    assert ems_calls == [
        GOODWE_EMS_MODE_STANDBY
    ], f"W2: charge+0W must be clamped to battery_standby, got {ems_calls}"


@pytest.mark.asyncio
async def test_TC_W2_NON_ZERO_not_clamped():
    """Non-zero discharge target → discharge_battery (W2 guard does not fire)."""
    calls: list = []
    hass = MagicMock()

    async def _call(domain, service, data, *, blocking=False):
        calls.append((domain, service, data))

    hass.services.async_call = _call

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)

    await coord._write_power_limit("kontor", 500.0, direction_w=500.0)

    ems_calls = [
        dat["option"]
        for d, s, dat in calls
        if s == "select_option" and "ems_mode" in dat.get("entity_id", "")
    ]
    assert ems_calls == [GOODWE_EMS_MODE_DISCHARGE], "500W discharge must not be clamped"


# ─────────────────────────────────────────────────────────────────────────────
# TC-W4-INCIDENT: regression — 2026-05-22 incident scenario
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_W4_INCIDENT_scenario_prevented():
    """AC-W4: simulate incident: global +1500W discharge, forrad bias-excluded (target=0).

    Before W2: forrad got discharge_battery+0W → GoodWe uncapped → 24A + fuse trip.
    After W2: forrad must get battery_standby+0W.
    """
    calls: list = []
    hass = MagicMock()

    async def _call(domain, service, data, *, blocking=False):
        calls.append((domain, service, data))

    hass.services.async_call = _call

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._last_goodwe_ems_modes = dict.fromkeys(BANKS)
    coord._last_goodwe_modes = dict.fromkeys(BANKS)
    coord._last_written_ems_modes = dict.fromkeys(BANKS)
    coord._hw_mismatch_ticks = dict.fromkeys(BANKS, 0)
    coord._full_bias_active = False
    coord._last_ems_limit_w = dict.fromkeys(BANKS, 0.0)

    # Kontor gets full 1500W (active bank)
    await coord._write_power_limit("kontor", 1500.0, direction_w=1500.0)
    # Forrad bias-excluded: gets 0W but global direction is still +1500 (discharge)
    await coord._write_power_limit("forrad", 0.0, direction_w=1500.0)

    def ems_for(bank: str) -> list:
        return [
            dat["option"]
            for d, s, dat in calls
            if s == "select_option"
            and "ems_mode" in dat.get("entity_id", "")
            and bank in dat.get("entity_id", "")
        ]

    assert ems_for("kontor") == [GOODWE_EMS_MODE_DISCHARGE], "kontor must discharge"
    assert ems_for("forrad") == [
        GOODWE_EMS_MODE_STANDBY
    ], "W2 REGRESSION: forrad must be battery_standby, not discharge_battery+0W"

    forrad_limits = [
        dat["value"]
        for d, s, dat in calls
        if s == "set_value" and "forrad" in dat.get("entity_id", "")
    ]
    assert forrad_limits == [0.0]
