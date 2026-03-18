"""End-to-end scenario tests for CARMA Box optimizer.

Tests full pipeline: prices + PV + state → planner → plan validation.
No HA mocks — pure Python optimizer only.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.ev_strategy import calculate_ev_schedule
from custom_components.carmabox.optimizer.grid_logic import (
    calculate_reserve,
    calculate_target,
    ellevio_weight,
    season_mode,
)
from custom_components.carmabox.optimizer.planner import generate_plan
from custom_components.carmabox.optimizer.safety_guard import SafetyGuard


class TestWinterPeakScenario:
    """Winter evening: high consumption, no PV, expensive electricity."""

    def test_winter_evening_discharge(self) -> None:
        """Should discharge aggressively during 17-21 peak."""
        # Typical Swedish winter evening
        prices = [40] * 6 + [60] * 3 + [80] * 3 + [100] * 4 + [120] * 5 + [80] * 3
        pv = [0.0] * 24  # No solar in winter
        loads = [0.8] * 6 + [2.0] * 3 + [1.5] * 5 + [3.0] * 2 + [4.0] * 3 + [3.5] * 2 + [1.5] * 3
        ev = [1.38] * 6 + [0.0] * 18  # Night charging 6A

        plan = generate_plan(
            num_hours=24,
            start_hour=0,
            target_weighted_kw=2.0,
            hourly_loads=loads,
            hourly_pv=pv,
            hourly_prices=prices,
            hourly_ev=ev,
            battery_soc=85,
            ev_soc=40,
        )
        assert len(plan) == 24

        # Evening peak (17-21) should have discharge actions
        evening = [h for h in plan if 17 <= h.hour <= 21]
        discharge_count = sum(1 for h in evening if h.action == "d")
        assert discharge_count >= 2, "Should discharge during winter peak"

        # Battery SoC should decrease over the day
        assert plan[-1].battery_soc < 85

    def test_winter_reserve_conservative(self) -> None:
        """Winter → 1.5× reserve multiplier."""
        reserve = calculate_reserve(
            pv_forecast_daily=[3, 2, 4],  # Winter: avg 3 kWh
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        assert reserve > 10  # Winter multiplier makes it conservative

    def test_winter_season_detected(self) -> None:
        assert season_mode([3, 2, 4]) == "winter"


class TestSummerSurplusScenario:
    """Summer day: high PV, low prices, full batteries."""

    def test_summer_solar_charges_batteries(self) -> None:
        """Surplus PV should charge batteries."""
        prices = [30] * 24  # Cheap summer
        pv = [0] * 6 + [1, 3, 5, 7, 8, 9, 9, 8, 6, 4, 2, 0.5] + [0] * 6
        loads = [0.8] * 6 + [1.5] * 12 + [2.0] * 6
        ev = [0.0] * 24

        plan = generate_plan(
            num_hours=24,
            start_hour=0,
            target_weighted_kw=1.5,
            hourly_loads=loads,
            hourly_pv=pv,
            hourly_prices=prices,
            hourly_ev=ev,
            battery_soc=30,
            ev_soc=-1,
        )

        # Midday should charge from PV
        midday = [h for h in plan if 9 <= h.hour <= 15]
        charge_count = sum(1 for h in midday if h.action == "c")
        assert charge_count >= 3, "Should charge from PV surplus"

        # Battery should increase significantly
        max_soc = max(h.battery_soc for h in plan)
        assert max_soc > 50, "Battery should charge from solar"

    def test_summer_reserve_minimal(self) -> None:
        """Summer → 0.5× reserve multiplier."""
        reserve = calculate_reserve(
            pv_forecast_daily=[30, 28, 25],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        assert reserve < 5  # Sunny = minimal reserve

    def test_summer_low_target(self) -> None:
        """Summer with full battery → aggressive target."""
        target = calculate_target(
            battery_kwh_available=20,
            hourly_loads=[2.0] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=0,
        )
        assert target < 2.0


class TestTransitionScenario:
    """Spring/autumn: variable PV, moderate consumption."""

    def test_transition_mixed_plan(self) -> None:
        """Should have a mix of charge/discharge/idle."""
        prices = [50] * 6 + [70] * 6 + [40] * 6 + [90] * 6
        pv = [0] * 7 + [1, 3, 4, 5, 4, 3, 2, 1] + [0] * 9
        loads = [1.0] * 6 + [2.0] * 12 + [2.5] * 4 + [1.5] * 2
        ev = [0.0] * 24

        plan = generate_plan(
            num_hours=24,
            start_hour=0,
            target_weighted_kw=2.0,
            hourly_loads=loads,
            hourly_pv=pv,
            hourly_prices=prices,
            hourly_ev=ev,
            battery_soc=60,
            ev_soc=-1,
        )

        actions = {h.action for h in plan}
        # Should have at least idle + one other action
        assert len(actions) >= 2, f"Expected mixed plan, got only {actions}"

    def test_transition_season(self) -> None:
        assert season_mode([10, 8, 12]) == "transition"


class TestGridChargeScenario:
    """Very cheap night electricity → grid charge batteries."""

    def test_grid_charge_at_cheap_hours(self) -> None:
        """Should charge from grid when price < 15 öre."""
        prices = [5, 8, 10, 12, 8, 5] + [60] * 18  # Cheap at night
        pv = [0.0] * 24
        loads = [0.8] * 6 + [2.0] * 18
        ev = [0.0] * 24

        plan = generate_plan(
            num_hours=24,
            start_hour=0,
            target_weighted_kw=2.0,
            hourly_loads=loads,
            hourly_pv=pv,
            hourly_prices=prices,
            hourly_ev=ev,
            battery_soc=25,
            ev_soc=-1,
            grid_charge_price_threshold=15.0,
        )

        # Night hours should have grid charge
        night = [h for h in plan if h.hour < 6]
        grid_charge = sum(1 for h in night if h.action == "g")
        assert grid_charge >= 3, "Should grid charge during cheap hours"


class TestEVPriorityScenario:
    """EV charging coordinated with battery."""

    def test_ev_charges_at_night(self) -> None:
        """EV should charge during night hours at cheapest prices."""
        prices = [80] * 18 + [20, 15, 10, 30, 25, 40]  # Cheap 20-22h
        loads = [1.5] * 24

        ev_schedule = calculate_ev_schedule(
            start_hour=18,
            num_hours=12,  # 18:00-06:00
            ev_soc_pct=30,
            ev_capacity_kwh=98,
            hourly_prices=prices[18:] + prices[:6],
            hourly_loads=loads[:12],
            target_weighted_kw=3.0,
            morning_target_soc=75.0,
        )

        # Cheapest hours (20-22 → index 2-4) should have most charging
        total_kwh = sum(ev_schedule)
        assert total_kwh > 10, f"EV needs ~44 kWh to reach 75%, got {total_kwh}"

    def test_ev_does_not_charge_daytime(self) -> None:
        """No EV charging during daytime hours."""
        ev_schedule = calculate_ev_schedule(
            start_hour=8,
            num_hours=10,  # 08-18, all daytime
            ev_soc_pct=30,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 10,
            hourly_loads=[2.0] * 10,
            target_weighted_kw=3.0,
        )
        assert all(kw == 0.0 for kw in ev_schedule)


class TestSafetyScenario:
    """Safety guards prevent dangerous operations."""

    def test_safety_blocks_low_soc_discharge(self) -> None:
        guard = SafetyGuard(min_soc=15.0)
        result = guard.check_discharge(soc_1=12, soc_2=10, min_soc=15, grid_power_w=3000)
        assert not result.ok

    def test_safety_blocks_crosscharge(self) -> None:
        guard = SafetyGuard()
        result = guard.check_crosscharge(power_1_w=-1500, power_2_w=1200)
        assert not result.ok

    def test_safety_blocks_export_discharge(self) -> None:
        guard = SafetyGuard()
        result = guard.check_discharge(soc_1=80, soc_2=80, min_soc=15, grid_power_w=-2000)
        assert not result.ok

    def test_rate_guard_blocks_oscillation(self) -> None:
        guard = SafetyGuard(max_mode_changes_per_hour=5)
        for _ in range(5):
            guard.record_mode_change()
        result = guard.check_rate_limit()
        assert not result.ok

    def test_write_verify_detects_lockup(self) -> None:
        guard = SafetyGuard()
        result = guard.check_write_verify("discharge_battery", "charge_pv")
        assert not result.ok


class TestEllevioWeightScenario:
    """Ellevio weighting affects optimal target."""

    def test_night_weight_doubles_capacity(self) -> None:
        """Night weight 0.5 → actual grid can be 2× target."""
        for h in [22, 23, 0, 1, 2, 3, 4, 5]:
            w = ellevio_weight(h)
            assert w == 0.5
            # If target is 2.0 kW weighted, actual grid can be 4.0 kW
            actual_max = 2.0 / w
            assert actual_max == 4.0

    def test_day_weight_1to1(self) -> None:
        """Day weight 1.0 → actual grid equals target."""
        for h in range(6, 22):
            w = ellevio_weight(h)
            assert w == 1.0
