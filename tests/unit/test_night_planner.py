"""Tests for custom_components.carmabox.optimizer.night_planner.

PLAT-1226: Night Planner — NightPlanner, NightPlan, NightSlot,
calculate_ev_trajectory.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.carmabox.const import MAX_NIGHTLY_SOC_DELTA_PCT
from custom_components.carmabox.optimizer.cost_model import CostModel, EllevioState
from custom_components.carmabox.optimizer.device_profiles import build_profiles
from custom_components.carmabox.optimizer.night_planner import (
    NightPlan,
    NightPlanner,
    NightSlot,
    calculate_ev_trajectory,
)
from custom_components.carmabox.optimizer.scenario_engine import ScenarioEngine

# ── Helpers ────────────────────────────────────────────────────────────────


def _engine() -> ScenarioEngine:
    return ScenarioEngine(profiles=build_profiles({}), cost_model=CostModel())


def _planner() -> NightPlanner:
    return NightPlanner(engine=_engine(), cost_model=CostModel())


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
    """Standard night state: EV at 50 %, batteries at 20 %, no PV tomorrow."""
    return {
        "battery_soc": 20.0,
        "ev_soc": 50.0,
        "ev_days_since_full": 2,
        "prices_ore": _night_prices(),
        "tomorrow_prices_ore": None,
        "pv_tomorrow_kwh": 10.0,
        "ellevio_state": _ellevio_zero(),
        "dishwasher_needed": False,
        "current_hour": 22,
    }


# ── 1. calculate_ev_trajectory ─────────────────────────────────────────────


def test_calculate_ev_trajectory_tactical_days_lt_4() -> None:
    """days_since_full < 4 → tactical +5 %, floor at 75."""
    # current_soc=75 + 5 = 80
    result = calculate_ev_trajectory(75.0, 3)
    assert result == pytest.approx(80.0)


def test_calculate_ev_trajectory_tactical_floor_at_75() -> None:
    """Tactical case: result never below 75 even when current_soc is very low."""
    result = calculate_ev_trajectory(40.0, 1)
    assert result >= 75.0


def test_calculate_ev_trajectory_spread_days_4() -> None:
    """days_since_full == 4 → spread: current + 20, min 75."""
    # current_soc=75, days=4 → max(75, min(100, 75+20)) = 95
    result = calculate_ev_trajectory(75.0, 4)
    assert result == pytest.approx(95.0)


def test_calculate_ev_trajectory_spread_days_5() -> None:
    """days_since_full == 5 → spread: current + 20."""
    result = calculate_ev_trajectory(75.0, 5)
    assert result == pytest.approx(95.0)


def test_calculate_ev_trajectory_deadline_days_6() -> None:
    """days_since_full >= 6 → always 100 %."""
    assert calculate_ev_trajectory(75.0, 6) == pytest.approx(100.0)


def test_calculate_ev_trajectory_deadline_days_7() -> None:
    """days_since_full = 7 → 100 %."""
    assert calculate_ev_trajectory(50.0, 7) == pytest.approx(100.0)


def test_calculate_ev_trajectory_max_delta_respected_tactical() -> None:
    """Tactical case: result - current_soc <= MAX_NIGHTLY_SOC_DELTA_PCT."""
    result = calculate_ev_trajectory(80.0, 3)
    assert result - 80.0 <= MAX_NIGHTLY_SOC_DELTA_PCT


def test_calculate_ev_trajectory_max_delta_respected_spread() -> None:
    """Spread case: result - current_soc <= MAX_NIGHTLY_SOC_DELTA_PCT."""
    result = calculate_ev_trajectory(85.0, 5)
    assert result - 85.0 <= MAX_NIGHTLY_SOC_DELTA_PCT


def test_calculate_ev_trajectory_clamp_at_100() -> None:
    """Result never exceeds 100 %."""
    assert calculate_ev_trajectory(95.0, 5) <= 100.0
    assert calculate_ev_trajectory(99.0, 6) == pytest.approx(100.0)


# ── 2. Battery target from PV forecast ────────────────────────────────────


def test_battery_target_low_pv_below_5() -> None:
    """pv < 5 kWh → battery target = 100 %."""
    state = _base_state()
    state["pv_tomorrow_kwh"] = 2.0
    plan = _planner().plan_tonight(state)
    assert plan.battery_target_soc == pytest.approx(100.0)


def test_battery_target_mid_pv_5_to_15() -> None:
    """pv in [5, 15] → battery target in (70, 100)."""
    state = _base_state()
    state["pv_tomorrow_kwh"] = 10.0
    plan = _planner().plan_tonight(state)
    assert 70.0 < plan.battery_target_soc < 100.0


def test_battery_target_mid_pv_15_to_30() -> None:
    """pv in [15, 30] → battery target in (45, 70)."""
    state = _base_state()
    state["pv_tomorrow_kwh"] = 22.0
    plan = _planner().plan_tonight(state)
    assert 45.0 <= plan.battery_target_soc <= 70.0


def test_battery_target_high_pv_above_30() -> None:
    """pv > 30 kWh → battery target in [35, 45]."""
    state = _base_state()
    state["pv_tomorrow_kwh"] = 35.0
    plan = _planner().plan_tonight(state)
    assert 35.0 <= plan.battery_target_soc <= 45.0


# ── 3. plan_tonight happy path ─────────────────────────────────────────────


def test_plan_tonight_returns_nightplan() -> None:
    """plan_tonight returns a NightPlan instance."""
    plan = _planner().plan_tonight(_base_state())
    assert isinstance(plan, NightPlan)


def test_plan_tonight_ev_target_soc_set() -> None:
    """ev_target_soc is set correctly from calculate_ev_trajectory."""
    state = _base_state()
    state["ev_soc"] = 75.0
    state["ev_days_since_full"] = 3
    plan = _planner().plan_tonight(state)
    # days=3 tactical: max(75, 75+5)=80
    assert plan.ev_target_soc == pytest.approx(80.0)


def test_plan_tonight_battery_target_soc_set() -> None:
    """battery_target_soc matches _battery_target_from_pv."""
    state = _base_state()
    state["pv_tomorrow_kwh"] = 0.0
    plan = _planner().plan_tonight(state)
    assert plan.battery_target_soc == pytest.approx(100.0)


def test_plan_tonight_scenario_name_populated() -> None:
    """scenario_name is a non-empty string after a successful plan."""
    plan = _planner().plan_tonight(_base_state())
    assert plan.scenario_name != ""


def test_plan_tonight_slots_within_night_window() -> None:
    """All plan slots use hours within the default night window [22..5]."""
    state = _base_state()
    state["current_hour"] = 22
    plan = _planner().plan_tonight(state)
    valid_hours = set(range(22, 24)) | set(range(0, 6))
    for slot in plan.slots:
        assert slot.hour in valid_hours, f"Slot at hour {slot.hour} outside night window"


# ── 4. EV disconnected ─────────────────────────────────────────────────────


def test_ev_disconnected_ev_skipped_true() -> None:
    """ev_soc == -1 → ev_skipped=True."""
    state = _base_state()
    state["ev_soc"] = -1
    plan = _planner().plan_tonight(state)
    assert plan.ev_skipped is True


def test_ev_disconnected_skip_reason_disconnected() -> None:
    """ev_soc == -1 → ev_skip_reason == 'disconnected'."""
    state = _base_state()
    state["ev_soc"] = -1
    plan = _planner().plan_tonight(state)
    assert plan.ev_skip_reason == "disconnected"


def test_ev_disconnected_no_ev_slots() -> None:
    """When EV is disconnected, no EV slots are created."""
    state = _base_state()
    state["ev_soc"] = -1
    plan = _planner().plan_tonight(state)
    ev_slots = [s for s in plan.slots if s.device == "ev"]
    assert ev_slots == []


# ── 5. Dishwasher ──────────────────────────────────────────────────────────


def test_dishwasher_included_when_needed() -> None:
    """dishwasher_needed=True → dishwasher slot in plan."""
    state = _base_state()
    state["dishwasher_needed"] = True
    plan = _planner().plan_tonight(state)
    devices = [s.device for s in plan.slots]
    assert "dishwasher" in devices


def test_dishwasher_not_included_when_not_needed() -> None:
    """dishwasher_needed=False (default) → no dishwasher slot."""
    plan = _planner().plan_tonight(_base_state())
    devices = [s.device for s in plan.slots]
    assert "dishwasher" not in devices


# ── 6. Fallback ────────────────────────────────────────────────────────────


def test_fallback_on_empty_scenario_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_scenarios returns [] → scenario_name == 'fallback'."""
    eng = _engine()
    monkeypatch.setattr(eng, "generate_scenarios", lambda _state: [])
    plan = NightPlanner(engine=eng, cost_model=CostModel()).plan_tonight(_base_state())
    assert plan.scenario_name == "fallback"


def test_fallback_has_battery_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallback plan contains at least one battery slot."""
    eng = _engine()
    monkeypatch.setattr(eng, "generate_scenarios", lambda _state: [])
    plan = NightPlanner(engine=eng, cost_model=CostModel()).plan_tonight(_base_state())
    battery_slots = [s for s in plan.slots if s.device.startswith("battery")]
    assert len(battery_slots) > 0


def test_fallback_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_scenarios raises → scenario_name == 'fallback'."""
    eng = _engine()

    def _raise(_state: object) -> list[object]:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(eng, "generate_scenarios", _raise)
    plan = NightPlanner(engine=eng, cost_model=CostModel()).plan_tonight(_base_state())
    assert plan.scenario_name == "fallback"


# ── 7. NightPlan serialization ─────────────────────────────────────────────


def test_nightplan_to_dict_returns_dict() -> None:
    """to_dict() returns a plain dict."""
    plan = NightPlan(
        slots=[NightSlot(hour=1, device="battery_kontor", power_kw=3.6)],
        total_cost_kr=1.5,
        ev_target_soc=80.0,
        battery_target_soc=70.0,
        scenario_name="balanced",
        created_at=datetime(2026, 4, 3, 22, 0, 0),
    )
    d = plan.to_dict()
    assert isinstance(d, dict)


def test_nightplan_to_dict_expected_keys() -> None:
    """to_dict() contains all required keys."""
    plan = NightPlan(
        slots=[],
        total_cost_kr=0.0,
        ev_target_soc=75.0,
        battery_target_soc=100.0,
        scenario_name="minimal",
        created_at=datetime(2026, 4, 3, 23, 0, 0),
    )
    d = plan.to_dict()
    for key in (
        "scenario_name",
        "total_cost_kr",
        "ev_target_soc",
        "battery_target_soc",
        "ev_skipped",
        "ev_skip_reason",
        "created_at",
        "slots",
    ):
        assert key in d, f"Missing key: {key}"


def test_nightplan_to_dict_created_at_isoformat() -> None:
    """to_dict() serializes created_at as ISO 8601 string."""
    ts = datetime(2026, 4, 3, 22, 30, 0)
    plan = NightPlan(created_at=ts)
    d = plan.to_dict()
    assert d["created_at"] == ts.isoformat()


def test_nightplan_to_dict_slot_structure() -> None:
    """Each slot in to_dict() has hour, device, power_kw, duration_min, reason."""
    plan = NightPlan(
        slots=[NightSlot(hour=3, device="ev", power_kw=6.9, duration_min=60, reason="test")],
        created_at=datetime(2026, 4, 3, 22, 0, 0),
    )
    d = plan.to_dict()
    assert len(d["slots"]) == 1
    slot_d = d["slots"][0]
    assert slot_d["hour"] == 3
    assert slot_d["device"] == "ev"
    assert slot_d["power_kw"] == pytest.approx(6.9)
    assert slot_d["duration_min"] == 60
    assert slot_d["reason"] == "test"


# ── 8. Night window edge cases ─────────────────────────────────────────────


def test_edge_case_only_two_hours_remaining() -> None:
    """current_hour=4 → only hours 4 and 5 available."""
    state = _base_state()
    state["current_hour"] = 4
    plan = _planner().plan_tonight(state)
    for slot in plan.slots:
        assert slot.hour in (4, 5), f"Slot at hour {slot.hour} outside [4,5] window"


def test_night_window_from_daytime_starts_at_22() -> None:
    """When called during daytime (e.g. hour 14), window starts at 22."""
    state = _base_state()
    state["current_hour"] = 14
    plan = _planner().plan_tonight(state)
    valid_hours = set(range(22, 24)) | set(range(0, 6))
    for slot in plan.slots:
        assert slot.hour in valid_hours


# ── 9. Dataclass properties ────────────────────────────────────────────────


def test_nightslot_is_frozen() -> None:
    """NightSlot is a frozen dataclass — assignment raises FrozenInstanceError."""
    slot = NightSlot(hour=1, device="ev", power_kw=6.9)
    with pytest.raises(Exception):  # noqa: B017
        slot.hour = 2  # type: ignore[misc]


def test_nightplan_is_frozen() -> None:
    """NightPlan is a frozen dataclass — assignment raises FrozenInstanceError."""
    plan = NightPlan()
    with pytest.raises(Exception):  # noqa: B017
        plan.total_cost_kr = 99.0  # type: ignore[misc]


# ── 10. Multi-night / skip-EV scenario ─────────────────────────────────────


def test_no_ev_slots_when_soc_above_75_and_tomorrow_cheap() -> None:
    """EV SoC > 75 % + tomorrow very cheap → plan has no EV charging slots.

    When EV is already above the 75 % minimum, neither battery_heavy nor
    skip_ev include EV slots (min_ev_h=0). The cheapest scenario selected
    should therefore contain no EV device slots.
    """
    state = _base_state()
    state["ev_soc"] = 82.0  # above 75 % minimum
    state["ev_days_since_full"] = 1  # tactical: target = max(75, 82+5)=87
    state["prices_ore"] = _flat_prices(200.0)  # very expensive today
    state["tomorrow_prices_ore"] = _flat_prices(5.0)  # very cheap tomorrow
    plan = _planner().plan_tonight(state)
    # No EV charging slots since ev_soc > 75 and min_ev_h == 0
    ev_slots = [s for s in plan.slots if s.device == "ev"]
    assert ev_slots == []
