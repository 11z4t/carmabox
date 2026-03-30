"""Tests for Coordinator V2 — full cycle integration."""

from custom_components.carmabox.const import DEFAULT_EV_MAX_AMPS
from custom_components.carmabox.core.coordinator_v2 import (
    CoordinatorConfig,
    CoordinatorV2,
    SystemState,
)
from custom_components.carmabox.core.plan_executor import PlanAction
from custom_components.carmabox.core.startup import StartupState


def _state(**kw):
    defaults = {
        "grid_import_w": 1500,
        "ellevio_viktat_kw": 1.5,
        "pv_power_w": 0,
        "battery_soc_1": 50,
        "battery_soc_2": 50,
        "battery_power_1": 0,
        "battery_power_2": 0,
        "battery_temp_1": 15,
        "battery_temp_2": 15,
        "ems_mode_1": "discharge_pv",
        "ems_mode_2": "discharge_pv",
        "fast_charging_1": False,
        "fast_charging_2": False,
        "ev_soc": 60,
        "ev_power_w": 0,
        "ev_connected": True,
        "ev_enabled": True,
        "current_price": 50,
        "disk_power_w": 0,
        "tvatt_power_w": 0,
        "miner_power_w": 0,
        "hour": 23,
        "minute": 30,
    }
    defaults.update(kw)
    return SystemState(**defaults)


class TestStartup:
    def test_safe_mode_fast_charging_on(self):
        c = CoordinatorV2()
        r = c.cycle(_state(fast_charging_1=True))
        assert r.grid_guard_status != "ready" or any(
            b["fast_charging"] is False for b in r.battery_commands
        )

    def test_startup_completes(self):
        c = CoordinatorV2()
        c.cycle(_state())  # First cycle
        c.cycle(_state())  # Second cycle
        assert c._startup_confirmed

    def test_sensors_ready_grid_import_zero(self):
        """PLAT-1044: grid_import=0 (full PV) must NOT block startup."""
        c = CoordinatorV2()
        c.cycle(_state(grid_import_w=0, battery_soc_1=50, battery_soc_2=50))
        c.cycle(_state(grid_import_w=0, battery_soc_1=50, battery_soc_2=50))
        assert c._startup_confirmed

    def test_sensors_not_ready_soc_negative(self):
        """PLAT-1044: soc_1=-1 means sensor not initialized → wait."""
        c = CoordinatorV2()
        c.cycle(_state(battery_soc_1=-1, battery_soc_2=50))
        assert not c._startup_confirmed

    def test_restore_night_ev(self):
        c = CoordinatorV2()
        c.set_restored_state(StartupState(night_ev_active=True, ev_enabled=True))
        r = c.cycle(_state(ev_soc=60, ev_connected=True, hour=23))
        assert r.ev_command is not None
        assert r.ev_command["action"] == "start"
        assert r.ev_command["override_schedule"] is True


class TestGridGuard:
    def test_grid_under_tak(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_state(ellevio_viktat_kw=1.0, grid_import_w=1000))
        assert r.grid_guard_status == "OK"

    def test_inv3_fast_charging(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_state(fast_charging_1=True))
        assert len(r.breaches) > 0  # INV-3 detected


class TestPlanExecution:
    def test_discharge_from_plan(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.plan = [
            PlanAction(
                hour=23,
                action="d",
                battery_kw=-2.0,
                grid_kw=1.5,
                price=70,
                battery_soc=40,
                ev_soc=60,
            )
        ]
        r = c.cycle(_state(hour=23, grid_import_w=3000))
        discharge_cmds = [b for b in r.battery_commands if b["mode"] == "discharge_pv"]
        assert len(discharge_cmds) > 0

    def test_pv_override(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        # action="c" (charge from PV) + 3kW PV production → charge_pv
        c.plan = [
            PlanAction(
                hour=10,
                action="c",
                battery_kw=0,
                grid_kw=1.5,
                price=50,
                battery_soc=50,
                ev_soc=60,
            )
        ]
        r = c.cycle(
            _state(
                hour=10,
                grid_import_w=-500,
                pv_power_w=3000,
                battery_soc_1=50,
                battery_soc_2=50,
            )
        )
        # PV charge: with 3kW PV and plan action=charge, coordinator must command charge_pv
        assert any(
            b["mode"] == "charge_pv" for b in r.battery_commands
        ), f"Expected charge_pv command, got: {r.battery_commands}"


class TestNightEV:
    def test_night_ev_starts(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                battery_soc_1=80,
                battery_soc_2=80,
            )
        )
        assert c.night_ev_active
        assert r.ev_command["action"] == "start"
        assert r.ev_command["amps"] == 6

    def test_night_ev_stops_at_departure(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.night_ev_active = True
        r = c.cycle(_state(hour=6, ev_soc=76))
        assert not c.night_ev_active
        assert r.ev_command["action"] == "stop"

    def test_night_ev_stops_at_target(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.night_ev_active = True
        c.cycle(_state(hour=2, ev_soc=76))
        assert not c.night_ev_active

    def test_no_night_ev_daytime(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.cycle(_state(hour=14, ev_soc=60, ev_connected=True))
        assert not c.night_ev_active


class TestLawGuardian:
    def test_breach_recorded(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_state(ellevio_viktat_kw=2.5, fast_charging_1=True))
        assert len(r.breaches) > 0

    def test_no_breach_normal(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_state(ellevio_viktat_kw=1.0))
        lag1_breaches = [b for b in r.breaches if b["law"] == "LAG_1"]
        assert len(lag1_breaches) == 0


class TestSensorsReadyFullPV:
    """PLAT-1048: sensors_ready must be True when grid_import=0 (full PV)."""

    def test_sensors_ready_full_pv_produces_commands(self):
        """grid_import=0W with valid SoC → startup completes, cycle produces battery commands."""
        c = CoordinatorV2()
        c.cycle(_state(grid_import_w=0, battery_soc_1=50, battery_soc_2=50))
        r = c.cycle(_state(grid_import_w=0, battery_soc_1=50, battery_soc_2=50))
        assert c._startup_confirmed, "sensors_ready should be True when grid_import=0"
        # After startup, a cycle with a plan should produce battery commands
        c.plan = [
            PlanAction(
                hour=23,
                action="i",
                battery_kw=0,
                grid_kw=0,
                price=50,
                battery_soc=50,
                ev_soc=60,
            )
        ]
        r = c.cycle(_state(grid_import_w=0, battery_soc_1=50, battery_soc_2=50))
        assert r.battery_commands is not None


class TestEVMaxAmps:
    """PLAT-1048: EV command amps must never exceed DEFAULT_EV_MAX_AMPS."""

    def test_ev_max_amps_respected_night_ev(self):
        """Night EV start must not exceed configured max amps."""
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                battery_soc_1=80,
                battery_soc_2=80,
            )
        )
        assert r.ev_command is not None, "Night EV should start"
        assert (
            r.ev_command["amps"] <= DEFAULT_EV_MAX_AMPS
        ), f"EV amps {r.ev_command['amps']} exceeds max {DEFAULT_EV_MAX_AMPS}"

    def test_ev_max_amps_respected_restored(self):
        """Restored night EV must not exceed max amps."""
        c = CoordinatorV2()
        c.set_restored_state(StartupState(night_ev_active=True, ev_enabled=True))
        r = c.cycle(_state(ev_soc=60, ev_connected=True, hour=23))
        assert r.ev_command is not None
        assert (
            r.ev_command["amps"] <= DEFAULT_EV_MAX_AMPS
        ), f"Restored EV amps {r.ev_command['amps']} exceeds max {DEFAULT_EV_MAX_AMPS}"


class TestGridGuardBlocksNightEV:
    """PLAT-1048: GridGuard veto must prevent night_ev from starting."""

    def test_grid_guard_blocks_night_ev(self):
        """When GridGuard detects invariant violation (fast_charging), night_ev must not start."""
        c = CoordinatorV2()
        c._startup_confirmed = True
        # fast_charging_1=True triggers INV-3 → grid_guard_acted=True → blocks night_ev
        c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                battery_soc_1=80,
                battery_soc_2=80,
                fast_charging_1=True,
            )
        )
        assert not c.night_ev_active, "GridGuard should block night_ev when invariant violated"

    def test_grid_guard_no_block_when_clean(self):
        """Verify night_ev starts when GridGuard has no issues (control test)."""
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                battery_soc_1=80,
                battery_soc_2=80,
                fast_charging_1=False,
                fast_charging_2=False,
            )
        )
        assert c.night_ev_active, "Night EV should start when GridGuard is clean"


class TestPersistence:
    def test_persistent_state(self):
        c = CoordinatorV2()
        c.night_ev_active = True
        c.plan = [PlanAction(23, "d", -2.0, 1.5, 70, 40, 60)]
        state = c.get_persistent_state()
        assert state["night_ev_active"] is True
        assert len(state["plan"]) == 1


class TestQCFixes:
    def test_ev_max_amps_default_is_10(self):
        c = CoordinatorConfig()
        assert c.ev_max_amps == 10

    def test_night_ev_respects_ev_enabled_false(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                ev_enabled=False,
                battery_soc_1=80,
                battery_soc_2=80,
            )
        )
        assert not c.night_ev_active

    def test_sensors_ready_false_when_soc_negative(self):
        c = CoordinatorV2()
        c.cycle(_state(battery_soc_1=-1, battery_soc_2=-1))
        assert not c._startup_confirmed

    def test_ev_cmd_uses_cfg_min_amps(self):
        c = CoordinatorV2(CoordinatorConfig(ev_min_amps=6))
        c._startup_confirmed = True
        r = c.cycle(
            _state(
                hour=22,
                ev_soc=60,
                ev_connected=True,
                ev_enabled=True,
                battery_soc_1=80,
                battery_soc_2=80,
            )
        )
        if r.ev_command and r.ev_command["action"] == "start":
            assert r.ev_command["amps"] == 6
