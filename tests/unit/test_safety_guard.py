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

    def test_pass_temp_none_fail_open(self, guard: SafetyGuard) -> None:
        """temp_c=None (unavailable) should allow discharge (fail-open)."""
        result = guard.check_discharge(
            soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000, temp_c=None
        )
        assert result.ok

    def test_block_at_zero_export(self, guard: SafetyGuard) -> None:
        """grid_power = 0 is not export, should pass."""
        result = guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=0)
        assert result.ok

    # ── IT-2075: Reserve-aware discharge gating ──────────────────

    def test_block_available_below_reserve(self, guard: SafetyGuard) -> None:
        """IT-2075: Block discharge when available_kwh < reserve_kwh + 1.0."""
        result = guard.check_discharge(
            soc_1=50,
            soc_2=50,
            min_soc=15,
            grid_power_w=2000,
            available_kwh=3.0,
            reserve_kwh=3.0,
        )
        assert not result.ok
        assert "reserve" in result.reason

    def test_pass_available_above_reserve(self, guard: SafetyGuard) -> None:
        """IT-2075: Allow discharge when available_kwh >= reserve_kwh + 1.0."""
        result = guard.check_discharge(
            soc_1=50,
            soc_2=50,
            min_soc=15,
            grid_power_w=2000,
            available_kwh=5.0,
            reserve_kwh=3.0,
        )
        assert result.ok

    def test_pass_reserve_exactly_at_margin(self, guard: SafetyGuard) -> None:
        """IT-2075: available == reserve + 1.0 should pass (not strictly less)."""
        result = guard.check_discharge(
            soc_1=50,
            soc_2=50,
            min_soc=15,
            grid_power_w=2000,
            available_kwh=4.0,
            reserve_kwh=3.0,
        )
        assert result.ok

    def test_pass_reserve_none_backward_compat(self, guard: SafetyGuard) -> None:
        """IT-2075: When reserve params not provided, skip the check (backward compat)."""
        result = guard.check_discharge(
            soc_1=50,
            soc_2=50,
            min_soc=15,
            grid_power_w=2000,
        )
        assert result.ok

    def test_block_reserve_zero_available_low(self, guard: SafetyGuard) -> None:
        """IT-2075: reserve=0 still requires 1.0 kWh margin."""
        result = guard.check_discharge(
            soc_1=50,
            soc_2=50,
            min_soc=15,
            grid_power_w=2000,
            available_kwh=0.5,
            reserve_kwh=0.0,
        )
        assert not result.ok
        assert "reserve" in result.reason


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

    def test_pass_charge_temp_none_fail_open(self, guard: SafetyGuard) -> None:
        """temp_c=None (unavailable) should allow charge (fail-open)."""
        result = guard.check_charge(soc_1=50, soc_2=50, temp_c=None)
        assert result.ok

    # ── PLAT-1019: Separate charge/discharge temperature thresholds ──

    def test_block_charge_at_1c(self, guard: SafetyGuard) -> None:
        """PLAT-1019: 1°C < 2°C charge threshold → block."""
        result = guard.check_charge(soc_1=50, soc_2=50, temp_c=1.0)
        assert not result.ok
        assert "temperature" in result.reason

    def test_block_charge_at_exactly_2c(self, guard: SafetyGuard) -> None:
        """PLAT-1019: Exactly 2°C is NOT below threshold → pass (boundary)."""
        # temp_min_charge=2.0, so temp_c=2.0 is NOT < 2.0 → pass
        result = guard.check_charge(soc_1=50, soc_2=50, temp_c=2.0)
        assert result.ok

    def test_pass_charge_at_3c(self, guard: SafetyGuard) -> None:
        """PLAT-1019: 3°C > 2°C charge threshold → pass."""
        result = guard.check_charge(soc_1=50, soc_2=50, temp_c=3.0)
        assert result.ok

    def test_pass_discharge_at_1c(self, guard: SafetyGuard) -> None:
        """PLAT-1019: 1°C > 0°C discharge threshold → pass (discharge OK above 0°C)."""
        result = guard.check_discharge(
            soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000, temp_c=1.0
        )
        assert result.ok

    def test_block_discharge_at_minus_1c(self, guard: SafetyGuard) -> None:
        """PLAT-1019: -1°C < 0°C discharge threshold → block."""
        result = guard.check_discharge(
            soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000, temp_c=-1.0
        )
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

    def test_block_power_1_unavailable(self, guard: SafetyGuard) -> None:
        """PLAT-946: power_1 unavailable at HA start → block."""
        result = guard.check_crosscharge(power_1_w=0, power_2_w=500, power_1_valid=False)
        assert not result.ok
        assert "unreliable" in result.reason

    def test_pass_power_2_unavailable_single_battery_mode(self, guard: SafetyGuard) -> None:
        """PLAT-946: power_2 unavailable but power_1 valid → single-battery mode (OK)."""
        result = guard.check_crosscharge(power_1_w=1000, power_2_w=0, power_2_valid=False)
        assert result.ok  # Single-battery mode — can't crosscharge with one battery

    def test_block_both_unavailable(self, guard: SafetyGuard) -> None:
        """PLAT-946: Both unavailable at HA start → block."""
        result = guard.check_crosscharge(
            power_1_w=0, power_2_w=0, power_1_valid=False, power_2_valid=False
        )
        assert not result.ok
        assert "unreliable" in result.reason

    def test_pass_valid_zero_readings(self, guard: SafetyGuard) -> None:
        """Both batteries at 0W with valid readings = idle, not crosscharge."""
        result = guard.check_crosscharge(
            power_1_w=0, power_2_w=0, power_1_valid=True, power_2_valid=True
        )
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


class TestSafetyLog:
    def test_log_populated_on_checks(self, guard: SafetyGuard) -> None:
        guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000)
        guard.check_charge(soc_1=50, soc_2=50)
        log = guard.get_safety_log()
        assert len(log) == 2
        assert log[0]["check"] == "discharge"
        assert log[0]["ok"] is True
        assert log[1]["check"] == "charge"

    def test_log_records_blocks(self, guard: SafetyGuard) -> None:
        guard.check_discharge(soc_1=10, soc_2=50, min_soc=15, grid_power_w=2000)
        log = guard.get_safety_log()
        assert len(log) == 1
        assert log[0]["ok"] is False
        assert "battery_1" in log[0]["reason"]

    def test_recent_block_count(self, guard: SafetyGuard) -> None:
        # Generate some blocks
        for _ in range(5):
            guard.check_discharge(soc_1=10, soc_2=50, min_soc=15, grid_power_w=2000)
        # And some passes
        guard.check_discharge(soc_1=50, soc_2=50, min_soc=15, grid_power_w=2000)
        assert guard.recent_block_count(3600) == 5

    def test_log_max_size(self) -> None:
        from custom_components.carmabox.optimizer.safety_guard import MAX_SAFETY_LOG_ENTRIES

        guard = SafetyGuard()
        for _ in range(MAX_SAFETY_LOG_ENTRIES + 10):
            guard.check_charge(soc_1=50, soc_2=50)
        assert len(guard.get_safety_log()) == MAX_SAFETY_LOG_ENTRIES

    def test_log_entries_are_dicts(self, guard: SafetyGuard) -> None:
        guard.check_crosscharge(1000, -1000)
        log = guard.get_safety_log()
        entry = log[0]
        assert isinstance(entry, dict)
        assert "timestamp" in entry
        assert "check" in entry
        assert "ok" in entry
        assert "reason" in entry
