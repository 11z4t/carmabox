"""Tests for custom_components.carmabox.optimizer.scenario_engine.

PLAT-1223: Scenario Engine Core -- ScenarioEngine generation, scoring, selection.
"""

from __future__ import annotations

import pytest

from custom_components.carmabox.const import SCENARIO_MAX_COUNT, SCENARIO_MIN_COUNT
from custom_components.carmabox.optimizer.cost_model import CostModel, EllevioState
from custom_components.carmabox.optimizer.device_profiles import build_profiles, can_coexist
from custom_components.carmabox.optimizer.scenario_engine import ScenarioEngine

# ── Helpers ────────────────────────────────────────────────────────────────


def _engine() -> ScenarioEngine:
    return ScenarioEngine(profiles=build_profiles({}), cost_model=CostModel())


def _night_prices(night: float = 20.0, day: float = 80.0) -> list[float]:
    """Cheap-night, expensive-day Nordpool prices."""
    prices = [day] * 24
    for h in [22, 23, 0, 1, 2, 3, 4, 5]:
        prices[h] = night
    return prices


def _flat_prices(price: float = 50.0) -> list[float]:
    return [price] * 24


def _ellevio_zero() -> EllevioState:
    return EllevioState(month_peak_kw=0.0, top3_weighted_hours=[])


def _base_state() -> dict:  # type: ignore[type-arg]
    """Standard night state: EV at 50 %, batteries at 20 %, target EV 90 %."""
    return {
        "battery_soc": 20.0,
        "ev_soc": 50.0,
        "ev_target_soc": 90.0,
        "battery_target_soc": 80.0,
        "hours": list(range(22, 24)) + list(range(0, 6)),  # [22,23,0,1,2,3,4,5] = 8 h
        "prices_ore": _night_prices(),
        "pv_tomorrow_kwh": 5.0,
    }


def _slot_count(scenario, device_prefix: str) -> int:  # type: ignore[no-untyped-def]
    return sum(1 for s in scenario.slots if s.device.startswith(device_prefix))


# ── 1. Count constraints ────────────────────────────────────────────────────


def test_generate_returns_at_least_min_count() -> None:
    scenarios = _engine().generate_scenarios(_base_state())
    assert len(scenarios) >= SCENARIO_MIN_COUNT


def test_generate_returns_at_most_max_count() -> None:
    scenarios = _engine().generate_scenarios(_base_state())
    assert len(scenarios) <= SCENARIO_MAX_COUNT


def test_generate_returns_exactly_fifteen_normally() -> None:
    """Five templates x three deltas = 15 scenarios in a normal state."""
    scenarios = _engine().generate_scenarios(_base_state())
    assert len(scenarios) == 15


# ── 2. can_coexist constraint ──────────────────────────────────────────────


def test_all_scenarios_respect_can_coexist() -> None:
    """No two devices in the same hour may violate can_coexist()."""
    eng = _engine()
    scenarios = eng.generate_scenarios(_base_state())
    profiles = eng.profiles
    for sc in scenarios:
        hour_devices: dict[int, list[str]] = {}
        for slot in sc.slots:
            hour_devices.setdefault(slot.hour, []).append(slot.device)
        for hour, devices in hour_devices.items():
            for i, a in enumerate(devices):
                for b in devices[i + 1 :]:
                    prof_a = profiles[a]
                    prof_b = profiles[b]
                    assert can_coexist(prof_a, prof_b), (
                        f"Scenario '{sc.name}' hour {hour}: {a} and {b} cannot coexist"
                    )


def test_no_hour_has_two_heavy_loads() -> None:
    """Never schedule more than one load >2 kW in the same hour."""
    heavy_kw = 2.0
    scenarios = _engine().generate_scenarios(_base_state())
    for sc in scenarios:
        hour_power: dict[int, list[float]] = {}
        for slot in sc.slots:
            hour_power.setdefault(slot.hour, []).append(slot.power_kw)
        for hour, powers in hour_power.items():
            heavy = [p for p in powers if p > heavy_kw]
            assert len(heavy) <= 1, (
                f"Scenario '{sc.name}' hour {hour} has {len(heavy)} heavy loads: {powers}"
            )


# ── 3. Template slot distributions ────────────────────────────────────────


def test_ev_heavy_has_more_ev_slots_than_battery_heavy() -> None:
    """In constrained window, ev_heavy gives EV priority over battery_heavy."""
    scenarios = _engine().generate_scenarios(_base_state())
    ev_heavy = next(s for s in scenarios if s.name == "ev_heavy")
    battery_heavy = next(s for s in scenarios if s.name == "battery_heavy")
    assert _slot_count(ev_heavy, "ev") > _slot_count(battery_heavy, "ev")


def test_battery_heavy_has_more_battery_slots_than_ev_heavy() -> None:
    """In constrained window, battery_heavy gives batteries priority over ev_heavy."""
    scenarios = _engine().generate_scenarios(_base_state())
    ev_heavy = next(s for s in scenarios if s.name == "ev_heavy")
    battery_heavy = next(s for s in scenarios if s.name == "battery_heavy")
    assert _slot_count(battery_heavy, "battery_") > _slot_count(ev_heavy, "battery_")


def test_minimal_has_fewer_ev_slots_than_ev_heavy() -> None:
    """Minimal schedules EV only to 75 % (less than ev_heavy's full target)."""
    state = _base_state()
    state["ev_target_soc"] = 90.0  # target > 75 ensures min_ev_h < ev_h
    scenarios = _engine().generate_scenarios(state)
    ev_heavy = next(s for s in scenarios if s.name == "ev_heavy")
    minimal = next(s for s in scenarios if s.name == "minimal")
    assert _slot_count(minimal, "ev") <= _slot_count(ev_heavy, "ev")


def test_skip_ev_base_has_no_ev_slots() -> None:
    """The 'skip_ev' scenario must contain zero EV slots."""
    scenarios = _engine().generate_scenarios(_base_state())
    skip_ev = next(s for s in scenarios if s.name == "skip_ev")
    assert _slot_count(skip_ev, "ev") == 0


def test_all_skip_ev_variants_have_no_ev_slots() -> None:
    """All three skip_ev variants (minus1/base/plus1) have no EV slots."""
    scenarios = _engine().generate_scenarios(_base_state())
    skip_variants = [s for s in scenarios if s.name.startswith("skip_ev")]
    assert len(skip_variants) == 3
    for sc in skip_variants:
        assert _slot_count(sc, "ev") == 0, f"{sc.name} unexpectedly has EV slots"


# ── 4. EV disconnected ─────────────────────────────────────────────────────


def test_ev_disconnected_no_ev_slots_in_any_scenario() -> None:
    """When ev_soc == -1, every generated scenario must have zero EV slots."""
    state = _base_state()
    state["ev_soc"] = -1.0
    scenarios = _engine().generate_scenarios(state)
    for sc in scenarios:
        assert _slot_count(sc, "ev") == 0, f"Scenario '{sc.name}' has EV slots when disconnected"


# ── 5. Scoring ─────────────────────────────────────────────────────────────


def test_score_scenarios_sorted_cheapest_first() -> None:
    """score_scenarios() must return scenarios sorted by total_cost_kr ascending."""
    eng = _engine()
    scenarios = eng.generate_scenarios(_base_state())
    scored = eng.score_scenarios(scenarios, _night_prices(), _ellevio_zero())
    costs = [s.total_cost_kr for s in scored]
    assert costs == sorted(costs)


def test_score_scenarios_updates_total_cost_kr() -> None:
    """Returned scenarios have total_cost_kr set (not the default 0.0 for all)."""
    eng = _engine()
    state = _base_state()
    state["ev_soc"] = 20.0  # ensure significant charging load → non-zero cost
    scenarios = eng.generate_scenarios(state)
    scored = eng.score_scenarios(scenarios, _night_prices(), _ellevio_zero())
    # At least some scenarios should have non-zero cost
    assert any(s.total_cost_kr != 0.0 for s in scored)


def test_score_scenarios_empty_list() -> None:
    """score_scenarios() with empty input returns empty list."""
    eng = _engine()
    result = eng.score_scenarios([], _flat_prices(), _ellevio_zero())
    assert result == []


def test_score_respects_price_difference() -> None:
    """A scenario with cheaper-hour slots should rank lower (cheaper) than expensive hours."""
    eng = _engine()
    prices = _flat_prices(50.0)
    prices[0] = 10.0  # hour 0 is cheapest
    prices[1] = 200.0  # hour 1 is most expensive

    from custom_components.carmabox.optimizer.device_profiles import LoadSlot, Scenario

    cheap_sc = Scenario(
        name="cheap",
        slots=[LoadSlot(hour=0, device="ev", power_kw=6.9)],
    )
    expensive_sc = Scenario(
        name="expensive",
        slots=[LoadSlot(hour=1, device="ev", power_kw=6.9)],
    )
    scored = eng.score_scenarios([expensive_sc, cheap_sc], prices, _ellevio_zero())
    assert scored[0].name == "cheap"
    assert scored[1].name == "expensive"


# ── 6. Selection ──────────────────────────────────────────────────────────


def test_select_best_returns_first_scenario() -> None:
    """select_best() returns the first (cheapest) scenario."""
    eng = _engine()
    scenarios = eng.generate_scenarios(_base_state())
    scored = eng.score_scenarios(scenarios, _night_prices(), _ellevio_zero())
    best = eng.select_best(scored)
    assert best is scored[0]


def test_select_best_returns_cheapest_by_cost() -> None:
    """select_best() returns the scenario with the lowest total_cost_kr."""
    eng = _engine()
    scenarios = eng.generate_scenarios(_base_state())
    scored = eng.score_scenarios(scenarios, _night_prices(), _ellevio_zero())
    best = eng.select_best(scored)
    assert best.total_cost_kr == min(s.total_cost_kr for s in scored)


def test_select_best_empty_list_returns_fallback() -> None:
    """select_best([]) must return the fallback scenario, not raise."""
    result = _engine().select_best([])
    assert result.name == "fallback"
    assert result.slots == []


# ── 7. Fallback ────────────────────────────────────────────────────────────


def test_fallback_has_empty_slots() -> None:
    fallback = _engine()._generate_fallback()
    assert fallback.slots == []


def test_fallback_name() -> None:
    fallback = _engine()._generate_fallback()
    assert fallback.name == "fallback"


def test_fallback_has_zero_targets() -> None:
    fallback = _engine()._generate_fallback()
    assert fallback.ev_target_soc == 0.0
    assert fallback.battery_target_soc == 0.0


# ── 8. Edge cases ──────────────────────────────────────────────────────────


def test_all_prices_equal_generates_valid_scenarios() -> None:
    """Flat price list still produces valid scenarios with correct count."""
    state = _base_state()
    state["prices_ore"] = _flat_prices(50.0)
    scenarios = _engine().generate_scenarios(state)
    assert SCENARIO_MIN_COUNT <= len(scenarios) <= SCENARIO_MAX_COUNT


def test_zero_energy_needed_all_devices_full() -> None:
    """When all devices are already at target, scenarios have minimal/no slots."""
    state = {
        "battery_soc": 100.0,
        "ev_soc": 100.0,
        "ev_target_soc": 100.0,
        "battery_target_soc": 100.0,
        "hours": list(range(22, 24)) + list(range(0, 6)),
        "prices_ore": _flat_prices(),
        "pv_tomorrow_kwh": 0.0,
    }
    scenarios = _engine().generate_scenarios(state)
    assert SCENARIO_MIN_COUNT <= len(scenarios) <= SCENARIO_MAX_COUNT
    # All slots should be empty when nothing needs charging
    for sc in scenarios:
        assert sc.slots == [], f"Scenario '{sc.name}' has slots when fully charged"


def test_two_hours_available_adapts_without_error() -> None:
    """Only 2 hours available: engine must not raise and obey count bounds."""
    state = _base_state()
    state["hours"] = [0, 1]
    scenarios = _engine().generate_scenarios(state)
    assert SCENARIO_MIN_COUNT <= len(scenarios) <= SCENARIO_MAX_COUNT
    # All slot hours must be within the 2 available hours
    for sc in scenarios:
        for slot in sc.slots:
            assert slot.hour in {0, 1}, f"Hour {slot.hour} not in available hours"


def test_scenario_names_contain_expected_templates() -> None:
    """All five template names appear in the generated scenario names."""
    scenarios = _engine().generate_scenarios(_base_state())
    names = {s.name for s in scenarios}
    for template in ("ev_heavy", "battery_heavy", "balanced", "minimal", "skip_ev"):
        assert template in names, f"Template '{template}' missing from scenario names"


def test_all_scenarios_have_battery_target_soc() -> None:
    """All generated scenarios carry the correct battery_target_soc."""
    state = _base_state()
    state["battery_target_soc"] = 70.0
    scenarios = _engine().generate_scenarios(state)
    for sc in scenarios:
        assert sc.battery_target_soc == pytest.approx(70.0), (
            f"Scenario '{sc.name}' has wrong battery_target_soc"
        )


def test_ev_soc_above_75_battery_heavy_has_no_ev_slots() -> None:
    """When EV SoC > 75 %, battery_heavy needs no EV slots (min_ev_h=0)."""
    state = _base_state()
    state["ev_soc"] = 80.0
    state["ev_target_soc"] = 90.0
    scenarios = _engine().generate_scenarios(state)
    battery_heavy = next(s for s in scenarios if s.name == "battery_heavy")
    assert _slot_count(battery_heavy, "ev") == 0
