"""Tests for custom_components.carmabox.optimizer.cost_model.

PLAT-1224: Cost Model -- EllevioState, ScenarioCost, CostModel.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.carmabox.const import (
    DEFAULT_BATTERY_EFFICIENCY,
    ELLEVIO_RATE_KR_PER_KW_MONTH,
    EXPORT_SPOT_FACTOR,
)
from custom_components.carmabox.optimizer.cost_model import (
    CostModel,
    EllevioState,
    ScenarioCost,
)
from custom_components.carmabox.optimizer.device_profiles import LoadSlot, Scenario

# ---- Helpers ------------------------------------------------------------


def _flat_prices(price_ore: float = 50.0) -> list[float]:
    """24-hour price list with a single flat price."""
    return [price_ore] * 24


def _night_prices(night_ore: float = 20.0, day_ore: float = 80.0) -> list[float]:
    """Cheap nights (0-05, 22-23), expensive days (06-21)."""
    prices = [day_ore] * 24
    for h in [*list(range(0, 6)), 22, 23]:
        prices[h] = night_ore
    return prices


def _ev_slot(hour: int = 23, power_kw: float = 6.9, duration_min: int = 60) -> LoadSlot:
    return LoadSlot(hour=hour, device="ev", power_kw=power_kw, duration_min=duration_min)


def _battery_slot(hour: int = 2, power_kw: float = 3.6, duration_min: int = 60) -> LoadSlot:
    return LoadSlot(
        hour=hour, device="battery_kontor", power_kw=power_kw, duration_min=duration_min
    )


def _ellevio_zero() -> EllevioState:
    """No existing peaks -- any load will establish the baseline."""
    return EllevioState(month_peak_kw=0.0, top3_weighted_hours=[])


def _ellevio_at(peak_kw: float, top3: list[float] | None = None) -> EllevioState:
    """Pre-existing peak state."""
    if top3 is None:
        top3 = [peak_kw, peak_kw, peak_kw]
    return EllevioState(month_peak_kw=peak_kw, top3_weighted_hours=top3)


def _scenario(name: str = "test", slots: list[LoadSlot] | None = None) -> Scenario:
    return Scenario(name=name, slots=slots or [])


# ---- ScenarioCost -------------------------------------------------------


class TestScenarioCostTotal:
    def test_total_is_sum_of_components(self) -> None:
        cost = ScenarioCost(
            grid_cost_kr=1.0,
            ellevio_penalty_kr=2.0,
            export_loss_kr=0.5,
            deferred_cost_kr=0.25,
        )
        assert cost.total_kr == pytest.approx(3.75, abs=0.001)

    def test_all_zeros_gives_zero(self) -> None:
        cost = ScenarioCost(
            grid_cost_kr=0.0,
            ellevio_penalty_kr=0.0,
            export_loss_kr=0.0,
            deferred_cost_kr=0.0,
        )
        assert cost.total_kr == 0.0

    def test_negative_grid_cost_allowed(self) -> None:
        """Negative spot prices produce negative grid cost -- correct."""
        cost = ScenarioCost(
            grid_cost_kr=-0.5,
            ellevio_penalty_kr=0.0,
            export_loss_kr=0.0,
            deferred_cost_kr=0.0,
        )
        assert cost.total_kr == pytest.approx(-0.5, abs=0.001)

    def test_total_is_read_only_property(self) -> None:
        cost = ScenarioCost(1.0, 2.0, 3.0, 4.0)
        # frozen dataclass -- assignment raises FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            cost.total_kr = 0.0  # type: ignore[misc]


# ---- EllevioState -------------------------------------------------------


class TestEllevioState:
    def test_defaults(self) -> None:
        state = EllevioState(month_peak_kw=2.5)
        assert state.target_kw == 2.0
        assert state.top3_weighted_hours == []

    def test_frozen(self) -> None:
        state = EllevioState(month_peak_kw=1.0)
        with pytest.raises(FrozenInstanceError):
            state.month_peak_kw = 9.9  # type: ignore[misc]


# ---- Constants ----------------------------------------------------------


class TestConstants:
    def test_ellevio_rate_defined(self) -> None:
        assert pytest.approx(81.25, abs=0.001) == ELLEVIO_RATE_KR_PER_KW_MONTH

    def test_export_spot_factor_defined(self) -> None:
        assert pytest.approx(0.8, abs=0.001) == EXPORT_SPOT_FACTOR


# ---- CostModel.calculate_scenario_cost ----------------------------------


class TestCalculateScenarioCost:
    def setup_method(self) -> None:
        self.model = CostModel()

    def test_empty_scenario_all_zeros(self) -> None:
        """Empty scenario -> ScenarioCost with all zeros."""
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("empty"),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
        )
        assert result.grid_cost_kr == 0.0
        assert result.ellevio_penalty_kr == 0.0
        assert result.export_loss_kr == 0.0
        assert result.deferred_cost_kr == 0.0
        assert result.total_kr == 0.0

    def test_grid_cost_ev_one_hour_flat_price(self) -> None:
        """EV at 6.9 kW x 1h x 50 ore/kWh = 3.45 kr."""
        slots = [_ev_slot(hour=23, power_kw=6.9, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_1h", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
        )
        expected = 6.9 * 1.0 * 50.0 / 100.0
        assert result.grid_cost_kr == pytest.approx(expected, abs=0.01)

    def test_grid_cost_battery_includes_efficiency(self) -> None:
        """Battery at 3.6 kW x 1h: grid import = 3.6 / 0.90 = 4.0 kWh."""
        slots = [_battery_slot(hour=2, power_kw=3.6, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("bat_1h", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
        )
        stored_kwh = 3.6
        import_kwh = stored_kwh / DEFAULT_BATTERY_EFFICIENCY  # 4.0
        expected = import_kwh * 50.0 / 100.0  # 2.0 kr
        assert result.grid_cost_kr == pytest.approx(expected, abs=0.01)

    def test_battery_efficiency_exactly(self) -> None:
        """Verify the exact 1/0.90 factor."""
        slots = [LoadSlot(hour=1, device="battery_forrad", power_kw=1.0, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("bat_eff", slots),
            prices_ore=_flat_prices(100.0),
            ellevio_state=_ellevio_zero(),
        )
        # 1.0 kWh stored / 0.90 = 1.111 kWh imported x 100 ore / 100 = 1.111 kr
        assert result.grid_cost_kr == pytest.approx(1.0 / DEFAULT_BATTERY_EFFICIENCY, abs=0.001)

    def test_negative_spot_price_gives_negative_grid_cost(self) -> None:
        """Negative spot prices produce negative grid cost -- correct behaviour."""
        prices = _flat_prices(-10.0)  # -10 ore/kWh
        slots = [_ev_slot(hour=3, power_kw=6.9, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("neg_price", slots),
            prices_ore=prices,
            ellevio_state=_ellevio_zero(),
        )
        assert result.grid_cost_kr < 0.0

    def test_ellevio_penalty_new_peak_raises_cost(self) -> None:
        """Daytime slot raises projected top-3 -> positive penalty."""
        # Current top-3: all 2.0 kW (weighted), new peak at 10:00 = 5.0 kW x 1.0 = 5.0
        state = _ellevio_at(2.0, [2.0, 2.0, 2.0])
        slots = [LoadSlot(hour=10, device="ev", power_kw=5.0, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("day_peak", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=state,
        )
        # New top-3 = [5.0, 2.0, 2.0] -> avg = 3.0, delta = 1.0
        expected_penalty = (3.0 - 2.0) * ELLEVIO_RATE_KR_PER_KW_MONTH
        assert result.ellevio_penalty_kr == pytest.approx(expected_penalty, abs=0.01)

    def test_ellevio_penalty_night_half_weight(self) -> None:
        """Night slot (hour=23) has 0.5 Ellevio weight -- reduces projected peak."""
        state = _ellevio_at(2.0, [2.0, 2.0, 2.0])
        # 6.9 kW at night x 0.5 = 3.45 weighted kW -> new top-3 = [3.45, 2.0, 2.0] -> avg=2.48
        slots = [LoadSlot(hour=23, device="ev", power_kw=6.9, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("night_ev", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=state,
        )
        new_weighted = 6.9 * 0.5  # 3.45
        new_avg = (new_weighted + 2.0 + 2.0) / 3
        expected_penalty = (new_avg - 2.0) * ELLEVIO_RATE_KR_PER_KW_MONTH
        assert result.ellevio_penalty_kr == pytest.approx(expected_penalty, abs=0.01)

    def test_ellevio_penalty_no_new_peak_is_zero(self) -> None:
        """Load stays below existing top-3 -> no Ellevio penalty."""
        # Existing top-3 are all 10.0 kW -> new 1.0 kW won't enter top-3
        state = _ellevio_at(10.0, [10.0, 10.0, 10.0])
        slots = [LoadSlot(hour=14, device="ev", power_kw=1.0, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("low_peak", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=state,
        )
        assert result.ellevio_penalty_kr == 0.0

    def test_deferred_cost_without_tomorrow_prices_is_zero(self) -> None:
        """No tomorrow prices -> deferred cost = 0."""
        slots = [_ev_slot()]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_night", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
        )
        assert result.deferred_cost_kr == 0.0

    def test_deferred_cost_with_tomorrow_prices(self) -> None:
        """With tomorrow prices -> deferred cost > 0."""
        slots = [_ev_slot(hour=23, power_kw=6.9, duration_min=60)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_night", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
            tomorrow_prices_ore=_flat_prices(30.0),
        )
        assert result.deferred_cost_kr > 0.0

    def test_export_loss_is_zero_for_night_scenario(self) -> None:
        """Night scenarios have no PV export opportunity -> export_loss = 0."""
        slots = [_ev_slot(hour=2)]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_night", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
        )
        assert result.export_loss_kr == 0.0

    def test_multiple_slots_grid_cost_additive(self) -> None:
        """Multiple slots -- total grid cost = sum of individual costs."""
        prices = _flat_prices(40.0)
        slots = [
            LoadSlot(hour=22, device="ev", power_kw=6.9, duration_min=60),
            LoadSlot(hour=23, device="ev", power_kw=6.9, duration_min=60),
        ]
        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_2h", slots),
            prices_ore=prices,
            ellevio_state=_ellevio_zero(),
        )
        expected = 2 * 6.9 * 40.0 / 100.0
        assert result.grid_cost_kr == pytest.approx(expected, abs=0.01)


# ---- CostModel.calculate_night_cost -------------------------------------


class TestCalculateNightCost:
    def setup_method(self) -> None:
        self.model = CostModel()

    def test_empty_slots_zero_cost(self) -> None:
        assert self.model.calculate_night_cost([], _flat_prices()) == 0.0

    def test_single_slot_correct_cost(self) -> None:
        """3.0 kW x 0.5h x 20 ore = 0.30 kr."""
        slot = LoadSlot(hour=3, device="ev", power_kw=3.0, duration_min=30)
        result = self.model.calculate_night_cost([slot], _flat_prices(20.0))
        assert result == pytest.approx(3.0 * 0.5 * 20.0 / 100.0, abs=0.001)

    def test_battery_efficiency_applied(self) -> None:
        """Battery device has efficiency adjustment in night cost too."""
        slot = LoadSlot(hour=2, device="battery_kontor", power_kw=3.6, duration_min=60)
        result = self.model.calculate_night_cost([slot], _flat_prices(50.0))
        expected = (3.6 / DEFAULT_BATTERY_EFFICIENCY) * 50.0 / 100.0
        assert result == pytest.approx(expected, abs=0.001)


# ---- CostModel.cheapest_hours -------------------------------------------


class TestCheapestHours:
    def setup_method(self) -> None:
        self.model = CostModel()

    def test_wrap_around_returns_correct_hours(self) -> None:
        """Night window 22-06 wraps midnight correctly."""
        prices = _night_prices(night_ore=10.0, day_ore=100.0)
        hours = self.model.cheapest_hours(prices, hours_needed=4)
        # All night hours are cheap: [0,1,2,3,4,5,22,23] -- pick 4 cheapest (all same price)
        assert len(hours) == 4
        for h in hours:
            assert h in [*list(range(0, 6)), 22, 23]

    def test_returns_sorted_by_hour(self) -> None:
        """Result is sorted by hour index, not by price."""
        prices = list(range(24))  # Hour 0 = cheapest, hour 23 = most expensive
        hours = self.model.cheapest_hours(prices, hours_needed=3)
        assert hours == sorted(hours)

    def test_returns_requested_count(self) -> None:
        prices = _flat_prices(50.0)
        hours = self.model.cheapest_hours(prices, hours_needed=3)
        assert len(hours) == 3

    def test_hours_needed_zero_returns_empty(self) -> None:
        prices = _flat_prices(50.0)
        hours = self.model.cheapest_hours(prices, hours_needed=0)
        assert hours == []

    def test_capped_at_window_size(self) -> None:
        """Requesting more hours than window size returns window size."""
        prices = _flat_prices(50.0)
        # Window 22-06 = 8 hours
        hours = self.model.cheapest_hours(prices, hours_needed=100)
        assert len(hours) == 8

    def test_cheapest_hour_selected_first(self) -> None:
        """Verify the actual cheapest hour appears in result."""
        prices = _flat_prices(50.0)
        prices[1] = 5.0  # Hour 1 is cheapest in night window
        hours = self.model.cheapest_hours(prices, hours_needed=1)
        assert hours == [1]

    def test_non_wrapping_window(self) -> None:
        """Window that does not cross midnight (window_start < window_end)."""
        prices = _flat_prices(50.0)
        prices[10] = 5.0  # Cheapest in daytime window
        hours = self.model.cheapest_hours(prices, hours_needed=1, window_start=8, window_end=16)
        assert hours == [10]

    def test_negative_prices_selected_first(self) -> None:
        """Negative spot prices rank as cheapest."""
        prices = _flat_prices(50.0)
        prices[4] = -20.0  # Hour 4 = very cheap
        hours = self.model.cheapest_hours(prices, hours_needed=1)
        assert hours == [4]


# ---- Deferred cost ------------------------------------------------------


class TestDeferredCost:
    def setup_method(self) -> None:
        self.model = CostModel()

    def test_deferred_cheaper_tomorrow(self) -> None:
        """Tomorrow cheaper -> deferred_cost < today's grid cost."""
        slots = [_ev_slot(hour=14, power_kw=6.9, duration_min=60)]  # Daytime today
        today_prices = _flat_prices(80.0)
        tomorrow_prices = _flat_prices(20.0)

        result = self.model.calculate_scenario_cost(
            scenario=_scenario("day_ev", slots),
            prices_ore=today_prices,
            ellevio_state=_ellevio_zero(),
            tomorrow_prices_ore=tomorrow_prices,
        )
        # Deferred cost should use 20 ore vs today's 80 ore
        assert result.deferred_cost_kr < result.grid_cost_kr

    def test_deferred_cost_exact_value(self) -> None:
        """1 h at 30 ore -> deferred cost = 6.9 kWh x 30 / 100 = 2.07 kr."""
        slots = [LoadSlot(hour=3, device="ev", power_kw=6.9, duration_min=60)]
        tomorrow_prices = _flat_prices(30.0)

        result = self.model.calculate_scenario_cost(
            scenario=_scenario("ev_d", slots),
            prices_ore=_flat_prices(50.0),
            ellevio_state=_ellevio_zero(),
            tomorrow_prices_ore=tomorrow_prices,
        )
        expected = 6.9 * 30.0 / 100.0
        assert result.deferred_cost_kr == pytest.approx(expected, abs=0.01)


# ---- to_dict ------------------------------------------------------------


class TestToDict:
    def test_scenario_cost_to_dict_contains_all_fields(self) -> None:
        sc = ScenarioCost(
            grid_cost_kr=1.0,
            ellevio_penalty_kr=2.0,
            export_loss_kr=0.5,
            deferred_cost_kr=0.3,
        )
        d = sc.to_dict()
        assert d["grid_cost_kr"] == pytest.approx(1.0)
        assert d["ellevio_penalty_kr"] == pytest.approx(2.0)
        assert d["export_loss_kr"] == pytest.approx(0.5)
        assert d["deferred_cost_kr"] == pytest.approx(0.3)
        assert d["total_kr"] == pytest.approx(3.8)

    def test_ellevio_state_to_dict_contains_all_fields(self) -> None:
        es = EllevioState(
            month_peak_kw=1.5,
            top3_weighted_hours=[1.2, 1.4, 1.5],
            target_kw=2.0,
        )
        d = es.to_dict()
        assert d["month_peak_kw"] == pytest.approx(1.5)
        assert d["top3_weighted_hours"] == [1.2, 1.4, 1.5]
        assert d["target_kw"] == pytest.approx(2.0)
