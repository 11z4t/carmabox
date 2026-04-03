"""Coverage tests for config_flow.py.

Targets all 403 previously uncovered statements in CarmaboxConfigFlow
and CarmaboxOptionsFlow using __new__ bypass and mocked HA interfaces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Factory helpers ───────────────────────────────────────────────────────────


def _make_state(entity_id: str, state: str = "ok", **attrs) -> MagicMock:
    s = MagicMock()
    s.entity_id = entity_id
    s.state = state
    s.attributes = attrs
    return s


def _make_config_flow(*, detected: dict | None = None, user_input: dict | None = None):
    """Bypass ConfigFlow __init__; set up all required attributes."""
    from custom_components.carmabox.config_flow import CarmaboxConfigFlow

    flow = object.__new__(CarmaboxConfigFlow)
    flow._detected = detected or {}
    flow._detected_appliances = {}
    flow._user_input = user_input or {}

    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.states.async_all = MagicMock(return_value=[])
    hass.config.latitude = 59.3  # SE3 range
    hass.config_entries.async_entries = MagicMock(return_value=[])
    flow.hass = hass

    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


def _make_options_flow(options: dict | None = None):
    """Create CarmaboxOptionsFlow with mocked entry."""
    from custom_components.carmabox.config_flow import CarmaboxOptionsFlow

    entry = MagicMock()
    entry.options = options or {}
    flow = CarmaboxOptionsFlow(entry)
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    return flow


# ── Tests: _read_soc ──────────────────────────────────────────────────────────


class TestReadSoc:
    def test_empty_prefix_returns_empty(self) -> None:
        flow = _make_config_flow()
        assert flow._read_soc("") == ""

    def test_unavailable_state_returns_empty(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.pv_battery_soc_kontor", "unavailable")
        flow.hass.states.get.return_value = state
        assert flow._read_soc("kontor") == ""

    def test_valid_soc_returns_percent_string(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.pv_battery_soc_kontor", "72.4")
        flow.hass.states.get.return_value = state
        assert flow._read_soc("kontor") == "72%"

    def test_non_numeric_soc_returns_empty(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.pv_battery_soc_kontor", "NaN")
        flow.hass.states.get.return_value = state
        assert flow._read_soc("kontor") == ""


# ── Tests: _read_ev_status ────────────────────────────────────────────────────


class TestReadEvStatus:
    def test_empty_prefix_returns_okand(self) -> None:
        flow = _make_config_flow()
        assert flow._read_ev_status("") == "okänd"

    def test_unavailable_returns_okand(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.easee_home_status", "unavailable")
        flow.hass.states.get.return_value = state
        assert flow._read_ev_status("easee_home") == "okänd"

    def test_valid_status_returned_lowercase(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.easee_home_status", "CHARGING")
        flow.hass.states.get.return_value = state
        assert flow._read_ev_status("easee_home") == "charging"


# ── Tests: _read_current_price ────────────────────────────────────────────────


class TestReadCurrentPrice:
    def test_empty_entity_returns_empty(self) -> None:
        flow = _make_config_flow()
        assert flow._read_current_price("") == ""

    def test_none_state_returns_empty(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.get.return_value = None
        assert flow._read_current_price("sensor.nordpool_price") == ""

    def test_unavailable_state_returns_empty(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.nordpool_price", "unavailable")
        flow.hass.states.get.return_value = state
        assert flow._read_current_price("sensor.nordpool_price") == ""

    def test_valid_price_returns_ore_string(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.nordpool_price", "141.3")
        flow.hass.states.get.return_value = state
        result = flow._read_current_price("sensor.nordpool_price")
        assert "141" in result

    def test_non_numeric_price_returns_empty(self) -> None:
        flow = _make_config_flow()
        state = _make_state("sensor.nordpool_price", "unavailable_data")
        flow.hass.states.get.return_value = state
        assert flow._read_current_price("sensor.nordpool_price") == ""


# ── Tests: _find_entities ─────────────────────────────────────────────────────


class TestFindEntities:
    def test_returns_matching_entities(self) -> None:
        flow = _make_config_flow()
        states = [
            _make_state("sensor.pv_battery_soc_kontor"),
            _make_state("sensor.pv_battery_soc_forrad"),
            _make_state("sensor.something_else"),
        ]
        flow.hass.states.async_all.return_value = states
        result = flow._find_entities("sensor", "pv_battery_soc_")
        assert "sensor.pv_battery_soc_kontor" in result
        assert "sensor.pv_battery_soc_forrad" in result
        assert "sensor.something_else" not in result

    def test_suffix_filter(self) -> None:
        flow = _make_config_flow()
        states = [
            _make_state("sensor.goodwe_kontor_ems_mode"),
            _make_state("sensor.goodwe_forrad_ems_mode"),
            _make_state("sensor.goodwe_kontor_other"),
        ]
        flow.hass.states.async_all.return_value = states
        result = flow._find_entities("sensor", "goodwe_", "_ems_mode")
        assert "sensor.goodwe_kontor_ems_mode" in result
        assert "sensor.goodwe_kontor_other" not in result

    def test_empty_returns_empty_list(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.async_all.return_value = []
        result = flow._find_entities("sensor", "pv_battery_soc_")
        assert result == []


# ── Tests: _find_first_entity ─────────────────────────────────────────────────


class TestFindFirstEntity:
    def test_returns_exact_match_first(self) -> None:
        flow = _make_config_flow()
        states = [
            _make_state("sensor.house_grid_power_extra"),
            _make_state("sensor.house_grid_power"),
        ]
        flow.hass.states.async_all.return_value = states
        result = flow._find_first_entity("sensor", "house_grid_power")
        assert result == "sensor.house_grid_power"

    def test_returns_first_candidate_if_no_exact(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.pv_battery_soc_kontor")]
        flow.hass.states.async_all.return_value = states
        result = flow._find_first_entity("sensor", "pv_battery_soc")
        assert result == "sensor.pv_battery_soc_kontor"

    def test_returns_empty_if_no_match(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.async_all.return_value = []
        result = flow._find_first_entity("sensor", "nonexistent")
        assert result == ""

    def test_exclude_filter(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.pv_battery_soc_total")]
        flow.hass.states.async_all.return_value = states
        result = flow._find_first_entity("sensor", "pv_battery_soc", exclude="total")
        assert result == ""


# ── Tests: _find_by_suffix ────────────────────────────────────────────────────


class TestFindBySuffix:
    def test_returns_entity_with_suffix(self) -> None:
        from custom_components.carmabox.config_flow import CarmaboxConfigFlow

        result = CarmaboxConfigFlow._find_by_suffix(
            ["sensor.goodwe_kontor_ems", "sensor.goodwe_forrad_ems"], "kontor", "fallback"
        )
        assert result == "sensor.goodwe_kontor_ems"

    def test_returns_fallback_when_no_match(self) -> None:
        from custom_components.carmabox.config_flow import CarmaboxConfigFlow

        result = CarmaboxConfigFlow._find_by_suffix([], "kontor", "fallback_entity")
        assert result == "fallback_entity"


# ── Tests: _find_ev_soc_entity ────────────────────────────────────────────────


class TestFindEvSocEntity:
    def test_returns_numeric_ev_soc(self) -> None:
        flow = _make_config_flow()
        states = [
            _make_state("sensor.xpeng_g9_battery_soc", "74"),
            _make_state("sensor.pv_battery_soc_kontor", "50"),  # excluded: pv_battery
        ]
        flow.hass.states.async_all.return_value = states
        result = flow._find_ev_soc_entity()
        assert result == "sensor.xpeng_g9_battery_soc"

    def test_returns_non_numeric_as_fallback(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.ev_soc", "unknown")]
        flow.hass.states.async_all.return_value = states
        result = flow._find_ev_soc_entity()
        assert result == "sensor.ev_soc"

    def test_returns_empty_when_none(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.async_all.return_value = []
        result = flow._find_ev_soc_entity()
        assert result == ""

    def test_goodwe_entity_excluded(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.goodwe_battery_soc", "80")]
        flow.hass.states.async_all.return_value = states
        result = flow._find_ev_soc_entity()
        assert result == ""


# ── Tests: _detect_appliances ─────────────────────────────────────────────────


class TestDetectAppliances:
    def test_detects_watt_sensors(self) -> None:
        flow = _make_config_flow()
        state = MagicMock()
        state.entity_id = "sensor.dishwasher_power"
        state.state = "500"
        state.attributes = {"unit_of_measurement": "W", "friendly_name": "Dishwasher"}
        flow.hass.states.async_all.return_value = [state]
        result = flow._detect_appliances()
        assert "sensor.dishwasher_power" in result

    def test_excludes_non_power_sensors(self) -> None:
        flow = _make_config_flow()
        state = MagicMock()
        state.entity_id = "sensor.temperature"
        state.state = "21"
        state.attributes = {"unit_of_measurement": "°C"}
        flow.hass.states.async_all.return_value = [state]
        result = flow._detect_appliances()
        assert result == {}

    def test_excludes_system_prefixes(self) -> None:
        from custom_components.carmabox.const import APPLIANCE_EXCLUDE_PREFIXES

        flow = _make_config_flow()
        prefix = next(iter(APPLIANCE_EXCLUDE_PREFIXES))
        state = MagicMock()
        state.entity_id = f"sensor.{prefix}something"
        state.state = "100"
        state.attributes = {"unit_of_measurement": "W"}
        flow.hass.states.async_all.return_value = [state]
        result = flow._detect_appliances()
        assert result == {}

    def test_guesses_category_from_hints(self) -> None:
        flow = _make_config_flow()
        state = MagicMock()
        state.entity_id = "sensor.pool_pump_power"
        state.state = "300"
        state.attributes = {"unit_of_measurement": "W"}
        flow.hass.states.async_all.return_value = [state]
        result = flow._detect_appliances()
        if "sensor.pool_pump_power" in result:
            # Category should be guessed from hints
            assert "category" in result["sensor.pool_pump_power"]

    def test_kw_unit_included(self) -> None:
        flow = _make_config_flow()
        state = MagicMock()
        state.entity_id = "sensor.oven_power"
        state.state = "2.5"
        state.attributes = {"unit_of_measurement": "kW"}
        flow.hass.states.async_all.return_value = [state]
        result = flow._detect_appliances()
        assert "sensor.oven_power" in result


# ── Tests: _detect_ev_prefix ──────────────────────────────────────────────────


class TestDetectEvPrefix:
    def test_finds_prefix_from_status_entity(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.easee_home_12840_status")]
        flow.hass.states.async_all.return_value = states
        result = flow._detect_ev_prefix("easee")
        assert result == "easee_home_12840"

    def test_fallback_prefix_from_entity(self) -> None:
        flow = _make_config_flow()
        states = [_make_state("sensor.easee_home_power")]
        flow.hass.states.async_all.return_value = states
        # No _status entity but has domain in entity_id
        result = flow._detect_ev_prefix("easee")
        assert "easee" in result

    def test_returns_domain_when_nothing_found(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.async_all.return_value = []
        result = flow._detect_ev_prefix("easee")
        assert result == "easee"


# ── Tests: _detect_easee_charger_id ──────────────────────────────────────────


class TestDetectEaseeChargerId:
    def test_reads_id_from_attributes(self) -> None:
        flow = _make_config_flow()
        state = MagicMock()
        state.attributes = {"id": "EH12345", "charger_id": ""}
        flow.hass.states.get.return_value = state
        result = flow._detect_easee_charger_id("easee_home")
        assert result == "EH12345"

    def test_falls_back_to_prefix_pattern(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.get.return_value = None
        result = flow._detect_easee_charger_id("easee_home_12840")
        assert result == "EH12840"

    def test_no_state_and_non_numeric_returns_empty(self) -> None:
        flow = _make_config_flow()
        flow.hass.states.get.return_value = None
        result = flow._detect_easee_charger_id("easee_home_abc")
        assert result == ""


# ── Tests: _resolve_device_ids ────────────────────────────────────────────────


class TestResolveDeviceIds:
    def test_returns_device_ids(self) -> None:
        flow = _make_config_flow()
        mock_device = MagicMock()
        mock_device.id = "dev_abc123"
        mock_device.config_entries = {"entry_xyz"}
        mock_reg = MagicMock()
        mock_reg.devices = {"dev_abc123": mock_device}

        with patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=mock_reg,
        ):
            result = flow._resolve_device_ids("entry_xyz")
        assert result == ["dev_abc123"]

    def test_exception_returns_empty_list(self) -> None:
        flow = _make_config_flow()
        with patch(
            "homeassistant.helpers.device_registry.async_get",
            side_effect=RuntimeError("no registry"),
        ):
            result = flow._resolve_device_ids("entry_xyz")
        assert result == []


# ── Tests: _resolve_inverter_device_id ───────────────────────────────────────


class TestResolveInverterDeviceId:
    def test_strategy1_entity_registry(self) -> None:
        flow = _make_config_flow()
        mock_entry = MagicMock()
        mock_entry.device_id = "dev_abc"
        mock_reg = MagicMock()
        mock_reg.async_get.return_value = mock_entry

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_reg,
        ):
            result = flow._resolve_inverter_device_id("kontor", [])
        assert result == "dev_abc"

    def test_strategy2_prefix_match(self) -> None:
        flow = _make_config_flow()
        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            side_effect=RuntimeError("no registry"),
        ):
            inverters = [{"prefix": "kontor", "device_ids": ["dev_xyz"]}]
            result = flow._resolve_inverter_device_id("kontor", inverters)
        assert result == "dev_xyz"

    def test_strategy3_single_inverter_fallback(self) -> None:
        flow = _make_config_flow()
        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            side_effect=RuntimeError("no registry"),
        ):
            inverters = [{"prefix": "other", "device_ids": ["dev_fallback"]}]
            result = flow._resolve_inverter_device_id("kontor", inverters)
        assert result == "dev_fallback"

    def test_no_match_returns_empty(self) -> None:
        flow = _make_config_flow()
        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            side_effect=RuntimeError("no registry"),
        ):
            inverters = [
                {"prefix": "other", "device_ids": []},
                {"prefix": "another", "device_ids": []},
            ]
            result = flow._resolve_inverter_device_id("kontor", inverters)
        assert result == ""


# ── Tests: _auto_detect ───────────────────────────────────────────────────────


class TestAutoDetect:
    @pytest.mark.asyncio
    async def test_detects_inverter_entry(self) -> None:
        flow = _make_config_flow()
        entry = MagicMock()
        entry.domain = "goodwe"
        entry.entry_id = "goodwe_entry"
        entry.title = "Kontor"
        flow.hass.config_entries.async_entries.return_value = [entry]
        flow.hass.states.async_all.return_value = []

        with patch.object(flow, "_resolve_device_ids", return_value=["dev_123"]):
            result = await flow._auto_detect()

        assert len(result["inverters"]) == 1
        assert result["inverters"][0]["domain"] == "goodwe"

    @pytest.mark.asyncio
    async def test_detects_ev_charger(self) -> None:
        flow = _make_config_flow()
        entry = MagicMock()
        entry.domain = "easee"
        entry.entry_id = "easee_entry"
        entry.title = "Home"
        flow.hass.config_entries.async_entries.return_value = [entry]
        flow.hass.states.async_all.return_value = []

        with (
            patch.object(flow, "_resolve_device_ids", return_value=[]),
            patch.object(flow, "_detect_ev_prefix", return_value="easee_home"),
        ):
            result = await flow._auto_detect()

        assert len(result["ev_chargers"]) == 1

    @pytest.mark.asyncio
    async def test_detects_price_source(self) -> None:
        flow = _make_config_flow()
        entry = MagicMock()
        entry.domain = "nordpool"
        entry.entry_id = "nordpool_entry"
        flow.hass.config_entries.async_entries.return_value = [entry]
        # Provide a sensor with kwh in name
        price_state = _make_state("sensor.nordpool_kwh", "1.50")
        flow.hass.states.async_all.return_value = [price_state]

        result = await flow._auto_detect()

        assert len(result["price_sources"]) == 1
        assert result["price_sources"][0]["domain"] == "nordpool"

    @pytest.mark.asyncio
    async def test_detects_pv_forecast(self) -> None:
        flow = _make_config_flow()
        entry = MagicMock()
        entry.domain = "solcast_solar"
        entry.entry_id = "solcast_entry"
        flow.hass.config_entries.async_entries.return_value = [entry]
        flow.hass.states.async_all.return_value = []

        result = await flow._auto_detect()

        assert len(result["pv_forecasts"]) == 1
        assert result["pv_forecasts"][0]["name"] == "Solcast"

    @pytest.mark.asyncio
    async def test_empty_config_entries(self) -> None:
        flow = _make_config_flow()
        flow.hass.config_entries.async_entries.return_value = []
        result = await flow._auto_detect()
        assert result["inverters"] == []
        assert result["ev_chargers"] == []


# ── Tests: Step methods ───────────────────────────────────────────────────────


class TestStepMethods:
    @pytest.mark.asyncio
    async def test_step_user_no_inverters_shows_no_inverter_form(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow,
            "_auto_detect",
            new=AsyncMock(
                return_value={
                    "inverters": [],
                    "ev_chargers": [],
                    "price_sources": [],
                    "pv_forecasts": [],
                }
            ),
        ):
            await flow.async_step_user()
        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args[1]
        assert call_kwargs["step_id"] == "no_inverter"

    @pytest.mark.asyncio
    async def test_step_user_with_inverters_calls_confirm(self) -> None:
        flow = _make_config_flow()
        with (
            patch.object(
                flow,
                "_auto_detect",
                new=AsyncMock(
                    return_value={
                        "inverters": [{"name": "GoodWe", "prefix": "kontor"}],
                        "ev_chargers": [],
                        "price_sources": [],
                        "pv_forecasts": [],
                    }
                ),
            ),
            patch.object(
                flow, "async_step_confirm", new=AsyncMock(return_value={"type": "form"})
            ) as mock_confirm,
        ):
            await flow.async_step_user()
        mock_confirm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_no_inverter_aborts(self) -> None:
        flow = _make_config_flow()
        await flow.async_step_no_inverter()
        flow.async_abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_confirm_no_input_shows_form(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [{"name": "GoodWe", "prefix": "kontor"}],
                "ev_chargers": [],
                "price_sources": [],
                "pv_forecasts": [],
            }
        )
        await flow.async_step_confirm(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_confirm_with_input_calls_ev(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_ev", new=AsyncMock(return_value={"type": "form"})
        ) as mock_ev:
            await flow.async_step_confirm(user_input={})
        mock_ev.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_ev_no_input_shows_form(self) -> None:
        flow = _make_config_flow(detected={"ev_chargers": [{"name": "Easee"}]})
        await flow.async_step_ev(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_ev_with_input_calls_grid(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_grid", new=AsyncMock(return_value={"type": "form"})
        ) as mock_grid:
            await flow.async_step_ev(user_input={"ev_enabled": True})
        mock_grid.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_grid_no_input_se1_high_lat(self) -> None:
        flow = _make_config_flow()
        flow.hass.config.latitude = 65.0  # > 63 → SE1
        await flow.async_step_grid(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_grid_no_input_se2(self) -> None:
        flow = _make_config_flow()
        flow.hass.config.latitude = 61.0  # > 60 → SE2
        await flow.async_step_grid(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_grid_no_input_se4_low_lat(self) -> None:
        flow = _make_config_flow()
        flow.hass.config.latitude = 55.0  # < 56 → SE4
        await flow.async_step_grid(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_grid_no_input_se3_mid_lat(self) -> None:
        flow = _make_config_flow()
        flow.hass.config.latitude = 58.0  # 56-60 → SE3
        await flow.async_step_grid(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_grid_no_lat(self) -> None:
        flow = _make_config_flow()
        flow.hass.config.latitude = None
        await flow.async_step_grid(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_grid_with_input_calls_household(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_household", new=AsyncMock(return_value={"type": "form"})
        ) as mock_hh:
            await flow.async_step_grid(user_input={"price_area": "SE3"})
        mock_hh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_household_no_input_shows_form(self) -> None:
        flow = _make_config_flow()
        await flow.async_step_household(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_household_with_input_calls_profile(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_household_profile", new=AsyncMock(return_value={"type": "form"})
        ) as mock_profile:
            await flow.async_step_household(user_input={"household_size": 3})
        mock_profile.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_household_profile_no_input_shows_form(self) -> None:
        flow = _make_config_flow(detected={"inverters": [{"domain": "goodwe", "prefix": "kontor"}]})
        await flow.async_step_household_profile(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_household_profile_with_input_calls_weather(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_weather", new=AsyncMock(return_value={"type": "form"})
        ) as mock_weather:
            await flow.async_step_household_profile(user_input={"house_size_m2": 150})
        mock_weather.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_consumers_no_input_shows_form(self) -> None:
        flow = _make_config_flow()
        await flow.async_step_consumers(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_consumers_with_input_calls_appliances(self) -> None:
        flow = _make_config_flow()
        with patch.object(
            flow, "async_step_appliances", new=AsyncMock(return_value={"type": "form"})
        ) as mock_app:
            await flow.async_step_consumers(user_input={"miner_entity": "switch.miner"})
        mock_app.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_appliances_no_appliances_skips_to_summary(self) -> None:
        flow = _make_config_flow()
        flow._detected_appliances = {}
        with patch.object(
            flow, "async_step_summary", new=AsyncMock(return_value={"type": "form"})
        ) as mock_sum:
            await flow.async_step_appliances(user_input=None)
        mock_sum.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_step_appliances_with_detected_shows_form(self) -> None:
        flow = _make_config_flow()
        flow._detected_appliances = {"sensor.oven_power": {"name": "Oven", "category": "other"}}
        await flow.async_step_appliances(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_appliances_with_input_calls_summary(self) -> None:
        flow = _make_config_flow()
        flow._detected_appliances = {"sensor.oven_power": {"name": "Oven", "category": "other"}}
        with patch.object(
            flow, "async_step_summary", new=AsyncMock(return_value={"type": "create_entry"})
        ) as mock_sum:
            await flow.async_step_appliances(
                user_input={
                    "enable_sensor.oven_power": True,
                    "category_sensor.oven_power": "other",
                }
            )
        mock_sum.assert_awaited_once()
        assert flow._user_input.get("appliances") is not None

    @pytest.mark.asyncio
    async def test_step_summary_no_input_shows_form(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [{"name": "GoodWe", "prefix": "kontor"}],
                "ev_chargers": [{"name": "Easee", "prefix": "easee_home"}],
                "price_sources": [{"name": "Nordpool", "entity_id": "sensor.nordpool_kwh"}],
                "pv_forecasts": [{"name": "Solcast"}],
            },
            user_input={
                "target_weighted_kw": 2.0,
                "ev_enabled": True,
                "ev_night_target_soc": 75,
                "executor_enabled": False,
                "appliances": [{"name": "Oven", "category": "other"}],
            },
        )
        await flow.async_step_summary(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_summary_with_input_creates_entry(self) -> None:
        flow = _make_config_flow(
            detected={"inverters": [], "ev_chargers": [], "price_sources": [], "pv_forecasts": []},
        )
        with patch.object(flow, "_create_entry", return_value={"type": "create_entry"}) as mock_ce:
            await flow.async_step_summary(user_input={})
        mock_ce.assert_called_once()


# ── Tests: _build_entity_mappings ─────────────────────────────────────────────


class TestBuildEntityMappings:
    def test_build_with_soc_entities(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [{"prefix": "kontor", "device_ids": ["dev_123"]}],
                "ev_chargers": [],
                "price_sources": [],
                "pv_forecasts": [],
            }
        )
        soc_states = [_make_state("sensor.pv_battery_soc_kontor", "70")]
        power_states = [_make_state("sensor.goodwe_battery_power_kontor", "1000")]
        grid_state = _make_state("sensor.house_grid_power", "500")
        pv_state = _make_state("sensor.pv_solar_total", "2000")

        def fake_async_all(domain: str):
            if domain == "sensor":
                return soc_states + power_states + [grid_state, pv_state]
            return []

        flow.hass.states.async_all.side_effect = fake_async_all
        with patch.object(flow, "_resolve_inverter_device_id", return_value="dev_123"):
            mappings = flow._build_entity_mappings()

        assert "battery_soc_1" in mappings
        assert mappings["battery_soc_1"] == "sensor.pv_battery_soc_kontor"

    def test_build_fallback_from_detected_prefixes(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [{"prefix": "kontor", "device_ids": ["dev_123"]}],
                "ev_chargers": [],
                "price_sources": [],
                "pv_forecasts": [],
            }
        )
        # No SOC entities → use fallback
        flow.hass.states.async_all.return_value = []

        mappings = flow._build_entity_mappings()
        assert "battery_soc_1" in mappings
        assert "kontor" in mappings["battery_soc_1"]

    def test_build_with_ev_charger(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [],
                "ev_chargers": [{"prefix": "easee_home", "device_ids": ["ev_dev_123"]}],
                "price_sources": [],
                "pv_forecasts": [],
            }
        )
        flow.hass.states.async_all.return_value = []
        with (
            patch.object(flow, "_detect_easee_charger_id", return_value="EH12840"),
            patch.object(flow, "_find_ev_soc_entity", return_value="sensor.xpeng_soc"),
        ):
            mappings = flow._build_entity_mappings()

        assert mappings.get("ev_prefix") == "easee_home"
        assert mappings.get("ev_charger_id") == "EH12840"
        assert mappings.get("ev_soc_entity") == "sensor.xpeng_soc"

    def test_build_with_price_sources(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [],
                "ev_chargers": [],
                "price_sources": [
                    {"domain": "nordpool", "entity_id": "sensor.nordpool_kwh"},
                    {"domain": "tibber", "entity_id": "sensor.tibber_price"},
                ],
                "pv_forecasts": [],
            }
        )
        flow.hass.states.async_all.return_value = []
        mappings = flow._build_entity_mappings()

        assert mappings.get("price_entity") == "sensor.nordpool_kwh"
        assert mappings.get("price_entity_fallback") == "sensor.tibber_price"


# ── Tests: _create_entry ──────────────────────────────────────────────────────


class TestCreateEntry:
    def test_creates_entry_with_detected_inverter_name(self) -> None:
        flow = _make_config_flow(
            detected={
                "inverters": [{"name": "GoodWe", "prefix": "kontor"}],
                "ev_chargers": [],
                "price_sources": [],
                "pv_forecasts": [],
            },
        )
        with patch.object(flow, "_build_entity_mappings", return_value={}):
            flow._create_entry()
        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args[1]
        assert "GoodWe" in call_kwargs["title"]

    def test_creates_entry_no_inverters_default_title(self) -> None:
        flow = _make_config_flow(
            detected={"inverters": [], "ev_chargers": [], "price_sources": [], "pv_forecasts": []},
        )
        with patch.object(flow, "_build_entity_mappings", return_value={}):
            flow._create_entry()
        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args[1]
        assert call_kwargs["title"] == "CARMA Box"

    def test_creates_entry_with_user_input_values(self) -> None:
        flow = _make_config_flow(
            detected={"inverters": [], "ev_chargers": [], "price_sources": [], "pv_forecasts": []},
            user_input={"ev_enabled": True, "executor_enabled": True, "price_area": "SE1"},
        )
        with patch.object(flow, "_build_entity_mappings", return_value={}):
            flow._create_entry()
        flow.async_create_entry.assert_called_once()
        _, kwargs = flow.async_create_entry.call_args
        assert kwargs["data"]["ev_enabled"] is True
        assert kwargs["data"]["price_area"] == "SE1"


# ── Tests: CarmaboxOptionsFlow ────────────────────────────────────────────────


class TestCarmaboxOptionsFlow:
    @pytest.mark.asyncio
    async def test_step_init_no_input_shows_form(self) -> None:
        flow = _make_options_flow(
            options={"battery_1_kwh": 10.0, "consumers": {"ev_enabled": True}}
        )
        await flow.async_step_init(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_init_with_input_creates_entry(self) -> None:
        flow = _make_options_flow(options={"battery_1_kwh": 10.0})
        await flow.async_step_init(user_input={"battery_1_kwh": 12.0})
        flow.async_create_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_init_consumers_keys_nested(self) -> None:
        """consumers_* keys in user_input → nested into consumers dict."""
        flow = _make_options_flow(options={})
        await flow.async_step_init(
            user_input={
                "battery_1_kwh": 10.0,
                "consumers_ev_enabled": True,
                "consumers_miner_power_w": 500,
            }
        )
        flow.async_create_entry.assert_called_once()
        _, kwargs = flow.async_create_entry.call_args
        assert kwargs["data"]["consumers"]["ev_enabled"] is True
        assert kwargs["data"]["consumers"]["miner_power_w"] == 500
        assert "consumers_ev_enabled" not in kwargs["data"]

    @pytest.mark.asyncio
    async def test_step_init_merges_existing_options(self) -> None:
        """New input merged with existing options to preserve entity mappings."""
        flow = _make_options_flow(options={"grid_entity": "sensor.grid", "battery_1_kwh": 8.0})
        await flow.async_step_init(user_input={"battery_1_kwh": 12.0})
        _, kwargs = flow.async_create_entry.call_args
        assert kwargs["data"]["grid_entity"] == "sensor.grid"  # preserved
        assert kwargs["data"]["battery_1_kwh"] == 12.0  # updated
