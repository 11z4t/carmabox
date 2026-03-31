"""Coverage tests for _execute_climate, _execute_pool, _execute_pool_circulation.

EXP-EPIC-SWEEP — targets coordinator.py clusters:
  Lines 2144-2195  — _execute_climate (VP/AC thermal storage)
  Lines 2228-2268  — _execute_pool (pool heater surplus control)
  Lines 2298-2327  — _execute_pool_circulation (cirk pump surplus control)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import CarmaboxState
from tests.unit.test_expert_control import _make_coord

# ── Helpers ──────────────────────────────────────────────────────────────────

def _state_importing(grid_w: float = 1500.0, price: float = 150.0, **kwargs) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_w,
        battery_soc_1=60.0,
        current_price=price,
        pv_power_w=0.0,
        **kwargs,
    )


def _state_exporting(grid_w: float = -600.0, price: float = 30.0, **kwargs) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_w,
        battery_soc_1=80.0,
        current_price=price,
        pv_power_w=1500.0,
        **kwargs,
    )


def _make_climate_state(mode: str = "off", temp: float = 22.0) -> MagicMock:
    s = MagicMock()
    s.state = mode
    s.attributes = {"current_temperature": temp}
    return s


def _add_climate(
    coord, entity_id: str = "climate.test_vp", mode: str = "off", temp: float = 22.0
) -> None:
    coord._cfg["climate_entity"] = entity_id
    coord._states[entity_id] = _make_climate_state(mode, temp)


def _add_pool(coord, entity_id: str = "switch.pool_heater", on: bool = False) -> None:
    coord._cfg["pool_entity"] = entity_id
    s = MagicMock()
    s.state = "on" if on else "off"
    s.attributes = {}
    coord._states[entity_id] = s


def _add_pool_temp(coord, temp: float = 26.0, entity_id: str = "sensor.pool_temp") -> None:
    coord._cfg["pool_temp_entity"] = entity_id
    s = MagicMock()
    s.state = str(temp)
    s.attributes = {}
    coord._states[entity_id] = s


def _add_cirk(coord, entity_id: str = "switch.pool_cirk", on: bool = False) -> None:
    coord._cfg["pool_circulation_entity"] = entity_id
    s = MagicMock()
    s.state = "on" if on else "off"
    s.attributes = {}
    coord._states[entity_id] = s


# ── _execute_climate ──────────────────────────────────────────────────────────

class TestExecuteClimate:
    """Lines 2144-2195: VP/AC thermal storage control."""

    @pytest.mark.asyncio
    async def test_climate_state_none_returns_early(self) -> None:
        """No climate state in HA → return early (line 2145)."""
        coord = _make_coord()
        coord._cfg["climate_entity"] = "climate.missing"
        # Don't add state — hass.states.get returns None
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12, month=7)
            await coord._execute_climate(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_current_temp_returns_early(self) -> None:
        """Climate state has no current_temperature → return early (line 2151)."""
        coord = _make_coord()
        coord._cfg["climate_entity"] = "climate.test_vp"
        s = MagicMock()
        s.state = "off"
        s.attributes = {}  # No current_temperature key
        coord._states["climate.test_vp"] = s
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12, month=7)
            await coord._execute_climate(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_night_returns_early(self) -> None:
        """is_night → return immediately (line 2164)."""
        coord = _make_coord()
        _add_climate(coord, temp=25.0)
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=23, month=7)  # Night, summer
            await coord._execute_climate(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_summer_surplus_precool_when_mode_off(self) -> None:
        """Summer + exporting surplus + temp > cool_target + mode off → pre-cool."""
        coord = _make_coord()
        cool_target = float(coord._cfg.get("climate_cool_target_c", 23.0))
        _add_climate(coord, mode="off", temp=cool_target + 1.0)  # 24°C > 23°C target
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-600.0)  # abs > 500W

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12, month=7)  # Day, summer
            await coord._execute_climate(state)

        # Should call set_hvac_mode to "cool" + set_temperature
        assert coord.hass.services.async_call.call_count >= 1
        calls = coord.hass.services.async_call.call_args_list
        domains = [c[0][0] for c in calls]
        assert "climate" in domains

    @pytest.mark.asyncio
    async def test_summer_surplus_no_precool_when_already_cooling(self) -> None:
        """Summer surplus + mode = 'cool' (not 'off') → no additional action."""
        coord = _make_coord()
        cool_target = float(coord._cfg.get("climate_cool_target_c", 23.0))
        _add_climate(coord, mode="cool", temp=cool_target + 1.0)
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-600.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12, month=7)
            await coord._execute_climate(state)

        # mode != "off" for Case1 → no call
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_winter_surplus_preheat(self) -> None:
        """Winter + exporting surplus + temp < heat_target+2 + mode off → pre-heat."""
        coord = _make_coord()
        heat_target = float(coord._cfg.get("climate_heat_target_c", 21.0))
        _add_climate(coord, mode="off", temp=heat_target + 1.5)  # 22.5 < heat_target+2=23
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-600.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12, month=12)  # Day, winter
            await coord._execute_climate(state)

        assert coord.hass.services.async_call.call_count >= 1
        calls = coord.hass.services.async_call.call_args_list
        # Should call set_hvac_mode = "heat"
        set_hvac_calls = [c for c in calls if c[0][0] == "climate"]
        assert len(set_hvac_calls) >= 1

    @pytest.mark.asyncio
    async def test_expensive_import_pauses_cooling(self) -> None:
        """Expensive + importing + mode != 'off' + temp OK → pause (lines 2180-2194)."""
        coord = _make_coord()
        coord._cfg["price_expensive_ore"] = 100.0
        cool_target = float(coord._cfg.get("climate_cool_target_c", 23.0))
        # temp < cool_target + 2 = 25 → temp_ok = True (summer + temp not too hot yet)
        _add_climate(coord, mode="cool", temp=cool_target + 1.5)
        coord.hass.services.async_call = AsyncMock()

        # Importing (not exporting), price > price_expensive
        state = _state_importing(grid_w=1500.0, price=150.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14, month=7)  # Day, summer
            await coord._execute_climate(state)

        # Should call turn_off for climate
        calls = coord.hass.services.async_call.call_args_list
        assert any(c[0][0] == "climate" for c in calls)

    @pytest.mark.asyncio
    async def test_expensive_import_no_pause_when_temp_not_ok(self) -> None:
        """Expensive + importing but temp too hot → don't pause (no call)."""
        coord = _make_coord()
        coord._cfg["price_expensive_ore"] = 100.0
        cool_target = float(coord._cfg.get("climate_cool_target_c", 23.0))
        # temp > cool_target + 2 = 25 → NOT temp_ok (already too hot)
        _add_climate(coord, mode="cool", temp=cool_target + 3.0)  # 26°C > 25
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing(grid_w=1500.0, price=150.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14, month=7)
            await coord._execute_climate(state)

        coord.hass.services.async_call.assert_not_called()


# ── _execute_pool ─────────────────────────────────────────────────────────────

class TestExecutePool:
    """Lines 2228-2268: Pool heater surplus control."""

    @pytest.mark.asyncio
    async def test_pool_state_none_returns_early(self) -> None:
        """No pool state → return early (line 2230)."""
        coord = _make_coord()
        coord._cfg["pool_entity"] = "switch.missing_pool"
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_too_hot_turns_off(self) -> None:
        """Pool temp >= pool_max + pool_on → turn OFF (lines 2247-2250)."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=True)
        _add_pool_temp(coord, temp=29.5)  # > 28.0 max
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing()
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_off"

    @pytest.mark.asyncio
    async def test_pool_not_turned_off_when_already_off_and_too_hot(self) -> None:
        """Pool temp >= pool_max but pool already OFF → no call."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=False)  # already off
        _add_pool_temp(coord, temp=29.5)
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing()
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_surplus_turns_pool_on(self) -> None:
        """Surplus > 300W + temp < max + pool off → turn ON (lines 2253-2262)."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=False)
        _add_pool_temp(coord, temp=26.0)  # < 28.0 max
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-500.0)  # abs > 300W export
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_on"

    @pytest.mark.asyncio
    async def test_surplus_no_turn_on_when_already_on(self) -> None:
        """Surplus but pool already ON → no redundant call."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=True)  # already on
        _add_pool_temp(coord, temp=26.0)
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-500.0)
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_surplus_no_turn_on_when_temp_at_max(self) -> None:
        """Surplus but pool temp at max → no turn on."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=False)
        _add_pool_temp(coord, temp=28.0)  # == pool_max
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-500.0)
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_importing_turns_pool_off(self) -> None:
        """Importing > 500W + pool on → turn OFF (lines 2265-2267)."""
        coord = _make_coord()
        _add_pool(coord, on=True)
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing(grid_w=800.0)  # importing > 500W
        await coord._execute_pool(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_off"

    @pytest.mark.asyncio
    async def test_pool_without_temp_entity_no_max_check(self) -> None:
        """No pool_temp_entity configured → pool_temp = None → turn on with surplus."""
        coord = _make_coord()
        coord._cfg["pool_max_temp_c"] = 28.0
        _add_pool(coord, on=False)
        # No pool_temp_entity in _cfg → pool_temp stays None
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-500.0)
        await coord._execute_pool(state)

        # pool_temp is None → max check skipped; surplus → turn ON
        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_on"


# ── _execute_pool_circulation ─────────────────────────────────────────────────

class TestExecutePoolCirculation:
    """Lines 2298-2327: Pool circulation pump control."""

    @pytest.mark.asyncio
    async def test_cirk_state_none_returns_early(self) -> None:
        """No cirk switch state → return early (line 2300)."""
        coord = _make_coord()
        coord._cfg["pool_circulation_entity"] = "switch.missing_cirk"
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_running_turns_cirk_on(self) -> None:
        """Pool heater running + cirk off → cirk ON (lines 2312-2315)."""
        coord = _make_coord()
        _add_cirk(coord, on=False)
        _add_pool(coord, on=True)  # pool heater is running
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing()
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_on"

    @pytest.mark.asyncio
    async def test_pool_running_cirk_already_on_no_change(self) -> None:
        """Pool running + cirk already ON → no redundant call."""
        coord = _make_coord()
        _add_cirk(coord, on=True)
        _add_pool(coord, on=True)
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing()
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_surplus_turns_cirk_on(self) -> None:
        """Exporting surplus > 200W + cirk off → cirk ON (lines 2318-2322)."""
        coord = _make_coord()
        _add_cirk(coord, on=False)
        # No pool running
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-400.0)  # abs > 200W
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_on"

    @pytest.mark.asyncio
    async def test_surplus_cirk_already_on_no_change(self) -> None:
        """Surplus + cirk already ON → no redundant call."""
        coord = _make_coord()
        _add_cirk(coord, on=True)
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting(grid_w=-400.0)
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_importing_pool_off_cirk_on_turns_cirk_off(self) -> None:
        """Importing > 500W + pool not running + cirk ON → cirk OFF (lines 2325-2327)."""
        coord = _make_coord()
        _add_cirk(coord, on=True)
        _add_pool(coord, on=False)  # pool NOT running
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing(grid_w=800.0)  # > 500W
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][1] == "turn_off"

    @pytest.mark.asyncio
    async def test_importing_pool_running_keeps_cirk_on(self) -> None:
        """Importing > 500W but pool IS running → don't stop cirk."""
        coord = _make_coord()
        _add_cirk(coord, on=True)
        _add_pool(coord, on=True)  # pool IS running
        coord.hass.services.async_call = AsyncMock()

        state = _state_importing(grid_w=800.0)
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cirk_entity_returns_early(self) -> None:
        """No pool_circulation_entity configured AND no auto-detect → return early."""
        coord = _make_coord()
        # No cirk entity in cfg, mock states.async_all to return empty
        coord.hass.states.async_all = MagicMock(return_value=[])
        coord.hass.services.async_call = AsyncMock()

        state = _state_exporting()
        await coord._execute_pool_circulation(state)

        coord.hass.services.async_call.assert_not_called()
