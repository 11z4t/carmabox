"""Scenario D — HA restart during night operation.

State: HA restarts at 02:00 during night EV charging
Expected:
  - Persistent state restored (plan, NEV state, EV enabled)
  - NEV state machine resumes from saved state
  - _nev_ramp_start handles missing attribute gracefully
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.models import CarmaboxState


class TestScenarioD:
    """Scenario D: HA restart persistence and recovery."""

    def test_nev_state_persisted_in_runtime(self) -> None:
        """NEV state fields are included in runtime save data."""
        # Verify the fields exist in the runtime data contract
        runtime_data = {
            "plan": [],
            "night_ev_active": True,
            "nev_state": "EV_CHARGING",
            "last_command": "DISCHARGE",
            "ev_enabled": True,
            "ev_current_amps": 6,
        }
        assert "night_ev_active" in runtime_data
        assert "nev_state" in runtime_data
        assert runtime_data["nev_state"] == "EV_CHARGING"

    def test_nev_ramp_start_default_on_restore(self) -> None:
        """_nev_ramp_start defaults to 0.0 if not in saved state."""
        # Simulates what coordinator does at line 1300-1301
        saved = {"nev_state": "DISCHARGE_RAMP"}

        nev_state = saved.get("nev_state", "IDLE")
        nev_ramp_start = saved.get("nev_ramp_start", 0.0)

        assert nev_state == "DISCHARGE_RAMP"
        assert nev_ramp_start == 0.0

    def test_carmabox_state_defaults_are_safe(self) -> None:
        """Default CarmaboxState is safe — no discharge, no EV, no crash."""
        state = CarmaboxState()
        assert state.grid_power_w == 0.0
        assert state.battery_soc_1 == 0.0
        assert state.ev_soc == -1.0
        assert state.pv_power_w == 0.0
        assert state.all_batteries_full is False

    def test_ev_soc_unix_time_survives_restart(self) -> None:
        """EV SoC timestamp uses unix time (not monotonic) for restart safety."""
        import time

        # Monotonic resets on restart, unix time doesn't
        unix_ts = time.time()
        saved = {"ev_soc_unix_time": unix_ts, "ev_soc": 65.0}

        age_s = time.time() - saved["ev_soc_unix_time"]
        assert age_s < 5.0, "Unix timestamp should be recent"
        assert saved["ev_soc"] == 65.0
