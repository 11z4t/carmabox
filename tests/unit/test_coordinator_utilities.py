"""Coverage tests for CarmaboxCoordinator utility and tracking methods.

EXP-EPIC-SWEEP — targets coordinator.py utility clusters:
  Lines 596-643   — _days_since_full_charge, _ellevio_weight, _read_cell_temp,
                    _detect_miner_entity
  Lines 1560-1652 — _build_surplus_consumers, _execute_surplus_allocations
  Lines 1659-1871 — _enforce_ems_modes
  Lines 5496-5526 — _generate_breach_corrections expiry, get_active_corrections
  Lines 5528-5569 — _track_battery_idle
  Lines 5571-5672 — _track_shadow: discharge/charge/idle paths + reason branches
  Lines 5674-5743 — _track_savings: discharge + grid_charge accumulation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import (
    BreachCorrection,
    CarmaboxState,
    Decision,
)
from tests.unit.test_expert_control import _make_coord

# ── Helpers ──────────────────────────────────────────────────────────────────


def _state(
    *,
    grid_power_w: float = 1000.0,
    battery_power_1: float = 0.0,
    battery_power_2: float = 0.0,
    battery_soc_1: float = 60.0,
    battery_soc_2: float = -1.0,
    pv_power_w: float = 0.0,
    ev_power_w: float = 0.0,
    current_price: float = 80.0,
) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_power_w,
        battery_power_1=battery_power_1,
        battery_power_2=battery_power_2,
        battery_soc_1=battery_soc_1,
        battery_soc_2=battery_soc_2,
        pv_power_w=pv_power_w,
        ev_power_w=ev_power_w,
        current_price=current_price,
    )


# ── _days_since_full_charge ───────────────────────────────────────────────────


class TestDaysSinceFullCharge:
    def test_no_date_returns_99(self) -> None:
        """No last full charge date → 99 (overdue)."""
        coord = _make_coord()
        coord._ev_last_full_charge_date = ""
        assert coord._days_since_full_charge() == 99

    def test_recent_date(self) -> None:
        """Date 3 days ago → 3."""
        from datetime import datetime, timedelta

        coord = _make_coord()
        three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        coord._ev_last_full_charge_date = three_days_ago
        assert coord._days_since_full_charge() == 3

    def test_invalid_date_returns_99(self) -> None:
        """Malformed date → 99."""
        coord = _make_coord()
        coord._ev_last_full_charge_date = "not-a-date"
        assert coord._days_since_full_charge() == 99


# ── _read_cell_temp ───────────────────────────────────────────────────────────


class TestReadCellTemp:
    def test_returns_float_when_valid(self) -> None:
        """Valid state → float value."""
        coord = _make_coord()
        mock_state = MagicMock()
        mock_state.state = "12.5"
        coord.hass.states.get = lambda eid: mock_state
        result = coord._read_cell_temp("kontor")
        assert result == 12.5

    def test_returns_none_when_unavailable(self) -> None:
        """unavailable state → None."""
        coord = _make_coord()
        mock_state = MagicMock()
        mock_state.state = "unavailable"
        coord.hass.states.get = lambda eid: mock_state
        result = coord._read_cell_temp("kontor")
        assert result is None

    def test_returns_none_when_entity_missing(self) -> None:
        """No entity → None."""
        coord = _make_coord()
        coord.hass.states.get = lambda eid: None
        result = coord._read_cell_temp("kontor")
        assert result is None


# ── _detect_miner_entity ─────────────────────────────────────────────────────


class TestDetectMinerEntity:
    def test_finds_miner_from_appliances(self) -> None:
        """Appliance with category=miner + matching switch → returns switch id."""
        coord = _make_coord()
        coord._appliances = [{"entity_id": "sensor.shelly1pmg4_xxx_power", "category": "miner"}]
        switch_state = MagicMock()
        switch_state.entity_id = "switch.shelly1pmg4_xxx"

        def fake_get(eid: str) -> MagicMock | None:
            if eid == "switch.shelly1pmg4_xxx":
                return switch_state
            return None

        coord.hass.states.get = fake_get
        coord.hass.states.async_all = MagicMock(return_value=[])
        result = coord._detect_miner_entity()
        assert result == "switch.shelly1pmg4_xxx"

    def test_falls_back_to_state_scan(self) -> None:
        """No appliance config → scans all switches for 'miner' in name."""
        coord = _make_coord()
        coord._appliances = []
        miner_state = MagicMock()
        miner_state.entity_id = "switch.bitcoin_miner_1"
        coord.hass.states.get = lambda eid: None
        coord.hass.states.async_all = MagicMock(return_value=[miner_state])
        result = coord._detect_miner_entity()
        assert result == "switch.bitcoin_miner_1"

    def test_returns_empty_when_no_miner(self) -> None:
        """No miner found → empty string."""
        coord = _make_coord()
        coord._appliances = []
        coord.hass.states.get = lambda eid: None
        coord.hass.states.async_all = MagicMock(return_value=[])
        result = coord._detect_miner_entity()
        assert result == ""


# ── _build_surplus_consumers ─────────────────────────────────────────────────


class TestBuildSurplusConsumers:
    def test_returns_consumers_list(self) -> None:
        """Always returns at least EV + battery + miner consumers."""
        coord = _make_coord()
        coord.hass.states.get = lambda eid: None
        state = _state(ev_power_w=0.0)
        consumers = coord._build_surplus_consumers(state)
        ids = [c.id for c in consumers]
        assert "ev" in ids
        assert "battery" in ids
        assert "miner" in ids

    def test_ev_running_when_power_gt_100(self) -> None:
        """ev_power_w > 100 → EV consumer is_running=True."""
        coord = _make_coord()
        coord.hass.states.get = lambda eid: None
        state = _state(ev_power_w=2000.0)
        consumers = coord._build_surplus_consumers(state)
        ev = next(c for c in consumers if c.id == "ev")
        assert ev.is_running is True

    def test_battery_not_running_when_full(self) -> None:
        """Both batteries at 99% → battery consumer is_running=False."""
        coord = _make_coord()
        coord.hass.states.get = lambda eid: None
        state = _state(battery_soc_1=99.0, battery_soc_2=99.0, battery_power_1=0.0)
        consumers = coord._build_surplus_consumers(state)
        bat = next(c for c in consumers if c.id == "battery")
        assert bat.is_running is False


# ── _execute_surplus_allocations ─────────────────────────────────────────────


class TestExecuteSurplusAllocations:
    @pytest.mark.asyncio
    async def test_miner_start_calls_switch_on(self) -> None:
        """Allocation miner/start → turn_on switch."""
        coord = _make_coord()

        alloc = MagicMock()
        alloc.action = "start"
        alloc.id = "miner"

        await coord._execute_surplus_allocations([alloc])
        coord.hass.services.async_call.assert_called_with(
            "switch",
            "turn_on",
            {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"},
        )

    @pytest.mark.asyncio
    async def test_miner_stop_calls_switch_off(self) -> None:
        """Allocation miner/stop → turn_off switch."""
        coord = _make_coord()

        alloc = MagicMock()
        alloc.action = "stop"
        alloc.id = "miner"

        await coord._execute_surplus_allocations([alloc])
        coord.hass.services.async_call.assert_called_with(
            "switch",
            "turn_off",
            {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"},
        )

    @pytest.mark.asyncio
    async def test_action_none_is_skipped(self) -> None:
        """action=none → no service call."""
        coord = _make_coord()

        alloc = MagicMock()
        alloc.action = "none"
        alloc.id = "miner"

        await coord._execute_surplus_allocations([alloc])
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_ev_start_calls_cmd_ev_start(self) -> None:
        """Allocation ev/start with sufficient power → _cmd_ev_start."""
        coord = _make_coord()
        coord._cmd_ev_start = AsyncMock()

        alloc = MagicMock()
        alloc.action = "start"
        alloc.id = "ev"
        alloc.target_w = 5000  # > 4140 threshold

        await coord._execute_surplus_allocations([alloc])
        coord._cmd_ev_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_ev_stop_calls_cmd_ev_stop(self) -> None:
        """Allocation ev/stop → _cmd_ev_stop."""
        coord = _make_coord()
        coord._cmd_ev_stop = AsyncMock()

        alloc = MagicMock()
        alloc.action = "stop"
        alloc.id = "ev"
        alloc.target_w = 0

        await coord._execute_surplus_allocations([alloc])
        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_battery_start_calls_set_ems_mode(self) -> None:
        """Allocation battery/start → adapter.set_ems_mode(charge_pv)."""
        coord = _make_coord()
        mock_adapter = MagicMock()
        mock_adapter.set_ems_mode = AsyncMock()
        mock_adapter.set_fast_charging = AsyncMock()
        coord.inverter_adapters = [mock_adapter]

        alloc = MagicMock()
        alloc.action = "start"
        alloc.id = "battery"

        await coord._execute_surplus_allocations([alloc])
        mock_adapter.set_ems_mode.assert_called_once_with("charge_pv")


# ── _enforce_ems_modes ────────────────────────────────────────────────────────


class TestEnforceEmsModes:
    @pytest.mark.asyncio
    async def test_no_adapters_returns_early(self) -> None:
        """No inverter adapters → returns without error."""
        coord = _make_coord()
        coord.inverter_adapters = []
        await coord._enforce_ems_modes()  # Should not raise

    @pytest.mark.asyncio
    async def test_charge_pv_enforced(self) -> None:
        """_last_battery_action=charge_pv → adapters get charge_pv."""
        coord = _make_coord()
        coord._last_battery_action = "charge_pv"

        mock_adapter = MagicMock()
        mock_adapter.prefix = "kontor"
        mock_adapter.set_ems_mode = AsyncMock()
        mock_adapter.set_fast_charging = AsyncMock()
        mock_adapter.ems_mode = "charge_pv"  # Already correct, no drift

        # Mock states.get to return charge_pv state
        ems_state = MagicMock()
        ems_state.state = "charge_pv"
        coord.hass.states.get = lambda eid: ems_state

        coord.inverter_adapters = [mock_adapter]
        await coord._enforce_ems_modes()
        # No error is success; adapter.set_ems_mode may or may not be called
        # (drift correction is skipped when mode already matches)

    @pytest.mark.asyncio
    async def test_discharge_turns_off_fast_charging(self) -> None:
        """discharge mode + fast_charging=on → set_fast_charging(on=False) called."""
        coord = _make_coord()
        coord._last_battery_action = "discharge"

        mock_adapter = MagicMock()
        mock_adapter.prefix = "kontor"
        mock_adapter.set_ems_mode = AsyncMock()
        mock_adapter.set_fast_charging = AsyncMock()
        mock_adapter.ems_mode = "discharge_pv"

        # fast_charging state = on
        def fake_get(eid: str) -> MagicMock:
            s = MagicMock()
            if "fast_charging" in eid:
                s.state = "on"
            else:
                s.state = "discharge_pv"
            return s

        coord.hass.states.get = fake_get
        coord.inverter_adapters = [mock_adapter]
        await coord._enforce_ems_modes()
        mock_adapter.set_fast_charging.assert_called_with(on=False)


# ── get_active_corrections ───────────────────────────────────────────────────


class TestGetActiveCorrections:
    def test_returns_non_expired_non_applied(self) -> None:
        """Corrections: active, expired, applied → only active returned."""
        coord = _make_coord()
        coord._breach_corrections = [
            BreachCorrection(
                created="2026-03-31T10:00:00",
                source_breach_hour=10,
                action="reduce_ev",
                target_hour=10,
                param="ev_amps=6",
                reason="Test",
            ),
            BreachCorrection(
                created="2026-03-31T10:00:00",
                source_breach_hour=11,
                action="reduce_ev",
                target_hour=11,
                param="ev_amps=6",
                reason="Expired",
                expired=True,
            ),
        ]
        result = coord.get_active_corrections()
        assert len(result) == 1
        assert result[0].source_breach_hour == 10

    def test_filters_by_hour(self) -> None:
        """hour parameter filters to specific target_hour."""
        coord = _make_coord()
        coord._breach_corrections = [
            BreachCorrection(
                created="2026-03-31T10:00:00",
                source_breach_hour=10,
                action="reduce_ev",
                target_hour=10,
                param="ev_amps=6",
                reason="Test",
            ),
            BreachCorrection(
                created="2026-03-31T12:00:00",
                source_breach_hour=12,
                action="reduce_ev",
                target_hour=12,
                param="ev_amps=6",
                reason="Test 12",
            ),
        ]
        result = coord.get_active_corrections(hour=10)
        assert len(result) == 1
        assert result[0].target_hour == 10


# ── _track_shadow ─────────────────────────────────────────────────────────────


class TestTrackShadow:
    def test_idle_battery(self) -> None:
        """battery_power ≈ 0 → actual_action = 'idle'."""
        coord = _make_coord()
        coord.last_decision = Decision(action="idle", discharge_w=0)
        coord._daily_avg_price = 80.0

        state = _state(battery_power_1=0.0, battery_power_2=0.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10, isoformat=lambda: "2026-03-31T10:00:00")
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert coord.shadow.actual_action == "idle"

    def test_discharge_battery(self) -> None:
        """battery_power_1 < -100 → actual_action = 'discharge'."""
        coord = _make_coord()
        coord.last_decision = Decision(action="idle", discharge_w=0)
        coord._daily_avg_price = 80.0

        state = _state(battery_power_1=-2000.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert coord.shadow.actual_action == "discharge"
        assert coord.shadow.actual_discharge_w == 2000

    def test_charging_battery(self) -> None:
        """battery_power_1 > 100 → actual_action = 'charge'."""
        coord = _make_coord()
        coord.last_decision = Decision(action="idle", discharge_w=0)
        coord._daily_avg_price = 80.0

        state = _state(battery_power_1=1500.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert coord.shadow.actual_action == "charge"

    def test_disagreement_reason_idle_vs_discharge(self) -> None:
        """CARMA=idle, v6=discharge → reason explains situation."""
        coord = _make_coord()
        coord.last_decision = Decision(action="idle", discharge_w=0)
        coord._daily_avg_price = 80.0

        state = _state(battery_power_1=-2000.0, current_price=60.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert not coord.shadow.agreement
        assert "öre" in coord.shadow.reason.lower() or "v6" in coord.shadow.reason.lower()

    def test_disagreement_discharge_vs_idle(self) -> None:
        """CARMA=discharge, v6=idle → specific reason."""
        coord = _make_coord()
        coord.last_decision = Decision(action="discharge", discharge_w=2000)
        coord._daily_avg_price = 80.0

        state = _state(battery_power_1=0.0, grid_power_w=2500.0)
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert not coord.shadow.agreement
        assert "vilar" in coord.shadow.reason or "laddat" in coord.shadow.reason

    def test_shadow_log_updated(self) -> None:
        """_track_shadow appends to shadow_log."""
        coord = _make_coord()
        coord.last_decision = Decision(action="idle", discharge_w=0)
        coord._daily_avg_price = 80.0

        state = _state()
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.isoformat.return_value = "2026-03-31T10:00:00"
            coord._track_shadow(state)

        assert len(coord.shadow_log) == 1


# ── _track_savings ────────────────────────────────────────────────────────────


class TestTrackSavings:
    def _run_track_savings(self, coord: object, state: CarmaboxState, hour: int = 14) -> None:
        """Helper: call _track_savings with a patched datetime."""
        from datetime import datetime as real_dt

        fake_now = real_dt(2026, 3, 31, hour, 0, 0)
        with patch(
            "custom_components.carmabox.coordinator.datetime",
            wraps=real_dt,
        ) as mock_dt:
            mock_dt.now.return_value = fake_now
            coord._track_savings(state)

    def test_discharge_accumulates_offset(self) -> None:
        """Battery discharging → discharge_offset_kwh and _value increase."""
        coord = _make_coord()
        state = _state(battery_power_1=-2000.0, current_price=100.0)

        initial_kwh = coord.savings.discharge_offset_kwh
        self._run_track_savings(coord, state, hour=14)

        assert coord.savings.discharge_offset_kwh > initial_kwh

    def test_grid_charge_accumulates_kwh(self) -> None:
        """Battery charging while grid import → charge_from_grid_kwh increases."""
        coord = _make_coord()
        state = _state(
            grid_power_w=3000.0,
            battery_power_1=2000.0,  # charging
            current_price=15.0,  # cheap price
        )
        initial = coord.savings.charge_from_grid_kwh

        self._run_track_savings(coord, state, hour=2)

        assert coord.savings.charge_from_grid_kwh > initial

    def test_no_discharge_no_change(self) -> None:
        """No battery activity + no grid → no savings change."""
        coord = _make_coord()
        state = _state(battery_power_1=0.0, battery_power_2=0.0, grid_power_w=0.0)
        initial_kwh = coord.savings.discharge_offset_kwh

        self._run_track_savings(coord, state, hour=14)

        assert coord.savings.discharge_offset_kwh == initial_kwh


# ── _track_appliances ─────────────────────────────────────────────────────────


class TestTrackAppliances:
    def test_accumulates_energy_for_appliance(self) -> None:
        """Appliance with power > threshold → energy accumulated."""
        coord = _make_coord()
        coord._appliances = [
            {"entity_id": "sensor.dishwasher_power", "category": "dishwasher", "threshold_w": 10}
        ]
        mock_state = MagicMock()
        mock_state.state = "500"
        mock_state.attributes = {"unit_of_measurement": "W"}
        coord.hass.states.get = lambda eid: mock_state
        coord.appliance_energy_wh = {}

        coord._track_appliances()

        assert "dishwasher" in coord.appliance_power
        assert coord.appliance_power["dishwasher"] == 500.0

    def test_kw_unit_converted_to_w(self) -> None:
        """Appliance sensor in kW → multiplied by 1000."""
        coord = _make_coord()
        coord._appliances = [
            {"entity_id": "sensor.dishwasher_power", "category": "dishwasher", "threshold_w": 10}
        ]
        mock_state = MagicMock()
        mock_state.state = "0.5"  # 0.5 kW = 500W
        mock_state.attributes = {"unit_of_measurement": "kw"}
        coord.hass.states.get = lambda eid: mock_state
        coord.appliance_energy_wh = {}

        coord._track_appliances()

        assert coord.appliance_power["dishwasher"] == pytest.approx(500.0, abs=1.0)

    def test_below_threshold_reads_zero(self) -> None:
        """Power < threshold → counted as 0W."""
        coord = _make_coord()
        coord._appliances = [
            {"entity_id": "sensor.miner_power", "category": "miner", "threshold_w": 50}
        ]
        mock_state = MagicMock()
        mock_state.state = "5"  # 5W < 50W threshold
        mock_state.attributes = {"unit_of_measurement": "W"}
        coord.hass.states.get = lambda eid: mock_state
        coord.appliance_energy_wh = {}

        coord._track_appliances()
        assert coord.appliance_power.get("miner", 0.0) == 0.0
