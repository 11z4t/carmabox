"""Tests for appliance detection and tracking."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.appliances import (
    Appliance,
    appliance_summary,
    detect_appliances,
    update_appliance_states,
)
from custom_components.carmabox.const import DEFAULT_APPLIANCE_THRESHOLD_W


def _make_state(entity_id: str, state: str, unit: str = "W", name: str | None = None):
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.entity_id = entity_id
    mock.state = state
    mock.attributes = {
        "unit_of_measurement": unit,
        "friendly_name": name or entity_id,
    }
    return mock


class TestAppliance:
    def test_category_name_known(self) -> None:
        app = Appliance(entity_id="sensor.test", name="Test", category="laundry")
        assert app.category_name == "Vitvaror"

    def test_category_name_unknown(self) -> None:
        app = Appliance(entity_id="sensor.test", name="Test", category="xyz")
        assert app.category_name == "xyz"

    def test_to_dict(self) -> None:
        app = Appliance(entity_id="sensor.test", name="Test", category="heating", threshold_w=50.0)
        d = app.to_dict()
        assert d["entity_id"] == "sensor.test"
        assert d["name"] == "Test"
        assert d["category"] == "heating"
        assert d["threshold_w"] == 50.0

    def test_from_dict(self) -> None:
        data = {
            "entity_id": "sensor.pump",
            "name": "Heat Pump",
            "category": "heating",
            "threshold_w": 25.0,
        }
        app = Appliance.from_dict(data)
        assert app.entity_id == "sensor.pump"
        assert app.name == "Heat Pump"
        assert app.category == "heating"
        assert app.threshold_w == 25.0

    def test_from_dict_defaults(self) -> None:
        app = Appliance.from_dict({})
        assert app.entity_id == ""
        assert app.name == ""
        assert app.category == "other"
        assert app.threshold_w == DEFAULT_APPLIANCE_THRESHOLD_W

    def test_roundtrip(self) -> None:
        original = Appliance(entity_id="sensor.x", name="X", category="pool", threshold_w=100.0)
        restored = Appliance.from_dict(original.to_dict())
        assert restored.entity_id == original.entity_id
        assert restored.name == original.name
        assert restored.category == original.category
        assert restored.threshold_w == original.threshold_w


class TestDetectAppliances:
    def test_detects_w_sensor(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = [
            _make_state("sensor.tvattmaskin_power", "150", "W", "Tvättmaskin Power"),
        ]
        result = detect_appliances(hass)
        assert len(result) == 1
        assert result[0].category == "laundry"

    def test_detects_kw_sensor(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = [
            _make_state("sensor.pool_pump_power", "1.5", "kW", "Pool Pump"),
        ]
        result = detect_appliances(hass)
        assert len(result) == 1
        assert result[0].category == "pool"

    def test_excludes_system_sensors(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = [
            _make_state("sensor.goodwe_battery_power", "500", "W"),
            _make_state("sensor.nordpool_price", "50", "W"),
            _make_state("sensor.pv_production", "1000", "W"),
            _make_state("sensor.carmabox_grid", "200", "W"),
        ]
        result = detect_appliances(hass)
        assert len(result) == 0

    def test_excludes_non_power_sensors(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = [
            _make_state("sensor.temperature", "22.5", "°C"),
        ]
        result = detect_appliances(hass)
        assert len(result) == 0

    def test_category_heuristics(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = [
            _make_state("sensor.diskmaskin", "100", "W", "Diskmaskin"),
            _make_state("sensor.varmepump", "2000", "W", "Värmepump"),
            _make_state("sensor.miner_rig", "500", "W", "Miner Rig"),
            _make_state("sensor.random_device", "50", "W", "Random Device"),
        ]
        result = detect_appliances(hass)
        categories = {a.entity_id: a.category for a in result}
        assert categories["sensor.diskmaskin"] == "laundry"
        assert categories["sensor.varmepump"] == "heating"
        assert categories["sensor.miner_rig"] == "miner"
        assert categories["sensor.random_device"] == "other"

    def test_empty_states(self) -> None:
        hass = MagicMock()
        hass.states.async_all.return_value = []
        result = detect_appliances(hass)
        assert result == []


class TestUpdateApplianceStates:
    def test_updates_power_w(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "500", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 500.0
        assert app.is_running is True

    def test_updates_power_kw(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "1.5", "kW")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 1500.0

    def test_unavailable_state(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "unavailable", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 0.0
        assert app.is_running is False

    def test_unknown_state(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "unknown", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 0.0

    def test_none_state(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        hass.states.get.return_value = None
        update_appliance_states(hass, [app])
        assert app.current_power_w == 0.0
        assert app.is_running is False

    def test_invalid_state_value(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "not_a_number", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 0.0
        assert app.is_running is False

    def test_below_threshold_not_running(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating", threshold_w=100)
        state = _make_state("sensor.pump", "50", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.current_power_w == 50.0
        assert app.is_running is False

    def test_run_counting(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        assert app.runs_today == 0

        # First run start
        state = _make_state("sensor.pump", "500", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app])
        assert app.runs_today == 1

        # Still running — no new count
        update_appliance_states(hass, [app])
        assert app.runs_today == 1

    def test_energy_tracking(self) -> None:
        hass = MagicMock()
        app = Appliance(entity_id="sensor.pump", name="Pump", category="heating")
        state = _make_state("sensor.pump", "1000", "W")
        hass.states.get.return_value = state
        update_appliance_states(hass, [app], interval_hours=1.0)
        assert app.today_kwh == 1.0  # 1000W * 1h = 1 kWh


class TestApplianceSummary:
    def test_empty(self) -> None:
        result = appliance_summary([])
        assert result["total_power_w"] == 0.0
        assert result["running_count"] == 0
        assert result["running_names"] == []

    def test_with_appliances(self) -> None:
        apps = [
            Appliance(
                entity_id="s.a",
                name="A",
                category="laundry",
                current_power_w=500,
                is_running=True,
            ),
            Appliance(
                entity_id="s.b",
                name="B",
                category="laundry",
                current_power_w=200,
                is_running=False,
            ),
            Appliance(
                entity_id="s.c",
                name="C",
                category="heating",
                current_power_w=2000,
                is_running=True,
            ),
        ]
        result = appliance_summary(apps)
        assert result["total_power_w"] == 2700.0
        assert result["running_count"] == 2
        assert "A" in result["running_names"]
        assert "C" in result["running_names"]
        cats = result["categories"]
        assert cats["laundry"]["total_power_w"] == 700.0
        assert cats["laundry"]["count"] == 2
        assert cats["heating"]["total_power_w"] == 2000.0

    def test_today_kwh_aggregation(self) -> None:
        apps = [
            Appliance(entity_id="s.a", name="A", category="pool", today_kwh=3.5),
            Appliance(entity_id="s.b", name="B", category="pool", today_kwh=1.5),
        ]
        result = appliance_summary(apps)
        assert result["categories"]["pool"]["today_kwh"] == 5.0
