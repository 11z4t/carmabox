"""Tests for CARMA Box planner — core optimizer logic."""

from custom_components.carmabox.optimizer.planner import (
    calculate_target,
    ellevio_weight,
    generate_plan,
)


class TestEllevioWeight:
    def test_day_weight_is_1(self) -> None:
        for h in range(6, 22):
            assert ellevio_weight(h) == 1.0

    def test_night_weight_is_half(self) -> None:
        for h in [22, 23, 0, 1, 2, 3, 4, 5]:
            assert ellevio_weight(h) == 0.5

    def test_custom_night_weight(self) -> None:
        assert ellevio_weight(23, night_weight=0.3) == 0.3
        assert ellevio_weight(12, night_weight=0.3) == 1.0


class TestCalculateTarget:
    def test_sunny_day_low_target(self) -> None:
        """Lots of battery + sunny forecast → low target (discharge more)."""
        target = calculate_target(
            battery_kwh_available=20.0,
            hours=14,
            hourly_loads=[2.5] * 14,
            hourly_weights=[1.0] * 14,
            pv_forecast_3d=[30, 28, 25],
        )
        assert target < 2.5  # Should discharge aggressively

    def test_cloudy_day_high_target(self) -> None:
        """Low battery + cloudy forecast → high target (conserve)."""
        target = calculate_target(
            battery_kwh_available=5.0,
            hours=14,
            hourly_loads=[2.5] * 14,
            hourly_weights=[1.0] * 14,
            pv_forecast_3d=[3, 4, 5],
        )
        assert target > 2.0  # Should conserve

    def test_zero_battery_max_target(self) -> None:
        """No battery available → target equals max load."""
        target = calculate_target(
            battery_kwh_available=0.0,
            hours=14,
            hourly_loads=[2.5] * 14,
            hourly_weights=[1.0] * 14,
            pv_forecast_3d=[10, 10, 10],
        )
        assert target >= 2.4  # Can't discharge at all


class TestGeneratePlan:
    def test_plan_length_matches_hours(self) -> None:
        plan = generate_plan(
            num_hours=24, start_hour=17,
            target_weighted_kw=2.0,
            hourly_loads=[2.5] * 24,
            hourly_pv=[0.0] * 24,
            hourly_prices=[50.0] * 24,
            hourly_ev=[0.0] * 24,
            battery_soc=80, ev_soc=50,
        )
        assert len(plan) == 24

    def test_never_discharge_during_surplus(self) -> None:
        """When PV > load (net < 0), should charge not discharge."""
        plan = generate_plan(
            num_hours=8, start_hour=9,
            target_weighted_kw=2.0,
            hourly_loads=[1.5] * 8,
            hourly_pv=[4.0] * 8,  # Surplus
            hourly_prices=[50.0] * 8,
            hourly_ev=[0.0] * 8,
            battery_soc=50, ev_soc=50,
        )
        for h in plan:
            assert h.action != 'd', f"Hour {h.hour}: discharge during surplus"
            assert h.battery_kw >= 0, f"Hour {h.hour}: negative (discharge) during surplus"

    def test_discharge_when_load_above_target(self) -> None:
        """High load should trigger discharge."""
        plan = generate_plan(
            num_hours=4, start_hour=17,
            target_weighted_kw=2.0,
            hourly_loads=[4.0] * 4,  # Above target
            hourly_pv=[0.0] * 4,
            hourly_prices=[100.0] * 4,
            hourly_ev=[0.0] * 4,
            battery_soc=80, ev_soc=50,
        )
        discharge_hours = [h for h in plan if h.action == 'd']
        assert len(discharge_hours) > 0

    def test_respects_min_soc(self) -> None:
        """Battery SoC should never go below min."""
        plan = generate_plan(
            num_hours=24, start_hour=17,
            target_weighted_kw=1.0,  # Very aggressive
            hourly_loads=[5.0] * 24,
            hourly_pv=[0.0] * 24,
            hourly_prices=[100.0] * 24,
            hourly_ev=[0.0] * 24,
            battery_soc=50, ev_soc=50,
            battery_min_soc=15.0,
        )
        for h in plan:
            assert h.battery_soc >= 14, f"Hour {h.hour}: SoC {h.battery_soc}% below min"

    def test_ev_soc_tracks_charging(self) -> None:
        """EV SoC should increase when EV demand > 0."""
        plan = generate_plan(
            num_hours=8, start_hour=22,
            target_weighted_kw=3.0,
            hourly_loads=[1.0] * 8,
            hourly_pv=[0.0] * 8,
            hourly_prices=[30.0] * 8,
            hourly_ev=[1.38] * 8,  # 6A charging
            battery_soc=50, ev_soc=30,
        )
        assert plan[-1].ev_soc > 30, "EV SoC should increase"

    def test_charges_from_solar_surplus(self) -> None:
        """Solar surplus should charge batteries."""
        plan = generate_plan(
            num_hours=4, start_hour=10,
            target_weighted_kw=2.0,
            hourly_loads=[1.0] * 4,
            hourly_pv=[5.0] * 4,  # Big surplus
            hourly_prices=[50.0] * 4,
            hourly_ev=[0.0] * 4,
            battery_soc=30, ev_soc=50,
        )
        charge_hours = [h for h in plan if h.action == 'c']
        assert len(charge_hours) > 0
        assert plan[-1].battery_soc > 30

    def test_grid_never_negative(self) -> None:
        """Grid import should never be negative in plan."""
        plan = generate_plan(
            num_hours=24, start_hour=0,
            target_weighted_kw=2.0,
            hourly_loads=[2.0] * 24,
            hourly_pv=[3.0] * 12 + [0.0] * 12,
            hourly_prices=[50.0] * 24,
            hourly_ev=[1.0] * 8 + [0.0] * 16,
            battery_soc=50, ev_soc=50,
        )
        for h in plan:
            assert h.grid_kw >= -0.1, f"Hour {h.hour}: negative grid {h.grid_kw}"
