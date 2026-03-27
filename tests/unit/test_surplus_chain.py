"""Tests for Surplus Chain — knapsack allocation."""

from __future__ import annotations

from custom_components.carmabox.core.surplus_chain import (
    ConsumerType,
    HysteresisState,
    SurplusConfig,
    SurplusConsumer,
    allocate_surplus,
    should_reduce_consumers,
)


def _ev(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "ev", "EV", priority=1, type=ConsumerType.VARIABLE,
        min_w=4140, max_w=11040, current_w=current_w,
        is_running=running, phase_count=3,
    )


def _battery(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "battery", "Batteri", priority=2, type=ConsumerType.VARIABLE,
        min_w=300, max_w=6000, current_w=current_w,
        is_running=running,
    )


def _miner(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "miner", "Miner", priority=5, type=ConsumerType.ON_OFF,
        min_w=400, max_w=500, current_w=current_w,
        is_running=running, entity_switch="switch.miner",
    )


def _vp_pool(running: bool = False, dep_met: bool = True) -> SurplusConsumer:
    return SurplusConsumer(
        "vp_pool", "VP Pool", priority=3, type=ConsumerType.ON_OFF,
        min_w=500, max_w=3000, current_w=2000 if running else 0,
        is_running=running, requires_active="cirkpump",
        dependency_met=dep_met,
    )


def _cfg(start_s: float = 0, stop_s: float = 0, bump_s: float = 0) -> SurplusConfig:
    """Config with zero delays for testing (unless specified)."""
    return SurplusConfig(
        start_delay_s=start_s, stop_delay_s=stop_s,
        bump_delay_s=bump_s, min_surplus_w=50,
    )


class TestBasicAllocation:
    def test_ev_fits(self):
        """5kW surplus → start EV at 4140W."""
        r = allocate_surplus(5000, [_ev()], config=_cfg())
        ev = next(a for a in r.allocations if a.id == "ev")
        assert ev.action == "start"
        assert ev.target_w >= 4140

    def test_ev_too_big_miner_fits(self):
        """700W surplus → EV doesn't fit, miner does."""
        r = allocate_surplus(700, [_ev(), _miner()], config=_cfg())
        ev = next(a for a in r.allocations if a.id == "ev")
        miner = next(a for a in r.allocations if a.id == "miner")
        assert ev.action == "none"
        assert miner.action == "start"
        assert r.export_w < 300

    def test_all_consumers_filled(self):
        """Large surplus fills everything."""
        consumers = [_ev(), _battery(), _miner()]
        r = allocate_surplus(20000, consumers, config=_cfg())
        started = [a for a in r.allocations if a.action == "start"]
        assert len(started) == 3

    def test_no_surplus(self):
        """No surplus → no actions."""
        r = allocate_surplus(30, [_ev(), _miner()], config=_cfg())
        assert r.actions_taken == 0

    def test_priority_when_both_fit(self):
        """Both fit → higher priority (lower number) starts first."""
        r = allocate_surplus(5000, [_miner(), _ev()], config=_cfg())
        ev = next(a for a in r.allocations if a.id == "ev")
        assert ev.action == "start"  # EV prio 1 > miner prio 5


class TestIncreaseExisting:
    def test_increase_battery_before_new(self):
        """Increase running battery before starting miner."""
        bat = _battery(current_w=2000, running=True)
        r = allocate_surplus(600, [bat, _miner()], config=_cfg())
        bat_alloc = next(a for a in r.allocations if a.id == "battery")
        miner_alloc = next(a for a in r.allocations if a.id == "miner")
        assert bat_alloc.action == "increase"
        assert bat_alloc.target_w > 2000
        assert miner_alloc.action == "none"  # Not started — battery absorbed it

    def test_increase_ev_full_amps(self):
        """EV increase in full amp steps (3-phase)."""
        ev = _ev(current_w=4140, running=True)  # 6A
        # 690W surplus = exactly 1 amp at 3-phase
        r = allocate_surplus(700, [ev], config=_cfg())
        ev_alloc = next(a for a in r.allocations if a.id == "ev")
        assert ev_alloc.action == "increase"
        assert ev_alloc.target_w == 4140 + 690  # +1 amp


class TestBump:
    def test_bump_miner_for_ev(self):
        """Surplus grows: stop miner to make room for EV."""
        miner = _miner(current_w=500, running=True)
        ev = _ev()
        # 4000W surplus + 500W from miner = 4500W ≥ EV min 4140
        r = allocate_surplus(4000, [ev, miner], config=_cfg())
        ev_started = any(a.id == "ev" and a.action == "start" for a in r.allocations)
        miner_stopped = any(a.id == "miner" and a.action == "stop" for a in r.allocations)
        assert ev_started
        assert miner_stopped


class TestDependencies:
    def test_vp_pool_without_cirkpump(self):
        """VP pool requires cirkpump — not started if dependency not met."""
        vp = _vp_pool(dep_met=False)
        r = allocate_surplus(3000, [vp], config=_cfg())
        vp_alloc = next(a for a in r.allocations if a.id == "vp_pool")
        assert vp_alloc.action == "none"

    def test_vp_pool_with_cirkpump(self):
        """VP pool starts when cirkpump is running."""
        vp = _vp_pool(dep_met=True)
        r = allocate_surplus(3000, [vp], config=_cfg())
        vp_alloc = next(a for a in r.allocations if a.id == "vp_pool")
        assert vp_alloc.action == "start"


class TestHysteresis:
    def test_start_delay(self):
        """Consumer not started until surplus stable for start_delay_s."""
        cfg = _cfg(start_s=60)
        hyst = HysteresisState()
        # First call — timer starts
        r1 = allocate_surplus(1000, [_miner()], hyst, cfg, now=100.0)
        miner1 = next(a for a in r1.allocations if a.id == "miner")
        assert miner1.action == "none"  # Waiting

        # 30s later — not enough
        r2 = allocate_surplus(1000, [_miner()], hyst, cfg, now=130.0)
        miner2 = next(a for a in r2.allocations if a.id == "miner")
        assert miner2.action == "none"

        # 60s later — OK now
        r3 = allocate_surplus(1000, [_miner()], hyst, cfg, now=160.0)
        miner3 = next(a for a in r3.allocations if a.id == "miner")
        assert miner3.action == "start"

    def test_start_resets_on_drop(self):
        """Timer resets if surplus drops below min_w."""
        cfg = _cfg(start_s=60)
        hyst = HysteresisState()
        miner = _miner()
        # Start timer at t=100
        allocate_surplus(1000, [miner], hyst, cfg, now=100.0)
        assert "miner" in hyst.surplus_above_since
        # Surplus drops at t=130 — clears timer
        allocate_surplus(30, [miner], hyst, cfg, now=130.0)
        assert "miner" not in hyst.surplus_above_since
        # Surplus back at t=160 — timer restarts
        allocate_surplus(1000, [miner], hyst, cfg, now=160.0)
        # At t=219 (59s since restart) — not enough
        r = allocate_surplus(1000, [miner], hyst, cfg, now=219.0)
        m = next(a for a in r.allocations if a.id == "miner")
        assert m.action == "none"  # 59s < 60s delay

    def test_no_oscillation(self):
        """Surplus stable for delay → start."""
        cfg = _cfg(start_s=60, stop_s=180)
        hyst = HysteresisState()
        miner = _miner()
        # t=0: Start timer
        allocate_surplus(1000, [miner], hyst, cfg, now=0.0)
        # t=30: Still waiting
        r1 = allocate_surplus(1000, [miner], hyst, cfg, now=30.0)
        assert next(a for a in r1.allocations if a.id == "miner").action == "none"
        # t=61: Full delay passed → start
        r2 = allocate_surplus(1000, [miner], hyst, cfg, now=61.0)
        assert next(a for a in r2.allocations if a.id == "miner").action == "start"


class TestReduceConsumers:
    def test_reduce_lowest_prio_first(self):
        """Reduce lowest priority consumer first."""
        consumers = [
            _ev(current_w=4140, running=True),  # prio 1
            _miner(current_w=500, running=True),  # prio 5
        ]
        reductions = should_reduce_consumers(600, consumers, config=_cfg())
        assert len(reductions) >= 1
        assert reductions[0].id == "miner"  # Lowest prio first

    def test_reduce_variable_partially(self):
        """Variable consumer reduced, not stopped."""
        bat = _battery(current_w=3000, running=True)
        reductions = should_reduce_consumers(1000, [bat], config=_cfg())
        assert len(reductions) == 1
        assert reductions[0].action == "decrease"
        assert reductions[0].target_w == 2000  # 3000 - 1000

    def test_stop_on_off(self):
        """On/off consumer stopped completely."""
        miner = _miner(current_w=500, running=True)
        reductions = should_reduce_consumers(600, [miner], config=_cfg())
        assert reductions[0].action == "stop"
        assert reductions[0].target_w == 0
