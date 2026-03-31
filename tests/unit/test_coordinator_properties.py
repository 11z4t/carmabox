"""Coverage tests for CarmaboxCoordinator computed properties.

EXP-EPIC-SWEEP — targets coordinator.py property clusters:
  Lines 4744-4795  — system_health: inverter/EV/safety branches
  Lines 4797-4816  — status_text: issue text branches
  Lines 4818-4880  — plan_score: 7d/30d/trend paths
  Lines 4883-5054  — daily_insight: Ellevio analysis + recommendations
  Lines 5056-5098  — _analyze_hour: worst/best label branches
  Lines 5101-5232  — rule_flow: pv/discharge/idle path nodes
"""

from __future__ import annotations

from collections import deque

from custom_components.carmabox.optimizer.models import Decision
from custom_components.carmabox.optimizer.savings import DailySavings
from tests.unit.test_expert_control import _make_coord

# ── Helpers ──────────────────────────────────────────────────────────────────


def _decision(
    *,
    action: str = "idle",
    hour: int = 10,
    price_ore: float = 80.0,
    grid_kw: float = 1.5,
    weighted_kw: float = 1.5,
    battery_soc: float = 60.0,
    pv_kw: float = 0.0,
    discharge_w: int = 0,
    reason: str = "Test",
) -> Decision:
    ts = f"2026-03-31T{hour:02d}:00:00"
    return Decision(
        timestamp=ts,
        action=action,
        reason=reason,
        price_ore=price_ore,
        grid_kw=grid_kw,
        weighted_kw=weighted_kw,
        battery_soc=battery_soc,
        pv_kw=pv_kw,
        discharge_w=discharge_w,
    )


# ── system_health ─────────────────────────────────────────────────────────────


class TestSystemHealth:
    def test_no_adapters_returns_only_safety_and_styrning(self) -> None:
        """No inverter/ev adapters → only safety + styrning keys."""
        coord = _make_coord()
        coord.inverter_adapters = []
        coord.ev_adapter = None
        health = coord.system_health
        assert "sakerhet" in health
        assert "styrning" in health
        assert health["styrning"] == "ok"

    def test_inverter_offline_when_state_unavailable(self) -> None:
        """EMS state unavailable → adapter key = 'offline'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coord()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 60.0

        # hass.states.get returns unavailable state
        ems_state = MagicMock()
        ems_state.state = "unavailable"
        coord.hass.states.get = lambda eid: ems_state

        coord.inverter_adapters = [adapter]
        health = coord.system_health
        assert health["kontor"] == "offline"

    def test_inverter_ok_when_state_valid(self) -> None:
        """EMS state valid + soc >= 0 → adapter key = 'ok'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coord()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 60.0

        ems_state = MagicMock()
        ems_state.state = "charge_pv"
        coord.hass.states.get = lambda eid: ems_state

        coord.inverter_adapters = [adapter]
        health = coord.system_health
        assert health["kontor"] == "ok"

    def test_inverter_no_data_when_soc_negative(self) -> None:
        """EMS state valid but soc < 0 → 'ingen data'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coord()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = -1.0

        ems_state = MagicMock()
        ems_state.state = "charge_pv"
        coord.hass.states.get = lambda eid: ems_state

        coord.inverter_adapters = [adapter]
        health = coord.system_health
        assert health["kontor"] == "ingen data"

    def test_ev_adapter_offline(self) -> None:
        """EaseeAdapter with status unavailable → ev = 'offline'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        coord.inverter_adapters = []
        ev = MagicMock(spec=EaseeAdapter)
        ev.status = "unavailable"
        ev.cable_locked = False
        ev.is_charging = False
        coord.ev_adapter = ev
        coord.hass.states.get = lambda eid: None

        health = coord.system_health
        assert health["ev"] == "offline"

    def test_ev_adapter_charging(self) -> None:
        """EaseeAdapter cable_locked + is_charging → 'laddar'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        coord.inverter_adapters = []
        ev = MagicMock(spec=EaseeAdapter)
        ev.status = "charging"
        ev.cable_locked = True
        ev.is_charging = True
        coord.ev_adapter = ev
        coord.hass.states.get = lambda eid: None

        health = coord.system_health
        assert health["ev"] == "laddar"

    def test_ev_adapter_connected_not_charging(self) -> None:
        """EaseeAdapter cable_locked but NOT charging → 'ansluten'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        coord.inverter_adapters = []
        ev = MagicMock(spec=EaseeAdapter)
        ev.status = "connected"
        ev.cable_locked = True
        ev.is_charging = False
        coord.ev_adapter = ev
        coord.hass.states.get = lambda eid: None

        health = coord.system_health
        assert health["ev"] == "ansluten"

    def test_ev_adapter_not_connected(self) -> None:
        """EaseeAdapter not cable_locked → 'ej ansluten'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.easee import EaseeAdapter

        coord = _make_coord()
        coord.inverter_adapters = []
        ev = MagicMock(spec=EaseeAdapter)
        ev.status = "disconnected"
        ev.cable_locked = False
        ev.is_charging = False
        coord.ev_adapter = ev
        coord.hass.states.get = lambda eid: None

        health = coord.system_health
        assert health["ev"] == "ej ansluten"

    def test_safety_warning_when_many_blocks(self) -> None:
        """recent_block_count >= SAFETY_BLOCK_THRESHOLD → 'varning'."""
        coord = _make_coord()
        coord.inverter_adapters = []
        coord.safety.recent_block_count = lambda seconds: 25  # above threshold=20
        health = coord.system_health
        assert health["sakerhet"] == "varning"

    def test_ems_paused(self) -> None:
        """_ems_pause_until in future → styrning = 'pausad'."""
        import time

        coord = _make_coord()
        coord.inverter_adapters = []
        coord._ems_pause_until = time.monotonic() + 3600  # 1h from now
        health = coord.system_health
        assert health["styrning"] == "pausad"


# ── status_text ───────────────────────────────────────────────────────────────


class TestStatusText:
    def test_all_ok(self) -> None:
        """No issues → 'Allt fungerar'."""
        coord = _make_coord()
        coord.inverter_adapters = []
        assert coord.status_text == "Allt fungerar"

    def test_offline_inverter_shown(self) -> None:
        """Offline inverter → text contains 'offline'."""
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        coord = _make_coord()
        adapter = MagicMock(spec=GoodWeAdapter)
        adapter.prefix = "kontor"
        adapter.soc = 60.0

        ems_state = MagicMock()
        ems_state.state = "unavailable"
        coord.hass.states.get = lambda eid: ems_state
        coord.inverter_adapters = [adapter]

        assert "offline" in coord.status_text.lower()

    def test_safety_warning_shown(self) -> None:
        """Safety block → text includes safety text."""
        coord = _make_coord()
        coord.inverter_adapters = []
        coord.safety.recent_block_count = lambda seconds: 25
        # 'Sakerhetsspaerr aktiv' in output
        assert "Sakerhetsspaerr" in coord.status_text


# ── plan_score ────────────────────────────────────────────────────────────────


class TestPlanScore:
    def test_no_actuals_returns_none_scores(self) -> None:
        """Less than 2 actuals → all scores None."""
        coord = _make_coord()
        coord.hourly_actuals = []
        result = coord.plan_score()
        assert result["score_today"] is None
        assert result["score_7d"] is None
        assert result["score_30d"] is None
        assert result["trend"] == "stable"

    def test_plan_score_with_actuals(self) -> None:
        """2 actuals → score_today computed."""
        from custom_components.carmabox.optimizer.models import HourActual

        coord = _make_coord()
        coord.hourly_actuals = [
            HourActual(hour=10, planned_weighted_kw=2.0, actual_weighted_kw=2.1),
            HourActual(hour=11, planned_weighted_kw=1.8, actual_weighted_kw=1.9),
        ]
        result = coord.plan_score()
        assert result["score_today"] is not None
        assert 80 < result["score_today"] <= 100

    def test_plan_score_7d_with_daily_savings(self) -> None:
        """7 days of daily_savings → score_7d computed from consistency."""
        coord = _make_coord()
        coord.hourly_actuals = []  # no actuals → score_today = None
        # Add 7 days of savings data
        ds = [DailySavings(date=f"2026-03-{i:02d}", total_kr=5.0) for i in range(1, 8)]
        coord.savings.daily_savings = ds
        # score_today = None (no actuals) → score_7d should equal score_today
        result = coord.plan_score()
        # With no hourly actuals, score_today = None → score_7d = None
        assert result["score_7d"] is None

    def test_plan_score_7d_computed_from_savings(self) -> None:
        """Actuals + 7 days savings → score_7d uses savings-based formula."""
        from custom_components.carmabox.optimizer.models import HourActual

        coord = _make_coord()
        coord.hourly_actuals = [
            HourActual(hour=10, planned_weighted_kw=2.0, actual_weighted_kw=2.0),
            HourActual(hour=11, planned_weighted_kw=2.0, actual_weighted_kw=2.0),
        ]
        ds = [DailySavings(date=f"2026-03-{i:02d}", total_kr=10.0) for i in range(1, 8)]
        coord.savings.daily_savings = ds
        result = coord.plan_score()
        assert result["score_7d"] is not None

    def test_plan_score_trend_improving(self) -> None:
        """Recent 7d > prev 7d * 1.1 → trend = 'improving'."""
        from custom_components.carmabox.optimizer.models import HourActual

        coord = _make_coord()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(2)
        ]
        # 14 days: last 7 total=70, prev 7 total=50 → improving
        older = [DailySavings(date=f"2026-02-{i:02d}", total_kr=50.0 / 7) for i in range(1, 8)]
        recent = [DailySavings(date=f"2026-03-{i:02d}", total_kr=70.0 / 7) for i in range(1, 8)]
        coord.savings.daily_savings = older + recent
        result = coord.plan_score()
        assert result["trend"] == "improving"

    def test_plan_score_trend_declining(self) -> None:
        """Recent 7d < prev 7d * 0.9 → trend = 'declining'."""
        from custom_components.carmabox.optimizer.models import HourActual

        coord = _make_coord()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(2)
        ]
        older = [DailySavings(date=f"2026-02-{i:02d}", total_kr=70.0 / 7) for i in range(1, 8)]
        recent = [DailySavings(date=f"2026-03-{i:02d}", total_kr=50.0 / 7) for i in range(1, 8)]
        coord.savings.daily_savings = older + recent
        result = coord.plan_score()
        assert result["trend"] == "declining"

    def test_plan_score_30d(self) -> None:
        """30+ daily_savings → score_30d computed."""
        from custom_components.carmabox.optimizer.models import HourActual

        coord = _make_coord()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(2)
        ]
        ds = [DailySavings(date=f"2026-03-{i:02d}", total_kr=8.0) for i in range(1, 31)]
        coord.savings.daily_savings = ds
        result = coord.plan_score()
        assert result["score_30d"] is not None


# ── daily_insight ─────────────────────────────────────────────────────────────


class TestDailyInsight:
    def test_collecting_when_no_peaks(self) -> None:
        """No ellevio peaks → status = 'collecting'."""
        coord = _make_coord()
        coord._ellevio_monthly_hourly_peaks = []
        insight = coord.daily_insight
        assert insight.get("status") == "collecting"

    def test_returns_ready_with_peaks(self) -> None:
        """24+ peaks → status = 'ready' with all fields."""
        coord = _make_coord()
        coord._ellevio_monthly_hourly_peaks = [2.0 + i * 0.1 for i in range(24)]
        insight = coord.daily_insight
        assert insight.get("status") == "ready"
        assert "ellevio_max_kw" in insight
        assert "ellevio_min_kw" in insight
        assert "recommendations" in insight

    def test_recommendation_r1_fires_when_max_gt_2x_target(self) -> None:
        """max_kw > target * 2 → R1 recommendation generated."""
        coord = _make_coord()
        coord.target_kw = 2.0
        # max = 5.0 kW > 2 * 2.0 = 4.0 → R1 fires
        coord._ellevio_monthly_hourly_peaks = [5.0] + [1.0] * 23
        insight = coord.daily_insight
        recs = insight.get("recommendations", [])
        categories = [r["category"] for r in recs]
        assert "effekt" in categories

    def test_recommendation_r4_fires_when_battery_idle_at_high_price(self) -> None:
        """Battery idle at high price > 3 times → R4 recommendation."""
        coord = _make_coord()
        coord._ellevio_monthly_hourly_peaks = [1.5] * 24
        # Add 3+ expensive-idle decisions
        coord.decision_log = deque(
            [
                _decision(action="idle", price_ore=100.0, battery_soc=60.0, hour=h)
                for h in range(10, 14)
            ],
            maxlen=48,
        )
        insight = coord.daily_insight
        recs = insight.get("recommendations", [])
        categories = [r["category"] for r in recs]
        assert "batteri" in categories

    def test_nordpool_cost_computed_from_grid_decisions(self) -> None:
        """Decisions with grid_kw + price_ore → total cost computed."""
        coord = _make_coord()
        coord._ellevio_monthly_hourly_peaks = [1.5] * 24
        coord.decision_log = deque(
            [_decision(grid_kw=2.0, price_ore=80.0, hour=10)],
            maxlen=48,
        )
        insight = coord.daily_insight
        assert insight["total_kwh"] > 0
        assert insight["total_cost_kr"] > 0

    def test_worst_best_hour_computed_from_decisions(self) -> None:
        """Decision log → worst/best hours identified."""
        coord = _make_coord()
        coord._ellevio_monthly_hourly_peaks = [1.5] * 24
        coord.decision_log = deque(
            [
                _decision(hour=10, weighted_kw=4.0),  # worst
                _decision(hour=14, weighted_kw=0.5),  # best
            ],
            maxlen=48,
        )
        insight = coord.daily_insight
        assert insight["worst_hour"] == 10
        assert insight["best_hour"] == 14


# ── _analyze_hour ─────────────────────────────────────────────────────────────


class TestAnalyzeHour:
    def test_negative_hour_returns_insufficient_data(self) -> None:
        """hour < 0 → 'Otillräcklig data'."""
        coord = _make_coord()
        result = coord._analyze_hour(-1, "worst")
        assert result == "Otillräcklig data"

    def test_no_log_for_hour_returns_no_data(self) -> None:
        """No decisions for hour → 'Ingen data för kl...'."""
        coord = _make_coord()
        coord.decision_log = deque([], maxlen=48)
        result = coord._analyze_hour(10, "worst")
        assert "Ingen data" in result

    def test_worst_label_high_grid_no_pv(self) -> None:
        """Worst + grid > 2.0 + pv < 0.5 → '→ hög last utan sol'."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=10, grid_kw=3.0, pv_kw=0.0, battery_soc=60.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(10, "worst")
        assert "hög last utan sol" in result

    def test_worst_label_idle_with_battery(self) -> None:
        """Worst + idle + soc > 30 → 'batteri outnyttjat'."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=10, action="idle", battery_soc=50.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(10, "worst")
        assert "batteri outnyttjat" in result

    def test_best_label_high_pv(self) -> None:
        """Best + pv > 2.0 → '→ sol drev förbrukningen'."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=14, pv_kw=3.0, battery_soc=80.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(14, "best")
        assert "sol drev" in result

    def test_best_label_discharge(self) -> None:
        """Best + discharge → '→ batteri sänkte toppen'."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=14, action="discharge", pv_kw=0.0, battery_soc=80.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(14, "best")
        assert "batteri sänkte toppen" in result

    def test_battery_low(self) -> None:
        """battery_soc < 20 → 'batteri lågt' in parts."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=10, battery_soc=15.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(10, "worst")
        assert "batteri lågt" in result

    def test_battery_full(self) -> None:
        """battery_soc > 95 → 'batteri fullt' in parts."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=10, battery_soc=98.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(10, "worst")
        assert "batteri fullt" in result

    def test_charge_pv_action(self) -> None:
        """charge_pv → 'solladdar' in parts."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=12, action="charge_pv", pv_kw=2.0, battery_soc=50.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(12, "best")
        assert "solladdar" in result

    def test_discharge_action_shows_watts(self) -> None:
        """discharge action → 'urladdning {w}W' in parts."""
        coord = _make_coord()
        coord.decision_log = deque(
            [_decision(hour=17, action="discharge", discharge_w=2000, battery_soc=60.0)],
            maxlen=48,
        )
        result = coord._analyze_hour(17, "worst")
        assert "2000W" in result


# ── rule_flow ─────────────────────────────────────────────────────────────────


class TestRuleFlow:
    def test_pv_active_charge_path(self) -> None:
        """PV > 0.5 + charge_pv → pv_check + charge_battery in active_path."""
        coord = _make_coord()
        coord.last_decision = Decision(
            action="charge_pv",
            pv_kw=2.0,
            grid_kw=-0.5,
            battery_soc=70.0,
            price_ore=80.0,
        )
        coord._ev_enabled = False
        coord._miner_on = False
        flow = coord.rule_flow
        assert "pv_check" in flow["active_path"]
        assert "charge_battery" in flow["active_path"]

    def test_pv_active_with_ev_and_miner(self) -> None:
        """PV + ev_enabled + miner_on → all three in active_path."""
        coord = _make_coord()
        coord.last_decision = Decision(
            action="idle",
            pv_kw=2.5,
            grid_kw=-1.0,
            battery_soc=70.0,
            price_ore=60.0,
        )
        coord._ev_enabled = True
        coord._miner_on = True
        flow = coord.rule_flow
        assert "charge_ev" in flow["active_path"]
        assert "miner" in flow["active_path"]

    def test_no_pv_discharge_path(self) -> None:
        """No PV + discharge → price_check + discharge in active_path."""
        coord = _make_coord()
        coord.last_decision = Decision(
            action="discharge",
            pv_kw=0.0,
            grid_kw=3.0,
            battery_soc=70.0,
            price_ore=120.0,
        )
        flow = coord.rule_flow
        assert "price_check" in flow["active_path"]
        assert "discharge" in flow["active_path"]

    def test_no_pv_idle_path(self) -> None:
        """No PV + idle → price_check + idle in active_path."""
        coord = _make_coord()
        coord.last_decision = Decision(
            action="idle",
            pv_kw=0.0,
            grid_kw=1.5,
            battery_soc=70.0,
            price_ore=80.0,
        )
        flow = coord.rule_flow
        assert "price_check" in flow["active_path"]
        assert "idle" in flow["active_path"]

    def test_guard_min_soc_warning_when_soc_low(self) -> None:
        """battery_soc <= min_soc → guard_min_soc status = 'warning'."""
        coord = _make_coord()
        coord.min_soc = 15.0
        coord.last_decision = Decision(
            action="idle",
            pv_kw=0.0,
            grid_kw=1.5,
            battery_soc=10.0,  # below min_soc
            price_ore=80.0,
        )
        flow = coord.rule_flow
        min_soc_guard = next(g for g in flow["guards"] if g["id"] == "guard_min_soc")
        assert min_soc_guard["status"] == "warning"

    def test_price_tier_cheap(self) -> None:
        """Price < price_cheap_ore → tier = 'billigt'."""
        coord = _make_coord()
        coord._cfg = {"price_cheap_ore": 40.0, "price_expensive_ore": 100.0}
        coord.last_decision = Decision(
            action="idle",
            pv_kw=0.0,
            grid_kw=1.5,
            battery_soc=70.0,
            price_ore=20.0,  # cheap
        )
        flow = coord.rule_flow
        price_node = next(n for n in flow["nodes"] if n["id"] == "price_check")
        assert price_node["tier"] == "billigt"

    def test_price_tier_expensive(self) -> None:
        """Price > price_expensive_ore → tier = 'dyrt'."""
        coord = _make_coord()
        coord._cfg = {"price_cheap_ore": 40.0, "price_expensive_ore": 100.0}
        coord.last_decision = Decision(
            action="discharge",
            pv_kw=0.0,
            grid_kw=3.0,
            battery_soc=70.0,
            price_ore=150.0,  # expensive
        )
        flow = coord.rule_flow
        price_node = next(n for n in flow["nodes"] if n["id"] == "price_check")
        assert price_node["tier"] == "dyrt"

    def test_returns_all_required_keys(self) -> None:
        """rule_flow returns nodes, guards, active_path, active_rule, summary_sv."""
        coord = _make_coord()
        flow = coord.rule_flow
        assert "nodes" in flow
        assert "guards" in flow
        assert "active_path" in flow
        assert "active_rule" in flow
        assert "summary_sv" in flow
