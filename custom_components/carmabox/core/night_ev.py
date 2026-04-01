"""Night EV State Machine — robust EV charging with battery support.

Pure logic module — no HA imports. Coordinator calls this every 30s cycle.

States:
    IDLE → DISCHARGE_RAMP → EV_CHARGING → APPLIANCE_PAUSE → EV_CHARGING
                                        → BATTERY_DEPLETED → IDLE

Principles:
    1. LAG 1 trumfar ALLT — grid ALDRIG över target
    2. Battery discharge starts BEFORE EV (5s ramp)
    3. Appliances (disk, tvätt) pause EV automatically
    4. Battery depleted → EV stops, waits for morning
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..const import (
    APPLIANCE_PAUSE_THRESHOLD_W,
    APPLIANCE_RESUME_THRESHOLD_W,
    DEFAULT_EV_MIN_AMPS,
    NEV_DISCHARGE_RAMP_S,
    NEV_GRID_OVERSHOOT_FACTOR,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NevState:
    """Night EV state snapshot for decision making."""

    is_night: bool
    ev_connected: bool
    ev_soc: float  # -1 if unknown, use last_known
    ev_target: float
    battery_soc: float
    min_soc: float
    grid_w: float
    target_kw: float  # Weighted target (e.g. 2.0)
    night_weight: float  # e.g. 0.5
    appliance_w: float  # Total appliance power (disk + tvätt + tork)
    hour: int


@dataclass
class NevCommand:
    """What the state machine wants the coordinator to do."""

    action: str  # "start_discharge", "start_ev", "stop_ev", "increase_discharge", "none"
    discharge_w: int = 0
    ev_amps: int = DEFAULT_EV_MIN_AMPS
    reason: str = ""


def decide_nev(
    state: NevState,
    current_state: str,  # IDLE/DISCHARGE_RAMP/EV_CHARGING/APPLIANCE_PAUSE/BATTERY_DEPLETED
    ramp_start: float,  # time.monotonic() when ramp started
) -> tuple[str, NevCommand]:
    """Pure function: given current state + inputs, return next state + command.

    Returns (new_state, command).
    """
    actual_target_w = state.target_kw * 1000 / max(0.1, state.night_weight)

    if current_state == "IDLE":
        if (
            state.is_night
            and state.ev_connected
            and 0 <= state.ev_soc < state.ev_target
            and state.battery_soc > state.min_soc + 5  # Enough battery to support
        ):
            return "DISCHARGE_RAMP", NevCommand(
                action="start_discharge",
                discharge_w=2000,
                reason=f"NEV: SoC {state.ev_soc:.0f}%<{state.ev_target:.0f}% → discharge ramp",
            )
        return "IDLE", NevCommand(action="none")

    elif current_state == "DISCHARGE_RAMP":
        elapsed = time.monotonic() - ramp_start
        if elapsed >= NEV_DISCHARGE_RAMP_S:
            return "EV_CHARGING", NevCommand(
                action="start_ev",
                ev_amps=DEFAULT_EV_MIN_AMPS,
                reason=f"Discharge stable ({elapsed:.0f}s), starting EV {DEFAULT_EV_MIN_AMPS}A",
            )
        return "DISCHARGE_RAMP", NevCommand(action="none", reason="Discharge stabilizing")

    elif current_state == "EV_CHARGING":
        # Check 1: Battery depleted?
        if state.battery_soc <= state.min_soc:
            return "BATTERY_DEPLETED", NevCommand(
                action="stop_ev",
                reason=f"Battery depleted ({state.battery_soc:.0f}% <= min {state.min_soc:.0f}%)",
            )

        # Check 2: Appliance running?
        if state.appliance_w > APPLIANCE_PAUSE_THRESHOLD_W:
            return "APPLIANCE_PAUSE", NevCommand(
                action="stop_ev",
                reason=f"Appliance {state.appliance_w:.0f}W → EV paused",
            )

        # Check 3: Grid over target? Increase discharge
        if state.grid_w > actual_target_w * NEV_GRID_OVERSHOOT_FACTOR:
            needed_w = int(state.grid_w - actual_target_w)
            return "EV_CHARGING", NevCommand(
                action="increase_discharge",
                discharge_w=needed_w,
                reason=f"Grid {state.grid_w:.0f}W>target → +{needed_w}W discharge",
            )

        # Check 4: EV target reached or departure?
        if state.ev_soc >= state.ev_target:
            return "IDLE", NevCommand(
                action="stop_ev",
                reason=f"EV target reached ({state.ev_soc:.0f}% >= {state.ev_target:.0f}%)",
            )
        if not state.is_night:
            return "IDLE", NevCommand(
                action="stop_ev",
                reason="Morning — stopping night EV",
            )

        return "EV_CHARGING", NevCommand(action="none", reason="Charging stable")

    elif current_state == "APPLIANCE_PAUSE":
        # Battery depleted while paused?
        if state.battery_soc <= state.min_soc:
            return "BATTERY_DEPLETED", NevCommand(
                action="none",
                reason="Battery depleted during appliance pause",
            )
        # Appliance done?
        if state.appliance_w < APPLIANCE_RESUME_THRESHOLD_W:
            return "DISCHARGE_RAMP", NevCommand(
                action="start_discharge",
                discharge_w=2000,
                reason=f"Appliance done ({state.appliance_w:.0f}W), restarting discharge ramp",
            )
        # Not night anymore?
        if not state.is_night:
            return "IDLE", NevCommand(action="none", reason="Morning during appliance pause")

        return "APPLIANCE_PAUSE", NevCommand(action="none", reason="Appliance running")

    elif current_state == "BATTERY_DEPLETED":
        if not state.is_night:
            return "IDLE", NevCommand(action="none", reason="Morning — reset")
        return "BATTERY_DEPLETED", NevCommand(action="none", reason="Battery depleted")

    return current_state, NevCommand(action="none")
