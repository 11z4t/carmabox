"""Brain v0.1 MVP — custom_component entry point.

Configured via configuration.yaml:

    brain:
      ev_status_entity: sensor.easee_home_12840_status   # optional
      decision_log_path: /config/brain_v01_decisions.jsonl  # optional
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .brain import BrainController
from .const import (
    DEFAULT_DECISION_LOG_PATH,
    DOMAIN,
    ENTITY_EV_STATUS_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

CONF_EV_STATUS_ENTITY = "ev_status_entity"
CONF_DECISION_LOG_PATH = "decision_log_path"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_EV_STATUS_ENTITY, default=ENTITY_EV_STATUS_DEFAULT): cv.entity_id,
                vol.Optional(CONF_DECISION_LOG_PATH, default=DEFAULT_DECISION_LOG_PATH): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:  # pragma: no cover
    """Set up Brain v0.1 from configuration.yaml entry."""
    conf: dict[str, Any] = config.get(DOMAIN, {})
    ev_entity: str = conf.get(CONF_EV_STATUS_ENTITY, ENTITY_EV_STATUS_DEFAULT)
    log_path: str = conf.get(CONF_DECISION_LOG_PATH, DEFAULT_DECISION_LOG_PATH)

    controller = BrainController(hass, ev_entity, log_path)
    hass.data[DOMAIN] = controller
    controller.start()

    async def _async_stop(_event: Any) -> None:
        controller.stop()

    hass.bus.async_listen_once("homeassistant_stop", _async_stop)
    _LOGGER.info("Brain v0.1 initialized (ev_entity=%s)", ev_entity)
    return True
