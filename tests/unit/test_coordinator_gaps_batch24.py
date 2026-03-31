"""Coverage tests for coordinator.py remaining small gaps — batch 24.

Targets:
  coordinator.py: 572-575, 624-625, 681-687, 793, 3845, 3934, 3997
"""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState


def _make_coordinator(options: dict | None = None):
    """Minimal coordinator via __new__, sets required attributes."""
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
    coord._license_features = []  # No license by default
    return coord


# ══════════════════════════════════════════════════════════════════════════════
# Synchronous utility methods
# ══════════════════════════════════════════════════════════════════════════════


class TestCableLocked:
    """Lines 572-575: cable_locked_entity property."""

    def test_returns_entity_when_ev_prefix_set(self) -> None:
        """ev_prefix present → return binary_sensor entity (lines 572-574)."""
        coord = _make_coordinator()
        coord._cfg = {"ev_prefix": "easee_home"}
        assert coord.cable_locked_entity == "binary_sensor.easee_home_plug"

    def test_returns_empty_when_no_ev_prefix(self) -> None:
        """No ev_prefix → return '' (lines 572-573, 575)."""
        coord = _make_coordinator()
        coord._cfg = {}
        assert coord.cable_locked_entity == ""


class TestReadCellTemp:
    """Lines 624-625: _read_cell_temp ValueError → pass → return None."""

    def test_invalid_state_returns_none(self) -> None:
        """State is non-numeric string → ValueError caught, return None (624-625)."""
        coord = _make_coordinator()
        s = MagicMock()
        s.state = "not_a_number"
        coord.hass.states.get = MagicMock(return_value=s)
        result = coord._read_cell_temp("left")
        assert result is None


class TestReadFloatOrNone:
    """Lines 681-687: _read_float_or_none extreme value and ValueError paths."""

    def test_extreme_value_returns_none(self) -> None:
        """abs(val) > 100000 → return None (lines 681-684)."""
        coord = _make_coordinator()
        s = MagicMock()
        s.state = "200000.0"  # > 100000
        coord.hass.states.get = MagicMock(return_value=s)
        result = coord._read_float_or_none("sensor.test")
        assert result is None

    def test_invalid_float_returns_none(self) -> None:
        """Non-numeric state → ValueError → return None (lines 681-682, 686-687)."""
        coord = _make_coordinator()
        s = MagicMock()
        s.state = "bad_value"
        coord.hass.states.get = MagicMock(return_value=s)
        result = coord._read_float_or_none("sensor.test")
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# Async early-returns
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncSaveConsumptionRateLimit:
    """Line 793: rate-limited early return in _async_save_consumption."""

    @pytest.mark.asyncio
    async def test_rate_limited_returns_early(self) -> None:
        """_consumption_last_save recent → return immediately (line 793)."""
        coord = _make_coordinator()
        coord._consumption_last_save = time.monotonic()  # Just saved
        # Should return immediately without touching _consumption_store
        await coord._async_save_consumption()
        # No assertion needed — the method must not AttributeError on missing store


class TestExecuteClimateNoFeature:
    """Line 3845: _execute_climate returns early when executor feature missing."""

    @pytest.mark.asyncio
    async def test_no_executor_feature_returns_early(self) -> None:
        """_license_features=[] → _has_feature('executor')=False → return (line 3845)."""
        coord = _make_coordinator()
        coord._license_features = []  # No executor
        state = MagicMock()
        await coord._execute_climate(state)  # Must return early without any HA calls
        coord.hass.services.async_call.assert_not_called()


class TestExecutePoolNoFeature:
    """Line 3934: _execute_pool returns early when executor feature missing."""

    @pytest.mark.asyncio
    async def test_no_executor_feature_returns_early(self) -> None:
        """_license_features=[] → return early (line 3934)."""
        coord = _make_coordinator()
        coord._license_features = []
        state = MagicMock()
        await coord._execute_pool(state)
        coord.hass.services.async_call.assert_not_called()


class TestExecutePoolCirculationNoFeature:
    """Line 3997: _execute_pool_circulation returns early without executor feature."""

    @pytest.mark.asyncio
    async def test_no_executor_feature_returns_early(self) -> None:
        """_license_features=[] → return early (line 3997)."""
        coord = _make_coordinator()
        coord._license_features = []
        state = MagicMock()
        await coord._execute_pool_circulation(state)
        coord.hass.services.async_call.assert_not_called()
