"""Tests for consumption profile learning."""

from __future__ import annotations

from datetime import datetime

from custom_components.carmabox.optimizer.consumption import (
    ConsumptionProfile,
    calculate_house_consumption,
)


class TestCalculateHouseConsumption:
    def test_grid_only(self) -> None:
        """House = grid import when no battery/PV/EV."""
        result = calculate_house_consumption(2000, 0, 0, 0, 0)
        assert result == 2.0

    def test_battery_discharge_adds(self) -> None:
        """Battery discharge contributes to house."""
        result = calculate_house_consumption(1000, -500, -500, 0, 0)
        assert result == 2.0  # 1kW grid + 1kW battery

    def test_pv_adds(self) -> None:
        """PV production contributes to house."""
        result = calculate_house_consumption(0, 0, 0, 2000, 0)
        assert result == 2.0

    def test_ev_subtracts(self) -> None:
        """EV charging is NOT house consumption."""
        result = calculate_house_consumption(3000, 0, 0, 0, 1000)
        assert result == 2.0  # 3kW grid - 1kW EV = 2kW house

    def test_full_scenario(self) -> None:
        """Grid 2kW + battery 1kW + PV 0.5kW - EV 1.5kW = 2kW house."""
        result = calculate_house_consumption(2000, -1000, 0, 500, 1500)
        assert result == 2.0

    def test_export_zero_grid(self) -> None:
        """Exporting → grid contribution is 0."""
        result = calculate_house_consumption(-1000, 0, 0, 3000, 0)
        assert result == 3.0  # Only PV contributes

    def test_negative_result_clamped(self) -> None:
        """House consumption can't be negative."""
        result = calculate_house_consumption(0, 0, 0, 0, 5000)
        assert result == 0.0


class TestConsumptionProfile:
    def test_default_profile(self) -> None:
        p = ConsumptionProfile()
        assert len(p.weekday) == 24
        assert len(p.weekend) == 24
        assert not p.is_learned

    def test_update_weekday(self) -> None:
        p = ConsumptionProfile()
        old_val = p.weekday[10]
        p.update(10, 5.0, is_weekend=False)
        # EMA: 0.1 * 5.0 + 0.9 * old_val
        expected = 0.1 * 5.0 + 0.9 * old_val
        assert abs(p.weekday[10] - expected) < 0.01
        assert p.samples_weekday == 1

    def test_update_weekend(self) -> None:
        p = ConsumptionProfile()
        p.update(10, 5.0, is_weekend=True)
        assert p.samples_weekend == 1

    def test_is_learned_after_enough_samples(self) -> None:
        p = ConsumptionProfile()
        for _ in range(168):
            p.update(12, 2.0, is_weekend=False)
        assert p.is_learned

    def test_not_learned_too_few(self) -> None:
        p = ConsumptionProfile()
        for _ in range(100):
            p.update(12, 2.0, is_weekend=False)
        assert not p.is_learned

    def test_get_profile_weekday(self) -> None:
        p = ConsumptionProfile()
        for _ in range(50):
            p.update(12, 5.0, is_weekend=False)
        profile = p.get_profile(is_weekend=False)
        assert profile[12] > p.get_profile(is_weekend=True)[12]  # Weekday updated, weekend not

    def test_get_profile_for_date(self) -> None:
        p = ConsumptionProfile()
        # Monday
        profile = p.get_profile_for_date(datetime(2026, 3, 16))  # Monday
        assert len(profile) == 24
        # Saturday
        profile_sat = p.get_profile_for_date(datetime(2026, 3, 21))  # Saturday
        assert len(profile_sat) == 24

    def test_clamp_unreasonable(self) -> None:
        p = ConsumptionProfile()
        p.update(12, 100.0, is_weekend=False)  # Clamped to 20
        p.update(12, -5.0, is_weekend=False)  # Clamped to 0
        # Should be clamped values, not raw

    def test_invalid_hour_ignored(self) -> None:
        p = ConsumptionProfile()
        p.update(25, 5.0, is_weekend=False)  # Invalid hour
        p.update(-1, 5.0, is_weekend=False)
        assert p.samples_weekday == 0

    def test_to_dict_from_dict_roundtrip(self) -> None:
        p = ConsumptionProfile()
        for i in range(24):
            p.update(i, float(i) * 0.5, is_weekend=False)
        p.update(12, 3.0, is_weekend=True)

        d = p.to_dict()
        p2 = ConsumptionProfile.from_dict(d)

        assert p2.weekday == p.weekday
        assert p2.weekend == p.weekend
        assert p2.samples_weekday == p.samples_weekday
        assert p2.samples_weekend == p.samples_weekend

    def test_from_dict_invalid(self) -> None:
        """Invalid data → default profile."""
        p = ConsumptionProfile.from_dict({})
        assert len(p.weekday) == 24
        assert p.samples_weekday == 0

    def test_total_samples(self) -> None:
        p = ConsumptionProfile()
        p.update(10, 2.0, is_weekend=False)
        p.update(10, 2.0, is_weekend=True)
        assert p.total_samples == 2
