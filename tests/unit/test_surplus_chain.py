"""Tests for Surplus Chain — knapsack allocation."""

from __future__ import annotations

from custom_components.carmabox.core.surplus_chain import (
    ConsumerType,
    HysteresisState,
    SurplusConfig,
    SurplusConsumer,
    allocate_surplus,
    build_default_consumers,
    calculate_climate_boost,
    should_reduce_consumers,
)


def _ev(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "ev",
        "EV",
        priority=1,
        type=ConsumerType.VARIABLE,
        min_w=4140,
        max_w=11040,
        current_w=current_w,
        is_running=running,
        phase_count=3,
    )


def _battery(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "battery",
        "Batteri",
        priority=2,
        type=ConsumerType.VARIABLE,
        min_w=300,
        max_w=6000,
        current_w=current_w,
        is_running=running,
    )


def _miner(current_w: float = 0, running: bool = False) -> SurplusConsumer:
    return SurplusConsumer(
        "miner",
        "Miner",
        priority=5,
        type=ConsumerType.ON_OFF,
        min_w=400,
        max_w=500,
        current_w=current_w,
        is_running=running,
        entity_switch="switch.miner",
    )


def _vp_pool(running: bool = False, dep_met: bool = True) -> SurplusConsumer:
    return SurplusConsumer(
        "vp_pool",
        "VP Pool",
        priority=3,
        type=ConsumerType.ON_OFF,
        min_w=500,
        max_w=3000,
        current_w=2000 if running else 0,
        is_running=running,
        requires_active="cirkpump",
        dependency_met=dep_met,
    )


def _cfg(start_s: float = 0, stop_s: float = 0, bump_s: float = 0) -> SurplusConfig:
    """Config with zero delays for testing (unless specified)."""
    return SurplusConfig(
        start_delay_s=start_s,
        stop_delay_s=stop_s,
        bump_delay_s=bump_s,
        min_surplus_w=50,
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


class TestExportAllowed:
    def test_all_running_export_ok(self):
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        consumers = [
            _ev(current_w=11000, running=True),  # At max
            _battery(current_w=6000, running=True),  # At max
            _miner(current_w=500, running=True),
        ]
        assert is_export_allowed(consumers) is True

    def test_miner_off_no_export(self):
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        consumers = [
            _ev(current_w=11000, running=True),
            _miner(current_w=0, running=False),
        ]
        assert is_export_allowed(consumers) is False

    def test_ev_not_at_max_no_export(self):
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        consumers = [
            _ev(current_w=4140, running=True),  # Not at max
        ]
        assert is_export_allowed(consumers) is False

    def test_dependency_not_met_skipped(self):
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        consumers = [_vp_pool(dep_met=False)]
        assert is_export_allowed(consumers) is True


class TestClimateBoost:
    def test_climate_boost_cool(self):
        """2kW surplus → lower cooling setpoint by up to 2 degrees."""
        result = calculate_climate_boost(
            current_temp_c=24.0,
            target_temp_c=23.0,
            surplus_w=2000.0,
            mode="cool",
        )
        assert result["boost"] is True
        assert result["new_target_c"] == 21.0  # 23 - min(2.0, 2000/1000) = 21
        assert "PV-boost cool" in result["reason"]

    def test_climate_boost_heat(self):
        """1kW surplus → raise heating setpoint by 1 degree."""
        result = calculate_climate_boost(
            current_temp_c=20.0,
            target_temp_c=21.0,
            surplus_w=1000.0,
            mode="heat",
        )
        assert result["boost"] is True
        assert result["new_target_c"] == 22.0  # 21 + min(2.0, 1000/1000) = 22
        assert "PV-boost heat" in result["reason"]

    def test_climate_boost_no_surplus(self):
        """200W surplus → no boost (below min_surplus_w)."""
        result = calculate_climate_boost(
            current_temp_c=24.0,
            target_temp_c=23.0,
            surplus_w=200.0,
            mode="cool",
        )
        assert result["boost"] is False
        assert result["new_target_c"] == 23.0
        assert "min" in result["reason"]

    def test_climate_boost_proportional(self):
        """More surplus = bigger boost, capped at boost_degrees."""
        # 800W → 0.8 degree offset
        r1 = calculate_climate_boost(
            current_temp_c=24.0,
            target_temp_c=23.0,
            surplus_w=800.0,
            mode="cool",
        )
        # 3000W → capped at 2.0 degrees
        r2 = calculate_climate_boost(
            current_temp_c=24.0,
            target_temp_c=23.0,
            surplus_w=3000.0,
            mode="cool",
        )
        assert r1["boost"] is True
        assert r1["new_target_c"] == 22.2  # 23 - 0.8
        assert r2["boost"] is True
        assert r2["new_target_c"] == 21.0  # 23 - 2.0 (capped)

    def test_climate_boost_cool_already_cold(self):
        """Room already below boost limit → no boost."""
        result = calculate_climate_boost(
            current_temp_c=20.5,
            target_temp_c=23.0,
            surplus_w=2000.0,
            mode="cool",
        )
        assert result["boost"] is False
        assert "redan under" in result["reason"]

    def test_climate_boost_heat_already_warm(self):
        """Room already above boost limit → no boost."""
        result = calculate_climate_boost(
            current_temp_c=23.5,
            target_temp_c=21.0,
            surplus_w=2000.0,
            mode="heat",
        )
        assert result["boost"] is False
        assert "redan över" in result["reason"]


class TestBuildDefaultConsumers:
    def test_build_default_consumers_count(self):
        """Returns exactly 6 consumers."""
        consumers = build_default_consumers()
        assert len(consumers) == 6

    def test_build_default_consumers_priority_order(self):
        """Consumers are sorted by priority (ascending)."""
        consumers = build_default_consumers()
        priorities = [c.priority for c in consumers]
        assert priorities == sorted(priorities)
        # Verify specific order
        ids = [c.id for c in consumers]
        assert ids == ["ev", "battery", "vp_kontor", "vp_pool", "pool_heater", "miner"]

    def test_build_default_ev_params(self):
        """EV has correct phase_count, min_w, max_w from defaults."""
        consumers = build_default_consumers()
        ev = next(c for c in consumers if c.id == "ev")
        assert ev.phase_count == 3
        assert ev.type == ConsumerType.VARIABLE
        # 230V * 3 phases * 6A = 4140W min
        assert ev.min_w == 230.0 * 3 * 6
        # 230V * 3 phases * 10A = 6900W max
        assert ev.max_w == 230.0 * 3 * 10

    def test_build_default_pool_dependency(self):
        """VP pool and pool heater require cirkpump."""
        consumers = build_default_consumers()
        vp_pool = next(c for c in consumers if c.id == "vp_pool")
        pool_heater = next(c for c in consumers if c.id == "pool_heater")
        assert vp_pool.requires_active == "cirkpump"
        assert pool_heater.requires_active == "cirkpump"
        # Others should NOT require cirkpump
        for c in consumers:
            if c.id not in ("vp_pool", "pool_heater"):
                assert c.requires_active == "", f"{c.id} should not require dependency"

    def test_build_default_custom_params(self):
        """Custom miner_w overrides default."""
        consumers = build_default_consumers(miner_w=750.0)
        miner = next(c for c in consumers if c.id == "miner")
        assert miner.min_w == 750.0
        assert miner.max_w == 750.0
