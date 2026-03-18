"""Tests for CARMA Box sensors."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.models import CarmaboxState
from custom_components.carmabox.sensor import (
    CarmaboxBatterySocSensor,
    CarmaboxEVSocSensor,
    CarmaboxGridImportSensor,
    CarmaboxPlanStatusSensor,
    CarmaboxSavingsSensor,
    CarmaboxTargetSensor,
)


def _make_sensor_deps(
    state: CarmaboxState | None = None,
    last_command: BatteryCommand = BatteryCommand.IDLE,
    target_kw: float = 2.0,
) -> tuple[CarmaboxCoordinator, MagicMock]:
    """Create mocked coordinator + entry for sensor tests."""
    coord = MagicMock(spec=CarmaboxCoordinator)
    coord.data = state
    coord._last_command = last_command
    coord.target_kw = target_kw

    entry = MagicMock()
    entry.entry_id = "test_entry"

    return coord, entry


class TestPlanStatusSensor:
    def test_idle(self) -> None:
        coord, entry = _make_sensor_deps(
            CarmaboxState(grid_power_w=1000, battery_soc_1=50),
            BatteryCommand.IDLE,
        )
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "idle"

    def test_charging(self) -> None:
        coord, entry = _make_sensor_deps(
            CarmaboxState(grid_power_w=1000, battery_soc_1=50),
            BatteryCommand.CHARGE_PV,
        )
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "charging"

    def test_discharging(self) -> None:
        coord, entry = _make_sensor_deps(
            CarmaboxState(grid_power_w=3000, battery_soc_1=50),
            BatteryCommand.DISCHARGE,
        )
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "discharging"

    def test_exporting_shows_charging_pv(self) -> None:
        coord, entry = _make_sensor_deps(
            CarmaboxState(grid_power_w=-1000, battery_soc_1=50),
        )
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "charging_pv"

    def test_all_full_shows_standby(self) -> None:
        coord, entry = _make_sensor_deps(
            CarmaboxState(grid_power_w=1000, battery_soc_1=100, battery_soc_2=-1),
        )
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "standby"

    def test_no_data_shows_unknown(self) -> None:
        coord, entry = _make_sensor_deps(state=None)
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.native_value == "unknown"

    def test_extra_attributes(self) -> None:
        state = CarmaboxState(
            grid_power_w=2000,
            battery_soc_1=80,
            battery_soc_2=70,
            ev_soc=50,
            target_weighted_kw=2.0,
        )
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs["target_weighted_kw"] == 2.0
        assert attrs["battery_soc_1"] == 80
        assert attrs["battery_soc_2"] == 70
        assert attrs["ev_soc"] == 50

    def test_extra_attributes_no_data(self) -> None:
        coord, entry = _make_sensor_deps(state=None)
        sensor = CarmaboxPlanStatusSensor(coord, entry)
        assert sensor.extra_state_attributes == {}


class TestTargetSensor:
    def test_returns_target(self) -> None:
        coord, entry = _make_sensor_deps(target_kw=2.5)
        sensor = CarmaboxTargetSensor(coord, entry)
        assert sensor.native_value == 2.5


class TestSavingsSensor:
    def test_returns_zero_for_now(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = CarmaboxSavingsSensor(coord, entry)
        assert sensor.native_value == 0.0


class TestBatterySocSensor:
    def test_single_battery(self) -> None:
        state = CarmaboxState(battery_soc_1=85, battery_soc_2=-1)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxBatterySocSensor(coord, entry)
        assert sensor.native_value == 85

    def test_dual_battery_average(self) -> None:
        state = CarmaboxState(battery_soc_1=80, battery_soc_2=60)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxBatterySocSensor(coord, entry)
        assert sensor.native_value == 70

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps(state=None)
        sensor = CarmaboxBatterySocSensor(coord, entry)
        assert sensor.native_value == 0


class TestGridImportSensor:
    def test_importing(self) -> None:
        state = CarmaboxState(grid_power_w=2500)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxGridImportSensor(coord, entry)
        assert sensor.native_value == 2.5

    def test_exporting_shows_zero(self) -> None:
        state = CarmaboxState(grid_power_w=-1000)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxGridImportSensor(coord, entry)
        assert sensor.native_value == 0

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps(state=None)
        sensor = CarmaboxGridImportSensor(coord, entry)
        assert sensor.native_value == 0


class TestEVSocSensor:
    def test_ev_present(self) -> None:
        state = CarmaboxState(ev_soc=65)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxEVSocSensor(coord, entry)
        assert sensor.native_value == 65

    def test_no_ev(self) -> None:
        state = CarmaboxState(ev_soc=-1)
        coord, entry = _make_sensor_deps(state)
        sensor = CarmaboxEVSocSensor(coord, entry)
        assert sensor.native_value is None

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps(state=None)
        sensor = CarmaboxEVSocSensor(coord, entry)
        assert sensor.native_value is None
