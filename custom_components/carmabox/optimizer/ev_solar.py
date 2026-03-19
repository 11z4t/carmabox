"""CARMA Box — EV Solar Charging (Daytime PV Surplus).

Pure Python. No HA imports. Fully testable.

Charges EV from PV surplus during daytime.
Free energy — no Ellevio impact (export becomes EV charge).
Priority: battery > EV > export.
"""

from __future__ import annotations


def should_start_solar_ev(
    pv_surplus_kw: float,
    battery_soc: float,
    ev_connected: bool,
    ev_soc: float,
    min_surplus_kw: float = 1.4,
    battery_full_threshold: float = 95.0,
) -> bool:
    """Check if we should start EV charging from PV surplus.

    Start conditions (ALL must be true):
    1. PV surplus > min_surplus_kw (at least 6A worth)
    2. Battery is nearly full (>95% — battery gets priority)
    3. EV is connected
    4. EV is not full

    Args:
        pv_surplus_kw: Current PV export/surplus (kW).
        battery_soc: Average battery SoC (%).
        ev_connected: True if EV cable connected.
        ev_soc: Current EV SoC (%). -1 = unknown.
        min_surplus_kw: Minimum surplus to start (default 1.4kW = 6A).
        battery_full_threshold: Battery SoC above which EV gets surplus.

    Returns:
        True if solar EV charging should start.
    """
    if not ev_connected:
        return False
    if ev_soc >= 100 or ev_soc < 0:
        return False
    if battery_soc < battery_full_threshold:
        return False
    return pv_surplus_kw >= min_surplus_kw


def should_stop_solar_ev(
    pv_surplus_kw: float,
    stop_threshold_kw: float = 0.5,
    consecutive_low_count: int = 0,
    required_consecutive: int = 10,
) -> bool:
    """Check if solar EV charging should stop.

    Stop when surplus drops below threshold for sustained period (5 min).
    Prevents oscillation from cloud shadows.

    Args:
        pv_surplus_kw: Current PV export/surplus.
        stop_threshold_kw: Below this = consider stopping.
        consecutive_low_count: How many cycles surplus has been low.
        required_consecutive: Cycles (×30s) required before stopping.

    Returns:
        True if should stop.
    """
    return pv_surplus_kw < stop_threshold_kw and consecutive_low_count >= required_consecutive


def calculate_solar_ev_amps(
    pv_surplus_kw: float,
    min_amps: int = 6,
    max_amps: int = 16,
    voltage: float = 230.0,
) -> int:
    """Calculate EV amps from PV surplus.

    Quantizes surplus to nearest valid amperage.

    Args:
        pv_surplus_kw: Available PV surplus (kW).
        min_amps: Minimum charge amps.
        max_amps: Maximum charge amps.
        voltage: Grid voltage.

    Returns:
        EV amps (0 if below minimum, 6-16 otherwise).
    """
    amps = int(pv_surplus_kw * 1000 / voltage)
    if amps < min_amps:
        return 0
    return min(amps, max_amps)
