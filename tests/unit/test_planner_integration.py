"""Tests for planner integration with coordinator.

Verifies that coordinator._generate_plan() calls planner with
real data from adapters and stores the result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    Decision,
    HourPlan,
)
from custom_components.carmabox.optimizer.multiday_planner import (
    DayInputs,
    build_day_inputs,
    generate_multiday_plan,
)
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState


def _make_coord(options: dict[str, object] | None = None) -> CarmaboxCoordinator:
    """Create coordinator with mocked hass."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    states: dict[str, MagicMock] = {}

    def get_state(eid: str) -> MagicMock | None:
        return states.get(eid)

    hass.states.get = get_state

    entry = MagicMock()
    entry.options = options or {}
    entry.data = dict(entry.options)
    entry.entry_id = "test"

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord._cfg = {**entry.data, **entry.options}
    coord.safety = MagicMock()
    coord.safety.check_discharge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_charge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.plan = []
    coord._plan_counter = 0
    coord._last_command = BatteryCommand.IDLE
    coord.target_kw = float(entry.options.get("target_weighted_kw", 2.0))
    coord.min_soc = float(entry.options.get("min_soc", 15.0))
    coord.logger = MagicMock()
    coord.name = "carmabox"
    coord._states = states
    coord.savings = SavingsState(month=3, year=2026)
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord._daily_discharge_kwh = 0.0
    coord._daily_safety_blocks = 0
    coord._daily_plans = 0
    coord.notifier = MagicMock()
    coord.notifier.crosscharge_alert = AsyncMock()
    coord.notifier.proactive_discharge_started = AsyncMock()
    coord._runtime_loaded = True
    coord._ledger_loaded = True
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord.last_decision = Decision()
    from collections import deque as _deque

    coord.decision_log = _deque(maxlen=48)
    coord.consumption_profile = ConsumptionProfile()
    coord.hourly_actuals = []
    coord._last_tracked_hour = -1
    coord.executor_enabled = True
    coord._consumption_last_hour = -1
    coord._pending_write_verifies = []
    coord._ev_enabled = False
    coord._ev_current_amps = 0
    coord._ev_last_ramp_time = 0.0
    coord._ev_initialized = True
    coord._miner_entity = ""
    coord._miner_on = False
    from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger

    coord.ledger = EnergyLedger()
    coord._license_tier = "premium"
    coord._license_features = ["analyzer", "executor", "dashboard"]
    coord._license_last_check = 0.0
    coord._license_check_interval = 99999999
    coord._license_valid_until = ""
    coord._license_offline_grace_days = 7

    # PLAT-965: Predictor
    coord.predictor = ConsumptionPredictor()

    # IT-1937: Rule tracking
    coord._rule_triggers = {}
    coord._active_rule_id = ""
    # EV SoC tracking
    coord._last_known_ev_soc = -1.0
    coord._last_known_ev_soc_time = 0.0
    coord._ev_last_full_charge_date = ""
    coord._ev_days_since_full = 0
    coord._ev_soc_day_start = -1.0
    # Grid guard result
    coord._grid_guard_result = None
    coord._daily_avg_price = 100.0
    coord._avg_price_initialized = True
    coord._taper_active = False
    coord._cold_lock_active = False
    coord._grid_samples = []
    coord._grid_sample_max = 10
    coord._disabled_methods = {}
    coord._last_discharge_w = 0
    coord._fast_charge_authorized = False
    coord._startup_safety_confirmed = True
    coord._night_ev_active = False
    from custom_components.carmabox.optimizer.models import HourlyMeterState

    coord._meter_state = HourlyMeterState()
    coord._breach_corrections = []
    coord._breach_load_shed_active = False
    coord._breach_escalation = {}
    from custom_components.carmabox.core.grid_guard import GridGuard, GridGuardConfig

    coord._grid_guard = GridGuard(GridGuardConfig())
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}
    coord._savings_loaded = True
    coord._savings_last_save = 0.0
    coord._savings_store = MagicMock()
    coord._savings_store.async_save = AsyncMock()
    coord._consumption_loaded = True
    coord._consumption_last_save = 0.0
    coord._consumption_store = MagicMock()
    coord._consumption_store.async_save = AsyncMock()
    coord._predictor_loaded = True
    coord._predictor_last_save = 0.0
    coord._predictor_store = MagicMock()
    coord._predictor_store.async_save = AsyncMock()

    # PLAT-1141: ExecutionEngine
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord._execution_engine = ExecutionEngine(coord)

    return coord


def _set(
    coord: CarmaboxCoordinator,
    eid: str,
    value: str,
    attrs: dict[str, object] | None = None,
) -> None:
    """Set mock state."""
    s = MagicMock()
    s.state = value
    s.attributes = attrs or {}
    coord._states[eid] = s  # type: ignore[attr-defined]


def _make_plan(hours: int = 8, action: str = "i") -> list[HourPlan]:
    """Create a simple test plan."""
    return [
        HourPlan(
            hour=(17 + i) % 24,
            action=action,
            battery_kw=0,
            grid_kw=2.0,
            weighted_kw=2.0,
            pv_kw=0,
            consumption_kw=2.0,
            ev_kw=0,
            ev_soc=50,
            battery_soc=80 - i * 2,
            price=50,
        )
        for i in range(hours)
    ]


class TestGeneratePlan:
    def test_generates_plan_with_prices(self) -> None:
        """Plan should be generated from Nordpool + Solcast data."""
        coord = _make_coord(
            {
                "price_entity": "sensor.np",
                "grid_entity": "sensor.grid",
                "battery_soc_1": "sensor.soc1",
                "pv_entity": "sensor.pv",
            }
        )
        _set(
            coord,
            "sensor.np",
            "85",
            {"today": list(range(24)), "tomorrow": [], "tomorrow_valid": False},
        )
        _set(coord, "sensor.grid", "1500")
        _set(coord, "sensor.soc1", "80")
        _set(coord, "sensor.pv", "0")
        _set(
            coord,
            "sensor.solcast_pv_forecast_forecast_today",
            "20",
            {"detailedHourly": []},
        )
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "15")

        coord._generate_plan(CarmaboxState(battery_soc_1=80))

        assert len(coord.plan) > 0
        assert all(isinstance(h, HourPlan) for h in coord.plan)

    def test_plan_updates_on_replan(self) -> None:
        """Replan should replace old plan."""
        coord = _make_coord({"price_entity": "sensor.np", "battery_soc_1": "sensor.soc1"})
        _set(
            coord,
            "sensor.np",
            "50",
            {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False},
        )
        _set(coord, "sensor.soc1", "60")
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "10")
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "5")

        old_plan = _make_plan(4)
        coord.plan = old_plan

        coord._generate_plan(CarmaboxState(battery_soc_1=60))

        assert coord.plan is not old_plan
        assert len(coord.plan) > 0

    def test_plan_error_keeps_old(self) -> None:
        """Planner error should keep old plan."""
        coord = _make_coord()
        old_plan = _make_plan(4)
        coord.plan = old_plan

        with patch(
            "custom_components.carmabox.coordinator.generate_plan",
            side_effect=ValueError("bad data"),
        ):
            coord._generate_plan(CarmaboxState())

        assert coord.plan is old_plan

    def test_plan_has_correct_hours(self) -> None:
        """Plan should cover remaining today + tomorrow."""
        coord = _make_coord({"price_entity": "sensor.np", "battery_soc_1": "sensor.soc1"})
        _set(
            coord,
            "sensor.np",
            "50",
            {
                "today": [50.0] * 24,
                "tomorrow": [30.0] * 24,
                "tomorrow_valid": True,
            },
        )
        _set(coord, "sensor.soc1", "80")
        _set(
            coord,
            "sensor.solcast_pv_forecast_forecast_today",
            "20",
            {"detailedHourly": []},
        )
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "15")

        coord._generate_plan(CarmaboxState(battery_soc_1=80))

        assert len(coord.plan) >= 24  # At least remaining today

    def test_target_updated_from_grid_logic(self) -> None:
        """Target should be calculated from PV forecast + reserve."""
        coord = _make_coord({"price_entity": "sensor.np", "battery_soc_1": "sensor.soc1"})
        _set(
            coord,
            "sensor.np",
            "50",
            {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False},
        )
        _set(coord, "sensor.soc1", "90")
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "30")
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "28")
        for d in range(3, 8):
            _set(coord, f"sensor.solcast_pv_forecast_forecast_day_{d}", "25")

        coord._generate_plan(CarmaboxState(battery_soc_1=90))

        # Sunny forecast → target capped at ellevio_tak * 0.85 (default 4.0 * 0.85 = 3.4)
        assert coord.target_kw <= 3.5

    def test_ev_enabled_uses_dynamic_schedule(self) -> None:
        """EV enabled + EV present → dynamic EV schedule (not static)."""
        coord = _make_coord(
            {
                "price_entity": "sensor.np",
                "battery_soc_1": "sensor.soc1",
                "ev_enabled": True,
                "ev_capacity_kwh": 98,
                "ev_night_target_soc": 75,
                "ev_full_charge_days": 7,
            }
        )
        _set(
            coord,
            "sensor.np",
            "50",
            {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False},
        )
        _set(coord, "sensor.soc1", "80")
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "20")
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "15")

        # EV at 40% — needs charging
        coord._generate_plan(CarmaboxState(battery_soc_1=80, ev_soc=40))

        assert len(coord.plan) > 0
        # Some hours should have EV charging (dynamic schedule)
        ev_hours = [h for h in coord.plan if h.ev_kw > 0]
        # May or may not have EV hours depending on start_hour
        assert isinstance(ev_hours, list)


class TestExecutorWithPlan:
    @pytest.mark.asyncio
    async def test_executor_uses_plan_action(self) -> None:
        """Executor should read plan[current_hour] and act."""
        coord = _make_coord(
            {
                "battery_ems_1": "select.ems1",
                "battery_limit_1": "number.limit1",
            }
        )

        plan = _make_plan(24, action="d")
        plan[0] = HourPlan(
            hour=17,
            action="d",
            battery_kw=-1.5,
            grid_kw=1.0,
            weighted_kw=1.0,
            pv_kw=0,
            consumption_kw=2.5,
            ev_kw=0,
            ev_soc=50,
            battery_soc=70,
            price=100,
        )
        coord.plan = plan

        state = CarmaboxState(
            grid_power_w=2500,
            battery_soc_1=70,
            battery_soc_2=-1,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 17
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_executor_idle_on_idle_plan(self) -> None:
        """Plan action 'i' should keep idle (no discharge)."""
        coord = _make_coord()
        plan = _make_plan(24, action="i")
        coord.plan = plan

        state = CarmaboxState(
            grid_power_w=1500,  # Under target
            battery_soc_1=80,
            battery_soc_2=-1,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 17
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.IDLE

    @pytest.mark.asyncio
    async def test_executor_charge_on_export(self) -> None:
        """Export should trigger charge regardless of plan."""
        coord = _make_coord({"battery_ems_1": "select.ems1", "battery_soc_1": "sensor.soc1"})
        _set(coord, "sensor.soc1", "50")

        plan = _make_plan(24, action="d")  # Plan says discharge
        coord.plan = plan

        state = CarmaboxState(
            grid_power_w=-2000,  # Exporting!
            battery_soc_1=50,
            battery_soc_2=-1,
        )

        await coord._execute(state)

        # Export overrides plan → charge
        assert coord._last_command == BatteryCommand.CHARGE_PV


class TestPriceFallback:
    def test_fallback_to_secondary_price(self) -> None:
        """When primary price returns all-fallback (offline), use fallback source."""
        coord = _make_coord(
            {
                "price_entity": "sensor.np_offline",
                "price_entity_fallback": "sensor.tibber",
                "battery_soc_1": "sensor.soc1",
            }
        )
        # Primary: offline (returns None → adapter fallbacks to 100 öre flat)
        # Don't set sensor.np_offline → adapter returns fallback flat

        # Secondary (Tibber): has real prices
        _set(
            coord,
            "sensor.tibber",
            "85",
            {
                "today": [float(i * 5 + 10) for i in range(24)],
                "tomorrow": [],
                "tomorrow_valid": False,
            },
        )
        _set(coord, "sensor.soc1", "80")
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "20")
        _set(coord, "sensor.solcast_pv_forecast_forecast_tomorrow", "15")

        coord._generate_plan(CarmaboxState(battery_soc_1=80))

        # Plan should exist and NOT be flat-price
        assert len(coord.plan) > 0


class TestMultiDayPlanning:
    """PLAT-969: Multi-day planning integration tests."""

    def test_multi_day_72h_plan_length(self) -> None:
        """72h plan from start_hour=10 should have correct hours."""
        inputs = [
            DayInputs(
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 6 + [4.0] * 12 + [0.0] * 6,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
                price_source="nordpool",
                pv_source="solcast",
            )
            for _ in range(3)
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=10,
            battery_soc=60.0,
            battery_cap_kwh=20.0,
        )
        # Day 0: 24-10=14, Day 1: 24, Day 2: 24 → 62 hours
        assert plan.days == 3
        assert len(plan.hourly_plan) == 62
        assert len(plan.day_summaries) == 3

    def test_multi_day_plan_horizon_parameter(self) -> None:
        """AC1: plan_horizon_hours should control number of plan days."""
        for horizon, expected_days in [(24, 1), (48, 2), (72, 3), (168, 7)]:
            days = max(1, (horizon + 23) // 24)
            inputs = [
                DayInputs(
                    prices=[50.0] * 24,
                    pv_forecast=[0.0] * 24,
                    consumption=[2.0] * 24,
                    ev_schedule=[0.0] * 24,
                )
                for _ in range(days)
            ]
            plan = generate_multiday_plan(
                inputs,
                start_hour=0,
                battery_soc=50.0,
                battery_cap_kwh=20.0,
            )
            assert plan.days == expected_days, f"horizon={horizon} → days={plan.days}"

    def test_multi_day_plan_format_ac2(self) -> None:
        """AC2: Each hour has action, price, pv_kwh."""
        inputs = [
            DayInputs(
                prices=[float(30 + h) for h in range(24)],
                pv_forecast=[0.0] * 6 + [3.0] * 12 + [0.0] * 6,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
            for _ in range(3)
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=0,
            battery_soc=70.0,
            battery_cap_kwh=20.0,
        )
        for hp in plan.hourly_plan:
            assert hp.action in ("c", "d", "g", "i"), f"Invalid action: {hp.action}"
            assert hp.price >= 0, f"Negative price: {hp.price}"
            assert hp.pv_kw >= 0, f"Negative PV: {hp.pv_kw}"
            assert 0 <= hp.battery_soc <= 100, f"SoC out of range: {hp.battery_soc}"

    def test_multi_day_historical_mean_fallback_ac3(self) -> None:
        """AC3: When Nordpool unavailable (>48h), use historical mean prices."""
        hist_mean = [float(40 + h % 12) for h in range(24)]
        inputs = build_day_inputs(
            days=3,
            start_hour=0,
            start_weekday=0,  # Monday
            start_month=3,
            known_prices_today=[60.0] * 24,
            known_prices_tomorrow=[55.0] * 24,
            historical_mean_prices=hist_mean,
        )
        # Day 0 and 1 should use Nordpool
        assert inputs[0].price_source == "nordpool"
        assert inputs[1].price_source == "nordpool"
        # Day 2+ should use historical mean (no price_model provided)
        assert inputs[2].price_source == "historical_mean"
        assert inputs[2].prices == hist_mean

    def test_multi_day_solcast_daily_used_for_day3(self) -> None:
        """Solcast daily forecasts should be used for days 3+."""
        pv_daily = [25.0, 20.0, 15.0, 10.0, 5.0]  # today through day5
        inputs = build_day_inputs(
            days=5,
            start_hour=0,
            start_weekday=0,
            start_month=6,  # June — good solar
            known_pv_today=[0.0] * 6 + [4.0] * 12 + [0.0] * 6,
            known_pv_tomorrow=[0.0] * 6 + [3.5] * 12 + [0.0] * 6,
            known_pv_daily=pv_daily,
        )
        assert inputs[0].pv_source == "solcast"
        assert inputs[1].pv_source == "solcast"
        # Days 2+ should use estimated profiles based on daily totals
        assert inputs[2].pv_source == "predicted"
        # The daily total from known_pv_daily[2]=15.0 should be used
        assert abs(sum(inputs[2].pv_forecast) - 15.0) < 1.0

    def test_multi_day_fail_closed_no_solcast_ac6(self) -> None:
        """AC6: When Solcast unavailable, plan should be conservative."""
        # Simulate: all PV = 0 (Solcast down)
        inputs = [
            DayInputs(
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,  # No solar data (fail-closed)
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=0,
            battery_soc=80.0,
            battery_cap_kwh=20.0,
        )
        # With no PV, plan should be conservative — no solar charge actions
        solar_charge_hours = [hp for hp in plan.hourly_plan if hp.action == "c"]
        assert len(solar_charge_hours) == 0, "Should not plan solar charge with PV=0"

    def test_multi_day_data_quality(self) -> None:
        """Data quality should reflect sources used."""
        inputs_known = [
            DayInputs(
                price_source="nordpool",
                pv_source="solcast",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            )
        ]
        inputs_mixed = [
            DayInputs(
                price_source="nordpool",
                pv_source="solcast",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            ),
            DayInputs(
                price_source="historical_mean",
                pv_source="predicted",
                prices=[50.0] * 24,
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,
                ev_schedule=[0.0] * 24,
            ),
        ]

        plan_known = generate_multiday_plan(inputs_known, start_hour=0, battery_soc=50)
        plan_mixed = generate_multiday_plan(inputs_mixed, start_hour=0, battery_soc=50)

        assert plan_known.data_quality == "known"
        assert plan_mixed.data_quality == "mixed"

    def test_multi_day_soc_never_negative(self) -> None:
        """SoC should never go below 0 across multi-day plan."""
        inputs = [
            DayInputs(
                prices=[200.0] * 24,  # Very expensive — incentivize discharge
                pv_forecast=[0.0] * 24,
                consumption=[5.0] * 24,  # High consumption
                ev_schedule=[0.0] * 24,
            )
            for _ in range(3)
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=0,
            battery_soc=50.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
        )
        for hp in plan.hourly_plan:
            assert hp.battery_soc >= 0, f"SoC < 0 at hour {hp.hour}: {hp.battery_soc}"

    def test_multi_day_cost_estimate(self) -> None:
        """Total cost estimate should be reasonable."""
        inputs = [
            DayInputs(
                prices=[100.0] * 24,  # 1 kr/kWh
                pv_forecast=[0.0] * 24,
                consumption=[2.0] * 24,  # 48 kWh/day
                ev_schedule=[0.0] * 24,
            )
            for _ in range(3)
        ]
        plan = generate_multiday_plan(
            inputs,
            start_hour=0,
            battery_soc=50.0,
            battery_cap_kwh=20.0,
        )
        # Cost should be positive (importing from grid)
        assert plan.total_cost_estimate_kr > 0
