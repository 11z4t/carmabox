"""HA BinarySensorEntity definitions for ev_balancer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EvBalancerStatus
from .coordinator import EvBalancerCoordinator
from .models import EvBalancerState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EvBalancerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EvBalancerSafeStateActive(coordinator),
            EvBalancerInCooldown(coordinator),
        ]
    )


class _EvBinaryBase(CoordinatorEntity[EvBalancerCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    @property
    def _state_obj(self) -> EvBalancerState:
        return self.coordinator.data


class EvBalancerSafeStateActive(_EvBinaryBase):
    """True when balancer is in a safe/disabled state (no HW writes)."""

    _attr_unique_id = "ev_balancer_safe_state_active"
    _attr_name = "Safe State Active"
    _attr_icon = "mdi:shield-check"

    @property
    def is_on(self) -> bool:
        s = self._state_obj
        if not s:
            return True
        return s.status in (
            EvBalancerStatus.SHADOW_MODE,
            EvBalancerStatus.OFFLINE,
            EvBalancerStatus.FAULT,
            EvBalancerStatus.INITIALIZING,
        )


class EvBalancerInCooldown(_EvBinaryBase):
    """True when any cooldown timer is active."""

    _attr_unique_id = "ev_balancer_in_cooldown"
    _attr_name = "In Cooldown"
    _attr_icon = "mdi:timer-sand"

    @property
    def is_on(self) -> bool:
        return len(self.coordinator.active_cooldowns) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "cooldowns": [
                {"type": k, "remaining_s": round(v, 1)}
                for k, v in self.coordinator.active_cooldowns
            ]
        }
