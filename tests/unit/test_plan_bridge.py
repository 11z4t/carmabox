"""Tests for optimizer/plan_bridge.py — PLAT-1228."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

import pytest

from custom_components.carmabox.const import (
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_NIGHT_WEIGHT,
    MAX_EV_CURRENT,
)
from custom_components.carmabox.optimizer.night_planner import NightPlan, NightSlot
from custom_components.carmabox.optimizer.plan_bridge import (
    NightPlanBridge,
    PlanSelector,
    _calc_ev_amps,
    load_night_plan,
    save_night_plan,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_slot(
    device: str,
    hour: int = 23,
    power_kw: float = 2.0,
    reason: str = "",
) -> NightSlot:
    return NightSlot(hour=hour, device=device, power_kw=power_kw, reason=reason)


def _make_plan(slots: list[NightSlot]) -> NightPlan:
    return NightPlan(
        slots=slots,
        total_cost_kr=1.5,
        ev_target_soc=80.0,
        battery_target_soc=70.0,
        scenario_name="test",
        created_at=datetime(2026, 4, 1, 22, 0, 0),
        ev_skipped=False,
        ev_skip_reason="",
    )


BRIDGE = NightPlanBridge()

# ── convert_to_plan_slots — EV slots ────────────────────────────────────────


class TestEVSlotConversion:
    def test_ev_action_is_e(self) -> None:
        slot = _make_slot("ev", power_kw=4.14)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["action"] == "e"

    def test_ev_kw_equals_slot_power(self) -> None:
        slot = _make_slot("ev", power_kw=3.5)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_kw"] == pytest.approx(3.5)

    def test_ev_amps_calculated_from_power(self) -> None:
        # 4.14 kW = 6A x 3ph x 230V -> int(4140/690) = 6
        slot = _make_slot("ev", power_kw=4.14)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_amps"] == 6

    def test_ev_amps_capped_at_max_ev_current(self) -> None:
        # 10 kW would give int(10000/690)=14A — must be capped at MAX_EV_CURRENT
        slot = _make_slot("ev", power_kw=10.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_amps"] == MAX_EV_CURRENT

    def test_ev_amps_minimum_is_default_min(self) -> None:
        # Very small power → raw amps < DEFAULT_EV_MIN_AMPS → clamped up
        slot = _make_slot("ev", power_kw=0.5)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_amps"] == DEFAULT_EV_MIN_AMPS

    def test_ev_battery_kw_is_zero(self) -> None:
        slot = _make_slot("ev", power_kw=4.14)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["battery_kw"] == 0.0

    def test_ev_miner_on_is_false(self) -> None:
        slot = _make_slot("ev", power_kw=4.14)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["miner_on"] is False


# ── convert_to_plan_slots — Battery slots ────────────────────────────────────


class TestBatterySlotConversion:
    def test_battery_kontor_action_is_g(self) -> None:
        slot = _make_slot("battery_kontor", power_kw=2.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["action"] == "g"

    def test_battery_forrad_action_is_g(self) -> None:
        slot = _make_slot("battery_forrad", power_kw=1.5)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["action"] == "g"

    def test_battery_kw_equals_slot_power(self) -> None:
        slot = _make_slot("battery_kontor", power_kw=3.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["battery_kw"] == pytest.approx(3.0)

    def test_battery_ev_kw_is_zero(self) -> None:
        slot = _make_slot("battery_kontor", power_kw=2.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_kw"] == 0.0

    def test_battery_ev_amps_is_zero(self) -> None:
        slot = _make_slot("battery_kontor", power_kw=2.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["ev_amps"] == 0


# ── convert_to_plan_slots — Miner slots ─────────────────────────────────────


class TestMinerSlotConversion:
    def test_miner_on_is_true(self) -> None:
        slot = _make_slot("miner", power_kw=0.5)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["miner_on"] is True

    def test_miner_action_is_i(self) -> None:
        slot = _make_slot("miner", power_kw=0.5)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["action"] == "i"


# ── convert_to_plan_slots — Dishwasher slots ─────────────────────────────────


class TestDishwasherSlotConversion:
    def test_dishwasher_action_is_i(self) -> None:
        slot = _make_slot("dishwasher", power_kw=1.2)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["action"] == "i"

    def test_dishwasher_miner_on_is_false(self) -> None:
        slot = _make_slot("dishwasher", power_kw=1.2)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["miner_on"] is False


# ── convert_to_plan_slots — Common fields ────────────────────────────────────


class TestCommonFields:
    def test_weighted_kw_uses_night_weight(self) -> None:
        power = 4.0
        slot = _make_slot("ev", power_kw=power)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["weighted_kw"] == pytest.approx(power * DEFAULT_NIGHT_WEIGHT)

    def test_pv_kw_is_zero(self) -> None:
        for device in ("ev", "battery_kontor", "dishwasher", "miner"):
            slot = _make_slot(device, power_kw=1.0)
            result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
            assert result[0]["pv_kw"] == 0.0, f"pv_kw should be 0 for {device}"

    def test_hour_preserved(self) -> None:
        slot = _make_slot("ev", hour=3, power_kw=2.0)
        result = BRIDGE.convert_to_plan_slots(_make_plan([slot]))
        assert result[0]["hour"] == 3

    def test_empty_night_plan_returns_empty_list(self) -> None:
        plan = _make_plan([])
        result = BRIDGE.convert_to_plan_slots(plan)
        assert result == []

    def test_multiple_slots_length(self) -> None:
        slots = [
            _make_slot("ev", hour=23),
            _make_slot("battery_kontor", hour=0),
            _make_slot("miner", hour=1),
        ]
        result = BRIDGE.convert_to_plan_slots(_make_plan(slots))
        assert len(result) == 3


# ── PlanSelector ─────────────────────────────────────────────────────────────


class TestPlanSelector:
    def _plan(self) -> NightPlan:
        return _make_plan([_make_slot("ev")])

    def test_night_hour_22_returns_night(self) -> None:
        assert PlanSelector.select_active_plan(22, self._plan(), 0.0, False) == "night"

    def test_night_hour_0_returns_night(self) -> None:
        assert PlanSelector.select_active_plan(0, self._plan(), 1.0, True) == "night"

    def test_night_hour_5_returns_night(self) -> None:
        assert PlanSelector.select_active_plan(5, self._plan(), 2.0, True) == "night"

    def test_day_with_pv_surplus_returns_surplus(self) -> None:
        assert PlanSelector.select_active_plan(12, None, 1.5, True) == "surplus"

    def test_day_without_pv_returns_fallback(self) -> None:
        assert PlanSelector.select_active_plan(12, self._plan(), 2.0, False) == "fallback"

    def test_night_without_plan_returns_fallback(self) -> None:
        assert PlanSelector.select_active_plan(23, None, 0.0, False) == "fallback"

    def test_day_zero_surplus_returns_fallback(self) -> None:
        assert PlanSelector.select_active_plan(14, None, 0.0, True) == "fallback"

    def test_day_boundary_hour_6_is_day(self) -> None:
        # Hour 6 = DEFAULT_NIGHT_END → daytime
        assert PlanSelector.select_active_plan(6, None, 1.0, True) == "surplus"

    def test_night_boundary_hour_21_is_day(self) -> None:
        # Hour 21 < DEFAULT_NIGHT_START=22 → daytime, no surplus → fallback
        assert PlanSelector.select_active_plan(21, None, 0.0, False) == "fallback"


# ── save / load round-trip ────────────────────────────────────────────────────


class TestPersistence:
    def test_save_load_round_trip(self) -> None:
        slots = [
            NightSlot(hour=23, device="ev", power_kw=4.14, duration_min=60, reason="cheap"),
            NightSlot(hour=0, device="battery_kontor", power_kw=2.0),
        ]
        original = NightPlan(
            slots=slots,
            total_cost_kr=3.7,
            ev_target_soc=85.0,
            battery_target_soc=80.0,
            scenario_name="balanced",
            created_at=datetime(2026, 4, 1, 21, 45, 0),
            ev_skipped=False,
            ev_skip_reason="",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_night_plan(original, path)
            loaded = load_night_plan(path)
            assert loaded is not None
            assert loaded.total_cost_kr == pytest.approx(3.7)
            assert loaded.ev_target_soc == pytest.approx(85.0)
            assert loaded.battery_target_soc == pytest.approx(80.0)
            assert loaded.scenario_name == "balanced"
            assert loaded.ev_skipped is False
            assert len(loaded.slots) == 2
            assert loaded.slots[0].device == "ev"
            assert loaded.slots[0].power_kw == pytest.approx(4.14)
            assert loaded.slots[1].device == "battery_kontor"
        finally:
            os.unlink(path)

    def test_load_missing_file_returns_none(self) -> None:
        result = load_night_plan("/tmp/carmabox_nonexistent_plan_12345.json")
        assert result is None

    def test_save_creates_file(self) -> None:
        plan = _make_plan([])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "night_plan.json")
            save_night_plan(plan, path)
            assert os.path.exists(path)
            with open(path) as fh:
                data = json.loads(fh.read())
            assert "slots" in data

    def test_load_invalid_json_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("not valid json {{{")
            path = f.name
        try:
            result = load_night_plan(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_ev_skip_reason_preserved(self) -> None:
        plan = NightPlan(
            slots=[],
            total_cost_kr=0.0,
            ev_target_soc=75.0,
            battery_target_soc=60.0,
            scenario_name="skip_ev",
            created_at=datetime(2026, 4, 1, 22, 0, 0),
            ev_skipped=True,
            ev_skip_reason="EV disconnected",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_night_plan(plan, path)
            loaded = load_night_plan(path)
            assert loaded is not None
            assert loaded.ev_skipped is True
            assert loaded.ev_skip_reason == "EV disconnected"
        finally:
            os.unlink(path)


# ── _calc_ev_amps unit tests ──────────────────────────────────────────────────


class TestCalcEvAmps:
    def test_6_9kw_gives_10a(self) -> None:
        # 6.9 kW = MAX_EV_CURRENT (10A) x3ph x230V
        amps = _calc_ev_amps(6.9)
        assert amps == MAX_EV_CURRENT

    def test_4_14kw_gives_6a(self) -> None:
        # 4.14 kW = 6A x3ph x230V
        amps = _calc_ev_amps(4.14)
        assert amps == 6

    def test_zero_power_clamped_to_min(self) -> None:
        amps = _calc_ev_amps(0.0)
        assert amps == DEFAULT_EV_MIN_AMPS

    def test_overcurrent_capped_at_max(self) -> None:
        amps = _calc_ev_amps(20.0)
        assert amps == MAX_EV_CURRENT
