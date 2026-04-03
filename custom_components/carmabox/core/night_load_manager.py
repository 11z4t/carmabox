"""Night Load Manager — centralized power budget for bat + EV + disk at night.

Ensures viktat_grid_kw NEVER exceeds target_kw regardless of combination of
battery charging, EV charging, and dishwasher running simultaneously.

Priority order (Storm P0 directive):
  1. Dishwasher  — NEVER interrupt (manual appliance)
  2. EV charging — reduce amps if needed (min DEFAULT_EV_MIN_AMPS = 6A)
  3. Battery     — lowest priority, takes what remains after disk + EV
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_MIN_GRID_CHARGE_KW,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_TARGET_WEIGHTED_KW,
    DISK_ACTIVE_THRESHOLD_W,
    EV_PHASE_COUNT,
    MAX_EV_CURRENT,
    NIGHT_SAFETY_MARGIN_KW,
)


@dataclass(frozen=True)
class NightLoadBudget:
    """Result of calculate_night_budget(). All power in kW / amps."""

    available_kw: float
    """Remaining headroom after safety margin (may be negative → defer all)."""

    bat_charge_kw: float
    """Maximum raw kW battery MAY charge from grid this cycle."""

    ev_amps: int
    """Maximum amps EV MAY draw this cycle."""

    disk_kw: float
    """Current dishwasher draw in raw kW (informational, never curtailed)."""

    defer_bat: bool
    """True → bat_charge_kw < DEFAULT_MIN_GRID_CHARGE_KW, skip grid charge."""

    defer_ev: bool
    """True → ev_amps < DEFAULT_EV_MIN_AMPS, skip EV charge."""


def calculate_night_budget(
    viktat_grid_kw: float,
    disk_w: float,
    ev_power_w: float,
    bat_charge_kw: float,
    *,
    target_kw: float = DEFAULT_TARGET_WEIGHTED_KW,
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    max_grid_charge_kw: float = DEFAULT_MIN_GRID_CHARGE_KW,
    phase_count: int = EV_PHASE_COUNT,
    max_ev_amps: int = MAX_EV_CURRENT,
) -> NightLoadBudget:
    """Calculate how much power bat and EV may consume without breaching target.

    viktat_grid_kw already includes ALL current loads (house + bat + disk + EV),
    multiplied by the night weight factor.  The function works out what the
    battery alone may draw given everything else that is running.

    Args:
        viktat_grid_kw:     Current weighted grid import (e.g. from
                            sensor.ellevio_viktad_timmedel_pagaende).
        disk_w:             Raw dishwasher power in W (sensor.98_shelly_plug_s_power).
        ev_power_w:         Raw EV charger power in W.
        bat_charge_kw:      Current battery charge power in raw kW
                            (sensor.v6_battery_charge_total_kw).
        target_kw:          Weighted grid import target (default 2.0 kW).
        night_weight:       Night tariff weight factor (default 0.5).
        max_grid_charge_kw: Hard ceiling on bat charge in raw kW (default 0.5,
                            caller should pass actual value e.g. 3.0).
        phase_count:        EV charger phase count (default 3).
        max_ev_amps:        Hard ceiling on EV amps (default 10A).

    Returns:
        NightLoadBudget with safe bat_charge_kw and ev_amps for this cycle.
    """
    # ── Battery budget ────────────────────────────────────────────────────
    # viktat_grid_kw includes bat contribution: bat_charge_kw * night_weight
    # Remove bat contribution to get non-bat weighted load:
    bat_viktat = bat_charge_kw * night_weight
    non_bat_viktat = max(0.0, viktat_grid_kw - bat_viktat)

    # How much weighted kW is available for battery alone:
    bat_budget_viktat = target_kw - non_bat_viktat - NIGHT_SAFETY_MARGIN_KW

    # Convert back to raw kW and cap at hardware ceiling:
    bat_budget_raw_kw = max(0.0, bat_budget_viktat / max(night_weight, 0.01))
    bat_budget_raw_kw = min(bat_budget_raw_kw, max_grid_charge_kw)

    # ── EV budget ─────────────────────────────────────────────────────────
    # EV budget = headroom after safety margin and disk, bat takes remainder.
    headroom_kw = target_kw - viktat_grid_kw
    available_kw = headroom_kw - NIGHT_SAFETY_MARGIN_KW

    disk_kw_raw = disk_w / 1000.0
    disk_viktat = disk_kw_raw * night_weight if disk_w > DISK_ACTIVE_THRESHOLD_W else 0.0

    ev_budget_viktat = max(0.0, available_kw - disk_viktat)
    ev_budget_raw_kw = ev_budget_viktat / max(night_weight, 0.01)
    w_per_amp = 230.0 * phase_count
    ev_amps = int(min(ev_budget_raw_kw * 1000.0 / w_per_amp, max_ev_amps))
    ev_amps = max(0, ev_amps)

    return NightLoadBudget(
        available_kw=round(available_kw, 3),
        bat_charge_kw=round(bat_budget_raw_kw, 3),
        ev_amps=ev_amps,
        disk_kw=round(disk_kw_raw, 3),
        defer_bat=bat_budget_raw_kw < DEFAULT_MIN_GRID_CHARGE_KW,
        defer_ev=ev_amps < DEFAULT_EV_MIN_AMPS,
    )
