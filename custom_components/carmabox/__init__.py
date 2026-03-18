"""CARMA Box — Energy Optimizer for Home Assistant.

Connected Automated Resource Management Advisor.
Optimizes battery, EV charging, and grid import to minimize
electricity costs and peak power charges.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import CarmaboxCoordinator

_LOGGER = logging.getLogger(__name__)

type CarmaboxConfigEntry = ConfigEntry[CarmaboxCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CarmaboxConfigEntry) -> bool:
    """Set up CARMA Box from a config entry."""
    _LOGGER.info("Setting up CARMA Box: %s", entry.title)

    coordinator = CarmaboxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates (live config changes)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info("CARMA Box started: %s", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CarmaboxConfigEntry) -> bool:
    """Unload CARMA Box config entry."""
    _LOGGER.info("Unloading CARMA Box: %s", entry.title)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(
    hass: HomeAssistant, entry: CarmaboxConfigEntry
) -> None:
    """Handle options update — reload coordinator with new config."""
    _LOGGER.info("CARMA Box options updated, reloading")
    await hass.config_entries.async_reload(entry.entry_id)
