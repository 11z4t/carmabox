"""Coverage tests for coordinator.py remaining gaps — batch 26.

Targets:
  coordinator.py: 3941-3942, 4674-4676, 4706-4711, 4719-4723, 4739-4745
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState


def _make_coordinator(options: dict | None = None):
    """Minimal coordinator via __new__."""
    from custom_components.carmabox.coordinator import CarmaboxCoordinator, Decision
    from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
    from custom_components.carmabox.optimizer.models import ShadowComparison

    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(return_value=None)

    entry = MagicMock()
    entry.options = options or {}
    entry.data = dict(entry.options)

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.entry = entry
    coord._cfg = {**entry.data}
    coord.safety = MagicMock()
    coord.safety.check_heartbeat = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.plan = []
    coord._plan_counter = 0
    coord._last_command = MagicMock()
    coord._last_battery_action = "charge_pv"
    coord._last_discharge_w = 0
    coord._pending_write_verifies = []
    coord.target_kw = 2.0
    coord.min_soc = 15.0
    coord.logger = MagicMock()
    coord.name = "carmabox"
    coord.savings = SavingsState(month=3, year=2026)
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord._daily_discharge_kwh = 0.0
    coord._daily_safety_blocks = 0
    coord._daily_plans = 0
    coord._current_date = "2026-03-18"
    coord._daily_avg_price = 80.0
    coord._avg_price_initialized = True
    coord.notifier = MagicMock()
    coord.notifier.crosscharge_alert = AsyncMock()
    coord._runtime_loaded = True
    coord._ledger_loaded = True
    coord.inverter_adapters = []
    coord.ev_adapter = None
    coord.last_decision = Decision()
    coord.decision_log = deque(maxlen=48)
    coord.consumption_profile = ConsumptionProfile()
    coord.hourly_actuals = []
    coord._last_tracked_hour = -1
    coord._plan_deviation_count = 0
    coord._plan_last_correction_time = 0.0
    coord._ellevio_hour_samples = []
    coord._ellevio_current_hour = -1
    coord._ellevio_monthly_hourly_peaks = []
    coord.shadow = ShadowComparison()
    coord.shadow_log = []
    coord._shadow_savings_kr = 0.0
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}
    coord._ev_enabled = False
    coord._ev_current_amps = 0
    coord._ev_last_ramp_time = 0.0
    coord._ev_initialized = True
    coord.benchmark_data = {}
    coord._license_features = ["executor"]
    return coord


def _make_state(entity_id: str, state_val: str = "ok") -> MagicMock:
    s = MagicMock()
    s.entity_id = entity_id
    s.state = state_val
    s.attributes = {}
    return s


# ══════════════════════════════════════════════════════════════════════════════
# _climate_call exception handler (3941-3942)
# ══════════════════════════════════════════════════════════════════════════════


class TestClimateCallException:
    """Lines 3941-3942: _climate_call exception caught."""

    @pytest.mark.asyncio
    async def test_climate_call_exception_caught(self) -> None:
        """hass.services.async_call raises → except caught → lines 3941-3942."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("climate unavailable"))
        # Should not raise — exception caught internally
        await coord._climate_call("climate.vp", "off")


# ══════════════════════════════════════════════════════════════════════════════
# _execute_miner paths
# ══════════════════════════════════════════════════════════════════════════════


def _make_miner_state(
    *,
    total_battery_soc: float = 80.0,
    current_price: float = 50.0,
    grid_power_w: float = -500.0,
    is_exporting: bool = True,
    ev_soc: float = 80.0,
) -> MagicMock:
    s = MagicMock()
    s.total_battery_soc = total_battery_soc
    s.current_price = current_price
    s.grid_power_w = grid_power_w
    s.is_exporting = is_exporting
    s.ev_soc = ev_soc
    return s


class TestExecuteMinerLazyInit:
    """Lines 4674-4676: miner lazy-init uses hardcoded Shelly fallback."""

    @pytest.mark.asyncio
    async def test_hardcoded_shelly_fallback(self) -> None:
        """No config miner_entity + Shelly switch present → 4674-4676."""
        coord = _make_coordinator()
        coord._cfg = {}  # No miner_entity in config
        coord._miner_entity = ""  # Not yet resolved
        coord._miner_on = False
        coord._cmd_miner = AsyncMock()

        # _detect_miner_entity will find nothing (no appliances)
        coord._appliances = []
        coord.hass.states.async_all = MagicMock(return_value=[])

        # Hardcoded Shelly switch is available
        shelly_state = _make_state("switch.shelly1pmg4_a085e3bd1e60", "off")
        coord.hass.states.get = MagicMock(
            side_effect=lambda eid: shelly_state if "shelly" in eid else None
        )

        state = _make_miner_state(grid_power_w=0.0, is_exporting=False)
        await coord._execute_miner(state)
        # Resolved to hardcoded Shelly entity (line 4674) and logged (line 4676)
        assert coord._miner_entity == "switch.shelly1pmg4_a085e3bd1e60"


class TestExecuteMinerLowBatteryExpensivePrice:
    """Lines 4706-4711: low battery + expensive price → miner OFF logged."""

    @pytest.mark.asyncio
    async def test_low_battery_expensive_price_miner_off(self) -> None:
        """total_battery_soc < 30 + price > expensive + miner ON → lines 4706-4711."""
        coord = _make_coordinator()
        coord._miner_entity = "switch.test_miner"
        coord._miner_on = True  # Miner is currently ON
        coord._cmd_miner = AsyncMock()

        miner_state = _make_state("switch.test_miner", "on")
        coord.hass.states.get = MagicMock(return_value=miner_state)

        state = _make_miner_state(
            total_battery_soc=20.0,  # < 30
            current_price=200.0,  # > default expensive (100 öre)
        )
        await coord._execute_miner(state)
        coord._cmd_miner.assert_called_once_with(False)


class TestExecuteMinerGridImporting:
    """Lines 4719-4723: grid importing + miner ON → miner OFF logged."""

    @pytest.mark.asyncio
    async def test_grid_importing_miner_off(self) -> None:
        """grid_power_w > 0 (importing) + miner ON → lines 4719-4723."""
        coord = _make_coordinator()
        coord._miner_entity = "switch.test_miner"
        coord._miner_on = True  # Miner is currently ON
        coord._cmd_miner = AsyncMock()

        miner_state = _make_state("switch.test_miner", "on")
        coord.hass.states.get = MagicMock(return_value=miner_state)

        state = _make_miner_state(
            total_battery_soc=80.0,
            current_price=50.0,  # Not expensive
            grid_power_w=500.0,  # Importing (> 0) → miner OFF
            is_exporting=False,
        )
        await coord._execute_miner(state)
        coord._cmd_miner.assert_called_once_with(False)


class TestExecuteMinerEvChargingStop:
    """Lines 4739-4745: EV charging → miner OFF."""

    @pytest.mark.asyncio
    async def test_ev_charging_miner_off(self) -> None:
        """EV enabled + power > 100W + miner ON → lines 4739-4745."""
        coord = _make_coordinator()
        coord._miner_entity = "switch.test_miner"
        coord._miner_on = True
        coord._cmd_miner = AsyncMock()
        coord._ev_enabled = True

        coord.ev_adapter = MagicMock()
        coord.ev_adapter.power_w = 500.0  # EV charging > 100W

        miner_state = _make_state("switch.test_miner", "on")
        coord.hass.states.get = MagicMock(return_value=miner_state)

        state = _make_miner_state(
            total_battery_soc=80.0,
            current_price=50.0,
            # grid_power_w negative (exporting) but abs < DEFAULT_MINER_START_EXPORT_W (200)
            # so line 4727 doesn't return, and we reach the EV check at 4738
            grid_power_w=-100.0,
            is_exporting=True,
        )
        await coord._execute_miner(state)
        coord._cmd_miner.assert_called_once_with(False)
