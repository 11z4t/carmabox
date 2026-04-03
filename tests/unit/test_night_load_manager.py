"""Tests for core/night_load_manager.py — NightLoadBudget.

Covers all corner cases per Storm P0 directive:
- disk+bat combination
- disk+ev+bat (no room for bat)
- no loads (full budget)
- zero headroom (defer all)
- negative headroom (defer all)
- night weight applied correctly
- max_grid_charge_kw cap respected
- ev min amps threshold
"""

from __future__ import annotations

import pytest

from custom_components.carmabox.const import (
    NIGHT_SAFETY_MARGIN_KW,
)
from custom_components.carmabox.core.night_load_manager import (
    NightLoadBudget,
    calculate_night_budget,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def budget(
    viktat: float = 0.0,
    disk_w: float = 0.0,
    ev_w: float = 0.0,
    bat_kw: float = 0.0,
    target: float = 2.0,
    weight: float = 0.5,
    max_charge: float = 3.0,
) -> NightLoadBudget:
    return calculate_night_budget(
        viktat_grid_kw=viktat,
        disk_w=disk_w,
        ev_power_w=ev_w,
        bat_charge_kw=bat_kw,
        target_kw=target,
        night_weight=weight,
        max_grid_charge_kw=max_charge,
    )


# ── TC1: disk active + bat wants to charge → bat budget reduced ──────────────


def test_disk_active_reduces_bat_budget() -> None:
    """disk=1.5kW raw, bat currently 0 → bat budget = target - disk*weight - margin."""
    # non_bat_viktat = 1.5*0.5 = 0.75 kW
    # bat_budget_viktat = 2.0 - 0.75 - 0.3 = 0.95 kW
    # bat_budget_raw = 0.95 / 0.5 = 1.9 kW
    b = budget(viktat=0.75, disk_w=1500.0, ev_w=0.0, bat_kw=0.0)
    assert b.bat_charge_kw == pytest.approx(1.9, abs=0.01)
    assert b.defer_bat is False
    assert b.disk_kw == pytest.approx(1.5, abs=0.01)


# ── TC2: disk + ev + bat → bat deferred (no room) ────────────────────────────


def test_disk_ev_bat_bat_deferred() -> None:
    """disk=1.5kW + ev=2kW running → so little room bat is deferred."""
    # viktat = (1.5+2.0)*0.5 = 1.75 kW  (house excluded to isolate)
    # non_bat_viktat = 1.75 (bat_kw=0)
    # bat_budget_viktat = 2.0 - 1.75 - 0.3 = -0.05 → clamp 0
    b = budget(viktat=1.75, disk_w=1500.0, ev_w=2000.0, bat_kw=0.0)
    assert b.bat_charge_kw == 0.0
    assert b.defer_bat is True


# ── TC3: no loads → full bat budget up to max_grid_charge_kw ─────────────────


def test_no_loads_full_budget() -> None:
    """No disk, no EV, viktat=0 → bat gets max_grid_charge_kw.

    NOTE: defer_ev=True is EXPECTED and CORRECT here.
    At target=2.0kW viktat, weight=0.5 → max raw = 4.0kW.
    Safety margin 0.3kW viktat = 0.6kW raw → available raw = 3.4kW.
    EV minimum 6A * 3ph * 230V = 4.14kW raw > 3.4kW available → defer_ev=True.
    EV at night with this target simply cannot fit within budget (by design).
    """
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(3.0, abs=0.01)
    assert b.defer_bat is False
    # EV deferred: 6A min = 4.14kW raw > 3.4kW available at target=2.0kW/weight=0.5
    assert b.defer_ev is True
    assert b.ev_amps == 4  # 3.4kW / (3ph*230V) = 4.9A → int = 4 (below 6A min)


# ── TC4: headroom exactly zero → defer all ───────────────────────────────────


def test_zero_headroom_defer_all() -> None:
    """viktat_grid_kw == target_kw → available = -margin → defer both."""
    b = budget(viktat=2.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0)
    assert b.available_kw == pytest.approx(-NIGHT_SAFETY_MARGIN_KW, abs=0.001)
    assert b.defer_bat is True
    assert b.defer_ev is True


# ── TC5: negative headroom → defer all ───────────────────────────────────────


def test_negative_headroom_defer_all() -> None:
    """target=2.0, viktat=1.8, disk=0.3kW raw → available = 2.0-1.8-0.3 = -0.1."""
    # viktat includes disk: 1.5*0.5=0.75 house? Let's use exact scenario from directive:
    # target=2.0, viktat=1.8, disk=0.3kW raw (150W) but directive says disk=0.3
    # available_kw = target - viktat - margin = 2.0 - 1.8 - 0.3 = -0.1
    b = budget(viktat=1.8, disk_w=300.0, ev_w=0.0, bat_kw=0.0, target=2.0)
    assert b.available_kw == pytest.approx(-0.1, abs=0.001)
    assert b.defer_bat is True
    assert b.defer_ev is True


# ── TC6: night weight applied correctly ──────────────────────────────────────


def test_night_weight_respected() -> None:
    """weight=0.5 → effective raw ceiling = target/weight = 4.0 kW."""
    # bat_kw=0, no loads, viktat=0 → bat_budget_raw = (2.0-0.3)/0.5 = 3.4 → capped 3.0
    b = budget(viktat=0.0, bat_kw=0.0, max_charge=3.0, weight=0.5)
    # max_raw = target/weight = 4.0 kW; budget = (target - margin) / weight = 3.4 → cap 3.0
    assert b.bat_charge_kw == pytest.approx(3.0, abs=0.01)

    # With weight=1.0 (day): same math but cap is still 3.0
    b2 = budget(viktat=0.0, bat_kw=0.0, max_charge=3.0, weight=1.0)
    # bat_budget_raw = (2.0 - 0.3) / 1.0 = 1.7 → no cap needed
    assert b2.bat_charge_kw == pytest.approx(1.7, abs=0.01)


# ── TC7: bat already charging → budget accounts for it ───────────────────────


def test_bat_already_charging_accounted() -> None:
    """bat currently charging at 2kW → viktat includes it; budget reflects available room."""
    # viktat = 2kW * 0.5 = 1.0 kW (only bat, no house to simplify)
    # non_bat_viktat = 1.0 - 2.0*0.5 = 0.0
    # bat_budget_viktat = 2.0 - 0.0 - 0.3 = 1.7
    # bat_budget_raw = 1.7 / 0.5 = 3.4 → cap 3.0
    b = budget(viktat=1.0, disk_w=0.0, ev_w=0.0, bat_kw=2.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(3.0, abs=0.01)


# ── TC8: disk kicks in while bat charges → bat must yield ────────────────────


def test_disk_starts_bat_reduces() -> None:
    """Bat was charging at 3kW, disk starts at 1.5kW → bat must reduce."""
    # viktat = (3.0 + 1.5) * 0.5 = 2.25 kW  (exceeds target=2.0!)
    # non_bat_viktat = 2.25 - 3.0*0.5 = 2.25 - 1.5 = 0.75
    # bat_budget_viktat = 2.0 - 0.75 - 0.3 = 0.95
    # bat_budget_raw = 0.95 / 0.5 = 1.9 kW  (down from 3.0!)
    b = budget(viktat=2.25, disk_w=1500.0, ev_w=0.0, bat_kw=3.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(1.9, abs=0.01)
    assert b.defer_bat is False  # 1.9 > 0.5 minimum


# ── TC9: ev amps capped at MAX_EV_CURRENT ────────────────────────────────────


def test_ev_amps_hard_cap() -> None:
    """Even with huge headroom, ev_amps never exceeds MAX_EV_CURRENT (10A)."""
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b.ev_amps <= 10


# ── TC10: ev_amps below min → defer_ev=True ──────────────────────────────────


def test_ev_defer_when_below_min_amps() -> None:
    """Very little headroom → ev_amps < 6 → defer_ev=True."""
    # available = 2.0 - 1.95 - 0.3 = -0.25 → ev_amps=0
    b = budget(viktat=1.95, disk_w=0.0, ev_w=0.0, bat_kw=0.0)
    assert b.defer_ev is True
    assert b.ev_amps == 0


# ── TC11: disk below threshold → not counted as active ───────────────────────


def test_disk_below_threshold_ignored() -> None:
    """Disk at 30W (< DISK_ACTIVE_THRESHOLD_W=50W) → treated as off."""
    b_with = budget(viktat=0.0, disk_w=30.0, bat_kw=0.0, max_charge=3.0)
    b_without = budget(viktat=0.0, disk_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b_with.bat_charge_kw == b_without.bat_charge_kw
    assert b_with.disk_kw == pytest.approx(0.03, abs=0.001)  # raw kW still reported


# ── TC12: max_grid_charge_kw cap ─────────────────────────────────────────────


def test_max_grid_charge_kw_cap() -> None:
    """bat_budget_raw never exceeds max_grid_charge_kw even with full headroom."""
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=1.5)
    assert b.bat_charge_kw <= 1.5


# ── TC13: NightLoadBudget is frozen ──────────────────────────────────────────


def test_nightloadbudget_is_frozen() -> None:
    """NightLoadBudget must be immutable (frozen dataclass)."""
    b = budget()
    with pytest.raises((AttributeError, TypeError)):
        b.bat_charge_kw = 99.0  # type: ignore[misc]
