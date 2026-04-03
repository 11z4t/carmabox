"""Tests for CARMA Box — Zaptec EV charger adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.adapters.zaptec import ZaptecAdapter


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    """Create mock hass with states."""
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


class TestZaptecAdapterRead:
    def test_status_charging(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "connected_charging"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.status == "charging"

    def test_status_disconnected(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "disconnected"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.status == "disconnected"

    def test_status_requesting(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "connected_requesting"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.status == "awaiting_start"

    def test_status_finished(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "connected_finished"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.status == "completed"

    def test_is_charging(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "connected_charging"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.is_charging is True

    def test_is_not_charging(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "disconnected"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.is_charging is False

    def test_current(self) -> None:
        hass = _make_hass(("sensor.zap_charger_current", "8.5"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.current_a == 8.5

    def test_power_estimated(self) -> None:
        hass = _make_hass(("sensor.zap_charger_current", "10"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        # 10A x 230V x 3 phases = 6900W
        assert adapter.power_w == 6900.0

    def test_power_zero_when_no_current(self) -> None:
        hass = _make_hass(("sensor.zap_charger_current", "0.1"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.power_w == 0.0

    def test_plug_connected(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "connected_charging"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.plug_connected is True

    def test_plug_disconnected(self) -> None:
        hass = _make_hass(("sensor.zap_charger_mode", "disconnected"))
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.plug_connected is False

    def test_phase_count_default_3(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.phase_count == 3

    def test_phase_count_1_phase(self) -> None:
        hass = _make_hass(
            ("number.zap_3_to_1_phase_switch_current", "32"),
        )
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.phase_count == 1

    def test_phase_count_3_phase_explicit(self) -> None:
        hass = _make_hass(
            ("number.zap_3_to_1_phase_switch_current", "0"),
        )
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        assert adapter.phase_count == 3


class TestZaptecAdapterWrite:
    @pytest.mark.asyncio
    async def test_enable(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        result = await adapter.enable()
        assert result is True
        calls = [(c[0][0], c[0][1]) for c in hass.services.async_call.call_args_list]
        assert ("switch", "turn_on") in calls
        assert ("button", "press") in calls

    @pytest.mark.asyncio
    async def test_disable(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        result = await adapter.disable()
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "switch",
            "turn_off",
            {"entity_id": "switch.zap_charging"},
        )

    @pytest.mark.asyncio
    async def test_set_current(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap", installation_prefix="zap_install")
        result = await adapter.set_current(8)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": "number.zap_install_available_current",
                "value": 8,
            },
        )

    @pytest.mark.asyncio
    async def test_set_current_clamped_min(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        await adapter.set_current(2)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 6  # Clamped to min 6A

    @pytest.mark.asyncio
    async def test_set_current_clamped_max(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        await adapter.set_current(32)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 10  # Clamped to MAX_EV_CURRENT

    @pytest.mark.asyncio
    async def test_set_current_cooldown(self) -> None:
        """Second set_current within 15 min is skipped (rate limited)."""
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        await adapter.set_current(8)
        hass.services.async_call.reset_mock()
        # Second call within cooldown — should be skipped
        result = await adapter.set_current(10)
        assert result is True  # Returns True (not an error)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_to_default(self) -> None:
        hass = _make_hass()
        adapter = ZaptecAdapter(hass, "dev1", "zap")
        result = await adapter.reset_to_default()
        assert result is True
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 6
