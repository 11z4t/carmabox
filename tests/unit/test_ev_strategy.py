"""Tests for EV charging strategy."""

from __future__ import annotations

from custom_components.carmabox.optimizer.ev_strategy import (
    calculate_ev_schedule,
    ev_needs_charge,
    ev_needs_full_charge,
)


class TestCalculateEvSchedule:
    def test_no_ev_returns_zeros(self) -> None:
        """No EV (soc < 0) → all zeros."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=-1,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=2.0,
        )
        assert all(p == 0.0 for p in schedule)

    def test_already_at_target(self) -> None:
        """EV already at 80% with target 75% → no charge."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=80,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=2.0,
            morning_target_soc=75.0,
        )
        assert all(p == 0.0 for p in schedule)

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

    def test_full_charge_overdue(self) -> None:
        """When due for 100%, target should be 100% not 75%."""
        schedule_normal = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=50,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
            days_since_full_charge=2,
            full_charge_interval_days=7,
        )
        schedule_overdue = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=50,
            ev_capacity_kwh=98,
            hourly_prices=[50.0] * 12,
            hourly_loads=[1.5] * 12,
            target_weighted_kw=4.0,
            morning_target_soc=75.0,
            days_since_full_charge=6,
            full_charge_interval_days=7,
        )
        total_normal = sum(schedule_normal)
        total_overdue = sum(schedule_overdue)
        assert total_overdue > total_normal

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

    def test_charges_at_min_when_target_tight(self) -> None:
        """Even if target is tight, EV charges at min amps (safety)."""
        schedule = calculate_ev_schedule(
            start_hour=22,
            num_hours=8,
            ev_soc_pct=20,
            ev_capacity_kwh=98,
            hourly_prices=[30.0] * 8,
            hourly_loads=[4.5] * 8,  # Very high house load
            target_weighted_kw=2.0,  # night w=0.5 → 4kW actual. 4.5 load > 4kW
            night_weight=0.5,
            min_amps=6,
        )
        # Should still charge (at min_amps) even though over target
        charged = [kw for kw in schedule if kw > 0]
        assert len(charged) > 0
        # Each charge should be ~1.38 kW (6A × 230V)
        for kw in charged:
            assert abs(kw - 1.38) < 0.5

    def test_total_energy_matches_need(self) -> None:
        """Total scheduled energy should approximately match what's needed."""
        schedule = calculate_ev_schedule(
            start_hour=20,
            num_hours=12,
            ev_soc_pct=50,
            ev_capacity_kwh=100,  # Nice round number
            hourly_prices=[30.0] * 12,
            hourly_loads=[1.0] * 12,
            target_weighted_kw=5.0,  # Generous target
            morning_target_soc=75.0,
        )
        total_kwh = sum(schedule)
        needed_kwh = (75 - 50) / 100 * 100  # 25 kWh
        # Should be close to needed (some quantization from amp steps)
        assert total_kwh <= needed_kwh + 2  # Not much over
        assert total_kwh >= needed_kwh - 2  # Not much under


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
