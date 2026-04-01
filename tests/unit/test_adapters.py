"""Tests for CARMA Box adapters — GoodWe, Easee, Nordpool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

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
        assert adapter.soc == -1.0  # -1 signals unavailable

    def test_read_soc_missing_entity(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == -1.0  # -1 signals unavailable

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
        hass = _make_hass(("sensor.goodwe_battery_min_cell_temperature_kontor", "12.5"))
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
        await adapter.set_ems_mode("battery_standby", verify=False)
        # Writes legacy desired_mode + EMS mode select
        calls = [c[0] for c in hass.services.async_call.call_args_list]
        assert (
            "input_select",
            "select_option",
            {"entity_id": "input_select.goodwe_kontor_desired_mode", "option": "wait"},
        ) in calls
        assert (
            "select",
            "select_option",
            {"entity_id": "select.goodwe_kontor_ems_mode", "option": "battery_standby"},
        ) in calls

    @pytest.mark.asyncio
    async def test_set_discharge_limit(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_discharge_limit(700)
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {"entity_id": "number.goodwe_kontor_ems_power_limit", "value": 700},
        )


class TestGoodWeBMSLimits:
    """EXP-02: BMS charge/discharge current limits and SoH."""

    def test_bms_discharge_limit(self) -> None:
        hass = _make_hass(("sensor.goodwe_battery_discharge_limit_kontor", "22.0"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.bms_discharge_limit_a == 22.0

    def test_bms_charge_limit_cold(self) -> None:
        """At 5C, BMS may limit charge to 1A."""
        hass = _make_hass(("sensor.goodwe_battery_charge_limit_kontor", "1.0"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.bms_charge_limit_a == 1.0

    def test_bms_limits_unavailable_returns_zero(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.bms_discharge_limit_a == 0.0
        assert adapter.bms_charge_limit_a == 0.0

    def test_max_discharge_w_from_bms(self) -> None:
        """max_discharge_w = bms_discharge_limit_a x voltage."""
        hass = _make_hass(
            ("sensor.goodwe_battery_discharge_limit_kontor", "22.0"),
            ("sensor.goodwe_battery_voltage_kontor", "399.2"),
        )
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.max_discharge_w == int(22.0 * 399.2)  # 8782W

    def test_max_discharge_w_zero_when_bms_zero(self) -> None:
        hass = _make_hass(
            ("sensor.goodwe_battery_discharge_limit_kontor", "0"),
            ("sensor.goodwe_battery_voltage_kontor", "400.0"),
        )
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.max_discharge_w == 0

    def test_max_charge_w_cold_limited(self) -> None:
        """At 5C, charge limit 1A x 400V = 400W."""
        hass = _make_hass(
            ("sensor.goodwe_battery_charge_limit_kontor", "1.0"),
            ("sensor.goodwe_battery_voltage_kontor", "400.0"),
        )
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.max_charge_w == 400

    def test_voltage_default(self) -> None:
        """voltage defaults to 400V when unavailable."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.voltage == 400.0

    def test_soh_pct(self) -> None:
        hass = _make_hass(("sensor.goodwe_battery_soh_kontor", "98.0"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soh_pct == 98.0

    def test_soh_default_100(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soh_pct == 100.0


class TestGoodWeWriteVerification:
    """EXP-08: Write verification with read-back."""

    @pytest.mark.asyncio
    async def test_set_ems_mode_verify_success(self) -> None:
        """Write + verify succeeds when read-back matches."""
        hass = _make_hass(("select.goodwe_kontor_ems_mode", "discharge_pv"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("discharge_pv", verify=True)
        assert result is True

    @pytest.mark.asyncio
    async def test_set_ems_mode_no_verify(self) -> None:
        """verify=False skips read-back."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("charge_pv", verify=False)
        assert result is True
        # 2 calls: legacy desired_mode + EMS mode select (no retry)
        assert hass.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_set_ems_mode_verify_mismatch_retries(self) -> None:
        """Read-back mismatch triggers retry."""
        # State returns wrong mode — verify will fail both times
        hass = _make_hass(("select.goodwe_kontor_ems_mode", "charge_pv"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("discharge_pv", verify=True)
        # Returns False because state never matches
        assert result is False
        # 3 calls: desired_mode + ems_mode + retry ems_mode
        assert hass.services.async_call.call_count == 3

    @pytest.mark.asyncio
    async def test_set_ems_mode_invalid_rejected(self) -> None:
        """Invalid mode rejected before any write."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("invalid_mode")
        assert result is False
        hass.services.async_call.assert_not_called()


class TestGoodWePeakShavingLimit:
    """EXP-03: set_peak_shaving_limit writes to correct entity with clamp."""

    @pytest.mark.asyncio
    async def test_set_peak_shaving_limit(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_peak_shaving_limit(500)
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {"entity_id": "number.goodwe_kontor_peak_shaving_power_limit", "value": 500},
        )

    @pytest.mark.asyncio
    async def test_set_peak_shaving_limit_clamp_max(self) -> None:
        """Values > 10000W clamped to 10000W."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_peak_shaving_limit(15000)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 10000

    @pytest.mark.asyncio
    async def test_set_peak_shaving_limit_clamp_min(self) -> None:
        """Negative values clamped to 0W."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_peak_shaving_limit(-100)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 0

    @pytest.mark.asyncio
    async def test_set_peak_shaving_limit_zero(self) -> None:
        """0W = battery covers ALL grid import."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_peak_shaving_limit(0)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 0


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
        """Easee reports kW, adapter returns W."""
        hass = _make_hass(("sensor.easee_home_12840_power", "1.414"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.power_w == pytest.approx(1414.0)

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
        # Last call should be the dynamic limit set (after ensure_initialized)
        hass.services.async_call.assert_any_call(
            "number",
            "set_value",
            {"entity_id": "number.easee_home_12840_dynamic_charger_limit", "value": 10},
        )

    @pytest.mark.asyncio
    async def test_set_current_clamps_max(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.set_current(50)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == 10  # S5: Hard cap at 10A (safety)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "input_amps,expected",
        [
            (6, 6),  # AC5: minimum — pass through
            (8, 8),  # AC3: mid-range — pass through
            (10, 10),  # AC5: at DEFAULT_EV_MAX_AMPS — pass through
            (11, 10),  # AC1/AC5: just above max — clamped to 10A
            (16, 10),  # AC1: 16A would blow fuse — clamped to 10A
            (32, 10),  # AC2: old max — clamped to 10A
        ],
    )
    async def test_set_current_safety_clamp(self, input_amps, expected) -> None:
        """PLAT-1009: Defense-in-depth — adapter clamps to DEFAULT_EV_MAX_AMPS."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.set_current(input_amps)
        call = hass.services.async_call.call_args
        assert call[0][2]["value"] == expected

    @pytest.mark.asyncio
    async def test_enable(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.enable()
        hass.services.async_call.assert_any_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.easee_home_12840_is_enabled"},
        )

    @pytest.mark.asyncio
    async def test_disable(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        await adapter.disable()
        hass.services.async_call.assert_any_call(
            "switch",
            "turn_off",
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
        assert adapter.current_price == 100.0  # Default fallback

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
        assert all(p == 100.0 for p in prices)

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


# ── Additional coverage tests ────────────────────────────────────


class TestGoodWeAdapterEdgeCases:
    def test_read_float_invalid_string(self) -> None:
        hass = _make_hass(("sensor.pv_battery_soc_kontor", "not_a_number"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == -1.0  # -1 signals unavailable/invalid

    def test_read_str_unavailable(self) -> None:
        hass = _make_hass(("select.goodwe_kontor_ems_mode", "unavailable"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.ems_mode == ""

    @pytest.mark.asyncio
    async def test_set_fast_charging_on(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_fast_charging(on=True, power_pct=80, soc_target=90, authorized=True)
        calls = hass.services.async_call.call_args_list
        assert len(calls) == 3  # switch + power + soc
        assert calls[0][0][1] == "turn_on"
        assert calls[1][0][2]["value"] == 80
        assert calls[2][0][2]["value"] == 90

    @pytest.mark.asyncio
    async def test_set_fast_charging_off(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        await adapter.set_fast_charging(on=False)
        calls = hass.services.async_call.call_args_list
        assert len(calls) == 1  # only switch
        assert calls[0][0][1] == "turn_off"


class TestEaseeAdapterEdgeCases:
    def test_read_float_invalid(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_current", "abc"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.current_a == 0.0

    def test_read_str_unavailable(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_status", "unavailable"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.status == ""

    def test_dynamic_limit(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_dynamic_charger_limit", "16"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.dynamic_limit_a == 16.0


class TestNordpoolAdapterEdgeCases:
    def test_current_price_invalid(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "not_a_number"
        state.attributes = {}
        hass.states.get = MagicMock(return_value=state)
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.current_price == 100.0  # Default fallback

    def test_attrs_missing_entity(self) -> None:
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.today_prices == [100.0] * 24

    def test_custom_fallback_price(self) -> None:
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = NordpoolAdapter(hass, "sensor.np", fallback_price=75.0)
        assert adapter.current_price == 75.0
        assert adapter.today_prices == [75.0] * 24

    def test_tomorrow_prices_empty_list(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "50"
        state.attributes = {
            "today": list(range(24)),
            "tomorrow": [],
            "tomorrow_valid": True,
        }
        hass.states.get = MagicMock(return_value=state)
        adapter = NordpoolAdapter(hass, "sensor.np")
        assert adapter.tomorrow_prices is None

    def test_read_float_empty_string(self) -> None:
        hass = _make_hass(("sensor.easee_home_12840_current", ""))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        assert adapter.current_a == 0.0


class TestGoodWeSafeCall:
    """Error handling in GoodWe adapter _safe_call."""

    @pytest.mark.asyncio
    async def test_service_not_found(self) -> None:
        hass = _make_hass()
        hass.services.async_call = AsyncMock(side_effect=ServiceNotFound("select", "select_option"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("charge_pv")
        assert result is False

    @pytest.mark.asyncio
    async def test_ha_error_retries(self) -> None:
        hass = _make_hass()
        hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("modbus timeout"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        with patch("custom_components.carmabox.adapters.goodwe._RETRY_DELAY_S", 0):
            result = await adapter.set_ems_mode("charge_pv")
        assert result is False
        # 3 calls: 1 desired_mode (best-effort) + 2 EMS retries
        assert hass.services.async_call.call_count == 3

    @pytest.mark.asyncio
    async def test_unexpected_error_retries(self) -> None:
        hass = _make_hass()
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("unexpected"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        with patch("custom_components.carmabox.adapters.goodwe._RETRY_DELAY_S", 0):
            result = await adapter.set_ems_mode("charge_pv")
        assert result is False
        # 3 calls: 1 desired_mode (best-effort, raises) + 2 EMS retries
        assert hass.services.async_call.call_count == 3

    @pytest.mark.asyncio
    async def test_set_fast_charging_partial_failure(self) -> None:
        """If switch succeeds but power set fails → returns False."""
        hass = _make_hass()
        calls = 0

        async def side_effect(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:  # power_pct call (and retry) fails
                raise HomeAssistantError("fail")

        hass.services.async_call = AsyncMock(side_effect=side_effect)
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        with patch("custom_components.carmabox.adapters.goodwe._RETRY_DELAY_S", 0):
            result = await adapter.set_fast_charging(
                on=True,
                power_pct=80,
                soc_target=90,
                authorized=True,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_set_discharge_limit_returns_bool(self) -> None:
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_discharge_limit(500)
        assert result is True


class TestEaseeSafeCall:
    """Error handling in Easee adapter _safe_call."""

    @pytest.mark.asyncio
    async def test_service_not_found(self) -> None:
        hass = _make_hass()
        hass.services.async_call = AsyncMock(side_effect=ServiceNotFound("switch", "turn_on"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        result = await adapter.enable()
        assert result is False

    @pytest.mark.asyncio
    async def test_ha_error_retries(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        # Pre-initialize so ensure_initialized() won't run during test
        await adapter.ensure_initialized()
        hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("timeout"))
        with patch("custom_components.carmabox.adapters.easee._RETRY_DELAY_S", 0):
            result = await adapter.set_current(6)
        assert result is False
        assert hass.services.async_call.call_count == 2

    @pytest.mark.asyncio
    async def test_disable_returns_bool(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        result = await adapter.disable()
        assert result is True

    @pytest.mark.asyncio
    async def test_enable_returns_bool(self) -> None:
        # enable() verifies switch state is "on" after 1s — pre-seed the mock
        hass = _make_hass(("switch.easee_home_12840_is_enabled", "on"))
        adapter = EaseeAdapter(hass, "dev1", "easee_home_12840")
        with patch("asyncio.sleep"):
            result = await adapter.enable()
        assert result is True
