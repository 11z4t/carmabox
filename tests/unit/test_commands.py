"""Tests for core/commands.py — legacy charge_pv path coverage (PLAT-1217)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.carmabox.adapters.goodwe import GoodWeAdapter
from custom_components.carmabox.core.commands import cmd_charge_pv
from custom_components.carmabox.optimizer.models import BatteryCommand
from custom_components.carmabox.optimizer.safety_guard import SafetyResult


def _safety_ok() -> MagicMock:
    """Return a SafetyGuard mock where all checks pass."""
    safety = MagicMock()
    safety.check_heartbeat.return_value = SafetyResult(ok=True)
    safety.check_rate_limit.return_value = SafetyResult(ok=True)
    safety.check_charge.return_value = SafetyResult(ok=True)
    return safety


def _state(soc1: float = 50.0, soc2: float = 50.0) -> MagicMock:
    s = MagicMock()
    s.battery_soc_1 = soc1
    s.battery_soc_2 = soc2
    return s


class TestChargePvLegacyGuards:
    """Lines 113-118: RuntimeError raised when legacy callbacks missing."""

    @pytest.mark.asyncio
    async def test_raises_when_get_entity_none(self) -> None:
        with pytest.raises(RuntimeError, match="get_entity required"):
            await cmd_charge_pv(
                hass=MagicMock(),
                adapters=[],
                safety=_safety_ok(),
                state=_state(),
                last_command=BatteryCommand.STANDBY,
                temp_c=20.0,
                get_entity=None,
                read_float=Mock(),
                safe_service_call=AsyncMock(return_value=True),
            )

    @pytest.mark.asyncio
    async def test_raises_when_read_float_none(self) -> None:
        with pytest.raises(RuntimeError, match="read_float required"):
            await cmd_charge_pv(
                hass=MagicMock(),
                adapters=[],
                safety=_safety_ok(),
                state=_state(),
                last_command=BatteryCommand.STANDBY,
                temp_c=20.0,
                get_entity=Mock(return_value="entity_id"),
                read_float=None,
                safe_service_call=AsyncMock(return_value=True),
            )

    @pytest.mark.asyncio
    async def test_raises_when_safe_service_call_none(self) -> None:
        with pytest.raises(RuntimeError, match="safe_service_call required"):
            await cmd_charge_pv(
                hass=MagicMock(),
                adapters=[],
                safety=_safety_ok(),
                state=_state(),
                last_command=BatteryCommand.STANDBY,
                temp_c=20.0,
                get_entity=Mock(return_value="entity_id"),
                read_float=Mock(return_value=50.0),
                safe_service_call=None,
            )


class TestChargePvLegacyLoop:
    """Lines 120-147: legacy for-loop paths."""

    @pytest.mark.asyncio
    async def test_entity_none_skips_both(self) -> None:
        """Line 123: get_entity returns None → continue for both EMS keys."""
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value=None),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
        )
        assert success is False
        assert delta == 0

    @pytest.mark.asyncio
    async def test_service_call_success_sets_success(self) -> None:
        """Lines 127-132: successful service call → success=True."""
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="ems_entity"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
        )
        assert success is True
        assert delta == 0

    @pytest.mark.asyncio
    async def test_service_call_failure_sets_failed(self) -> None:
        """Line 134: service call returns False → failed=True, success=False."""
        success, _delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="ems_entity"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=False),
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_check_write_verify_called_when_executor_enabled(self) -> None:
        """Line 131: check_write_verify invoked when executor_enabled=True."""
        verify = Mock()
        await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="ems_entity"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
            check_write_verify=verify,
            executor_enabled=True,
        )
        assert verify.called

    @pytest.mark.asyncio
    async def test_rollback_on_partial_failure(self) -> None:
        """Lines 138-147: first battery succeeds, second fails → rollback to standby."""
        call_results = [True, False]  # battery_ems_1 OK, battery_ems_2 fails
        rollback_calls: list[tuple] = []

        async def mixed_service_call(domain: str, service: str, data: dict) -> bool:
            if data.get("option") == "battery_standby":
                rollback_calls.append((domain, service, data))
                return True
            return call_results.pop(0) if call_results else False

        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="ems_entity"),
            read_float=Mock(return_value=50.0),
            safe_service_call=mixed_service_call,
        )
        assert success is False
        assert delta == 1  # rollback increments delta
        assert len(rollback_calls) >= 1  # standby rollback was called


class TestChargePvEarlyReturn:
    """Line 68: early return when command already CHARGE_PV / CHARGE_PV_TAPER."""

    @pytest.mark.asyncio
    async def test_skips_when_already_charge_pv(self) -> None:
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.CHARGE_PV,
            temp_c=20.0,
        )
        assert success is False
        assert delta == 0

    @pytest.mark.asyncio
    async def test_skips_when_already_charge_pv_taper(self) -> None:
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.CHARGE_PV_TAPER,
            temp_c=20.0,
        )
        assert success is False
        assert delta == 0


class TestChargePvSafetyBlocks:
    """Lines 72-84: SafetyGuard block paths."""

    @pytest.mark.asyncio
    async def test_heartbeat_block(self) -> None:
        """Lines 73-74: heartbeat not ok → return False, 1."""
        safety = MagicMock()
        safety.check_heartbeat.return_value = SafetyResult(ok=False, reason="stale")
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=safety,
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="e"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
        )
        assert success is False
        assert delta == 1

    @pytest.mark.asyncio
    async def test_rate_limit_block(self) -> None:
        """Lines 78-79: rate_limit not ok → return False, 1."""
        safety = MagicMock()
        safety.check_heartbeat.return_value = SafetyResult(ok=True)
        safety.check_rate_limit.return_value = SafetyResult(ok=False, reason="rate")
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=safety,
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="e"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
        )
        assert success is False
        assert delta == 1

    @pytest.mark.asyncio
    async def test_charge_check_block(self) -> None:
        """Lines 83-84: charge_check not ok → return False, 1."""
        safety = MagicMock()
        safety.check_heartbeat.return_value = SafetyResult(ok=True)
        safety.check_rate_limit.return_value = SafetyResult(ok=True)
        safety.check_charge.return_value = SafetyResult(ok=False, reason="soc too high")
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[],
            safety=safety,
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
            get_entity=Mock(return_value="e"),
            read_float=Mock(return_value=50.0),
            safe_service_call=AsyncMock(return_value=True),
        )
        assert success is False
        assert delta == 1


class TestChargePvAdapterPath:
    """Lines 91-110: adapter-based control path."""

    def _mock_adapter(self, soc: float = 50.0, set_ok: bool = True) -> MagicMock:
        adapter = MagicMock()
        adapter.soc = soc
        adapter.set_ems_mode = AsyncMock(return_value=set_ok)
        return adapter

    @pytest.mark.asyncio
    async def test_adapter_success(self) -> None:
        """Lines 95,100-101: adapter set_ems_mode charge_pv succeeds."""
        adapter = self._mock_adapter(soc=50.0, set_ok=True)
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[adapter],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
        )
        assert success is True
        assert delta == 0
        adapter.set_ems_mode.assert_called_with("charge_pv")

    @pytest.mark.asyncio
    async def test_adapter_soc_100_uses_standby(self) -> None:
        """Line 93: soc >= 100 → set battery_standby."""
        adapter = self._mock_adapter(soc=100.0, set_ok=True)
        success, _delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[adapter],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
        )
        assert success is True
        adapter.set_ems_mode.assert_called_with("battery_standby")

    @pytest.mark.asyncio
    async def test_adapter_failure_sets_failed(self) -> None:
        """Lines 102-103: set_ems_mode returns False → failed=True."""
        adapter = self._mock_adapter(soc=50.0, set_ok=False)
        success, _delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[adapter],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
        )
        assert success is False

    @pytest.mark.asyncio
    async def test_adapter_partial_failure_rollback(self) -> None:
        """Lines 106-110: one adapter succeeds, one fails → rollback."""
        good = self._mock_adapter(soc=50.0, set_ok=True)
        bad = self._mock_adapter(soc=50.0, set_ok=False)
        success, delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[good, bad],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
        )
        assert success is False
        assert delta == 1
        # Both adapters should receive rollback standby call
        good.set_ems_mode.assert_called_with("battery_standby")
        bad.set_ems_mode.assert_called_with("battery_standby")

    @pytest.mark.asyncio
    async def test_goodwe_adapter_disables_fast_charging(self) -> None:
        """Line 99: GoodWeAdapter → set_fast_charging(on=False) called after charge_pv."""
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.soc = 50.0
        adapter.set_ems_mode = AsyncMock(return_value=True)
        adapter.set_fast_charging = AsyncMock()
        success, _delta = await cmd_charge_pv(
            hass=MagicMock(),
            adapters=[adapter],
            safety=_safety_ok(),
            state=_state(),
            last_command=BatteryCommand.STANDBY,
            temp_c=20.0,
        )
        assert success is True
        adapter.set_fast_charging.assert_called_once_with(on=False)
