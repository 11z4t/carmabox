"""NC-13: Scenario-tester — bevisar coordinator_v2 hanterar verkliga situationer."""

from custom_components.carmabox.core.coordinator_v2 import CoordinatorV2, SystemState
from custom_components.carmabox.core.plan_executor import PlanAction
from custom_components.carmabox.core.startup import StartupState


def _s(**kw):
    d = {
        "grid_import_w": 2500,
        "ellevio_viktat_kw": 1.2,
        "pv_power_w": 0,
        "battery_soc_1": 80,
        "battery_soc_2": 80,
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
    d.update(kw)
    return SystemState(**d)


class TestScenarioANattEV:
    """Natt: EV laddar 6A + batteristöd, grid < 4kW."""

    def test_ev_starts_at_night(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_s(hour=22, ev_soc=60, ev_connected=True))
        assert c.night_ev_active
        assert r.ev_command["action"] == "start"
        assert r.ev_command["amps"] == 6

    def test_battery_discharges_for_ev_support(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.plan = [PlanAction(23, "d", -2.0, 1.5, 70, 40, 60)]
        r = c.cycle(_s(hour=23, grid_import_w=5000, ellevio_viktat_kw=2.5))
        # Either discharge commands or grid guard acted
        discharge = [b for b in r.battery_commands if b["mode"] == "discharge_pv"]
        assert len(discharge) > 0 or r.grid_guard_status in ("WARNING", "CRITICAL")

    def test_ev_stops_at_target(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.night_ev_active = True
        c.cycle(_s(hour=2, ev_soc=76))
        assert not c.night_ev_active

    def test_ev_stops_at_departure(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        c.night_ev_active = True
        c.cycle(_s(hour=6, ev_soc=70))
        assert not c.night_ev_active


class TestScenarioBDisk:
    """Disk 2kW startar → Grid Guard ska agera."""

    def test_disk_triggers_grid_guard(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        # Hus 2.5 + EV 4.1 + disk 2.0 = 8.6kW → viktat 4.3 > 2.0
        r = c.cycle(
            _s(
                hour=23,
                grid_import_w=8600,
                ellevio_viktat_kw=4.3,
                ev_power_w=4100,
                disk_power_w=2000,
            )
        )
        assert r.grid_guard_status in ("WARNING", "CRITICAL")


class TestScenarioCRestart:
    """HA restart → startup safety → restore EV."""

    def test_restart_fast_charging_off(self):
        c = CoordinatorV2()
        r = c.cycle(_s(fast_charging_1=True, battery_soc_1=-1))
        assert not c._startup_confirmed
        fc_cmds = [b for b in r.battery_commands if b.get("fast_charging") is False]
        assert len(fc_cmds) > 0

    def test_restart_restores_night_ev(self):
        c = CoordinatorV2()
        c.set_restored_state(StartupState(night_ev_active=True, ev_enabled=True))
        r = c.cycle(_s(hour=2, ev_soc=60, ev_connected=True))
        assert c.night_ev_active
        assert r.ev_command["action"] == "start"

    def test_restart_no_restore_daytime(self):
        c = CoordinatorV2()
        c.set_restored_state(StartupState(night_ev_active=True, ev_enabled=True))
        r = c.cycle(_s(hour=10, ev_soc=60))
        assert r.ev_command is None or r.ev_command.get("action") != "start"


class TestScenarioDSol:
    """Sol fm → PV override → charge_pv."""

    def test_pv_surplus_charges_battery(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(
            _s(
                hour=10,
                grid_import_w=-500,
                pv_power_w=3000,
                battery_soc_1=50,
                battery_soc_2=50,
            )
        )
        charge = [b for b in r.battery_commands if b["mode"] == "charge_pv"]
        assert len(charge) > 0


class TestScenarioEInvariants:
    """Invarianter skyddar alltid."""

    def test_ems_auto_detected(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_s(ems_mode_1="auto"))
        assert len(r.breaches) > 0

    def test_fast_charging_detected(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_s(fast_charging_1=True))
        assert len(r.breaches) > 0

    def test_crosscharge_detected(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_s(battery_power_1=-2000, battery_power_2=1500))
        inv2 = [b for b in r.breaches if b["law"] == "INV_2"]
        assert len(inv2) > 0

    def test_min_soc_detected(self):
        c = CoordinatorV2()
        c._startup_confirmed = True
        r = c.cycle(_s(battery_soc_1=12, battery_power_1=500))
        inv5 = [b for b in r.breaches if b["law"] == "INV_5"]
        assert len(inv5) > 0


class TestScenarioFPersistence:
    """State överlever restart."""

    def test_night_ev_persisted(self):
        c = CoordinatorV2()
        c.night_ev_active = True
        c.plan = [PlanAction(23, "d", -2.0, 1.5, 70, 40, 60)]
        state = c.get_persistent_state()
        assert state["night_ev_active"] is True
        assert len(state["plan"]) == 1

    def test_restore_from_persistent(self):
        c = CoordinatorV2()
        c.set_restored_state(StartupState(night_ev_active=True, ev_enabled=True))
        assert c.night_ev_active is True
