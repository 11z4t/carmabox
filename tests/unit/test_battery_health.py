"""Tests for CARMA Box battery health module (PLAT-963)."""

from custom_components.carmabox.optimizer.battery_health import (
    BatteryHealthState,
    complete_cycle,
    efficiency_for_temperature,
    efficiency_trend,
    estimate_degradation,
    health_summary,
    record_charge,
    record_discharge,
    record_monthly_snapshot,
    state_from_dict,
    state_to_dict,
)


class TestRecording:
    """Test charge/discharge recording."""

    def test_record_charge(self):
        state = BatteryHealthState()
        record_charge(state, 5.0, 20.0)
        assert state.current_charge_kwh == 5.0
        assert state.total_charge_kwh == 5.0

    def test_record_discharge(self):
        state = BatteryHealthState()
        record_discharge(state, 3.0, 20.0)
        assert state.current_discharge_kwh == 3.0
        assert state.total_discharge_kwh == 3.0

    def test_zero_ignored(self):
        state = BatteryHealthState()
        record_charge(state, 0.0)
        record_charge(state, -1.0)
        record_discharge(state, 0.0)
        record_discharge(state, -1.0)
        assert state.current_charge_kwh == 0.0
        assert state.current_discharge_kwh == 0.0

    def test_temperature_tracking(self):
        state = BatteryHealthState()
        record_charge(state, 5.0, 25.0)
        record_charge(state, 5.0, 15.0)
        assert state.current_temp_count == 2
        assert state.current_temp_sum == 40.0


class TestCompleteCycle:
    """Test cycle completion."""

    def test_basic_cycle(self):
        state = BatteryHealthState()
        record_charge(state, 10.0)
        record_discharge(state, 9.0)

        rec = complete_cycle(state, "2026-03-21", battery_cap_kwh=20.0)
        assert rec is not None
        assert rec.charge_kwh == 10.0
        assert rec.discharge_kwh == 9.0
        assert 0.85 < rec.efficiency < 0.95

    def test_cycle_resets_accumulators(self):
        state = BatteryHealthState()
        record_charge(state, 10.0)
        record_discharge(state, 9.0)
        complete_cycle(state, "2026-03-21")
        assert state.current_charge_kwh == 0
        assert state.current_discharge_kwh == 0

    def test_no_activity_returns_none(self):
        state = BatteryHealthState()
        record_charge(state, 0.1)  # Below 0.5 threshold
        rec = complete_cycle(state, "2026-03-21")
        assert rec is None

    def test_cycle_count_increments(self):
        state = BatteryHealthState()
        record_charge(state, 20.0)
        record_discharge(state, 18.0)
        complete_cycle(state, "2026-03-21", battery_cap_kwh=20.0)
        assert state.total_cycles == 1.0

    def test_partial_cycle(self):
        state = BatteryHealthState()
        record_charge(state, 5.0)
        record_discharge(state, 4.5)
        complete_cycle(state, "2026-03-21", battery_cap_kwh=20.0)
        assert state.total_cycles == 0.25  # 5/20

    def test_daily_records_kept(self):
        state = BatteryHealthState()
        for i in range(5):
            record_charge(state, 10.0)
            record_discharge(state, 9.0)
            complete_cycle(state, f"2026-03-{i + 1:02d}")
        assert len(state.daily_records) == 5

    def test_daily_records_max_90(self):
        state = BatteryHealthState()
        for i in range(100):
            record_charge(state, 10.0)
            record_discharge(state, 9.0)
            complete_cycle(state, f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}")
        assert len(state.daily_records) <= 90

    def test_temperature_affects_temp_efficiency(self):
        state = BatteryHealthState()
        for _ in range(20):
            record_charge(state, 10.0, -5.0)
            record_discharge(state, 7.0, -5.0)
            complete_cycle(state, "2026-01-01")
        # Cold temp should lower efficiency tracking
        assert state.temp_efficiency[0] < 0.85  # bin 0 = <0°C


class TestEfficiency:
    """Test efficiency tracking."""

    def test_ema_converges(self):
        state = BatteryHealthState()
        # Feed consistent 85% efficiency
        for i in range(50):
            record_charge(state, 10.0)
            record_discharge(state, 8.5)
            complete_cycle(state, f"2026-03-{(i % 28) + 1:02d}")
        # Should converge near 0.85
        assert 0.83 < state.roundtrip_efficiency < 0.87

    def test_efficiency_for_temperature(self):
        state = BatteryHealthState()
        # Default values
        eff = efficiency_for_temperature(state, 20.0)
        assert eff == 0.90  # Default (insufficient data)

    def test_efficiency_trend_insufficient(self):
        state = BatteryHealthState()
        assert efficiency_trend(state) == "insufficient_data"


class TestDegradation:
    """Test degradation estimation."""

    def test_fresh_battery(self):
        state = BatteryHealthState()
        pct = estimate_degradation(state)
        assert pct == 100.0

    def test_degradation_after_cycles(self):
        state = BatteryHealthState()
        state.total_cycles = 1000
        pct = estimate_degradation(state)
        assert 95 < pct < 100

    def test_heavily_cycled(self):
        state = BatteryHealthState()
        state.total_cycles = 6000
        pct = estimate_degradation(state)
        assert pct < 85


class TestMonthlySnapshot:
    """Test monthly efficiency snapshots."""

    def test_records_monthly(self):
        state = BatteryHealthState()
        record_monthly_snapshot(state, 3, 2026)
        assert len(state.monthly_efficiency) == 1
        assert state.monthly_efficiency[0]["month"] == 3

    def test_no_duplicate_month(self):
        state = BatteryHealthState()
        record_monthly_snapshot(state, 3, 2026)
        record_monthly_snapshot(state, 3, 2026)
        assert len(state.monthly_efficiency) == 1

    def test_max_24_months(self):
        state = BatteryHealthState()
        for i in range(30):
            record_monthly_snapshot(state, (i % 12) + 1, 2024 + i // 12)
        assert len(state.monthly_efficiency) <= 24


class TestHealthSummary:
    """Test health summary."""

    def test_summary_structure(self):
        state = BatteryHealthState()
        s = health_summary(state)
        assert "roundtrip_efficiency_pct" in s
        assert "total_cycles" in s
        assert "estimated_capacity_pct" in s
        assert "temp_efficiency" in s


class TestSerialization:
    """Test serialization."""

    def test_roundtrip(self):
        state = BatteryHealthState()
        record_charge(state, 10.0, 20.0)
        record_discharge(state, 9.0, 20.0)
        complete_cycle(state, "2026-03-21")
        state.total_cycles = 500

        data = state_to_dict(state)
        state2 = state_from_dict(data)

        assert state2.total_cycles == 500
        assert abs(state2.roundtrip_efficiency - state.roundtrip_efficiency) < 0.001
        assert len(state2.daily_records) == 1

    def test_from_empty_dict(self):
        state = state_from_dict({})
        assert state.roundtrip_efficiency == 0.90

    def test_from_none(self):
        state = state_from_dict(None)
        assert state.roundtrip_efficiency == 0.90
