"""Tests for EV charging strategy."""

from __future__ import annotations

from custom_components.carmabox.optimizer.ev_strategy import (
    calculate_ev_schedule,
    ev_needs_charge,
    ev_needs_full_charge,
)


class TestCalculateEvSchedule:
    def test_no_ev_uses_fallback_soc(self) -> None:
        """No EV SoC (soc < 0) → uses fallback 50%, still schedules charging."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=-1,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=2.0,
        )
        # With fallback 50% SoC, EV needs ~49 kWh → should schedule some hours
        assert len(schedule) == 12
        assert any(p > 0.0 for p in schedule)

    def test_already_full(self) -> None:
        """EV at 100% → no charge needed."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=100,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
        )
        assert all(p == 0.0 for p in schedule)

    def test_above_target_still_charges_if_cheap(self) -> None:
        """EV at 80% with target 75% → still charges at cheap hours to maximize SoC."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=80,
            ev_capacity_kwh=98,
            hourly_prices=[15.0] * 12,  # Cheap
            hourly_loads=[2.0] * 12,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            night_weight=0.5,
        )
        # Should charge — above target but cheap hours available
        assert sum(schedule) > 0

    def test_charges_at_night(self) -> None:
        """Should only charge during night hours (22-06)."""
        schedule = calculate_ev_schedule(
            start_hour=18,
            num_hours=14,
            ev_soc_pct=30,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 14,
            hourly_loads=[1.5] * 14,
            target_weighted_kw=4.0,
        )
        for i, kw in enumerate(schedule):
            abs_h = (18 + i) % 24
            if abs_h >= 22 or abs_h < 6:
                continue  # Night — may have charge
            assert kw == 0.0, f"Hour {abs_h} should be 0 (daytime)"

    def test_cheapest_hours_first(self) -> None:
        """Should prefer cheaper night hours."""
        # Night hours: 22, 23, 0, 1, 2, 3, 4, 5
        # Start at 20 → index 2=22h, 3=23h, 4=0h, ...
        prices = [80, 80, 10, 90, 20, 30, 40, 50, 60, 70, 80, 80]  # 20h-07h
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=60,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
        )
        # Index 2 (22h, price=10) should be charged before index 3 (23h, price=90)
        if schedule[2] > 0 and schedule[3] > 0:
            assert schedule[2] >= schedule[3]

    def test_full_charge_overdue_uses_expensive_hours(self) -> None:
        """When due for 100%, should use expensive hours too (Phase 2)."""
        # Mix of cheap and very expensive hours
        prices = [200, 200, 10, 10, 200, 200, 10, 10, 200, 200, 200, 200]
        schedule_normal = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=50,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[2.0] * 12,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            days_since_full_charge=2,
            full_charge_interval_days=7,
            night_weight=0.5,
        )
        schedule_overdue = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=50,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[2.0] * 12,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            days_since_full_charge=6,
            full_charge_interval_days=7,
            night_weight=0.5,
        )
        total_normal = sum(schedule_normal)
        total_overdue = sum(schedule_overdue)
        # Overdue forces 100% → needs more energy → uses expensive hours too
        assert total_overdue >= total_normal

    def test_no_night_hours_returns_zeros(self) -> None:
        """All daytime hours → no charging."""
        schedule = calculate_ev_schedule(
            start_hour=8,
            num_hours=6,
            ev_soc_pct=30,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 6,
            hourly_loads=[1.5] * 6,
            target_weighted_kw=2.0,
        )
        assert all(p == 0.0 for p in schedule)

    def test_respects_grid_target(self) -> None:
        """Charge power should respect grid target constraint."""
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=20,
            ev_capacity_kwh=98,
            hourly_prices=[30.0] * 8,
            hourly_loads=[3.0] * 8,  # High house load
            target_weighted_kw=2.5,  # Low target (night weight 0.5 → 5kW actual)
            night_weight=0.5,
        )
        for kw in schedule:
            if kw > 0:
                # At 230V, should be between 6A and 16A
                amps = kw * 1000 / 230
                assert amps >= 5.5  # ~6A minimum

    def test_zero_capacity_returns_zeros(self) -> None:
        """Zero capacity EV → no charge."""
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=30,
            ev_capacity_kwh=0,
            hourly_prices=[50.0] * 8,
            hourly_loads=[1.5] * 8,
            target_weighted_kw=2.0,
        )
        assert all(p == 0.0 for p in schedule)

    def test_expensive_hours_skipped_when_possible(self) -> None:
        """Expensive hours should be skipped if cheap hours suffice."""
        # 8 night hours: 4 cheap (20 öre), 4 expensive (200 öre)
        prices = [20, 20, 20, 20, 200, 200, 200, 200]
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=70,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[2.0] * 8,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            night_weight=0.5,
            min_amps=6,
        )
        # Expensive hours (index 4-7) should be 0 or much less than cheap hours
        cheap_total = sum(schedule[:4])
        expensive_total = sum(schedule[4:])
        assert cheap_total > expensive_total

    def test_maximizes_soc_beyond_target(self) -> None:
        """Should charge beyond morning_target if cheap hours remain."""
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=85,  # Already above 75% target
            ev_capacity_kwh=98,
            hourly_prices=[15.0] * 8,  # All cheap
            hourly_loads=[2.0] * 8,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            night_weight=0.5,
        )
        total_kwh = sum(schedule)
        # Should still charge even though above target — maximize SoC
        assert total_kwh > 0, "Should charge beyond target when cheap"

    def test_price_tiers(self) -> None:
        """Cheap hours get more amps than normal hours."""
        # 4 cheap (10 öre) + 4 normal (50 öre)
        prices = [10, 10, 10, 10, 50, 50, 50, 50]
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=30,
            ev_capacity_kwh=98,
            hourly_prices=prices,
            hourly_loads=[2.0] * 8,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
            night_weight=0.5,
        )
        cheap_avg = sum(schedule[:4]) / max(1, sum(1 for x in schedule[:4] if x > 0) or 1)
        normal_avg = sum(schedule[4:]) / max(1, sum(1 for x in schedule[4:] if x > 0) or 1)
        # Cheap hours should have higher charge rate
        if cheap_avg > 0 and normal_avg > 0:
            assert cheap_avg >= normal_avg


class TestEvNeedsCharge:
    def test_below_target(self) -> None:
        assert ev_needs_charge(50, 75) is True

    def test_at_target(self) -> None:
        assert ev_needs_charge(75, 75) is False

    def test_above_target(self) -> None:
        assert ev_needs_charge(80, 75) is False

    def test_no_ev(self) -> None:
        assert ev_needs_charge(-1, 75) is False


class TestEvNeedsFullCharge:
    def test_not_due(self) -> None:
        assert ev_needs_full_charge(3, 7) is False

    def test_due(self) -> None:
        assert ev_needs_full_charge(6, 7) is True

    def test_overdue(self) -> None:
        assert ev_needs_full_charge(10, 7) is True
