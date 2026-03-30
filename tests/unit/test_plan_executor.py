"""Tests for Plan Executor — plan drives execution."""

from __future__ import annotations

from custom_components.carmabox.core.plan_executor import (
    ExecutorState,
    PlanAction,
    calculate_ev_amps,
    calculate_ev_start_amps,
    check_replan_needed,
    execute_plan_hour,
    should_charge_ev_full,
)


def _plan(
    action: str = "i",
    battery_kw: float = 0,
    grid_kw: float = 1.5,
    price: float = 50.0,
    battery_soc: int = 50,
    ev_soc: int = 50,
    hour: int = 14,
) -> PlanAction:
    return PlanAction(
        hour=hour,
        action=action,
        battery_kw=battery_kw,
        grid_kw=grid_kw,
        price=price,
        battery_soc=battery_soc,
        ev_soc=ev_soc,
    )


def _state(
    grid_w: float = 1500,
    pv_w: float = 0,
    soc1: float = 50,
    soc2: float = 50,
    ev_soc: float = 50,
    ev_connected: bool = False,
    price: float = 50.0,
    target_kw: float = 2.0,
    weight: float = 1.0,
    headroom_kw: float = 1.0,
    ev_last_full_charge_days: int = 0,
) -> ExecutorState:
    return ExecutorState(
        grid_import_w=grid_w,
        pv_power_w=pv_w,
        battery_soc_1=soc1,
        battery_soc_2=soc2,
        battery_power_1=0,
        battery_power_2=0,
        ev_power_w=0,
        ev_soc=ev_soc,
        ev_connected=ev_connected,
        current_price=price,
        target_kw=target_kw,
        ellevio_weight=weight,
        headroom_kw=headroom_kw,
        ev_last_full_charge_days=ev_last_full_charge_days,
    )


class TestPlanDischarge:
    def test_plan_says_discharge(self):
        cmd = execute_plan_hour(
            _plan(action="d", battery_kw=-2.0),
            _state(grid_w=3000),
        )
        assert cmd.battery_action == "discharge"
        assert cmd.battery_discharge_w >= 2000
        assert cmd.plan_followed is True

    def test_discharge_adjusts_to_actual_need(self):
        """If actual grid > planned, discharge more."""
        cmd = execute_plan_hour(
            _plan(action="d", battery_kw=-1.0, grid_kw=2.0),
            _state(grid_w=4000, target_kw=2.0, weight=1.0),
        )
        assert cmd.battery_discharge_w >= 2000  # More than planned 1kW

    def test_discharge_with_ev(self):
        """EV starts if headroom available during discharge."""
        cmd = execute_plan_hour(
            _plan(action="d", battery_kw=-2.0),
            _state(grid_w=2000, ev_connected=True, headroom_kw=5.0),
        )
        assert cmd.ev_amps >= 6
        assert cmd.ev_action == "start"


class TestPlanChargePV:
    def test_charge_pv_with_solar(self):
        cmd = execute_plan_hour(
            _plan(action="c"),
            _state(pv_w=3000),
        )
        assert cmd.battery_action == "charge_pv"
        assert cmd.plan_followed is True

    def test_charge_pv_no_solar(self):
        """No PV → standby instead of charge."""
        cmd = execute_plan_hour(
            _plan(action="c"),
            _state(pv_w=100),
        )
        assert cmd.battery_action == "standby"
        assert cmd.plan_followed is False


class TestPlanGridCharge:
    def test_grid_charge_cheap_price(self):
        cmd = execute_plan_hour(
            _plan(action="g"),
            _state(price=10.0),
        )
        assert cmd.battery_action == "grid_charge"
        assert cmd.plan_followed is True

    def test_grid_charge_expensive_skipped(self):
        """Price too high → standby."""
        cmd = execute_plan_hour(
            _plan(action="g"),
            _state(price=50.0),
        )
        assert cmd.battery_action == "standby"
        assert cmd.plan_followed is False


class TestPlanIdle:
    def test_idle_normal(self):
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=1500, target_kw=2.0, weight=1.0),
        )
        assert cmd.battery_action == "standby"
        assert cmd.plan_followed is True

    def test_idle_but_grid_over_target(self):
        """Idle but grid exceeds target → reactive discharge."""
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=3000, target_kw=2.0, weight=1.0),
        )
        assert cmd.battery_action == "discharge"
        assert cmd.battery_discharge_w > 0
        assert cmd.plan_followed is False


class TestPVOverride:
    def test_pv_override_during_idle(self):
        """Exporting + PV + batteries not full → charge regardless of plan."""
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=-500, pv_w=3000, soc1=50, soc2=50),
        )
        assert cmd.battery_action == "charge_pv"

    def test_pv_no_override_batteries_full(self):
        """Exporting but batteries full → follow plan (idle)."""
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=-500, pv_w=3000, soc1=100, soc2=100),
        )
        assert cmd.battery_action == "standby"


class TestNoPlan:
    def test_no_plan_with_pv(self):
        cmd = execute_plan_hour(
            None,
            _state(pv_w=3000),
        )
        assert cmd.battery_action == "charge_pv"

    def test_no_plan_no_pv(self):
        cmd = execute_plan_hour(
            None,
            _state(pv_w=100),
        )
        assert cmd.battery_action == "standby"

    def test_no_plan_grid_over_target_reactive_discharge(self):
        """No plan but grid > target → reactive discharge (RC-5 fix)."""
        cmd = execute_plan_hour(
            None,
            _state(grid_w=3000, pv_w=0, target_kw=2.0, weight=1.0),
        )
        assert cmd.battery_action == "discharge"
        assert cmd.battery_discharge_w > 0


class TestEVAmps:
    def test_3phase_6a(self):
        amps = calculate_ev_amps(4.2, phase_count=3, min_amps=6)
        assert amps == 6  # 4200W / 690 = 6.08 → 6

    def test_3phase_10a(self):
        amps = calculate_ev_amps(7.0, phase_count=3)
        assert amps == 10  # 7000 / 690 = 10.1

    def test_3phase_below_min(self):
        amps = calculate_ev_amps(1.0, phase_count=3, min_amps=6)
        assert amps == 0  # 1000 / 690 = 1.4 < 6

    def test_1phase_6a(self):
        amps = calculate_ev_amps(1.5, phase_count=1, min_amps=6)
        assert amps == 6  # 1500 / 230 = 6.5

    def test_max_clamped(self):
        amps = calculate_ev_amps(20.0, phase_count=3, max_amps=16)
        assert amps == 16

    def test_zero_headroom(self):
        amps = calculate_ev_amps(0, phase_count=3)
        assert amps == 0

    def test_negative_headroom(self):
        amps = calculate_ev_amps(-1.0, phase_count=3)
        assert amps == 0


class TestReplanNeeded:
    def test_no_deviation(self):
        replan, count = check_replan_needed(
            _plan(grid_kw=1.5, battery_soc=50, ev_soc=50),
            _state(grid_w=1500, soc1=50, ev_soc=50),
            deviation_count=0,
        )
        assert replan is False
        assert count == 0

    def test_grid_deviation(self):
        """Grid 50% over planned → deviation."""
        _, count = check_replan_needed(
            _plan(grid_kw=1.0),
            _state(grid_w=1500),  # 50% over
            deviation_count=0,
        )
        assert count == 1

    def test_replan_after_3_cycles(self):
        """3 consecutive deviations → replan."""
        replan, count = check_replan_needed(
            _plan(grid_kw=1.0),
            _state(grid_w=2000),
            deviation_count=2,  # This is the 3rd
        )
        assert replan is True
        assert count == 3

    def test_deviation_resets_on_ok(self):
        """Good cycle resets counter."""
        _, count = check_replan_needed(
            _plan(grid_kw=1.5),
            _state(grid_w=1500),  # Matches plan
            deviation_count=2,
        )
        assert count == 0

    def test_ev_soc_behind(self):
        """EV 10% behind plan → deviation."""
        _, count = check_replan_needed(
            _plan(ev_soc=70),
            _state(ev_soc=55),  # 15% behind
            deviation_count=0,
        )
        assert count == 1

    def test_battery_soc_deviation(self):
        """Battery SoC 20% off plan → deviation."""
        _, count = check_replan_needed(
            _plan(battery_soc=50),
            _state(soc1=25, soc2=25),  # 25% avg vs 50% planned
            deviation_count=0,
        )
        assert count == 1

    def test_no_plan_needs_replan(self):
        replan, _ = check_replan_needed(
            None,
            _state(),
            deviation_count=0,
        )
        assert replan is True


class TestEVStartAmps:
    def test_ev_start_amps_plenty_of_time(self):
        """8h to go, need 10% of 92kWh = 9.2kWh → 9.2/8=1.15kW → 2A → clamp to min 6A."""
        amps = calculate_ev_start_amps(
            ev_soc=65,
            ev_target_soc=75,
            ev_cap_kwh=92,
            hours_until_departure=8,
        )
        assert amps == 6  # min_amps, plenty of time

    def test_ev_start_amps_tight(self):
        """2h to go, need 20% of 92kWh = 18.4kWh → 9.2kW → ceil(9200/690) = 14A → clamp ≤10 → 10."""
        amps = calculate_ev_start_amps(
            ev_soc=55,
            ev_target_soc=75,
            ev_cap_kwh=92,
            hours_until_departure=2,
        )
        assert amps >= 8
        # With default max_amps=10, 14A is clamped to 10
        assert amps == 10

    def test_ev_start_amps_already_full(self):
        """SoC >= target → 0."""
        amps = calculate_ev_start_amps(
            ev_soc=80,
            ev_target_soc=75,
            ev_cap_kwh=92,
            hours_until_departure=5,
        )
        assert amps == 0

    def test_ev_start_amps_impossible(self):
        """1h, need 50% of 92kWh = 46kWh → 46kW → way over max → max_amps."""
        amps = calculate_ev_start_amps(
            ev_soc=25,
            ev_target_soc=75,
            ev_cap_kwh=92,
            hours_until_departure=1,
        )
        assert amps == 10  # DEFAULT_EV_MAX_AMPS


class TestEVFullCharge:
    """IT-GAP05: EV 100% charge every 7th day for battery calibration."""

    def test_ev_full_charge_after_7_days(self):
        """7 days since last full charge → target 100%."""
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=1500, target_kw=2.0, weight=1.0, ev_last_full_charge_days=7),
        )
        assert cmd.ev_target_soc == 100

    def test_ev_normal_charge_before_7_days(self):
        """3 days since last full charge → target 75%."""
        cmd = execute_plan_hour(
            _plan(action="i"),
            _state(grid_w=1500, target_kw=2.0, weight=1.0, ev_last_full_charge_days=3),
        )
        assert cmd.ev_target_soc == 75

    def test_should_charge_ev_full_boundary(self):
        """Boundary: 6 days → False, 7 days → True."""
        assert should_charge_ev_full(6) is False
        assert should_charge_ev_full(7) is True
