"""Tests for CARMA Box — FusionSolarChargerAdapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.adapters.fusionsolar_charger import (
    _ALERT_THRESHOLD_S,
    _CLOUD_TOLERANCE_S,
    _MIN_CURRENT_A,
    FusionSolarChargerAdapter,
)

PREFIX = "fusionsolarplus_ne132457623"


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state
    hass.states.get = lambda eid: states.get(eid)
    hass.services.async_call = AsyncMock()
    return hass


def _adapter(
    **state_overrides: str,
) -> tuple[FusionSolarChargerAdapter, MagicMock]:
    defaults = {
        f"sensor.{PREFIX}_charger_state": "charging",
        f"sensor.{PREFIX}_dynamic_charger_current": "8.0",
        f"sensor.{PREFIX}_charger_session_energy": "5.2",
    }
    defaults.update(state_overrides)
    hass = _make_hass(*defaults.items())
    adapter = FusionSolarChargerAdapter(hass, PREFIX)
    return adapter, hass


class TestFusionSolarChargerAdapterRead:
    def test_status_charging(self) -> None:
        adapter, _ = _adapter()
        assert adapter.status == "charging"

    def test_status_disconnected(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_charger_state": "disconnected"})
        assert adapter.status == "disconnected"

    def test_current_a(self) -> None:
        adapter, _ = _adapter()
        assert adapter.current_a == 8.0

    def test_current_unavailable_defaults_zero(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "unavailable"})
        assert adapter.current_a == 0.0

    def test_power_w_approximation(self) -> None:
        """power_w = current_a × 230 (1-phase approximation)."""
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "10.0"})
        assert adapter.power_w == pytest.approx(2300.0)

    def test_is_charging_true(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_charger_state": "charging"})
        assert adapter.is_charging is True

    def test_is_charging_false_connected(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_charger_state": "connected_waiting"})
        assert adapter.is_charging is False

    def test_plug_connected_true(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_charger_state": "charging"})
        assert adapter.plug_connected is True

    def test_plug_connected_false_when_disconnected(self) -> None:
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_charger_state": "disconnected"})
        assert adapter.plug_connected is False

    def test_session_energy(self) -> None:
        adapter, _ = _adapter()
        assert adapter.session_energy_kwh == 5.2


class TestFusionSolarChargerAdapterWrite:
    @pytest.mark.asyncio
    async def test_set_current_calls_number_entity(self) -> None:
        adapter, hass = _adapter()
        result = await adapter.set_current(10)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": f"number.{PREFIX}_dynamic_charger_current",
                "value": 10,
            },
        )

    @pytest.mark.asyncio
    async def test_set_current_clamps_to_min(self) -> None:
        adapter, hass = _adapter()
        await adapter.set_current(3)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == _MIN_CURRENT_A

    @pytest.mark.asyncio
    async def test_set_current_clamps_to_max(self) -> None:
        from custom_components.carmabox.const import MAX_EV_CURRENT

        adapter, hass = _adapter()
        await adapter.set_current(99)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == MAX_EV_CURRENT

    @pytest.mark.asyncio
    async def test_set_current_updates_target(self) -> None:
        adapter, _ = _adapter()
        await adapter.set_current(8)
        assert adapter._target_amps == 8
        assert adapter._target_set_at is not None

    @pytest.mark.asyncio
    async def test_enable_calls_resume_button(self) -> None:
        adapter, hass = _adapter()
        result = await adapter.enable()
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "button",
            "press",
            {"entity_id": f"button.{PREFIX}_charger_resume"},
        )

    @pytest.mark.asyncio
    async def test_disable_calls_pause_button(self) -> None:
        adapter, hass = _adapter()
        result = await adapter.disable()
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "button",
            "press",
            {"entity_id": f"button.{PREFIX}_charger_pause"},
        )

    @pytest.mark.asyncio
    async def test_disable_clears_target(self) -> None:
        adapter, _ = _adapter()
        await adapter.set_current(10)
        await adapter.disable()
        assert adapter._target_amps == 0
        assert adapter._target_set_at is None

    @pytest.mark.asyncio
    async def test_reset_to_default(self) -> None:
        adapter, hass = _adapter()
        await adapter.reset_to_default()
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == _MIN_CURRENT_A


class TestFusionSolarChargerDriftRecovery:
    @pytest.mark.asyncio
    async def test_needs_recovery_false_before_tolerance(self) -> None:
        """No drift flag within cloud tolerance window."""
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "8.0"})
        await adapter.set_current(12)
        # Actual=8A, target=12A — but within tolerance window (just set)
        assert adapter.needs_recovery is False

    @pytest.mark.asyncio
    async def test_needs_recovery_true_after_tolerance_with_drift(self) -> None:
        """Drift>1A after 60s → needs_recovery=True."""
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "6.0"})
        await adapter.set_current(10)  # MAX_EV_CURRENT=10; actual=6.0 → drift=4A
        # Backdate command timestamp past tolerance
        adapter._target_set_at = time.monotonic() - (_CLOUD_TOLERANCE_S + 1)
        assert adapter.needs_recovery is True

    @pytest.mark.asyncio
    async def test_needs_recovery_false_when_within_drift(self) -> None:
        """Drift≤1A even after tolerance → no recovery needed."""
        adapter, _ = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "7.5"})
        await adapter.set_current(8)
        adapter._target_set_at = time.monotonic() - (_CLOUD_TOLERANCE_S + 5)
        # Drift = 0.5A ≤ _DRIFT_MAX_A=1 → no recovery
        assert adapter.needs_recovery is False

    @pytest.mark.asyncio
    async def test_needs_recovery_false_when_no_target(self) -> None:
        adapter, _ = _adapter()
        assert adapter.needs_recovery is False

    @pytest.mark.asyncio
    async def test_try_recover_resends_command(self) -> None:
        """try_recover() resends the last current command."""
        adapter, hass = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "6.0"})
        await adapter.set_current(8)  # 8A within MAX_EV_CURRENT=10
        adapter._target_set_at = time.monotonic() - (_CLOUD_TOLERANCE_S + 5)
        hass.services.async_call.reset_mock()

        result = await adapter.try_recover()
        assert result == "fusionsolar_resend"
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": f"number.{PREFIX}_dynamic_charger_current",
                "value": 8,
            },
        )

    @pytest.mark.asyncio
    async def test_try_recover_none_when_not_needed(self) -> None:
        adapter, _ = _adapter()
        result = await adapter.try_recover()
        assert result is None

    @pytest.mark.asyncio
    async def test_try_recover_sends_alert_after_120s(self) -> None:
        """Alert is sent via notify service after >120s unresolved drift."""
        adapter, hass = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "6.0"})
        adapter.notify_service = "notify.slack_carma"
        await adapter.set_current(8)
        adapter._target_set_at = time.monotonic() - (_ALERT_THRESHOLD_S + 5)

        await adapter.try_recover()

        # Expect both resend + notify calls
        calls = hass.services.async_call.call_args_list
        domains = [c[0][0] for c in calls]
        assert "notify" in domains

    @pytest.mark.asyncio
    async def test_try_recover_no_alert_without_notify_service(self) -> None:
        """No alert when notify_service is empty."""
        adapter, hass = _adapter(**{f"sensor.{PREFIX}_dynamic_charger_current": "6.0"})
        adapter.notify_service = ""
        await adapter.set_current(8)
        adapter._target_set_at = time.monotonic() - (_ALERT_THRESHOLD_S + 5)

        await adapter.try_recover()

        calls = hass.services.async_call.call_args_list
        domains = [c[0][0] for c in calls]
        assert "notify" not in domains

    @pytest.mark.asyncio
    async def test_set_current_resets_alert_timer(self) -> None:
        """Fresh set_current() resets alert state."""
        adapter, _ = _adapter()
        adapter._alert_sent_at = time.monotonic() - 1000
        await adapter.set_current(8)
        assert adapter._alert_sent_at is None
