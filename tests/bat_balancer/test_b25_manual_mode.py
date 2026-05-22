"""B25 — coordinator reads sensor.bat_balancer_target_effective_w.

Template balancer_anti_export.yaml routes MANUAL/AUTO — coordinator needs no mode-branch.
Sign convention: +W=charge, -W=discharge (same as brain_target_bat_w).

TC1: effective_w=+5000 → distribute gets +5000 (charge)
TC2: effective_w=+3000 → distribute gets +3000 (AUTO compat, same path)
TC4: effective_w=-3000 → distribute gets -3000 (discharge)
TC5: const has ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W, coordinator has no mode-branch
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W,
    ENTITY_BRAIN_TARGET_BAT_W,
    ENTITY_SHADOW_MODE,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator


def _make_hass(states: dict) -> MagicMock:
    hass = MagicMock()
    hass.data = {}

    def _get(entity_id: str):
        if entity_id not in states:
            return None
        s = MagicMock()
        s.state = states[entity_id]
        return s

    hass.states.get = MagicMock(side_effect=_get)
    hass.services.async_call = AsyncMock()
    return hass


def _make_coord(hass: MagicMock) -> BatBalancerCoordinator:
    from custom_components.bat_balancer.const import BANKS
    from custom_components.bat_balancer.models import BankConfig, BatBalancerState
    from custom_components.bat_balancer.sign_state_machine import SignStateMachine

    entry = MagicMock()
    entry.entry_id = "test"
    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._entry = entry
    coord._startup_safe_mode_done = True
    coord._last_brain_write_ts = time.monotonic()
    coord._bank_was_online = {bid: True for bid in BANKS}
    coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
    coord._bank_configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    coord._state = BatBalancerState()
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._last_written_ems_modes = {bid: None for bid in BANKS}
    coord._hw_mismatch_ticks = {bid: 0 for bid in BANKS}
    coord._hw_autonomy_initialized = False
    coord._current_emergency_active = False
    coord._current_below_reset_since = None
    coord._full_bias_active = False
    coord._last_ems_limit_w = {bid: 0.0 for bid in BANKS}
    coord._hw_actual_w = {bid: 0.0 for bid in BANKS}
    coord._hw_overshoot_ema = 0.0
    coord._hw_overshoot_ticks = 0
    coord._hw_undershoot_ticks = 0
    coord._hw_correction_active = False
    return coord


def _fake_distribute(target_w, bank_configs, bank_states, snapshot):
    result = MagicMock()
    result.targets = {"kontor": target_w / 2, "forrad": target_w / 2}
    result.status = MagicMock()
    result.equalization_active = False
    result.bms_cap_suppressed_w = 0.0
    result.rejected_reason = None
    result.overflow_redistributed = False
    result.capped_bank_ids = frozenset()
    return result


# ---------------------------------------------------------------------------
# TC1: effective_w=+5000 → distribute gets +5000 (charge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_target_charge() -> None:
    """TC1: sensor.bat_balancer_target_effective_w=5000 → distribute gets 5000W."""
    hass = _make_hass(
        {
            ENTITY_SHADOW_MODE: "off",
            ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W: "5000",
            ENTITY_BRAIN_TARGET_BAT_W: "5000",  # Design B: brain must match effective
            "sensor.bat_soc_filtered_kontor": "60",
            "sensor.bat_soc_filtered_forrad": "60",
        }
    )
    coord = _make_coord(hass)
    captured = {}

    def fake_dist(target_w, *a, **kw):
        captured["target_w"] = target_w
        return _fake_distribute(target_w, *a, **kw)

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        side_effect=fake_dist,
    ):
        with patch.object(coord, "_read_bank_state", return_value=MagicMock(is_online=True)):
            with patch.object(coord, "_write_power_limit", new_callable=AsyncMock):
                await coord._tick()

    assert captured.get("target_w") == 5000.0, f"Expected 5000, got {captured.get('target_w')}"


# ---------------------------------------------------------------------------
# TC2: effective_w=+3000 → distribute gets +3000 (same path, AUTO compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_target_auto_compat() -> None:
    """TC2: effective_w=3000 → distribute gets 3000W (no special mode logic needed)."""
    hass = _make_hass(
        {
            ENTITY_SHADOW_MODE: "off",
            ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W: "3000",
            ENTITY_BRAIN_TARGET_BAT_W: "3000",  # Design B: brain must match effective
            "sensor.bat_soc_filtered_kontor": "60",
            "sensor.bat_soc_filtered_forrad": "60",
        }
    )
    coord = _make_coord(hass)
    captured = {}

    def fake_dist(target_w, *a, **kw):
        captured["target_w"] = target_w
        return _fake_distribute(target_w, *a, **kw)

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        side_effect=fake_dist,
    ):
        with patch.object(coord, "_read_bank_state", return_value=MagicMock(is_online=True)):
            with patch.object(coord, "_write_power_limit", new_callable=AsyncMock):
                await coord._tick()

    assert captured.get("target_w") == 3000.0, f"Expected 3000, got {captured.get('target_w')}"


# ---------------------------------------------------------------------------
# TC4: effective_w=-3000 → distribute gets -3000 (discharge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_target_discharge() -> None:
    """TC4: effective_w=-3000 → distribute gets -3000W (discharge via negative)."""
    hass = _make_hass(
        {
            ENTITY_SHADOW_MODE: "off",
            ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W: "-3000",
            ENTITY_BRAIN_TARGET_BAT_W: "-3000",  # Design B: brain must match effective
            "sensor.bat_soc_filtered_kontor": "60",
            "sensor.bat_soc_filtered_forrad": "60",
        }
    )
    coord = _make_coord(hass)
    captured = {}

    def fake_dist(target_w, *a, **kw):
        captured["target_w"] = target_w
        return _fake_distribute(target_w, *a, **kw)

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        side_effect=fake_dist,
    ):
        with patch.object(coord, "_read_bank_state", return_value=MagicMock(is_online=True)):
            with patch.object(coord, "_write_power_limit", new_callable=AsyncMock):
                await coord._tick()

    assert captured.get("target_w") == -3000.0, f"Expected -3000, got {captured.get('target_w')}"


# ---------------------------------------------------------------------------
# TC5: const + coordinator structure
# ---------------------------------------------------------------------------


def test_coordinator_structure_post_b25() -> None:
    """TC5: ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W in const, no mode-branch in _tick."""
    import inspect
    import textwrap

    import custom_components.bat_balancer.coordinator as coord_mod

    # const must have the new entity
    assert ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W == "sensor.bat_balancer_target_effective_w"

    # _tick must NOT have if mode == "MANUAL" branch (template routes it)
    source = textwrap.dedent(inspect.getsource(coord_mod.BatBalancerCoordinator._tick))
    assert 'mode == "MANUAL"' not in source, "_tick must not have explicit MANUAL branch"
    assert "ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W" in source, "_tick must read effective_w entity"
