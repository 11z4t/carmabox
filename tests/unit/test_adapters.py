"""Tests for CARMA Box adapters — GoodWe, Easee, Nordpool."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.adapters.easee import EaseeAdapter
from custom_components.carmabox.adapters.goodwe import GoodWeAdapter
from custom_components.carmabox.adapters.nordpool import NordpoolAdapter


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    """Create mock hass with states."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state

    def get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = get_state
    hass.services.async_call = AsyncMock()
    return hass


class TestGoodWeAdapter:
    def test_read_soc(self) -> None:
        hass = _make_hass(("sensor.pv_battery_soc_kontor", "85.0"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == 85.0

    def test_read_soc_unavailable(self) -> None:
        hass = _make_hass(("sensor.pv_battery_soc_kontor", "unavailable"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == 0.0

    def test_read_soc_missing_entity(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == 0.0

    def test_read_power(self) -> None:
        hass = _make_hass(("sensor.goodwe_battery_power_kontor", "-1500"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.power_w == -1500.0

    def test_read_ems_mode(self) -> None:
        hass = _make_hass(("select.goodwe_kontor_ems_mode", "charge_pv"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.ems_mode == "charge_pv"

    def test_read_fast_charging(self) -> None:
        hass = _make_hass(("switch.goodwe_fast_charging_switch_kontor", "on"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.fast_charging_on is True

    def test_read_fast_charging_off(self) -> None:
        hass = _make_hass(("switch.goodwe_fast_charging_switch_kontor", "off"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.fast_charging_on is False

    def test_read_temperature(self) -> None:
        hass = _make_hass(
            ("sensor.goodwe_battery_min_cell_temperature_kontor", "12.5")
        )
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.temperature_c == 12.5

    def test_read_temperature_unavailable(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.temperature_c is None

    @pytest.mark.asyncio
    async def test_set_ems_mode(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_ems_mode("battery_standby")
        hass.services.async_call.assert_called_once_with(
            "select", "select_option",
            {"entity_id": "select.goodwe_kontor_ems_mode", "option": "battery_standby"},
        )

    @pytest.mark.asyncio
    async def test_set_discharge_limit(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_discharge_limit(700)
        hass.services.async_call.assert_called_once_with(
            "number", "set_value",
            {"entity_id": "number.goodwe_kontor_ems_power_limit", "value": 700},
        )


class TestEaseeAdapter:
    def test_read_status(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_status", "charging"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.status == "charging"
        assert adapter.is_charging is True

    def test_read_current(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_current", "6.15"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.current_a == pytest.approx(6.15)

    def test_read_power(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_power", "1414"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.power_w == 1414.0

    def test_read_enabled(self) -> None:
        hass = _make_hass(("switch.easee_home_12840_is_enabled", "on"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.is_enabled is True

    def test_read_cable_locked(self) -> None:
        hass = _make_hass(("binary_sensor.easee_home_12840_cable_locked", "on"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.cable_locked is True

    @pytest.mark.asyncio
    async def test_set_current(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.set_current(10)
        hass.services.async_call.assert_called_once_with(
            "number", "set_value",
            {"entity_id": "number.easee_home_12840_dynamic_charger_limit", "value": 10},
        )

    @pytest.mark.asyncio
    async def test_set_current_clamps_max(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.set_current(50)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 32  # Clamped to max

    @pytest.mark.asyncio
    async def test_enable(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.enable()
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_on",
            {"entity_id": "switch.easee_home_12840_is_enabled"},
        )

    @pytest.mark.asyncio
    async def test_disable(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.disable()
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_off",
            {"entity_id": "switch.easee_home_12840_is_enabled"},
        )


class TestNordpoolAdapter:
    def _make_np_hass(
        self,
        current: str = "85.5",
        today: list[float] | None = None,
        tomorrow: list[float] | None = None,
        tomorrow_valid: bool = False,
    ) -> MagicMock:
        hass = MagicMock()
        state = MagicMock()
        state.state = current
        state.attributes = {
            "today": today or [],
            "tomorrow": tomorrow or [],
            "tomorrow_valid": tomorrow_valid,
        }
        hass.states.get = MagicMock(return_value=state)
        return hass

    def test_current_price(self) -> None:
        hass = self._make_np_hass("85.5")
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.current_price == 85.5

    def test_current_price_unavailable(self) -> None:
        hass = self._make_np_hass("unavailable")
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.current_price == 50.0  # Fallback

    def test_today_prices_24_entries(self) -> None:
        hass = self._make_np_hass(today=list(range(24)))
        adapter = NordpoolAdapter(hass, "sensor.np")
        prices = adapter.today_prices
        assert len(prices) == 24

    def test_today_prices_96_entries(self) -> None:
        """96 entries (15-min) should be averaged to 24 hourly."""
        raw = []
        for h in range(24):
            for _ in range(4):
                raw.append(float(h * 10))
        hass = self._make_np_hass(today=raw)
        adapter = NordpoolAdapter(hass, "sensor.np")
        prices = adapter.today_prices
        assert len(prices) == 24
        assert prices[0] == 0.0
        assert prices[10] == 100.0

    def test_today_prices_empty_fallback(self) -> None:
        hass = self._make_np_hass(today=[])
        adapter = NordpoolAdapter(hass, "sensor.np")
        prices = adapter.today_prices
        assert len(prices) == 24
        assert all(p == 50.0 for p in prices)

    def test_tomorrow_prices_valid(self) -> None:
        hass = self._make_np_hass(
            tomorrow=list(range(24)),
            tomorrow_valid=True,
        )
        adapter = NordpoolAdapter(hass, "sensor.np")
        prices = adapter.tomorrow_prices
        assert prices is not None
        assert len(prices) == 24

    def test_tomorrow_prices_not_valid(self) -> None:
        hass = self._make_np_hass(tomorrow_valid=False)
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.tomorrow_prices is None

    def test_to_hourly_already_24(self) -> None:
        result = NordpoolAdapter._to_hourly(list(range(24)))
        assert len(result) == 24

    def test_to_hourly_empty(self) -> None:
        result = NordpoolAdapter._to_hourly([])
        assert result == []
