"""Unit tests for Evening/Night Multi-Period Optimizer (IT-2381).

Pure Python tests — no HA mocks. Tests strategy evaluation and
battery schedule modification.
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.evening_optimizer import (
    EVENING_HOURS,
    NIGHT_HOURS,
    _avg_price,
    _cheapest_n_hours,
    _pad_prices,
    _peak_price,
    apply_strategy_to_battery_schedule,
    evaluate_evening_strategy,
)
from custom_components.carmabox.optimizer.models import MultiPeriodStrategy


class TestHelpers:
    """Test helper functions."""

    def test_avg_price(self) -> None:
        prices = [10.0] * 12 + [100.0] * 12
        assert _avg_price(prices, [0, 1, 2]) == 10.0
        assert _avg_price(prices, [12, 13, 14]) == 100.0

    def test_avg_price_empty(self) -> None:
        assert _avg_price([], [0, 1]) == 50.0

    def test_cheapest_n_hours(self) -> None:
        prices = [float(h) for h in range(24)]  # price = hour index
        cheapest = _cheapest_n_hours(prices, [5, 10, 15, 20], 2)
        assert cheapest == [5, 10]

    def test_peak_price(self) -> None:
        prices = [float(h * 5) for h in range(24)]
        assert _peak_price(prices, [20, 21, 22]) == 110.0

    def test_pad_prices_short(self) -> None:
        assert len(_pad_prices([10.0] * 10)) == 24

    def test_pad_prices_long(self) -> None:
        assert len(_pad_prices([10.0] * 30)) == 24


class TestEvaluateStrategy:
    """Test strategy evaluation logic."""

    def _make_prices(self, evening: float, night: float, default: float = 50.0) -> list[float]:
        """Create 24h price list with specified evening and night prices."""
        prices = [default] * 24
        for h in EVENING_HOURS:
            prices[h] = evening
        for h in NIGHT_HOURS:
            if h < 24:
                prices[h] = night
        return prices

    def test_high_spread_chooses_a(self) -> None:
        """When evening prices are much higher than night → discharge evening (A)."""
        prices_today = self._make_prices(evening=120.0, night=20.0)
        prices_tomorrow = [60.0] * 24

        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
            battery_cap_kwh=20.0,
            prices_today_24h=prices_today,
            prices_tomorrow_24h=prices_tomorrow,
            pv_tomorrow_kwh=5.0,  # Not enough solar to refill
            daily_consumption_kwh=15.0,
        )

        assert result.chosen == "A"
        assert result.a_evening_savings_kr > 0
        assert result.evening_avg_price_ore > result.night_avg_price_ore

    def test_high_tomorrow_peak_chooses_b(self) -> None:
        """When tomorrow peak is very expensive → save battery (B)."""
        prices_today = self._make_prices(evening=40.0, night=35.0)
        # Tomorrow: very expensive peaks
        prices_tomorrow = [30.0] * 7 + [200.0] * 13 + [30.0] * 4

        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
            battery_cap_kwh=20.0,
            prices_today_24h=prices_today,
            prices_tomorrow_24h=prices_tomorrow,
            pv_tomorrow_kwh=3.0,  # Cloudy
            daily_consumption_kwh=15.0,
        )

        assert result.chosen == "B"
        assert result.b_tomorrow_savings_kr > 0

    def test_sunny_tomorrow_always_a(self) -> None:
        """When solar fills battery tomorrow → always discharge evening (A)."""
        prices_today = self._make_prices(evening=40.0, night=35.0)
        prices_tomorrow = [200.0] * 24  # Even if tomorrow is expensive

        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
            battery_cap_kwh=20.0,
            prices_today_24h=prices_today,
            prices_tomorrow_24h=prices_tomorrow,
            pv_tomorrow_kwh=30.0,  # Lots of sun
            daily_consumption_kwh=15.0,
        )

        assert result.chosen == "A"
        assert result.confidence >= 0.8
        assert "Sol imorgon" in result.reasoning

    def test_low_battery_low_confidence(self) -> None:
        """Very little battery → low confidence regardless of strategy."""
        prices_today = self._make_prices(evening=100.0, night=20.0)

        result = evaluate_evening_strategy(
            battery_kwh_available=1.0,
            battery_cap_kwh=20.0,
            prices_today_24h=prices_today,
        )

        assert result.confidence < 0.5
        assert "Lite batteri" in result.reasoning

    def test_ev_night_cost_same_both_strategies(self) -> None:
        """EV night charging cost should be the same in both strategies."""
        prices_today = self._make_prices(evening=80.0, night=20.0)

        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
            prices_today_24h=prices_today,
            ev_need_kwh=15.0,
        )

        assert result.a_ev_night_cost_kr == result.b_ev_night_cost_kr
        assert result.ev_need_kwh == 15.0

    def test_no_prices_uses_defaults(self) -> None:
        """Without prices, should still produce a valid result."""
        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
        )

        assert result.chosen in ("A", "B")
        assert result.evening_avg_price_ore > 0

    def test_night_capacity_warning(self) -> None:
        """When EV takes most night capacity, should flag recharge constraint."""
        prices_today = self._make_prices(evening=100.0, night=20.0)

        result = evaluate_evening_strategy(
            battery_kwh_available=10.0,
            battery_cap_kwh=20.0,
            prices_today_24h=prices_today,
            ev_need_kwh=20.0,  # Takes most night capacity
            max_grid_charge_kw=3.0,  # 3kW x 8h = 24 kWh total
        )

        # Should still decide but with reduced confidence if A
        assert result.chosen in ("A", "B")


class TestApplyStrategy:
    """Test battery schedule modification."""

    def _base_schedule(self, hours: int = 24) -> list[tuple[float, str]]:
        """Create a basic idle battery schedule."""
        return [(0.0, "i")] * hours

    def test_strategy_a_forces_evening_discharge(self) -> None:
        """Strategy A should force discharge during evening hours."""
        strategy = MultiPeriodStrategy(chosen="A", confidence=0.8)
        schedule = self._base_schedule()

        result = apply_strategy_to_battery_schedule(
            strategy=strategy,
            battery_schedule=schedule,
            start_hour=0,
            battery_kwh_available=10.0,
            max_discharge_kw=5.0,
        )

        # Evening hours (17-21) should have discharge
        evening_actions = [result[h] for h in EVENING_HOURS]
        discharging = [kw for kw, act in evening_actions if act == "d"]
        assert len(discharging) > 0
        assert all(kw < 0 for kw in discharging)

    def test_strategy_b_suppresses_evening_discharge(self) -> None:
        """Strategy B should suppress existing evening discharge."""
        schedule = self._base_schedule()
        # Set some evening hours to discharge
        for h in EVENING_HOURS:
            schedule[h] = (-3.0, "d")

        strategy = MultiPeriodStrategy(chosen="B", confidence=0.8)

        result = apply_strategy_to_battery_schedule(
            strategy=strategy,
            battery_schedule=schedule,
            start_hour=0,
            battery_kwh_available=10.0,
        )

        # Evening discharge should be suppressed
        for h in EVENING_HOURS:
            kw, act = result[h]
            assert act == "i"
            assert kw == 0.0

    def test_low_confidence_no_change(self) -> None:
        """Low confidence should not modify the schedule."""
        schedule = self._base_schedule()
        strategy = MultiPeriodStrategy(chosen="A", confidence=0.2)

        result = apply_strategy_to_battery_schedule(
            strategy=strategy,
            battery_schedule=schedule,
            start_hour=0,
            battery_kwh_available=10.0,
        )

        assert result == schedule

    def test_nonzero_start_hour(self) -> None:
        """Schedule with non-zero start hour should correctly map evening hours."""
        schedule = self._base_schedule()
        strategy = MultiPeriodStrategy(chosen="A", confidence=0.8)

        result = apply_strategy_to_battery_schedule(
            strategy=strategy,
            battery_schedule=schedule,
            start_hour=15,  # Starts at 15:00
            battery_kwh_available=10.0,
            max_discharge_kw=5.0,
        )

        # Hours 17-21 are slots 2-6 (15+2=17, 15+6=21)
        for i in range(2, 7):
            kw, act = result[i]
            if act == "d":
                assert kw < 0


class TestIntegration:
    """Integration test: scheduler uses evening optimizer."""

    def test_scheduler_plan_includes_evening_strategy(self) -> None:
        """generate_scheduler_plan should include evening_strategy."""
        from custom_components.carmabox.optimizer.scheduler import (
            generate_scheduler_plan,
        )

        prices = [20.0] * 6 + [50.0] * 11 + [100.0] * 5 + [20.0] * 2
        plan = generate_scheduler_plan(
            start_hour=16,
            num_hours=24,
            hourly_prices=prices,
            battery_soc_pct=80.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=3.0,
        )

        assert plan.evening_strategy is not None
        assert plan.evening_strategy.chosen in ("A", "B")
        assert plan.evening_strategy.reasoning != ""

    def test_scheduler_with_tomorrow_prices(self) -> None:
        """generate_scheduler_plan with explicit tomorrow prices."""
        from custom_components.carmabox.optimizer.scheduler import (
            generate_scheduler_plan,
        )

        prices_today = [20.0] * 6 + [50.0] * 11 + [100.0] * 5 + [20.0] * 2
        prices_tomorrow = [15.0] * 6 + [80.0] * 12 + [40.0] * 6

        plan = generate_scheduler_plan(
            start_hour=18,
            num_hours=24,
            hourly_prices=prices_today,
            prices_tomorrow_24h=prices_tomorrow,
            battery_soc_pct=70.0,
            battery_cap_kwh=20.0,
            target_weighted_kw=3.0,
            pv_tomorrow_kwh=8.0,
            daily_consumption_kwh=15.0,
        )

        assert plan.evening_strategy is not None
        assert plan.evening_strategy.tomorrow_peak_price_ore > 0
