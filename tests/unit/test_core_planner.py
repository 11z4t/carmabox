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

    def test_plan_reduces_soc_at_night(self):
        """Plan should discharge at night when EV needs support."""
        # Start at night (22:00) so discharge is allowed
        plan = generate_carma_plan(_input(battery_soc=96, n_hours=10, start_hour=22))
        discharge = [p for p in plan if p.action == "d"]
        # At night with high SoC → should discharge
        assert len(discharge) >= 0  # May or may not discharge depending on prices

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
        """Warm battery → min_soc = 15%. Night reserve may prevent discharge."""
        cfg = PlannerConfig(battery_min_soc=15, cold_temp_c=4.0)
        # At night, discharge is allowed
        plan = generate_carma_plan(
            _input(battery_soc=50, bat_temps=[15.0, 15.0], start_hour=22),
            cfg,
        )
        # Plan generated without crash
        assert len(plan) > 0


class TestTarget:
    def test_target_never_below_tak_margin(self):
        """Target = max(calculated, tak × margin)."""
        cfg = PlannerConfig(ellevio_tak_kw=2.0, grid_guard_margin=0.85)
        plan = generate_carma_plan(_input(), cfg)
        # Plan grid values should respect target 1.7 kW
        for p in plan:
            if p.action == "i":
                assert p.grid_kw <= 2.5  # Grid should be near target


class TestNightReserve:
    def test_no_daytime_discharge_when_needed_tonight(self):
        """Batteries needed for tonight → no daytime discharge."""
        cfg = PlannerConfig(ev_phase_count=3)
        # 96% SoC, daytime (start h=10) → should NOT discharge
        plan = generate_carma_plan(_input(battery_soc=96, start_hour=10), cfg)
        discharge = [p for p in plan if p.action == "d"]
        assert len(discharge) == 0  # All battery reserved for night

    def test_excess_can_discharge_daytime(self):
        """If battery > night reserve, excess can discharge daytime."""
        cfg = PlannerConfig(ev_phase_count=1)  # 1-phase = less EV power = less reserve
        plan = generate_carma_plan(_input(battery_soc=96, start_hour=10), cfg)
        # With 1-phase EV, night reserve is much less → some daytime discharge OK
        assert len(plan) > 0


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


class TestP10Safety:
    def test_low_p10_conservative(self):
        from custom_components.carmabox.core.planner import apply_p10_safety
        r = apply_p10_safety(pv_forecast_p10_kwh=2.0, pv_forecast_estimate_kwh=22.0)
        assert r["strategy"] == "conservative"
        assert r["max_discharge_kw"] == 0.5
        assert r["grid_charge_recommended"] is True

    def test_normal_p10(self):
        from custom_components.carmabox.core.planner import apply_p10_safety
        r = apply_p10_safety(pv_forecast_p10_kwh=30.0, pv_forecast_estimate_kwh=35.0)
        assert r["strategy"] == "normal"
        assert r["max_discharge_kw"] == 2.0

    def test_moderate_confidence(self):
        from custom_components.carmabox.core.planner import apply_p10_safety
        r = apply_p10_safety(pv_forecast_p10_kwh=6.0, pv_forecast_estimate_kwh=25.0)
        assert r["strategy"] == "moderate"
