"""CARMA Box — Bridge between NightPlan/SurplusPlan and Plan Executor format.

Pure Python. No HA imports. Fully testable.

Responsibilities:
  - Convert NightSlot list to executor-compatible plan slot dicts
  - Select active plan type based on time and system state
  - Persist NightPlan to JSON for survival across HA restarts
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..const import (
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_VOLTAGE,
    MAX_EV_CURRENT,
)
from ..optimizer.night_planner import NightPlan, NightSlot

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "NightPlanBridge",
    "PlanSelector",
    "load_night_plan",
    "save_night_plan",
]

# Standard 3-phase EV charging (L1+L2+L3)
_EV_PHASE_COUNT: int = 3


class NightPlanBridge:
    """Convert NightPlan slots to Plan Executor dict format.

    Mapping rules:
      device='ev'         → action='e', ev_kw/ev_amps filled
      device='battery_*'  → action='g' (grid charge at night), battery_kw filled
      device='dishwasher' → action='i' (idle; handled by appliance logic)
      device='miner'      → action='i', miner_on=True

    Each returned dict contains:
      hour, action, battery_kw, ev_kw, ev_amps, miner_on, weighted_kw, pv_kw
    """

    def convert_to_plan_slots(self, night_plan: NightPlan) -> list[dict[str, Any]]:
        """Convert NightPlan slots to executor-compatible plan slot dicts.

        Args:
            night_plan: NightPlan produced by NightPlanner.

        Returns:
            Ordered list of plan slot dicts, one per NightSlot.
        """
        return [self._convert_slot(slot) for slot in night_plan.slots]

    def _convert_slot(self, slot: NightSlot) -> dict[str, Any]:
        action: str = "i"
        battery_kw: float = 0.0
        ev_kw: float = 0.0
        ev_amps: int = 0
        miner_on: bool = False

        if slot.device == "ev":
            action = "e"
            ev_kw = slot.power_kw
            ev_amps = _calc_ev_amps(slot.power_kw)
        elif slot.device.startswith("battery_"):
            action = "g"
            battery_kw = slot.power_kw
        elif slot.device == "dishwasher":
            action = "i"
        elif slot.device == "miner":
            action = "i"
            miner_on = True

        # Night weight: Ellevio bills night imports at DEFAULT_NIGHT_WEIGHT (0.5)
        weighted_kw = slot.power_kw * DEFAULT_NIGHT_WEIGHT

        return {
            "hour": slot.hour,
            "action": action,
            "battery_kw": battery_kw,
            "ev_kw": ev_kw,
            "ev_amps": ev_amps,
            "miner_on": miner_on,
            "weighted_kw": weighted_kw,
            "pv_kw": 0.0,
        }


class PlanSelector:
    """Select which plan should govern the current hour."""

    @staticmethod
    def select_active_plan(
        hour: int,
        night_plan: NightPlan | None,
        surplus_available_kw: float,
        has_pv: bool,
    ) -> str:
        """Choose the active plan type.

        Priority:
          1. Night window (22-06) with an existing NightPlan → 'night'
          2. Daytime with PV and positive surplus → 'surplus'
          3. Otherwise → 'fallback'

        Args:
            hour:                Current hour (0-23).
            night_plan:          Active NightPlan, or None if none exists.
            surplus_available_kw: Available PV surplus in kW (positive = exporting).
            has_pv:              True if the system has PV panels installed.

        Returns:
            'night', 'surplus', or 'fallback'.
        """
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        if is_night and night_plan is not None:
            return "night"
        if not is_night and has_pv and surplus_available_kw > 0:
            return "surplus"
        return "fallback"


# ── Persistence ────────────────────────────────────────────────────────────


def save_night_plan(plan: NightPlan, storage_path: str) -> None:
    """Persist NightPlan to a JSON file so it survives HA restarts.

    Args:
        plan:         NightPlan to save.
        storage_path: Absolute path for the JSON file (parent dirs created).
    """
    path = Path(storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
    _LOGGER.debug("NightPlan saved to %s", storage_path)


def load_night_plan(storage_path: str) -> NightPlan | None:
    """Load a previously saved NightPlan from JSON.

    Args:
        storage_path: Absolute path to the JSON file.

    Returns:
        NightPlan if the file exists and is valid JSON, None otherwise.
    """
    path = Path(storage_path)
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        slots = [
            NightSlot(
                hour=int(s["hour"]),
                device=str(s["device"]),
                power_kw=float(s["power_kw"]),
                duration_min=int(s.get("duration_min", 60)),
                reason=str(s.get("reason", "")),
            )
            for s in data.get("slots", [])
        ]
        return NightPlan(
            slots=slots,
            total_cost_kr=float(data.get("total_cost_kr", 0.0)),
            ev_target_soc=float(data.get("ev_target_soc", 0.0)),
            battery_target_soc=float(data.get("battery_target_soc", 0.0)),
            scenario_name=str(data.get("scenario_name", "")),
            created_at=datetime.fromisoformat(
                str(data.get("created_at", datetime.now().isoformat()))
            ),
            ev_skipped=bool(data.get("ev_skipped", False)),
            ev_skip_reason=str(data.get("ev_skip_reason", "")),
        )
    except Exception:
        _LOGGER.warning("Failed to load NightPlan from %s", storage_path, exc_info=True)
        return None


# ── Internal helpers ────────────────────────────────────────────────────────


def _calc_ev_amps(power_kw: float) -> int:
    """Convert charging power (kW) to amps for 3-phase EV charger.

    Clamped to [DEFAULT_EV_MIN_AMPS, MAX_EV_CURRENT].
    """
    raw = int(power_kw * 1000.0 / (DEFAULT_VOLTAGE * _EV_PHASE_COUNT))
    return min(max(raw, DEFAULT_EV_MIN_AMPS), MAX_EV_CURRENT)
