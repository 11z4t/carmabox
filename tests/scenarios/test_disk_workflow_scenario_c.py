"""Scenario C — Diskmaskin under natt-EV.

State: EV charging at night, dishwasher starts 23:30 (90 min, ~2kW)
Expected:
  - NEV state machine detects appliance > APPLIANCE_PAUSE_THRESHOLD_W
  - EV pauses within one cycle
  - Grid stays under target throughout
  - EV resumes when appliance finishes (< APPLIANCE_RESUME_THRESHOLD_W)
"""

from __future__ import annotations

from custom_components.carmabox.const import (
    APPLIANCE_PAUSE_THRESHOLD_W,
    APPLIANCE_RESUME_THRESHOLD_W,
)
from custom_components.carmabox.core.night_ev import NevState, decide_nev


class TestScenarioC:
    """Scenario C: Dishwasher during night EV charging."""

    def _make_nev_state(
        self,
        appliance_w: float = 0.0,
        battery_soc: float = 60.0,
    ) -> NevState:
        return NevState(
            is_night=True,
            ev_connected=True,
            ev_soc=50.0,
            ev_target=75.0,
            battery_soc=battery_soc,
            min_soc=15.0,
            grid_w=1500.0,
            target_kw=2.0,
            night_weight=0.5,
            appliance_w=appliance_w,
            hour=23,
        )

    def test_appliance_pauses_ev(self) -> None:
        """Dishwasher > threshold → NEV transitions to APPLIANCE_PAUSE."""
        state = self._make_nev_state(appliance_w=2000.0)
        new_state, cmd = decide_nev(state, "EV_CHARGING", 0.0)
        assert new_state == "APPLIANCE_PAUSE", f"Expected APPLIANCE_PAUSE, got {new_state}"
        assert cmd.action == "stop_ev"

    def test_appliance_below_threshold_no_pause(self) -> None:
        """Appliance below threshold → EV continues charging."""
        state = self._make_nev_state(appliance_w=300.0)
        new_state, cmd = decide_nev(state, "EV_CHARGING", 0.0)
        assert new_state == "EV_CHARGING"

    def test_ev_resumes_after_appliance(self) -> None:
        """Appliance finishes (< resume threshold) → back to DISCHARGE_RAMP."""
        state = self._make_nev_state(appliance_w=50.0)
        new_state, cmd = decide_nev(state, "APPLIANCE_PAUSE", 0.0)
        assert new_state == "DISCHARGE_RAMP", f"Expected DISCHARGE_RAMP, got {new_state}"
        assert cmd.action == "start_discharge"

    def test_appliance_still_running_stays_paused(self) -> None:
        """Appliance still running → stay in APPLIANCE_PAUSE."""
        state = self._make_nev_state(appliance_w=1500.0)
        new_state, cmd = decide_nev(state, "APPLIANCE_PAUSE", 0.0)
        assert new_state == "APPLIANCE_PAUSE"

    def test_threshold_constants_correct(self) -> None:
        """Verify threshold constants are named (not magic numbers)."""
        assert APPLIANCE_PAUSE_THRESHOLD_W == 500
        assert APPLIANCE_RESUME_THRESHOLD_W == 100
