"""Tests for diagnostics platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.diagnostics import (
    _anonymize_options,
    _hash_entity,
    async_get_config_entry_diagnostics,
)
from custom_components.carmabox.optimizer.models import CarmaboxState, HourPlan
from custom_components.carmabox.optimizer.safety_guard import SafetyGuard
from custom_components.carmabox.optimizer.savings import SavingsState


class TestHashEntity:
    def test_hashes_entity(self) -> None:
        result = _hash_entity("sensor.my_private_sensor")
        assert result.startswith("entity_")
        assert "my_private" not in result

    def test_empty_returns_empty(self) -> None:
        assert _hash_entity("") == ""

    def test_consistent(self) -> None:
        assert _hash_entity("sensor.x") == _hash_entity("sensor.x")


class TestAnonymizeOptions:
    def test_hashes_entity_ids(self) -> None:
        opts = {
            "price_area": "SE3",
            "battery_soc_1": "sensor.pv_battery_soc_kontor",
            "min_soc": 15.0,
        }
        result = _anonymize_options(opts)
        assert result["price_area"] == "SE3"
        assert result["min_soc"] == 15.0
        assert "sensor.pv" not in result["battery_soc_1"]
        assert result["battery_soc_1"].startswith("entity_")

    def test_keeps_non_entity_strings(self) -> None:
        opts = {"grid_operator": "ellevio", "ev_model": "XPENG G9"}
        result = _anonymize_options(opts)
        assert result["grid_operator"] == "ellevio"
        assert result["ev_model"] == "XPENG G9"


class TestAsyncGetDiagnostics:
    @pytest.mark.asyncio
    async def test_returns_diagnostics(self) -> None:
        coord = MagicMock(spec=CarmaboxCoordinator)
        coord.data = CarmaboxState(grid_power_w=1500, battery_soc_1=80)
        coord.data.plan = [
            HourPlan(
                hour=17,
                action="d",
                battery_kw=-1.5,
                grid_kw=1.0,
                weighted_kw=1.0,
                pv_kw=0,
                consumption_kw=2.5,
                ev_kw=0,
                ev_soc=50,
                battery_soc=78,
                price=85,
            )
        ]
        coord.target_kw = 2.0
        coord.min_soc = 15.0
        coord._last_command = BatteryCommand.IDLE
        coord._daily_plans = 5
        coord._daily_safety_blocks = 1
        coord._daily_discharge_kwh = 3.5
        coord.plan = coord.data.plan
        coord.savings = SavingsState(month=3, year=2026)
        coord.safety = SafetyGuard()
        coord.safety.update_heartbeat()

        entry = MagicMock()
        entry.options = {
            "price_area": "SE3",
            "battery_soc_1": "sensor.secret",
            "peak_cost_per_kw": 80.0,
        }
        entry.runtime_data = coord

        hass = MagicMock()
        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "config" in result
        assert "coordinator" in result
        assert "state" in result
        assert "plan" in result
        assert "savings" in result
        assert "safety" in result
        # Entity IDs anonymized
        assert "sensor.secret" not in str(result["config"])
        # Plan included
        assert len(result["plan"]) == 1
        assert result["plan"][0]["hour"] == 17
        # Safety status
        assert result["safety"]["heartbeat_ok"] is True

    @pytest.mark.asyncio
    async def test_no_data(self) -> None:
        coord = MagicMock(spec=CarmaboxCoordinator)
        coord.data = None
        coord.target_kw = 2.0
        coord.min_soc = 15.0
        coord._last_command = BatteryCommand.IDLE
        coord._daily_plans = 0
        coord._daily_safety_blocks = 0
        coord._daily_discharge_kwh = 0.0
        coord.plan = []
        coord.savings = SavingsState(month=3, year=2026)
        coord.safety = SafetyGuard()

        entry = MagicMock()
        entry.options = {"peak_cost_per_kw": 80.0}
        entry.runtime_data = coord

        result = await async_get_config_entry_diagnostics(MagicMock(), entry)
        assert result["state"]["grid_power_w"] is None
        assert result["plan"] == []
