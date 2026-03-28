"""Tests for Startup Safety."""

from __future__ import annotations

from custom_components.carmabox.core.startup import (
    StartupState,
    evaluate_startup,
)


class TestStartupSafety:
    def test_fast_charging_not_confirmed(self):
        """Always wait until fast_charging confirmed OFF."""
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=False,
            restored_state=None,
            is_night=True,
            ev_connected=True,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "safe_mode"
        assert cmd.fast_charging_off is True
        assert cmd.set_standby is True

    def test_sensors_not_ready(self):
        """Wait for sensors before acting."""
        cmd = evaluate_startup(
            sensors_ready=False,
            fast_charging_confirmed_off=True,
            restored_state=None,
            is_night=True,
            ev_connected=True,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "wait"

    def test_restore_night_ev(self):
        """Night EV was active → restore after restart."""
        state = StartupState(night_ev_active=True, ev_enabled=True)
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=state,
            is_night=True,
            ev_connected=True,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "restore_ev"
        assert cmd.start_ev is True
        assert cmd.ev_amps == 6  # ALWAYS 6A
        assert cmd.override_schedule is True

    def test_no_restore_daytime(self):
        """Night EV was active but it's daytime → don't restore."""
        state = StartupState(night_ev_active=True)
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=state,
            is_night=False,
            ev_connected=True,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "ready"
        assert cmd.start_ev is False

    def test_no_restore_ev_full(self):
        """Night EV was active but EV already at target → don't restore."""
        state = StartupState(night_ev_active=True)
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=state,
            is_night=True,
            ev_connected=True,
            ev_soc=80,
            ev_target_soc=75,
        )
        assert cmd.action == "ready"

    def test_no_restore_ev_disconnected(self):
        """Night EV was active but cable disconnected → don't restore."""
        state = StartupState(night_ev_active=True)
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=state,
            is_night=True,
            ev_connected=False,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "ready"

    def test_no_state_ready(self):
        """No persistent state → ready for normal operation."""
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=None,
            is_night=True,
            ev_connected=True,
            ev_soc=60,
            ev_target_soc=75,
        )
        assert cmd.action == "ready"

    def test_ev_soc_unavailable(self):
        """EV SoC unavailable (-1) → don't restore."""
        state = StartupState(night_ev_active=True)
        cmd = evaluate_startup(
            sensors_ready=True,
            fast_charging_confirmed_off=True,
            restored_state=state,
            is_night=True,
            ev_connected=True,
            ev_soc=-1,
            ev_target_soc=75,
        )
        assert cmd.action == "ready"
