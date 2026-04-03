"""CARMA Box -- Scenario Engine for optimization.

Pure Python. No HA imports. Fully testable.

Generates, scores, and selects optimal energy scheduling scenarios
by combining device profiles with cost modelling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..const import (
    DEFAULT_EV_NIGHT_TARGET_SOC,
    SCENARIO_MAX_COUNT,
    SCENARIO_MIN_COUNT,
)
from ..optimizer.device_profiles import (
    DeviceProfile,
    LoadSlot,
    Scenario,
)

if TYPE_CHECKING:
    from ..optimizer.cost_model import CostModel, EllevioState

__all__ = ["ScenarioEngine"]


# ── Helpers ────────────────────────────────────────────────────────────────


def _hours_for_energy(energy_kwh: float, power_kw: float) -> int:
    """Return ceiling number of hours needed to deliver energy_kwh at power_kw."""
    if energy_kwh <= 0.0 or power_kw <= 0.0:
        return 0
    return max(1, math.ceil(energy_kwh / power_kw))


def _ev_first_slots(
    ev_count: int,
    bat_count: int,
    cheap: list[int],
    ev_power: float,
    bat_k_power: float,
    bat_f_power: float,
    bat_k_h: int,
) -> list[LoadSlot]:
    """Assign cheap hours with EV taking priority (first slots)."""
    n = len(cheap)
    ev_cnt = max(0, min(ev_count, n))
    ev_hrs = cheap[:ev_cnt]
    bat_hrs = cheap[ev_cnt : ev_cnt + max(0, min(bat_count, n - ev_cnt))]
    return _build_slots(ev_hrs, bat_hrs, ev_power, bat_k_power, bat_f_power, bat_k_h)


def _bat_first_slots(
    ev_count: int,
    bat_count: int,
    cheap: list[int],
    ev_power: float,
    bat_k_power: float,
    bat_f_power: float,
    bat_k_h: int,
) -> list[LoadSlot]:
    """Assign cheap hours with battery taking priority (first slots)."""
    n = len(cheap)
    bat_cnt = max(0, min(bat_count, n))
    bat_hrs = cheap[:bat_cnt]
    ev_hrs = cheap[bat_cnt : bat_cnt + max(0, min(ev_count, n - bat_cnt))]
    return _build_slots(ev_hrs, bat_hrs, ev_power, bat_k_power, bat_f_power, bat_k_h)


def _build_slots(
    ev_hours: list[int],
    bat_hours: list[int],
    ev_power_kw: float,
    bat_k_power_kw: float,
    bat_f_power_kw: float,
    bat_k_count: int,
) -> list[LoadSlot]:
    """Build LoadSlots for EV and battery devices from assigned hour lists.

    The first bat_k_count entries in bat_hours become battery_kontor slots;
    any remaining entries become battery_forrad slots.
    """
    slots: list[LoadSlot] = []
    if ev_power_kw > 0.0:
        for h in ev_hours:
            slots.append(LoadSlot(hour=h, device="ev", power_kw=ev_power_kw, reason="scenario"))
    for i, h in enumerate(bat_hours):
        if i < bat_k_count and bat_k_power_kw > 0.0:
            slots.append(
                LoadSlot(
                    hour=h,
                    device="battery_kontor",
                    power_kw=bat_k_power_kw,
                    reason="scenario",
                )
            )
        elif i >= bat_k_count and bat_f_power_kw > 0.0:
            slots.append(
                LoadSlot(
                    hour=h,
                    device="battery_forrad",
                    power_kw=bat_f_power_kw,
                    reason="scenario",
                )
            )
    return slots


# ── ScenarioEngine ─────────────────────────────────────────────────────────


@dataclass
class ScenarioEngine:
    """Generate, score, and select optimal energy scheduling scenarios.

    Combines DeviceProfile energy needs with CostModel pricing to produce
    a ranked list of scheduling options for the night optimizer.
    """

    profiles: dict[str, DeviceProfile]
    cost_model: CostModel

    def generate_scenarios(self, state: dict[str, Any]) -> list[Scenario]:
        """Generate SCENARIO_MIN_COUNT-SCENARIO_MAX_COUNT scheduling scenarios.

        Five base templates (ev_heavy, battery_heavy, balanced, minimal,
        skip_ev) are each varied by ±1 available hour to produce up to 15
        parametric scenarios.  Scenarios respect the can_coexist() constraint
        and the >2 kW heavy-load-per-hour limit.

        Args:
            state: Dict with keys:
                battery_soc        -- current battery SoC 0-100
                ev_soc             -- current EV SoC 0-100, -1 if disconnected
                ev_target_soc      -- desired EV SoC (default 75)
                battery_target_soc -- desired battery SoC from PV forecast
                hours              -- available scheduling hours e.g. [22,23,0..5]
                prices_ore         -- 24-element Nordpool price list (öre/kWh)
                pv_tomorrow_kwh    -- Solcast PV forecast for tomorrow (kWh)

        Returns:
            List of Scenario objects, sorted by name, length in [5, 15].
        """
        battery_soc: float = float(state.get("battery_soc", 50.0))
        ev_soc: float = float(state.get("ev_soc", -1.0))
        ev_target_soc: float = float(state.get("ev_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC))
        battery_target_soc: float = float(state.get("battery_target_soc", 80.0))
        hours: list[int] = list(state.get("hours", list(range(22, 24)) + list(range(0, 6))))
        prices_ore: list[float] = list(state.get("prices_ore", [50.0] * 24))

        ev_disconnected: bool = ev_soc < 0.0

        ev_prof = self.profiles.get("ev")
        bat_k_prof = self.profiles.get("battery_kontor")
        bat_f_prof = self.profiles.get("battery_forrad")

        # ── Energy and hours needed per device ────────────────────────────

        ev_energy = (
            0.0
            if ev_disconnected or ev_prof is None
            else ev_prof.energy_needed(ev_soc, ev_target_soc)
        )
        bat_k_energy = (
            bat_k_prof.energy_needed(battery_soc, battery_target_soc) if bat_k_prof else 0.0
        )
        bat_f_energy = (
            bat_f_prof.energy_needed(battery_soc, battery_target_soc) if bat_f_prof else 0.0
        )

        ev_h = (
            0
            if ev_disconnected or ev_prof is None
            else _hours_for_energy(ev_energy, ev_prof.power_kw)
        )
        bat_k_h = _hours_for_energy(bat_k_energy, bat_k_prof.power_kw) if bat_k_prof else 0
        bat_f_h = _hours_for_energy(bat_f_energy, bat_f_prof.power_kw) if bat_f_prof else 0
        total_bat_h = bat_k_h + bat_f_h

        # Minimum EV hours -- just enough to reach 75 %
        min_ev_energy = (
            0.0
            if ev_disconnected or ev_prof is None or ev_soc >= DEFAULT_EV_NIGHT_TARGET_SOC
            else ev_prof.energy_needed(ev_soc, DEFAULT_EV_NIGHT_TARGET_SOC)
        )
        min_ev_h = (
            0
            if ev_disconnected or ev_prof is None
            else _hours_for_energy(min_ev_energy, ev_prof.power_kw)
        )

        # ── Sort available hours by price (cheapest first) ─────────────────

        cheap = sorted(
            hours,
            key=lambda h: prices_ore[h] if h < len(prices_ore) else float("inf"),
        )
        n = len(cheap)

        ev_power = ev_prof.power_kw if ev_prof else 0.0
        bat_k_power = bat_k_prof.power_kw if bat_k_prof else 0.0
        bat_f_power = bat_f_prof.power_kw if bat_f_prof else 0.0

        # ── Generate 5 templates x 3 parametric deltas ────────────────────

        scenarios: list[Scenario] = []

        for delta in (-1, 0, 1):
            suffix = ("_minus1", "", "_plus1")[delta + 1]

            # Only apply delta when base count is already > 0 (avoid creating
            # slots for devices that have no energy need or are disconnected).
            ev_delta = delta if ev_h > 0 else 0
            bat_delta = delta if total_bat_h > 0 else 0

            # ev_heavy: EV gets the cheapest hours (full charge), battery gets rest
            ev_cnt_heavy = max(0, min(ev_h + ev_delta, n))
            bat_cnt_heavy = max(0, min(total_bat_h, n - ev_cnt_heavy))
            scenarios.append(
                Scenario(
                    name=f"ev_heavy{suffix}",
                    slots=_ev_first_slots(
                        ev_cnt_heavy,
                        bat_cnt_heavy,
                        cheap,
                        ev_power,
                        bat_k_power,
                        bat_f_power,
                        bat_k_h,
                    ),
                    ev_target_soc=ev_target_soc,
                    battery_target_soc=battery_target_soc,
                )
            )

            # battery_heavy: Battery gets cheapest hours, EV gets minimum (to 75 %)
            bat_cnt_bh = max(0, min(total_bat_h + bat_delta, n))
            ev_cnt_bh = max(0, min(min_ev_h, n - bat_cnt_bh))
            scenarios.append(
                Scenario(
                    name=f"battery_heavy{suffix}",
                    slots=_bat_first_slots(
                        ev_cnt_bh,
                        bat_cnt_bh,
                        cheap,
                        ev_power,
                        bat_k_power,
                        bat_f_power,
                        bat_k_h,
                    ),
                    ev_target_soc=ev_target_soc,
                    battery_target_soc=battery_target_soc,
                )
            )

            # balanced: Split available hours equally between EV and battery
            half = max(0, min(n // 2 + (ev_delta if n > 0 else 0), n))
            ev_cnt_bal = min(ev_h, half)
            bat_cnt_bal = min(total_bat_h, n - ev_cnt_bal)
            scenarios.append(
                Scenario(
                    name=f"balanced{suffix}",
                    slots=_ev_first_slots(
                        ev_cnt_bal,
                        bat_cnt_bal,
                        cheap,
                        ev_power,
                        bat_k_power,
                        bat_f_power,
                        bat_k_h,
                    ),
                    ev_target_soc=ev_target_soc,
                    battery_target_soc=battery_target_soc,
                )
            )

            # minimal: Only necessary -- EV to 75 %, battery to target
            ev_cnt_min = min_ev_h
            bat_cnt_min = max(0, min(total_bat_h, n - ev_cnt_min))
            scenarios.append(
                Scenario(
                    name=f"minimal{suffix}",
                    slots=_ev_first_slots(
                        ev_cnt_min,
                        bat_cnt_min,
                        cheap,
                        ev_power,
                        bat_k_power,
                        bat_f_power,
                        bat_k_h,
                    ),
                    ev_target_soc=min(DEFAULT_EV_NIGHT_TARGET_SOC, ev_target_soc),
                    battery_target_soc=battery_target_soc,
                )
            )

            # skip_ev: Skip EV entirely (useful when EV SoC > 75 %), all to battery
            bat_cnt_skip = max(0, min(total_bat_h + bat_delta, n))
            scenarios.append(
                Scenario(
                    name=f"skip_ev{suffix}",
                    slots=_ev_first_slots(
                        0,
                        bat_cnt_skip,
                        cheap,
                        ev_power,
                        bat_k_power,
                        bat_f_power,
                        bat_k_h,
                    ),
                    ev_target_soc=max(0.0, ev_soc),
                    battery_target_soc=battery_target_soc,
                )
            )

        # ── Ensure minimum scenario count ─────────────────────────────────

        while len(scenarios) < SCENARIO_MIN_COUNT:
            scenarios.append(self._generate_fallback())

        return scenarios[:SCENARIO_MAX_COUNT]

    def score_scenarios(
        self,
        scenarios: list[Scenario],
        prices_ore: list[float],
        ellevio_state: EllevioState,
        tomorrow_prices_ore: list[float] | None = None,
    ) -> list[Scenario]:
        """Score each scenario and return list sorted cheapest-first.

        Calls CostModel.calculate_scenario_cost() for each scenario and
        returns new Scenario objects with total_cost_kr set.

        Args:
            scenarios:             Scenarios to score.
            prices_ore:            24h Nordpool prices (öre/kWh).
            ellevio_state:         Current-month Ellevio peak state.
            tomorrow_prices_ore:   Optional tomorrow prices for deferred cost.

        Returns:
            New list of Scenario objects sorted by total_cost_kr ascending.
        """
        scored: list[Scenario] = []
        for sc in scenarios:
            cost = self.cost_model.calculate_scenario_cost(
                sc, prices_ore, ellevio_state, tomorrow_prices_ore
            )
            scored.append(replace(sc, total_cost_kr=cost.total_kr))
        return sorted(scored, key=lambda s: s.total_cost_kr)

    def select_best(self, scenarios: list[Scenario]) -> Scenario:
        """Return the cheapest (first) scenario, or a fallback if list is empty.

        Assumes scenarios is already sorted by score_scenarios().
        """
        if not scenarios:
            return self._generate_fallback()
        return scenarios[0]

    def _generate_fallback(self) -> Scenario:
        """Create a minimal idle scenario with empty slots.

        Used when no valid scenarios can be generated, or as the
        default when select_best() receives an empty list.
        """
        return Scenario(name="fallback", slots=[], ev_target_soc=0.0, battery_target_soc=0.0)
