"""Tests for CARMA Box sensors — EntityDescription pattern."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.models import CarmaboxState
from custom_components.carmabox.optimizer.savings import SavingsState
from custom_components.carmabox.sensor import (
    SENSOR_DESCRIPTIONS,
    CarmaboxSensor,
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
    coord.savings = SavingsState(month=3, year=2026)

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {"peak_cost_per_kw": 80.0}

    # Savings functions read coord.entry.options
    coord.entry = entry

    return coord, entry


def _get_sensor(key: str, coord: MagicMock, entry: MagicMock) -> CarmaboxSensor:
    """Get sensor by key from descriptions."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
    return CarmaboxSensor(coord, entry, desc)


class TestSensorDescriptions:
    def test_all_descriptions_have_key(self) -> None:
        assert len(SENSOR_DESCRIPTIONS) == 6
        keys = {d.key for d in SENSOR_DESCRIPTIONS}
        assert "plan_status" in keys
        assert "target_kw" in keys
        assert "savings_month" in keys
        assert "battery_soc" in keys
        assert "grid_import" in keys
        assert "ev_soc" in keys

    def test_all_have_translation_key(self) -> None:
        for desc in SENSOR_DESCRIPTIONS:
            assert desc.translation_key, f"{desc.key} missing translation_key"

    def test_all_have_icon(self) -> None:
        for desc in SENSOR_DESCRIPTIONS:
            assert desc.icon, f"{desc.key} missing icon"


class TestPlanStatusSensor:
    def test_idle(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.data = CarmaboxState()
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "idle"

    def test_charging(self) -> None:
        coord, entry = _make_sensor_deps(
            state=CarmaboxState(), last_command=BatteryCommand.CHARGE_PV
        )
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "charging"

    def test_discharging(self) -> None:
        coord, entry = _make_sensor_deps(
            state=CarmaboxState(), last_command=BatteryCommand.DISCHARGE
        )
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "discharging"

    def test_exporting_shows_charging_pv(self) -> None:
        coord, entry = _make_sensor_deps(state=CarmaboxState(grid_power_w=-2000))
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "charging_pv"

    def test_all_full_shows_standby(self) -> None:
        coord, entry = _make_sensor_deps(state=CarmaboxState(battery_soc_1=100, battery_soc_2=-1))
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "standby"

    def test_no_data_shows_unknown(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.native_value == "unknown"

    def test_extra_attributes(self) -> None:
        coord, entry = _make_sensor_deps(state=CarmaboxState(grid_power_w=1500, battery_soc_1=80))
        sensor = _get_sensor("plan_status", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert "target_weighted_kw" in attrs
        assert "plan_hours" in attrs

    def test_extra_attributes_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("plan_status", coord, entry)
        assert sensor.extra_state_attributes == {}


class TestTargetSensor:
    def test_returns_target(self) -> None:
        coord, entry = _make_sensor_deps(target_kw=2.5)
        sensor = _get_sensor("target_kw", coord, entry)
        assert sensor.native_value == 2.5


class TestSavingsSensor:
    def test_returns_zero_when_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("savings_month", coord, entry)
        assert sensor.native_value == 0.0

    def test_returns_savings_with_data(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.savings.peak_samples = [2.0, 2.0, 2.0]
        coord.savings.baseline_peak_samples = [4.0, 4.0, 4.0]
        coord.savings.discharge_savings_kr = 10.0
        sensor = _get_sensor("savings_month", coord, entry)
        # Peak: (4-2)×80=160, discharge: 10, total: 170
        assert sensor.native_value == 170.0

    def test_extra_attributes(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.savings.discharge_savings_kr = 5.0
        coord.savings.total_discharge_kwh = 12.0
        sensor = _get_sensor("savings_month", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert "peak_reduction_kr" in attrs
        assert "discharge_savings_kr" in attrs
        assert attrs["total_discharge_kwh"] == 12.0


class TestBatterySocSensor:
    def test_single_battery(self) -> None:
        state = CarmaboxState(battery_soc_1=85, battery_soc_2=-1)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("battery_soc", coord, entry)
        assert sensor.native_value == 85

    def test_dual_battery_average(self) -> None:
        state = CarmaboxState(battery_soc_1=80, battery_soc_2=60)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("battery_soc", coord, entry)
        assert sensor.native_value == 70

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("battery_soc", coord, entry)
        assert sensor.native_value == 0


class TestGridImportSensor:
    def test_importing(self) -> None:
        state = CarmaboxState(grid_power_w=2500)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("grid_import", coord, entry)
        assert sensor.native_value == 2.5

    def test_exporting_shows_zero(self) -> None:
        state = CarmaboxState(grid_power_w=-1000)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("grid_import", coord, entry)
        assert sensor.native_value == 0

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("grid_import", coord, entry)
        assert sensor.native_value == 0


class TestEVSocSensor:
    def test_ev_present(self) -> None:
        state = CarmaboxState(ev_soc=65)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("ev_soc", coord, entry)
        assert sensor.native_value == 65

    def test_no_ev(self) -> None:
        state = CarmaboxState(ev_soc=-1)
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("ev_soc", coord, entry)
        assert sensor.native_value is None

    def test_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("ev_soc", coord, entry)
        assert sensor.native_value is None


class TestDeviceInfo:
    def test_device_info(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("plan_status", coord, entry)
        info = sensor.device_info
        assert info["manufacturer"] == "CARMA Box"
        assert info["model"] == "Energy Optimizer"


class TestUniqueId:
    def test_unique_id_format(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("target_kw", coord, entry)
        assert sensor.unique_id == "test_entry_target_kw"


class TestNoExtraAttrs:
    def test_sensors_without_attrs_return_none(self) -> None:
        coord, entry = _make_sensor_deps(state=CarmaboxState())
        for desc in SENSOR_DESCRIPTIONS:
            if desc.extra_attrs_fn is None:
                sensor = CarmaboxSensor(coord, entry, desc)
                assert sensor.extra_state_attributes is None
