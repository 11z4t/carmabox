"""CARMA Box — Night Planner for overnight energy scheduling.

Pure Python. No HA imports. Fully testable.

Plans overnight EV and battery charging using ScenarioEngine scenarios,
Nordpool prices, Solcast PV forecast, and Ellevio capacity state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..const import DEFAULT_EV_NIGHT_TARGET_SOC, MAX_NIGHTLY_SOC_DELTA_PCT

if TYPE_CHECKING:
    from ..optimizer.cost_model import CostModel, EllevioState
    from ..optimizer.scenario_engine import ScenarioEngine

_LOGGER = logging.getLogger(__name__)

__all__ = ["NightPlan", "NightPlanner", "NightSlot", "calculate_ev_trajectory"]

# ── Night window constants ─────────────────────────────────────────────────

_NIGHT_START: int = 22
_NIGHT_END: int = 6  # exclusive — window covers hours [22..5]

# ── Internal helpers ───────────────────────────────────────────────────────


def _build_night_window(current_hour: int) -> list[int]:
    """Return ordered list of night hours from current_hour (or 22) to 05 inclusive.

    If current_hour is in the daytime window (6-21) the window starts at 22.
    If current_hour is already in the night window (22-23 or 0-5) it starts
    from current_hour so we never schedule into the past.
    """
    start = _NIGHT_START if 6 <= current_hour < _NIGHT_START else current_hour
    hours: list[int] = []
    h = start
    while h % 24 != _NIGHT_END:
        hours.append(h % 24)
        h += 1
    return hours


def _battery_target_from_pv(pv_kwh: float) -> float:
    """Calculate battery charge target (%) from tomorrow's PV forecast.

    Tiers:
        pv < 5 kWh   → 100 %  (no solar → charge full tonight)
        5-15 kWh     → 100-70 % linear
        15-30 kWh    → 70-45 % linear
        >30 kWh      → 45-35 % (diminishing reserve needed)
    """
    if pv_kwh < 5.0:
        return 100.0
    if pv_kwh < 15.0:
        t = (pv_kwh - 5.0) / 10.0
        return 100.0 - t * 30.0  # 100 → 70
    if pv_kwh < 30.0:
        t = (pv_kwh - 15.0) / 15.0
        return 70.0 - t * 25.0  # 70 → 45
    # >30 kWh: 45 → 35 (linear over [30, 50], floor at 35)
    return max(35.0, 45.0 - (pv_kwh - 30.0) * 0.5)


# ── EV trajectory ──────────────────────────────────────────────────────────


def calculate_ev_trajectory(
    current_soc: float,
    days_since_full: int,
    avg_daily_use: float = 15.0,
) -> float:
    """Calculate tonight's EV charge target SoC.

    Strategy:
        days_since_full >= 6  → charge to 100 % (deadline, BMS health)
        days_since_full >= 4  → spread charging: current + MAX_NIGHTLY_SOC_DELTA_PCT
        otherwise             → tactical minimal: current + 5 %, capped at delta limit

    Args:
        current_soc:     Current EV battery SoC (0-100).
        days_since_full: Days elapsed since last 100 % charge.
        avg_daily_use:   Average daily energy use in kWh (reserved, not yet used).

    Returns:
        Target SoC clamped to [DEFAULT_EV_NIGHT_TARGET_SOC, 100.0].
    """
    daily_min: float = DEFAULT_EV_NIGHT_TARGET_SOC  # 75.0

    if days_since_full >= 6:
        # Deadline near: always charge to full
        tonight_target = 100.0
    elif days_since_full >= 4:
        # Spread over multiple nights: add up to MAX_NIGHTLY_SOC_DELTA_PCT
        tonight_target = max(daily_min, min(100.0, current_soc + MAX_NIGHTLY_SOC_DELTA_PCT))
    else:
        # Tactical: minimal +5 %, respect delta cap
        raw = max(daily_min, current_soc + 5.0)
        tonight_target = min(raw, current_soc + MAX_NIGHTLY_SOC_DELTA_PCT)

    return max(75.0, min(100.0, tonight_target))


# ── NightSlot ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NightSlot:
    """A scheduled load assignment for a specific night hour."""

    hour: int
    device: str
    power_kw: float
    duration_min: int = 60
    reason: str = ""


# ── NightPlan ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NightPlan:
    """Complete overnight scheduling plan.

    Attributes:
        slots:               Ordered list of load assignments for the night.
        total_cost_kr:       Estimated Nordpool + Ellevio cost (kr).
        ev_target_soc:       Target EV SoC by morning (%).
        battery_target_soc:  Target battery SoC by morning (%).
        scenario_name:       Name of the winning ScenarioEngine scenario.
        created_at:          Plan creation timestamp.
        ev_skipped:          True when EV is disconnected or explicitly skipped.
        ev_skip_reason:      Human-readable reason for skipping EV.
    """

    slots: list[NightSlot] = field(default_factory=list, hash=False, compare=False)
    total_cost_kr: float = 0.0
    ev_target_soc: float = 0.0
    battery_target_soc: float = 0.0
    scenario_name: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    ev_skipped: bool = False
    ev_skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for sensor attributes and logging."""
        return {
            "scenario_name": self.scenario_name,
            "total_cost_kr": self.total_cost_kr,
            "ev_target_soc": self.ev_target_soc,
            "battery_target_soc": self.battery_target_soc,
            "ev_skipped": self.ev_skipped,
            "ev_skip_reason": self.ev_skip_reason,
            "created_at": self.created_at.isoformat(),
            "slots": [
                {
                    "hour": s.hour,
                    "device": s.device,
                    "power_kw": s.power_kw,
                    "duration_min": s.duration_min,
                    "reason": s.reason,
                }
                for s in self.slots
            ],
        }


# ── NightPlanner ───────────────────────────────────────────────────────────


@dataclass
class NightPlanner:
    """Plan overnight EV and battery charging using ScenarioEngine.

    Replaces generate_scheduler_plan() + _schedule_ev_backwards() for night
    scheduling. Calls ScenarioEngine instead of scheduler.py directly.

    Attributes:
        engine:     ScenarioEngine used to generate and score scenarios.
        cost_model: CostModel used for standalone cost calculations (e.g. fallback).
    """

    engine: ScenarioEngine
    cost_model: CostModel

    def plan_tonight(self, state: dict[str, Any]) -> NightPlan:
        """Generate overnight charging plan from current state.

        Args:
            state: Dict with keys:
                battery_soc        -- current battery SoC (0-100)
                ev_soc             -- current EV SoC (0-100), -1 if disconnected
                ev_days_since_full -- days since last 100 % charge (int)
                prices_ore         -- 24 h Nordpool prices (öre/kWh)
                tomorrow_prices_ore -- tomorrow's prices, or None
                pv_tomorrow_kwh    -- Solcast p10 PV forecast for tomorrow (kWh)
                ellevio_state      -- EllevioState instance
                dishwasher_needed  -- True to add a dishwasher slot
                current_hour       -- current hour 0-23

        Returns:
            NightPlan with optimal overnight slots.
        """
        battery_soc: float = float(state.get("battery_soc", 50.0))
        ev_soc: float = float(state.get("ev_soc", -1.0))
        ev_days_since_full: int = int(state.get("ev_days_since_full", 0))
        prices_ore: list[float] = list(state.get("prices_ore", [50.0] * 24))
        tomorrow_prices_ore: list[float] | None = state.get("tomorrow_prices_ore")
        pv_tomorrow_kwh: float = float(state.get("pv_tomorrow_kwh", 0.0))
        ellevio_state: EllevioState = state["ellevio_state"]
        dishwasher_needed: bool = bool(state.get("dishwasher_needed", False))
        current_hour: int = int(state.get("current_hour", 22))

        battery_target = _battery_target_from_pv(pv_tomorrow_kwh)
        hours = _build_night_window(current_hour)

        # EV disconnected: produce a battery-only plan
        if ev_soc < 0:
            return self._plan_no_ev(
                battery_soc=battery_soc,
                battery_target=battery_target,
                prices_ore=prices_ore,
                ellevio_state=ellevio_state,
                tomorrow_prices_ore=tomorrow_prices_ore,
                pv_tomorrow_kwh=pv_tomorrow_kwh,
                hours=hours,
                dishwasher_needed=dishwasher_needed,
                skip_reason="disconnected",
            )

        ev_target = calculate_ev_trajectory(ev_soc, ev_days_since_full)

        engine_state: dict[str, Any] = {
            "battery_soc": battery_soc,
            "ev_soc": ev_soc,
            "ev_target_soc": ev_target,
            "battery_target_soc": battery_target,
            "hours": hours,
            "prices_ore": prices_ore,
            "pv_tomorrow_kwh": pv_tomorrow_kwh,
        }

        try:
            scenarios = self.engine.generate_scenarios(engine_state)
            if not scenarios:
                return self._fallback_plan(battery_target, prices_ore, hours)
            scored = self.engine.score_scenarios(
                scenarios, prices_ore, ellevio_state, tomorrow_prices_ore
            )
        except Exception as exc:
            _LOGGER.warning("NightPlanner: scenario generation failed: %s", exc)
            return self._fallback_plan(battery_target, prices_ore, hours)

        best = self.engine.select_best(scored)
        slots = self._to_night_slots(best.slots)

        if dishwasher_needed:
            dw = self._dishwasher_slot(slots, hours, prices_ore)
            if dw is not None:
                slots = [*slots, dw]

        return NightPlan(
            slots=slots,
            total_cost_kr=best.total_cost_kr,
            ev_target_soc=ev_target,
            battery_target_soc=battery_target,
            scenario_name=best.name,
            created_at=datetime.now(),
        )

    # ── Private helpers ────────────────────────────────────────────────────

    def _plan_no_ev(
        self,
        battery_soc: float,
        battery_target: float,
        prices_ore: list[float],
        ellevio_state: EllevioState,
        tomorrow_prices_ore: list[float] | None,
        pv_tomorrow_kwh: float,
        hours: list[int],
        dishwasher_needed: bool,
        skip_reason: str,
    ) -> NightPlan:
        """Create a plan without EV charging (EV disconnected or skipped)."""
        engine_state: dict[str, Any] = {
            "battery_soc": battery_soc,
            "ev_soc": -1.0,
            "ev_target_soc": 0.0,
            "battery_target_soc": battery_target,
            "hours": hours,
            "prices_ore": prices_ore,
            "pv_tomorrow_kwh": pv_tomorrow_kwh,
        }
        try:
            scenarios = self.engine.generate_scenarios(engine_state)
            if not scenarios:
                return self._fallback_plan(
                    battery_target,
                    prices_ore,
                    hours,
                    ev_skipped=True,
                    ev_skip_reason=skip_reason,
                )
            scored = self.engine.score_scenarios(
                scenarios, prices_ore, ellevio_state, tomorrow_prices_ore
            )
            best = self.engine.select_best(scored)
            slots = self._to_night_slots(best.slots)
            if dishwasher_needed:
                dw = self._dishwasher_slot(slots, hours, prices_ore)
                if dw is not None:
                    slots = [*slots, dw]
            return NightPlan(
                slots=slots,
                total_cost_kr=best.total_cost_kr,
                ev_target_soc=0.0,
                battery_target_soc=battery_target,
                scenario_name=best.name,
                created_at=datetime.now(),
                ev_skipped=True,
                ev_skip_reason=skip_reason,
            )
        except Exception as exc:
            _LOGGER.warning("NightPlanner: no-EV scenario generation failed: %s", exc)
            return self._fallback_plan(
                battery_target,
                prices_ore,
                hours,
                ev_skipped=True,
                ev_skip_reason=skip_reason,
            )

    def _fallback_plan(
        self,
        battery_target: float,
        prices_ore: list[float],
        hours: list[int],
        ev_skipped: bool = False,
        ev_skip_reason: str = "",
    ) -> NightPlan:
        """Minimal fallback: battery-only on the 2 cheapest available hours."""
        cheap = sorted(
            hours,
            key=lambda h: prices_ore[h] if h < len(prices_ore) else float("inf"),
        )[:2]
        slots = [
            NightSlot(
                hour=h,
                device="battery_kontor",
                power_kw=3.6,
                duration_min=60,
                reason="fallback",
            )
            for h in cheap
        ]
        total_cost = sum(
            s.power_kw
            * s.duration_min
            / 60.0
            * (prices_ore[s.hour] if s.hour < len(prices_ore) else 50.0)
            / 100.0
            for s in slots
        )
        return NightPlan(
            slots=slots,
            total_cost_kr=total_cost,
            ev_target_soc=0.0,
            battery_target_soc=battery_target,
            scenario_name="fallback",
            created_at=datetime.now(),
            ev_skipped=ev_skipped,
            ev_skip_reason=ev_skip_reason,
        )

    def _dishwasher_slot(
        self,
        existing_slots: list[NightSlot],
        hours: list[int],
        prices_ore: list[float],
    ) -> NightSlot | None:
        """Return the cheapest available hour for dishwasher without device conflict.

        Dishwasher cannot coexist with EV or battery slots (can_coexist() rules).
        Returns None if no conflict-free hour is available.
        """
        conflicting_hours = {
            s.hour for s in existing_slots if s.device in ("ev", "battery_kontor", "battery_forrad")
        }
        free_hours = [h for h in hours if h not in conflicting_hours]
        if not free_hours:
            return None
        best_hour = min(
            free_hours,
            key=lambda h: prices_ore[h] if h < len(prices_ore) else float("inf"),
        )
        dw_prof = self.engine.profiles.get("dishwasher")
        power_kw = dw_prof.power_kw if dw_prof else 1.2
        return NightSlot(
            hour=best_hour,
            device="dishwasher",
            power_kw=power_kw,
            duration_min=120,
            reason="dishwasher_needed",
        )

    @staticmethod
    def _to_night_slots(load_slots: list[Any]) -> list[NightSlot]:
        """Convert LoadSlots from a Scenario into NightSlots."""
        return [
            NightSlot(
                hour=s.hour,
                device=s.device,
                power_kw=s.power_kw,
                duration_min=s.duration_min,
                reason=s.reason,
            )
            for s in load_slots
        ]
