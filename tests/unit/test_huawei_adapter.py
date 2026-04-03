"""Tests for CARMA Box — Huawei Solar adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.adapters.huawei import HuaweiAdapter


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


class TestHuaweiAdapterRead:
    def test_read_soc(self) -> None:
        hass = _make_hass(("sensor.battery_state_of_capacity_inv1", "72.0"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.soc == 72.0

    def test_read_soc_unavailable(self) -> None:
        hass = _make_hass(("sensor.battery_state_of_capacity_inv1", "unavailable"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.soc == -1.0

    def test_read_soc_missing(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.soc == -1.0

    def test_read_soc_clamped(self) -> None:
        hass = _make_hass(("sensor.battery_state_of_capacity_inv1", "105"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.soc == 100.0

    def test_read_power_discharge(self) -> None:
        """Huawei positive=charge → CARMA positive=discharge (inverted)."""
        hass = _make_hass(("sensor.battery_charge_discharge_power_inv1", "1500"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.power_w == -1500.0  # Charging in CARMA convention

    def test_read_power_charge(self) -> None:
        hass = _make_hass(("sensor.battery_charge_discharge_power_inv1", "-2000"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.power_w == 2000.0  # Discharging in CARMA convention

    def test_read_ems_mode(self) -> None:
        hass = _make_hass(("select.batteries_working_mode_inv1", "maximise_self_consumption"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.ems_mode == "charge_pv"

    def test_read_ems_mode_tou(self) -> None:
        hass = _make_hass(("select.batteries_working_mode_inv1", "time_of_use"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.ems_mode == "discharge_pv"

    def test_read_ems_mode_unknown(self) -> None:
        hass = _make_hass(("select.batteries_working_mode_inv1", "something_new"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.ems_mode == "charge_pv"  # Default fallback

    def test_read_temperature(self) -> None:
        hass = _make_hass(("sensor.battery_temperature_inv1", "25.3"))
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.temperature_c == 25.3

    def test_read_temperature_unavailable(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        assert adapter.temperature_c is None

    def test_no_prefix(self) -> None:
        """Test adapter with empty prefix (single-inverter setup)."""
        hass = _make_hass(("sensor.battery_state_of_capacity", "90"))
        adapter = HuaweiAdapter(hass, "dev1", "")
        assert adapter.soc == 90.0


class TestHuaweiAdapterWrite:
    @pytest.mark.asyncio
    async def test_set_ems_mode(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_ems_mode("charge_pv")
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {
                "entity_id": "select.batteries_working_mode_inv1",
                "option": "maximise_self_consumption",
            },
        )

    @pytest.mark.asyncio
    async def test_set_ems_mode_discharge(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_ems_mode("discharge_pv")
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {
                "entity_id": "select.batteries_working_mode_inv1",
                "option": "time_of_use",
            },
        )

    @pytest.mark.asyncio
    async def test_set_ems_mode_invalid(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_ems_mode("invalid_mode")
        assert result is False
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_discharge_limit(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_discharge_limit(2000)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": "number.storage_maximum_discharging_power_inv1",
                "value": 2000,
            },
        )

    @pytest.mark.asyncio
    async def test_set_discharge_limit_negative_clamped(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        await adapter.set_discharge_limit(-100)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 0
