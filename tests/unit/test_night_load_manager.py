"""Tests for core/night_load_manager.py — NightLoadBudget v2.

Covers all corner cases per Storm P0 directive + regression tests for
the two bugs found by QC:
  - Bug 1: ev_power_w was unused (dead parameter) — now used correctly
  - Bug 2: disk_viktat was double-subtracted in EV budget — fixed
"""

from __future__ import annotations

import pytest

from custom_components.carmabox.const import DISK_ACTIVE_THRESHOLD_W
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
    """disk=1.5kW raw in viktat, bat=0 → bat gets (target - non_bat - margin) / weight."""
    # viktat=0.75 (disk 1.5kW * 0.5), bat_kw=0
    # non_bat_viktat = 0.75, bat_budget_viktat = 2.0 - 0.75 - 0.3 = 0.95, raw = 1.9kW
    b = budget(viktat=0.75, disk_w=1500.0, ev_w=0.0, bat_kw=0.0)
    assert b.bat_charge_kw == pytest.approx(1.9, abs=0.01)
    assert b.defer_bat is False
    assert b.disk_kw == pytest.approx(1.5, abs=0.01)


# ── TC2: disk + ev + bat → bat deferred (no room) ────────────────────────────


def test_disk_ev_bat_bat_deferred() -> None:
    """disk=1.5kW + ev=2kW in viktat → bat is deferred."""
    # viktat = (1.5+2.0)*0.5 = 1.75, bat_kw=0
    # non_bat_viktat = 1.75, bat_budget_viktat = 2.0 - 1.75 - 0.3 = -0.05 → 0
    b = budget(viktat=1.75, disk_w=1500.0, ev_w=2000.0, bat_kw=0.0)
    assert b.bat_charge_kw == 0.0
    assert b.defer_bat is True


# ── TC3: no loads → full bat budget up to max_grid_charge_kw ─────────────────


def test_no_loads_full_budget() -> None:
    """No disk, no EV, viktat=0 → bat gets max_grid_charge_kw (capped).

    Note: defer_ev=True is correct. At target=2.0kW/weight=0.5, max_raw=4.0kW.
    After margin (0.6kW raw), available=3.4kW. EV min 6A=4.14kW > 3.4kW → defer.
    """
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(3.0, abs=0.01)
    assert b.defer_bat is False
    assert b.defer_ev is True  # 6A min > available at 2.0kW target
    assert b.ev_amps == 4  # (2.0-0.3)/0.5/3/(0.23) ≈ 4.9 → int 4


# ── TC4: zero headroom → defer all ───────────────────────────────────────────


def test_zero_headroom_defer_all() -> None:
    """viktat_grid_kw == target_kw → available = -margin → defer both."""
    b = budget(viktat=2.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0)
    assert b.available_kw == pytest.approx(-0.3, abs=0.001)
    assert b.defer_bat is True
    assert b.defer_ev is True


# ── TC5: negative headroom → defer all ───────────────────────────────────────


def test_negative_headroom_defer_all() -> None:
    """target=2.0, viktat=1.8 → available=2.0-1.8-0.3=-0.1 → defer all."""
    b = budget(viktat=1.8, disk_w=300.0, ev_w=0.0, bat_kw=0.0, target=2.0)
    assert b.available_kw == pytest.approx(-0.1, abs=0.001)
    assert b.defer_bat is True
    assert b.defer_ev is True


# ── TC6: night weight respected ──────────────────────────────────────────────


def test_night_weight_respected() -> None:
    """weight=0.5 vs weight=1.0 give different raw budgets."""
    b_night = budget(viktat=0.0, bat_kw=0.0, max_charge=3.0, weight=0.5)
    b_day = budget(viktat=0.0, bat_kw=0.0, max_charge=3.0, weight=1.0)
    # Night: (2.0-0.3)/0.5=3.4 → cap 3.0
    assert b_night.bat_charge_kw == pytest.approx(3.0, abs=0.01)
    # Day: (2.0-0.3)/1.0=1.7
    assert b_day.bat_charge_kw == pytest.approx(1.7, abs=0.01)


# ── TC7: bat already charging accounted correctly ─────────────────────────────


def test_bat_already_charging_accounted() -> None:
    """bat charging at 2kW is in viktat; budget reflects correct non-bat load."""
    # viktat=1.0 (bat 2kW * 0.5), bat_kw=2.0
    # bat_viktat=1.0, non_bat_viktat=0.0, bat_budget_viktat=2.0-0.0-0.3=1.7, raw=3.4 → cap 3.0
    b = budget(viktat=1.0, disk_w=0.0, ev_w=0.0, bat_kw=2.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(3.0, abs=0.01)


# ── TC8: disk starts while bat charges → bat reduces ─────────────────────────


def test_disk_starts_bat_reduces() -> None:
    """bat=3kW charging, disk starts at 1.5kW → bat must yield headroom."""
    # viktat = (3.0+1.5)*0.5 = 2.25, bat_kw=3.0
    # non_bat_viktat = 2.25 - 1.5 = 0.75, bat_budget = 2.0-0.75-0.3=0.95, raw=1.9kW
    b = budget(viktat=2.25, disk_w=1500.0, ev_w=0.0, bat_kw=3.0, max_charge=3.0)
    assert b.bat_charge_kw == pytest.approx(1.9, abs=0.01)
    assert b.defer_bat is False


# ── TC9: ev_amps capped at MAX_EV_CURRENT ────────────────────────────────────


def test_ev_amps_hard_cap() -> None:
    """ev_amps never exceeds MAX_EV_CURRENT (10A) even with huge headroom."""
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b.ev_amps <= 10


# ── TC10: ev_amps below min → defer_ev=True ──────────────────────────────────


def test_ev_defer_when_below_min_amps() -> None:
    """Very little headroom → ev_amps < 6 → defer_ev=True."""
    b = budget(viktat=1.95, disk_w=0.0, ev_w=0.0, bat_kw=0.0)
    assert b.defer_ev is True
    assert b.ev_amps == 0


# ── TC11: disk below threshold → not counted ─────────────────────────────────


def test_disk_below_threshold_ignored() -> None:
    """Disk at 30W (< DISK_ACTIVE_THRESHOLD_W) → bat budget unchanged."""
    b_with = budget(viktat=0.0, disk_w=30.0, bat_kw=0.0, max_charge=3.0)
    b_without = budget(viktat=0.0, disk_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b_with.bat_charge_kw == b_without.bat_charge_kw


# ── TC12: max_grid_charge_kw cap ─────────────────────────────────────────────


def test_max_grid_charge_kw_cap() -> None:
    """bat_charge_kw never exceeds max_grid_charge_kw."""
    b = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=1.5)
    assert b.bat_charge_kw <= 1.5


# ── TC13: NightLoadBudget is frozen ──────────────────────────────────────────


def test_nightloadbudget_is_frozen() -> None:
    """NightLoadBudget must be immutable (frozen dataclass)."""
    b = budget()
    with pytest.raises((AttributeError, TypeError)):
        b.bat_charge_kw = 99.0  # type: ignore[misc]


# ── TC14: bat_charge_kw and ev_amps never negative ───────────────────────────


def test_bat_ev_never_negative_under_overload() -> None:
    """Even with massive overload, bat_charge_kw >= 0 and ev_amps >= 0."""
    b = budget(viktat=10.0, disk_w=5000.0, ev_w=7000.0, bat_kw=5.0)
    assert b.bat_charge_kw >= 0.0
    assert b.ev_amps >= 0


# ── TC15: house load reduces bat budget ──────────────────────────────────────


def test_house_load_reduces_bat_budget() -> None:
    """House 1kW raw = 0.5kW viktat → bat gets 2.4kW (not 3.0kW)."""
    b = budget(viktat=0.5, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    # non_bat=0.5, bat_viktat=2.0-0.5-0.3=1.2, raw=2.4kW
    assert b.bat_charge_kw == pytest.approx(2.4, abs=0.01)


# ── TC16: disk threshold boundary ────────────────────────────────────────────


def test_disk_threshold_boundary() -> None:
    """Disk exactly above DISK_ACTIVE_THRESHOLD_W is counted in disk_kw."""
    b_at = budget(viktat=0.0, disk_w=DISK_ACTIVE_THRESHOLD_W + 1.0)
    b_off = budget(viktat=0.0, disk_w=DISK_ACTIVE_THRESHOLD_W - 1.0)
    assert b_at.disk_kw > 0.0
    # bat budget same (disk not used in bat calculation directly — it's in viktat)
    assert b_at.bat_charge_kw == b_off.bat_charge_kw


# ── TC17: P0 scenario — disk+bat+house NEVER exceeds target ──────────────────


def test_p0_scenario_disk_bat_house_never_exceeds_target() -> None:
    """P0: disk 1.5kW + potential bat 6kW + house 0.5kW = 8kW raw would be 4kW viktat.
    NightLoadBudget must constrain bat so total stays <= target=2.0kW viktat.
    """
    house_disk_raw_kw = 0.5 + 1.5
    viktat_no_bat = house_disk_raw_kw * 0.5  # = 1.0 kW viktat
    b = budget(viktat=viktat_no_bat, disk_w=1500.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    # bat raw = (2.0-1.0-0.3)/0.5 = 1.4kW
    assert b.bat_charge_kw == pytest.approx(1.4, abs=0.01)
    total_viktat = viktat_no_bat + b.bat_charge_kw * 0.5
    assert total_viktat <= 2.0, f"P0 BREACH: {total_viktat:.3f} kW viktat"


# ── TC18: weight=1.0 does not crash ──────────────────────────────────────────


def test_day_weight_does_not_crash() -> None:
    """weight=1.0 must not cause ZeroDivisionError or return negative values."""
    b = calculate_night_budget(
        viktat_grid_kw=0.5,
        disk_w=500.0,
        ev_power_w=0.0,
        bat_charge_kw=0.0,
        target_kw=2.0,
        night_weight=1.0,
        max_grid_charge_kw=3.0,
    )
    assert b.bat_charge_kw >= 0.0
    assert isinstance(b.ev_amps, int)


# ── TC19: field types correct ─────────────────────────────────────────────────


def test_budget_field_types() -> None:
    """All NightLoadBudget fields must have correct Python types."""
    b = budget()
    assert isinstance(b.available_kw, float)
    assert isinstance(b.bat_charge_kw, float)
    assert isinstance(b.ev_amps, int)
    assert isinstance(b.disk_kw, float)
    assert isinstance(b.defer_bat, bool)
    assert isinstance(b.defer_ev, bool)


# ── TC20: REGRESSION — ev_power_w actually used (Bug 1 fix) ──────────────────


def test_ev_power_w_used_in_ev_budget() -> None:
    """REGRESSION: ev_power_w must affect ev_amps (was dead parameter in v1).

    If EV is already charging at some power, it's already in viktat.
    Removing EV's viktat gives higher non-EV load → less room for EV to grow.
    A running EV at 2kW (1kW viktat) leaves less EV budget than no EV.
    """
    # No EV running: viktat = 0
    b_no_ev = budget(viktat=0.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    # EV running at 2kW raw = 1.0kW viktat (included in viktat=1.0)
    b_with_ev = budget(viktat=1.0, disk_w=0.0, ev_w=2000.0, bat_kw=0.0, max_charge=3.0)
    # With EV: non_ev_viktat=0, ev_budget_viktat=2.0-0-0.3=1.7, raw=3.4kW
    # No EV: non_ev_viktat=0, ev_budget_viktat=2.0-0-0.3=1.7 → same (EV removed from both)
    # But with_ev viktat=1.0 so available_kw = 2.0-1.0-0.3=0.7 (different available!)
    assert b_with_ev.available_kw == pytest.approx(0.7, abs=0.001)
    assert b_no_ev.available_kw == pytest.approx(1.7, abs=0.001)
    # Proof ev_power_w IS used: same viktat=1.0 but ev_w=0 gives fewer amps.
    # With ev_w=2000: non_ev_viktat=0 → ev_budget=3.4kW → 4A
    # With ev_w=0:    non_ev_viktat=1.0 → ev_budget=1.4kW → 2A
    b_same_viktat_no_ev = budget(viktat=1.0, disk_w=0.0, ev_w=0.0, bat_kw=0.0, max_charge=3.0)
    assert b_with_ev.ev_amps == 4
    assert b_same_viktat_no_ev.ev_amps == 2
    assert b_with_ev.ev_amps > b_same_viktat_no_ev.ev_amps  # ev_power_w changes result


# ── TC21: REGRESSION — disk NOT double-subtracted in EV budget (Bug 2 fix) ────


def test_disk_not_double_subtracted_from_ev_budget() -> None:
    """REGRESSION: disk was subtracted from ev_budget even though it's already in viktat.

    With disk running (already in viktat), EV budget in v1 was:
        available_kw - disk_viktat (double count!)
    In v2: EV budget is based on non_ev_viktat, disk is NOT re-subtracted.
    So disk running = same EV budget as without disk (both already in viktat).
    """
    # Disk running at 1.5kW: viktat includes it. EV=0.
    viktat_with_disk = 1.5 * 0.5  # = 0.75kW viktat
    b_disk_on = budget(viktat=viktat_with_disk, disk_w=1500.0, ev_w=0.0, bat_kw=0.0)
    # No disk: same viktat composition, ev budget should be same
    b_disk_off = budget(viktat=viktat_with_disk, disk_w=0.0, ev_w=0.0, bat_kw=0.0)

    # EV budget: non_ev_viktat = viktat (since ev=0) → ev_budget_viktat = target - viktat - margin
    # Both should give the same ev_amps since disk is already captured in viktat
    assert b_disk_on.ev_amps == b_disk_off.ev_amps, (
        f"Bug 2 regression: disk double-subtracted. "
        f"disk_on={b_disk_on.ev_amps}A vs disk_off={b_disk_off.ev_amps}A"
    )
