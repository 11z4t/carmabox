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


class TestRateGuard:
    def test_pass_under_limit(self, guard: SafetyGuard) -> None:
        for _ in range(5):
            guard.record_mode_change()
        assert guard.check_rate_limit().ok

    def test_block_over_limit(self) -> None:
        guard = SafetyGuard(max_mode_changes_per_hour=3)
        for _ in range(3):
            guard.record_mode_change()
        result = guard.check_rate_limit()
        assert not result.ok
        assert "rate limit" in result.reason

    def test_old_changes_pruned(self) -> None:
        import time

        guard = SafetyGuard(max_mode_changes_per_hour=2)
        # Add old timestamps (>1h ago)
        guard._mode_change_timestamps = [time.monotonic() - 3700, time.monotonic() - 3600]
        # Should pass since old entries are pruned
        assert guard.check_rate_limit().ok


class TestHeartbeat:
    def test_pass_fresh(self, guard: SafetyGuard) -> None:
        guard.update_heartbeat()
        assert guard.check_heartbeat().ok

    def test_block_stale(self, guard: SafetyGuard) -> None:
        import time

        guard._last_heartbeat = time.monotonic() - 200
        result = guard.check_heartbeat(max_stale_seconds=120)
        assert not result.ok
        assert "heartbeat" in result.reason

    def test_custom_threshold(self, guard: SafetyGuard) -> None:
        import time

        guard._last_heartbeat = time.monotonic() - 50
        assert guard.check_heartbeat(max_stale_seconds=60).ok
        assert not guard.check_heartbeat(max_stale_seconds=30).ok


class TestWriteVerify:
    def test_pass_matching(self, guard: SafetyGuard) -> None:
        result = guard.check_write_verify("discharge_battery", "discharge_battery")
        assert result.ok

    def test_block_mismatch(self, guard: SafetyGuard) -> None:
        result = guard.check_write_verify("discharge_battery", "charge_pv")
        assert not result.ok
        assert "write-verify" in result.reason

    def test_pass_empty(self, guard: SafetyGuard) -> None:
        result = guard.check_write_verify("", "")
        assert result.ok

    def test_pass_no_expected(self, guard: SafetyGuard) -> None:
        result = guard.check_write_verify("", "discharge_battery")
        assert result.ok
