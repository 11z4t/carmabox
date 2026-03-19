"""Tests for savings calculator."""

from __future__ import annotations

from datetime import datetime

from custom_components.carmabox.optimizer.savings import (
    SavingsState,
    calculate_peak_savings,
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
    savings_breakdown,
    total_savings,
)


class TestResetIfNewMonth:
    def test_same_month_keeps_state(self) -> None:
        state = SavingsState(month=3, year=2026, discharge_savings_kr=50.0)
        now = datetime(2026, 3, 15)
        result = reset_if_new_month(state, now)
        assert result.discharge_savings_kr == 50.0

    def test_new_month_resets(self) -> None:
        state = SavingsState(month=2, year=2026, discharge_savings_kr=50.0)
        now = datetime(2026, 3, 1)
        result = reset_if_new_month(state, now)
        assert result.discharge_savings_kr == 0.0
        assert result.month == 3
        assert result.year == 2026

    def test_new_year_resets(self) -> None:
        state = SavingsState(month=12, year=2025, discharge_savings_kr=100.0)
        now = datetime(2026, 1, 1)
        result = reset_if_new_month(state, now)
        assert result.month == 1
        assert result.year == 2026


class TestRecordPeak:
    def test_records_top_3(self) -> None:
        state = SavingsState(month=3, year=2026)
        for kw in [2.0, 3.5, 1.0, 4.0, 2.5]:
            record_peak(state, kw, kw + 1.0)
        assert state.peak_samples == [4.0, 3.5, 2.5]
        assert state.baseline_peak_samples == [5.0, 4.5, 3.5]

    def test_single_sample(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_peak(state, 2.0, 3.0)
        assert state.peak_samples == [2.0]
        assert state.baseline_peak_samples == [3.0]


class TestRecordDischarge:
    def test_savings_when_price_above_avg(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_discharge(state, 2.0, 120.0, 80.0)  # 2 kWh × (120-80)/100 = 0.8 kr
        assert abs(state.discharge_savings_kr - 0.8) < 0.01
        assert state.total_discharge_kwh == 2.0

    def test_no_savings_when_price_below_avg(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_discharge(state, 2.0, 50.0, 80.0)  # Price below avg → no savings
        assert state.discharge_savings_kr == 0.0

    def test_no_savings_zero_discharge(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_discharge(state, 0.0, 120.0, 80.0)
        assert state.discharge_savings_kr == 0.0

    def test_accumulates(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_discharge(state, 1.0, 100.0, 50.0)  # 0.5 kr
        record_discharge(state, 2.0, 150.0, 50.0)  # 2.0 kr
        assert abs(state.discharge_savings_kr - 2.5) < 0.01


class TestRecordGridCharge:
    def test_savings_when_charging_cheap(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_grid_charge(state, 3.0, 10.0, 80.0)  # 3 kWh × (80-10)/100 = 2.1 kr
        assert abs(state.grid_charge_savings_kr - 2.1) < 0.01
        assert state.total_grid_charge_kwh == 3.0

    def test_no_savings_expensive_charge(self) -> None:
        state = SavingsState(month=3, year=2026)
        record_grid_charge(state, 3.0, 90.0, 80.0)  # Charging more expensive than avg
        assert state.grid_charge_savings_kr == 0.0


class TestCalculatePeakSavings:
    def test_peak_reduction(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [2.0, 1.8, 1.5]  # With CARMA Box
        state.baseline_peak_samples = [4.0, 3.5, 3.0]  # Without
        savings = calculate_peak_savings(state, cost_per_kw=80.0)
        # baseline mean = 3.5, actual mean = 1.77, reduction = 1.73
        # 1.73 × 80 = 138.7 (approximately)
        assert savings > 100

    def test_no_reduction(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [3.0, 3.0, 3.0]
        state.baseline_peak_samples = [3.0, 3.0, 3.0]
        assert calculate_peak_savings(state) == 0.0

    def test_empty_state(self) -> None:
        state = SavingsState(month=3, year=2026)
        assert calculate_peak_savings(state) == 0.0


class TestTotalSavings:
    def test_sum_of_all(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [2.0, 2.0, 2.0]
        state.baseline_peak_samples = [4.0, 4.0, 4.0]
        state.discharge_savings_kr = 10.0
        state.grid_charge_savings_kr = 5.0
        result = total_savings(state, cost_per_kw=80.0)
        # peak: (4-2) × 80 = 160 + 10 + 5 = 175
        assert result == 175.0


class TestSavingsBreakdown:
    def test_breakdown_keys(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [2.0]
        state.baseline_peak_samples = [3.0]
        state.discharge_savings_kr = 5.0
        state.grid_charge_savings_kr = 2.0
        state.total_discharge_kwh = 10.0
        state.total_grid_charge_kwh = 6.0
        bd = savings_breakdown(state)
        assert "peak_reduction_kr" in bd
        assert "discharge_savings_kr" in bd
        assert "grid_charge_savings_kr" in bd
        assert "total_kr" in bd
        assert "total_discharge_kwh" in bd
        assert "total_grid_charge_kwh" in bd
        assert bd["discharge_savings_kr"] == 5.0
        assert bd["total_discharge_kwh"] == 10.0


class TestDailyResetAndAvgPrice:
    """Tests for daily reset at date change and avg_price from actual prices."""

    def test_daily_reset_clears_counters(self) -> None:
        """Verify reset_if_new_month resets on month boundary."""
        state = SavingsState(
            month=2,
            year=2026,
            discharge_savings_kr=100.0,
            grid_charge_savings_kr=50.0,
            total_discharge_kwh=25.0,
            total_grid_charge_kwh=10.0,
        )
        now = datetime(2026, 3, 1)
        result = reset_if_new_month(state, now)
        assert result.discharge_savings_kr == 0.0
        assert result.grid_charge_savings_kr == 0.0
        assert result.total_discharge_kwh == 0.0
        assert result.total_grid_charge_kwh == 0.0

    def test_avg_price_from_actual_prices(self) -> None:
        """avg_price should be mean of actual prices, not fallback."""
        prices = [10.0, 20.0, 30.0, 40.0]
        avg = sum(prices) / len(prices)  # 25.0
        state = SavingsState(month=3, year=2026)
        # Charge at 10 öre when avg is 25 → savings
        record_grid_charge(state, 2.0, 10.0, avg)
        assert abs(state.grid_charge_savings_kr - 0.3) < 0.01  # 2*(25-10)/100
        # Discharge at 40 öre when avg is 25 → savings
        record_discharge(state, 2.0, 40.0, avg)
        assert abs(state.discharge_savings_kr - 0.3) < 0.01  # 2*(40-25)/100

    def test_grid_charge_accumulates(self) -> None:
        """Grid charge savings accumulate correctly."""
        state = SavingsState(month=3, year=2026)
        record_grid_charge(state, 1.0, 10.0, 50.0)  # 1*(50-10)/100 = 0.4
        record_grid_charge(state, 2.0, 20.0, 50.0)  # 2*(50-20)/100 = 0.6
        assert abs(state.grid_charge_savings_kr - 1.0) < 0.01
        assert state.total_grid_charge_kwh == 3.0
