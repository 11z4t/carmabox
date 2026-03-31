"""CARMA Box — Energy Optimizer for Home Assistant.

Connected Automated Resource Management Advisor.
Optimizes battery, EV charging, and grid import to minimize
electricity costs and peak power charges.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)

from .const import DOMAIN as DOMAIN
from .const import PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

# IT-2466: Module reload REMOVED — caused silent failures that left
# coordinator in broken state (P0 2026-03-31). HA native reload handles this.

# PLAT-1144: _USE_BRIDGE removed — always use legacy coordinator
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

    # PLAT-992: Instant EV reaction when cable is plugged in
    cable_entity = coordinator.cable_locked_entity
    if cable_entity:
        _LOGGER.info("CARMA Box: watching %s for instant EV trigger", cable_entity)

        from homeassistant.core import Event

        @callback
        def _on_cable_change(event: Event[EventStateChangedData]) -> None:
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            if new is None:
                return
            was_locked = old is not None and old.state == "on"
            now_locked = new.state == "on"
            if now_locked and not was_locked:
                _LOGGER.info("CARMA Box: cable plugged in — triggering EV check")
                hass.async_create_task(
                    coordinator.on_ev_cable_connected(),
                    "carmabox_ev_cable_trigger",
                )

        unsub = async_track_state_change_event(hass, cable_entity, _on_cable_change)
        entry.async_on_unload(unsub)

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
