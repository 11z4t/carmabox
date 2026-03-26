"""CARMA Box — Energy Optimizer for Home Assistant.

Connected Automated Resource Management Advisor.
Optimizes battery, EV charging, and grid import to minimize
electricity costs and peak power charges.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)

from .const import DOMAIN, PLATFORMS

# IT-2466: Invalidate module cache on reload to pick up hotfixes
for _mod in [
    "custom_components.carmabox.coordinator",
    "custom_components.carmabox.optimizer.scheduler",
    "custom_components.carmabox.optimizer.models",
    "custom_components.carmabox.optimizer.predictor",
]:
    if _mod in sys.modules:
        try:
            importlib.reload(sys.modules[_mod])
        except Exception:  # noqa: BLE001
            pass

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
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        _invalidate_module_cache()
    return ok


def _invalidate_module_cache() -> None:
    """IT-2466: Purge all carmabox modules from sys.modules.

    Python caches imported modules in sys.modules. When files are changed
    on disk (e.g. via sed hotfix) and the integration is reloaded, Python
    reuses the stale cached bytecode instead of re-reading from disk.

    By removing all carmabox entries from sys.modules during unload,
    the next async_setup_entry will force a fresh import from disk.
    """
    prefix = f"custom_components.{DOMAIN}"
    stale = [name for name in sys.modules if name == prefix or name.startswith(f"{prefix}.")]
    for name in stale:
        del sys.modules[name]
    if stale:
        _LOGGER.info(
            "IT-2466: Purged %d cached modules for hotfix support", len(stale)
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload coordinator with new config."""
    _LOGGER.info("CARMA Box options updated, reloading")
    await hass.config_entries.async_reload(entry.entry_id)
