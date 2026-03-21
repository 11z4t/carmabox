"""Tests for CARMA Box ROI calculator module (PLAT-963)."""

from custom_components.carmabox.optimizer.roi import (
    ROIState,
    monthly_trend,
    payback_months,
    payback_progress_pct,
    record_month,
    roi_summary,
    state_from_dict,
    state_to_dict,
    total_actual_cost,
    total_baseline_cost,
    total_investment,
    total_savings,
    whatif_summary,
)


class TestInvestment:
    """Test investment tracking."""

    def test_total_investment(self):
        state = ROIState(
            battery_cost_kr=50000,
            solar_cost_kr=80000,
            installation_cost_kr=15000,
        )
        assert total_investment(state) == 145000

    def test_zero_investment(self):
        state = ROIState()
        assert total_investment(state) == 0


class TestRecordMonth:
    """Test monthly savings recording."""

    def test_record_first_month(self):
        state = ROIState()
        record_month(state, 2026, 1, peak_savings_kr=200, discharge_savings_kr=100)
        assert len(state.monthly_savings) == 1
        assert state.monthly_savings[0].total_savings_kr == 300.0
        assert state.start_year == 2026
        assert state.start_month == 1

    def test_update_existing_month(self):
        state = ROIState()
        record_month(state, 2026, 1, peak_savings_kr=200)
        record_month(state, 2026, 1, peak_savings_kr=300)
        assert len(state.monthly_savings) == 1
        assert state.monthly_savings[0].peak_savings_kr == 300.0

    def test_multiple_months(self):
        state = ROIState()
        for m in range(1, 13):
            record_month(state, 2026, m, peak_savings_kr=100 + m * 10)
        assert len(state.monthly_savings) == 12

    def test_max_60_months(self):
        state = ROIState()
        for i in range(70):
            year = 2024 + i // 12
            month = (i % 12) + 1
            record_month(state, year, month, peak_savings_kr=100)
        assert len(state.monthly_savings) <= 60

    def test_with_baseline_cost(self):
        state = ROIState()
        record_month(state, 2026, 3, baseline_cost_kr=2000, actual_cost_kr=1500)
        assert state.monthly_savings[0].baseline_cost_kr == 2000
        assert state.monthly_savings[0].actual_cost_kr == 1500


class TestPayback:
    """Test payback calculations."""

    def test_no_investment(self):
        state = ROIState()
        assert payback_months(state) == 0

    def test_no_data(self):
        state = ROIState(battery_cost_kr=100000)
        assert payback_months(state) is None

    def test_payback_estimation(self):
        state = ROIState(battery_cost_kr=100000)
        for m in range(1, 13):
            record_month(state, 2026, m, peak_savings_kr=500, discharge_savings_kr=200)
        # 700/month savings, 100000 investment → ~143 months
        pb = payback_months(state)
        assert pb is not None
        assert 100 < pb < 200

    def test_payback_progress(self):
        state = ROIState(battery_cost_kr=10000)
        record_month(state, 2026, 1, peak_savings_kr=2500)
        assert payback_progress_pct(state) == 25.0

    def test_over_100_percent(self):
        state = ROIState(battery_cost_kr=1000)
        record_month(state, 2026, 1, peak_savings_kr=1500)
        assert payback_progress_pct(state) == 150.0


class TestTotalSavings:
    """Test savings accumulation."""

    def test_total_savings(self):
        state = ROIState()
        record_month(state, 2026, 1, peak_savings_kr=100, discharge_savings_kr=50)
        record_month(state, 2026, 2, peak_savings_kr=120, grid_charge_savings_kr=30)
        assert total_savings(state) == 300.0

    def test_baseline_vs_actual(self):
        state = ROIState()
        record_month(state, 2026, 1, baseline_cost_kr=3000, actual_cost_kr=2000)
        assert total_baseline_cost(state) == 3000
        assert total_actual_cost(state) == 2000


class TestMonthlyTrend:
    """Test monthly trend output."""

    def test_empty(self):
        state = ROIState()
        assert monthly_trend(state) == []

    def test_running_total(self):
        state = ROIState()
        for m in range(1, 4):
            record_month(state, 2026, m, peak_savings_kr=100)
        t = monthly_trend(state)
        assert len(t) == 3
        assert t[0]["running_total_kr"] == 100
        assert t[2]["running_total_kr"] == 300

    def test_last_n(self):
        state = ROIState()
        for m in range(1, 13):
            record_month(state, 2026, m, peak_savings_kr=100)
        t = monthly_trend(state, last_n=3)
        assert len(t) == 3


class TestWhatIf:
    """Test what-if comparison."""

    def test_basic(self):
        state = ROIState()
        record_month(
            state, 2026, 1, baseline_cost_kr=3000, actual_cost_kr=2000, peak_savings_kr=500
        )
        w = whatif_summary(state)
        assert w["total_without_carma_kr"] == 3000
        assert w["total_with_carma_kr"] == 2000

    def test_annualized_requires_3_months(self):
        state = ROIState()
        record_month(state, 2026, 1, peak_savings_kr=500)
        w = whatif_summary(state)
        assert w["annualized_savings_kr"] == 0  # < 3 months


class TestROISummary:
    """Test full ROI summary."""

    def test_summary_structure(self):
        state = ROIState(battery_cost_kr=100000)
        record_month(state, 2026, 1, peak_savings_kr=500)
        s = roi_summary(state)
        assert "total_investment_kr" in s
        assert "total_savings_kr" in s
        assert "payback_progress_pct" in s
        assert "whatif" in s


class TestSerialization:
    """Test serialization."""

    def test_roundtrip(self):
        state = ROIState(battery_cost_kr=100000, solar_cost_kr=80000)
        record_month(state, 2026, 1, peak_savings_kr=500)
        record_month(state, 2026, 2, discharge_savings_kr=300)

        data = state_to_dict(state)
        state2 = state_from_dict(data)

        assert state2.battery_cost_kr == 100000
        assert len(state2.monthly_savings) == 2
        assert state2.monthly_savings[0].peak_savings_kr == 500

    def test_from_empty(self):
        s = state_from_dict({})
        assert s.battery_cost_kr == 0

    def test_from_none(self):
        s = state_from_dict(None)
        assert s.battery_cost_kr == 0
