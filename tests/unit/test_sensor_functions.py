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


# ── Additional sensor.py functions (sensor.py lines 366-703) ─────────────────

# Imported locally since they weren't in the original import block
from custom_components.carmabox.sensor import (  # noqa: E402
    _daily_insight_value,
    _energy_ledger_attrs,
    _energy_ledger_value,
    _household_insights_attrs,
    _household_insights_value,
    _rules_attrs,
    _rules_value,
    _scheduler_24h_plan_attrs,
    _scheduler_24h_plan_value,
    _scheduler_breach_count_value,
    _scheduler_ev_full_charge_value,
    _scheduler_last_breach_attrs,
    _scheduler_last_breach_value,
    _shadow_attrs,
    _shadow_value,
)


def _shadow(*, timestamp: str = "", agreement: bool = True, reason: str = "") -> ShadowComparison:
    s = ShadowComparison()
    s.timestamp = timestamp
    s.agreement = agreement
    s.reason = reason
    s.carma_action = "idle"
    s.actual_action = "idle"
    s.carma_weighted_kw = 1.5
    s.actual_weighted_kw = 1.5
    s.carma_better_kr = 0.0
    s.price_ore = 80.0
    return s


def _sched_slot(hour: int = 10, action: str = "c", constraint_ok: bool = True) -> MagicMock:
    s = MagicMock()
    s.hour = hour
    s.action = action
    s.battery_kw = 2.0
    s.ev_kw = 0.0
    s.ev_amps = 0
    s.miner_on = False
    s.grid_kw = 0.5
    s.weighted_kw = 0.5
    s.pv_kw = 3.0
    s.consumption_kw = 1.0
    s.price = 85.0
    s.battery_soc = 80
    s.ev_soc = 50
    s.constraint_ok = constraint_ok
    s.reasoning = "Test"
    return s


def _breach_record() -> MagicMock:
    b = MagicMock()
    b.timestamp = "2026-03-31T10:00:00"
    b.hour = 10
    b.actual_weighted_kw = 3.0
    b.target_kw = 2.0
    b.loads_active = ["ev", "miner"]
    b.root_cause = "EV + miner running simultaneously"
    b.remediation = "Schedule EV off-peak"
    b.severity = "high"
    return b


def _sched_plan(*, breaches: list | None = None, slots: list | None = None) -> MagicMock:
    p = MagicMock()
    p.breaches = breaches or []
    p.breach_count_month = len(p.breaches)
    p.learnings = []
    p.slots = slots or []
    p.target_weighted_kw = 2.0
    p.max_weighted_kw = 2.5
    p.total_ev_kwh = 20.0
    p.ev_soc_at_06 = 75
    p.total_charge_kwh = 5.0
    p.total_discharge_kwh = 3.0
    p.estimated_cost_kr = 12.5
    p.ev_next_full_charge_date = "2026-04-01"
    return p


# ── _shadow_value ─────────────────────────────────────────────────────────────


class TestShadowValue:
    def test_no_timestamp_returns_ingen_data(self) -> None:
        """No timestamp → 'Ingen data'."""
        coord = _coord(shadow=_shadow(timestamp=""))
        assert _shadow_value(coord) == "Ingen data"

    def test_agreement_shows_action(self) -> None:
        """Agreement → 'Eniga: <action>'."""
        s = _shadow(timestamp="2026-03-31T10:00:00", agreement=True)
        s.carma_action = "charge_pv"
        coord = _coord(shadow=s)
        assert _shadow_value(coord) == "Eniga: charge_pv"

    def test_disagreement_with_reason(self) -> None:
        """Disagreement + reason → reason returned."""
        s = _shadow(timestamp="2026-03-31T10:00:00", agreement=False, reason="v6 laddar ur")
        coord = _coord(shadow=s)
        assert _shadow_value(coord) == "v6 laddar ur"

    def test_disagreement_no_reason_fallback(self) -> None:
        """Disagreement without reason → 'CARMA: X, v6: Y' format."""
        s = _shadow(timestamp="2026-03-31T10:00:00", agreement=False, reason="")
        s.carma_action = "idle"
        s.actual_action = "discharge"
        coord = _coord(shadow=s)
        result = _shadow_value(coord)
        assert "CARMA:" in result and "idle" in result


# ── _shadow_attrs ─────────────────────────────────────────────────────────────


class TestShadowAttrs:
    def test_returns_complete_attrs(self) -> None:
        """Returns all required keys including agreement_pct_24h."""
        s1 = _shadow(timestamp="T", agreement=True)
        s2 = _shadow(timestamp="T2", agreement=False, reason="Test")
        s2.timestamp = "2026-03-31T10:00:00"
        coord = _coord(shadow=s1, shadow_log=[s1, s2])
        coord._shadow_savings_kr = 2.5
        attrs = _shadow_attrs(coord)
        assert "agreement_pct_24h" in attrs
        assert attrs["agreement_pct_24h"] == 50.0

    def test_empty_log(self) -> None:
        """Empty log → agreement_pct = 0."""
        coord = _coord(shadow=_shadow(), shadow_log=[])
        attrs = _shadow_attrs(coord)
        assert attrs["agreement_pct_24h"] == 0


# ── _energy_ledger_value ──────────────────────────────────────────────────────


class TestEnergyLedgerValue:
    def test_returns_formatted_string(self) -> None:
        """Returns 'X.X kr sparat, Y.Y kr total (Zh)'."""
        ledger = MagicMock()
        ledger.daily_summary = MagicMock(
            return_value={"battery_net_saving_kr": 5.3, "total_cost_kr": 12.1, "hours": 8}
        )
        coord = _coord()
        coord.ledger = ledger
        result = _energy_ledger_value(coord)
        assert "5.3 kr sparat" in result
        assert "12.1 kr total" in result
        assert "8h" in result


# ── _energy_ledger_attrs ──────────────────────────────────────────────────────


class TestEnergyLedgerAttrs:
    def test_returns_daily_summary_dict(self) -> None:
        """Returns ledger.daily_summary result."""
        summary = {"battery_net_saving_kr": 5.3, "total_cost_kr": 12.1, "hours": 8}
        ledger = MagicMock()
        ledger.daily_summary = MagicMock(return_value=summary)
        coord = _coord()
        coord.ledger = ledger
        result = _energy_ledger_attrs(coord)
        assert result == summary


# ── _daily_insight_value ──────────────────────────────────────────────────────


class TestDailyInsightValue:
    def test_collecting_returns_samlar_data(self) -> None:
        """status=collecting → 'Samlar data'."""
        coord = _coord()
        coord.daily_insight = MagicMock(return_value={"status": "collecting"})
        # daily_insight is a property, override via mock attr
        type(coord).daily_insight = property(lambda self: {"status": "collecting"})
        result = _daily_insight_value(coord)
        assert result == "Samlar data"

    def test_ready_returns_summary(self) -> None:
        """status=ready → summary string with max_kw, cost, recs."""
        coord = _coord()
        type(coord).daily_insight = property(
            lambda self: {
                "status": "ready",
                "recommendation_count": 2,
                "ellevio_max_kw": 3.5,
                "total_cost_kr": 45.0,
            }
        )
        result = _daily_insight_value(coord)
        assert "3.5" in result
        assert "45" in result
        assert "2" in result


# ── _household_insights_value ────────────────────────────────────────────────


class TestHouseholdInsightsValue:
    def test_no_data_returns_samlar_data(self) -> None:
        """No benchmark_data → 'Samlar data'."""
        coord = _coord()
        coord.benchmark_data = None
        assert _household_insights_value(coord) == "Samlar data"

    def test_below_10_households_returns_samlar(self) -> None:
        """similar_households < 10 → 'Samlar data'."""
        coord = _coord()
        coord.benchmark_data = {"similar_households": 5}
        assert _household_insights_value(coord) == "Samlar data"

    def test_below_average(self) -> None:
        """diff_pct < -5 → 'X% under snittet'."""
        coord = _coord()
        coord.benchmark_data = {"similar_households": 50, "diff_pct": -12.0}
        assert "under snittet" in _household_insights_value(coord)

    def test_above_average(self) -> None:
        """diff_pct > 5 → 'X% över snittet'."""
        coord = _coord()
        coord.benchmark_data = {"similar_households": 50, "diff_pct": 10.0}
        assert "över snittet" in _household_insights_value(coord)

    def test_near_average(self) -> None:
        """diff_pct in [-5, 5] → 'Nära snittet'."""
        coord = _coord()
        coord.benchmark_data = {"similar_households": 50, "diff_pct": 2.0}
        assert _household_insights_value(coord) == "Nära snittet"


# ── _household_insights_attrs ─────────────────────────────────────────────────


class TestHouseholdInsightsAttrs:
    def test_no_data_returns_waiting(self) -> None:
        """No benchmark_data → waiting status."""
        coord = _coord()
        coord.benchmark_data = None
        attrs = _household_insights_attrs(coord)
        assert attrs["status"] == "waiting"

    def test_with_data_returns_full_attrs(self) -> None:
        """With benchmark_data → returns structured attrs."""
        coord = _coord()
        coord.benchmark_data = {
            "similar_households": 50,
            "comparison_group": "villa_10-20kwp",
            "your_monthly_kwh": 350,
            "avg_monthly_kwh": 400,
            "diff_pct": -12.5,
            "trend_3m": "improving",
            "your_savings_kr": 250,
            "avg_savings_kr": 180,
            "savings_rank_pct": 75,
            "tips": ["Charge EV off-peak"],
            "battery_roi_months": 84,
            "solar_roi_months": 120,
            "updated": "2026-03-31",
        }
        attrs = _household_insights_attrs(coord)
        assert attrs["similar_households"] == 50
        assert attrs["diff_pct"] == -12.5


# ── _rules_value ──────────────────────────────────────────────────────────────


class TestRulesValue:
    def test_no_active_rule(self) -> None:
        """_active_rule_id = '' → 'Ingen aktiv regel'."""
        coord = _coord()
        coord._active_rule_id = ""
        assert _rules_value(coord) == "Ingen aktiv regel"

    def test_known_rule(self) -> None:
        """RULE_0_5 → 'Solar charge'."""
        coord = _coord()
        coord._active_rule_id = "RULE_0_5"
        assert _rules_value(coord) == "Solar charge"

    def test_unknown_rule_returns_id(self) -> None:
        """Unknown rule ID → returns the ID itself."""
        coord = _coord()
        coord._active_rule_id = "RULE_9999"
        assert _rules_value(coord) == "RULE_9999"


# ── _rules_attrs ───────────────────────────────────────────────────────────────


class TestRulesAttrs:
    def test_no_data_returns_no_data_status(self) -> None:
        """coord.data = None → status: no_data."""
        coord = _coord()
        coord.data = None
        attrs = _rules_attrs(coord)
        assert attrs == {"status": "no_data"}

    def test_with_data_returns_all_rules(self) -> None:
        """coord.data set → returns list of 7 rules."""
        coord = _coord()
        coord.data = CarmaboxState()
        coord._active_rule_id = "RULE_2"
        coord._rule_triggers = {}
        attrs = _rules_attrs(coord)
        assert "rules" in attrs
        assert len(attrs["rules"]) == 7
        # Active rule is marked
        rule_2 = next(r for r in attrs["rules"] if r["id"] == "RULE_2")
        assert rule_2["active"] is True


# ── Scheduler sensor functions ────────────────────────────────────────────────


class TestSchedulerLastBreachValue:
    def test_no_breaches_returns_no_overträdelser(self) -> None:
        """No breaches → 'Inga överträdelser'."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        assert _scheduler_last_breach_value(coord) == "Inga överträdelser"

    def test_with_breach(self) -> None:
        """Has breach → root_cause[:200]."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan(breaches=[_breach_record()])
        result = _scheduler_last_breach_value(coord)
        assert "EV" in result


class TestSchedulerLastBreachAttrs:
    def test_no_breaches(self) -> None:
        """No breaches → breach_count_month key present."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        attrs = _scheduler_last_breach_attrs(coord)
        assert "breach_count_month" in attrs

    def test_with_breach_returns_details(self) -> None:
        """Breach exists → full details including severity."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan(breaches=[_breach_record()])
        attrs = _scheduler_last_breach_attrs(coord)
        assert attrs["hour"] == 10
        assert attrs["severity"] == "high"


class TestSchedulerBreachCountValue:
    def test_returns_count(self) -> None:
        """Returns breach_count_month from scheduler_plan."""
        coord = _coord()
        plan = _sched_plan(breaches=[_breach_record()])
        plan.breach_count_month = 5
        coord.scheduler_plan = plan
        assert _scheduler_breach_count_value(coord) == 5


class TestScheduler24hPlanValue:
    def test_no_slots_returns_ingen_plan(self) -> None:
        """No slots → 'Ingen plan'."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        assert _scheduler_24h_plan_value(coord) == "Ingen plan"

    def test_slots_all_ok(self) -> None:
        """All slots ok → 'Plan aktiv — alla timmar OK'."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan(slots=[_sched_slot()])
        assert "OK" in _scheduler_24h_plan_value(coord)

    def test_slots_with_violations(self) -> None:
        """Some slots not ok → 'varning(ar)' in result."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan(slots=[_sched_slot(constraint_ok=False)])
        result = _scheduler_24h_plan_value(coord)
        assert "varning" in result


class TestScheduler24hPlanAttrs:
    def test_returns_plan_structure(self) -> None:
        """Returns slots data with all fields."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan(slots=[_sched_slot(hour=10)])
        attrs = _scheduler_24h_plan_attrs(coord)
        assert len(attrs["slots"]) == 1
        assert attrs["slots"][0]["h"] == 10

    def test_empty_slots(self) -> None:
        """No slots → empty slots list."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        attrs = _scheduler_24h_plan_attrs(coord)
        assert attrs["slots"] == []


class TestSchedulerEvFullChargeValue:
    def test_returns_date(self) -> None:
        """date set → returns it."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        coord.scheduler_plan.ev_next_full_charge_date = "2026-04-05"
        assert _scheduler_ev_full_charge_value(coord) == "2026-04-05"

    def test_no_date_returns_ej_planerad(self) -> None:
        """date = None → 'Ej planerad'."""
        coord = _coord()
        coord.scheduler_plan = _sched_plan()
        coord.scheduler_plan.ev_next_full_charge_date = None
        assert _scheduler_ev_full_charge_value(coord) == "Ej planerad"
