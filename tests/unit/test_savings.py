"""Tests for savings calculator."""

from __future__ import annotations

from datetime import datetime

from custom_components.carmabox.optimizer.savings import (
    DailySavings,
    SavingsState,
    calculate_peak_savings,
    daily_trend,
    ellevio_peak_penalty,
    peak_comparison,
    record_cost_estimate,
    record_daily_snapshot,
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
    reset_savings,
    savings_breakdown,
    savings_whatif,
    state_from_dict,
    state_to_dict,
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

    def test_negative_savings_when_price_below_avg(self) -> None:
        """Discharge at cheap price = negative savings (loss)."""
        state = SavingsState(month=3, year=2026)
        record_discharge(state, 2.0, 50.0, 80.0)  # 2 kWh × (50-80)/100 = -0.6 kr
        assert abs(state.discharge_savings_kr - (-0.6)) < 0.01
        assert state.total_discharge_kwh == 2.0

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


class TestDailySnapshot:
    def test_creates_entry(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.discharge_savings_kr = 10.0
        state.grid_charge_savings_kr = 5.0
        state.peak_samples = [2.0, 2.0, 2.0]
        state.baseline_peak_samples = [4.0, 4.0, 4.0]
        record_daily_snapshot(state, "2026-03-15")
        assert len(state.daily_savings) == 1
        assert state.daily_savings[0].date == "2026-03-15"
        assert state.daily_savings[0].discharge_kr == 10.0

    def test_updates_same_date(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.discharge_savings_kr = 5.0
        record_daily_snapshot(state, "2026-03-15")
        state.discharge_savings_kr = 15.0
        record_daily_snapshot(state, "2026-03-15")
        assert len(state.daily_savings) == 1
        assert state.daily_savings[0].discharge_kr == 15.0

    def test_max_30_days(self) -> None:
        state = SavingsState(month=3, year=2026)
        for i in range(35):
            record_daily_snapshot(state, f"2026-03-{i + 1:02d}")
        assert len(state.daily_savings) == 30


class TestCostEstimate:
    def test_accumulates_costs(self) -> None:
        state = SavingsState(month=3, year=2026)
        # 2 kWh consumption, 100 öre, 1 kWh from battery
        record_cost_estimate(state, 2.0, 100.0, 1.0)
        # Without CARMA: 2 kWh × 1 kr = 2 kr
        assert abs(state.baseline_cost_kr - 2.0) < 0.01
        # With CARMA: (2-1) kWh × 1 kr = 1 kr
        assert abs(state.actual_cost_kr - 1.0) < 0.01

    def test_no_negative_grid(self) -> None:
        state = SavingsState(month=3, year=2026)
        # Battery discharge exceeds consumption
        record_cost_estimate(state, 1.0, 100.0, 2.0)
        assert state.actual_cost_kr == 0.0  # max(0, 1-2) = 0


class TestWhatIf:
    def test_whatif_with_data(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.baseline_cost_kr = 4000.0
        state.actual_cost_kr = 3500.0
        state.peak_samples = [2.0, 2.0, 2.0]
        state.baseline_peak_samples = [4.0, 4.0, 4.0]
        result = savings_whatif(state, cost_per_kw=80.0)
        assert result["without_carma_kr"] > result["with_carma_kr"]
        assert result["saved_kr"] > 0

    def test_whatif_empty_state(self) -> None:
        state = SavingsState(month=3, year=2026)
        result = savings_whatif(state)
        assert result["without_carma_kr"] == 0
        assert result["with_carma_kr"] == 0


class TestPeakComparison:
    def test_returns_peaks(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [2.1, 1.9, 1.8]
        state.baseline_peak_samples = [4.5, 3.8, 3.2]
        result = peak_comparison(state)
        assert result["actual"] == [2.1, 1.9, 1.8]
        assert result["baseline"] == [4.5, 3.8, 3.2]

    def test_empty_state(self) -> None:
        state = SavingsState(month=3, year=2026)
        result = peak_comparison(state)
        assert result["actual"] == []
        assert result["baseline"] == []


class TestDailyTrend:
    def test_returns_trend_data(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.daily_savings = [
            DailySavings(date="2026-03-14", total_kr=10.0),
            DailySavings(date="2026-03-15", total_kr=25.0),
        ]
        result = daily_trend(state)
        assert len(result) == 2
        assert result[0]["date"] == "2026-03-14"
        assert result[1]["total_kr"] == 25.0


class TestStateSerialization:
    def test_roundtrip(self) -> None:
        """Serialize and deserialize preserves all data."""
        state = SavingsState(
            month=3,
            year=2026,
            peak_samples=[4.0, 3.5, 2.5],
            baseline_peak_samples=[5.0, 4.5, 3.5],
            discharge_savings_kr=12.5,
            grid_charge_savings_kr=8.3,
            total_discharge_kwh=20.0,
            total_grid_charge_kwh=15.0,
            daily_savings=[
                DailySavings(
                    date="2026-03-14",
                    peak_kr=5.0,
                    discharge_kr=3.0,
                    grid_charge_kr=2.0,
                    total_kr=10.0,
                ),
                DailySavings(
                    date="2026-03-15",
                    peak_kr=8.0,
                    discharge_kr=4.0,
                    grid_charge_kr=3.0,
                    total_kr=15.0,
                ),
            ],
            baseline_cost_kr=4000.0,
            actual_cost_kr=3500.0,
        )
        data = state_to_dict(state)
        restored = state_from_dict(data)
        assert restored.month == 3
        assert restored.year == 2026
        assert restored.peak_samples == [4.0, 3.5, 2.5]
        assert restored.baseline_peak_samples == [5.0, 4.5, 3.5]
        assert restored.discharge_savings_kr == 12.5
        assert restored.grid_charge_savings_kr == 8.3
        assert restored.total_discharge_kwh == 20.0
        assert restored.total_grid_charge_kwh == 15.0
        assert len(restored.daily_savings) == 2
        assert restored.daily_savings[0].date == "2026-03-14"
        assert restored.daily_savings[0].peak_kr == 5.0
        assert restored.daily_savings[1].total_kr == 15.0
        assert restored.baseline_cost_kr == 4000.0
        assert restored.actual_cost_kr == 3500.0

    def test_from_dict_empty(self) -> None:
        """Empty dict returns fresh state."""
        result = state_from_dict({})
        assert result.month == 0
        assert result.discharge_savings_kr == 0.0
        assert result.daily_savings == []

    def test_from_dict_none(self) -> None:
        """None returns fresh state."""
        result = state_from_dict(None)
        assert result.month == 0

    def test_from_dict_invalid(self) -> None:
        """Invalid data returns fresh state."""
        result = state_from_dict({"month": "not_a_number"})
        assert result.month == 0

    def test_to_dict_keys(self) -> None:
        """state_to_dict includes all expected keys."""
        state = SavingsState(month=3, year=2026)
        data = state_to_dict(state)
        expected_keys = {
            "month",
            "year",
            "peak_samples",
            "baseline_peak_samples",
            "discharge_savings_kr",
            "grid_charge_savings_kr",
            "total_discharge_kwh",
            "total_grid_charge_kwh",
            "daily_savings",
            "baseline_cost_kr",
            "actual_cost_kr",
            "charge_from_grid_kwh",
            "charge_from_grid_cost_ore",
            "discharge_offset_kwh",
            "discharge_offset_value_ore",
            "grid_charge_prices",
        }
        assert set(data.keys()) == expected_keys

    def test_roundtrip_after_operations(self) -> None:
        """Roundtrip works after recording operations."""
        state = SavingsState(month=3, year=2026)
        record_peak(state, 2.0, 3.5)
        record_discharge(state, 1.5, 120.0, 80.0)
        record_grid_charge(state, 2.0, 10.0, 80.0)
        record_cost_estimate(state, 3.0, 100.0, 1.5)
        record_daily_snapshot(state, "2026-03-19")

        data = state_to_dict(state)
        restored = state_from_dict(data)

        assert restored.peak_samples == state.peak_samples
        assert restored.discharge_savings_kr == state.discharge_savings_kr
        assert restored.grid_charge_savings_kr == state.grid_charge_savings_kr
        assert restored.baseline_cost_kr == state.baseline_cost_kr
        assert restored.actual_cost_kr == state.actual_cost_kr
        assert len(restored.daily_savings) == len(state.daily_savings)
        assert restored.daily_savings[0].date == "2026-03-19"


class TestResetSavings:
    def test_returns_fresh_state(self) -> None:
        result = reset_savings()
        assert result.month == 0
        assert result.year == 0
        assert result.peak_samples == []
        assert result.baseline_peak_samples == []
        assert result.discharge_savings_kr == 0.0
        assert result.grid_charge_savings_kr == 0.0
        assert result.total_discharge_kwh == 0.0
        assert result.total_grid_charge_kwh == 0.0
        assert result.daily_savings == []
        assert result.baseline_cost_kr == 0.0
        assert result.actual_cost_kr == 0.0

    def test_independent_of_existing_state(self) -> None:
        """reset_savings always returns a clean state regardless of prior data."""
        # Even if some global state existed, reset_savings gives zeros
        state = reset_savings()
        assert state.charge_from_grid_kwh == 0.0
        assert state.grid_charge_prices == []


class TestEllevioPeakPenalty:
    def test_empty_samples(self) -> None:
        result = ellevio_peak_penalty([])
        assert result["actual_avg_kw"] == 0.0
        assert result["excess_kw"] == 0.0
        assert result["excess_cost_kr"] == 0.0
        assert result["peaks"] == []

    def test_under_target(self) -> None:
        """Peaks below target = no excess cost."""
        result = ellevio_peak_penalty([1.5, 1.2, 1.0], target_kw=2.0)
        assert result["excess_kw"] == 0.0
        assert result["excess_cost_kr"] == 0.0
        assert result["actual_avg_kw"] == round((1.5 + 1.2 + 1.0) / 3, 2)

    def test_over_target(self) -> None:
        """Peaks above target = excess cost calculated."""
        # Top 3: 4.0, 3.0, 2.5 → avg 3.167, excess 1.167, cost 1.167*80 = 93.3
        result = ellevio_peak_penalty([4.0, 3.0, 2.5, 1.0], target_kw=2.0, cost_per_kw=80.0)
        assert result["actual_avg_kw"] == round((4.0 + 3.0 + 2.5) / 3, 2)
        assert result["excess_kw"] == round((4.0 + 3.0 + 2.5) / 3 - 2.0, 2)
        assert result["excess_cost_kr"] > 0
        assert len(result["peaks"]) == 3
        assert result["peaks"] == [4.0, 3.0, 2.5]

    def test_exact_target(self) -> None:
        """Peaks exactly at target = no excess."""
        result = ellevio_peak_penalty([2.0, 2.0, 2.0], target_kw=2.0)
        assert result["excess_kw"] == 0.0
        assert result["excess_cost_kr"] == 0.0

    def test_fewer_than_top_n(self) -> None:
        """Fewer samples than top_n uses all available."""
        result = ellevio_peak_penalty([3.0], target_kw=2.0, cost_per_kw=80.0)
        assert result["actual_avg_kw"] == 3.0
        assert result["excess_kw"] == 1.0
        assert result["excess_cost_kr"] == 80.0
        assert result["peaks"] == [3.0]

    def test_custom_top_n(self) -> None:
        """Custom top_n takes different number of peaks."""
        result = ellevio_peak_penalty([5.0, 4.0, 3.0, 2.0, 1.0], target_kw=2.0, top_n=5)
        assert len(result["peaks"]) == 5
        assert result["actual_avg_kw"] == 3.0  # (5+4+3+2+1)/5

    def test_cost_calculation(self) -> None:
        """Verify exact cost calculation."""
        # Top 3: 4.0, 3.0, 2.0 → avg 3.0, excess 1.0, cost 1.0*80 = 80.0
        result = ellevio_peak_penalty([4.0, 3.0, 2.0], target_kw=2.0, cost_per_kw=80.0)
        assert result["excess_cost_kr"] == 80.0


class TestSavingsBreakdownEllevio:
    def test_includes_ellevio_excess_kr(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [3.0, 2.5, 2.0]  # avg 2.5, excess 0.5 over 2.0 target
        state.baseline_peak_samples = [5.0, 4.0, 3.0]
        bd = savings_breakdown(state, target_kw=2.0)
        assert "ellevio_excess_kr" in bd
        assert bd["ellevio_excess_kr"] == 40.0  # 0.5 kW * 80 kr/kW

    def test_no_excess_when_under_target(self) -> None:
        state = SavingsState(month=3, year=2026)
        state.peak_samples = [1.5, 1.2, 1.0]
        state.baseline_peak_samples = [3.0, 2.5, 2.0]
        bd = savings_breakdown(state, target_kw=2.0)
        assert bd["ellevio_excess_kr"] == 0.0
