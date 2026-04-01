"""Coordinator V2 — ren, utan legacy, bara core-moduler.

Varje 30s-cykel:
1. Startup Safety
2. Collect State
3. Grid Guard
4. Plan Generation (var 5 min)
5. Plan Execution
6. Battery Balancer
7. Natt-EV Workflow
8. Surplus Chain
9. Law Guardian
10. Persist State
11. Publish Sensors
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..const import (
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
)
from .battery_balancer import BatteryInfo, calculate_proportional_discharge
from .grid_guard import BatteryState, GridGuard, GridGuardConfig
from .law_guardian import GuardianState, LawGuardian
from .plan_executor import (
    ExecutorConfig,
    ExecutorState,
    PlanAction,
    execute_plan_hour,
)
from .startup import StartupState, evaluate_startup
from .surplus_chain import (
    ConsumerType,
    HysteresisState,
    SurplusConfig,
    SurplusConsumer,
    allocate_surplus,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoordinatorConfig:
    """All config — parameterstyrd."""

    ellevio_tak_kw: float = 2.0
    ellevio_night_weight: float = 0.5
    grid_guard_margin: float = 0.85
    battery_1_kwh: float = 15.0
    battery_2_kwh: float = 5.0
    battery_min_soc: float = 15.0
    battery_min_soc_cold: float = 20.0
    cold_lock_temp_c: float = 4.0
    max_discharge_kw: float = 5.0
    ev_phase_count: int = 3
    ev_min_amps: int = DEFAULT_EV_MIN_AMPS
    ev_max_amps: int = DEFAULT_EV_MAX_AMPS
    ev_target_soc: float = 75.0
    ev_departure_hour: int = 6
    ev_capacity_kwh: float = 92.0
    grid_charge_price_threshold: float = 15.0
    plan_interval_cycles: int = 10  # 10 x 30s = 5 min


@dataclass
class SystemState:
    """All sensor readings for one cycle."""

    grid_import_w: float = 0.0
    ellevio_viktat_kw: float = 0.0
    pv_power_w: float = 0.0
    battery_soc_1: float = -1.0
    battery_soc_2: float = -1.0
    battery_power_1: float = 0.0
    battery_power_2: float = 0.0
    battery_temp_1: float = 15.0
    battery_temp_2: float = 15.0
    ems_mode_1: str = ""
    ems_mode_2: str = ""
    fast_charging_1: bool = False
    fast_charging_2: bool = False
    ev_soc: float = -1.0
    ev_power_w: float = 0.0
    ev_connected: bool = False
    ev_enabled: bool = False
    current_price: float = 50.0
    disk_power_w: float = 0.0
    tvatt_power_w: float = 0.0
    miner_power_w: float = 0.0
    hour: int = 0
    minute: int = 0


@dataclass
class CycleResult:
    """Result of one coordinator cycle."""

    battery_commands: list[dict[str, Any]]  # Per adapter: {id, mode, power_limit, fast_charging}
    ev_command: dict[str, Any] | None  # {action, amps, override_schedule}
    surplus_actions: list[dict[str, Any]]
    grid_guard_status: str
    plan_action: str
    reason: str
    breaches: list[dict[str, Any]]
    notifications: list[dict[str, Any]]


class CoordinatorV2:
    """Clean coordinator — only calls core modules."""

    def __init__(self, config: CoordinatorConfig | None = None) -> None:
        self.config = config or CoordinatorConfig()
        self.grid_guard = GridGuard(
            GridGuardConfig(
                tak_kw=self.config.ellevio_tak_kw,
                night_weight=self.config.ellevio_night_weight,
                margin=self.config.grid_guard_margin,
                cold_lock_temp_c=self.config.cold_lock_temp_c,
            )
        )
        self.law_guardian = LawGuardian()
        self.surplus_hysteresis = HysteresisState()
        self.plan: list[PlanAction] = []
        self.night_ev_active: bool = False
        self.plan_counter: int = 0
        self.replan_deviation_count: int = 0
        self._startup_confirmed: bool = False
        self._restored_state: StartupState | None = None

    def set_restored_state(self, state: StartupState) -> None:
        """Restore state from persistent storage after restart."""
        self._restored_state = state
        self.night_ev_active = state.night_ev_active

    def cycle(self, state: SystemState) -> CycleResult:
        """Run one 30s cycle. Returns commands to execute."""
        cfg = self.config
        is_night = state.hour >= DEFAULT_NIGHT_START or state.hour < DEFAULT_NIGHT_END
        weight = cfg.ellevio_night_weight if is_night else 1.0
        bat_commands = []
        ev_cmd = None
        surplus_actions = []
        reason_parts = []

        # ── 1. STARTUP SAFETY ───────────────────────────────────
        if not self._startup_confirmed:
            sensors_ready = state.battery_soc_1 >= 0 and state.battery_soc_2 >= 0
            fc_off = not state.fast_charging_1 and not state.fast_charging_2
            startup = evaluate_startup(
                sensors_ready=sensors_ready,
                fast_charging_confirmed_off=fc_off,
                restored_state=self._restored_state,
                is_night=is_night,
                ev_connected=state.ev_connected,
                ev_soc=state.ev_soc,
                ev_target_soc=cfg.ev_target_soc,
            )
            if startup.action == "ready":
                self._startup_confirmed = True
            elif startup.action == "restore_ev":
                self._startup_confirmed = True
                self.night_ev_active = True
                ev_cmd = {
                    "action": "start",
                    "amps": cfg.ev_min_amps,
                    "override_schedule": True,
                }
            else:
                # Safe mode — standby + fast_charging OFF
                for i in range(2):
                    bat_commands.append(
                        {
                            "id": i,
                            "mode": "battery_standby",
                            "power_limit": 0,
                            "fast_charging": False,
                        }
                    )
                return CycleResult(
                    bat_commands,
                    None,
                    [],
                    startup.action,
                    "startup",
                    startup.reason,
                    [],
                    [],
                )

        # ── 3. GRID GUARD ───────────────────────────────────────
        batteries_gg = []
        for i, (soc, pw, tmp, ems, fc) in enumerate(
            [
                (
                    state.battery_soc_1,
                    state.battery_power_1,
                    state.battery_temp_1,
                    state.ems_mode_1,
                    state.fast_charging_1,
                ),
                (
                    state.battery_soc_2,
                    state.battery_power_2,
                    state.battery_temp_2,
                    state.ems_mode_2,
                    state.fast_charging_2,
                ),
            ]
        ):
            cap = cfg.battery_1_kwh if i == 0 else cfg.battery_2_kwh
            avail = max(0, (soc - cfg.battery_min_soc) / 100 * cap) if soc >= 0 else 0
            batteries_gg.append(
                BatteryState(
                    id=f"bat{i}",
                    soc=soc,
                    power_w=pw,
                    cell_temp_c=tmp,
                    ems_mode=ems,
                    fast_charging_on=fc,
                    available_kwh=avail,
                )
            )

        gg_result = self.grid_guard.evaluate(
            viktat_timmedel_kw=state.ellevio_viktat_kw,
            grid_import_w=max(0, state.grid_import_w),
            hour=state.hour,
            minute=state.minute,
            ev_power_w=state.ev_power_w,
            ev_amps=cfg.ev_min_amps if state.ev_enabled else 0,
            ev_phase_count=cfg.ev_phase_count,
            batteries=batteries_gg,
        )

        grid_guard_acted = False
        if gg_result.commands:
            grid_guard_acted = True
            for cmd in gg_result.commands:
                if cmd.get("action") == "pause_ev":
                    ev_cmd = {"action": "stop", "amps": 0, "override_schedule": False}
                elif cmd.get("action") == "set_ems_mode":
                    bat_id = 0 if "kontor" in cmd.get("battery_id", "bat0") else 1
                    bat_commands.append(
                        {
                            "id": bat_id,
                            "mode": cmd.get("mode", "battery_standby"),
                            "power_limit": 0,
                            "fast_charging": False,
                        }
                    )
            reason_parts.append(f"GG:{gg_result.status}")

        # ── 4. PLAN GENERATION (var 5 min) ──────────────────────
        self.plan_counter += 1
        if self.plan_counter >= cfg.plan_interval_cycles:
            self.plan_counter = 0
            # Plan generation delegeras till caller (behöver HA adapters)
            reason_parts.append("plan_due")

        # ── 5. PLAN EXECUTION (om grid guard inte agerade) ──────
        if not grid_guard_acted:
            planned = next((p for p in self.plan if p.hour == state.hour), None)
            headroom = gg_result.headroom_kw

            exec_state = ExecutorState(
                grid_import_w=max(0, state.grid_import_w),
                pv_power_w=state.pv_power_w,
                battery_soc_1=state.battery_soc_1,
                battery_soc_2=state.battery_soc_2,
                battery_power_1=state.battery_power_1,
                battery_power_2=state.battery_power_2,
                ev_power_w=state.ev_power_w,
                ev_soc=state.ev_soc,
                ev_connected=state.ev_connected,
                current_price=state.current_price,
                target_kw=cfg.ellevio_tak_kw,
                ellevio_weight=weight,
                headroom_kw=headroom,
            )
            exec_cfg = ExecutorConfig(
                ev_phase_count=cfg.ev_phase_count,
                ev_min_amps=cfg.ev_min_amps,
                ev_max_amps=cfg.ev_max_amps,
            )
            exec_cmd = execute_plan_hour(planned, exec_state, exec_cfg)
            reason_parts.append(f"exec:{exec_cmd.battery_action}")

            # ── 6. BATTERY BALANCER ─────────────────────────────
            if exec_cmd.battery_action == "discharge" and exec_cmd.battery_discharge_w > 0:
                bats = [
                    BatteryInfo(
                        "kontor",
                        state.battery_soc_1,
                        cfg.battery_1_kwh,
                        state.battery_temp_1,
                        min_soc=cfg.battery_min_soc,
                    ),
                    BatteryInfo(
                        "forrad",
                        state.battery_soc_2,
                        cfg.battery_2_kwh,
                        state.battery_temp_2,
                        min_soc=cfg.battery_min_soc,
                    ),
                ]
                bal = calculate_proportional_discharge(bats, exec_cmd.battery_discharge_w)
                for j, alloc in enumerate(bal.allocations):
                    bat_commands.append(
                        {
                            "id": j,
                            "mode": "discharge_pv",
                            "power_limit": alloc.watts,
                            "fast_charging": False,
                        }
                    )
            elif exec_cmd.battery_action == "charge_pv":
                for j in range(2):
                    bat_commands.append(
                        {
                            "id": j,
                            "mode": "charge_pv",
                            "power_limit": 0,
                            "fast_charging": False,
                        }
                    )
            elif exec_cmd.battery_action == "standby":
                for j in range(2):
                    bat_commands.append(
                        {
                            "id": j,
                            "mode": "battery_standby",
                            "power_limit": 0,
                            "fast_charging": False,
                        }
                    )

        # ── 7. NATT-EV WORKFLOW ─────────────────────────────────
        if (
            is_night
            and state.ev_connected
            and 0 <= state.ev_soc < cfg.ev_target_soc
            and not self.night_ev_active
            and state.ev_enabled
            and not grid_guard_acted
        ):
            bat_avail = sum(
                max(0, (s - cfg.battery_min_soc) / 100 * c)
                for s, c in [
                    (state.battery_soc_1, cfg.battery_1_kwh),
                    (state.battery_soc_2, cfg.battery_2_kwh),
                ]
                if s >= 0
            )
            min_bat_for_ev_kwh = 2.0
            if bat_avail > min_bat_for_ev_kwh:
                self.night_ev_active = True
                ev_cmd = {
                    "action": "start",
                    "amps": cfg.ev_min_amps,
                    "override_schedule": True,
                }
                reason_parts.append("night_ev_start")

        # Stop natt-EV at departure or target
        if self.night_ev_active:
            if (
                state.hour == cfg.ev_departure_hour
                or (state.ev_soc >= 0 and state.ev_soc >= cfg.ev_target_soc)
                or not is_night
            ):
                self.night_ev_active = False
                ev_cmd = {"action": "stop", "amps": 0, "override_schedule": False}
                reason_parts.append("night_ev_stop")
            elif ev_cmd is None:
                # Keep night EV running — re-assert charge command each cycle
                ev_cmd = {
                    "action": "start",
                    "amps": cfg.ev_min_amps,
                    "override_schedule": False,
                }
                reason_parts.append("night_ev_keep")

        # ── 8. SURPLUS CHAIN ────────────────────────────────────
        consumers = [
            SurplusConsumer(
                "miner",
                "Miner",
                5,
                ConsumerType.ON_OFF,
                400,
                500,
                state.miner_power_w,
                state.miner_power_w > 50,
            ),
        ]
        if state.grid_import_w < -100:
            surplus_result = allocate_surplus(
                abs(state.grid_import_w),
                consumers,
                self.surplus_hysteresis,
                SurplusConfig(start_delay_s=60, stop_delay_s=180),
            )
            surplus_actions = [
                {"id": a.id, "action": a.action, "target_w": a.target_w}
                for a in surplus_result.allocations
                if a.action != "none"
            ]

        # ── 9. LAW GUARDIAN ─────────────────────────────────────
        guardian_state = GuardianState(
            grid_import_w=max(0, state.grid_import_w),
            grid_viktat_timmedel_kw=state.ellevio_viktat_kw,
            ellevio_tak_kw=cfg.ellevio_tak_kw,
            battery_soc_1=state.battery_soc_1,
            battery_soc_2=state.battery_soc_2,
            battery_power_1=state.battery_power_1,
            battery_power_2=state.battery_power_2,
            battery_idle_hours=0,
            ev_soc=state.ev_soc,
            ev_target_soc=cfg.ev_target_soc,
            ev_departure_hour=cfg.ev_departure_hour,
            current_hour=state.hour,
            current_price=state.current_price,
            pv_power_w=state.pv_power_w,
            export_w=abs(min(0, state.grid_import_w)),
            ems_mode_1=state.ems_mode_1,
            ems_mode_2=state.ems_mode_2,
            fast_charging_1=state.fast_charging_1,
            fast_charging_2=state.fast_charging_2,
            cell_temp_1=state.battery_temp_1,
            cell_temp_2=state.battery_temp_2,
            min_soc=cfg.battery_min_soc,
            cold_lock_temp=cfg.cold_lock_temp_c,
        )
        guardian_report = self.law_guardian.evaluate(guardian_state)
        breaches = [
            {
                "law": b.law.value,
                "actual": b.actual_value,
                "limit": b.limit_value,
                "cause": b.root_cause,
            }
            for b in guardian_report.breaches
        ]

        return CycleResult(
            battery_commands=bat_commands,
            ev_command=ev_cmd,
            surplus_actions=surplus_actions,
            grid_guard_status=gg_result.status,
            plan_action=", ".join(reason_parts),
            reason=", ".join(reason_parts),
            breaches=breaches,
            notifications=guardian_report.notifications,
        )

    def get_persistent_state(self) -> dict[str, Any]:
        """State to persist for restart survival."""
        return {
            "night_ev_active": self.night_ev_active,
            "plan": [
                {
                    "hour": p.hour,
                    "action": p.action,
                    "battery_kw": p.battery_kw,
                    "grid_kw": p.grid_kw,
                    "price": p.price,
                    "battery_soc": p.battery_soc,
                    "ev_soc": p.ev_soc,
                }
                for p in self.plan
            ],
        }
