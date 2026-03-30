"""Tests for notification engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.notifications import (
    _THROTTLE,
    _THROTTLE_INTERVALS,
    CarmaNotifier,
    _throttled,
)


@pytest.fixture(autouse=True)
def _clear_throttle():
    """Clear throttle state between tests."""
    _THROTTLE.clear()
    yield
    _THROTTLE.clear()


class TestThrottle:
    def test_first_call_not_throttled(self) -> None:
        assert _throttled("battery_full") is False

    def test_second_call_throttled(self) -> None:
        _throttled("battery_full")
        assert _throttled("battery_full") is True

    def test_different_types_independent(self) -> None:
        _throttled("battery_full")
        assert _throttled("low_soc") is False

    def test_unknown_type_uses_default_interval(self) -> None:
        assert _throttled("unknown_type") is False
        assert _throttled("unknown_type") is True

    def test_throttle_intervals_defined(self) -> None:
        assert "battery_full" in _THROTTLE_INTERVALS
        assert "crosscharge_alert" in _THROTTLE_INTERVALS
        assert _THROTTLE_INTERVALS["crosscharge_alert"] == 300


class TestCarmaNotifier:
    def _make_notifier(self, enabled: bool = True) -> tuple[CarmaNotifier, MagicMock]:
        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        config = {
            "notifications_enabled": enabled,
            "notify_push_entity": "notify.test_push",
            "notify_slack_service": "rest_command.test_slack",
        }
        return CarmaNotifier(hass, config), hass

    @pytest.mark.asyncio
    async def test_battery_full(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.battery_full("Kontor", 100.0)
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "rest_command"
        assert call_args[0][1] == "test_slack"

    @pytest.mark.asyncio
    async def test_battery_full_throttled(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.battery_full("Kontor", 100.0)
        hass.services.async_call.reset_mock()
        await notifier.battery_full("Kontor", 100.0)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_notifications(self) -> None:
        notifier, hass = self._make_notifier(enabled=False)
        await notifier.battery_full("Kontor", 100.0)
        # Throttle passes but _send_slack returns early
        # No service call should be made
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_soc_warning(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.low_soc_warning("Förråd", 12.0, 15.0)
        # Should send both slack and push
        assert hass.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_discharge_blocked(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.discharge_blocked("Temperature too low")
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_proactive_discharge_started(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.proactive_discharge_started(1500, 85.0, 300.0, 2.5)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_miner_started(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.miner_started("solar surplus", 90.0, 25.0)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_miner_stopped(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.miner_stopped("price too high", 80.0, 120.0)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_ev_started(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.ev_started(8, 45.0, 75.0)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_ev_target_reached(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.ev_target_reached(75.0)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_crosscharge_alert(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.crosscharge_alert(500.0, -800.0)
        # Should send both slack and push
        assert hass.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_safety_block(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.safety_block("SoC below minimum")
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_morning_report(self) -> None:
        notifier, hass = self._make_notifier()
        await notifier.morning_report(85.0, 70.0, 60.0, 15.0, 8.0, 45.0)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_slack_exception_handled(self) -> None:
        notifier, hass = self._make_notifier()
        hass.services.async_call = AsyncMock(side_effect=Exception("Service unavailable"))
        # Should not raise
        await notifier.battery_full("Kontor", 100.0)

    @pytest.mark.asyncio
    async def test_send_push_exception_handled(self) -> None:
        notifier, hass = self._make_notifier()
        hass.services.async_call = AsyncMock(side_effect=Exception("Push failed"))
        # Should not raise
        await notifier.low_soc_warning("Kontor", 10.0, 15.0)
