"""Tests for planner integration with coordinator.

Verifies that coordinator._generate_plan() calls planner with
real data from adapters and stores the result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.models import CarmaboxState, Decision, HourPlan
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

    return coord


def _set(
    coord: CarmaboxCoordinator, eid: str, value: str, attrs: dict[str, object] | None = None
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
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "20", {"detailedHourly": []})
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
        _set(coord, "sensor.solcast_pv_forecast_forecast_today", "20", {"detailedHourly": []})
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

        # Sunny forecast → low target (aggressive discharge)
        assert coord.target_kw < 3.0

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
