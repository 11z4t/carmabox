"""Tests for CARMA Box Unified Decision Engine."""

from __future__ import annotations

import pytest

from custom_components.carmabox.core.decision_engine import (
    BatteryAction,
    Decision,
    EVAction,
    _avg_top25,
    decide,
)

# ---------------------------------------------------------------------------
# Helper: common defaults so tests stay DRY
# ---------------------------------------------------------------------------

_BASE = {
    "battery_soc_pct": 50.0,
    "battery_cap_kwh": 10.0,
    "grid_import_w": 2000.0,
    "pv_power_w": 0.0,
    "ev_soc_pct": 30.0,
    "ev_connected": False,
}


def _decide(**overrides) -> Decision:
    """Call decide() with sane defaults, overridden by kwargs."""
    params = {**_BASE, **overrides}
    return decide(**params)


# ---------------------------------------------------------------------------
# 1. test_discharge_at_expensive_price
# ---------------------------------------------------------------------------
def test_discharge_at_expensive_price():
    """When price is expensive and SoC is adequate, discharge."""
    # avg_top25 of [100,90,80,70] = 100, threshold = 70
    d = _decide(
        current_price_ore=80.0,
        upcoming_prices_ore=[100, 90, 80, 70],
        house_load_w=2500.0,
    )
    assert d.battery == BatteryAction.DISCHARGE
    assert d.battery_limit_w > 0
    assert d.fast_charging is False


# ---------------------------------------------------------------------------
# 2. test_standby_at_cheap_price (but not below cheap threshold)
# ---------------------------------------------------------------------------
def test_standby_at_moderate_price():
    """When price is moderate (not expensive, not cheap), standby."""
    # avg_top25 of [100,90,80,70] = 100, threshold = 70.  Price 40 < 70 → not expensive.
    # cheap_price_ore default = 30, price 40 > 30 → not cheap.
    d = _decide(
        current_price_ore=40.0,
        upcoming_prices_ore=[100, 90, 80, 70],
        house_load_w=2500.0,
        pv_power_w=0.0,
    )
    assert d.battery == BatteryAction.STANDBY


# ---------------------------------------------------------------------------
# 3. test_charge_grid_at_very_cheap
# ---------------------------------------------------------------------------
def test_charge_grid_at_very_cheap():
    """When price is very cheap and SoC below max, grid charge."""
    d = _decide(
        current_price_ore=10.0,
        cheap_price_ore=30.0,
        battery_soc_pct=40.0,
        grid_charge_max_soc=80.0,
    )
    assert d.battery == BatteryAction.CHARGE_GRID
    assert d.fast_charging is True


# ---------------------------------------------------------------------------
# 4. test_charge_pv_when_solar
# ---------------------------------------------------------------------------
def test_charge_pv_when_solar():
    """When PV exceeds house load and price is moderate, charge from PV."""
    d = _decide(
        current_price_ore=40.0,
        upcoming_prices_ore=[60, 50, 40, 30],  # threshold = 42 → 40 < 42 not expensive
        pv_power_w=4000.0,
        house_load_w=2500.0,
    )
    assert d.battery == BatteryAction.CHARGE_PV
    assert d.fast_charging is False


# ---------------------------------------------------------------------------
# 5. test_never_fast_charging_during_discharge
# ---------------------------------------------------------------------------
def test_never_fast_charging_during_discharge():
    """fast_charging MUST be False whenever battery is discharging."""
    d = _decide(
        current_price_ore=90.0,
        upcoming_prices_ore=[100, 90, 80, 70],
        battery_soc_pct=60.0,
    )
    assert d.battery == BatteryAction.DISCHARGE
    assert d.fast_charging is False


# ---------------------------------------------------------------------------
# 6. test_ev_start_night_workday
# ---------------------------------------------------------------------------
def test_ev_start_night_workday():
    """EV should start charging at night before a workday."""
    d = _decide(
        ev_connected=True,
        ev_soc_pct=30.0,
        ev_target_pct=75.0,
        is_night=True,
        is_workday_tomorrow=True,
        current_price_ore=40.0,
        upcoming_prices_ore=[50, 40, 30, 20],  # not expensive
        house_load_w=1500.0,  # low load → headroom for EV
        pv_power_w=0.0,
    )
    assert d.ev == EVAction.START
    assert d.ev_amps == 6


# ---------------------------------------------------------------------------
# 7. test_ev_skip_night_weekend_expensive
# ---------------------------------------------------------------------------
def test_ev_skip_night_weekend_expensive():
    """EV should NOT start at night on weekend when price >= 30."""
    d = _decide(
        ev_connected=True,
        ev_soc_pct=30.0,
        is_night=True,
        is_workday_tomorrow=False,
        current_price_ore=40.0,
        upcoming_prices_ore=[50, 40, 30, 20],
    )
    assert d.ev == EVAction.NONE


# ---------------------------------------------------------------------------
# 8. test_ev_respects_tak
# ---------------------------------------------------------------------------
def test_ev_respects_tak():
    """EV must not start if even 1-phase would break Ellevio tak."""
    # Use moderate price so battery goes standby (no discharge to offset)
    # avg_top25([30,25,20,15]) = 30, threshold = 21. Price 15 < 21 → not expensive.
    # Price 15 <= cheap 30 → CHARGE_GRID. But we want standby, so set soc above max.
    d = _decide(
        ev_connected=True,
        ev_soc_pct=30.0,
        is_night=True,
        is_workday_tomorrow=True,
        house_load_w=7000.0,  # 7kW house load
        tak_kw=2.0,
        night_weight=0.5,
        pv_power_w=0.0,
        current_price_ore=35.0,
        cheap_price_ore=30.0,
        upcoming_prices_ore=[40, 35, 30, 25],  # threshold = 28, price 35 >= 28 → discharge
        battery_soc_pct=14.0,  # Below min_soc → STANDBY, no discharge
        min_soc=15.0,
    )
    # Standby → discharge_w = 0
    # 7kW + 1.38kW = 8.38kW → 8.38 * 0.5 = 4.19 > 2.0 tak → skip
    assert d.battery == BatteryAction.STANDBY
    assert d.ev == EVAction.NONE
    assert "tak" in d.reason.lower() or "skip" in d.reason.lower()


# ---------------------------------------------------------------------------
# 9. test_ev_1phase_when_low_headroom
# ---------------------------------------------------------------------------
def test_ev_1phase_when_low_headroom():
    """EV falls back to 1-phase when 3-phase would break tak."""
    # Night weight 0.5 → max_grid = 4kW actual
    # Ensure STANDBY so discharge_w = 0: low SoC forces standby
    # house 2500W + EV 3p 4140W = 6640W → 6.64 * 0.5 = 3.32 > 2.0 → 3p fails
    # house 2500W + EV 1p 1380W = 3880W → 3.88 * 0.5 = 1.94 < 2.0 → 1p OK
    d = _decide(
        ev_connected=True,
        ev_soc_pct=30.0,
        is_night=True,
        is_workday_tomorrow=True,
        house_load_w=2500.0,
        tak_kw=2.0,
        night_weight=0.5,
        pv_power_w=0.0,
        current_price_ore=40.0,
        upcoming_prices_ore=[50, 40, 30, 20],
        battery_soc_pct=14.0,  # Below min_soc → STANDBY, discharge_w=0
        min_soc=15.0,
    )
    assert d.battery == BatteryAction.STANDBY
    assert d.ev == EVAction.START
    assert d.ev_phase == "1_phase"


# ---------------------------------------------------------------------------
# 10. test_low_battery_standby
# ---------------------------------------------------------------------------
def test_low_battery_standby():
    """Battery at or below min SoC must go to standby regardless of price."""
    d = _decide(
        battery_soc_pct=15.0,
        min_soc=15.0,
        current_price_ore=200.0,
        upcoming_prices_ore=[200, 180, 160, 140],
    )
    assert d.battery == BatteryAction.STANDBY
    assert d.fast_charging is False


# ---------------------------------------------------------------------------
# 11. test_discharge_rate_matches_house_load
# ---------------------------------------------------------------------------
def test_discharge_rate_matches_house_load():
    """Discharge rate should approximate house_load - pv + margin."""
    d = _decide(
        current_price_ore=90.0,
        upcoming_prices_ore=[100, 90, 80, 70],
        battery_soc_pct=60.0,
        house_load_w=3000.0,
        pv_power_w=500.0,
    )
    assert d.battery == BatteryAction.DISCHARGE
    # Expected: min(3000 - 500 + 200, 5000) = 2700
    assert d.battery_limit_w == 2700


# ---------------------------------------------------------------------------
# 12. test_no_grid_charge_when_expensive
# ---------------------------------------------------------------------------
def test_no_grid_charge_when_expensive():
    """Never grid charge at expensive price even if SoC is low."""
    d = _decide(
        battery_soc_pct=30.0,
        current_price_ore=100.0,
        cheap_price_ore=30.0,
        upcoming_prices_ore=[100, 90, 80, 70],
    )
    assert d.battery != BatteryAction.CHARGE_GRID


# ---------------------------------------------------------------------------
# 13. test_no_discharge_when_pv_surplus
# ---------------------------------------------------------------------------
def test_no_discharge_when_pv_surplus():
    """When PV covers house load, prefer CHARGE_PV over discharge."""
    # Price moderate → not expensive. PV > load → CHARGE_PV
    d = _decide(
        current_price_ore=40.0,
        upcoming_prices_ore=[60, 50, 40, 30],  # threshold = 42, price 40 < 42
        pv_power_w=5000.0,
        house_load_w=2000.0,
        battery_soc_pct=50.0,
    )
    assert d.battery == BatteryAction.CHARGE_PV
    assert d.battery != BatteryAction.DISCHARGE


# ---------------------------------------------------------------------------
# 14. test_projected_weighted_calculated
# ---------------------------------------------------------------------------
def test_projected_weighted_calculated():
    """projected_weighted_kw reflects net grid import * weight."""
    # Force standby: low SoC so no discharge, moderate price so no charge
    d = _decide(
        house_load_w=2000.0,
        pv_power_w=0.0,
        is_night=True,
        night_weight=0.5,
        current_price_ore=40.0,
        cheap_price_ore=30.0,
        upcoming_prices_ore=[50, 40, 30, 20],
        battery_soc_pct=14.0,  # Below min_soc → STANDBY
        min_soc=15.0,
    )
    assert d.battery == BatteryAction.STANDBY
    # Standby: net_grid = 2000 - 0 - 0 = 2000W → 2.0 kW * 0.5 = 1.0
    assert d.projected_weighted_kw == pytest.approx(1.0, abs=0.1)


# ---------------------------------------------------------------------------
# 15. test_reason_string_informative
# ---------------------------------------------------------------------------
def test_reason_string_informative():
    """Reason string should contain useful information about the decision."""
    d = _decide(
        current_price_ore=10.0,
        cheap_price_ore=30.0,
        battery_soc_pct=40.0,
    )
    assert d.reason  # Non-empty
    assert "grid charge" in d.reason.lower() or "cheap" in d.reason.lower()
    assert "10" in d.reason  # Price appears in reason


# ---------------------------------------------------------------------------
# Extra: helper function
# ---------------------------------------------------------------------------
def test_avg_top25_empty():
    """_avg_top25 returns 50.0 for empty list."""
    assert _avg_top25([]) == 50.0


def test_avg_top25_four_values():
    """_avg_top25 of [100, 80, 60, 40] = top 25% = avg of [100] = 100."""
    assert _avg_top25([100, 80, 60, 40]) == 100.0


# ---------------------------------------------------------------------------
# Extra: EV solar surplus daytime
# ---------------------------------------------------------------------------
def test_ev_solar_surplus_daytime():
    """EV starts from PV surplus during the day."""
    d = _decide(
        ev_connected=True,
        ev_soc_pct=30.0,
        ev_target_pct=75.0,
        is_night=False,
        pv_power_w=8000.0,
        house_load_w=2000.0,
        current_price_ore=40.0,
        upcoming_prices_ore=[50, 40, 30, 20],
    )
    assert d.ev == EVAction.START
    assert d.ev_phase == "3_phase"


# ---------------------------------------------------------------------------
# Extra: Grid charge sets fast_charging True
# ---------------------------------------------------------------------------
def test_grid_charge_fast_charging_true():
    """Grid charge is the ONLY action that sets fast_charging=True."""
    d = _decide(
        current_price_ore=10.0,
        cheap_price_ore=30.0,
        battery_soc_pct=40.0,
    )
    assert d.battery == BatteryAction.CHARGE_GRID
    assert d.fast_charging is True


def test_pv_charge_fast_charging_false():
    """PV charge must NOT set fast_charging."""
    d = _decide(
        current_price_ore=40.0,
        upcoming_prices_ore=[60, 50, 40, 30],
        pv_power_w=5000.0,
        house_load_w=2000.0,
    )
    assert d.battery == BatteryAction.CHARGE_PV
    assert d.fast_charging is False
