"""Binary Balancer — Typ D (binary on/off) asset balancer for Home Assistant.

Manages pool_vp, pool_elv, and miner assets via switch entities,
following the Brain-Balancer interface protocol.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import DOMAIN as DOMAIN
from .const import PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Binary Balancer from a config entry."""
    from .coordinator import BinaryBalancerCoordinator

    _LOGGER.info("Setting up Binary Balancer: %s", entry.title)

    coordinator = BinaryBalancerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Binary Balancer config entry."""
    _LOGGER.info("Unloading Binary Balancer: %s", entry.title)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
