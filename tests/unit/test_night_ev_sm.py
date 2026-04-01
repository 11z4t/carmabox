"""Tests for Night EV State Machine (core/night_ev.py)."""

import time

from custom_components.carmabox.core.night_ev import NevState, decide_nev


def _state(**kw) -> NevState:
    defaults = dict(  # noqa: C408
        is_night=True,
        ev_connected=True,
        ev_soc=60.0,
        ev_target=75.0,
        battery_soc=80.0,
        min_soc=15.0,
        grid_w=2000.0,
        target_kw=2.0,
        night_weight=0.5,
        appliance_w=0.0,
        hour=23,
    )
    defaults.update(kw)
    return NevState(**defaults)


class TestIdleToDischargeRamp:
    def test_starts_when_conditions_met(self):
        s = _state()
        new_state, cmd = decide_nev(s, "IDLE", 0)
        assert new_state == "DISCHARGE_RAMP"
        assert cmd.action == "start_discharge"

    def test_stays_idle_if_not_night(self):
        s = _state(is_night=False)
        new_state, cmd = decide_nev(s, "IDLE", 0)
        assert new_state == "IDLE"
        assert cmd.action == "none"

    def test_stays_idle_if_ev_not_connected(self):
        s = _state(ev_connected=False)
        new_state, cmd = decide_nev(s, "IDLE", 0)
        assert new_state == "IDLE"

    def test_stays_idle_if_battery_too_low(self):
        s = _state(battery_soc=18.0)  # min_soc=15 + 5 = 20
        new_state, cmd = decide_nev(s, "IDLE", 0)
        assert new_state == "IDLE"


class TestDischargeRamp:
    def test_waits_for_stabilization(self):
        s = _state()
        new_state, cmd = decide_nev(s, "DISCHARGE_RAMP", time.monotonic())
        assert new_state == "DISCHARGE_RAMP"
        assert cmd.action == "none"

    def test_starts_ev_after_ramp(self):
        s = _state()
        new_state, cmd = decide_nev(s, "DISCHARGE_RAMP", time.monotonic() - 10)
        assert new_state == "EV_CHARGING"
        assert cmd.action == "start_ev"
        assert cmd.ev_amps == 6


class TestEvCharging:
    def test_battery_depleted_stops_ev(self):
        s = _state(battery_soc=15.0)  # == min_soc
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "BATTERY_DEPLETED"
        assert cmd.action == "stop_ev"

    def test_appliance_pauses_ev(self):
        s = _state(appliance_w=1500.0)
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "APPLIANCE_PAUSE"
        assert cmd.action == "stop_ev"

    def test_grid_over_target_increases_discharge(self):
        # target_kw=2.0, night_weight=0.5 → actual=4000W
        # grid=5000 > 4000*1.05=4200 → increase
        s = _state(grid_w=5000.0)
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "EV_CHARGING"
        assert cmd.action == "increase_discharge"
        assert cmd.discharge_w == 1000  # 5000 - 4000

    def test_ev_target_reached_stops(self):
        s = _state(ev_soc=80.0, ev_target=75.0)
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "IDLE"
        assert cmd.action == "stop_ev"

    def test_morning_stops(self):
        s = _state(is_night=False)
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "IDLE"
        assert cmd.action == "stop_ev"

    def test_stable_charging_no_action(self):
        s = _state(grid_w=3500.0)  # Under target 4000
        new_state, cmd = decide_nev(s, "EV_CHARGING", 0)
        assert new_state == "EV_CHARGING"
        assert cmd.action == "none"


class TestAppliancePause:
    def test_resumes_when_appliance_done(self):
        s = _state(appliance_w=50.0)
        new_state, cmd = decide_nev(s, "APPLIANCE_PAUSE", 0)
        assert new_state == "DISCHARGE_RAMP"
        assert cmd.action == "start_discharge"

    def test_stays_paused_while_running(self):
        s = _state(appliance_w=1500.0)
        new_state, cmd = decide_nev(s, "APPLIANCE_PAUSE", 0)
        assert new_state == "APPLIANCE_PAUSE"
        assert cmd.action == "none"

    def test_battery_depleted_during_pause(self):
        s = _state(appliance_w=1500.0, battery_soc=15.0)
        new_state, cmd = decide_nev(s, "APPLIANCE_PAUSE", 0)
        assert new_state == "BATTERY_DEPLETED"


class TestBatteryDepleted:
    def test_stays_depleted_at_night(self):
        s = _state(battery_soc=15.0)
        new_state, cmd = decide_nev(s, "BATTERY_DEPLETED", 0)
        assert new_state == "BATTERY_DEPLETED"

    def test_resets_at_morning(self):
        s = _state(is_night=False, battery_soc=15.0)
        new_state, cmd = decide_nev(s, "BATTERY_DEPLETED", 0)
        assert new_state == "IDLE"
