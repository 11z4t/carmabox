"""Coverage tests for CarmaboxCoordinator async persistence methods.

Targets coordinator.py persist/restore methods:
  Lines 506-563  — _check_license (hub validation)
  Lines 573-594  — on_ev_cable_connected
  Lines 696-745  — _async_restore_savings
  Lines 747-761  — _async_save_savings
  Lines 763-794  — _async_restore_consumption + _async_save_consumption
  Lines 796-808  — _async_restore_predictor
  Lines 810-931  — _async_restore_runtime + _async_save_runtime
  Lines 933-958  — _async_restore_ledger + _async_save_ledger
  Lines 960-990  — _async_save_predictor + _async_fetch_benchmarking
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger
from custom_components.carmabox.optimizer.models import (
    Decision,
    HourPlan,
    ShadowComparison,
)
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState

# ── Factory ───────────────────────────────────────────────────────────────────


def _make_coord() -> CarmaboxCoordinator:
    """Minimal coordinator for testing persistence methods."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=None)
    hass.states.async_all = MagicMock(return_value=[])

    def _safe_create_task(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    hass.async_create_task = _safe_create_task

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.name = "carmabox"
    coord._cfg = {}

    # Stores
    coord._savings_store = MagicMock()
    coord._savings_store.async_load = AsyncMock(return_value=None)
    coord._savings_store.async_save = AsyncMock()
    coord._savings_last_save = 0.0

    coord._consumption_store = MagicMock()
    coord._consumption_store.async_load = AsyncMock(return_value=None)
    coord._consumption_store.async_save = AsyncMock()
    coord._consumption_last_save = 0.0

    coord._predictor_store = MagicMock()
    coord._predictor_store.async_load = AsyncMock(return_value=None)
    coord._predictor_store.async_save = AsyncMock()
    coord._predictor_last_save = 0.0

    coord._runtime_store = MagicMock()
    coord._runtime_store.async_load = AsyncMock(return_value=None)
    coord._runtime_store.async_save = AsyncMock()

    coord._ledger_store = MagicMock()
    coord._ledger_store.async_load = AsyncMock(return_value=None)
    coord._ledger_store.async_save = AsyncMock()
    coord._ledger_last_save = 0.0

    # Core state
    coord.savings = SavingsState(month=3, year=2026)
    coord.consumption_profile = ConsumptionProfile()
    coord.predictor = ConsumptionPredictor()
    coord.ledger = EnergyLedger()
    coord.plan = []
    coord._last_command = BatteryCommand.STANDBY
    coord._ev_enabled = False
    coord._ev_current_amps = 6
    coord._miner_on = False
    coord._night_ev_active = False
    coord._ellevio_hour_samples = []
    coord._ellevio_monthly_hourly_peaks = []
    coord._surplus_hysteresis = None

    # License
    coord._license_last_check = 0.0
    coord._license_check_interval = 99_999_999
    coord._license_tier = "premium"
    coord._license_features = ["analyzer", "executor"]
    coord._license_valid_until = ""
    coord.executor_enabled = True

    # EV
    coord.ev_adapter = None
    coord.inverter_adapters = []
    coord._last_known_ev_soc = -1.0

    # Other
    coord.last_decision = Decision()
    coord.decision_log = deque(maxlen=48)
    coord.shadow = ShadowComparison()
    coord.shadow_log = []
    coord._shadow_savings_kr = 0.0
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}
    coord._miner_entity = ""
    coord._rule_triggers = {}
    coord._active_rule_id = ""
    coord._ems_consecutive_failures = 0
    coord._ems_pause_until = 0.0
    coord._benchmark_last_fetch = 0.0
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord.data = None
    coord.target_kw = 2.0
    coord.min_soc = 15.0

    return coord


# ── _check_license ────────────────────────────────────────────────────────────


class TestCheckLicense:
    @pytest.mark.asyncio
    async def test_skipped_when_interval_not_elapsed(self) -> None:
        """Check interval not elapsed → return immediately without HTTP call."""
        coord = _make_coord()
        coord._license_check_interval = 3600
        coord._license_last_check = 1e12  # Very recent

        with patch("aiohttp.ClientSession") as mock_session:
            await coord._check_license()
            mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_no_hub_url(self) -> None:
        """No hub_url in config → dev mode, skip HTTP call."""
        coord = _make_coord()
        coord._license_check_interval = 0  # Always run

        with patch("aiohttp.ClientSession") as mock_session:
            await coord._check_license()
            mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_license_valid_updates_features(self) -> None:
        """Hub returns 200 → updates tier, features, executor_enabled."""
        coord = _make_coord()
        coord._cfg = {
            "hub_url": "https://hub.test",
            "hub_api_key": "key",
            "hub_box_id": "box1",
            "executor_enabled": True,
        }
        coord._license_check_interval = 0

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "tier": "enterprise",
                "features": ["analyzer", "executor", "ev_control"],
                "valid_until": "2026-12-31",
            }
        )

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_get_ctx = AsyncMock()
        mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_get_ctx)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await coord._check_license()

        assert coord._license_tier == "enterprise"
        assert "ev_control" in coord._license_features

    @pytest.mark.asyncio
    async def test_license_http_error_uses_cached(self) -> None:
        """Hub returns non-200 → warning logged, cached license unchanged."""
        coord = _make_coord()
        coord._cfg = {"hub_url": "https://hub.test", "hub_api_key": "key", "hub_box_id": "box1"}
        coord._license_check_interval = 0
        coord._license_tier = "premium"

        mock_resp = AsyncMock()
        mock_resp.status = 503

        mock_get_ctx = AsyncMock()
        mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_get_ctx)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await coord._check_license()

        # Tier unchanged
        assert coord._license_tier == "premium"

    @pytest.mark.asyncio
    async def test_license_network_error_uses_cached(self) -> None:
        """Network error → debug logged, cached license unchanged."""
        coord = _make_coord()
        coord._cfg = {"hub_url": "https://hub.test", "hub_api_key": "key", "hub_box_id": "box1"}
        coord._license_check_interval = 0

        with patch("aiohttp.ClientSession", side_effect=OSError("offline")):
            await coord._check_license()  # Should not raise


# ── on_ev_cable_connected ─────────────────────────────────────────────────────


class TestOnEvCableConnected:
    @pytest.mark.asyncio
    async def test_no_ev_adapter_returns_early(self) -> None:
        """No ev_adapter → return immediately."""
        coord = _make_coord()
        coord.ev_adapter = None
        await coord.on_ev_cable_connected()  # Should not raise

    @pytest.mark.asyncio
    async def test_executor_disabled_returns_early(self) -> None:
        """executor_enabled=False → return immediately."""
        coord = _make_coord()
        coord.ev_adapter = MagicMock()
        coord.executor_enabled = False
        await coord.on_ev_cable_connected()  # Should not crash

    @pytest.mark.asyncio
    async def test_pv_surplus_starts_ev(self) -> None:
        """PV > 1kW → start EV charging at min amps."""
        coord = _make_coord()
        coord.ev_adapter = MagicMock()

        # Mock _collect_state to return PV=2kW
        from custom_components.carmabox.optimizer.models import CarmaboxState

        state = CarmaboxState(pv_power_w=2000.0)
        coord._collect_state = MagicMock(return_value=state)
        coord._cmd_ev_start = AsyncMock()

        await coord.on_ev_cable_connected()

        coord._cmd_ev_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_pv_surplus_no_ev_start(self) -> None:
        """PV < 1kW → no EV start, log and wait."""
        coord = _make_coord()
        coord.ev_adapter = MagicMock()

        from custom_components.carmabox.optimizer.models import CarmaboxState

        state = CarmaboxState(pv_power_w=500.0)
        coord._collect_state = MagicMock(return_value=state)
        coord._cmd_ev_start = AsyncMock()

        await coord.on_ev_cable_connected()

        coord._cmd_ev_start.assert_not_awaited()


# ── _async_restore_savings ────────────────────────────────────────────────────


class TestAsyncRestoreSavings:
    @pytest.mark.asyncio
    async def test_no_stored_data_keeps_fresh_savings(self) -> None:
        """No stored data → savings unchanged from init."""
        coord = _make_coord()
        coord._savings_store.async_load = AsyncMock(return_value=None)
        original_month = coord.savings.month

        await coord._async_restore_savings()

        assert coord.savings.month == original_month

    @pytest.mark.asyncio
    async def test_restores_current_month_data(self) -> None:
        """Valid stored data → savings restored."""
        from custom_components.carmabox.optimizer.savings import state_to_dict

        coord = _make_coord()
        sample = SavingsState(month=3, year=2026)
        sample.discharge_savings_kr = 12.5
        data = state_to_dict(sample)
        data["_last_save_ts"] = datetime.now().isoformat()

        coord._savings_store.async_load = AsyncMock(return_value=data)
        await coord._async_restore_savings()

        assert coord.savings.discharge_savings_kr == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_stale_data_resets_savings(self) -> None:
        """Data > 30 days old → savings reset to zero."""
        from custom_components.carmabox.optimizer.savings import state_to_dict

        coord = _make_coord()
        sample = SavingsState(month=1, year=2026)
        sample.discharge_savings_kr = 100.0
        data = state_to_dict(sample)
        old_ts = (datetime.now() - timedelta(days=45)).isoformat()
        data["_last_save_ts"] = old_ts

        coord._savings_store.async_load = AsyncMock(return_value=data)
        await coord._async_restore_savings()

        # After stale reset, savings should be zeroed
        assert coord.savings.discharge_savings_kr == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_executor_off_too_long_resets_savings(self) -> None:
        """Executor disabled for >24h → savings reset."""
        from custom_components.carmabox.optimizer.savings import state_to_dict

        coord = _make_coord()
        sample = SavingsState(month=3, year=2026)
        sample.discharge_savings_kr = 50.0
        data = state_to_dict(sample)
        # Recent save but executor was disabled
        data["_last_save_ts"] = (datetime.now() - timedelta(hours=30)).isoformat()
        data["_executor_enabled"] = False

        coord._savings_store.async_load = AsyncMock(return_value=data)
        await coord._async_restore_savings()

        assert coord.savings.discharge_savings_kr == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_invalid_timestamp_proceeds_normally(self) -> None:
        """Unparseable timestamp → fallback continues without reset."""
        from custom_components.carmabox.optimizer.savings import state_to_dict

        coord = _make_coord()
        sample = SavingsState(month=3, year=2026)
        sample.discharge_savings_kr = 7.0
        data = state_to_dict(sample)
        data["_last_save_ts"] = "NOT_A_DATE"

        coord._savings_store.async_load = AsyncMock(return_value=data)
        await coord._async_restore_savings()

        assert coord.savings.discharge_savings_kr == pytest.approx(7.0)

    @pytest.mark.asyncio
    async def test_store_exception_handled(self) -> None:
        """Store raises exception → warning logged, not re-raised."""
        coord = _make_coord()
        coord._savings_store.async_load = AsyncMock(side_effect=RuntimeError("store error"))
        await coord._async_restore_savings()  # Should not raise


# ── _async_save_savings ───────────────────────────────────────────────────────


class TestAsyncSaveSavings:
    @pytest.mark.asyncio
    async def test_rate_limited_skip(self) -> None:
        """Called within rate limit window → no save."""
        coord = _make_coord()
        coord._savings_last_save = 1e12  # Very recent

        await coord._async_save_savings()

        coord._savings_store.async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_saves_when_interval_elapsed(self) -> None:
        """Interval elapsed → savings stored."""
        coord = _make_coord()
        coord._savings_last_save = 0.0

        await coord._async_save_savings()

        coord._savings_store.async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_exception_handled(self) -> None:
        """Save raises → debug logged, not re-raised."""
        coord = _make_coord()
        coord._savings_last_save = 0.0
        coord._savings_store.async_save = AsyncMock(side_effect=OSError("disk full"))

        await coord._async_save_savings()  # Should not raise


# ── _async_restore_consumption ────────────────────────────────────────────────


class TestAsyncRestoreConsumption:
    @pytest.mark.asyncio
    async def test_no_data_keeps_default_profile(self) -> None:
        """No stored data → consumption_profile unchanged."""
        coord = _make_coord()
        coord._consumption_store.async_load = AsyncMock(return_value=None)

        await coord._async_restore_consumption()

        assert coord.consumption_profile is not None

    @pytest.mark.asyncio
    async def test_restores_profile_from_store(self) -> None:
        """Valid stored profile → restored."""
        coord = _make_coord()
        profile = ConsumptionProfile()
        profile.samples_weekday = 42
        data = profile.to_dict()
        coord._consumption_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_consumption()

        assert coord.consumption_profile.samples_weekday == 42

    @pytest.mark.asyncio
    async def test_migrates_from_config_entry(self) -> None:
        """No store data but config entry has profile → migrate."""
        coord = _make_coord()
        profile = ConsumptionProfile()
        profile.samples_weekday = 10
        coord._cfg = {"consumption_profile": profile.to_dict()}
        coord._consumption_store.async_load = AsyncMock(return_value=None)

        await coord._async_restore_consumption()

        assert coord.consumption_profile.samples_weekday == 10

    @pytest.mark.asyncio
    async def test_store_exception_handled(self) -> None:
        """Exception → warning logged, not re-raised."""
        coord = _make_coord()
        coord._consumption_store.async_load = AsyncMock(side_effect=RuntimeError("crash"))
        await coord._async_restore_consumption()  # Should not raise


# ── _async_restore_predictor ──────────────────────────────────────────────────


class TestAsyncRestorePredictor:
    @pytest.mark.asyncio
    async def test_no_data_keeps_default_predictor(self) -> None:
        """No data → predictor unchanged."""
        coord = _make_coord()
        await coord._async_restore_predictor()
        assert coord.predictor is not None

    @pytest.mark.asyncio
    async def test_restores_predictor_state(self) -> None:
        """Valid data → predictor restored."""
        coord = _make_coord()
        p = ConsumptionPredictor()
        data = p.to_dict()
        coord._predictor_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_predictor()

        assert coord.predictor is not None

    @pytest.mark.asyncio
    async def test_exception_handled(self) -> None:
        """Exception → warning logged, not re-raised."""
        coord = _make_coord()
        coord._predictor_store.async_load = AsyncMock(side_effect=RuntimeError("fail"))
        await coord._async_restore_predictor()  # Should not raise


# ── _async_restore_runtime ────────────────────────────────────────────────────


class TestAsyncRestoreRuntime:
    @pytest.mark.asyncio
    async def test_no_data_keeps_defaults(self) -> None:
        """No stored data → defaults unchanged."""
        coord = _make_coord()
        await coord._async_restore_runtime()
        assert coord.plan == []

    @pytest.mark.asyncio
    async def test_restores_plan_and_command(self) -> None:
        """Valid runtime data → plan and last_command restored."""
        coord = _make_coord()
        data = {
            "plan": [
                {
                    "hour": 10,
                    "action": "c",
                    "battery_kw": 3.0,
                    "grid_kw": 2.0,
                    "weighted_kw": 2.0,
                    "pv_kw": 1.0,
                    "consumption_kw": 2.0,
                    "ev_kw": 0.0,
                    "ev_soc": 0,
                    "battery_soc": 60,
                    "price": 80.0,
                },
            ],
            "last_command": "CHARGE_PV",
            "ev_enabled": True,
            "ev_current_amps": 16,
            "miner_on": True,
            "night_ev_active": True,
            "ellevio_hour_samples": [[1.5, 1.0], [2.0, 0.5]],
            "ellevio_monthly_hourly_peaks": [1.2, 1.8, 2.1],
        }
        coord._runtime_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_runtime()

        assert len(coord.plan) == 1
        assert coord.plan[0].hour == 10
        assert coord._last_command == BatteryCommand.CHARGE_PV
        assert coord._ev_enabled is True
        assert coord._miner_on is True
        assert coord._night_ev_active is True
        assert len(coord._ellevio_hour_samples) == 2
        assert len(coord._ellevio_monthly_hourly_peaks) == 3

    @pytest.mark.asyncio
    async def test_restores_hysteresis_state(self) -> None:
        """Surplus hysteresis state → restored."""
        coord = _make_coord()
        data = {
            "plan": [],
            "last_command": "STANDBY",
            "ev_enabled": False,
            "ev_current_amps": 6,
            "miner_on": False,
            "night_ev_active": False,
            "ellevio_hour_samples": [],
            "ellevio_monthly_hourly_peaks": [],
            "surplus_hysteresis": {
                "above": {"miner": 1711929600.0},
                "below": {},
            },
        }
        coord._runtime_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_runtime()

        assert coord._surplus_hysteresis is not None
        assert "miner" in coord._surplus_hysteresis.surplus_above_since

    @pytest.mark.asyncio
    async def test_invalid_command_falls_back_to_standby(self) -> None:
        """Unknown command string → BatteryCommand.STANDBY fallback."""
        coord = _make_coord()
        data = {
            "plan": [],
            "last_command": "INVALID_CMD",
            "ev_enabled": False,
            "ev_current_amps": 6,
            "miner_on": False,
            "night_ev_active": False,
            "ellevio_hour_samples": [],
            "ellevio_monthly_hourly_peaks": [],
        }
        coord._runtime_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_runtime()

        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_exception_handled(self) -> None:
        """Exception → warning logged, not re-raised."""
        coord = _make_coord()
        coord._runtime_store.async_load = AsyncMock(side_effect=RuntimeError("crash"))
        await coord._async_restore_runtime()  # Should not raise


# ── _async_save_runtime ───────────────────────────────────────────────────────


class TestAsyncSaveRuntime:
    @pytest.mark.asyncio
    async def test_saves_plan_and_state(self) -> None:
        """Save writes plan + EV + miner state to store."""
        coord = _make_coord()
        coord.plan = [
            HourPlan(
                hour=10,
                action="c",
                battery_kw=3.0,
                grid_kw=2.0,
                weighted_kw=2.0,
                pv_kw=1.0,
                consumption_kw=2.0,
                ev_kw=0.0,
                ev_soc=0,
                battery_soc=60,
                price=80.0,
            )
        ]
        coord._ev_enabled = True
        coord._miner_on = False

        await coord._async_save_runtime()

        coord._runtime_store.async_save.assert_awaited_once()
        call_data = coord._runtime_store.async_save.call_args[0][0]
        assert len(call_data["plan"]) == 1
        assert call_data["ev_enabled"] is True

    @pytest.mark.asyncio
    async def test_saves_hysteresis_when_present(self) -> None:
        """Surplus hysteresis saved when set."""
        from custom_components.carmabox.core.surplus_chain import HysteresisState

        coord = _make_coord()
        hyst = HysteresisState()
        hyst.surplus_above_since["miner"] = 1711929600.0
        coord._surplus_hysteresis = hyst

        await coord._async_save_runtime()

        call_data = coord._runtime_store.async_save.call_args[0][0]
        assert "surplus_hysteresis" in call_data
        assert "miner" in call_data["surplus_hysteresis"]["above"]

    @pytest.mark.asyncio
    async def test_exception_handled(self) -> None:
        """Save raises → debug logged, not re-raised."""
        coord = _make_coord()
        coord._runtime_store.async_save = AsyncMock(side_effect=OSError("fail"))
        await coord._async_save_runtime()  # Should not raise


# ── _async_restore_ledger ─────────────────────────────────────────────────────


class TestAsyncRestoreLedger:
    @pytest.mark.asyncio
    async def test_no_data_keeps_default_ledger(self) -> None:
        """No stored data → ledger unchanged."""
        coord = _make_coord()
        await coord._async_restore_ledger()
        assert coord.ledger is not None

    @pytest.mark.asyncio
    async def test_restores_ledger_from_store(self) -> None:
        """Valid ledger data → restored."""
        coord = _make_coord()
        ledger = EnergyLedger()
        data = ledger.to_dict()
        coord._ledger_store.async_load = AsyncMock(return_value=data)

        await coord._async_restore_ledger()

        assert coord.ledger is not None

    @pytest.mark.asyncio
    async def test_exception_handled(self) -> None:
        """Exception → warning logged, not re-raised."""
        coord = _make_coord()
        coord._ledger_store.async_load = AsyncMock(side_effect=RuntimeError("broken"))
        await coord._async_restore_ledger()  # Should not raise


# ── _async_save_ledger ────────────────────────────────────────────────────────


class TestAsyncSaveLedger:
    @pytest.mark.asyncio
    async def test_rate_limited_skip(self) -> None:
        """Within rate limit → no save."""
        coord = _make_coord()
        coord._ledger_last_save = 1e12

        await coord._async_save_ledger()

        coord._ledger_store.async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_saves_when_interval_elapsed(self) -> None:
        """Interval elapsed → ledger saved."""
        coord = _make_coord()
        coord._ledger_last_save = 0.0

        await coord._async_save_ledger()

        coord._ledger_store.async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_exception_handled(self) -> None:
        """Exception → debug logged, not re-raised."""
        coord = _make_coord()
        coord._ledger_last_save = 0.0
        coord._ledger_store.async_save = AsyncMock(side_effect=OSError("disk"))
        await coord._async_save_ledger()  # Should not raise


# ── _async_save_predictor ─────────────────────────────────────────────────────


class TestAsyncSavePredictor:
    @pytest.mark.asyncio
    async def test_rate_limited_skip(self) -> None:
        """Within rate limit → no save."""
        coord = _make_coord()
        coord._predictor_last_save = 1e12

        await coord._async_save_predictor()

        coord._predictor_store.async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_saves_when_interval_elapsed(self) -> None:
        """Interval elapsed → predictor saved."""
        coord = _make_coord()
        coord._predictor_last_save = 0.0

        await coord._async_save_predictor()

        coord._predictor_store.async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_exception_handled(self) -> None:
        """Exception → debug logged, not re-raised."""
        coord = _make_coord()
        coord._predictor_last_save = 0.0
        coord._predictor_store.async_save = AsyncMock(side_effect=OSError("fail"))
        await coord._async_save_predictor()  # Should not raise


# ── _async_fetch_benchmarking ─────────────────────────────────────────────────


class TestAsyncFetchBenchmarking:
    @pytest.mark.asyncio
    async def test_rate_limited_skip(self) -> None:
        """Within 1-hour rate limit → no hub call."""
        coord = _make_coord()
        coord._benchmark_last_fetch = 1e12  # Very recent

        hub = AsyncMock()
        coord._hub = hub

        await coord._async_fetch_benchmarking()

        hub.fetch_benchmarking.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_hub_skips_fetch(self) -> None:
        """No _hub attribute → return without fetch."""
        coord = _make_coord()
        coord._benchmark_last_fetch = 0.0
        # No _hub set

        await coord._async_fetch_benchmarking()  # Should not raise

    @pytest.mark.asyncio
    async def test_fetches_data_when_interval_elapsed(self) -> None:
        """Interval elapsed + hub available → fetch called."""
        coord = _make_coord()
        coord._benchmark_last_fetch = 0.0
        coord.benchmark_data = None

        hub = MagicMock()
        hub.fetch_benchmarking = AsyncMock(return_value={"avg_kw": 1.5})
        coord._hub = hub

        await coord._async_fetch_benchmarking()

        hub.fetch_benchmarking.assert_awaited_once()
        assert coord.benchmark_data == {"avg_kw": 1.5}

    @pytest.mark.asyncio
    async def test_hub_returns_none_no_update(self) -> None:
        """Hub returns None → benchmark_data not updated."""
        coord = _make_coord()
        coord._benchmark_last_fetch = 0.0
        coord.benchmark_data = {"old": "data"}

        hub = MagicMock()
        hub.fetch_benchmarking = AsyncMock(return_value=None)
        coord._hub = hub

        await coord._async_fetch_benchmarking()

        assert coord.benchmark_data == {"old": "data"}
