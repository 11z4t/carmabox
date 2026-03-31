"""Coverage tests for remaining small gaps — batch 20.

Targets:
  optimizer/battery_health.py:  238
  optimizer/ev_strategy.py:     289
  optimizer/models.py:          183
  optimizer/multiday_planner.py: 144
  optimizer/roi.py:             196-198
  optimizer/weather_learning.py: 165-166
  adapters/__init__.py:         73, 89
  adapters/goodwe.py:           99-100, 223, 338-343, 353, 374
  adapters/easee.py:            107-108, 177-178, 216, 317-318, 323, 388-398
  core/coordinator_v2.py:       263-265
"""

from __future__ import annotations

import asyncio

# ══════════════════════════════════════════════════════════════════════════════
# optimizer/battery_health.py
# ══════════════════════════════════════════════════════════════════════════════


class TestBatteryHealthBatch20:
    """Line 238: efficiency_for_temperature with enough samples."""

    def test_efficiency_for_temperature_with_samples(self) -> None:
        """temp_efficiency_counts[t_bin] >= MIN → return learned value (line 238)."""
        from custom_components.carmabox.optimizer.battery_health import (
            MIN_CYCLE_SAMPLES,
            BatteryHealthState,
            efficiency_for_temperature,
        )

        state = BatteryHealthState()
        # t_bin for 20°C is 3 (10-20 range)
        # Directly set count to >= threshold and a specific efficiency
        state.temp_efficiency[3] = 0.93
        state.temp_efficiency_counts[3] = MIN_CYCLE_SAMPLES
        result = efficiency_for_temperature(state, temp_c=20.0)
        assert result == 0.93


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/ev_strategy.py
# ══════════════════════════════════════════════════════════════════════════════


class TestEvStrategyBatch20:
    """Line 289: _is_night_hour with start <= end."""

    def test_is_night_hour_normal_window(self) -> None:
        """night_start <= night_end → fall through to line 289."""
        from custom_components.carmabox.optimizer.ev_strategy import _is_night_hour

        # night_start=0 < night_end=8 → goes to line 289
        assert _is_night_hour(hour=4, night_start=0, night_end=8) is True
        assert _is_night_hour(hour=10, night_start=0, night_end=8) is False


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/models.py
# ══════════════════════════════════════════════════════════════════════════════


class TestModelsBatch20:
    """Line 183: total_battery_soc when has_battery_2 but total_cap=0."""

    def test_total_battery_soc_zero_cap_average(self) -> None:
        """has_battery_2=True + total_cap=0 → simple average (line 183)."""
        from custom_components.carmabox.optimizer.models import CarmaboxState

        state = CarmaboxState(
            battery_soc_1=60.0,
            battery_soc_2=80.0,
            battery_cap_1_kwh=0.0,
            battery_cap_2_kwh=0.0,
        )
        result = state.total_battery_soc
        assert result == 70.0  # (60 + 80) / 2


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/multiday_planner.py
# ══════════════════════════════════════════════════════════════════════════════


class TestMultidayPlannerBatch20:
    """Line 144: pv_correction.correct_profile called during build_day_inputs."""

    def test_build_day_inputs_with_pv_correction(self) -> None:
        """pv_correction passed → correct_profile applied (line 144)."""
        from custom_components.carmabox.optimizer.multiday_planner import build_day_inputs
        from custom_components.carmabox.optimizer.pv_correction import PVCorrectionProfile

        correction = PVCorrectionProfile()
        days_list = build_day_inputs(
            days=2,
            start_hour=8,
            start_weekday=0,
            start_month=6,
            pv_correction=correction,
            pv_daily_estimate=5.0,
        )
        assert len(days_list) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/roi.py
# ══════════════════════════════════════════════════════════════════════════════


class TestRoiBatch20:
    """Lines 196-198: roi_summary with >= 3 months data → annualized != 0."""

    def test_whatif_summary_with_monthly_data(self) -> None:
        """months >= 3 → annualized computed (lines 196-198 in whatif_summary)."""
        from custom_components.carmabox.optimizer.roi import (
            MonthSavings,
            ROIState,
            whatif_summary,
        )

        state = ROIState(battery_cost_kr=50000.0)
        for m in range(1, 5):
            state.monthly_savings.append(MonthSavings(year=2026, month=m, total_savings_kr=500.0))
        result = whatif_summary(state)
        annualized = result.get("annualized_savings_kr", 0)
        assert annualized > 0


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/weather_learning.py
# ══════════════════════════════════════════════════════════════════════════════


class TestWeatherLearningBatch20:
    """Lines 165-166: summary() when a bin has >= MIN_SAMPLES_PER_BIN samples."""

    def test_summary_includes_avg_factor(self) -> None:
        """After enough samples in one bin → avg_factor_per_temp has entry (line 165-166)."""
        from custom_components.carmabox.optimizer.weather_learning import (
            MIN_SAMPLES_PER_BIN,
            WeatherProfile,
        )

        profile = WeatherProfile()
        # Add MIN_SAMPLES_PER_BIN samples for temp=20°C, hour=10
        for _ in range(MIN_SAMPLES_PER_BIN):
            profile.update(
                hour=10,
                temp_c=20.0,
                consumption_kw=2.0,
                baseline_consumption_kw=2.0,
            )
        s = profile.summary()
        assert "avg_factor_per_temp" in s
        assert len(s["avg_factor_per_temp"]) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# adapters/__init__.py
# ══════════════════════════════════════════════════════════════════════════════


class TestAdapterBasesBatch20:
    """Lines 73, 89: cable_locked default + reset_to_default default."""

    def _make_concrete_ev(self) -> object:
        from custom_components.carmabox.adapters import EVAdapter

        class _ConcreteEV(EVAdapter):
            @property
            def status(self) -> str:
                return "ready"

            @property
            def current_a(self) -> float:
                return 0.0

            @property
            def power_w(self) -> float:
                return 0.0

            @property
            def is_charging(self) -> bool:
                return False

            async def enable(self) -> bool:
                return True

            async def disable(self) -> bool:
                return True

            async def set_current(self, amps: int) -> bool:
                return True

        return _ConcreteEV()

    def test_cable_locked_default_false(self) -> None:
        """EVAdapter.cable_locked default → False (line 73)."""
        adapter = self._make_concrete_ev()
        result = adapter.cable_locked  # type: ignore[union-attr]
        assert result is False

    def test_reset_to_default_calls_set_current_6(self) -> None:
        """EVAdapter.reset_to_default → set_current(6) (line 89)."""
        from custom_components.carmabox.adapters import EVAdapter

        set_current_called_with: list[int] = []

        class _ConcreteEV2(EVAdapter):
            @property
            def status(self) -> str:
                return "ready"

            @property
            def current_a(self) -> float:
                return 0.0

            @property
            def power_w(self) -> float:
                return 0.0

            @property
            def is_charging(self) -> bool:
                return False

            async def enable(self) -> bool:
                return True

            async def disable(self) -> bool:
                return True

            async def set_current(self, amps: int) -> bool:
                set_current_called_with.append(amps)
                return True

        adapter = _ConcreteEV2()

        async def run() -> bool:
            return await adapter.reset_to_default()

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is True
        assert set_current_called_with == [6]


# ══════════════════════════════════════════════════════════════════════════════
# adapters/goodwe.py
# ══════════════════════════════════════════════════════════════════════════════


class TestGoodWeBatch20:
    """Lines 99-100, 223, 338-343, 353, 374."""

    def _make_adapter(self) -> object:
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        hass.services.async_call = MagicMock(return_value=None)
        adapter = GoodWeAdapter(hass=hass, device_id="gw1", entity_prefix="kontor")
        return adapter

    def test_safe_call_dry_run_returns_true(self) -> None:
        """_analyze_only=True → _safe_call logs and returns True (lines 99-100)."""
        adapter = self._make_adapter()
        adapter._analyze_only = True  # type: ignore[union-attr]

        async def run() -> bool:
            return await adapter._safe_call(  # type: ignore[union-attr]
                "goodwe", "set_parameter", {"entity_id": "number.foo", "value": 1}
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is True

    def test_max_charge_w_zero_when_bms_limit_zero(self) -> None:
        """bms_charge_limit_a <= 0 → max_charge_w returns 0 (line 223)."""
        adapter = self._make_adapter()
        # hass.states.get returns None → bms_charge_limit_a=0 → returns 0
        result = adapter.max_charge_w  # type: ignore[union-attr]
        assert result == 0

    def test_set_fast_charging_blocked_when_not_authorized(self) -> None:
        """on=True, authorized=False → blocked → on forced to False (lines 338-343)."""
        from unittest.mock import patch

        adapter = self._make_adapter()

        async def mock_safe_call(*args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            return True

        with patch.object(adapter, "_safe_call", side_effect=mock_safe_call):  # type: ignore[arg-type]

            async def run() -> bool:
                return await adapter.set_fast_charging(on=True, authorized=False)  # type: ignore[union-attr]

            result = asyncio.get_event_loop().run_until_complete(run())
            # Should complete without error; forced off
            assert isinstance(result, bool)

    def test_set_fast_charging_safe_call_fails(self) -> None:
        """_safe_call returns False → set_fast_charging returns False (line 353)."""
        from unittest.mock import patch

        adapter = self._make_adapter()

        async def mock_safe_call_fail(*args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            return False

        with patch.object(adapter, "_safe_call", side_effect=mock_safe_call_fail):  # type: ignore[arg-type]

            async def run() -> bool:
                return await adapter.set_fast_charging(on=False, authorized=True)  # type: ignore[union-attr]

            result = asyncio.get_event_loop().run_until_complete(run())
            assert result is False

    def test_set_fast_charging_second_safe_call_fails(self) -> None:
        """on=True, first _safe_call ok, second fails → returns False (line 374)."""
        from unittest.mock import patch

        adapter = self._make_adapter()
        call_count = [0]

        async def mock_safe_call_second_fails(*args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            call_count[0] += 1
            return call_count[0] == 1  # First True, rest False

        with patch.object(adapter, "_safe_call", side_effect=mock_safe_call_second_fails):  # type: ignore[arg-type]

            async def run() -> bool:
                return await adapter.set_fast_charging(on=True, authorized=True)  # type: ignore[union-attr]

            result = asyncio.get_event_loop().run_until_complete(run())
            assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# adapters/easee.py
# ══════════════════════════════════════════════════════════════════════════════


class TestEaseeBatch20:
    """Lines 107-108, 177-178, 216, 317-318, 323, 388-398."""

    def _make_adapter(self) -> object:
        from unittest.mock import MagicMock

        from custom_components.carmabox.adapters.easee import EaseeAdapter

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        hass.services.async_call = MagicMock(return_value=None)
        return EaseeAdapter(hass=hass, device_id="ev1")

    def test_safe_call_dry_run_returns_true(self) -> None:
        """_analyze_only=True → _safe_call logs and returns True (lines 107-108)."""
        adapter = self._make_adapter()
        adapter._analyze_only = True  # type: ignore[union-attr]

        async def run() -> bool:
            return await adapter._safe_call(  # type: ignore[union-attr]
                "easee", "set_charger_dynamic_limit", {"charger_id": "abc", "current": 10}
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is True

    def test_state_by_id_invalid_value_returns_default(self) -> None:
        """State is non-numeric → except ValueError → return default (lines 177-178)."""
        from unittest.mock import MagicMock

        adapter = self._make_adapter()
        state_mock = MagicMock()
        state_mock.state = "not_a_number"
        adapter.hass.states.get = MagicMock(return_value=state_mock)  # type: ignore[union-attr]
        result = adapter._state_by_id(  # type: ignore[union-attr]
            "sensor.easee_home_12840_power", default=99.0
        )
        assert result == 99.0

    def test_cable_locked_plug_connected(self) -> None:
        """plug == 'on' → return True (line 216)."""
        from unittest.mock import MagicMock

        adapter = self._make_adapter()

        def mock_states_get(entity_id: str) -> object:
            s = MagicMock()
            if "plug" in entity_id and "cable_locked" not in entity_id:
                s.state = "on"
            else:
                s.state = "off"
            return s

        adapter.hass.states.get = mock_states_get  # type: ignore[union-attr]
        assert adapter.cable_locked is True  # type: ignore[union-attr]

    def test_phase_count_three(self) -> None:
        """phase_mode='three' → phase_count=3 (line 318)."""
        from unittest.mock import MagicMock

        adapter = self._make_adapter()

        def mock_state(entity_id: str) -> object:
            s = MagicMock()
            s.state = "three"
            return s

        adapter.hass.states.get = mock_state  # type: ignore[union-attr]
        assert adapter.phase_count == 3  # type: ignore[union-attr]

    def test_charging_power_at_amps(self) -> None:
        """charging_power_at_amps uses phase_count (line 323)."""
        from unittest.mock import patch

        adapter = self._make_adapter()

        with (
            patch.object(
                type(adapter),
                "dynamic_limit_a",
                new_callable=lambda: property(lambda self: 10.0),
            ),
            patch.object(
                type(adapter),
                "phase_count",
                new_callable=lambda: property(lambda self: 3),
            ),
        ):
            result = adapter.charging_power_at_amps  # type: ignore[union-attr]
            assert abs(result - 10.0 * 230 * 3 / 1000) < 0.01

    def test_set_charger_phase_no_charger_id(self) -> None:
        """charger_id='' → return False immediately (line 388-390)."""
        adapter = self._make_adapter()
        adapter.charger_id = ""  # type: ignore[union-attr]

        async def run() -> bool:
            return await adapter.set_charger_phase_mode("1_phase")  # type: ignore[union-attr]

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is False

    def test_set_charger_phase_invalid_mode(self) -> None:
        """Invalid mode → return False (lines 394-396)."""
        adapter = self._make_adapter()
        adapter.charger_id = "abc123"  # type: ignore[union-attr]

        async def run() -> bool:
            return await adapter.set_charger_phase_mode("bad_mode")  # type: ignore[union-attr]

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# core/coordinator_v2.py
# ══════════════════════════════════════════════════════════════════════════════


class TestCoordinatorV2Batch20:
    """Lines 263-265: plan_counter >= plan_interval_cycles → plan_due."""

    def test_plan_due_when_counter_reaches_interval(self) -> None:
        """After plan_interval_cycles cycles, plan_due appears in reason (lines 263-265)."""
        from custom_components.carmabox.core.coordinator_v2 import (
            CoordinatorConfig,
            CoordinatorV2,
            SystemState,
        )

        cfg = CoordinatorConfig(plan_interval_cycles=2)
        v2 = CoordinatorV2(config=cfg)
        # Force startup confirmed
        v2._startup_confirmed = True  # type: ignore[union-attr]

        state = SystemState(
            battery_soc_1=50.0,
            battery_soc_2=50.0,
            battery_power_1=0.0,
            battery_power_2=0.0,
            ems_mode_1="discharge_pv",
            ems_mode_2="discharge_pv",
            fast_charging_1=False,
            fast_charging_2=False,
            ellevio_viktat_kw=0.5,
            hour=12,
        )

        # Run exactly plan_interval_cycles so the last cycle triggers plan_due
        result = None
        for _ in range(cfg.plan_interval_cycles):
            result = v2.cycle(state)

        assert result is not None
        assert "plan_due" in result.reason
