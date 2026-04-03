"""Coverage tests for CarmaboxCoordinator EV command and daily goal methods.

Targets coordinator.py:
  Lines 4579-4606  — _cmd_ev_start (clamping, idempotent, set_current fail)
  Lines 4610-4614  — _cmd_ev_stop (no adapter, disable+reset)
  Lines 4636-4648  — _cmd_ev_adjust (no adapter, no-op if same amps, ramp UP)
  Lines 4396-4402  — EV ramp adjust in surplus path
  Lines 5286-5291  — _send_morning_report
  Lines 5307-5311  — _update_daily_avg_price
  Lines 5962-5966  — _feed_predictor_ml EV usage at 22:00
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger
from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    Decision,
    ShadowComparison,
)
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState

# ── Factory ───────────────────────────────────────────────────────────────────

_EV_PREFIX = "easee_home_test"


def _make_coord(*, ev_enabled: bool = False, ev_amps: int = 0) -> CarmaboxCoordinator:
    """Minimal coordinator for EV command testing."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()

    def _safe_create_task(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    hass.async_create_task = _safe_create_task
    hass.states.get = MagicMock(return_value=None)

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.name = "carmabox"
    coord._cfg = {}
    coord.executor_enabled = True

    # EV adapter
    ev = MagicMock()
    ev.prefix = _EV_PREFIX
    ev.enable = AsyncMock(return_value=True)
    ev.disable = AsyncMock(return_value=True)
    ev.set_current = AsyncMock(return_value=True)
    ev.reset_to_default = AsyncMock(return_value=True)
    coord.ev_adapter = ev

    # EV state
    coord._ev_enabled = ev_enabled
    coord._ev_current_amps = ev_amps
    coord._ev_last_ramp_time = 0.0
    coord._ev_initialized = True

    # Inverter adapters
    coord.inverter_adapters = []

    # Persistence
    coord._runtime_store = MagicMock()
    coord._runtime_store.async_save = AsyncMock()
    coord._runtime_store.async_load = AsyncMock(return_value=None)
    coord._savings_store = MagicMock()
    coord._savings_store.async_save = AsyncMock()
    coord._savings_last_save = 0.0
    coord._predictor_store = MagicMock()
    coord._predictor_store.async_save = AsyncMock()
    coord._predictor_last_save = 0.0
    # PLAT-975: ML Predictor
    from custom_components.carmabox.core.ml_predictor import MLPredictor as _MLPred

    coord._ml_predictor = _MLPred()
    coord._ml_predictor_store = MagicMock()
    coord._ml_predictor_store.async_save = AsyncMock()
    coord._ml_predictor_loaded = True
    coord._ml_predictor_last_save = 0.0
    coord.ml_forecast_24h = []
    coord._consumption_store = MagicMock()
    coord._consumption_store.async_save = AsyncMock()
    coord._consumption_last_save = 0.0
    coord._ledger_store = MagicMock()
    coord._ledger_store.async_save = AsyncMock()
    coord._ledger_last_save = 0.0
    coord._ledger_store.async_load = AsyncMock(return_value=None)

    # Core state
    coord._last_command = BatteryCommand.IDLE
    coord._miner_on = False
    coord._miner_entity = ""
    coord._night_ev_active = False
    coord.plan = []
    coord.savings = SavingsState(month=3, year=2026)
    coord.ledger = EnergyLedger()
    coord.predictor = ConsumptionPredictor()
    coord.consumption_profile = ConsumptionProfile()
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord.last_decision = Decision()
    coord.decision_log = deque(maxlen=48)
    coord.shadow = ShadowComparison()
    coord.shadow_log = []
    coord._shadow_savings_kr = 0.0
    coord.target_kw = 2.0
    coord.min_soc = 15.0
    coord.data = None
    coord._last_known_ev_soc = -1.0
    coord._last_known_ev_soc_time = 0.0
    coord._rule_triggers = {}
    coord._active_rule_id = ""
    coord._ems_consecutive_failures = 0
    coord._ems_pause_until = 0.0
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}
    coord._ellevio_hour_samples = []
    coord._ellevio_current_hour = -1
    coord._ellevio_monthly_hourly_peaks = []
    coord._estimated_house_base_kw = 2.0
    coord._daily_goals: dict = {}
    coord._breach_history: dict = {}
    coord._benchmark_last_fetch = 0.0
    coord._plan_deviation_count = 0
    coord._plan_last_correction_time = 0.0
    coord._plan_grid_excess_count = 0

    # License
    coord._license_tier = "premium"
    coord._license_features = ["analyzer", "executor", "ev_control"]
    coord._license_last_check = 0.0
    coord._license_check_interval = 99_999_999
    coord._license_valid_until = ""

    # Safety
    coord.safety = MagicMock()
    coord.notifier = MagicMock()
    coord.notifier.morning_report = AsyncMock()

    return coord


# ── _cmd_ev_start ─────────────────────────────────────────────────────────────


class TestCmdEvStart:
    @pytest.mark.asyncio
    async def test_clamps_amps_below_min(self) -> None:
        """amps < DEFAULT_EV_MIN_AMPS → clamped to min, not lower."""
        coord = _make_coord()

        await coord._cmd_ev_start(2)  # Below min (6A)

        # set_current should be called with min (6A)
        coord.ev_adapter.set_current.assert_awaited_once_with(6)

    @pytest.mark.asyncio
    async def test_clamps_amps_above_max(self) -> None:
        """amps > MAX_EV_CURRENT → clamped to max (10A)."""
        coord = _make_coord()

        await coord._cmd_ev_start(999)

        coord.ev_adapter.set_current.assert_awaited_once_with(10)

    @pytest.mark.asyncio
    async def test_idempotent_if_already_at_amps(self) -> None:
        """EV already enabled at same amps → return early, no adapter calls."""
        coord = _make_coord(ev_enabled=True, ev_amps=10)

        await coord._cmd_ev_start(10)

        coord.ev_adapter.set_current.assert_not_awaited()
        coord.ev_adapter.enable.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_ev_adapter_returns_early(self) -> None:
        """No ev_adapter → return immediately without crash."""
        coord = _make_coord()
        coord.ev_adapter = None

        await coord._cmd_ev_start(10)  # Should not raise

    @pytest.mark.asyncio
    async def test_set_current_fail_aborts(self) -> None:
        """set_current returns False → enable() not called."""
        coord = _make_coord()
        coord.ev_adapter.set_current = AsyncMock(return_value=False)

        await coord._cmd_ev_start(10)

        coord.ev_adapter.enable.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enable_fail_disables_and_returns(self) -> None:
        """enable() returns False → disable() called and state unchanged."""
        coord = _make_coord(ev_enabled=False, ev_amps=0)
        coord.ev_adapter.set_current = AsyncMock(return_value=True)
        coord.ev_adapter.enable = AsyncMock(return_value=False)

        await coord._cmd_ev_start(10)

        coord.ev_adapter.disable.assert_awaited_once()
        assert coord._ev_enabled is False

    @pytest.mark.asyncio
    async def test_successful_start_updates_state(self) -> None:
        """Successful start → _ev_enabled=True, _ev_current_amps updated."""
        coord = _make_coord(ev_enabled=False)
        coord.ev_adapter.set_current = AsyncMock(return_value=True)
        coord.ev_adapter.enable = AsyncMock(return_value=True)

        await coord._cmd_ev_start(10)  # MAX_AMPS=10

        assert coord._ev_enabled is True
        assert coord._ev_current_amps == 10


# ── _cmd_ev_stop ──────────────────────────────────────────────────────────────


class TestCmdEvStop:
    @pytest.mark.asyncio
    async def test_no_ev_adapter_returns_early(self) -> None:
        """No ev_adapter → return immediately."""
        coord = _make_coord()
        coord.ev_adapter = None

        await coord._cmd_ev_stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_disable_and_reset_called(self) -> None:
        """stop → disable() + reset_to_default() called."""
        coord = _make_coord(ev_enabled=True, ev_amps=16)

        await coord._cmd_ev_stop()

        coord.ev_adapter.disable.assert_awaited_once()
        coord.ev_adapter.reset_to_default.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_state_reset_after_stop(self) -> None:
        """After stop → _ev_enabled=False, _ev_current_amps=0."""
        coord = _make_coord(ev_enabled=True, ev_amps=16)

        await coord._cmd_ev_stop()

        assert coord._ev_enabled is False
        assert coord._ev_current_amps == 0

    @pytest.mark.asyncio
    async def test_runtime_saved_after_stop(self) -> None:
        """State persisted after stop."""
        coord = _make_coord(ev_enabled=True)

        await coord._cmd_ev_stop()

        coord._runtime_store.async_save.assert_awaited_once()


# ── _cmd_ev_adjust ────────────────────────────────────────────────────────────


class TestCmdEvAdjust:
    @pytest.mark.asyncio
    async def test_no_ev_adapter_returns_early(self) -> None:
        """No ev_adapter → return immediately."""
        coord = _make_coord(ev_enabled=True)
        coord.ev_adapter = None

        await coord._cmd_ev_adjust(10)  # Should not raise

    @pytest.mark.asyncio
    async def test_ev_not_enabled_returns_early(self) -> None:
        """EV not enabled → return immediately."""
        coord = _make_coord(ev_enabled=False)

        await coord._cmd_ev_adjust(10)

        coord.ev_adapter.set_current.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_amps_no_op(self) -> None:
        """Already at requested amps → no-op."""
        coord = _make_coord(ev_enabled=True, ev_amps=10)

        await coord._cmd_ev_adjust(10)

        coord.ev_adapter.set_current.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ramp_up_one_step_at_a_time(self) -> None:
        """Ramp UP follows EV_RAMP_STEPS — not direct jump."""
        coord = _make_coord(ev_enabled=True, ev_amps=6)

        # Ramp steps: [6, 8, 10, 13, 16, 20, 25, 32]
        # Current=6, target=16 → next_step=8 (one step above current)
        await coord._cmd_ev_adjust(16)

        call_amps = coord.ev_adapter.set_current.call_args[0][0]
        assert call_amps == 8  # First step above 6

    @pytest.mark.asyncio
    async def test_ramp_down_direct(self) -> None:
        """Ramp DOWN goes directly to target (no surge risk)."""
        coord = _make_coord(ev_enabled=True, ev_amps=16)

        await coord._cmd_ev_adjust(10)

        # Direct to target
        coord.ev_adapter.set_current.assert_awaited_once_with(10)

    @pytest.mark.asyncio
    async def test_successful_adjust_updates_amps(self) -> None:
        """Successful adjust → _ev_current_amps updated."""
        coord = _make_coord(ev_enabled=True, ev_amps=6)
        coord.ev_adapter.set_current = AsyncMock(return_value=True)

        await coord._cmd_ev_adjust(8)

        assert coord._ev_current_amps == 8


# ── _send_morning_report ──────────────────────────────────────────────────────


class TestSendMorningReport:
    @pytest.mark.asyncio
    async def test_morning_report_calls_notifier(self) -> None:
        """_send_morning_report collects data and calls notifier.morning_report."""
        coord = _make_coord()
        coord._cfg = {
            "battery_soc_1": "sensor.soc1",
            "battery_soc_2": "sensor.soc2",
            "ev_soc_entity": "sensor.ev_soc",
            "price_entity": "sensor.price",
        }

        # Mock state reads
        soc1_state = MagicMock(state="85")
        soc2_state = MagicMock(state="60")
        ev_state = MagicMock(state="75")
        price_state = MagicMock(state="82.5")

        def _get_state(eid: str) -> MagicMock | None:
            return {
                "sensor.soc1": soc1_state,
                "sensor.soc2": soc2_state,
                "sensor.ev_soc": ev_state,
                "sensor.price": price_state,
            }.get(eid)

        coord.hass.states.get = _get_state

        await coord._send_morning_report()

        coord.notifier.morning_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_morning_report_exception_handled(self) -> None:
        """Exception in morning report → debug logged, not re-raised."""
        coord = _make_coord()
        coord.notifier.morning_report = AsyncMock(side_effect=RuntimeError("notify fail"))
        coord._cfg = {}

        await coord._send_morning_report()  # Should not raise


# ── _update_daily_avg_price ───────────────────────────────────────────────────


class TestUpdateDailyAvgPrice:
    def test_no_price_entity_skips(self) -> None:
        """No price_entity configured → no update."""
        coord = _make_coord()
        coord._cfg = {}
        coord._daily_avg_price = 80.0

        coord._update_daily_avg_price()

        assert coord._daily_avg_price == 80.0

    def test_updates_avg_from_nordpool_prices(self) -> None:
        """Valid Nordpool prices → _daily_avg_price updated."""
        coord = _make_coord()
        coord._cfg = {"price_entity": "sensor.nordpool_kwh_se3"}
        coord._daily_avg_price = 50.0

        # Mock NordpoolAdapter
        mock_adapter = MagicMock()
        mock_adapter.today_prices = [60.0, 70.0, 80.0, 90.0]

        with patch(
            "custom_components.carmabox.coordinator.NordpoolAdapter",
            return_value=mock_adapter,
        ):
            coord._update_daily_avg_price()

        assert coord._daily_avg_price == pytest.approx(75.0)

    def test_all_fallback_prices_no_update(self) -> None:
        """All prices are fallback → no update (avoid dummy data)."""
        coord = _make_coord()
        fallback = 50.0
        coord._cfg = {"price_entity": "sensor.nordpool", "fallback_price_ore": fallback}
        coord._daily_avg_price = 99.0

        mock_adapter = MagicMock()
        mock_adapter.today_prices = [fallback] * 24  # All fallback

        with patch(
            "custom_components.carmabox.coordinator.NordpoolAdapter",
            return_value=mock_adapter,
        ):
            coord._update_daily_avg_price()

        # Should NOT update when all prices are fallback
        assert coord._daily_avg_price == 99.0

    def test_empty_prices_no_update(self) -> None:
        """Empty prices list → no update."""
        coord = _make_coord()
        coord._cfg = {"price_entity": "sensor.nordpool"}
        coord._daily_avg_price = 55.0

        mock_adapter = MagicMock()
        mock_adapter.today_prices = []

        with patch(
            "custom_components.carmabox.coordinator.NordpoolAdapter",
            return_value=mock_adapter,
        ):
            coord._update_daily_avg_price()

        assert coord._daily_avg_price == 55.0


# ── _feed_predictor_ml EV usage ───────────────────────────────────────────────


class TestFeedPredictorMlEvUsage:
    def test_feed_predictor_no_sensors_no_crash(self) -> None:
        """_feed_predictor_ml with all sensors unavailable → no crash."""
        coord = _make_coord()
        coord.predictor = MagicMock()
        coord.hass.states.get = MagicMock(return_value=None)
        coord._ev_usage_tracked_today = True

        state = CarmaboxState(ev_soc=-1.0)
        coord._feed_predictor_ml(state)  # Should not raise

    def test_feed_predictor_appliance_below_threshold_no_event(self) -> None:
        """Appliance power < 500W → add_appliance_event NOT called."""
        coord = _make_coord()
        coord.predictor = MagicMock()
        coord.predictor.add_appliance_event = MagicMock()
        coord._ev_usage_tracked_today = True

        low_power = MagicMock(state="200")

        def get_state(eid: str) -> MagicMock | None:
            if eid == "sensor.98_shelly_plug_s_power":
                return low_power
            return None

        coord.hass.states.get = get_state

        state = CarmaboxState()
        coord._feed_predictor_ml(state)

        coord.predictor.add_appliance_event.assert_not_called()

    def test_feed_predictor_invalid_sensor_state_no_crash(self) -> None:
        """Invalid sensor state string → ValueError handled, no crash."""
        coord = _make_coord()
        coord.predictor = MagicMock()
        coord._ev_usage_tracked_today = True

        bad_state = MagicMock(state="not_a_number")
        temp_bad = MagicMock(state="N/A")

        def get_state(eid: str) -> MagicMock | None:
            if eid == "sensor.98_shelly_plug_s_power":
                return bad_state
            if eid == "sensor.tempest_temperature":
                return temp_bad
            return None

        coord.hass.states.get = get_state

        state = CarmaboxState()
        coord._feed_predictor_ml(state)  # Should not raise
