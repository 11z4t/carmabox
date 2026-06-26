"""Tests for binary_balancer coordinator — MANUAL mode guard in _apply_switch_action.

PRIO 1 (Borje 2026-06-26): Invariant #4 — MANUAL-mode = operator äger switchen.
Balancer skriver ALDRIG switch.turn_on/turn_off i MANUAL.

Per-asset tests (ett testfall per balancer):
  - pool_elv  (TestPoolElvManual)
  - pool_vp   (TestPoolVpManual)
  - miner     (TestMinerManual)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.binary_balancer.coordinator import BinaryBalancerCoordinator
from custom_components.binary_balancer.models import AssetConfig, BalancerState, SwitchAction


def _make_coordinator(
    asset_id: str, override_mode: str
) -> tuple[BinaryBalancerCoordinator, MagicMock]:
    """Build a minimal BinaryBalancerCoordinator with mocked hass."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()

    states: dict[str, MagicMock] = {}

    def get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = get_state

    mode_state = MagicMock()
    mode_state.state = override_mode
    states[f"input_select.{asset_id}_balancer_mode"] = mode_state

    op_state = MagicMock()
    op_state.state = "off"
    states[f"input_boolean.{asset_id}_operator_override"] = op_state

    config = AssetConfig(
        asset_id=asset_id,
        typical_drag_w=3000.0,
        switch_entity=f"switch.{asset_id}_switch",
        season_active_entity=None,
        min_dwell_s=60,
    )

    coord = BinaryBalancerCoordinator.__new__(BinaryBalancerCoordinator)
    coord.hass = hass
    coord._config = config
    coord._state = BalancerState()
    coord.logger = MagicMock()

    return coord, hass


# ---------------------------------------------------------------------------
# pool_elv
# ---------------------------------------------------------------------------


class TestPoolElvManual:
    """pool_elv binary_balancer: MANUAL → switch aldrig rörd."""

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_off(self) -> None:
        """Brain beordrar TURN_OFF i MANUAL — switch.pool_elvarmare rörs INTE."""
        coord, hass = _make_coordinator("pool_elv", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_OFF)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_on(self) -> None:
        """Brain beordrar TURN_ON i MANUAL — switch.pool_elvarmare rörs INTE."""
        coord, hass = _make_coordinator("pool_elv", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_ON)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_executes_turn_on(self) -> None:
        """AUTO-mode: TURN_ON anropar switch.turn_on (kontrollfall)."""
        coord, hass = _make_coordinator("pool_elv", "AUTO")
        await coord._apply_switch_action(SwitchAction.TURN_ON)
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_on", {"entity_id": "switch.pool_elv_switch"}, blocking=False
        )


# ---------------------------------------------------------------------------
# pool_vp
# ---------------------------------------------------------------------------


class TestPoolVpManual:
    """pool_vp binary_balancer: MANUAL → switch aldrig rörd."""

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_off(self) -> None:
        """Brain beordrar TURN_OFF i MANUAL — switch.pool_vp rörs INTE."""
        coord, hass = _make_coordinator("pool_vp", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_OFF)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_on(self) -> None:
        """Brain beordrar TURN_ON i MANUAL — switch.pool_vp rörs INTE."""
        coord, hass = _make_coordinator("pool_vp", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_ON)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_executes_turn_off(self) -> None:
        """AUTO-mode: TURN_OFF anropar switch.turn_off (kontrollfall)."""
        coord, hass = _make_coordinator("pool_vp", "AUTO")
        await coord._apply_switch_action(SwitchAction.TURN_OFF)
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_off", {"entity_id": "switch.pool_vp_switch"}, blocking=False
        )


# ---------------------------------------------------------------------------
# miner
# ---------------------------------------------------------------------------


class TestMinerManual:
    """miner binary_balancer: MANUAL → switch aldrig rörd."""

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_off(self) -> None:
        """Brain beordrar TURN_OFF i MANUAL — switch.shelly1pmg4 rörs INTE."""
        coord, hass = _make_coordinator("miner", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_OFF)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_manual_blocks_turn_on(self) -> None:
        """Brain beordrar TURN_ON i MANUAL — switch.shelly1pmg4 rörs INTE."""
        coord, hass = _make_coordinator("miner", "MANUAL")
        await coord._apply_switch_action(SwitchAction.TURN_ON)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_executes_turn_on(self) -> None:
        """AUTO-mode: TURN_ON anropar switch.turn_on (kontrollfall)."""
        coord, hass = _make_coordinator("miner", "AUTO")
        await coord._apply_switch_action(SwitchAction.TURN_ON)
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_on", {"entity_id": "switch.miner_switch"}, blocking=False
        )

    @pytest.mark.asyncio
    async def test_manual_lowercase_still_blocks(self) -> None:
        """HA-state 'manual' (lowercase) uppercasas korrekt — switch rörs INTE."""
        coord, hass = _make_coordinator("miner", "manual")
        await coord._apply_switch_action(SwitchAction.TURN_OFF)
        hass.services.async_call.assert_not_called()
