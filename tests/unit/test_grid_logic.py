"""Tests for grid_logic — target calculation + Ellevio weighting + season."""

from __future__ import annotations

from custom_components.carmabox.optimizer.grid_logic import (
    calculate_reserve,
    calculate_target,
    ellevio_weight,
    season_mode,
    season_reserve_multiplier,
)


class TestEllevioWeight:
    def test_day_is_1(self) -> None:
        for h in range(6, 22):
            assert ellevio_weight(h) == 1.0

    def test_night_is_half(self) -> None:
        for h in [22, 23, 0, 1, 2, 3, 4, 5]:
            assert ellevio_weight(h) == 0.5

    def test_custom_weight(self) -> None:
        assert ellevio_weight(23, night_weight=0.3) == 0.3


class TestSeasonMode:
    def test_summer(self) -> None:
        assert season_mode([30, 28, 25]) == "summer"

    def test_winter(self) -> None:
        assert season_mode([2, 3, 4]) == "winter"

    def test_transition(self) -> None:
        assert season_mode([8, 10, 12]) == "transition"

    def test_empty_forecast(self) -> None:
        assert season_mode([]) == "winter"

    def test_single_day(self) -> None:
        assert season_mode([30]) == "summer"


class TestSeasonReserveMultiplier:
    def test_summer_halves(self) -> None:
        assert season_reserve_multiplier("summer") == 0.5

    def test_winter_increases(self) -> None:
        assert season_reserve_multiplier("winter") == 1.5

    def test_transition_neutral(self) -> None:
        assert season_reserve_multiplier("transition") == 1.0


class TestCalculateReserve:
    def test_sunny_tomorrow_zero_reserve(self) -> None:
        """Sunny day ahead → no reserve needed (summer x0.5)."""
        reserve = calculate_reserve(
            pv_forecast_daily=[30, 28, 25],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=27.7 → summer (x0.5), surplus>10 → base=0, x0.5=0
        assert reserve == 0

    def test_cloudy_tomorrow_needs_reserve(self) -> None:
        """Cloudy tomorrow → reserve with season multiplier."""
        reserve = calculate_reserve(
            pv_forecast_daily=[30, 4, 28],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=20.7 → summer (x0.5). Base=5, x0.5=2.5
        assert reserve == 2.5

    def test_multiple_cloudy_days_transition(self) -> None:
        """3 cloudy days in transition season → x1.0."""
        reserve = calculate_reserve(
            pv_forecast_daily=[30, 4, 3, 5, 28],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=14 → transition (x1.0). 3 days x 5 kWh = 15
        assert reserve == 15

    def test_empty_forecast_winter_reserve(self) -> None:
        """No forecast → winter assumption (x1.5)."""
        reserve = calculate_reserve(
            pv_forecast_daily=[],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # winter: 2 x 5 x 1.5 = 15
        assert reserve == 15

    def test_partial_surplus_summer(self) -> None:
        """Partly cloudy in summer → summer multiplier."""
        reserve = calculate_reserve(
            pv_forecast_daily=[30, 18, 28],  # 18-15=3 surplus
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=25.3 → summer (x0.5). Base: need 5-3=2, x0.5=1.0
        assert reserve == 1.0

    def test_winter_forecast_high_reserve(self) -> None:
        """Winter (low PV avg) → 1.5x reserve."""
        reserve = calculate_reserve(
            pv_forecast_daily=[3, 4, 2],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=3.0 → winter (x1.5). Days: 4kWh→5, 2kWh→5. Base=10, x1.5=15
        assert reserve == 15

    def test_transition_season_neutral(self) -> None:
        """Transition (5-15 kWh avg) → x1.0."""
        reserve = calculate_reserve(
            pv_forecast_daily=[10, 4, 3, 28],
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # avg=11.25 → transition (x1.0). Days: 4→5, 3→5. Base=10, x1.0=10
        assert reserve == 10


class TestCalculateTarget:
    def test_sunny_low_target(self) -> None:
        """Lots of battery + sunny → low target."""
        target = calculate_target(
            battery_kwh_available=20,
            hourly_loads=[2.5] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=0,
        )
        assert target < 2.5

    def test_low_battery_high_target(self) -> None:
        """Little battery → high target (can't discharge much)."""
        target = calculate_target(
            battery_kwh_available=2,
            hourly_loads=[2.5] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=0,
        )
        assert target > 2.0

    def test_reserve_reduces_available(self) -> None:
        """Reserve reduces what's available → higher target."""
        target_no_reserve = calculate_target(
            battery_kwh_available=20,
            hourly_loads=[3.0] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=0,
        )
        target_with_reserve = calculate_target(
            battery_kwh_available=20,
            hourly_loads=[3.0] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=15,
        )
        assert target_with_reserve > target_no_reserve

    def test_zero_battery_max_target(self) -> None:
        """No battery → target equals max load."""
        target = calculate_target(
            battery_kwh_available=0,
            hourly_loads=[3.0] * 14,
            hourly_weights=[1.0] * 14,
            reserve_kwh=0,
        )
        assert target >= 2.9

    def test_night_weight_allows_more(self) -> None:
        """Night hours (weight 0.5) allow higher actual load."""
        target = calculate_target(
            battery_kwh_available=20,
            hourly_loads=[4.0] * 14,
            hourly_weights=[0.5] * 14,  # All night
            reserve_kwh=0,
        )
        # Night: 4kW x 0.5 = 2kW weighted → target should be ~2
        assert target < 3.0


class TestEdgeCases:
    def test_reserve_7_day_cap(self) -> None:
        """Reserve caps at 7 days horizon, with season multiplier."""
        # [30]+[2]*10 → avg≈4.5 → winter (x1.5)
        reserve = calculate_reserve(
            pv_forecast_daily=[30] + [2] * 10,
            daily_consumption_kwh=15,
            daily_battery_need_kwh=5,
        )
        # 7 x 5 x 1.5 = 52.5
        assert reserve == 52.5

    def test_target_empty_loads(self) -> None:
        """Empty loads returns fallback 5.0."""
        target = calculate_target(
            battery_kwh_available=0,
            hourly_loads=[],
            hourly_weights=[],
            reserve_kwh=0,
        )
        assert target == 5.0
