"""CARMA Box — Dynamic EV Amperage Adjustment.

Pure Python. No HA imports. Fully testable.

Adjusts EV charging amps every cycle (30s) based on:
- Actual house consumption (not predicted)
- Grid target constraint
- Battery discharge headroom
- Appliance spike detection (dishwasher, dryer)

Keeps grid import at or below target at all times.
"""

from __future__ import annotations

from ..const import DEFAULT_EV_MIN_AMPS, DEFAULT_SPIKE_THRESHOLD_KW, DEFAULT_VOLTAGE


def calculate_dynamic_amps(
    house_load_kw: float,
    current_ev_amps: int,
    target_weighted_kw: float,
    night_weight: float,
    battery_discharge_kw: float = 0.0,
    min_amps: int = 0,
    max_amps: int = 10,
    voltage: float = DEFAULT_VOLTAGE,
) -> int:
    """Calculate optimal EV amps for current conditions.

    Args:
        house_load_kw: Current house consumption (excluding EV).
        current_ev_amps: Current EV charge amps.
        target_weighted_kw: Grid import target (weighted kW).
        night_weight: Ellevio weight for current hour (0.5 night, 1.0 day).
        battery_discharge_kw: Battery support available (kW).
        min_amps: Minimum charge amps (0 = can pause).
        max_amps: Maximum charge amps.
        voltage: Grid voltage.

    Returns:
        Optimal EV amps (0 = pause, 6-16 = active charging).
    """
    if night_weight <= 0:
        return 0

    # Max grid import allowed (actual kW, not weighted)
    max_grid_kw = target_weighted_kw / night_weight

    # Available headroom for EV = max_grid - house_load + battery_support
    headroom_kw = max_grid_kw - house_load_kw + battery_discharge_kw
    headroom_kw = max(0, headroom_kw)

    # Convert to amps
    optimal_amps = int(headroom_kw * 1000 / voltage)
    optimal_amps = max(min_amps, min(optimal_amps, max_amps))

    # Easee minimum is 6A — below that, pause (0A)
    if 0 < optimal_amps < DEFAULT_EV_MIN_AMPS:
        optimal_amps = 0

    return optimal_amps


def detect_appliance_spike(
    current_load_kw: float,
    previous_load_kw: float,
    spike_threshold_kw: float = DEFAULT_SPIKE_THRESHOLD_KW,
) -> bool:
    """Detect sudden load spike (dishwasher, dryer, etc.).

    A spike is a sudden increase > threshold within one measurement cycle.
    """
    return (current_load_kw - previous_load_kw) > spike_threshold_kw


def calculate_spike_response(
    current_ev_amps: int,
    spike_kw: float,
    voltage: float = DEFAULT_VOLTAGE,
    min_amps: int = 0,
) -> int:
    """Reduce EV amps to compensate for appliance spike.

    Args:
        current_ev_amps: Current charging amps.
        spike_kw: Size of the detected spike (kW).
        voltage: Grid voltage.
        min_amps: Minimum amps (0 = can pause).

    Returns:
        Reduced EV amps.
    """
    reduce_amps = int(spike_kw * 1000 / voltage) + 1  # +1 for margin
    new_amps = max(min_amps, current_ev_amps - reduce_amps)

    # Below min amps → pause
    if 0 < new_amps < DEFAULT_EV_MIN_AMPS:
        new_amps = 0

    return new_amps
