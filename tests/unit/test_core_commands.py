"""Tests for custom_components.carmabox.core.commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.core.commands import cmd_charge_pv
from custom_components.carmabox.optimizer.models import BatteryCommand, CarmaboxState


def _make_state(soc1: float = 50.0, soc2: float = 50.0) -> CarmaboxState:
    s = MagicMock(spec=CarmaboxState)
    s.battery_soc_1 = soc1
    s.battery_soc_2 = soc2
    return s


def _ok_safety():
    safety = MagicMock()
    safety.check_heartbeat.return_value = MagicMock(ok=True)
    safety.check_rate_limit.return_value = MagicMock(ok=True)
    safety.check_charge.return_value = MagicMock(ok=True)
    return safety


@pytest.mark.asyncio
async def test_skip_when_already_charge_pv():
    """No-op when last_command is CHARGE_PV (IT-1939)."""
    safety = _ok_safety()
    success, blocks = await cmd_charge_pv(
        MagicMock(), [], safety, _make_state(), BatteryCommand.CHARGE_PV, 20.0
    )
    assert success is False
    assert blocks == 0
    safety.check_heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_charge_pv_taper():
    """No-op when last_command is CHARGE_PV_TAPER (IT-1939)."""
    safety = _ok_safety()
    success, blocks = await cmd_charge_pv(
        MagicMock(), [], safety, _make_state(), BatteryCommand.CHARGE_PV_TAPER, 20.0
    )
    assert success is False
    assert blocks == 0


@pytest.mark.asyncio
async def test_blocked_by_heartbeat():
    safety = MagicMock()
    safety.check_heartbeat.return_value = MagicMock(ok=False, reason="hub offline")
    success, blocks = await cmd_charge_pv(
        MagicMock(), [], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is False
    assert blocks == 1


@pytest.mark.asyncio
async def test_blocked_by_rate_limit():
    safety = MagicMock()
    safety.check_heartbeat.return_value = MagicMock(ok=True)
    safety.check_rate_limit.return_value = MagicMock(ok=False, reason="rate limit")
    success, blocks = await cmd_charge_pv(
        MagicMock(), [], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is False
    assert blocks == 1


@pytest.mark.asyncio
async def test_blocked_by_charge_check():
    safety = MagicMock()
    safety.check_heartbeat.return_value = MagicMock(ok=True)
    safety.check_rate_limit.return_value = MagicMock(ok=True)
    safety.check_charge.return_value = MagicMock(ok=False, reason="too cold")
    success, blocks = await cmd_charge_pv(
        MagicMock(), [], safety, _make_state(), BatteryCommand.IDLE, 5.0
    )
    assert success is False
    assert blocks == 1


@pytest.mark.asyncio
async def test_adapter_path_success():
    """Modern adapter path — set_ems_mode charge_pv called."""
    safety = _ok_safety()
    adapter = MagicMock()
    adapter.soc = 50
    adapter.set_ems_mode = AsyncMock(return_value=True)

    success, blocks = await cmd_charge_pv(
        MagicMock(), [adapter], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is True
    assert blocks == 0
    adapter.set_ems_mode.assert_called_once_with("charge_pv")


@pytest.mark.asyncio
async def test_adapter_path_standby_when_full():
    """Adapter at 100% SoC → battery_standby mode."""
    safety = _ok_safety()
    adapter = MagicMock()
    adapter.soc = 100
    adapter.set_ems_mode = AsyncMock(return_value=True)

    success, blocks = await cmd_charge_pv(
        MagicMock(), [adapter], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is True
    adapter.set_ems_mode.assert_called_once_with("battery_standby")


@pytest.mark.asyncio
async def test_adapter_path_goodwe_disables_fast_charging():
    """GoodWe adapter must have fast_charging=False set (INV-3)."""
    from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

    safety = _ok_safety()
    adapter = MagicMock(spec=GoodWeAdapter)
    adapter.soc = 50
    adapter.set_ems_mode = AsyncMock(return_value=True)
    adapter.set_fast_charging = AsyncMock()

    success, blocks = await cmd_charge_pv(
        MagicMock(), [adapter], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is True
    adapter.set_fast_charging.assert_called_once_with(on=False)


@pytest.mark.asyncio
async def test_adapter_path_rollback_on_partial_failure():
    """R3: partial failure → rollback all adapters to standby."""
    safety = _ok_safety()
    adapter_ok = MagicMock()
    adapter_ok.soc = 50
    adapter_ok.set_ems_mode = AsyncMock(return_value=True)

    adapter_fail = MagicMock()
    adapter_fail.soc = 50
    adapter_fail.set_ems_mode = AsyncMock(side_effect=[False, AsyncMock(return_value=True)])

    # After the first pass: adapter_ok succeeds, adapter_fail fails → rollback
    adapter_ok.set_ems_mode = AsyncMock(side_effect=[True, True])   # charge_pv, then standby
    adapter_fail.set_ems_mode = AsyncMock(side_effect=[False, True])  # fails, then standby

    success, blocks = await cmd_charge_pv(
        MagicMock(), [adapter_ok, adapter_fail], safety, _make_state(), BatteryCommand.IDLE, 20.0
    )
    assert success is False
    assert blocks == 1
    # Both adapters should have received standby during rollback
    assert adapter_ok.set_ems_mode.call_count == 2
    assert adapter_fail.set_ems_mode.call_count == 2


@pytest.mark.asyncio
async def test_legacy_path_success():
    """Legacy path (no adapters) uses get_entity + safe_service_call."""
    safety = _ok_safety()

    def get_entity(key: str) -> str:
        return f"select.{key}"

    def read_float(entity_id: str | None) -> float:
        return 50.0  # not full

    service_call = AsyncMock(return_value=True)

    success, blocks = await cmd_charge_pv(
        MagicMock(),
        [],  # no adapters → legacy path
        safety,
        _make_state(),
        BatteryCommand.IDLE,
        20.0,
        get_entity=get_entity,
        read_float=read_float,
        safe_service_call=service_call,
    )
    assert success is True
    assert blocks == 0
    assert service_call.call_count == 2  # once per battery
