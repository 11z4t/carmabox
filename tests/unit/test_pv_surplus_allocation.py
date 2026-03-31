"""EXP-11: PV surplus allocation tests.

Tests the real-time PV allocation priority stack:
1. House (implicit) 2. EV (if home) 3. Battery 4. Consumers 5. Export (never)
"""

from __future__ import annotations

from custom_components.carmabox.core.planner import allocate_pv_surplus


def _alloc(**kwargs):
    """Helper with sensible defaults."""
    defaults = {
        "pv_now_w": 5000,
        "grid_now_w": 0,
        "house_consumption_w": 1500,
        "battery_soc_pct": 50,
        "battery_cap_kwh": 20,
        "ev_soc_pct": 60,
        "ev_connected": True,
        "ev_target_pct": 75,
        "is_workday": False,
        "hours_to_sunset": 6,
        "hourly_pv_remaining_kw": [5.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        "pv_confidence": 1.0,
    }
    defaults.update(kwargs)
    return allocate_pv_surplus(**defaults)


class TestNoSurplus:
    """When PV < house consumption, no allocation possible."""

    def test_no_surplus(self) -> None:
        r = _alloc(pv_now_w=1000, house_consumption_w=1500)
        assert r.ev_action == "hold"
        assert r.battery_action == "hold"
        assert r.consumers_action == "deactivate"
        assert r.will_export is False

    def test_zero_pv(self) -> None:
        r = _alloc(pv_now_w=0)
        assert r.surplus_w == 0
        assert r.ev_action == "hold"


class TestEVPriority:
    """Weekend + battery fills anyway = EV first."""

    def test_weekend_ev_first(self) -> None:
        """Weekend, big PV, battery fills anyway -> EV gets priority."""
        r = _alloc(
            pv_now_w=6000,
            house_consumption_w=1000,
            battery_soc_pct=50,
            ev_connected=True,
            ev_soc_pct=60,
            is_workday=False,
            hourly_pv_remaining_kw=[6.0, 6.0, 5.0, 4.0, 3.0, 2.0],  # 26 kWh
        )
        # 20 kWh cap * 50% need = 10 kWh. PV 26 kWh > 10+1 margin. EV priority.
        assert r.ev_action == "charge"
        assert r.ev_amps >= 6

    def test_workday_battery_first(self) -> None:
        """Workday, limited PV, battery needs charging -> battery first."""
        r = _alloc(
            pv_now_w=4000,
            house_consumption_w=1500,
            battery_soc_pct=30,
            ev_connected=False,  # Car not home on workday
            is_workday=True,
            hourly_pv_remaining_kw=[3.0, 3.0, 2.0, 1.0],
        )
        assert r.ev_action == "hold"
        assert r.battery_action == "charge"

    def test_battery_full_ev_gets_all(self) -> None:
        """Battery at 100% -> all surplus to EV."""
        r = _alloc(
            pv_now_w=6000,
            house_consumption_w=1000,
            battery_soc_pct=100,
            ev_connected=True,
            ev_soc_pct=50,
            is_workday=False,
        )
        assert r.ev_action == "charge"
        assert r.battery_action == "hold"

    def test_ev_not_connected(self) -> None:
        """EV not connected -> surplus to battery."""
        r = _alloc(
            pv_now_w=6000,
            house_consumption_w=1000,
            ev_connected=False,
            battery_soc_pct=50,
        )
        assert r.ev_action == "hold"
        assert r.battery_action == "charge"

    def test_ev_at_100_skipped(self) -> None:
        """EV at 100% -> no EV charging."""
        r = _alloc(ev_soc_pct=100, ev_connected=True)
        assert r.ev_action == "hold"
        assert r.ev_amps == 0


class TestBatteryAllocation:
    """Battery charges from remaining surplus after EV."""

    def test_battery_charges_from_surplus(self) -> None:
        r = _alloc(
            pv_now_w=4000,
            house_consumption_w=1500,
            ev_connected=False,
            battery_soc_pct=50,
        )
        assert r.battery_action == "charge"
        assert r.battery_target_w > 0

    def test_battery_at_100_no_charge(self) -> None:
        r = _alloc(battery_soc_pct=100, ev_connected=False)
        assert r.battery_action == "hold"


class TestConsumers:
    """Controllable consumers absorb remaining surplus."""

    def test_consumers_activated_on_surplus(self) -> None:
        """After EV + battery, remaining surplus -> consumers."""
        r = _alloc(
            pv_now_w=8000,
            house_consumption_w=1000,
            battery_soc_pct=100,
            ev_connected=False,
        )
        # Battery full, EV not connected -> all 7000W to consumers
        assert r.consumers_action == "activate"
        assert r.consumers_available_w > 0

    def test_no_consumers_when_no_surplus(self) -> None:
        r = _alloc(pv_now_w=1000, house_consumption_w=1500)
        assert r.consumers_action == "deactivate"


class TestExportPrevention:
    """Export should NEVER happen if consumers can absorb."""

    def test_no_export_when_consumers_available(self) -> None:
        r = _alloc(
            pv_now_w=8000,
            house_consumption_w=1000,
            battery_soc_pct=100,
            ev_connected=False,
        )
        # All surplus should go to consumers, zero export
        assert r.will_export is False

    def test_reason_includes_allocations(self) -> None:
        r = _alloc(
            pv_now_w=6000,
            house_consumption_w=1000,
            ev_connected=True,
            ev_soc_pct=50,
            battery_soc_pct=50,
            is_workday=False,
            hourly_pv_remaining_kw=[6.0, 6.0, 5.0, 4.0, 3.0, 2.0],
        )
        assert "PV" in r.reason


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_ev_soc_unknown_minus1(self) -> None:
        """ev_soc=-1 (unknown) -> don't charge."""
        r = _alloc(ev_soc_pct=-1, ev_connected=True)
        assert r.ev_action == "hold"

    def test_small_surplus_below_min(self) -> None:
        """Surplus exists but < EV min power -> battery only."""
        r = _alloc(
            pv_now_w=2000,
            house_consumption_w=1500,
            ev_connected=True,
            battery_soc_pct=50,
            is_workday=False,
            # PV fills battery anyway
            hourly_pv_remaining_kw=[5.0] * 6,
        )
        # 500W surplus < 4140W (6A x 3 x 230V) min EV
        assert r.ev_action == "hold"
        assert r.battery_action == "charge"
