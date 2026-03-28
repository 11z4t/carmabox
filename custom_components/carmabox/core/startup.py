"""Startup Safety — ensures safe state after HA restart.

Pure Python. No HA imports. Fully testable.

Sequence:
1. fast_charging OFF on ALL inverters (BEFORE anything else)
2. battery_standby until sensors ready
3. Restore persistent state (night_ev_active, plan, ev_enabled)
4. If night_ev_active: override_schedule + start EV 6A
5. Wait until ALL sensors respond before normal operation

Key principle: NEVER act on unavailable data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StartupState:
    """Persistent state that survives restart."""

    night_ev_active: bool = False
    ev_enabled: bool = False
    ev_current_amps: int = 6


@dataclass
class StartupCommand:
    """What startup safety wants to do."""

    action: str  # "wait" | "safe_mode" | "restore_ev" | "ready"
    fast_charging_off: bool = True  # ALWAYS
    set_standby: bool = True
    start_ev: bool = False
    ev_amps: int = 6  # STARTUP_EV_SAFE_AMPS
    override_schedule: bool = False
    reason: str = ""


def evaluate_startup(
    sensors_ready: bool,
    fast_charging_confirmed_off: bool,
    restored_state: StartupState | None,
    is_night: bool,
    ev_connected: bool,
    ev_soc: float,
    ev_target_soc: float,
) -> StartupCommand:
    """Evaluate what to do at startup. Called every cycle until ready.

    Args:
        sensors_ready: True if all critical sensors have valid data.
        fast_charging_confirmed_off: True if fast_charging OFF on all inverters.
        restored_state: State from persistent storage (None if not loaded).
        is_night: True if 22-06.
        ev_connected: True if EV cable connected.
        ev_soc: EV SoC (%). -1 if unavailable.
        ev_target_soc: EV target SoC (%).

    Returns:
        StartupCommand with actions to take.
    """
    # Step 1: Always turn off fast_charging
    if not fast_charging_confirmed_off:
        return StartupCommand(
            action="safe_mode",
            fast_charging_off=True,
            set_standby=True,
            reason="Väntar på fast_charging OFF bekräftelse",
        )

    # Step 2: Wait for sensors
    if not sensors_ready:
        return StartupCommand(
            action="wait",
            fast_charging_off=True,
            set_standby=True,
            reason="Väntar på sensorer",
        )

    # Step 3: Restore night EV if applicable
    if (restored_state
            and restored_state.night_ev_active
            and restored_state.ev_enabled
            and is_night
            and ev_connected
            and 0 <= ev_soc < ev_target_soc):
        return StartupCommand(
            action="restore_ev",
            fast_charging_off=True,
            set_standby=False,
            start_ev=True,
            ev_amps=6,  # ALWAYS start at 6A
            override_schedule=True,
            reason=f"Återställer natt-EV (SoC {ev_soc:.0f}% < target {ev_target_soc:.0f}%)",
        )

    # Step 4: Ready for normal operation
    return StartupCommand(
        action="ready",
        fast_charging_off=True,
        set_standby=False,
        reason="Startup klar — normal drift",
    )
