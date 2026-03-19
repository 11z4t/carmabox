"""CARMA Box — Diagnostics.

Provides downloadable debug information for troubleshooting.
All entity IDs are anonymized (hashed).
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import CarmaboxCoordinator
from .optimizer.savings import savings_breakdown


def _hash_entity(entity_id: str) -> str:
    """Hash entity ID for privacy."""
    if not entity_id:
        return ""
    return f"entity_{hashlib.sha256(entity_id.encode()).hexdigest()[:8]}"


def _anonymize_options(options: dict[str, Any]) -> dict[str, Any]:
    """Anonymize config options — remove entity IDs."""
    safe = {}
    for k, v in options.items():
        if isinstance(v, str) and ("sensor." in v or "select." in v or "number." in v):
            safe[k] = _hash_entity(v)
        else:
            safe[k] = v
    return safe


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: CarmaboxCoordinator = entry.runtime_data

    state = coordinator.data
    plan_summary = []
    if state and state.plan:
        for h in state.plan[:24]:
            plan_summary.append(
                {
                    "hour": h.hour,
                    "action": h.action,
                    "battery_kw": h.battery_kw,
                    "grid_kw": h.grid_kw,
                    "weighted_kw": h.weighted_kw,
                    "battery_soc": h.battery_soc,
                    "price": h.price,
                }
            )

    savings = savings_breakdown(
        coordinator.savings,
        float(entry.options.get("peak_cost_per_kw", 80.0)),
    )

    return {
        "config": _anonymize_options(dict(entry.options)),
        "coordinator": {
            "target_kw": coordinator.target_kw,
            "min_soc": coordinator.min_soc,
            "last_command": coordinator._last_command.value,
            "plan_hours": len(coordinator.plan),
            "daily_plans": coordinator._daily_plans,
            "daily_safety_blocks": coordinator._daily_safety_blocks,
            "daily_discharge_kwh": round(coordinator._daily_discharge_kwh, 2),
        },
        "state": {
            "grid_power_w": state.grid_power_w if state else None,
            "battery_soc_1": state.battery_soc_1 if state else None,
            "battery_soc_2": state.battery_soc_2 if state else None,
            "pv_power_w": state.pv_power_w if state else None,
            "ev_soc": state.ev_soc if state else None,
            "is_exporting": state.is_exporting if state else None,
            "current_price": state.current_price if state else None,
        },
        "plan": plan_summary,
        "savings": dict(savings),
        "safety": {
            "heartbeat_ok": coordinator.safety.check_heartbeat().ok,
            "rate_limit_ok": coordinator.safety.check_rate_limit().ok,
            "mode_changes_last_hour": len(coordinator.safety._mode_change_timestamps),
            "blocks_last_hour": coordinator.safety.recent_block_count(3600),
            "log": coordinator.safety.get_safety_log(),
        },
    }
