"""EXP-03: Reactive peak-shaving control — pure logic module.

GoodWe register 47542 (peak_shaving_power_limit) tells the inverter the
max grid import it should allow — the firmware discharges the battery to
cover everything above that value. Recomputing the limit every cycle from
the CURRENTLY measured grid power (instead of writing a static target
once) means the discharge automatically tracks house-load swings:

    peak_shaving_power_limit = actual_grid_w + target_headroom_w

No HA imports — testable in isolation. Coordinator calls this every
update cycle (coordinator.py `_apply_reactive_peak_shaving`).
"""

from __future__ import annotations

from ..const import PEAK_SHAVING_MAX_W, PEAK_SHAVING_MIN_W

# EMS modes in which the inverter is actively discharging the battery.
# EXP-03 AC3 only calls for adjusting an ALREADY active discharge — never
# for using peak-shaving writes to trigger charging/standby behavior.
REACTIVE_PEAK_SHAVING_EMS_MODES = frozenset({"discharge_pv", "discharge_battery"})


def compute_reactive_peak_shaving_w(
    actual_grid_w: float,
    target_headroom_w: float,
    *,
    max_w: float = PEAK_SHAVING_MAX_W,
    min_w: float = PEAK_SHAVING_MIN_W,
) -> int:
    """Compute the reactive peak_shaving_power_limit for this cycle.

    Formula (EXP-03 AC2): actual_grid + target_headroom, clamped to
    [min_w, max_w] for hardware safety.

    Args:
        actual_grid_w: Currently measured grid power (W). Positive=import,
            negative=export (PLAT-1134 convention).
        target_headroom_w: Configurable margin added above the live grid
            reading (coordinator's `peak_shaving_headroom_w`, default
            DEFAULT_PEAK_SHAVING_TARGET_HEADROOM_W).
        max_w: Safety ceiling. Defaults to the shared inverter-rated-power
            clamp also enforced in GoodWeAdapter.set_peak_shaving_limit.
        min_w: Safety floor.

    Returns:
        Clamped integer watts to write to register 47542.
    """
    raw_w = actual_grid_w + target_headroom_w
    return int(max(min_w, min(raw_w, max_w)))


def should_apply_reactive_peak_shaving(ems_mode: str) -> bool:
    """True when EMS mode indicates an active discharge worth reactively tuning.

    EXP-03 AC3: only adjusts a discharge that is already underway — never
    triggers discharge on its own from e.g. charge_pv/battery_standby.
    """
    return ems_mode in REACTIVE_PEAK_SHAVING_EMS_MODES
