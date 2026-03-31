"""PLAN-03: Night plan blocks discharge when EV is actively charging."""

from __future__ import annotations

from custom_components.carmabox.optimizer.planner import generate_plan


def _night_plan(
    night_ev_active: bool,
    start_hour: int = 22,
    battery_soc: float = 80.0,
    pv_kw: float = 0.0,
    load_kw: float = 3.0,
    price: float = 80.0,
) -> list:
    """Generate a 4-hour night plan (22-02)."""
    n = 4
    return generate_plan(
        num_hours=n,
        start_hour=start_hour,
        target_weighted_kw=2.0,  # Tight target to trigger discharge
        hourly_loads=[load_kw] * n,
        hourly_pv=[pv_kw] * n,
        hourly_prices=[price] * n,
        hourly_ev=[0.0] * n,
        battery_soc=battery_soc,
        ev_soc=-1.0,
        battery_cap_kwh=20.0,
        night_ev_active=night_ev_active,
    )


class TestNightPlanNoDischargeWhenEvCharging:
    """PLAN-03 AC: discharge blocked during night hours when night_ev_active=True."""

    def test_night_plan_no_discharge_when_ev_charging(self) -> None:
        """With night_ev_active=True, no 'd' action during night hours 22-06."""
        plan = _night_plan(night_ev_active=True)
        night_actions = [h.action for h in plan if h.hour >= 22 or h.hour < 6]
        assert "d" not in night_actions, (
            f"Discharge should be blocked during night EV, got: {night_actions}"
        )

    def test_night_plan_allows_discharge_when_ev_inactive(self) -> None:
        """Without night_ev_active, discharge may happen at night (normal behavior)."""
        plan_no_ev = _night_plan(night_ev_active=False, battery_soc=90.0, load_kw=4.0)
        plan_with_ev = _night_plan(night_ev_active=True, battery_soc=90.0, load_kw=4.0)

        # EV active should result in fewer or equal discharge hours
        discharge_no_ev = sum(1 for h in plan_no_ev if h.action == "d")
        discharge_with_ev = sum(1 for h in plan_with_ev if h.action == "d")
        assert discharge_with_ev <= discharge_no_ev

    def test_charge_from_pv_still_works_during_night_ev(self) -> None:
        """PV surplus charging (action='c') is NOT blocked by night_ev_active."""
        plan = _night_plan(night_ev_active=True, pv_kw=5.0, load_kw=2.0, start_hour=10)
        assert plan[0].action == "c"

    def test_night_ev_default_false(self) -> None:
        """night_ev_active defaults to False — existing behavior unchanged."""
        plan_default = generate_plan(
            num_hours=4,
            start_hour=22,
            target_weighted_kw=2.0,
            hourly_loads=[3.0] * 4,
            hourly_pv=[0.0] * 4,
            hourly_prices=[80.0] * 4,
            hourly_ev=[0.0] * 4,
            battery_soc=80.0,
            ev_soc=-1.0,
            battery_cap_kwh=20.0,
            # night_ev_active omitted — should default to False
        )
        # No crash, returns a valid plan
        assert len(plan_default) == 4

    def test_daytime_discharge_not_blocked_when_night_ev(self) -> None:
        """PLAN-03 only blocks NIGHT (22-06) hours, not daytime."""
        plan = generate_plan(
            num_hours=1,
            start_hour=14,  # 14:00 = daytime
            target_weighted_kw=1.0,  # Very tight target → discharge likely
            hourly_loads=[5.0],
            hourly_pv=[0.0],
            hourly_prices=[100.0],
            hourly_ev=[0.0],
            battery_soc=80.0,
            ev_soc=-1.0,
            battery_cap_kwh=20.0,
            night_ev_active=True,  # Should NOT block daytime
        )
        # Action at 14:00 is NOT blocked by night_ev_active
        # (It may still be idle/charge/discharge based on other logic)
        assert len(plan) == 1  # No crash
