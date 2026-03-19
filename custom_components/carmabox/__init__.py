"""CARMA Box — Energy Optimizer for Home Assistant.

Connected Automated Resource Management Advisor.
Optimizes battery, EV charging, and grid import to minimize
electricity costs and peak power charges.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import CarmaboxCoordinator

CARD_JS = Path(__file__).parent / "dashboard" / "carmabox-card.js"
CARD_URL = "/carmabox/carmabox-card.js"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CARMA Box from a config entry."""
    _LOGGER.info("Setting up CARMA Box: %s", entry.title)

    coordinator = CarmaboxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register Lovelace card (may not be available during tests/reload)
    if hass.http is not None:
        try:
            hass.http.register_static_path(CARD_URL, str(CARD_JS), cache_headers=False)
        except Exception:
            _LOGGER.debug("Static path %s already registered", CARD_URL)

    _LOGGER.info("CARMA Box started: %s", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload CARMA Box config entry."""
    _LOGGER.info("Unloading CARMA Box: %s", entry.title)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload coordinator with new config."""
    _LOGGER.info("CARMA Box options updated, reloading")
    await hass.config_entries.async_reload(entry.entry_id)
