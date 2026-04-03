"""CARMA Box -- Cost Model for scenario evaluation.

Pure Python. No HA imports. Fully testable.

Calculates Nordpool grid costs, Ellevio capacity tariff penalties,
PV export losses, and deferred scheduling costs for energy scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..const import (
    DEFAULT_BATTERY_EFFICIENCY,
    ELLEVIO_RATE_KR_PER_KW_MONTH,
)
from ..optimizer.grid_logic import ellevio_weight

if TYPE_CHECKING:
    from ..optimizer.device_profiles import LoadSlot, Scenario


# ---- EllevioState -------------------------------------------------------


@dataclass(frozen=True)
class EllevioState:
    """Current-month Ellevio peak capacity state.

    month_peak_kw is the weighted average of top3_weighted_hours.
    Used to project whether new load will increase the monthly bill.

    Attributes:
        month_peak_kw: Current weighted average of the top-3 hourly peaks.
        top3_weighted_hours: The 3 highest weighted hourly mean values this month.
        target_kw: Ellevio contracted import cap (default 2.0 kW).
    """

    month_peak_kw: float
    top3_weighted_hours: list[float] = field(
        default_factory=list, hash=False, compare=False, repr=False
    )
    target_kw: float = 2.0


# ---- ScenarioCost -------------------------------------------------------


@dataclass(frozen=True)
class ScenarioCost:
    """Full cost breakdown for a scheduling scenario.

    All monetary values in SEK (kr).

    Attributes:
        grid_cost_kr: Nordpool spot cost (timpriser x kWh).
        ellevio_penalty_kr: Capacity tariff increase from new monthly peak.
        export_loss_kr: Missed PV export value (foregone self-consumption).
        deferred_cost_kr: Projected cost if same energy deferred to tomorrow night.
    """

    grid_cost_kr: float
    ellevio_penalty_kr: float
    export_loss_kr: float
    deferred_cost_kr: float

    @property
    def total_kr(self) -> float:
        """Sum of all cost components (kr)."""
        return (
            self.grid_cost_kr
            + self.ellevio_penalty_kr
            + self.export_loss_kr
            + self.deferred_cost_kr
        )


# ---- CostModel ----------------------------------------------------------


class CostModel:
    """Calculate and compare scenario costs for energy scheduling.

    Uses Nordpool spot prices, Ellevio capacity tariff, and PV export
    value to evaluate the full cost of each scheduling scenario.
    """

    def calculate_scenario_cost(
        self,
        scenario: Scenario,
        prices_ore: list[float],
        ellevio_state: EllevioState,
        tomorrow_prices_ore: list[float] | None = None,
    ) -> ScenarioCost:
        """Calculate full cost breakdown for a scenario.

        Args:
            scenario: Scheduled load scenario (LoadSlots with device/hour/power).
            prices_ore: 24-hour Nordpool prices (ore/kWh), index=hour.
            ellevio_state: Current-month Ellevio peak state.
            tomorrow_prices_ore: Tomorrow's Nordpool prices, used for deferred cost.

        Returns:
            ScenarioCost with grid, Ellevio, export, and deferred costs.
        """
        grid_cost_kr = self._calc_grid_cost(scenario.slots, prices_ore)
        ellevio_penalty_kr = self._calc_ellevio_penalty(scenario.slots, ellevio_state)
        export_loss_kr = 0.0  # Night scenarios: no PV surplus to export
        deferred_cost_kr = self._calc_deferred_cost(scenario.slots, tomorrow_prices_ore)

        return ScenarioCost(
            grid_cost_kr=grid_cost_kr,
            ellevio_penalty_kr=ellevio_penalty_kr,
            export_loss_kr=export_loss_kr,
            deferred_cost_kr=deferred_cost_kr,
        )

    def calculate_night_cost(
        self,
        slots: list[LoadSlot],
        prices_ore: list[float],
    ) -> float:
        """Calculate Nordpool-only cost for a night schedule.

        Simpler variant -- no Ellevio or deferred cost calculations.

        Args:
            slots: Scheduled load slots.
            prices_ore: 24-hour Nordpool prices (ore/kWh).

        Returns:
            Total grid cost in kr.
        """
        return self._calc_grid_cost(slots, prices_ore)

    def cheapest_hours(
        self,
        prices_ore: list[float],
        hours_needed: int,
        window_start: int = 22,
        window_end: int = 6,
    ) -> list[int]:
        """Return the N cheapest hours in a night window, sorted by hour.

        Handles wrap-around (e.g. 22 to 06 crosses midnight).

        Args:
            prices_ore: 24-hour Nordpool prices (ore/kWh).
            hours_needed: Number of hours to select.
            window_start: Night window start hour (inclusive).
            window_end: Night window end hour (exclusive).

        Returns:
            List of hour indices sorted ascending (not by price).
        """
        if window_start > window_end:
            # Wraps midnight: e.g. [22, 23, 0, 1, 2, 3, 4, 5]
            window_hours: list[int] = list(range(window_start, 24)) + list(range(0, window_end))
        else:
            window_hours = list(range(window_start, window_end))

        n = min(hours_needed, len(window_hours))
        if n <= 0:
            return []

        sorted_by_price = sorted(
            window_hours,
            key=lambda h: prices_ore[h] if h < len(prices_ore) else float("inf"),
        )
        selected = sorted_by_price[:n]
        return sorted(selected)

    # ---- Private helpers -------------------------------------------------

    def _calc_grid_cost(
        self,
        slots: list[LoadSlot],
        prices_ore: list[float],
    ) -> float:
        """Calculate Nordpool grid cost for a list of load slots.

        Battery devices incur extra grid import due to round-trip efficiency loss:
        grid_import_kwh = stored_kwh / DEFAULT_BATTERY_EFFICIENCY.
        """
        total = 0.0
        for slot in slots:
            energy_kwh = slot.power_kw * slot.duration_min / 60.0
            if slot.device.startswith("battery_"):
                energy_kwh = energy_kwh / DEFAULT_BATTERY_EFFICIENCY
            price = prices_ore[slot.hour] if slot.hour < len(prices_ore) else 0.0
            total += energy_kwh * price / 100.0
        return total

    def _calc_ellevio_penalty(
        self,
        slots: list[LoadSlot],
        ellevio_state: EllevioState,
    ) -> float:
        """Calculate Ellevio capacity tariff penalty from new monthly peak.

        For each slot, projects the Ellevio-weighted peak (power x weight).
        Combines with existing top-3 peaks; if the new average exceeds
        current month_peak_kw, the delta x ELLEVIO_RATE_KR_PER_KW_MONTH
        is the penalty.

        Night hours (22-06) are weighted x 0.5 per Ellevio tariff.
        """
        if not slots:
            return 0.0

        # Project maximum weighted peak per hour across all slots
        projected: dict[int, float] = {}
        for slot in slots:
            w = ellevio_weight(slot.hour)
            weighted_kw = slot.power_kw * w
            projected[slot.hour] = max(projected.get(slot.hour, 0.0), weighted_kw)

        # Combine new projections with existing top-3 and find new top-3
        existing = list(ellevio_state.top3_weighted_hours)
        combined = existing + list(projected.values())
        new_top3 = sorted(combined, reverse=True)[:3]

        if not new_top3:
            return 0.0

        new_avg = sum(new_top3) / len(new_top3)
        if new_avg > ellevio_state.month_peak_kw:
            return (new_avg - ellevio_state.month_peak_kw) * ELLEVIO_RATE_KR_PER_KW_MONTH

        return 0.0

    def _calc_deferred_cost(
        self,
        slots: list[LoadSlot],
        tomorrow_prices_ore: list[float] | None,
    ) -> float:
        """Calculate cost if same energy load were deferred to tomorrow night.

        Finds the cheapest tomorrow-night hours for the total energy duration,
        distributes the energy evenly, and returns the projected cost.

        Returns 0.0 when tomorrow prices are unavailable.
        """
        if tomorrow_prices_ore is None or not slots:
            return 0.0

        total_kwh = sum(s.power_kw * s.duration_min / 60.0 for s in slots)
        if total_kwh <= 0.0:
            return 0.0

        total_duration_h = sum(s.duration_min / 60.0 for s in slots)
        hours_needed = max(1, round(total_duration_h))
        cheapest = self.cheapest_hours(tomorrow_prices_ore, hours_needed)

        if not cheapest:
            return 0.0

        energy_per_hour = total_kwh / len(cheapest)
        return sum(energy_per_hour * tomorrow_prices_ore[h] / 100.0 for h in cheapest)


__all__ = [
    "CostModel",
    "EllevioState",
    "ScenarioCost",
]
