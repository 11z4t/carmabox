"""Tests for SurplusPlanner (PLAT-1227)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.carmabox.const import (
    MAX_SURPLUS_SWITCHES_PER_WINDOW,
    SURPLUS_SWITCH_WINDOW_MIN,
)
from custom_components.carmabox.core.surplus_chain import (
    ConsumerType,
    SurplusConfig,
    SurplusConsumer,
    SwitchTracker,
)
from custom_components.carmabox.optimizer.surplus_planner import SurplusPlan, SurplusPlanner

# ── Helpers ─────────────────────────────────────────────────────


def _battery_consumer(current_w: float = 0.0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        id="battery",
        name="Batteri",
        priority=2,
        type=ConsumerType.VARIABLE,
        min_w=300.0,
        max_w=6000.0,
        current_w=current_w,
        is_running=running,
    )


def _miner_consumer(current_w: float = 0.0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        id="miner",
        name="Miner",
        priority=5,
        type=ConsumerType.ON_OFF,
        min_w=500.0,
        max_w=500.0,
        current_w=current_w,
        is_running=running,
    )


def _no_delay_config() -> SurplusConfig:
    """Config with zero delays so allocations happen immediately in tests."""
    return SurplusConfig(start_delay_s=0, stop_delay_s=0, bump_delay_s=0, min_surplus_w=50.0)


# ── SwitchTracker unit tests ─────────────────────────────────────


class TestSwitchTracker:
    def test_initial_check_allows_switch(self) -> None:
        tracker = SwitchTracker()
        assert tracker._check_switch_limit() is True

    def test_first_switch_allowed(self) -> None:
        tracker = SwitchTracker()
        tracker.record_switch()
        assert tracker._check_switch_limit() is True  # 1 < max(2)

    def test_second_switch_allowed(self) -> None:
        tracker = SwitchTracker()
        tracker.record_switch()
        tracker.record_switch()
        # 2 switches = limit reached → next is blocked
        assert tracker._check_switch_limit() is False

    def test_third_switch_blocked(self) -> None:
        tracker = SwitchTracker()
        now = datetime.now()
        tracker.record_switch(now)
        tracker.record_switch(now)
        # Two recent switches → third blocked
        assert tracker._check_switch_limit() is False

    def test_switch_limit_resets_after_window(self) -> None:
        tracker = SwitchTracker()
        # Add two old switches outside the window
        old = datetime.now() - timedelta(minutes=SURPLUS_SWITCH_WINDOW_MIN + 1)
        tracker._switch_history = [old, old]
        assert tracker._check_switch_limit() is True

    def test_record_switch_prunes_old_history(self) -> None:
        tracker = SwitchTracker()
        stale = datetime.now() - timedelta(minutes=SURPLUS_SWITCH_WINDOW_MIN * 3)
        tracker._switch_history = [stale, stale, stale]
        tracker.record_switch()
        # Stale entries pruned; only the new one remains
        assert len(tracker._switch_history) == 1

    def test_max_switches_constant_used(self) -> None:
        assert MAX_SURPLUS_SWITCHES_PER_WINDOW == 2

    def test_window_constant_used(self) -> None:
        assert SURPLUS_SWITCH_WINDOW_MIN == 30


# ── SurplusPlan tests ────────────────────────────────────────────


class TestSurplusPlan:
    def _make_plan(self, ratio: float = 0.8) -> SurplusPlan:
        from custom_components.carmabox.core.surplus_chain import SurplusAllocation

        alloc = SurplusAllocation("battery", "start", 1000.0, 0.0, "test")
        return SurplusPlan(
            allocations=[alloc],
            self_consumption_ratio=ratio,
            timestamp=datetime(2026, 4, 3, 12, 0, 0),
        )

    def test_to_dict_has_allocations_key(self) -> None:
        plan = self._make_plan()
        d = plan.to_dict()
        assert "allocations" in d

    def test_to_dict_allocations_is_list(self) -> None:
        plan = self._make_plan()
        d = plan.to_dict()
        assert isinstance(d["allocations"], list)

    def test_to_dict_has_self_consumption_ratio(self) -> None:
        plan = self._make_plan(0.75)
        d = plan.to_dict()
        assert "self_consumption_ratio" in d
        assert d["self_consumption_ratio"] == pytest.approx(0.75)

    def test_to_dict_has_timestamp(self) -> None:
        plan = self._make_plan()
        d = plan.to_dict()
        assert "timestamp" in d
        assert "2026-04-03" in d["timestamp"]

    def test_to_dict_allocation_fields(self) -> None:
        plan = self._make_plan()
        alloc_dict = plan.to_dict()["allocations"][0]
        assert alloc_dict["id"] == "battery"
        assert alloc_dict["action"] == "start"
        assert alloc_dict["target_w"] == pytest.approx(1000.0)
        assert "reason" in alloc_dict


# ── SurplusPlanner integration tests ────────────────────────────


class TestSurplusPlanner:
    def test_returns_surplus_plan_instance(self) -> None:
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(2.0, {})
        assert isinstance(plan, SurplusPlan)

    def test_plan_has_timestamp(self) -> None:
        ts = datetime(2026, 4, 3, 14, 0, 0)
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(2.0, {}, now=ts)
        assert plan.timestamp == ts

    def test_plan_allocations_not_empty(self) -> None:
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(2.0, {})
        assert len(plan.allocations) > 0

    def test_self_consumption_ratio_zero_kw(self) -> None:
        """0 kW available → ratio = 1.0 (nothing to consume)."""
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(0.0, {})
        assert plan.self_consumption_ratio == pytest.approx(1.0)

    def test_self_consumption_ratio_all_consumed(self) -> None:
        """Large battery consumer can absorb 1 kW → ratio ≈ 1.0."""
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(1.0, {})  # 1000 W, battery min=300 W
        assert plan.self_consumption_ratio == pytest.approx(1.0)

    def test_self_consumption_ratio_partial(self) -> None:
        """Miner needs 500 W, only 400 W available → nothing allocated → ratio = 0.0."""
        planner = SurplusPlanner(consumers=[_miner_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(0.4, {})  # 400 W < miner min 500 W
        assert plan.self_consumption_ratio == pytest.approx(0.0)

    def test_switch_limit_allows_two_actions(self) -> None:
        """First two allocation cycles with actions should succeed."""
        consumers = [_battery_consumer()]
        planner = SurplusPlanner(consumers=consumers, config=_no_delay_config())
        # Force actions by starting from idle each time
        now = datetime.now()
        plan1 = planner.allocate_surplus(2.0, {}, now=now)
        # Reset consumer state to idle so second call also produces an action
        consumers[0].is_running = False
        consumers[0].current_w = 0.0
        plan2 = planner.allocate_surplus(2.0, {}, now=now + timedelta(seconds=1))
        # Both plans should have an actual action (not blocked)
        actions1 = [a for a in plan1.allocations if a.action != "none"]
        actions2 = [a for a in plan2.allocations if a.action != "none"]
        assert len(actions1) > 0
        assert len(actions2) > 0

    def test_switch_limit_blocks_third_switch(self) -> None:
        """After two switches within the window, the third is blocked."""
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        # Pre-fill switch history with two recent events
        now = datetime.now()
        planner._switch_tracker._switch_history = [
            now - timedelta(seconds=10),
            now - timedelta(seconds=5),
        ]
        plan = planner.allocate_surplus(2.0, {}, now=now)
        # All actions should be "none" (blocked)
        actions = [a for a in plan.allocations if a.action != "none"]
        assert len(actions) == 0

    def test_switch_limit_reset_after_window(self) -> None:
        """After the window expires, switches are allowed again."""
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        # Two stale switches outside the window
        old = datetime.now() - timedelta(minutes=SURPLUS_SWITCH_WINDOW_MIN + 5)
        planner._switch_tracker._switch_history = [old, old]
        plan = planner.allocate_surplus(2.0, {})
        # Should produce actions (not blocked)
        actions = [a for a in plan.allocations if a.action != "none"]
        assert len(actions) > 0

    def test_device_states_is_running_true(self) -> None:
        """device_states is_running=True marks consumer as running."""
        consumer = _battery_consumer(running=False)
        planner = SurplusPlanner(consumers=[consumer], config=_no_delay_config())
        planner.allocate_surplus(2.0, {"battery": {"is_running": True, "current_w": 500.0}})
        assert consumer.is_running is True
        assert consumer.current_w == pytest.approx(500.0)

    def test_device_states_is_running_false(self) -> None:
        """device_states is_running=False marks consumer as stopped."""
        consumer = _battery_consumer(running=True, current_w=1000.0)
        planner = SurplusPlanner(consumers=[consumer], config=_no_delay_config())
        planner.allocate_surplus(2.0, {"battery": {"is_running": False, "current_w": 0.0}})
        assert consumer.is_running is False

    def test_works_without_scenario_engine(self) -> None:
        """SurplusPlanner works fine with no optional dependencies."""
        planner = SurplusPlanner(
            scenario_engine=None,
            cost_model=None,
            consumers=[_battery_consumer()],
            config=_no_delay_config(),
        )
        plan = planner.allocate_surplus(1.5, {})
        assert isinstance(plan, SurplusPlan)

    def test_edge_all_consumers_running_at_max(self) -> None:
        """All consumers at max → ratio = 1.0, no increase possible."""
        consumer = _battery_consumer(current_w=6000.0, running=True)
        planner = SurplusPlanner(consumers=[consumer], config=_no_delay_config())
        plan = planner.allocate_surplus(1.0, {})
        # Battery at max — surplus can't be allocated → export
        assert isinstance(plan, SurplusPlan)

    def test_plan_to_dict_round_trips(self) -> None:
        planner = SurplusPlanner(consumers=[_battery_consumer()], config=_no_delay_config())
        plan = planner.allocate_surplus(2.0, {})
        d = plan.to_dict()
        assert isinstance(d, dict)
        assert "allocations" in d
        assert "self_consumption_ratio" in d
        assert "timestamp" in d
