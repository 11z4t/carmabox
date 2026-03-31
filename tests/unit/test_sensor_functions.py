"""Coverage tests for sensor.py utility functions.

EXP-EPIC-SWEEP — targets sensor.py uncovered branches:
  Lines 61-63    — _plan_status_value: cold_lock + taper branches
  Lines 77       — _plan_status_attrs: plan data iteration
  Lines 156      — _decision_attrs: _pv_allocation branch
  Lines 199      — _plan_accuracy_value: both-near-zero path
  Lines 234-265  — _battery_efficiency_value / _attrs
  Lines 270-298  — _optimization_score_value / _attrs
  Lines 301-334  — _grid_charge_efficiency_value / _attrs
  Lines 337-361  — _ellevio_realtime_value / _attrs
"""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock

from pytest import approx as pytest_approx

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    Decision,
    HourActual,
    HourPlan,
    ShadowComparison,
)
from custom_components.carmabox.optimizer.savings import SavingsState
from custom_components.carmabox.sensor import (
    _battery_efficiency_attrs,
    _battery_efficiency_value,
    _decision_attrs,
    _ellevio_realtime_attrs,
    _ellevio_realtime_value,
    _grid_charge_efficiency_attrs,
    _grid_charge_efficiency_value,
    _optimization_score_attrs,
    _optimization_score_value,
    _plan_accuracy_attrs,
    _plan_accuracy_value,
    _plan_status_attrs,
    _plan_status_value,
)

# ── Minimal coord factory ────────────────────────────────────────────────────


def _coord(
    *,
    last_command: BatteryCommand = BatteryCommand.IDLE,
    savings: SavingsState | None = None,
    data: CarmaboxState | None = None,
    last_decision: Decision | None = None,
    decision_log: list | None = None,
    hourly_actuals: list | None = None,
    ellevio_samples: list | None = None,
    ellevio_peaks: list | None = None,
    pv_allocation: dict | None = None,
    shadow: ShadowComparison | None = None,
    shadow_log: list | None = None,
) -> CarmaboxCoordinator:
    coord = MagicMock(spec=CarmaboxCoordinator)
    coord._last_command = last_command
    coord.data = data
    coord.savings = savings or SavingsState(month=3, year=2026)
    coord.last_decision = last_decision or Decision()
    coord.decision_log = deque(decision_log or [], maxlen=48)
    coord.hourly_actuals = hourly_actuals or []
    coord._ellevio_hour_samples = ellevio_samples or []
    coord._ellevio_monthly_hourly_peaks = ellevio_peaks or []
    coord._pv_allocation = pv_allocation
    coord.shadow = shadow or ShadowComparison()
    coord.shadow_log = shadow_log or []
    coord._shadow_savings_kr = 0.0
    coord.target_kw = 2.0
    coord.min_soc = 15.0
    coord._cfg = {}
    coord._daily_avg_price = 80.0
    coord._taper_active = False
    coord._cold_lock_active = False
    coord.executor_enabled = True
    return coord


def _hour_plan(hour: int = 10) -> HourPlan:
    return HourPlan(
        hour=hour,
        action="c",
        battery_kw=2.0,
        grid_kw=0.5,
        weighted_kw=0.5,
        pv_kw=3.0,
        consumption_kw=1.0,
        ev_kw=0.0,
        ev_soc=50,
        battery_soc=80,
        price=85.0,
    )


def _hour_actual(
    planned_w: float = 2.0,
    actual_w: float = 2.1,
    hour: int = 10,
) -> HourActual:
    return HourActual(
        hour=hour,
        planned_weighted_kw=planned_w,
        actual_weighted_kw=actual_w,
    )


# ── _plan_status_value ───────────────────────────────────────────────────────


class TestPlanStatusValue:
    def test_cold_lock_branch(self) -> None:
        """BMS_COLD_LOCK → 'cold_lock'."""
        state = CarmaboxState(grid_power_w=100.0, battery_soc_1=50.0)
        coord = _coord(
            last_command=BatteryCommand.BMS_COLD_LOCK,
            data=state,
        )
        assert _plan_status_value(coord) == "cold_lock"

    def test_taper_branch(self) -> None:
        """CHARGE_PV_TAPER → 'charging_taper'."""
        state = CarmaboxState(grid_power_w=100.0, battery_soc_1=50.0)
        coord = _coord(
            last_command=BatteryCommand.CHARGE_PV_TAPER,
            data=state,
        )
        assert _plan_status_value(coord) == "charging_taper"


# ── _plan_status_attrs ───────────────────────────────────────────────────────


class TestPlanStatusAttrs:
    def test_with_plan_data(self) -> None:
        """State has plan entries → plan list serialized."""
        hp = _hour_plan(10)
        state = CarmaboxState(
            grid_power_w=200.0,
            battery_soc_1=70.0,
            plan=[hp],
        )
        coord = _coord(data=state)
        attrs = _plan_status_attrs(coord)

        assert attrs["plan_hours"] == 1
        assert attrs["plan"][0]["h"] == 10
        assert attrs["plan"][0]["a"] == "c"
        assert attrs["plan"][0]["soc"] == 80

    def test_empty_plan(self) -> None:
        """Empty plan → plan list empty, still returns attributes."""
        state = CarmaboxState(grid_power_w=200.0, battery_soc_1=70.0)
        coord = _coord(data=state)
        attrs = _plan_status_attrs(coord)

        assert attrs["plan_hours"] == 0
        assert attrs["plan"] == []


# ── _decision_attrs ──────────────────────────────────────────────────────────


class TestDecisionAttrs:
    def test_pv_allocation_branch(self) -> None:
        """_pv_allocation present → included in attrs."""
        coord = _coord(pv_allocation={"10": 1.5, "11": 2.0})
        attrs = _decision_attrs(coord)
        assert "pv_allocation" in attrs
        assert attrs["pv_allocation"]["10"] == 1.5

    def test_no_pv_allocation(self) -> None:
        """_pv_allocation absent → key not in attrs."""
        coord = _coord(pv_allocation=None)
        attrs = _decision_attrs(coord)
        assert "pv_allocation" not in attrs


# ── _plan_accuracy_value ─────────────────────────────────────────────────────


class TestPlanAccuracyValue:
    def test_both_near_zero_counts_as_100(self) -> None:
        """Both planned and actual near zero → score = 100%."""
        actuals = [
            _hour_actual(planned_w=0.0, actual_w=0.0),
            _hour_actual(planned_w=0.005, actual_w=0.003),
        ]
        coord = _coord(hourly_actuals=actuals)
        result = _plan_accuracy_value(coord)
        assert result == 100.0

    def test_normal_accuracy(self) -> None:
        """plan=2.0, actual=2.5 → accuracy = 80%."""
        actuals = [
            _hour_actual(planned_w=2.0, actual_w=2.5),
            _hour_actual(planned_w=2.0, actual_w=2.0),
        ]
        coord = _coord(hourly_actuals=actuals)
        result = _plan_accuracy_value(coord)
        assert result is not None
        assert 80 < result <= 100

    def test_less_than_2_actuals_returns_none(self) -> None:
        """Fewer than 2 actuals → None."""
        coord = _coord(hourly_actuals=[_hour_actual()])
        assert _plan_accuracy_value(coord) is None


# ── _plan_accuracy_attrs ─────────────────────────────────────────────────────


class TestPlanAccuracyAttrs:
    def test_returns_history_list(self) -> None:
        """hourly_actuals → history with correct structure."""
        actuals = [_hour_actual(planned_w=2.0, actual_w=2.1, hour=h) for h in range(5)]
        coord = _coord(hourly_actuals=actuals)
        attrs = _plan_accuracy_attrs(coord)
        assert attrs["hours_tracked"] == 5
        assert len(attrs["history"]) == 5
        assert "h" in attrs["history"][0]


# ── _battery_efficiency_value ────────────────────────────────────────────────


class TestBatteryEfficiencyValue:
    def test_returns_ratio_when_data_present(self) -> None:
        """Sufficient data → returns ratio > 0."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 10.0
        s.charge_from_grid_cost_ore = 800.0  # avg buy = 80 öre
        s.discharge_offset_kwh = 10.0
        s.discharge_offset_value_ore = 1200.0  # avg sell = 120 öre
        coord = _coord(savings=s)
        result = _battery_efficiency_value(coord)
        assert result is not None
        assert result == pytest_approx(1.5, abs=0.1)

    def test_returns_none_when_no_charge(self) -> None:
        """charge_from_grid_kwh = 0 → None."""
        coord = _coord()
        assert _battery_efficiency_value(coord) is None

    def test_returns_none_when_no_discharge(self) -> None:
        """discharge_offset_kwh = 0 → None."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 5.0
        s.charge_from_grid_cost_ore = 400.0
        coord = _coord(savings=s)
        assert _battery_efficiency_value(coord) is None


# ── _battery_efficiency_attrs ────────────────────────────────────────────────


class TestBatteryEfficiencyAttrs:
    def test_with_data(self) -> None:
        """Returns complete attrs including ratio, summary."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 10.0
        s.charge_from_grid_cost_ore = 800.0
        s.discharge_offset_kwh = 10.0
        s.discharge_offset_value_ore = 1200.0
        coord = _coord(savings=s)
        attrs = _battery_efficiency_attrs(coord)
        assert "avg_buy_price_ore" in attrs
        assert "ratio" in attrs
        assert attrs["ratio"] == pytest_approx(1.5, abs=0.1)
        assert "summary" in attrs
        assert "öre" in attrs["summary"]

    def test_without_data(self) -> None:
        """No charge data → zeros, Ingen data summary."""
        coord = _coord()
        attrs = _battery_efficiency_attrs(coord)
        assert attrs["ratio"] == 0.0
        assert attrs["summary"] == "Ingen data"


# ── _optimization_score_value ────────────────────────────────────────────────


class TestOptimizationScoreValue:
    def test_returns_none_when_insufficient_samples(self) -> None:
        """Less than 3 samples → None."""
        coord = _coord()
        assert _optimization_score_value(coord) is None

    def test_returns_reduction_pct(self) -> None:
        """CARMA < baseline → positive score."""
        s = SavingsState(month=3, year=2026)
        s.baseline_peak_samples = [3.0, 2.8, 2.6, 2.4]
        s.peak_samples = [2.0, 1.8, 1.6, 1.4]
        coord = _coord(savings=s)
        score = _optimization_score_value(coord)
        assert score is not None
        assert score > 0


# ── _optimization_score_attrs ────────────────────────────────────────────────


class TestOptimizationScoreAttrs:
    def test_returns_complete_structure(self) -> None:
        """Returns native/carma top3 averages and saved kr."""
        s = SavingsState(month=3, year=2026)
        s.baseline_peak_samples = [3.0, 2.8, 2.6]
        s.peak_samples = [2.0, 1.8, 1.6]
        coord = _coord(savings=s)
        coord._cfg = {"peak_cost_per_kw": 80.0}
        attrs = _optimization_score_attrs(coord)
        assert "native_top3_avg_kw" in attrs
        assert "carma_top3_avg_kw" in attrs
        assert "saved_kr" in attrs
        assert attrs["saved_kr"] > 0

    def test_empty_samples(self) -> None:
        """No samples → zeroes."""
        coord = _coord()
        coord._cfg = {}
        attrs = _optimization_score_attrs(coord)
        assert attrs["native_top3_avg_kw"] == 0.0
        assert attrs["carma_top3_avg_kw"] == 0.0


# ── _grid_charge_efficiency_value ────────────────────────────────────────────


class TestGridChargeEfficiencyValue:
    def test_returns_none_when_no_charge(self) -> None:
        """charge_from_grid_kwh = 0 → None."""
        coord = _coord()
        assert _grid_charge_efficiency_value(coord) is None

    def test_returns_none_when_avg_daily_zero(self) -> None:
        """daily avg price = 0 → None."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 5.0
        s.charge_from_grid_cost_ore = 400.0
        coord = _coord(savings=s)
        coord._daily_avg_price = 0.0
        assert _grid_charge_efficiency_value(coord) is None

    def test_returns_efficiency_pct(self) -> None:
        """Charged at 50 öre vs avg 80 öre → 37% discount."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 10.0
        s.charge_from_grid_cost_ore = 500.0  # avg 50 öre
        coord = _coord(savings=s)
        coord._daily_avg_price = 80.0
        result = _grid_charge_efficiency_value(coord)
        assert result is not None
        assert result > 0


# ── _grid_charge_efficiency_attrs ────────────────────────────────────────────


class TestGridChargeEfficiencyAttrs:
    def test_with_charge_data(self) -> None:
        """Returns avg prices, summary, and price min/max."""
        s = SavingsState(month=3, year=2026)
        s.charge_from_grid_kwh = 10.0
        s.charge_from_grid_cost_ore = 500.0
        s.grid_charge_prices = [40.0, 55.0, 60.0]
        coord = _coord(savings=s)
        coord._daily_avg_price = 80.0
        attrs = _grid_charge_efficiency_attrs(coord)
        assert "avg_charge_price_ore" in attrs
        assert "avg_daily_price_ore" in attrs
        assert "price_min" in attrs
        assert attrs["price_min"] == 40.0
        assert attrs["price_max"] == 60.0

    def test_no_charge_data(self) -> None:
        """No charge data → Ingen data summary."""
        coord = _coord()
        coord._daily_avg_price = 80.0
        attrs = _grid_charge_efficiency_attrs(coord)
        assert "Ingen data" in attrs["summary"]
        assert attrs["price_min"] == 0.0
        assert attrs["price_max"] == 0.0


# ── _ellevio_realtime_value ──────────────────────────────────────────────────


class TestEllevioRealtimeValue:
    def test_returns_none_when_no_samples(self) -> None:
        """No samples → None."""
        coord = _coord(ellevio_samples=[])
        assert _ellevio_realtime_value(coord) is None

    def test_weighted_average(self) -> None:
        """Samples with weights → weighted average."""
        # [(power, weight), ...]
        coord = _coord(ellevio_samples=[(2.0, 1.0), (4.0, 1.0)])
        result = _ellevio_realtime_value(coord)
        assert result == 3.0


# ── _ellevio_realtime_attrs ──────────────────────────────────────────────────


class TestEllevioRealtimeAttrs:
    def test_with_three_peaks(self) -> None:
        """Three peaks → top1/2/3 populated."""
        coord = _coord(ellevio_peaks=[3.5, 2.8, 2.1, 1.5])
        coord._cfg = {}
        attrs = _ellevio_realtime_attrs(coord)
        assert attrs["top1_kw"] == 3.5
        assert attrs["top2_kw"] == 2.8
        assert attrs["top3_kw"] == 2.1
        assert attrs["top3_avg_kw"] == pytest_approx((3.5 + 2.8 + 2.1) / 3, abs=0.01)

    def test_with_no_peaks(self) -> None:
        """No peaks → zeroes."""
        coord = _coord(ellevio_peaks=[])
        coord._cfg = {}
        attrs = _ellevio_realtime_attrs(coord)
        assert attrs["top1_kw"] == 0.0
        assert attrs["top3_avg_kw"] == 0.0
        assert attrs["total_hours_tracked"] == 0

    def test_with_one_peak(self) -> None:
        """One peak → top1 set, top2/3 zero."""
        coord = _coord(ellevio_peaks=[2.5])
        coord._cfg = {"peak_cost_per_kw": 80.0}
        attrs = _ellevio_realtime_attrs(coord)
        assert attrs["top1_kw"] == 2.5
        assert attrs["top2_kw"] == 0.0


