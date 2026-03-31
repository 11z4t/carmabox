"""Coverage tests for config_flow.py remaining gaps — batch 23.

Targets:
  config_flow.py: 129-131, 169, 171, 173, 395-405, 690, 716, 1002
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_state(entity_id: str) -> MagicMock:
    s = MagicMock()
    s.entity_id = entity_id
    s.state = "ok"
    s.attributes = {}
    return s


def _make_config_flow(*, detected: dict | None = None, user_input: dict | None = None):
    from custom_components.carmabox.config_flow import CarmaboxConfigFlow

    flow = object.__new__(CarmaboxConfigFlow)
    flow._detected = detected or {}
    flow._detected_appliances = {}
    flow._user_input = user_input or {}

    hass = MagicMock()
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(return_value=None)
    hass.config.latitude = 59.3
    hass.config_entries.async_entries = MagicMock(return_value=[])
    flow.hass = hass

    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestConfigFlowInitBatch23:
    """Lines 129-131: __init__ initialises three instance attributes."""

    def test_init_sets_attributes(self) -> None:
        """Instantiate CarmaboxConfigFlow directly (with mocked HA parent __init__)."""
        from homeassistant.config_entries import ConfigFlow

        from custom_components.carmabox.config_flow import CarmaboxConfigFlow

        with patch.object(ConfigFlow, "__init__", lambda self: None):
            flow = CarmaboxConfigFlow()
        assert flow._detected == {}
        assert flow._detected_appliances == {}
        assert flow._user_input == {}


class TestConfigFlowConfirmBatch23:
    """Lines 169, 171, 173: detected_text.append for ev_chargers/price_sources/pv_forecasts."""

    @pytest.mark.asyncio
    async def test_confirm_appends_ev_price_pv(self) -> None:
        """async_step_confirm with ev/price/pv detected → lines 169/171/173."""
        flow = _make_config_flow(
            detected={
                "inverters": [{"name": "GoodWe ET"}],
                "ev_chargers": [{"name": "Easee Home"}],
                "price_sources": [{"name": "Nordpool"}],
                "pv_forecasts": [{"name": "Solcast"}],
            }
        )
        flow.async_step_ev = AsyncMock(return_value={"type": "form"})
        await flow.async_step_confirm(user_input=None)
        # async_show_form was called — verify detected_text was built correctly
        call_kwargs = flow.async_show_form.call_args[1]
        placeholders = call_kwargs["description_placeholders"]["detected"]
        assert "Easee Home" in placeholders
        assert "Nordpool" in placeholders
        assert "Solcast" in placeholders


class TestConfigFlowAppliancesBatch23:
    """Lines 395-405: schema building for appliances when appliances are detected."""

    @pytest.mark.asyncio
    async def test_appliances_schema_built_when_detected(self) -> None:
        """async_step_appliances with detected appliances → schema dict built (lines 395-405)."""
        flow = _make_config_flow()
        flow._detect_appliances = MagicMock(
            return_value={
                "sensor.washing_machine_power": {
                    "name": "Tvättmaskin",
                    "category": "washing",
                }
            }
        )
        # Call the actual step (prior show_form call was just setup noise)
        flow.async_show_form.reset_mock()
        await flow.async_step_appliances(user_input=None)
        assert flow.async_show_form.called
        call_kwargs = flow.async_show_form.call_args[1]
        assert call_kwargs["step_id"] == "appliances"


class TestConfigFlowBuildMappingsBatch23:
    """Lines 690 (empty prefix → continue) and 716 (battery temp entity found)."""

    def test_build_entity_mappings_empty_prefix_skipped(self) -> None:
        """Inverter with prefix='' → line 690 continue (no mapping added)."""
        flow = _make_config_flow(
            detected={
                "inverters": [{"prefix": "", "name": "Unknown"}],
            }
        )
        # No battery_soc entities in HA states → fallback branch
        flow.hass.states.async_all = MagicMock(return_value=[])
        mappings = flow._build_entity_mappings()
        # Empty prefix → skipped, no battery_soc_1 key added for this inverter
        assert "battery_soc_1" not in mappings

    def test_build_entity_mappings_temp_entity_found(self) -> None:
        """_find_first_entity finds temp sensor → line 716 sets battery_temp_entity."""
        flow = _make_config_flow(
            detected={"inverters": [{"prefix": "left", "name": "GoodWe ET"}]},
        )

        def _states_for_domain(domain: str) -> list:
            if domain == "sensor":
                return [
                    _make_state("sensor.pv_battery_min_temperature_left"),
                ]
            return []

        flow.hass.states.async_all = MagicMock(side_effect=_states_for_domain)
        mappings = flow._build_entity_mappings()
        assert mappings.get("battery_temp_entity") == "sensor.pv_battery_min_temperature_left"


class TestConfigFlowOptionsFlowBatch23:
    """Line 1002: async_get_options_flow returns CarmaboxOptionsFlow instance."""

    def test_async_get_options_flow(self) -> None:
        """Static method returns a CarmaboxOptionsFlow for the entry (line 1002)."""
        from custom_components.carmabox.config_flow import (
            CarmaboxConfigFlow,
            CarmaboxOptionsFlow,
        )

        mock_entry = MagicMock()
        mock_entry.options = {}
        result = CarmaboxConfigFlow.async_get_options_flow(mock_entry)
        assert isinstance(result, CarmaboxOptionsFlow)
