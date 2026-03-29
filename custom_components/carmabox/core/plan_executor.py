"""Plan Executor — reads plan, returns what to do.

Pure Python. No HA imports. Fully testable.

The plan DRIVES execution, not ad-hoc rules. Each 30s cycle:
1. Find planned action for current hour
2. Adjust for actual conditions (PV, grid, SoC)
3. Return commands to execute

Grid Guard has VETO — if Grid Guard acted, Plan Executor is skipped.

Key principles:
  - Plan says WHAT to do, executor says HOW
  - PV physics overrides plan (solar MUST be captured)
  - Reactive discharge if grid > target even during idle hours
  - EV amps calculated from grid headroom (3-phase aware)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlanAction:
    """What the plan says for this hour."""

    hour: int
    action: str  # 'c' = charge_pv, 'd' = discharge, 'g' = grid_charge, 'i' = idle
    battery_kw: float  # Planned battery power (+ charge, - discharge)
    grid_kw: float  # Planned grid import
    price: float  # Electricity price (öre/kWh)
    battery_soc: int  # Planned SoC at end of hour
    ev_soc: int  # Planned EV SoC at end of hour


@dataclass
class ExecutorConfig:
    """Parameterstyrd konfiguration."""

    ev_phase_count: int = 3
    ev_min_amps: int = 6
    ev_max_amps: int = 10
    grid_charge_price_threshold: float = 15.0
    pv_charge_threshold_w: float = 500.0
    reactive_discharge_margin: float = 1.05  # Discharge if grid > target × this


@dataclass
class ExecutorState:
    """Current system state for executor decisions."""

    grid_import_w: float
    pv_power_w: float
    battery_soc_1: float
    battery_soc_2: float
    battery_power_1: float  # + discharge, - charge
    battery_power_2: float
    ev_power_w: float
    ev_soc: float
    ev_connected: bool
    current_price: float
    target_kw: float  # Ellevio weighted target
    ellevio_weight: float  # Current hour weight (0.5 night, 1.0 day)
    headroom_kw: float  # From Grid Guard


@dataclass
class ExecutorCommand:
    """What executor wants to do."""

    battery_action: str  # "charge_pv" | "discharge" | "grid_charge" | "standby"
    battery_discharge_w: int  # Total discharge watts (0 if not discharging)
    ev_action: str  # "start" | "stop" | "adjust" | "none"
    ev_amps: int  # Target amps (0 = stop)
    reason: str
    plan_followed: bool  # True if executor followed the plan
    deviation_pct: float  # How much actual deviates from plan


def execute_plan_hour(
    plan_action: PlanAction | None,
    state: ExecutorState,
    config: ExecutorConfig | None = None,
) -> ExecutorCommand:
    """Determine what to do based on plan + actual state.

    Args:
        plan_action: What the plan says for this hour. None = no plan.
        state: Current system state.
        config: Executor configuration.

    Returns:
        ExecutorCommand with battery + EV actions.
    """
    cfg = config or ExecutorConfig()

    # ── No plan → safe standby ──────────────────────────────────
    if plan_action is None:
        return _pv_or_standby(state, cfg, "Ingen plan — safe standby")

    # ── PV override: solar MUST be captured ─────────────────────
    # Physics overrides plan — if PV is producing and batteries not full,
    # charge from PV regardless of what plan says
    is_exporting = state.grid_import_w < -100
    pv_producing = state.pv_power_w > cfg.pv_charge_threshold_w
    batteries_full = state.battery_soc_1 >= 99 and (
        state.battery_soc_2 < 0 or state.battery_soc_2 >= 99
    )

    if is_exporting and pv_producing and not batteries_full:
        return ExecutorCommand(
            battery_action="charge_pv",
            battery_discharge_w=0,
            ev_action="none",
            ev_amps=0,
            reason="PV överskott — ladda batteri (fysik)",
            plan_followed=plan_action.action == "c",
            deviation_pct=0,
        )

    # ── Execute plan action ─────────────────────────────────────
    action = plan_action.action

    if action == "d":  # Discharge
        return _execute_discharge(plan_action, state, cfg)

    elif action == "c":  # Charge from PV
        if pv_producing:
            return ExecutorCommand(
                battery_action="charge_pv",
                battery_discharge_w=0,
                ev_action="none",
                ev_amps=0,
                reason="Plan: PV-laddning",
                plan_followed=True,
                deviation_pct=0,
            )
        else:
            return ExecutorCommand(
                battery_action="standby",
                battery_discharge_w=0,
                ev_action="none",
                ev_amps=0,
                reason="Plan: PV-laddning men ingen sol → standby",
                plan_followed=False,
                deviation_pct=100,
            )

    elif action == "g":  # Grid charge
        if state.current_price <= cfg.grid_charge_price_threshold:
            return ExecutorCommand(
                battery_action="grid_charge",
                battery_discharge_w=0,
                ev_action="none",
                ev_amps=0,
                reason=f"Plan: Nätladdning ({state.current_price:.0f} öre)",
                plan_followed=True,
                deviation_pct=0,
            )
        else:
            return ExecutorCommand(
                battery_action="standby",
                battery_discharge_w=0,
                ev_action="none",
                ev_amps=0,
                reason=(
                    f"Plan: Nätladdning men pris {state.current_price:.0f}"
                    f" > {cfg.grid_charge_price_threshold:.0f} öre → standby"
                ),
                plan_followed=False,
                deviation_pct=100,
            )

    else:  # 'i' = idle
        return _execute_idle(plan_action, state, cfg)


def calculate_ev_amps(
    headroom_kw: float,
    phase_count: int = 3,
    min_amps: int = 6,
    max_amps: int = 10,
) -> int:
    """Calculate max EV amps that fit within grid headroom.

    3-phase aware: 1 amp = 230V × phase_count.
    """
    if headroom_kw <= 0:
        return 0
    w_per_amp = 230 * phase_count
    amps = int(headroom_kw * 1000 / w_per_amp)
    if amps < min_amps:
        return 0  # Below minimum → can't charge
    return min(amps, max_amps)


# ── Internal helpers ────────────────────────────────────────────


def _execute_discharge(
    plan: PlanAction,
    state: ExecutorState,
    cfg: ExecutorConfig,
) -> ExecutorCommand:
    """Execute planned discharge."""
    planned_w = abs(plan.battery_kw) * 1000
    grid_kw = max(0, state.grid_import_w) / 1000
    weight = state.ellevio_weight

    # Adjust discharge based on actual grid import
    if weight > 0:
        actual_need_w = max(0, (grid_kw - state.target_kw / weight)) * 1000 / weight
    else:
        actual_need_w = planned_w

    # Take the larger: planned OR actual need
    discharge_w = int(max(planned_w, actual_need_w))

    # Calculate EV amps from headroom
    ev_amps = 0
    ev_action = "none"
    if state.ev_connected and state.headroom_kw > 0:
        ev_amps = calculate_ev_amps(
            state.headroom_kw, cfg.ev_phase_count,
            cfg.ev_min_amps, cfg.ev_max_amps,
        )
        if ev_amps >= cfg.ev_min_amps:
            ev_action = "start"

    deviation = abs(discharge_w - planned_w) / max(1, planned_w) * 100

    return ExecutorCommand(
        battery_action="discharge",
        battery_discharge_w=discharge_w,
        ev_action=ev_action,
        ev_amps=ev_amps,
        reason=f"Plan: Urladda {discharge_w}W (planerat {planned_w:.0f}W)",
        plan_followed=True,
        deviation_pct=deviation,
    )


def _execute_idle(
    plan: PlanAction,
    state: ExecutorState,
    cfg: ExecutorConfig,
) -> ExecutorCommand:
    """Execute idle — but reactively discharge if grid over target."""
    grid_kw = max(0, state.grid_import_w) / 1000
    weight = state.ellevio_weight
    weighted_kw = grid_kw * weight

    if weighted_kw > state.target_kw * cfg.reactive_discharge_margin:
        # Grid over target — reactive discharge
        need_w = int((weighted_kw - state.target_kw) / weight * 1000) if weight > 0 else 0
        return ExecutorCommand(
            battery_action="discharge",
            battery_discharge_w=need_w,
            ev_action="none",
            ev_amps=0,
            reason=f"Idle men grid {grid_kw:.1f} kW > target → reaktiv urladdning {need_w}W",
            plan_followed=False,
            deviation_pct=100,
        )

    return ExecutorCommand(
        battery_action="standby",
        battery_discharge_w=0,
        ev_action="none",
        ev_amps=0,
        reason="Plan: Idle",
        plan_followed=True,
        deviation_pct=0,
    )


def _pv_or_standby(
    state: ExecutorState,
    cfg: ExecutorConfig,
    reason: str,
) -> ExecutorCommand:
    """Default: charge from PV if available, reactive discharge if over target, else standby."""
    if state.pv_power_w > cfg.pv_charge_threshold_w:
        return ExecutorCommand(
            battery_action="charge_pv",
            battery_discharge_w=0,
            ev_action="none",
            ev_amps=0,
            reason=reason + " (PV tillgänglig → ladda)",
            plan_followed=False,
            deviation_pct=0,
        )
    # Reactive discharge even without plan — LAG 1 trumps
    grid_kw = max(0, state.grid_import_w) / 1000
    weight = state.ellevio_weight
    weighted_kw = grid_kw * weight
    if weighted_kw > state.target_kw * cfg.reactive_discharge_margin:
        need_w = int((weighted_kw - state.target_kw) / weight * 1000) if weight > 0 else 0
        return ExecutorCommand(
            battery_action="discharge",
            battery_discharge_w=need_w,
            ev_action="none",
            ev_amps=0,
            reason=f"Ingen plan men grid {grid_kw:.1f} kW > target → reaktiv urladdning {need_w}W",
            plan_followed=False,
            deviation_pct=100,
        )
    return ExecutorCommand(
        battery_action="standby",
        battery_discharge_w=0,
        ev_action="none",
        ev_amps=0,
        reason=reason,
        plan_followed=False,
        deviation_pct=0,
    )


def check_replan_needed(
    plan_action: PlanAction | None,
    state: ExecutorState,
    deviation_count: int,
    threshold_pct: float = 20.0,
    threshold_cycles: int = 3,
) -> tuple[bool, int]:
    """Check if replanning is needed due to deviation.

    Returns (needs_replan, updated_deviation_count).
    """
    if plan_action is None:
        return True, deviation_count + 1

    grid_kw = max(0, state.grid_import_w) / 1000

    # Check deviations
    deviated = False

    # Grid import >20% over planned
    if plan_action.grid_kw > 0 and grid_kw > plan_action.grid_kw * (1 + threshold_pct / 100):
        deviated = True

    # EV SoC falling behind
    if state.ev_soc >= 0 and plan_action.ev_soc > 0 and state.ev_soc < plan_action.ev_soc - 5:
        deviated = True

    # Battery SoC significantly different
    avg_soc = state.battery_soc_1
    if state.battery_soc_2 >= 0:
        avg_soc = (state.battery_soc_1 + state.battery_soc_2) / 2
    if abs(avg_soc - plan_action.battery_soc) > 15:
        deviated = True

    new_count = deviation_count + 1 if deviated else 0

    return new_count >= threshold_cycles, new_count
