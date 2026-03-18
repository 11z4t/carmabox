"""Tests for SafetyGuard — CARMA Box safety layer."""

import pytest

from custom_components.carmabox.optimizer.safety_guard import SafetyGuard


@pytest.fixture
def guard() -> SafetyGuard:
    return SafetyGuard(min_soc=15.0, crosscharge_threshold_w=500.0)


class TestDischarge:
    def test_pass_normal(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000)
        assert result.ok

    def test_block_below_min_soc_battery_1(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=10, soc_2=50, min_soc=15, grid_power_w=2000)
        assert not result.ok
        assert "battery_1" in result.reason

    def test_block_below_min_soc_battery_2(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=12, min_soc=15, grid_power_w=2000)
        assert not result.ok
        assert "battery_2" in result.reason

    def test_block_during_export(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=-500)
        assert not result.ok
        assert "exporting" in result.reason

    def test_pass_no_second_battery(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=-1, min_soc=15, grid_power_w=2000)
        assert result.ok

    def test_block_cold_temperature(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000, temp_c=-5)
        assert not result.ok
        assert "temperature" in result.reason

    def test_block_hot_temperature(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000, temp_c=50)
        assert not result.ok
        assert "temperature" in result.reason

    def test_pass_at_exact_min_soc(self, guard: SafetyGuard) -> None:
        result = guard.check_discharge(soc_1=15, soc_2=15, min_soc=15, grid_power_w=2000)
        assert result.ok

    def test_block_at_zero_export(self, guard: SafetyGuard) -> None:
        """grid_power = 0 is not export, should pass."""
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=0)
        assert result.ok


class TestCharge:
    def test_pass_normal(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=50, soc_2=50)
        assert result.ok

    def test_block_all_full(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=100, soc_2=100)
        assert not result.ok
        assert "full" in result.reason

    def test_pass_one_full_one_not(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=100, soc_2=80)
        assert result.ok

    def test_pass_no_second_battery_not_full(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=80, soc_2=-1)
        assert result.ok

    def test_block_single_battery_full(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=100, soc_2=-1)
        assert not result.ok

    def test_block_cold_charge(self, guard: SafetyGuard) -> None:
        result = guard.check_charge(soc_1=50, soc_2=50, temp_c=-2)
        assert not result.ok
        assert "temperature" in result.reason


class TestCrosscharge:
    def test_pass_both_charging(self, guard: SafetyGuard) -> None:
        result = guard.check_crosscharge(power_1_w=-1000, power_2_w=-800)
        assert result.ok

    def test_pass_both_discharging(self, guard: SafetyGuard) -> None:
        result = guard.check_crosscharge(power_1_w=1000, power_2_w=800)
        assert result.ok

    def test_block_crosscharge(self, guard: SafetyGuard) -> None:
        result = guard.check_crosscharge(power_1_w=-1000, power_2_w=800)
        assert not result.ok
        assert "crosscharge" in result.reason

    def test_pass_below_threshold(self, guard: SafetyGuard) -> None:
        """Small opposite-sign power should not trigger."""
        result = guard.check_crosscharge(power_1_w=-200, power_2_w=300)
        assert result.ok

    def test_pass_no_second_battery(self, guard: SafetyGuard) -> None:
        result = guard.check_crosscharge(power_1_w=1000, power_2_w=0)
        assert result.ok
