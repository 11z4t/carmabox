"""Tests for core Planner — generates PlanAction lists."""

from __future__ import annotations

from custom_components.carmabox.core.planner import (
    PlannerConfig,
    PlannerInput,
    generate_carma_plan,
)


def _input(
    start_hour: int = 22,
    battery_soc: float = 96,
    ev_soc: float = 80,
    pv_tomorrow: float = 30.0,
    n_hours: int = 10,
    bat_temps: list[float] | None = None,
) -> PlannerInput:
    prices = [50, 45, 40, 35, 30, 32, 35, 60, 65, 70][:n_hours]
    pv = [0, 0, 0, 0, 0, 0, 0.5, 2, 4, 6][:n_hours]
    loads = [1.7] * n_hours
    ev = [0] * n_hours
    return PlannerInput(
        start_hour=start_hour,
        hourly_prices=prices, hourly_pv=pv,
        hourly_loads=loads, hourly_ev=ev,
        battery_soc=battery_soc, battery_cap_kwh=20,
        ev_soc=ev_soc, ev_cap_kwh=92,
        pv_forecast_tomorrow_kwh=pv_tomorrow,
        battery_temps=bat_temps,
    )


class TestPlanGeneration:
    def test_generates_plan(self):
        plan = generate_carma_plan(_input())
        assert len(plan) == 10
        assert all(hasattr(p, "action") for p in plan)

    def test_plan_has_discharge(self):
        """With batteries at 96% and prices > 30 öre, should discharge."""
        plan = generate_carma_plan(_input(battery_soc=96))
        discharge_hours = [p for p in plan if p.action == "d"]
        assert len(discharge_hours) > 0

    def test_plan_reduces_soc(self):
        """Plan should discharge significantly from high SoC."""
        plan = generate_carma_plan(_input(battery_soc=96, n_hours=10))
        first_soc = plan[0].battery_soc
        last_soc = plan[-1].battery_soc
        assert last_soc < first_soc  # SoC reduced

    def test_plan_returns_plan_actions(self):
        """Output is PlanAction objects usable by Plan Executor."""
        plan = generate_carma_plan(_input())
        for p in plan:
            assert p.action in ("c", "d", "g", "i")
            assert isinstance(p.hour, int)
            assert isinstance(p.battery_kw, float)
            assert isinstance(p.price, int | float)


class TestSolarAwareness:
    def test_strong_solar_aggressive_discharge(self):
        """Strong solar tomorrow → discharge at 2 kW."""
        cfg = PlannerConfig(
            discharge_rate_solar_kw=2.0,
            solar_strong_threshold_kwh=25,
        )
        plan = generate_carma_plan(_input(pv_tomorrow=35), cfg)
        discharge = [p for p in plan if p.action == "d"]
        if discharge:
            max_discharge = max(abs(p.battery_kw) for p in discharge)
            assert max_discharge <= 2.1  # ~2 kW max

    def test_weak_solar_conservative(self):
        """Weak solar → discharge at 0.5 kW (save battery)."""
        cfg = PlannerConfig(
            discharge_rate_winter_kw=0.5,
            solar_partial_threshold_kwh=15,
        )
        plan = generate_carma_plan(_input(pv_tomorrow=5), cfg)
        discharge = [p for p in plan if p.action == "d"]
        if discharge:
            max_discharge = max(abs(p.battery_kw) for p in discharge)
            assert max_discharge <= 1.8  # Conservative


class TestColdBattery:
    def test_cold_battery_higher_min_soc(self):
        """Cold battery → min_soc = 20% instead of 15%."""
        cfg = PlannerConfig(
            battery_min_soc=15, battery_min_soc_cold=20, cold_temp_c=4.0,
        )
        plan = generate_carma_plan(
            _input(battery_soc=25, bat_temps=[3.0, 15.0]),
            cfg,
        )
        # With min_soc=20 and starting at 25%, very little discharge
        discharge = [p for p in plan if p.action == "d"]
        if discharge:
            last_soc = plan[-1].battery_soc
            assert last_soc >= 18  # Should not go below ~20%

    def test_warm_battery_normal_min_soc(self):
        """Warm battery → min_soc = 15%."""
        cfg = PlannerConfig(battery_min_soc=15, cold_temp_c=4.0)
        plan = generate_carma_plan(
            _input(battery_soc=50, bat_temps=[15.0, 15.0]),
            cfg,
        )
        discharge = [p for p in plan if p.action == "d"]
        assert len(discharge) > 0  # Should discharge freely


class TestTarget:
    def test_target_never_below_tak_margin(self):
        """Target = max(calculated, tak × margin)."""
        cfg = PlannerConfig(ellevio_tak_kw=2.0, grid_guard_margin=0.85)
        plan = generate_carma_plan(_input(), cfg)
        # Plan grid values should respect target 1.7 kW
        for p in plan:
            if p.action == "i":
                assert p.grid_kw <= 2.5  # Grid should be near target


class TestEdgeCases:
    def test_empty_prices(self):
        inp = _input()
        inp.hourly_prices = []
        plan = generate_carma_plan(inp)
        assert len(plan) == 0

    def test_zero_battery(self):
        plan = generate_carma_plan(_input(battery_soc=0))
        assert len(plan) > 0
        discharge = [p for p in plan if p.action == "d"]
        assert len(discharge) == 0  # Can't discharge at 0%

    def test_full_ev(self):
        plan = generate_carma_plan(_input(ev_soc=100))
        assert len(plan) > 0
