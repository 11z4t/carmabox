"""Coverage tests for coordinator.py remaining gaps — batch 25.

Targets:
  coordinator.py: 685, 797-798, 995-996, 3863-3865, 3999-4000, 4015-4019
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
    coord._license_features = ["executor"]  # Feature enabled by default
    return coord


def _make_state(entity_id: str, state_val: str = "ok") -> MagicMock:
    s = MagicMock()
    s.entity_id = entity_id
    s.state = state_val
    s.attributes = {}
    return s


# ══════════════════════════════════════════════════════════════════════════════
# Sync utilities
# ══════════════════════════════════════════════════════════════════════════════


class TestReadFloatOrNoneSuccess:
    """Line 685: _read_float_or_none normal success path (valid float ≤ 100000)."""

    def test_valid_float_returned(self) -> None:
        """Non-extreme valid state → return float (line 685)."""
        coord = _make_coordinator()
        s = _make_state("sensor.power", "1234.5")
        coord.hass.states.get = MagicMock(return_value=s)
        result = coord._read_float_or_none("sensor.power")
        assert result == 1234.5


# ══════════════════════════════════════════════════════════════════════════════
# Async save/fetch with exceptions
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncSaveConsumptionException:
    """Lines 797-798: _async_save_consumption try block exception path."""

    @pytest.mark.asyncio
    async def test_save_exception_logged(self) -> None:
        """async_save raises → except caught → line 797-798."""
        from custom_components.carmabox.optimizer.consumption import ConsumptionProfile

        coord = _make_coordinator()
        coord._consumption_last_save = 0.0  # Old enough to pass rate limit
        coord._consumption_store = MagicMock()
        coord._consumption_store.async_save = AsyncMock(side_effect=OSError("disk full"))
        coord.consumption_profile = ConsumptionProfile()
        # Should not raise — exception caught internally
        await coord._async_save_consumption()


class TestAsyncFetchBenchmarkingException:
    """Lines 995-996: _async_fetch_benchmarking exception handler."""

    @pytest.mark.asyncio
    async def test_fetch_exception_logged(self) -> None:
        """hub.fetch_benchmarking raises → except caught → lines 995-996."""
        coord = _make_coordinator()
        coord._benchmark_last_fetch = 0.0  # Long enough ago to pass rate limit
        coord._hub = MagicMock()
        coord._hub.fetch_benchmarking = AsyncMock(side_effect=ConnectionError("timeout"))
        # Should not raise — exception caught internally
        await coord._async_fetch_benchmarking()


# ══════════════════════════════════════════════════════════════════════════════
# Async execute methods — auto-detect paths
# ══════════════════════════════════════════════════════════════════════════════


class TestExecuteClimateAutoDetect:
    """Lines 3863-3865: _execute_climate auto-detects climate entity."""

    @pytest.mark.asyncio
    async def test_climate_autodetect_matching_entity(self) -> None:
        """hass.states.async_all returns climate entity with 'vp' → auto-detected (3863-3865)."""
        coord = _make_coordinator()
        coord._cfg = {}  # No climate_entity configured → triggers auto-detect

        climate_state = _make_state("climate.vp_unit", "cool")
        climate_state.attributes = {"current_temperature": 22.0}
        coord.hass.states.async_all = MagicMock(return_value=[climate_state])
        coord.hass.states.get = MagicMock(return_value=None)  # No actual climate state

        state = MagicMock()
        state.current_price = 80.0
        state.pv_power_w = 0.0
        state.grid_import_w = 500.0
        # Returns early at climate_state is None → doesn't call services
        await coord._execute_climate(state)


class TestPoolSwitchException:
    """Lines 3999-4000: _pool_switch exception handler."""

    @pytest.mark.asyncio
    async def test_pool_switch_exception_caught(self) -> None:
        """hass.services.async_call raises → except caught → lines 3999-4000."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("HA unavailable"))
        # Should not raise — exception caught internally
        await coord._pool_switch("switch.pool_pump", on=True)


class TestExecutePoolCirculationAutoDetect:
    """Lines 4015-4019: _execute_pool_circulation auto-detects cirk entity."""

    @pytest.mark.asyncio
    async def test_cirk_autodetect_matching_entity(self) -> None:
        """hass.states.async_all returns switch with 'cirk' + 'pool' → auto-detected (4015-4019)."""
        coord = _make_coordinator()
        coord._cfg = {}  # No pool_circulation_entity configured

        cirk_switch = _make_state("switch.pool_cirk_pump", "off")
        coord.hass.states.async_all = MagicMock(return_value=[cirk_switch])
        coord.hass.states.get = MagicMock(return_value=None)  # No cirk state → early return

        state = MagicMock()
        await coord._execute_pool_circulation(state)
