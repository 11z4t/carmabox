"""Tests for CARMA Box — Huawei Solar adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceNotFound

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


class TestHuaweiAdapterSetTargetW:
    """Tests for set_target_w() — forcible_* precision control."""

    @pytest.mark.asyncio
    async def test_positive_target_calls_forcible_discharge(self) -> None:
        """Positive target_w (CARMA discharge) → forcible_discharge_soc."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(1200.0)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "huawei_solar",
            "forcible_discharge_soc",
            {"device_id": "dev1", "target_soc": 0, "power": 1200},
        )

    @pytest.mark.asyncio
    async def test_negative_target_calls_forcible_charge(self) -> None:
        """Negative target_w (CARMA charge) → forcible_charge_soc."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(-800.0)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "huawei_solar",
            "forcible_charge_soc",
            {"device_id": "dev1", "target_soc": 100, "power": 800},
        )

    @pytest.mark.asyncio
    async def test_zero_target_calls_stop_forcible(self) -> None:
        """Zero target_w → stop_forcible_charge."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(0.0)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "huawei_solar",
            "stop_forcible_charge",
            {"device_id": "dev1"},
        )

    @pytest.mark.asyncio
    async def test_rounds_to_100w_up(self) -> None:
        """150W rounds to 200W (nearest 100)."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        await adapter.set_target_w(150.0)
        call = hass.services.async_call.call_args
        assert call[0][2]["power"] == 200

    @pytest.mark.asyncio
    async def test_rounds_to_100w_down(self) -> None:
        """49W rounds to 0 → stop_forcible."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        await adapter.set_target_w(49.0)
        hass.services.async_call.assert_called_once_with(
            "huawei_solar",
            "stop_forcible_charge",
            {"device_id": "dev1"},
        )

    @pytest.mark.asyncio
    async def test_discharge_fallback_to_working_mode_on_service_not_found(self) -> None:
        """ServiceNotFound on forcible_discharge → fallback to working_mode."""
        hass = _make_hass()
        hass.services.async_call = AsyncMock(
            side_effect=[ServiceNotFound("huawei_solar", "forcible_discharge_soc"), None]
        )
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(500.0)
        assert result is True
        # Second call must be select.select_option (working_mode fallback)
        second_call = hass.services.async_call.call_args_list[1]
        assert second_call[0][0] == "select"
        assert second_call[0][1] == "select_option"

    @pytest.mark.asyncio
    async def test_charge_fallback_to_working_mode_on_service_not_found(self) -> None:
        """ServiceNotFound on forcible_charge → fallback to working_mode."""
        hass = _make_hass()
        hass.services.async_call = AsyncMock(
            side_effect=[ServiceNotFound("huawei_solar", "forcible_charge_soc"), None]
        )
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(-500.0)
        assert result is True
        second_call = hass.services.async_call.call_args_list[1]
        assert second_call[0][0] == "select"

    @pytest.mark.asyncio
    async def test_stop_fallback_to_working_mode_on_service_not_found(self) -> None:
        """ServiceNotFound on stop_forcible → fallback to battery_standby."""
        hass = _make_hass()
        hass.services.async_call = AsyncMock(
            side_effect=[ServiceNotFound("huawei_solar", "stop_forcible_charge"), None]
        )
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_target_w(0.0)
        assert result is True
        second_call = hass.services.async_call.call_args_list[1]
        assert second_call[0][0] == "select"

    @pytest.mark.asyncio
    async def test_set_maximum_feed_grid_power(self) -> None:
        """set_maximum_feed_grid_power rounds to 100W and calls HA service."""
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_maximum_feed_grid_power(3750)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "huawei_solar",
            "set_maximum_feed_grid_power",
            {"device_id": "dev1", "power": 3800},
        )

    @pytest.mark.asyncio
    async def test_set_maximum_feed_grid_power_clamps_negative(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        await adapter.set_maximum_feed_grid_power(-100)
        call = hass.services.async_call.call_args
        assert call[0][2]["power"] == 0

    @pytest.mark.asyncio
    async def test_set_charge_limit(self) -> None:
        hass = _make_hass()
        adapter = HuaweiAdapter(hass, "dev1", "inv1")
        result = await adapter.set_charge_limit(2500)
        assert result is True
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": "number.storage_maximum_charging_power_inv1",
                "value": 2500,
            },
        )
