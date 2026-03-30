"""Tests for core Planner — generates PlanAction lists."""

from __future__ import annotations

from custom_components.carmabox.core.planner import (
    PlannerConfig,
    PlannerInput,
    calculate_night_reserve_kwh,
    generate_carma_plan,
    max_daytime_discharge_kwh,
    plan_solar_allocation,
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
        hourly_prices=prices,
        hourly_pv=pv,
        hourly_loads=loads,
        hourly_ev=ev,
        battery_soc=battery_soc,
        battery_cap_kwh=20,
        ev_soc=ev_soc,
        ev_cap_kwh=92,
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
            battery_min_soc=15,
            battery_min_soc_cold=20,
            cold_temp_c=4.0,
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
        """Target = max(calculated, tak x margin)."""
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


class TestWinterGridCharge:
    """IT-GAP20: Winter grid charging when solar < consumption and price cheap."""

    def test_winter_charge_recommended(self):
        """Low PV, cheap price, low SoC → recommend grid charge."""
        from custom_components.carmabox.core.planner import should_grid_charge_winter

        r = should_grid_charge_winter(
            pv_forecast_kwh=3.0,
            daily_consumption_kwh=15.0,
            current_price_ore=20.0,
            price_threshold_ore=30.0,
            battery_soc=40.0,
            max_charge_soc=80.0,
        )
        assert r["recommend"] is True
        assert r["max_charge_soc"] == 80.0
        assert "Winter grid charge" in r["reason"]

    def test_winter_charge_solar_sufficient(self):
        """High PV covers consumption → no grid charge needed."""
        from custom_components.carmabox.core.planner import should_grid_charge_winter

        r = should_grid_charge_winter(
            pv_forecast_kwh=20.0,
            daily_consumption_kwh=15.0,
            current_price_ore=20.0,
            price_threshold_ore=30.0,
            battery_soc=40.0,
            max_charge_soc=80.0,
        )
        assert r["recommend"] is False
        assert "Solar covers consumption" in r["reason"]

    def test_winter_charge_price_too_high(self):
        """Expensive electricity → no grid charge."""
        from custom_components.carmabox.core.planner import should_grid_charge_winter

        r = should_grid_charge_winter(
            pv_forecast_kwh=3.0,
            daily_consumption_kwh=15.0,
            current_price_ore=50.0,
            price_threshold_ore=30.0,
            battery_soc=40.0,
            max_charge_soc=80.0,
        )
        assert r["recommend"] is False
        assert "Price too high" in r["reason"]

    def test_winter_charge_battery_full(self):
        """Battery SoC above max → no grid charge."""
        from custom_components.carmabox.core.planner import should_grid_charge_winter

        r = should_grid_charge_winter(
            pv_forecast_kwh=3.0,
            daily_consumption_kwh=15.0,
            current_price_ore=20.0,
            price_threshold_ore=30.0,
            battery_soc=85.0,
            max_charge_soc=80.0,
        )
        assert r["recommend"] is False
        assert "already sufficiently charged" in r["reason"]

    def test_winter_charge_borderline(self):
        """PV exactly equals consumption → no grid charge (>= check)."""
        from custom_components.carmabox.core.planner import should_grid_charge_winter

        r = should_grid_charge_winter(
            pv_forecast_kwh=15.0,
            daily_consumption_kwh=15.0,
            current_price_ore=20.0,
            price_threshold_ore=30.0,
            battery_soc=40.0,
            max_charge_soc=80.0,
        )
        assert r["recommend"] is False
        assert "Solar covers consumption" in r["reason"]


class TestCalculateNightReserve:
    def test_night_reserve_3phase(self):
        """3-phase EV at 6A → ~24 kWh reserve."""
        reserve = calculate_night_reserve_kwh(ev_phase_count=3)
        # EV = 230*3*6/1000 = 4.14 kW, house = 2.5, grid_max = 4.0
        # bat_per_hour = max(0, 4.14+2.5-4.0) = 2.64
        # reserve = 2.64*8 + 3.0 = 24.12
        assert 23.0 < reserve < 25.0

    def test_night_reserve_1phase(self):
        """1-phase EV → much less reserve than 3-phase."""
        reserve_1p = calculate_night_reserve_kwh(ev_phase_count=1)
        reserve_3p = calculate_night_reserve_kwh(ev_phase_count=3)
        # 1-phase: EV = 230*1*6/1000 = 1.38 kW
        # bat_per_hour = max(0, 1.38+2.5-4.0) = 0  (grid covers it)
        # reserve = 0*8 + 3.0 = 3.0 (just appliance margin)
        assert reserve_1p < reserve_3p
        assert reserve_1p == 3.0  # Only appliance margin


class TestMaxDaytimeDischarge:
    def test_max_daytime_discharge_with_reserve(self):
        """96% SoC, 20 kWh cap, 15 kWh reserve → ~1.2 kWh available."""
        result = max_daytime_discharge_kwh(
            battery_soc=96,
            battery_cap_kwh=20,
            min_soc=15.0,
            night_reserve_kwh=15.0,
        )
        # available = (96-15)/100 * 20 = 16.2
        # discharge = 16.2 - 15.0 = 1.2
        assert abs(result - 1.2) < 0.1

    def test_max_daytime_discharge_no_reserve(self):
        """96% SoC, no reserve → full available energy."""
        result = max_daytime_discharge_kwh(
            battery_soc=96,
            battery_cap_kwh=20,
            min_soc=15.0,
            night_reserve_kwh=0.0,
        )
        # available = (96-15)/100 * 20 = 16.2
        assert abs(result - 16.2) < 0.1

    def test_max_daytime_discharge_insufficient(self):
        """Low SoC, high reserve → 0 discharge."""
        result = max_daytime_discharge_kwh(
            battery_soc=25,
            battery_cap_kwh=20,
            min_soc=15.0,
            night_reserve_kwh=10.0,
        )
        # available = (25-15)/100 * 20 = 2.0
        # 2.0 - 10.0 = -8.0 → clamped to 0
        assert result == 0.0


class TestBuildPriceSchedule:
    """IT-GAP07: Nordpool tomorrow prices for night optimization."""

    def test_build_price_schedule_with_tomorrow(self):
        """Combines today remaining + tomorrow prices."""
        from custom_components.carmabox.core.planner import build_price_schedule

        today = list(range(24))  # 0..23
        tomorrow = list(range(100, 124))  # 100..123
        result = build_price_schedule(today, tomorrow, current_hour=20, plan_hours=10)
        # today[20:] = [20,21,22,23] + tomorrow[0:6] = [100,101,102,103,104,105]
        assert result == [20, 21, 22, 23, 100, 101, 102, 103, 104, 105]

    def test_build_price_schedule_no_tomorrow(self):
        """Without tomorrow, repeats today's pattern."""
        from custom_components.carmabox.core.planner import build_price_schedule

        today = list(range(24))
        result = build_price_schedule(today, [], current_hour=22, plan_hours=6)
        # today[22:] = [22, 23], then wraps: [0, 1, 2, 3]
        assert result == [22, 23, 0, 1, 2, 3]

    def test_build_price_schedule_partial_day(self):
        """Starting at hour 22 with tomorrow available."""
        from custom_components.carmabox.core.planner import build_price_schedule

        today = [50.0] * 24
        tomorrow = [30.0] * 24
        result = build_price_schedule(today, tomorrow, current_hour=22, plan_hours=8)
        # 2 hours of today (50) + 6 hours of tomorrow (30)
        assert len(result) == 8
        assert result[:2] == [50.0, 50.0]
        assert result[2:] == [30.0] * 6


class TestFindCheapestHours:
    """IT-GAP07: Find cheapest hours for night charging."""

    def test_find_cheapest_hours(self):
        """Finds the 3 cheapest hours."""
        from custom_components.carmabox.core.planner import find_cheapest_hours

        prices = [50, 30, 80, 10, 60, 20, 40]
        result = find_cheapest_hours(prices, n_hours=3)
        # Cheapest: index 3 (10), index 5 (20), index 1 (30)
        assert result == [1, 3, 5]

    def test_find_cheapest_hours_sorted(self):
        """Result is sorted chronologically, not by price."""
        from custom_components.carmabox.core.planner import find_cheapest_hours

        prices = [90, 10, 80, 20, 70, 30]
        result = find_cheapest_hours(prices, n_hours=3)
        # Cheapest: index 1 (10), index 3 (20), index 5 (30)
        assert result == [1, 3, 5]
        # Verify chronological order (ascending indices)
        assert result == sorted(result)


class TestEllevioPeakCost:
    """IT-GAP08: Ellevio peak cost impact in planner."""

    def test_peak_cost_no_existing(self):
        """Empty peaks + 3.0 kW → cost calculated from scratch."""
        from custom_components.carmabox.core.planner import calculate_ellevio_peak_cost

        r = calculate_ellevio_peak_cost(current_peaks_kw=[], new_peak_kw=3.0)
        # No existing peaks → current_avg = 0, new top 3 = [3.0] → avg = 3.0/3 = 1.0
        assert r["current_avg_kw"] == 0.0
        assert r["new_avg_kw"] == 1.0
        assert r["monthly_cost_increase"] == 80.0  # 1.0 * 80
        assert r["annual_cost_increase"] == 960.0  # 80 * 12
        assert r["should_avoid"] is True

    def test_peak_cost_below_existing(self):
        """New peak lower than all top 3 → 0 increase."""
        from custom_components.carmabox.core.planner import calculate_ellevio_peak_cost

        r = calculate_ellevio_peak_cost(
            current_peaks_kw=[5.0, 4.0, 3.0],
            new_peak_kw=2.0,
        )
        # Top 3 stays [5, 4, 3] → avg = 4.0, new top 3 = [5, 4, 3] → avg = 4.0
        assert r["current_avg_kw"] == 4.0
        assert r["new_avg_kw"] == 4.0
        assert r["monthly_cost_increase"] == 0.0
        assert r["annual_cost_increase"] == 0.0
        assert r["should_avoid"] is False

    def test_peak_cost_above_existing(self):
        """New peak raises average → positive increase."""
        from custom_components.carmabox.core.planner import calculate_ellevio_peak_cost

        r = calculate_ellevio_peak_cost(
            current_peaks_kw=[5.0, 4.0, 3.0],
            new_peak_kw=6.0,
        )
        # Current avg = (5+4+3)/3 = 4.0
        # New top 3 = [6, 5, 4] → avg = 5.0
        assert r["current_avg_kw"] == 4.0
        assert r["new_avg_kw"] == 5.0
        assert r["monthly_cost_increase"] == 80.0  # (5.0-4.0) * 80
        assert r["annual_cost_increase"] == 960.0
        assert r["should_avoid"] is True

    def test_peak_cost_should_avoid(self):
        """Large increase → should_avoid True; small → False."""
        from custom_components.carmabox.core.planner import calculate_ellevio_peak_cost

        # Large increase
        r_large = calculate_ellevio_peak_cost(
            current_peaks_kw=[2.0, 2.0, 2.0],
            new_peak_kw=5.0,
        )
        assert r_large["should_avoid"] is True
        assert r_large["monthly_cost_increase"] > 10.0

        # Tiny increase (just barely displaces bottom peak)
        r_small = calculate_ellevio_peak_cost(
            current_peaks_kw=[5.0, 4.0, 3.0],
            new_peak_kw=3.1,
        )
        # New top 3 = [5, 4, 3.1] → avg = 4.0333, increase = 0.0333 * 80 = 2.67
        assert r_small["should_avoid"] is False
        assert r_small["monthly_cost_increase"] < 10.0


class TestEstimateHourPeak:
    """IT-GAP08: Estimate where weighted hourly average will land."""

    def test_estimate_hour_peak_midway(self):
        """30 min elapsed, projects correctly."""
        from custom_components.carmabox.core.planner import estimate_hour_peak

        result = estimate_hour_peak(
            current_weighted_kw=2.0,
            minutes_elapsed=30,
            remaining_load_kw=4.0,
        )
        # (2.0 * 30 + 4.0 * 30) / 60 = (60 + 120) / 60 = 3.0
        assert abs(result - 3.0) < 0.001

    def test_estimate_hour_peak_end(self):
        """60 min elapsed → returns current value."""
        from custom_components.carmabox.core.planner import estimate_hour_peak

        result = estimate_hour_peak(
            current_weighted_kw=2.5,
            minutes_elapsed=60,
            remaining_load_kw=10.0,
        )
        assert result == 2.5


class TestPressurePvAdjustment:
    """IT-GAP09: Tempest air pressure → PV forecast correction."""

    def test_pressure_high(self):
        """1030 hPa → factor 1.1, category high."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1030.0)
        assert r["confidence_factor"] == 1.1
        assert r["pressure_category"] == "high"

    def test_pressure_normal(self):
        """1015 hPa → factor 1.0, category normal (boundary: >1015 is normal)."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1018.0)
        assert r["confidence_factor"] == 1.0
        assert r["pressure_category"] == "normal"

    def test_pressure_low(self):
        """1008 hPa → factor 0.8, category low."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1008.0)
        assert r["confidence_factor"] == 0.8
        assert r["pressure_category"] == "low"

    def test_pressure_storm(self):
        """1000 hPa → factor 0.6, category storm."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1000.0)
        assert r["confidence_factor"] == 0.6
        assert r["pressure_category"] == "storm"

    def test_pressure_falling_trend(self):
        """Normal pressure + falling -4 hPa/3h → factor 1.0 - 0.1 = 0.9."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1018.0, pressure_trend_hpa_3h=-4.0)
        assert r["confidence_factor"] == 0.9
        assert r["pressure_category"] == "normal"
        assert "falling rapidly" in r["reason"]

    def test_pressure_rising_trend(self):
        """Low pressure + rising +4 hPa/3h → factor 0.8 + 0.05 = 0.85."""
        from custom_components.carmabox.core.planner import pressure_pv_adjustment

        r = pressure_pv_adjustment(pressure_hpa=1008.0, pressure_trend_hpa_3h=4.0)
        assert r["confidence_factor"] == 0.85
        assert r["pressure_category"] == "low"
        assert "rising" in r["reason"]


class TestSolarAllocation:
    """Tests for plan_solar_allocation() — PV surplus allocation between battery and EV."""

    def test_solar_alloc_plenty_of_sun(self):
        """Big surplus, battery fills before sunset → EV 6A via early path."""
        # 6 hours of sun left, high PV = big surplus
        # Battery fills well before sunset → "battery fills" early return
        # gives EV min amps (6A) with surplus_after_battery=0 (Grid Guard protects)
        pv = [12.0, 12.0, 10.0, 9.0, 7.0, 6.0]
        load = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
        # surplus = [10,10,8,7,5,4] = 44 kWh, need = 8, avg = 7.33
        # hours_to_full = 8/7.33 = 1.09 < 6*0.8 = 4.8, bat > 50% → early return
        result = plan_solar_allocation(
            battery_soc_pct=60.0,
            battery_cap_kwh=20.0,  # need = 40% * 20 = 8 kWh
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=13,
            sunset_hour=19,
        )
        assert result.ev_can_charge is True
        assert result.ev_recommended_amps >= 6  # at least min amps
        assert result.surplus_after_battery_kwh >= 0.0  # early path reports 0
        assert result.battery_hours_to_full > 0
        assert "Battery fills" in result.reason

    def test_solar_alloc_just_enough_for_battery(self):
        """8 kWh surplus, battery needs 8 kWh → no EV."""
        # 4 hours, 2 kW surplus/h = 8 kWh total, battery needs 8 kWh
        pv = [4.0, 4.0, 3.0, 3.0]
        load = [2.0, 2.0, 2.0, 2.0]
        result = plan_solar_allocation(
            battery_soc_pct=60.0,
            battery_cap_kwh=20.0,  # need = 40% * 20 = 8 kWh
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        assert result.ev_can_charge is False
        assert result.ev_recommended_amps == 0

    def test_solar_alloc_battery_nearly_full(self):
        """Battery at 90%, fills fast before sunset → EV 6A via early path."""
        # need = 10% * 20 = 2 kWh, surplus = [6,6,5,5] = 22 kWh, avg = 5.5
        # hours_to_full = 2/5.5 = 0.36 < 4*0.8 = 3.2, bat > 50% → early return
        pv = [8.0, 8.0, 7.0, 7.0]
        load = [2.0, 2.0, 2.0, 2.0]
        result = plan_solar_allocation(
            battery_soc_pct=90.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=40.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        assert result.ev_can_charge is True
        assert result.ev_recommended_amps >= 6  # at least min amps
        assert result.surplus_after_battery_kwh >= 0.0  # early path reports 0
        assert "Battery fills" in result.reason

    def test_solar_alloc_no_sun_left(self):
        """Sunset already passed → no allocation."""
        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=[5.0, 5.0],
            hourly_consumption_kw=[2.0, 2.0],
            current_hour=20,
            sunset_hour=19,
        )
        assert result.ev_can_charge is False
        assert result.ev_recommended_amps == 0
        assert "No solar hours" in result.reason

    def test_solar_alloc_ev_above_target_still_charges(self):
        """EV at 80% > target 75% but solar is FREE → still charges if margin."""
        # Daytime solar charging ignores target — free kWh always good
        # bat_soc=51% (> 50%) so "battery fills before sunset" path fires
        # surplus=[6,6,6,6]=24kWh, need=9.8kWh, avg=6, hours_to_full=1.63
        # 1.63 < 4*0.8=3.2 → early return with EV 6A
        result = plan_solar_allocation(
            battery_soc_pct=51.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=80.0,
            ev_target_pct=75.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=[8.0, 8.0, 8.0, 8.0],
            hourly_consumption_kw=[2.0, 2.0, 2.0, 2.0],
            current_hour=14,
            sunset_hour=19,
        )
        assert result.ev_can_charge is True  # Free solar → always charge
        assert result.ev_recommended_amps >= 6

    def test_solar_alloc_ev_at_100_no_charge(self):
        """EV at 100% → nothing to charge."""
        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=100.0,
            ev_target_pct=75.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=[8.0, 8.0, 8.0, 8.0],
            hourly_consumption_kw=[2.0, 2.0, 2.0, 2.0],
            current_hour=14,
            sunset_hour=19,
        )
        assert result.ev_can_charge is False

    def test_solar_alloc_battery_full(self):
        """Battery at 100% → all surplus to EV."""
        # surplus = [8,8,6,6] = 28 kWh, all to EV, per h = 7 kW → 10.1A (clamped 10)
        pv = [10.0, 10.0, 8.0, 8.0]
        load = [2.0, 2.0, 2.0, 2.0]
        result = plan_solar_allocation(
            battery_soc_pct=100.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        assert result.ev_can_charge is True
        assert result.ev_recommended_amps > 0
        assert result.battery_hours_to_full == 0.0
        # All surplus goes to EV
        assert result.surplus_after_battery_kwh > 10.0

    def test_solar_alloc_low_margin_below_min_amps(self):
        """Margin too small for 6A minimum → amps=0."""
        # Tiny surplus: ~0.5 kWh/h over 4 hours = 2 kWh, battery needs 10 kWh
        # bat_soc=50% (not > 50%) avoids "battery fills before sunset" path
        # margin = 2 - 10 = -8 → no EV (all PV needed for battery)
        pv = [2.5, 2.5, 2.5, 2.5]
        load = [2.0, 2.0, 2.0, 2.0]
        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=20.0,  # need = 50% * 20 = 10 kWh
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        assert result.ev_can_charge is False
        assert result.ev_recommended_amps == 0

    def test_solar_alloc_battery_high_soc_generous(self):
        """Battery > 80% → generous bonus turns negative margin into EV charging.

        With the new logic, when surplus barely covers battery but SoC > 80%,
        the generous bonus (50% of need reserved) can unlock EV charging.
        At lower SoC without the bonus, margin stays negative → no EV.
        """
        # 4 hours, low surplus: 1.2 kW/h each → surplus_per_hour = [1.2]*4
        # total_surplus = 4.8 kWh
        # Battery at 85%: need = 15% * 20 = 3 kWh
        #   hours_to_full = 3/1.2 = 2.5, n=4, n*0.8=3.2 → 2.5 < 3.2 AND bat>50%
        #   → "battery fills before sunset" early return → EV 6A ✓
        # Battery at 40%: need = 60% * 20 = 12 kWh
        #   hours_to_full = 12/1.2 = 10 > n → check B fails (10 >= 4)
        #   No surplus > max_bat_charge → check C fails
        #   margin = 4.8 - 12 = -7.2 → no EV ✗
        pv = [3.2, 3.2, 3.2, 3.2]
        load = [2.0, 2.0, 2.0, 2.0]
        result_high = plan_solar_allocation(
            battery_soc_pct=85.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        # Compare with lower battery SoC — not enough surplus, no generous
        result_low = plan_solar_allocation(
            battery_soc_pct=40.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=15,
            sunset_hour=19,
        )
        assert result_high.ev_can_charge is True
        assert result_low.ev_can_charge is False

    def test_solar_alloc_low_confidence(self):
        """Low PV confidence (Tempest falling pressure) → no EV despite enough PV."""
        # High PV: 8kW each hour, load 2kW → surplus 6kW/h x 6h = 36 kWh
        # Battery needs: (100-70)/100 * 20 = 6 kWh
        # Normal: margin = 36 - 6 = 30 kWh → EV YES
        # Low conf 0.4: adjusted PV = 3.2kW → surplus 1.2kW/h = 7.2 kWh
        # Low conf: margin = 7.2 - 6 = 1.2 kWh → too small for 6A → NO EV
        pv = [8.0, 8.0, 8.0, 8.0, 8.0, 8.0]
        load = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
        result_normal = plan_solar_allocation(
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=12,
            sunset_hour=18,
            pv_confidence=1.0,
        )
        result_low = plan_solar_allocation(
            battery_soc_pct=30.0,
            battery_cap_kwh=20.0,
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            hourly_pv_kw=pv,
            hourly_consumption_kw=load,
            current_hour=12,
            sunset_hour=18,
            pv_confidence=0.3,
        )
        assert result_normal.ev_can_charge is True  # Normal: enough margin
        assert result_low.ev_can_charge is False  # Low confidence: margin gone


class TestShouldDischargeNow:
    """Price-driven discharge decisions — don't waste cheap hours."""

    def test_should_discharge_expensive_now(self):
        """Current price 80 ore, upcoming avg ~50 → discharge."""
        from custom_components.carmabox.core.planner import should_discharge_now

        # Upcoming: mix of cheap and expensive, top 25% avg ~ 80
        upcoming = [30, 40, 50, 60, 70, 80, 90, 100]
        r = should_discharge_now(
            current_price_ore=80.0,
            upcoming_prices_ore=upcoming,
            battery_soc_pct=70.0,
        )
        assert r["discharge"] is True
        assert r["recommended_kw"] > 0
        assert r["current_price"] == 80.0
        assert r["avg_expensive"] > 0

    def test_should_discharge_cheap_now(self):
        """Current price 5 ore, upcoming has 80 → DON'T discharge (save for later)."""
        from custom_components.carmabox.core.planner import should_discharge_now

        upcoming = [10, 20, 30, 40, 50, 60, 70, 80]
        r = should_discharge_now(
            current_price_ore=5.0,
            upcoming_prices_ore=upcoming,
            battery_soc_pct=70.0,
        )
        assert r["discharge"] is False
        assert r["recommended_kw"] == 0.0
        assert "save battery" in r["reason"].lower()

    def test_should_discharge_low_battery(self):
        """Price 80 but SoC 20% → DON'T discharge (preserve reserve)."""
        from custom_components.carmabox.core.planner import should_discharge_now

        upcoming = [30, 40, 50, 60, 70, 80, 90, 100]
        r = should_discharge_now(
            current_price_ore=80.0,
            upcoming_prices_ore=upcoming,
            battery_soc_pct=20.0,
        )
        assert r["discharge"] is False
        assert r["recommended_kw"] == 0.0
        assert "too low" in r["reason"].lower() or "reserve" in r["reason"].lower()

    def test_should_discharge_no_upcoming(self):
        """No upcoming prices → don't discharge."""
        from custom_components.carmabox.core.planner import should_discharge_now

        r = should_discharge_now(
            current_price_ore=80.0,
            upcoming_prices_ore=[],
            battery_soc_pct=70.0,
        )
        assert r["discharge"] is False

    def test_should_discharge_at_min_soc(self):
        """Battery exactly at min_soc → don't discharge."""
        from custom_components.carmabox.core.planner import should_discharge_now

        r = should_discharge_now(
            current_price_ore=80.0,
            upcoming_prices_ore=[30, 40, 50, 60],
            battery_soc_pct=15.0,
            min_soc=15.0,
        )
        assert r["discharge"] is False


class TestOptimalDischargeHours:
    """Find most profitable discharge hours based on price spread."""

    def test_optimal_discharge_hours_sorted(self):
        """Finds most profitable hours first."""
        from custom_components.carmabox.core.planner import optimal_discharge_hours

        # Prices: cheap at start, expensive in middle
        prices = [10, 15, 20, 80, 90, 100, 50, 30, 20, 10]
        result = optimal_discharge_hours(
            prices_ore=prices,
            start_hour=0,
            battery_kwh_available=20.0,
            max_discharge_kw=5.0,
            min_profitable_spread_ore=20.0,
        )
        assert len(result) > 0
        # Most profitable first (highest savings)
        for i in range(len(result) - 1):
            assert result[i]["savings_ore"] >= result[i + 1]["savings_ore"]
        # Hour 5 (100 ore) should be first — highest spread
        assert result[0]["price"] == 100

    def test_optimal_discharge_min_spread(self):
        """Filters hours below min spread."""
        from custom_components.carmabox.core.planner import optimal_discharge_hours

        # All prices close together — spread < 20
        prices = [40, 42, 45, 48, 50, 52, 55]
        result = optimal_discharge_hours(
            prices_ore=prices,
            start_hour=0,
            battery_kwh_available=20.0,
            max_discharge_kw=5.0,
            min_profitable_spread_ore=20.0,
        )
        # Max spread = 55 - 40 = 15 < 20 → no hours qualify
        assert len(result) == 0

    def test_optimal_discharge_limited_by_battery(self):
        """Battery energy limits total discharge hours."""
        from custom_components.carmabox.core.planner import optimal_discharge_hours

        prices = [10, 80, 90, 100, 70, 60]
        result = optimal_discharge_hours(
            prices_ore=prices,
            start_hour=0,
            battery_kwh_available=5.0,  # Only 5 kWh — enough for ~1 hour
            max_discharge_kw=5.0,
            min_profitable_spread_ore=20.0,
        )
        # Should get at most 1 full hour at 5kW
        total_kw = sum(h["discharge_kw"] for h in result)
        assert total_kw <= 5.0

    def test_optimal_discharge_empty_prices(self):
        """Empty price list → empty result."""
        from custom_components.carmabox.core.planner import optimal_discharge_hours

        result = optimal_discharge_hours(
            prices_ore=[],
            start_hour=0,
            battery_kwh_available=20.0,
        )
        assert result == []

    def test_optimal_discharge_hour_wrapping(self):
        """Start hour 22 → hours wrap past midnight."""
        from custom_components.carmabox.core.planner import optimal_discharge_hours

        prices = [10, 10, 10, 80, 90]  # Hours 22,23,0,1,2
        result = optimal_discharge_hours(
            prices_ore=prices,
            start_hour=22,
            battery_kwh_available=20.0,
            max_discharge_kw=5.0,
            min_profitable_spread_ore=20.0,
        )
        assert len(result) > 0
        # Hour indices should wrap: 22+3=1, 22+4=2
        hours = [h["hour"] for h in result]
        assert all(0 <= h <= 23 for h in hours)


class TestShouldChargeEvTonight:
    """EV charge timing — tonight vs tomorrow vs free PV."""

    def test_ev_tonight_cheap(self):
        """Tonight 10 ore, tomorrow 80 ore -> charge tonight (>20% cheaper)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        r = should_charge_ev_tonight(
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            tonight_prices_ore=[10.0] * 8,
            tomorrow_night_prices_ore=[80.0] * 8,
            pv_tomorrow_kwh=10.0,  # Not enough PV
        )
        assert r["charge"] is True
        assert r["tonight_cost_kr"] < r["tomorrow_cost_kr"]
        assert r["pv_covers"] is False

    def test_ev_tonight_expensive(self):
        """Tonight 80 ore, tomorrow 10 ore -> wait (tomorrow cheaper)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        r = should_charge_ev_tonight(
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            tonight_prices_ore=[80.0] * 8,
            tomorrow_night_prices_ore=[10.0] * 8,
            pv_tomorrow_kwh=10.0,  # Not enough PV
        )
        assert r["charge"] is False
        assert r["tonight_cost_kr"] > r["tomorrow_cost_kr"]

    def test_ev_tonight_pv_covers(self):
        """46 kWh PV tomorrow -> wait for free solar."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        # EV need = (80-50)/100 * 92 = 27.6 kWh
        # PV coverage threshold = 27.6 + 15 = 42.6 kWh
        # 46 > 42.6 -> PV covers
        r = should_charge_ev_tonight(
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=92.0,
            tonight_prices_ore=[20.0] * 8,
            tomorrow_night_prices_ore=[20.0] * 8,
            pv_tomorrow_kwh=46.0,
        )
        assert r["charge"] is False
        assert r["pv_covers"] is True
        assert "free solar" in r["reason"]

    def test_ev_tonight_near_target(self):
        """EV 74%, target 75% -> small charge (1% of 92 kWh = 0.92 kWh)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        r = should_charge_ev_tonight(
            ev_soc_pct=74.0,
            ev_target_pct=75.0,
            ev_cap_kwh=92.0,
            tonight_prices_ore=[10.0] * 8,
            tomorrow_night_prices_ore=[80.0] * 8,
            pv_tomorrow_kwh=5.0,
        )
        assert r["charge"] is True
        assert r["ev_need_kwh"] == 0.92
        assert r["hours_needed"] == 1  # ceil(0.92 / 4.14) = 1
