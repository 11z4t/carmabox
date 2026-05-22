"""AC-HW-1..AC-HW-4: GoodWe HW autonomy disable on init + per-tick watchdog.

TC-HW-AUTONOMY-DISABLE: init call → eco_mode_soc=0, dod_holding=off, fast_charging=off both banks
TC-WATCHDOG-RE_ENFORCE: tick with eco_mode_soc != 0 → write back 0 + persistent_notification
TC-OPERATOR-BYPASS: allow_hw_autonomy=on → enforce/init skipped
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
    ENTITY_GOODWE_DEVICE_ID,
    ENTITY_GOODWE_DOD_HOLDING,
    ENTITY_GOODWE_ECO_MODE_SOC,
    ENTITY_GOODWE_FAST_CHARGING,
    GOODWE_ECO_MODE_ENABLE,
    GOODWE_ECO_MODE_SLOTS,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator
from custom_components.bat_balancer.models import BankConfig, BatBalancerState
from custom_components.bat_balancer.sign_state_machine import SignStateMachine

_GOODWE_DEVICE_IDS = {
    "kontor": "696f2a85fed59b45f2ced7fc2663984a",
    "forrad": "e087f4789d3713e9b18f1ff27d4e7cb9",
}


def _make_states(**extra: str) -> MagicMock:
    """Return a hass.states mock with entity states."""
    defaults: dict[str, str] = {
        ENTITY_BAT_ALLOW_HW_AUTONOMY: "off",
        ENTITY_BAT_ECO_MODE_DISABLE_SOC: "0",
    }
    for bid in BANKS:
        defaults[ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id=bid)] = "0"
        defaults[ENTITY_GOODWE_DOD_HOLDING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_FAST_CHARGING.format(bank_id=bid)] = "off"
        defaults[ENTITY_GOODWE_DEVICE_ID.format(bank_id=bid)] = _GOODWE_DEVICE_IDS[bid]
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


def _make_coord(states_mock: MagicMock | None = None) -> BatBalancerCoordinator:
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
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._hw_autonomy_initialized = False
    return coord


def _service_calls(coord: BatBalancerCoordinator, domain: str, service: str) -> list[dict]:
    return [
        call.args[2]
        for call in coord.hass.services.async_call.call_args_list
        if call.args[0] == domain and call.args[1] == service
    ]


# ─────────────────────────────────────────────────────────────────────────────
# TC-HW-AUTONOMY-DISABLE (AC-HW-1, AC-HW-6)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_HW_AUTONOMY_DISABLE_writes_both_banks():
    """Init: eco_mode_soc=0 + dod_holding=off + fast_charging=off for kontor and forrad."""
    coord = _make_coord()
    await coord.async_disable_hw_autonomy_on_init()

    number_calls = _service_calls(coord, "number", "set_value")
    switch_off_calls = _service_calls(coord, "switch", "turn_off")

    eco_entity_ids = {c["entity_id"] for c in number_calls}
    assert "number.goodwe_eco_mode_soc_kontor" in eco_entity_ids
    assert "number.goodwe_eco_mode_soc_forrad" in eco_entity_ids

    for c in number_calls:
        assert c["value"] == 0.0, f"Expected eco_mode_soc=0 but got {c['value']}"

    switch_entity_ids = {c["entity_id"] for c in switch_off_calls}
    assert "switch.goodwe_kontor_dod_holding" in switch_entity_ids
    assert "switch.goodwe_forrad_dod_holding" in switch_entity_ids
    assert "switch.goodwe_fast_charging_switch_kontor" in switch_entity_ids
    assert "switch.goodwe_fast_charging_switch_forrad" in switch_entity_ids

    assert coord._hw_autonomy_initialized is True


@pytest.mark.asyncio
async def test_TC_HW_AUTONOMY_DISABLE_existing_eco_100_reset():
    """AC-HW-6: eco_mode_soc=100 on init → reset to 0."""
    states = _make_states(
        **{
            ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id="kontor"): "100",
            ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id="forrad"): "100",
        }
    )
    coord = _make_coord(states)
    await coord.async_disable_hw_autonomy_on_init()

    number_calls = _service_calls(coord, "number", "set_value")
    for c in number_calls:
        assert c["value"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TC-WATCHDOG-RE_ENFORCE (AC-HW-2, AC-HW-3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_WATCHDOG_eco_mode_soc_triggers_re_enforce():
    """AC-HW-2: eco_mode_soc_kontor=50 → write back 0 + persistent_notification."""
    states = _make_states(**{ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id="kontor"): "50"})
    coord = _make_coord(states)
    await coord._enforce_hw_autonomy_off()

    number_calls = _service_calls(coord, "number", "set_value")
    eco_writes = [c for c in number_calls if "eco_mode_soc" in c["entity_id"]]
    assert any(c["entity_id"] == "number.goodwe_eco_mode_soc_kontor" for c in eco_writes)

    pn_calls = _service_calls(coord, "persistent_notification", "create")
    assert len(pn_calls) >= 1
    assert any("kontor" in str(c) for c in pn_calls)


@pytest.mark.asyncio
async def test_TC_WATCHDOG_dod_holding_on_triggers_re_enforce():
    """AC-HW-3: dod_holding_forrad=on → turn_off + persistent_notification."""
    states = _make_states(**{ENTITY_GOODWE_DOD_HOLDING.format(bank_id="forrad"): "on"})
    coord = _make_coord(states)
    await coord._enforce_hw_autonomy_off()

    switch_off_calls = _service_calls(coord, "switch", "turn_off")
    assert any(c["entity_id"] == "switch.goodwe_forrad_dod_holding" for c in switch_off_calls)
    pn_calls = _service_calls(coord, "persistent_notification", "create")
    assert len(pn_calls) >= 1


@pytest.mark.asyncio
async def test_TC_WATCHDOG_no_write_when_all_correct():
    """Watchdog skips writes when eco_mode_soc=0 + dod_holding=off + fast_charging=off."""
    coord = _make_coord()
    await coord._enforce_hw_autonomy_off()

    number_calls = _service_calls(coord, "number", "set_value")
    switch_off_calls = _service_calls(coord, "switch", "turn_off")
    pn_calls = _service_calls(coord, "persistent_notification", "create")
    assert number_calls == []
    assert switch_off_calls == []
    assert pn_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# TC-OPERATOR-BYPASS (AC-HW-4)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_OPERATOR_BYPASS_init_skip():
    """AC-HW-4: allow_hw_autonomy=on → init disable skipped, no writes."""
    states = _make_states(
        **{
            ENTITY_BAT_ALLOW_HW_AUTONOMY: "on",
            ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id="kontor"): "100",
        }
    )
    coord = _make_coord(states)
    await coord.async_disable_hw_autonomy_on_init()

    number_calls = _service_calls(coord, "number", "set_value")
    assert number_calls == [], "Init should not write when allow_hw_autonomy=on"
    assert coord._hw_autonomy_initialized is False


@pytest.mark.asyncio
async def test_TC_OPERATOR_BYPASS_watchdog_skip():
    """AC-HW-4: allow_hw_autonomy=on → watchdog skipped even with eco_mode_soc=100."""
    states = _make_states(
        **{
            ENTITY_BAT_ALLOW_HW_AUTONOMY: "on",
            ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id="kontor"): "100",
            ENTITY_GOODWE_DOD_HOLDING.format(bank_id="forrad"): "on",
        }
    )
    coord = _make_coord(states)
    await coord._enforce_hw_autonomy_off()

    number_calls = _service_calls(coord, "number", "set_value")
    switch_off_calls = _service_calls(coord, "switch", "turn_off")
    assert number_calls == []
    assert switch_off_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# TC-ECO-MODE-SLOTS (root-cause fix — slots 2/3/4 + eco_mode_enable)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_TC_ECO_MODE_SLOTS_all_disabled_on_init():
    """Root cause fix: init disables eco_mode_enable + all 4 slot switches via goodwe.set_parameter."""
    coord = _make_coord()
    await coord.async_disable_hw_autonomy_on_init()

    gw_calls = _service_calls(coord, "goodwe", "set_parameter")
    params_written = {c["parameter"] for c in gw_calls}

    assert GOODWE_ECO_MODE_ENABLE in params_written, "eco_mode_enable not disabled"
    for slot in GOODWE_ECO_MODE_SLOTS:
        assert slot in params_written, f"{slot} not disabled"

    # All values must be 0
    for c in gw_calls:
        assert c["value"] == 0, f"Expected 0 for {c['parameter']}, got {c['value']}"


@pytest.mark.asyncio
async def test_TC_ECO_MODE_SLOTS_device_id_per_bank():
    """Each bank's eco_mode slots get the correct device_id."""
    coord = _make_coord()
    await coord.async_disable_hw_autonomy_on_init()

    gw_calls = _service_calls(coord, "goodwe", "set_parameter")
    device_ids_used = {c["device_id"] for c in gw_calls}

    assert _GOODWE_DEVICE_IDS["kontor"] in device_ids_used
    assert _GOODWE_DEVICE_IDS["forrad"] in device_ids_used


@pytest.mark.asyncio
async def test_TC_ECO_MODE_SLOTS_no_calls_when_device_id_missing():
    """If device_id helper is empty, log warning but don't crash."""
    states = _make_states(
        **{
            ENTITY_GOODWE_DEVICE_ID.format(bank_id="kontor"): "",
            ENTITY_GOODWE_DEVICE_ID.format(bank_id="forrad"): "",
        }
    )
    coord = _make_coord(states)
    # Should not raise
    await coord.async_disable_hw_autonomy_on_init()

    gw_calls = _service_calls(coord, "goodwe", "set_parameter")
    assert gw_calls == [], "No goodwe.set_parameter calls when device_id is empty"
