"""Tests for CARMA Box multi-day planner module (PLAT-963)."""

from custom_components.carmabox.optimizer.multiday_planner import (
    DayInputs,
    _estimate_pv_profile,
    build_day_inputs,
    generate_multiday_plan,
)
from custom_components.carmabox.optimizer.price_patterns import PriceProfile


class TestEstimatePVProfile:
    """Test PV profile estimation."""

    def test_summer_has_long_day(self):
        profile = _estimate_pv_profile(30.0, 6)  # June, 30 kWh
        assert len(profile) == 24
        # Night hours should be 0
        assert profile[0] == 0.0
        assert profile[2] == 0.0
        # Midday should be positive
        assert profile[12] > 0
        # Total should approximate input
        total = sum(profile)
        assert abs(total - 30.0) < 1.0

    def test_winter_has_short_day(self):
        profile = _estimate_pv_profile(5.0, 12)  # December, 5 kWh
        assert len(profile) == 24
        # Long night hours
        assert profile[3] == 0.0
        assert profile[17] == 0.0
        total = sum(profile)
        assert abs(total - 5.0) < 1.0

    def test_zero_production(self):
        profile = _estimate_pv_profile(0.0, 1)
        assert all(v == 0.0 for v in profile)


class TestBuildDayInputs:
    """Test building multi-day input data."""

    def test_single_day(self):
        inputs = build_day_inputs(
            days=1,
            start_hour=10,
            start_weekday=0,
            start_month=3,
        )
        assert len(inputs) == 1
        assert inputs[0].weekday == 0

    def test_seven_days(self):
        inputs = build_day_inputs(
            days=7,
            start_hour=0,
            start_weekday=0,
            start_month=6,
        )
        assert len(inputs) == 7
        # Weekday wraps around
        assert inputs[5].weekday == 5  # Saturday
        assert inputs[6].weekday == 6  # Sunday

    def test_known_prices_used(self):
        today = [100.0 + h for h in range(24)]
        tomorrow = [50.0 + h for h in range(24)]
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=3,
            known_prices_today=today,
            known_prices_tomorrow=tomorrow,
        )
        assert inputs[0].price_source == "nordpool"
        assert inputs[0].prices == today
        assert inputs[1].price_source == "nordpool"
        assert inputs[1].prices == tomorrow
        assert inputs[2].price_source in ("predicted", "default")

    def test_known_pv_used(self):
        today_pv = [0] * 6 + [3.0] * 12 + [0] * 6
        inputs = build_day_inputs(
            days=2,
            start_hour=0,
            start_weekday=0,
            start_month=6,
            known_pv_today=today_pv,
        )
        assert inputs[0].pv_source == "solcast"
        assert inputs[0].pv_forecast == today_pv

    def test_price_model_used_for_day3(self):
        pp = PriceProfile()
        prices = [40.0 + h for h in range(24)]
        for _ in range(20):
            pp.record_day(prices, 3, False)

        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,
            start_month=3,
            price_model=pp,
        )
        assert inputs[2].price_source == "predicted"

    def test_consumption_profiles(self):
        weekday = [1.5] * 24
        weekend = [2.5] * 24
        inputs = build_day_inputs(
            days=7,
            start_hour=0,
            start_weekday=4,  # Friday
            start_month=3,
            consumption_profile_weekday=weekday,
            consumption_profile_weekend=weekend,
        )
        # Day 0 = Friday (weekday)
        assert inputs[0].consumption == weekday
        # Day 1 = Saturday (weekend)
        assert inputs[1].consumption == weekend

    def test_clamped_to_7_days(self):
        inputs = build_day_inputs(
            days=14,
            start_hour=0,
            start_weekday=0,
            start_month=3,
        )
        assert len(inputs) == 7


class TestGenerateMultiDayPlan:
    """Test multi-day plan generation."""

    def test_single_day_plan(self):
        di = DayInputs(
            prices=[50.0] * 24,
            pv_forecast=[0.0] * 6 + [5.0] * 12 + [0.0] * 6,
            consumption=[2.0] * 24,
            ev_schedule=[0.0] * 24,
        )
        plan = generate_multiday_plan(
            [di],
            start_hour=0,
            battery_soc=50.0,
            battery_cap_kwh=20.0,
        )
        assert plan.days == 1
        assert len(plan.hourly_plan) == 24
        assert len(plan.day_summaries) == 1

    def test_three_day_plan(self):
        inputs = [
            DayInputs(
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
                price_source="nordpool",
                pv_source="solcast",
            )
            for _ in range(3)
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=10,
            battery_soc=60.0,
            battery_cap_kwh=20.0,
        )
        assert plan.days == 3
        # Day 0: 24-10=14 hours, Day 1-2: 24 hours each
        expected_hours = 14 + 24 + 24
        assert len(plan.hourly_plan) == expected_hours
        assert len(plan.day_summaries) == 3

    def test_data_quality_known(self):
        inputs = [
            DayInputs(
                price_source="nordpool",
                pv_source="solcast",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        plan = generate_multiday_plan(inputs, start_hour=0, battery_soc=50)
        assert plan.data_quality == "known"

    def test_data_quality_mixed(self):
        inputs = [
            DayInputs(
                price_source="nordpool",
                pv_source="solcast",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            ),
            DayInputs(
                price_source="predicted",
                pv_source="predicted",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            ),
        ]
        plan = generate_multiday_plan(inputs, start_hour=0, battery_soc=50)
        assert plan.data_quality == "mixed"

    def test_data_quality_predicted(self):
        inputs = [
            DayInputs(
                price_source="predicted",
                pv_source="predicted",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        plan = generate_multiday_plan(inputs, start_hour=0, battery_soc=50)
        assert plan.data_quality == "predicted"

    def test_empty_inputs(self):
        plan = generate_multiday_plan([], start_hour=0, battery_soc=50)
        assert plan.days == 0
        assert plan.hourly_plan == []

    def test_total_cost_calculated(self):
        inputs = [
            DayInputs(
                prices=[100.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[3.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=0,
            battery_soc=50,
            battery_cap_kwh=20.0,
        )
        assert plan.total_cost_estimate_kr > 0

    def test_day_summaries_structure(self):
        inputs = [
            DayInputs(
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        plan = generate_multiday_plan(inputs, start_hour=0, battery_soc=50)
        ds = plan.day_summaries[0]
        assert "max_weighted_kw" in ds
        assert "avg_price" in ds
        assert "end_soc" in ds
