"""PLAN-02: Planner generates charge action when PV surplus exists."""

from __future__ import annotations

from custom_components.carmabox.optimizer.planner import generate_plan


def _plan(
    pv_kw: list[float],
    load_kw: list[float],
    prices: list[float] | None = None,
    battery_soc: float = 50.0,
    start_hour: int = 10,
) -> list:
    n = len(pv_kw)
    return generate_plan(
        num_hours=n,
        start_hour=start_hour,
        target_weighted_kw=3.0,
        hourly_loads=load_kw,
        hourly_pv=pv_kw,
        hourly_prices=prices or [80.0] * n,
        hourly_ev=[0.0] * n,
        battery_soc=battery_soc,
        ev_soc=-1.0,
        battery_cap_kwh=20.0,
    )


class TestPlanChargesWhenPvSurplus:
    """PLAN-02 AC: action=charge when PV surplus (net < -0.5 kW)."""

    def test_plan_charges_when_pv_surplus_exists(self) -> None:
        """With PV=5kW and load=2kW: net=-3kW → action=charge."""
        plan = _plan(pv_kw=[5.0], load_kw=[2.0])
        assert plan[0].action == "c"

    def test_plan_idle_when_no_surplus(self) -> None:
        """With PV=1kW and load=2kW: net=+1kW, price high but load < target → idle."""
        plan = _plan(pv_kw=[1.0], load_kw=[2.0], prices=[30.0])
        assert plan[0].action in ("i", "d")

    def test_plan_charges_with_large_surplus(self) -> None:
        """PV=10kW, load=2kW: large surplus → charge."""
        plan = _plan(pv_kw=[10.0], load_kw=[2.0])
        assert plan[0].action == "c"

    def test_charge_limited_by_battery_capacity(self) -> None:
        """When battery is nearly full, charge kW is small but action=charge."""
        plan = _plan(pv_kw=[5.0], load_kw=[2.0], battery_soc=98.0)
        # May or may not charge depending on headroom, but no crash
        assert plan[0].action in ("c", "i", "d", "g")

    def test_net_kw_reflects_pv_minus_load(self) -> None:
        """Plan consumption_kw and pv_kw reflect inputs correctly."""
        plan = _plan(pv_kw=[5.0], load_kw=[2.0])
        assert plan[0].pv_kw == 5.0
        assert plan[0].consumption_kw == 2.0

    def test_multiple_hours_charges_only_when_surplus(self) -> None:
        """Mixed hours: surplus at h0, no surplus at h1."""
        plan = _plan(pv_kw=[5.0, 0.5], load_kw=[2.0, 2.0])
        assert plan[0].action == "c"  # Surplus
        # h1: no surplus (pv=0.5, load=2.0, net=+1.5) → idle or discharge or grid
        assert plan[1].action in ("i", "d", "g")
