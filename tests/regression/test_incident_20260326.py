"""Regression tests for incident 2026-03-26 (10.6 kW grid spike).

Root cause: Concurrent Modbus calls caused bus contention → EMS mode
not set correctly → crosscharge + fast_charging ON → 10.6 kW grid spike.

PLAT-1082: These tests ensure the fix (Modbus serialization + invariant
enforcement) prevents recurrence.
"""

from __future__ import annotations

import asyncio
import unittest.mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.carmabox.adapters.goodwe import GoodWeAdapter
from custom_components.carmabox.core.grid_guard import BatteryState, GridGuard, GridGuardConfig
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    """Create mock hass with states."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state

    hass.states.get = lambda eid: states.get(eid)
    hass.services.async_call = AsyncMock()
    return hass


class TestNoFastChargeDuringDischarge:
    """INV-3: fast_charging must NEVER be ON when battery is discharging."""

    @pytest.mark.asyncio
    async def test_fast_charging_blocked_without_authorization(self) -> None:
        """fast_charging ON is blocked unless authorized=True (INV-3 hard lock)."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        # Reset class-level lock for test isolation
        GoodWeAdapter._modbus_lock = None

        with (
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            result = await adapter.set_fast_charging(on=True)

        # Should have been forced OFF (INV-3 blocks unauthorized)
        call = hass.services.async_call.call_args_list[0]
        assert call[0][1] == "turn_off"  # Forced to turn_off
        assert result is True  # Succeeds (as turn_off)

    @pytest.mark.asyncio
    async def test_fast_charging_allowed_with_authorization(self) -> None:
        """fast_charging ON allowed when explicitly authorized."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        GoodWeAdapter._modbus_lock = None

        with (
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            result = await adapter.set_fast_charging(
                on=True,
                power_pct=80,
                soc_target=90,
                authorized=True,
            )

        assert result is True
        calls = hass.services.async_call.call_args_list
        assert calls[0][0][1] == "turn_on"

    def test_grid_guard_detects_crosscharge_during_fast_charging(self) -> None:
        """Grid Guard INV-2 must detect crosscharge even with fast_charging."""
        guard = GridGuard(GridGuardConfig())
        batteries = [
            BatteryState(
                id="kontor",
                soc=50,
                power_w=-1500,  # charging
                cell_temp_c=20,
                ems_mode="charge_pv",
                fast_charging_on=True,
                available_kwh=5.0,
            ),
            BatteryState(
                id="forrad",
                soc=80,
                power_w=2000,  # discharging
                cell_temp_c=20,
                ems_mode="discharge_pv",
                fast_charging_on=False,
                available_kwh=8.0,
            ),
        ]
        result = guard._check_invariants(batteries, fast_charge_authorized=False)
        inv2_violations = [v for v in result.invariant_violations if "INV-2" in v]
        assert len(inv2_violations) >= 1, "INV-2 crosscharge not detected"


class TestExecutorFollowsPlan:
    """Executor must execute the plan's EMS mode, not invent its own."""

    @pytest.mark.asyncio
    async def test_executor_sets_planned_mode(self) -> None:
        """When plan says discharge_pv, adapter must set discharge_pv."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        GoodWeAdapter._modbus_lock = None

        with (
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            result = await adapter.set_ems_mode("discharge_pv")

        assert result is True
        # set_ems_mode now writes BOTH legacy input_select (desired_mode)
        # AND select (ems_mode) — 2 calls expected (PLAT-1134 dual-write)
        calls = hass.services.async_call.call_args_list
        assert len(calls) == 2
        # First: legacy desired_mode
        assert calls[0] == unittest.mock.call(
            "input_select",
            "select_option",
            {"entity_id": "input_select.goodwe_kontor_desired_mode", "option": "discharge"},
        )
        # Second: actual EMS mode
        assert calls[1] == unittest.mock.call(
            "select",
            "select_option",
            {"entity_id": "select.goodwe_kontor_ems_mode", "option": "discharge_pv"},
        )

    @pytest.mark.asyncio
    async def test_executor_rejects_invalid_mode(self) -> None:
        """Adapter rejects unknown EMS modes (safety valve)."""
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        result = await adapter.set_ems_mode("turbo_mode")
        assert result is False
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_executor_rejects_auto_mode(self) -> None:
        """'auto' is in VALID_EMS_MODES but should be caught by higher layers.

        At adapter level it is technically valid — but callers must enforce
        the MANIFEST prohibition. This test documents current behavior.
        """
        hass = _make_hass()
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        GoodWeAdapter._modbus_lock = None

        with (
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            result = await adapter.set_ems_mode("auto")

        # auto is technically valid at adapter level
        assert result is True


class TestPlanReceivesCorrectSoC:
    """Plan must receive real SoC, not stale/cached values."""

    def test_soc_reads_current_state(self) -> None:
        """SoC reads HA state directly (no caching)."""
        hass = _make_hass(("sensor.pv_battery_soc_kontor", "72.5"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == 72.5

        # Simulate state change
        new_state = MagicMock()
        new_state.state = "65.0"
        new_state.attributes = {}
        hass.states.get = lambda eid: new_state if "soc" in eid else None

        assert adapter.soc == 65.0  # Reads new value, not cached

    def test_soc_unavailable_returns_sentinel(self) -> None:
        """Unavailable SoC returns -1 (sentinel), not 0 or stale value."""
        hass = _make_hass(("sensor.pv_battery_soc_kontor", "unavailable"))
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == -1.0


class TestPredictNoneFallback:
    """Predictor must handle None/missing data without crashing."""

    def test_predict_with_insufficient_data(self) -> None:
        """With no training data, predictor returns fallback value."""
        predictor = ConsumptionPredictor()
        result = predictor.predict_hour(weekday=0, hour=14, month=3)
        assert result == 2.0  # Default fallback

    def test_predict_24h_with_no_data(self) -> None:
        """24h prediction with no training data returns fallback profile."""
        predictor = ConsumptionPredictor()
        fallback = [1.5] * 24
        result = predictor.predict_24h(
            start_hour=0,
            weekday=0,
            month=3,
            fallback_profile=fallback,
        )
        assert len(result) == 24
        assert all(v == 1.5 for v in result)


class TestSolcastMissingAttribute:
    """Solcast adapter must handle missing forecast attributes gracefully."""

    def test_today_kwh_unavailable(self) -> None:
        """Missing today sensor returns 0.0, not crash."""
        from custom_components.carmabox.adapters.solcast import SolcastAdapter

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = SolcastAdapter(hass)
        assert adapter.today_kwh == 0.0

    def test_tomorrow_kwh_unavailable(self) -> None:
        """Missing tomorrow sensor returns 0.0."""
        from custom_components.carmabox.adapters.solcast import SolcastAdapter

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = SolcastAdapter(hass)
        assert adapter.tomorrow_kwh == 0.0

    def test_today_hourly_unavailable(self) -> None:
        """Missing hourly forecast returns safe fallback, not crash."""
        from custom_components.carmabox.adapters.solcast import SolcastAdapter

        hass = MagicMock()
        state = MagicMock()
        state.state = "unavailable"
        state.attributes = {}
        hass.states.get = MagicMock(return_value=state)
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        # Must not crash; returns either empty or 24 zeros
        assert isinstance(result, list)
        assert all(v == 0.0 for v in result)


class TestCoordinatorInitCompletes:
    """Coordinator init must complete even with degraded adapters."""

    @pytest.mark.asyncio
    async def test_modbus_lock_created_on_first_use(self) -> None:
        """Modbus lock is created lazily (safe for any event loop)."""
        GoodWeAdapter._modbus_lock = None
        lock = GoodWeAdapter._get_modbus_lock()
        assert isinstance(lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_modbus_lock_shared_across_instances(self) -> None:
        """Both kontor and forrad adapters share the same Modbus lock."""
        GoodWeAdapter._modbus_lock = None
        hass = _make_hass()
        a1 = GoodWeAdapter(hass, "dev1", "kontor")
        a2 = GoodWeAdapter(hass, "dev2", "forrad")
        lock1 = a1._get_modbus_lock()
        lock2 = a2._get_modbus_lock()
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_adapter_initializes_with_unavailable_entities(self) -> None:
        """Adapter init succeeds even when all entities are unavailable."""
        hass = _make_hass()  # No entities
        adapter = GoodWeAdapter(hass, "dev1", "kontor")
        assert adapter.soc == -1.0
        assert adapter.power_w == 0.0
        assert adapter.ems_mode == ""
        assert adapter.temperature_c is None
        assert adapter.fast_charging_on is False


class TestModbusSerializationRegression:
    """PLAT-1082: Verify concurrent Modbus calls are serialized."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_serialized(self) -> None:
        """Two adapters calling simultaneously must be serialized (no overlap)."""
        GoodWeAdapter._modbus_lock = None
        active_count = 0
        max_concurrent = 0

        async def mock_call(domain: str, service: str, data: dict) -> None:
            nonlocal active_count, max_concurrent
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
            await asyncio.sleep(0.05)
            active_count -= 1

        hass = _make_hass()
        hass.services.async_call = AsyncMock(side_effect=mock_call)

        a1 = GoodWeAdapter(hass, "dev1", "kontor")
        a2 = GoodWeAdapter(hass, "dev2", "forrad")

        with (
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            await asyncio.gather(
                a1.set_discharge_limit(500),
                a2.set_discharge_limit(700),
            )

        assert hass.services.async_call.call_count == 2
        assert (
            max_concurrent <= 1
        ), f"Max {max_concurrent} concurrent Modbus calls — must be serialized!"

    @pytest.mark.asyncio
    async def test_modbus_timeout_retries_once(self) -> None:
        """Modbus timeout retries exactly 1 time then fails."""
        GoodWeAdapter._modbus_lock = None
        hass = _make_hass()
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("Modbus timeout"),
        )
        adapter = GoodWeAdapter(hass, "dev1", "kontor")

        with (
            patch("custom_components.carmabox.adapters.goodwe._RETRY_DELAY_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._ADAPTER_RATE_LIMIT_S", 0),
            patch("custom_components.carmabox.adapters.goodwe._MODBUS_MIN_INTERVAL_S", 0),
        ):
            result = await adapter.set_ems_mode("charge_pv")

        assert result is False
        # 1 legacy input_select (best-effort, suppressed) + 2 select retries = 3
        assert hass.services.async_call.call_count == 3
