"""Integration tests — verify HA setup/unload with real HA framework."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.carmabox.const import DOMAIN

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
async def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create and add a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="CARMA Box Test",
        data={"detected": {}},
        options={
            "target_weighted_kw": 2.0,
            "min_soc": 15.0,
            "grid_entity": "sensor.test_grid",
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    """Test that async_setup_entry loads the integration."""
    with patch(
        "custom_components.carmabox.coordinator.CarmaboxCoordinator._async_update_data",
        return_value=None,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state == ConfigEntryState.LOADED


async def test_unload_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    """Test that async_unload_entry cleans up properly."""
    with patch(
        "custom_components.carmabox.coordinator.CarmaboxCoordinator._async_update_data",
        return_value=None,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    result = await hass.config_entries.async_unload(config_entry.entry_id)
    assert result is True
    assert config_entry.state == ConfigEntryState.NOT_LOADED


async def test_options_update_triggers_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    """Test that changing options reloads the integration."""
    with patch(
        "custom_components.carmabox.coordinator.CarmaboxCoordinator._async_update_data",
        return_value=None,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state == ConfigEntryState.LOADED

    # Update options
    with patch(
        "custom_components.carmabox.coordinator.CarmaboxCoordinator._async_update_data",
        return_value=None,
    ):
        hass.config_entries.async_update_entry(
            config_entry, options={**config_entry.options, "target_weighted_kw": 3.0}
        )
        await hass.async_block_till_done()

    # Should still be loaded (reloaded)
    assert config_entry.state == ConfigEntryState.LOADED
