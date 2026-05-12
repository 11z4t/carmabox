"""Battery Balancer v3 — brand-agnostic multi-bank battery coordinator (BAL-10-BAT v1.0)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import BatBalancerCoordinator

    hass.data.setdefault(DOMAIN, {})
    coordinator = BatBalancerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("bat_balancer v3 started (entry=%s)", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
    if coordinator:
        coordinator.shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
