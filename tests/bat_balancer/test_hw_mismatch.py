"""AC-HW-MISMATCH-1..5: HW-EMS mismatch watchdog (Q4 follow-up from RCA 2026-05-22).

TC-MISMATCH-STANDBY:   ems=standby, hw_power=+500W → alarm after N ticks
TC-MISMATCH-DISCHARGE: ems=discharge, hw_power=-500W → alarm (charging when should discharge)
TC-MISMATCH-CHARGE:    ems=charge, hw_power=+500W → alarm (discharging when should charge)
TC-MISMATCH-CLEARS:    mismatch resolves → dismiss notification
TC-MISMATCH-BELOW_THRESHOLD: hw_power=50W with standby → no alarm (below 200W default)
TC-MISMATCH-NO_EMS_WRITTEN: last_written=None → no alarm (nothing written yet)
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
    ENTITY_BAT_ALLOW_HW_AUTONOMY,
    ENTITY_BAT_ECO_MODE_DISABLE_SOC,
    ENTITY_GOODWE_BATTERY_POWER,
    ENTITY_GOODWE_DEVICE_ID,
    ENTITY_GOODWE_DOD_HOLDING,
    ENTITY_GOODWE_ECO_MODE_SOC,
    ENTITY_GOODWE_FAST_CHARGING,
    ENTITY_HW_MISMATCH_THRESHOLD_W,
    GOODWE_EMS_MODE_CHARGE,
    GOODWE_EMS_MODE_DISCHARGE,
    GOODWE_EMS_MODE_STANDBY,
    HW_MISMATCH_TICKS_DEFAULT,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator
from custom_components.bat_balancer.models import BankConfig, BatBalancerState
from custom_components.bat_balancer.sign_state_machine import SignStateMachine

_GOODWE_DEVICE_IDS = {
    "kontor": "696f2a85fed59b45f2ced7fc2663984a",
    "forrad": "e087f4789d3713e9b18f1ff27d4e7cb9",
}


def _make_states(**extra: str) -> MagicMock:
    defaults: dict[str, str] = {
        ENTITY_BAT_ALLOW_HW_AUTONOMY: "off",
        ENTITY_BAT_ECO_MODE_DISABLE_SOC: "0",
        ENTITY_HW_MISMATCH_THRESHOLD_W: "200",
    }
    for bid in BANKS:
        defaults[ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id=bid)] = "0"
        defaults[ENTITY_GOODWE_DOD_HOLDING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_FAST_CHARGING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_DEVICE_ID.format(bank_id=bid)] = _GOODWE_DEVICE_IDS[bid]
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
    last_written: dict[str, str | None] | None = None,
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
    coord._bank_was_online = {bid: True for bid in BANKS}
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._hw_autonomy_initialized = False
    coord._last_written_ems_modes = last_written or {bid: None for bid in BANKS}
    coord._hw_mismatch_ticks = {bid: 0 for bid in BANKS}
    coord._off_grid_lock_recovery_ts = {bid: 0.0 for bid in BANKS}
    coord._full_bias_active = False
    coord._last_ems_limit_w = {bid: 0.0 for bid in BANKS}
    return coord


def _pn_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "persistent_notification"
    ]


def _pn_create_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "persistent_notification" and call.args[1] == "create"
    ]


def _pn_dismiss_calls(coord: BatBalancerCoordinator) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == "persistent_notification" and call.args[1] == "dismiss"
    ]


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-STANDBY: standby written but HW discharging
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_STANDBY_fires_after_N_ticks():
    """Alarm fires after exactly HW_MISMATCH_TICKS_DEFAULT consecutive ticks."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"): "500",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_STANDBY, "forrad": GOODWE_EMS_MODE_STANDBY},
    )

    for tick in range(1, HW_MISMATCH_TICKS_DEFAULT + 1):
        coord.hass.services.async_call.reset_mock()
        await coord._check_hw_ems_mismatch()
        creates = _pn_create_calls(coord)
        if tick < HW_MISMATCH_TICKS_DEFAULT:
            assert creates == [], f"Alarm fired early at tick {tick}"
        else:
            assert len(creates) == 1
            assert "forrad" in creates[0]["notification_id"]
            assert "battery_standby" in creates[0]["message"]


@pytest.mark.asyncio
async def test_TC_MISMATCH_STANDBY_no_double_fire():
    """Alarm fires only ONCE — not on every tick after threshold."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "600",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_STANDBY, "forrad": GOODWE_EMS_MODE_STANDBY},
    )
    coord._hw_mismatch_ticks["kontor"] = HW_MISMATCH_TICKS_DEFAULT  # already fired

    await coord._check_hw_ems_mismatch()

    creates = _pn_create_calls(coord)
    assert creates == [], "Should not re-fire after threshold already reached"
    assert coord._hw_mismatch_ticks["kontor"] == HW_MISMATCH_TICKS_DEFAULT + 1


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-DISCHARGE: wrote discharge but HW charging
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_DISCHARGE_charging_triggers_alarm():
    """ems=discharge_battery but hw_power < -threshold → mismatch alarm."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "-500",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_DISCHARGE, "forrad": GOODWE_EMS_MODE_STANDBY},
    )
    coord._hw_mismatch_ticks["kontor"] = HW_MISMATCH_TICKS_DEFAULT - 1

    await coord._check_hw_ems_mismatch()

    creates = _pn_create_calls(coord)
    assert len(creates) == 1
    assert "kontor" in creates[0]["notification_id"]
    assert "discharge_battery" in creates[0]["message"]


@pytest.mark.asyncio
async def test_TC_MISMATCH_DISCHARGE_discharging_is_correct():
    """ems=discharge_battery and hw_power > 0 → no alarm."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "500",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_DISCHARGE, "forrad": GOODWE_EMS_MODE_STANDBY},
    )

    for _ in range(HW_MISMATCH_TICKS_DEFAULT + 1):
        await coord._check_hw_ems_mismatch()

    assert _pn_create_calls(coord) == []


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-CHARGE: wrote charge but HW discharging
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_CHARGE_discharging_triggers_alarm():
    """ems=charge_battery but hw_power > +threshold → mismatch alarm."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"): "300",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_STANDBY, "forrad": GOODWE_EMS_MODE_CHARGE},
    )
    coord._hw_mismatch_ticks["forrad"] = HW_MISMATCH_TICKS_DEFAULT - 1

    await coord._check_hw_ems_mismatch()

    creates = _pn_create_calls(coord)
    assert len(creates) == 1
    assert "forrad" in creates[0]["notification_id"]
    assert "charge_battery" in creates[0]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-CLEARS: mismatch resolves → dismiss
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_CLEARS_dismiss_when_resolved():
    """When mismatch ticks≥threshold and then resolves → persistent_notification.dismiss."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "0",  # back to 0 → resolved
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_STANDBY, "forrad": GOODWE_EMS_MODE_STANDBY},
    )
    coord._hw_mismatch_ticks["kontor"] = HW_MISMATCH_TICKS_DEFAULT  # was active

    await coord._check_hw_ems_mismatch()

    dismisses = _pn_dismiss_calls(coord)
    assert len(dismisses) == 1
    assert dismisses[0]["notification_id"] == "bat_balancer_hw_mismatch_kontor"
    assert coord._hw_mismatch_ticks["kontor"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-BELOW_THRESHOLD
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_BELOW_THRESHOLD_no_alarm():
    """hw_power=150W with standby and threshold=200W → no alarm."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"): "150",
            ENTITY_HW_MISMATCH_THRESHOLD_W: "200",
        }
    )
    coord = _make_coord(
        states,
        last_written={"kontor": GOODWE_EMS_MODE_STANDBY, "forrad": GOODWE_EMS_MODE_STANDBY},
    )

    for _ in range(HW_MISMATCH_TICKS_DEFAULT + 1):
        await coord._check_hw_ems_mismatch()

    assert _pn_create_calls(coord) == []
    assert coord._hw_mismatch_ticks["forrad"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# TC-MISMATCH-NO_EMS_WRITTEN
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_MISMATCH_NO_EMS_WRITTEN_skips_check():
    """last_written_ems=None (nothing written yet) → no alarm, ticks reset to 0."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "5000",
        }
    )
    coord = _make_coord(states)  # last_written defaults to None for all banks

    for _ in range(HW_MISMATCH_TICKS_DEFAULT + 1):
        await coord._check_hw_ems_mismatch()

    assert _pn_create_calls(coord) == []
    assert coord._hw_mismatch_ticks["kontor"] == 0


@pytest.mark.asyncio
async def test_TC_FIX_B_cross_dir_busts_ems_cache():
    """Fix-B: discharge commanded but HW charges → EMS+op_mode caches busted after N ticks."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"): "-3717",  # charging
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "2558",  # discharging (correct)
        }
    )
    coord = _make_coord(
        states,
        last_written={
            "kontor": GOODWE_EMS_MODE_DISCHARGE,
            "forrad": GOODWE_EMS_MODE_DISCHARGE,  # commanded discharge but HW charges
        },
    )
    coord._last_goodwe_ems_modes["forrad"] = GOODWE_EMS_MODE_DISCHARGE
    coord._last_goodwe_modes["forrad"] = "peak_shaving"
    coord._last_goodwe_ems_modes["kontor"] = (
        GOODWE_EMS_MODE_DISCHARGE  # set so we can verify it's untouched
    )

    for _ in range(HW_MISMATCH_TICKS_DEFAULT):
        await coord._check_hw_ems_mismatch()

    # After N ticks: EMS cache must be busted so next write is forced
    assert coord._last_goodwe_ems_modes["forrad"] is None, "EMS cache not busted"
    assert coord._last_goodwe_modes["forrad"] is None, "op_mode cache not busted"
    # Kontor was correct → not busted
    assert coord._last_goodwe_ems_modes["kontor"] == GOODWE_EMS_MODE_DISCHARGE


@pytest.mark.asyncio
async def test_TC_FIX_B_cooldown_prevents_repeated_bust():
    """Fix-B cooldown: recovery_ts set → second bust blocked until 60s passes."""
    states = _make_states(
        **{
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"): "-3717",
            ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"): "0",
        }
    )
    coord = _make_coord(
        states,
        last_written={
            "kontor": None,
            "forrad": GOODWE_EMS_MODE_DISCHARGE,
        },
    )
    # Simulate that recovery already fired recently (not yet 60s ago)
    coord._off_grid_lock_recovery_ts["forrad"] = time.monotonic()
    coord._last_goodwe_ems_modes["forrad"] = GOODWE_EMS_MODE_DISCHARGE

    for _ in range(HW_MISMATCH_TICKS_DEFAULT):
        await coord._check_hw_ems_mismatch()

    # Cache should NOT be busted (cooldown active)
    assert (
        coord._last_goodwe_ems_modes["forrad"] == GOODWE_EMS_MODE_DISCHARGE
    ), "EMS cache busted during cooldown — Fix-B cooldown not working"
