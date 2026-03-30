"""CARMA Box — Appliance Detection & Tracking.

Auto-detects power sensors and categorizes them as appliances.
Tracks running state, energy usage, and provides recommendations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import (
    APPLIANCE_CATEGORIES,
    APPLIANCE_EXCLUDE_PREFIXES,
    APPLIANCE_HINTS,
    DEFAULT_APPLIANCE_THRESHOLD_W,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class Appliance:
    """A detected appliance with power sensor."""

    entity_id: str
    name: str
    category: str  # Key from APPLIANCE_CATEGORIES
    threshold_w: float = DEFAULT_APPLIANCE_THRESHOLD_W
    current_power_w: float = 0.0
    is_running: bool = False
    today_kwh: float = 0.0
    runs_today: int = 0

    @property
    def category_name(self) -> str:
        """Human-readable category name."""
        return APPLIANCE_CATEGORIES.get(self.category, self.category)

    def to_dict(self) -> dict[str, object]:
        """Serialize for config storage."""
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "category": self.category,
            "threshold_w": self.threshold_w,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Appliance:
        """Deserialize from config storage."""
        return cls(
            entity_id=str(data.get("entity_id", "")),
            name=str(data.get("name", "")),
            category=str(data.get("category", "other")),
            threshold_w=float(data.get("threshold_w", DEFAULT_APPLIANCE_THRESHOLD_W)),
        )


def detect_appliances(hass: HomeAssistant) -> list[Appliance]:
    """Auto-detect power sensors that could be appliances.

    Scans all sensor.* entities with W/kW unit, excludes known system sensors,
    and guesses category from name heuristics.
    """
    appliances: list[Appliance] = []

    for state in hass.states.async_all("sensor"):
        eid = state.entity_id
        unit = state.attributes.get("unit_of_measurement", "")
        if unit not in ("W", "kW"):
            continue

        # Skip system sensors
        short = eid.replace("sensor.", "")
        if any(p in short for p in APPLIANCE_EXCLUDE_PREFIXES):
            continue

        # Skip sensors with 0 total energy (never used)
        name = state.attributes.get("friendly_name", eid)
        name_lower = (
            name.lower().replace("\u00e4", "a").replace("\u00f6", "o").replace("\u00e5", "a")
        )
        eid_lower = eid.lower()

        # Guess category
        category = "other"
        for hint, cat in APPLIANCE_HINTS.items():
            if hint in name_lower or hint in eid_lower:
                category = cat
                break

        appliances.append(
            Appliance(
                entity_id=eid,
                name=name,
                category=category,
            )
        )

    _LOGGER.info("Detected %d appliances", len(appliances))
    return appliances


def update_appliance_states(
    hass: HomeAssistant,
    appliances: list[Appliance],
    interval_hours: float = 30 / 3600,
) -> None:
    """Update current power and running state for all appliances."""
    for app in appliances:
        state = hass.states.get(app.entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            app.current_power_w = 0.0
            app.is_running = False
            continue

        try:
            val = float(state.state)
            unit = state.attributes.get("unit_of_measurement", "W")
            if unit == "kW":
                val *= 1000
            app.current_power_w = val

            was_running = app.is_running
            app.is_running = val > app.threshold_w

            # Track energy
            if app.is_running:
                app.today_kwh += val / 1000 * interval_hours

            # Count run starts
            if app.is_running and not was_running:
                app.runs_today += 1

        except (ValueError, TypeError):
            app.current_power_w = 0.0
            app.is_running = False


def appliance_summary(appliances: list[Appliance]) -> dict[str, Any]:
    """Summarize appliance state by category."""
    categories: dict[str, dict[str, Any]] = {}

    for app in appliances:
        cat = app.category
        if cat not in categories:
            categories[cat] = {
                "name": app.category_name,
                "total_power_w": 0.0,
                "today_kwh": 0.0,
                "running": [],
                "count": 0,
            }
        categories[cat]["count"] += 1
        categories[cat]["total_power_w"] += app.current_power_w
        categories[cat]["today_kwh"] += app.today_kwh
        if app.is_running:
            categories[cat]["running"].append(app.name)

    return {
        "categories": categories,
        "total_power_w": sum(a.current_power_w for a in appliances),
        "running_count": sum(1 for a in appliances if a.is_running),
        "running_names": [a.name for a in appliances if a.is_running],
    }
