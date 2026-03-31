"""Tests for StateManager — PLAT-1140: HA entity state reader.

Verifierar read_float, read_float_or_none, read_str,
read_battery_temp och collect_state isolerat från coordinator.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.core.state_manager import StateManager
from custom_components.carmabox.optimizer.models import CarmaboxState

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_mgr(cfg: dict | None = None) -> StateManager:
    """Create StateManager with mocked hass."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    hass.states.get = lambda eid: states.get(eid)
    mgr = StateManager(hass, cfg or {})
    mgr._test_states = states  # type: ignore[attr-defined]
    return mgr


def _set(mgr: StateManager, entity_id: str, value: str) -> None:
    """Set a mock entity state on the manager's hass."""
    s = MagicMock()
    s.state = value
    mgr._test_states[entity_id] = s  # type: ignore[attr-defined]


def _make_adapter(soc: float = 50.0, power_w: float = 0.0, prefix: str = "a") -> MagicMock:
    a = MagicMock()
    a.soc = soc
    a.power_w = power_w
    a.ems_mode = "charge_pv"
    a.temperature_c = None
    a.prefix = prefix
    return a


def _make_ev_adapter(power_w: float = 0.0, current_a: float = 0.0) -> MagicMock:
    ev = MagicMock()
    ev.power_w = power_w
    ev.current_a = current_a
    ev.status = "charging"
    return ev


# ── read_float ────────────────────────────────────────────────────────────────


class TestReadFloat:
    def test_valid_float_returned(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "42.5")
        assert mgr.read_float("sensor.test") == 42.5

    def test_empty_entity_id_returns_default(self) -> None:
        assert _make_mgr().read_float("") == 0.0

    def test_custom_default_returned(self) -> None:
        assert _make_mgr().read_float("sensor.missing", -1.0) == -1.0

    def test_unknown_state_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "unknown")
        assert mgr.read_float("sensor.test") == 0.0

    def test_unavailable_state_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "unavailable")
        assert mgr.read_float("sensor.test") == 0.0

    def test_empty_string_state_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "")
        assert mgr.read_float("sensor.test") == 0.0

    def test_non_numeric_string_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "not_a_number")
        assert mgr.read_float("sensor.test") == 0.0

    def test_unreasonable_value_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "200000")
        assert mgr.read_float("sensor.test") == 0.0

    def test_negative_unreasonable_value_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "-200000")
        assert mgr.read_float("sensor.test") == 0.0

    def test_boundary_value_accepted(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "99999")
        assert mgr.read_float("sensor.test") == 99999.0

    def test_missing_entity_returns_default(self) -> None:
        mgr = _make_mgr()
        assert mgr.read_float("sensor.nonexistent") == 0.0

    def test_negative_valid_value_returned(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "-500.0")
        assert mgr.read_float("sensor.test") == -500.0


# ── read_float_or_none ────────────────────────────────────────────────────────


class TestReadFloatOrNone:
    def test_valid_float_returned(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "1234.5")
        assert mgr.read_float_or_none("sensor.test") == 1234.5

    def test_empty_entity_id_returns_none(self) -> None:
        assert _make_mgr().read_float_or_none("") is None

    def test_unavailable_returns_none(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "unavailable")
        assert mgr.read_float_or_none("sensor.test") is None

    def test_unknown_returns_none(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "unknown")
        assert mgr.read_float_or_none("sensor.test") is None

    def test_missing_entity_returns_none(self) -> None:
        assert _make_mgr().read_float_or_none("sensor.missing") is None

    def test_unreasonable_value_returns_none(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "sensor.test", "999999")
        assert mgr.read_float_or_none("sensor.test") is None


# ── read_str ──────────────────────────────────────────────────────────────────


class TestReadStr:
    def test_valid_string_returned(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "select.mode", "charge_pv")
        assert mgr.read_str("select.mode") == "charge_pv"

    def test_empty_entity_id_returns_default(self) -> None:
        assert _make_mgr().read_str("") == ""

    def test_unavailable_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "select.mode", "unavailable")
        assert mgr.read_str("select.mode") == ""

    def test_unknown_returns_default(self) -> None:
        mgr = _make_mgr()
        _set(mgr, "select.mode", "unknown")
        assert mgr.read_str("select.mode") == ""

    def test_missing_entity_returns_default(self) -> None:
        assert _make_mgr().read_str("select.missing") == ""

    def test_custom_default_returned(self) -> None:
        assert _make_mgr().read_str("select.missing", "fallback") == "fallback"


# ── read_battery_temp ─────────────────────────────────────────────────────────


class TestReadBatteryTemp:
    def test_returns_min_temp_from_adapters(self) -> None:
        mgr = _make_mgr()
        a1 = _make_adapter()
        a1.temperature_c = 20.0
        a2 = _make_adapter()
        a2.temperature_c = 15.0
        assert mgr.read_battery_temp([a1, a2]) == 15.0

    def test_returns_none_when_no_adapters_and_no_entity(self) -> None:
        mgr = _make_mgr({})
        assert mgr.read_battery_temp([]) is None

    def test_returns_none_when_adapter_temp_is_none(self) -> None:
        mgr = _make_mgr()
        a1 = _make_adapter()
        a1.temperature_c = None
        assert mgr.read_battery_temp([a1]) is None

    def test_falls_back_to_entity_when_no_adapters(self) -> None:
        mgr = _make_mgr({"battery_temp_entity": "sensor.batt_temp"})
        _set(mgr, "sensor.batt_temp", "32.5")
        assert mgr.read_battery_temp([]) == 32.5

    def test_entity_unavailable_returns_none(self) -> None:
        mgr = _make_mgr({"battery_temp_entity": "sensor.batt_temp"})
        _set(mgr, "sensor.batt_temp", "unavailable")
        assert mgr.read_battery_temp([]) is None

    def test_adapter_takes_priority_over_entity(self) -> None:
        mgr = _make_mgr({"battery_temp_entity": "sensor.batt_temp"})
        _set(mgr, "sensor.batt_temp", "99.0")
        a1 = _make_adapter()
        a1.temperature_c = 25.0
        assert mgr.read_battery_temp([a1]) == 25.0


# ── collect_state ─────────────────────────────────────────────────────────────


class TestCollectState:
    def test_reads_grid_power_from_entity(self) -> None:
        mgr = _make_mgr({"grid_entity": "sensor.grid"})
        _set(mgr, "sensor.grid", "2500")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.grid_power_w == 2500.0

    def test_reads_battery_soc_from_config_entity(self) -> None:
        mgr = _make_mgr({"battery_soc_1": "sensor.soc1"})
        _set(mgr, "sensor.soc1", "85")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.battery_soc_1 == 85.0

    def test_battery_soc_from_adapter_takes_priority(self) -> None:
        mgr = _make_mgr({"battery_soc_1": "sensor.soc1"})
        _set(mgr, "sensor.soc1", "50")
        a1 = _make_adapter(soc=90.0)
        state = mgr.collect_state([a1], None, 2.0, [])
        assert state.battery_soc_1 == 90.0

    def test_two_adapters_read_both_batteries(self) -> None:
        mgr = _make_mgr()
        a1 = _make_adapter(soc=80.0, power_w=1000.0, prefix="a")
        a2 = _make_adapter(soc=60.0, power_w=500.0, prefix="b")
        state = mgr.collect_state([a1, a2], None, 2.0, [])
        assert state.battery_soc_1 == 80.0
        assert state.battery_soc_2 == 60.0

    def test_ev_adapter_power_and_current(self) -> None:
        mgr = _make_mgr()
        ev = _make_ev_adapter(power_w=3300.0, current_a=8.0)
        state = mgr.collect_state([], ev, 2.0, [])
        assert state.ev_power_w == 3300.0
        assert state.ev_current_a == 8.0

    def test_target_kw_and_plan_passed_through(self) -> None:
        mgr = _make_mgr()
        plan_mock = MagicMock()
        state = mgr.collect_state([], None, 3.5, [plan_mock])
        assert state.target_weighted_kw == 3.5
        assert state.plan == [plan_mock]

    def test_unavailable_entity_returns_zero_for_grid(self) -> None:
        mgr = _make_mgr({"grid_entity": "sensor.grid"})
        _set(mgr, "sensor.grid", "unavailable")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.grid_power_w == 0.0

    def test_battery_temp_from_entity_in_collect_state(self) -> None:
        mgr = _make_mgr({"battery_temp_entity": "sensor.batt_temp"})
        _set(mgr, "sensor.batt_temp", "28.5")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.battery_temp_c == 28.5

    def test_battery_temp_none_when_not_configured(self) -> None:
        mgr = _make_mgr({})
        state = mgr.collect_state([], None, 2.0, [])
        assert state.battery_temp_c is None

    def test_battery_power_validity_true_when_available(self) -> None:
        """PLAT-946: battery_power_1_valid=True when sensor is readable."""
        mgr = _make_mgr({"battery_power_1": "sensor.bat_pwr"})
        _set(mgr, "sensor.bat_pwr", "500.0")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.battery_power_1_valid is True

    def test_battery_power_validity_false_when_unavailable(self) -> None:
        """PLAT-946: battery_power_1_valid=False when sensor is unavailable."""
        mgr = _make_mgr({"battery_power_1": "sensor.bat_pwr"})
        _set(mgr, "sensor.bat_pwr", "unavailable")
        state = mgr.collect_state([], None, 2.0, [])
        assert state.battery_power_1_valid is False

    def test_returns_carmabox_state_instance(self) -> None:
        mgr = _make_mgr()
        result = mgr.collect_state([], None, 2.0, [])
        assert isinstance(result, CarmaboxState)
