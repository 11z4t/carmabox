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
    """Maximum total amps EV MAY draw this cycle (includes current draw)."""

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

    viktat_grid_kw includes ALL current loads (house + bat + disk + EV) already
    weighted by night_weight.  Each budget is computed by removing that device's
    current contribution from viktat, then allocating the remaining headroom.

    Args:
        viktat_grid_kw:     Current weighted grid import (ellevio_viktad_timmedel).
        disk_w:             Raw dishwasher power in W (sensor.98_shelly_plug_s_power).
        ev_power_w:         Raw EV charger power in W (sensor.easee_home_*_power).
        bat_charge_kw:      Current battery charge power in raw kW
                            (sensor.v6_battery_charge_total_kw).
        target_kw:          Weighted grid import target (default 2.0 kW).
        night_weight:       Night tariff weight factor (default 0.5).
        max_grid_charge_kw: Hard ceiling on bat charge in raw kW.
        phase_count:        EV charger phase count (default 3).
        max_ev_amps:        Hard ceiling on EV amps (default 10A).

    Returns:
        NightLoadBudget with safe bat_charge_kw and ev_amps for this cycle.
    """
    safe_weight = max(night_weight, 0.01)

    # ── Shared: headroom after safety margin ──────────────────────────────
    # viktat_grid_kw already includes all running loads.
    # available_kw = how much MORE weighted kW we could add right now.
    headroom_kw = target_kw - viktat_grid_kw
    available_kw = headroom_kw - NIGHT_SAFETY_MARGIN_KW

    # ── Battery budget ────────────────────────────────────────────────────
    # Remove bat's current contribution from viktat to find non-bat load.
    # Bat gets: target - non_bat_viktat - margin (symmetric with EV below).
    bat_viktat = bat_charge_kw * night_weight
    non_bat_viktat = max(0.0, viktat_grid_kw - bat_viktat)
    bat_budget_viktat = target_kw - non_bat_viktat - NIGHT_SAFETY_MARGIN_KW
    bat_budget_raw_kw = max(0.0, bat_budget_viktat / safe_weight)
    bat_budget_raw_kw = min(bat_budget_raw_kw, max_grid_charge_kw)

    # ── EV budget ─────────────────────────────────────────────────────────
    # Remove EV's current contribution from viktat to find non-EV load.
    # EV TOTAL budget = how much EV can draw in total (not just additional).
    # Disk is NOT re-subtracted here — it is already captured in viktat_grid_kw,
    # so available_kw already reflects disk being active (fixes double-count bug).
    ev_raw_kw = ev_power_w / 1000.0
    ev_viktat = ev_raw_kw * night_weight
    non_ev_viktat = max(0.0, viktat_grid_kw - ev_viktat)
    ev_budget_viktat = target_kw - non_ev_viktat - NIGHT_SAFETY_MARGIN_KW
    ev_budget_raw_kw = max(0.0, ev_budget_viktat / safe_weight)
    w_per_amp = 230.0 * phase_count
    ev_amps = int(min(ev_budget_raw_kw * 1000.0 / w_per_amp, max_ev_amps))
    ev_amps = max(0, ev_amps)

    disk_kw_raw = disk_w / 1000.0

    return NightLoadBudget(
        available_kw=round(available_kw, 3),
        bat_charge_kw=round(bat_budget_raw_kw, 3),
        ev_amps=ev_amps,
        disk_kw=round(disk_kw_raw, 3),
        defer_bat=bat_budget_raw_kw < DEFAULT_MIN_GRID_CHARGE_KW,
        defer_ev=ev_amps < DEFAULT_EV_MIN_AMPS,
    )
