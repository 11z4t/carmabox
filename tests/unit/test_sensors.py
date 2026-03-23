"""Tests for CARMA Box sensors — EntityDescription pattern."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.coordinator import BatteryCommand, CarmaboxCoordinator
from custom_components.carmabox.optimizer.models import CarmaboxState, Decision, HourActual
from custom_components.carmabox.optimizer.savings import SavingsState
from custom_components.carmabox.sensor import (
    SENSOR_DESCRIPTIONS,
    CarmaboxSensor,
    _appliance_attrs_factory,
    _appliance_value_factory,
    _build_appliance_descriptions,
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
    coord.last_decision = Decision()
    coord.decision_log = []
    coord.hourly_actuals = []
    coord.executor_enabled = True
    coord._taper_active = False
    coord._cold_lock_active = False
    coord.status_text = "Allt fungerar"
    coord.system_health = {"kontor": "ok", "forrad": "ok", "sakerhet": "ok", "styrning": "ok"}
    coord.plan_score = MagicMock(
        return_value={
            "score_today": None,
            "score_7d": None,
            "score_30d": None,
            "trend": "stable",
        }
    )

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
        assert len(SENSOR_DESCRIPTIONS) == 19
        keys = {d.key for d in SENSOR_DESCRIPTIONS}
        assert "plan_accuracy" in keys
        assert "decision" in keys
        assert "plan_status" in keys
        assert "target_kw" in keys
        assert "savings_month" in keys
        assert "battery_soc" in keys
        assert "grid_import" in keys
        assert "ev_soc" in keys
        assert "household_insights" in keys

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

    def test_dual_battery_weighted(self) -> None:
        """Weighted SoC: 80%×15 + 60%×5 = 75%."""
        state = CarmaboxState(
            battery_soc_1=80, battery_soc_2=60, battery_cap_1_kwh=15, battery_cap_2_kwh=5
        )
        coord, entry = _make_sensor_deps(state=state)
        sensor = _get_sensor("battery_soc", coord, entry)
        assert sensor.native_value == 75

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
        assert sensor.unique_id == "carmabox_target_kw"


class TestDecisionSensor:
    def test_decision_value_shows_reason(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.last_decision = Decision(
            action="discharge",
            reason="Urladdning 500W — grid 3.2 kW > target 2.0 kW (141 öre/kWh)",
        )
        sensor = _get_sensor("decision", coord, entry)
        assert "Urladdning 500W" in sensor.native_value

    def test_decision_value_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("decision", coord, entry)
        assert sensor.native_value == "Ingen data"

    def test_decision_attrs_has_required_fields(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.last_decision = Decision(
            timestamp="2026-03-19T14:30:00",
            action="discharge",
            reason="Urladdning 500W — grid 3.2 kW > target 2.0 kW (141 öre/kWh)",
            target_kw=2.0,
            grid_kw=3.2,
            weighted_kw=3.2,
            price_ore=141.0,
            battery_soc=80.0,
            ev_soc=65.0,
            pv_kw=1.5,
        )
        sensor = _get_sensor("decision", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        # All AC-required attributes
        assert attrs["action"] == "discharge"
        assert "Urladdning 500W" in attrs["reason_text"]
        assert attrs["target_kw"] == 2.0
        assert attrs["grid_kw"] == 3.2
        assert attrs["weighted_kw"] == 3.2
        assert attrs["price_ore"] == 141.0
        assert attrs["battery_soc"] == 80.0
        assert attrs["ev_soc"] == 65.0
        assert attrs["pv_kw"] == 1.5
        assert attrs["timestamp"] == "2026-03-19T14:30:00"

    def test_decisions_24h_attribute(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.decision_log = [
            Decision(
                timestamp=f"2026-03-19T{h:02d}:00:00",
                action="idle",
                reason=f"Vila — timme {h}",
                target_kw=2.0,
                grid_kw=1.0,
                weighted_kw=1.0,
                price_ore=50.0,
                battery_soc=60.0,
                ev_soc=-1.0,
                pv_kw=0.5,
            )
            for h in range(48)
        ]
        sensor = _get_sensor("decision", coord, entry)
        attrs = sensor.extra_state_attributes
        assert "decisions_24h" in attrs
        assert len(attrs["decisions_24h"]) == 48
        # Verify structure of each entry
        entry_0 = attrs["decisions_24h"][0]
        assert "timestamp" in entry_0
        assert "action" in entry_0
        assert "reason_text" in entry_0
        assert "target_kw" in entry_0
        assert "grid_kw" in entry_0
        assert "weighted_kw" in entry_0
        assert "price_ore" in entry_0
        assert "battery_soc" in entry_0
        assert "pv_kw" in entry_0

    def test_decision_updates_on_execute(self) -> None:
        """Decision sensor must update whenever _execute() runs."""
        coord, entry = _make_sensor_deps()
        coord.last_decision = Decision(
            timestamp="2026-03-19T14:00:00",
            action="idle",
            reason="Vila — grid 1.0 kW < target 2.0 kW",
        )
        sensor = _get_sensor("decision", coord, entry)
        assert sensor.native_value == "Vila — grid 1.0 kW < target 2.0 kW"

        # Simulate _execute() updating the decision
        coord.last_decision = Decision(
            timestamp="2026-03-19T14:00:30",
            action="discharge",
            reason="Urladdning 3000W — grid 5.0 kW > target 2.0 kW (120 öre/kWh)",
        )
        # Sensor reads live from coordinator — no staleness
        assert "Urladdning 3000W" in sensor.native_value


class TestPlanAccuracySensor:
    """Tests for plan_accuracy sensor — PLAT-896 AC requirements."""

    def test_ac_example_plan_2_actual_2_3_gives_87(self) -> None:
        """AC: plan=2.0 kW, actual=2.3 kW → accuracy 87%."""
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.3) for h in range(3)
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value == 87  # 2.0/2.3 = 86.96 ≈ 87

    def test_perfect_match_gives_100(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=2.0) for h in range(3)
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value == 100

    def test_none_when_insufficient_data(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [HourActual(hour=0, planned_weighted_kw=2.0, actual_weighted_kw=2.0)]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value is None

    def test_none_when_empty(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = []
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value is None

    def test_both_zero_counts_as_perfect(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(hour=0, planned_weighted_kw=0.0, actual_weighted_kw=0.0),
            HourActual(hour=1, planned_weighted_kw=2.0, actual_weighted_kw=2.0),
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value == 100

    def test_actual_lower_than_plan(self) -> None:
        """Symmetrical: plan=2.3, actual=2.0 → same 87%."""
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.3, actual_weighted_kw=2.0) for h in range(3)
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        assert sensor.native_value == 87

    def test_attrs_include_full_history(self) -> None:
        """AC: 24h history with plan/actual per hour."""
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(
                hour=h,
                planned_action="d",
                actual_action="d",
                planned_grid_kw=2.0,
                actual_grid_kw=2.3,
                planned_weighted_kw=2.0,
                actual_weighted_kw=2.3,
                planned_battery_soc=80,
                actual_battery_soc=78,
                planned_ev_soc=65,
                actual_ev_soc=62,
                price=141.0,
            )
            for h in range(5)
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["hours_tracked"] == 5
        assert attrs["goal_pct"] == 70
        assert attrs["goal_met"] is True
        assert len(attrs["history"]) == 5
        entry_0 = attrs["history"][0]
        assert entry_0["plan_grid_kw"] == 2.0
        assert entry_0["actual_grid_kw"] == 2.3
        assert entry_0["plan_kw"] == 2.0
        assert entry_0["actual_kw"] == 2.3
        assert entry_0["plan_action"] == "d"
        assert entry_0["actual_action"] == "d"
        assert entry_0["bat_plan"] == 80
        assert entry_0["bat_actual"] == 78
        assert entry_0["ev_plan"] == 65
        assert entry_0["ev_actual"] == 62
        assert entry_0["price"] == 141.0

    def test_goal_not_met_below_70(self) -> None:
        """AC: driftmål >70%."""
        coord, entry = _make_sensor_deps()
        coord.hourly_actuals = [
            HourActual(hour=h, planned_weighted_kw=2.0, actual_weighted_kw=5.0) for h in range(3)
        ]
        sensor = _get_sensor("plan_accuracy", coord, entry)
        # 2.0/5.0 = 40%
        assert sensor.native_value == 40
        attrs = sensor.extra_state_attributes
        assert attrs["goal_met"] is False


class TestNoExtraAttrs:
    def test_sensors_without_attrs_return_none(self) -> None:
        coord, entry = _make_sensor_deps(state=CarmaboxState())
        for desc in SENSOR_DESCRIPTIONS:
            if desc.extra_attrs_fn is None:
                sensor = CarmaboxSensor(coord, entry, desc)
                assert sensor.extra_state_attributes is None


class TestApplianceSensors:
    """PLAT-943: Appliance category sensor tests."""

    def test_build_descriptions_creates_per_category(self) -> None:
        appliances = [
            {"entity_id": "sensor.tvatt", "name": "Tvättmaskin", "category": "laundry"},
            {"entity_id": "sensor.tork", "name": "Torktumlare", "category": "laundry"},
            {"entity_id": "sensor.miner", "name": "Miner", "category": "miner"},
        ]
        descs = _build_appliance_descriptions(appliances)
        assert len(descs) == 2  # laundry + miner
        keys = {d.key for d in descs}
        assert "appliance_laundry" in keys
        assert "appliance_miner" in keys

    def test_build_descriptions_empty_appliances(self) -> None:
        descs = _build_appliance_descriptions([])
        assert len(descs) == 0

    def test_appliance_value_reads_category_power(self) -> None:
        coord, _ = _make_sensor_deps()
        coord.appliance_power = {"laundry": 250.3, "miner": 800.0}
        fn = _appliance_value_factory("laundry")
        assert fn(coord) == 250.3

    def test_appliance_value_missing_category_returns_zero(self) -> None:
        coord, _ = _make_sensor_deps()
        coord.appliance_power = {}
        fn = _appliance_value_factory("pool")
        assert fn(coord) == 0.0

    def test_appliance_attrs_includes_energy_and_members(self) -> None:
        coord, _ = _make_sensor_deps()
        coord.appliance_energy_wh = {"laundry": 1500.0}
        coord._appliances = [
            {"entity_id": "sensor.tvatt", "name": "Tvättmaskin", "category": "laundry"},
            {"entity_id": "sensor.tork", "name": "Torktumlare", "category": "laundry"},
            {"entity_id": "sensor.miner", "name": "Miner", "category": "miner"},
        ]
        fn = _appliance_attrs_factory("laundry")
        attrs = fn(coord)
        assert attrs["energy_today_kwh"] == 1.5
        assert len(attrs["appliances"]) == 2
        assert attrs["appliances"][0]["entity_id"] == "sensor.tvatt"

    def test_appliance_description_has_correct_metadata(self) -> None:
        appliances = [{"entity_id": "s.x", "name": "X", "category": "heating"}]
        descs = _build_appliance_descriptions(appliances)
        assert len(descs) == 1
        d = descs[0]
        assert d.key == "appliance_heating"
        assert d.native_unit_of_measurement == "W"
        assert d.icon == "mdi:lightning-bolt"


class TestStatusSensor:
    """PLAT-964: Transparency sensor tests."""

    def test_status_value_all_ok(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("status", coord, entry)
        assert sensor.native_value == "Allt fungerar"

    def test_status_value_offline(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.status_text = "Kontor offline"
        sensor = _get_sensor("status", coord, entry)
        assert sensor.native_value == "Kontor offline"

    def test_status_attrs_has_system_health(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("status", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert "system_health" in attrs
        assert isinstance(attrs["system_health"], dict)

    def test_status_never_shows_technical_errors(self) -> None:
        """Status should NEVER contain technical error messages."""
        coord, entry = _make_sensor_deps()
        coord.status_text = "Allt fungerar"
        sensor = _get_sensor("status", coord, entry)
        value = sensor.native_value
        # Should not contain exception names, tracebacks, etc
        assert "Error" not in value
        assert "Exception" not in value
        assert "Traceback" not in value


class TestPlanScoreSensor:
    """PLAT-966: Plan score sensor tests."""

    def test_plan_score_no_data(self) -> None:
        coord, entry = _make_sensor_deps()
        sensor = _get_sensor("plan_score", coord, entry)
        assert sensor.native_value is None

    def test_plan_score_with_data(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.plan_score = MagicMock(
            return_value={
                "score_today": 85.0,
                "score_7d": 80.0,
                "score_30d": 78.0,
                "trend": "improving",
            }
        )
        sensor = _get_sensor("plan_score", coord, entry)
        assert sensor.native_value == 85.0

    def test_plan_score_attrs(self) -> None:
        coord, entry = _make_sensor_deps()
        coord.plan_score = MagicMock(
            return_value={
                "score_today": 85.0,
                "score_7d": 80.0,
                "score_30d": 78.0,
                "trend": "improving",
            }
        )
        sensor = _get_sensor("plan_score", coord, entry)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["score_today"] == 85.0
        assert attrs["score_7d"] == 80.0
        assert attrs["score_30d"] == 78.0
        assert attrs["trend"] == "improving"

    def test_plan_score_has_percent_unit(self) -> None:
        """Plan score sensor should have % unit."""
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "plan_score")
        assert desc.native_unit_of_measurement == "%"
