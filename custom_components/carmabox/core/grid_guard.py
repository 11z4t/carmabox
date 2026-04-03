"""Grid Guard — LAG 1: Ellevio viktat timmedel får ALDRIG överstiga tak.

Pure Python. No HA imports. Fully testable.

Runs EVERY 30s cycle BEFORE any other logic. Has VETO over all decisions.
Also enforces all INV-* invariants (crosscharge, fast_charging, cold lock, EMS auto).

Key concepts:
  - Ellevio measures weighted hourly average (klocktimme XX:00-XX:59)
  - Night weight = 0.5 (22-06), day weight = 1.0 (06-22)
  - Tak = 2.0 kW weighted (= 4.0 kW actual at night)
  - Projects where current hour will land, acts BEFORE breach
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from custom_components.carmabox.const import (
    GRID_GUARD_BRAKE_RELEASE_PCT,
    GRID_GUARD_BRAKE_THRESHOLD_PCT,
)

ACTION_LADDER_HYSTERESIS_S: float = 60.0  # PLAT-1164: min seconds between escalations

_EV_ACTIVE_MIN_W = 100  # W — minimum EV power to consider it actually charging


@dataclass
class GridGuardConfig:
    """Parameterstyrd konfiguration."""

    tak_kw: float = 2.0
    night_weight: float = 0.5
    margin: float = 0.85
    emergency_factor: float = 1.1
    day_start_hour: int = 6
    day_end_hour: int = 22
    main_fuse_a: int = 25
    main_fuse_phases: int = 3
    vp_min_temp_c: float = 10.0
    cold_lock_temp_c: float = 4.0
    recovery_hold_s: float = 60.0
    fallback_grid_w: float = 2000.0
    ev_min_amps: int = 6
    ladder_cooldown_s: float = ACTION_LADDER_HYSTERESIS_S  # PLAT-1164
    brake_threshold_pct: float = GRID_GUARD_BRAKE_THRESHOLD_PCT  # IT-2064
    brake_release_pct: float = GRID_GUARD_BRAKE_RELEASE_PCT  # IT-2064


@dataclass
class Consumer:
    """A controllable consumer in the action ladder."""

    id: str
    name: str
    power_w: float  # Current power draw
    is_active: bool
    priority_shed: int  # Lower = shed first (1=first to turn off)
    entity_switch: str = ""  # HA switch entity
    entity_climate: str = ""  # HA climate entity (for VP)
    min_w: float = 0  # Minimum operating power
    max_w: float = 0  # Maximum power


@dataclass
class BatteryState:
    """Battery state for invariant checks."""

    id: str
    soc: float
    power_w: float  # + = discharging, - = charging
    cell_temp_c: float
    ems_mode: str
    fast_charging_on: bool
    available_kwh: float


@dataclass
class GridGuardResult:
    """What Grid Guard wants to do."""

    status: str  # OK | WARNING | CRITICAL | RECOVERY
    headroom_kw: float = 0.0  # PLAT-1162: default prevents AttributeError on partial construction
    projected_kw: float = 0.0
    viktat_timmedel_kw: float = 0.0
    commands: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    invariant_violations: list[str] = field(default_factory=list)
    replan_needed: bool = False
    braking: bool = False  # IT-2064: proactive grid-import braking active


class GridGuard:
    """Enforces LAG 1 (Ellevio limit) and all INV-* invariants."""

    def __init__(self, config: GridGuardConfig | None = None) -> None:
        self.config = config or GridGuardConfig()
        self._hour: int = -1
        self._accumulated_viktat_wh: float = 0.0
        self._sample_count: int = 0
        self._last_grid_w: float = 0.0
        self._last_update: float = 0.0
        self._status: str = "OK"
        self._recovery_start: float = 0.0
        self._actions_taken: list[str] = []
        self._last_known_grid_w: float = 0.0
        self._last_projected_kw: float = 0.0  # PLAT-1162: init to avoid AttributeError
        self._last_ladder_ts: float = 0.0  # PLAT-1164: cooldown between escalations
        self._braking: bool = False  # IT-2064: proactive braking state

    # ── Public API ──────────────────────────────────────────────

    def evaluate(
        self,
        viktat_timmedel_kw: float,
        grid_import_w: float,
        hour: int,
        minute: int,
        ev_power_w: float = 0.0,
        ev_amps: int = 0,
        ev_phase_count: int = 3,
        batteries: list[BatteryState] | None = None,
        consumers: list[Consumer] | None = None,
        kontor_temp_c: float = 20.0,
        timestamp: float | None = None,
        fast_charge_authorized: bool = False,
    ) -> GridGuardResult:
        """Evaluate grid status. Called every 30s cycle.

        Returns actions to take. Caller executes commands.
        """
        ts = timestamp or time.monotonic()
        batteries = batteries or []
        consumers = consumers or []

        # Handle sensor unavailable
        if grid_import_w < -90000 or math.isnan(grid_import_w):
            grid_import_w = self._last_known_grid_w * 1.1  # +10% margin
        else:
            self._last_known_grid_w = grid_import_w

        # Hour reset
        if hour != self._hour:
            self._reset_hour(hour)

        # PLAT-1159: Cold-start seed — if no prior data this hour (first cycle after
        # restart or hour boundary with no restore_state()), seed _accumulated_viktat_wh
        # from the measured running average so the projection is immediately accurate.
        # Subsequent cycles use real accumulated measurements from _accumulate().
        # Safe: skipped when restore_state() provided non-zero accumulated data.
        if self._last_update == 0 and self._accumulated_viktat_wh == 0 and viktat_timmedel_kw > 0:
            self._accumulated_viktat_wh = viktat_timmedel_kw * (minute / 60.0) * 1000.0

        # Accumulate
        self._accumulate(grid_import_w, hour, ts)

        # Check invariants FIRST
        inv_result = self._check_invariants(batteries, fast_charge_authorized)

        # Calculate projection
        vikt = self._weight(hour)
        projected_kw = self._project(viktat_timmedel_kw, grid_import_w, vikt, minute)

        # IT-2064: braking thresholds (80% / 70% hysteresis)
        brake_limit = self.config.tak_kw * self.config.brake_threshold_pct
        brake_release_limit = self.config.tak_kw * self.config.brake_release_pct

        # Update braking hysteresis state
        if projected_kw >= brake_limit:
            self._braking = True
        elif projected_kw < brake_release_limit:
            self._braking = False
        # else: keep current _braking state (70-80% hysteresis band)

        # 3-level thresholds (plus BRAKE between OK and WARN)
        warn_limit = self.config.tak_kw * self.config.margin  # tak * 0.85
        stop_limit = self.config.tak_kw  # tak * 1.0
        emergency_limit = self.config.tak_kw * self.config.emergency_factor  # tak * 1.1
        headroom_kw = warn_limit - projected_kw

        # Check main fuse (absolute safety)
        main_fuse_w = self.config.main_fuse_a * 230 * self.config.main_fuse_phases
        if grid_import_w > main_fuse_w * 0.9:
            inv_result.invariant_violations.append(
                f"Huvudsäkring: {grid_import_w:.0f}W > {main_fuse_w * 0.9:.0f}W (90%)"
            )

        # Determine escalation level (IT-2064: BRAKE inserted between OK and WARN)
        if projected_kw > emergency_limit:
            level = "EMERGENCY"
        elif projected_kw > stop_limit:
            level = "STOP"
        elif projected_kw > warn_limit:
            level = "WARN"
        elif self._braking:
            level = "BRAKE"  # 80-85% zone or hysteresis hold
        else:
            level = "OK"

        # If invariants violated, fix them BUT also check headroom
        if inv_result.invariant_violations:
            inv_result.headroom_kw = headroom_kw
            inv_result.projected_kw = projected_kw
            inv_result.viktat_timmedel_kw = viktat_timmedel_kw
            inv_result.replan_needed = True
            inv_result.braking = self._braking  # IT-2064
            # ALSO run action ladder if over limit
            if level not in ("OK", "BRAKE"):
                inv_result.status = "CRITICAL"
                if ts - self._last_ladder_ts >= self.config.ladder_cooldown_s:
                    overshoot_w = abs(headroom_kw) * 1000 / max(0.01, vikt)
                    extra_cmds, reason = self._action_ladder(
                        overshoot_w,
                        consumers,
                        ev_power_w,
                        ev_amps,
                        ev_phase_count,
                        batteries,
                        kontor_temp_c,
                        level=level,
                    )
                    inv_result.commands.extend(extra_cmds)
                    inv_result.reason += f"; {reason}" if reason else ""
                    self._last_ladder_ts = ts  # PLAT-1164
            return inv_result

        # Headroom OK — no projection breach
        if level in ("OK", "BRAKE"):
            if self._status == "RECOVERY":
                if ts - self._recovery_start >= self.config.recovery_hold_s:
                    self._status = "OK"
                    self._actions_taken.clear()
                    self._last_ladder_ts = 0.0  # PLAT-1164: reset cooldown on full recovery
            elif self._status in ("WARNING", "CRITICAL"):
                self._status = "RECOVERY"
                self._recovery_start = ts

            # IT-2064: BRAKE overrides OK but not RECOVERY — recovery takes priority
            # so the coordinator can distinguish "calming down after incident" from
            # "proactive braking with no prior breach".
            effective_status = (
                "BRAKE" if level == "BRAKE" and self._status == "OK" else self._status
            )
            brake_commands: list[dict[str, Any]] = (
                [{"action": "limit_grid_charging"}] if self._braking else []
            )
            if effective_status == "BRAKE":
                brake_reason = f"Broms: {projected_kw:.2f} kW ≥ {brake_limit:.2f} kW (80% tak)"
            elif self._status == "OK":
                brake_reason = "OK"
            else:
                brake_reason = "Recovering"

            return GridGuardResult(
                status=effective_status,
                headroom_kw=headroom_kw,
                projected_kw=projected_kw,
                viktat_timmedel_kw=viktat_timmedel_kw,
                commands=brake_commands,
                reason=brake_reason,
                braking=self._braking,
            )

        # OVER LIMIT — action ladder with escalation level
        self._status = "CRITICAL" if level in ("STOP", "EMERGENCY") else "WARNING"

        overshoot_w = abs(headroom_kw) * 1000 / max(0.01, vikt)  # Convert to actual W
        if ts - self._last_ladder_ts >= self.config.ladder_cooldown_s:
            commands, reason = self._action_ladder(
                overshoot_w,
                consumers,
                ev_power_w,
                ev_amps,
                ev_phase_count,
                batteries,
                kontor_temp_c,
                level=level,
            )
            self._last_ladder_ts = ts  # PLAT-1164
        else:
            commands, reason = [], "Hysteres — väntar cooldown"

        return GridGuardResult(
            status=self._status,
            headroom_kw=headroom_kw,
            projected_kw=projected_kw,
            viktat_timmedel_kw=viktat_timmedel_kw,
            commands=commands,
            reason=reason,
            braking=self._braking,
        )

    @property
    def headroom_kw(self) -> float:
        """Current headroom in weighted kW."""
        return self.config.tak_kw * self.config.margin - self._last_projected_kw

    @property
    def projected_timmedel_kw(self) -> float:
        """Projected weighted hourly average."""
        return getattr(self, "_last_projected_kw", 0.0)

    @property
    def status(self) -> str:
        return self._status

    # ── Invariant checks ────────────────────────────────────────

    def _check_invariants(
        self,
        batteries: list[BatteryState],
        fast_charge_authorized: bool,
    ) -> GridGuardResult:
        """Check all INV-* invariants. Returns violations + fix commands."""
        violations: list[str] = []
        commands: list[dict[str, Any]] = []

        for bat in batteries:
            # INV-1: Never EMS auto
            if bat.ems_mode == "auto":
                violations.append(f"INV-1: {bat.id} EMS=auto")
                commands.append(
                    {
                        "action": "set_ems_mode",
                        "battery_id": bat.id,
                        "mode": "battery_standby",
                    }
                )

            # INV-3: Never fast_charging without authorization
            if bat.fast_charging_on and not fast_charge_authorized:
                violations.append(f"INV-3: {bat.id} fast_charging utan beslut")
                commands.append(
                    {
                        "action": "set_fast_charging",
                        "battery_id": bat.id,
                        "on": False,
                    }
                )

            # INV-4: Never charge at cold lock
            if bat.cell_temp_c < self.config.cold_lock_temp_c and bat.power_w < -50:
                violations.append(f"INV-4: {bat.id} laddar vid {bat.cell_temp_c:.1f}°C")
                commands.append(
                    {
                        "action": "set_ems_mode",
                        "battery_id": bat.id,
                        "mode": "battery_standby",
                    }
                )

        # INV-5: Never discharge below min_soc
        for bat in batteries:
            # PLAT-1161: 0.0 is falsy — must use explicit None check
            effective_min = (
                self.config.cold_lock_temp_c is not None
                and bat.cell_temp_c < self.config.cold_lock_temp_c
            )
            min_soc = 20.0 if effective_min else 15.0  # cold → higher floor
            if bat.soc < 0:
                continue  # SoC unavailable — skip check
            if bat.soc <= min_soc and bat.power_w > 50:  # discharging below min
                violations.append(
                    f"INV-5: {bat.id} urladdar vid SoC {bat.soc:.0f}% (min {min_soc:.0f}%)"
                )
                commands.append(
                    {
                        "action": "set_ems_mode",
                        "battery_id": bat.id,
                        "mode": "battery_standby",
                    }
                )

        # INV-2: Never crosscharge
        if len(batteries) >= 2:
            charging = [b for b in batteries if b.power_w < -50]
            discharging = [b for b in batteries if b.power_w > 50]
            if charging and discharging:
                violations.append(
                    f"INV-2: Korskörning {charging[0].id} laddar, {discharging[0].id} urladdar"
                )
                for bat in batteries:
                    commands.append(
                        {
                            "action": "set_ems_mode",
                            "battery_id": bat.id,
                            "mode": "battery_standby",
                        }
                    )

        return GridGuardResult(
            status="CRITICAL" if violations else "OK",
            headroom_kw=0,
            projected_kw=0,
            viktat_timmedel_kw=0,
            commands=commands,
            invariant_violations=violations,
            reason="; ".join(violations) if violations else "",
        )

    # ── Projection ──────────────────────────────────────────────

    def _weight(self, hour: int) -> float:
        """Ellevio weight for given hour."""
        if hour >= self.config.day_end_hour or hour < self.config.day_start_hour:
            return self.config.night_weight
        return 1.0

    def _project(
        self,
        viktat_timmedel_kw: float,
        grid_import_w: float,
        vikt: float,
        minute: int,
    ) -> float:
        """Project where weighted hourly average will land.

        PLAT-1159 formula fix: use _accumulated_viktat_wh (actual measured Wh
        so far this hour) instead of viktat_timmedel_kw x elapsed_min.
        The running-average proxy had dimensionally consistent but less accurate
        results -- accumulated Wh tracks real energy, not a lagged average.

        Formula: (accumulated_Wh_past + current_W x vikt x remaining_h) / 1000
        Result is kWh = kW (1-hour window), directly comparable to tak_kw.
        """
        remaining = max(1, 60 - minute)  # minutes remaining in this hour
        remaining_h = remaining / 60.0
        grid_viktat_w = max(0.0, grid_import_w) * vikt

        projected = (self._accumulated_viktat_wh + grid_viktat_w * remaining_h) / 1000.0
        self._last_projected_kw = projected
        return projected

    # ── Action ladder ───────────────────────────────────────────

    def _action_ladder(
        self,
        overshoot_w: float,
        consumers: list[Consumer],
        ev_power_w: float,
        ev_amps: int,
        ev_phase_count: int,
        batteries: list[BatteryState],
        kontor_temp_c: float,
        level: str = "EMERGENCY",
    ) -> tuple[list[dict[str, Any]], str]:
        """Determine actions to reduce grid import.

        Level controls max escalation:
          WARN     — shed consumers + reduce EV amps (no pause, no discharge)
          STOP     — shed consumers + pause EV (no discharge)
          EMERGENCY — full ladder including battery discharge
        """
        commands: list[dict[str, Any]] = []
        reasons: list[str] = []
        remaining = overshoot_w

        # Sort consumers by shed priority (lowest first = shed first)
        shedable = sorted(
            [c for c in consumers if c.is_active],
            key=lambda c: c.priority_shed,
        )

        # Step 1-4: Shed consumers in priority order
        for consumer in shedable:
            if remaining <= 0:
                break

            # Special handling for VP kontor — check temperature
            if consumer.id == "vp_kontor" and kontor_temp_c < self.config.vp_min_temp_c:
                continue  # Skip — too cold

            if consumer.entity_climate:
                commands.append(
                    {
                        "action": "set_hvac_off",
                        "entity": consumer.entity_climate,
                        "consumer_id": consumer.id,
                    }
                )
            elif consumer.entity_switch:
                commands.append(
                    {
                        "action": "switch_off",
                        "entity": consumer.entity_switch,
                        "consumer_id": consumer.id,
                    }
                )

            remaining -= consumer.power_w
            reasons.append(f"{consumer.name} av ({consumer.power_w:.0f}W)")
            self._actions_taken.append(consumer.id)

        # Step 5: Reduce EV amps (WARN level and above)
        if remaining > 0 and ev_power_w > _EV_ACTIVE_MIN_W and ev_amps > 0:
            w_per_amp = 230 * ev_phase_count
            amps_to_reduce = math.ceil(remaining / w_per_amp)
            new_amps = max(self.config.ev_min_amps, ev_amps - amps_to_reduce)

            if new_amps >= self.config.ev_min_amps and new_amps < ev_amps:
                reduction_w = (ev_amps - new_amps) * w_per_amp
                commands.append(
                    {
                        "action": "reduce_ev",
                        "amps": new_amps,
                    }
                )
                remaining -= reduction_w
                reasons.append(f"EV {ev_amps}→{new_amps}A")
                self._actions_taken.append("ev_reduced")

        # Step 6: Pause EV completely (STOP level and above)
        if level in ("STOP", "EMERGENCY") and remaining > 0 and ev_power_w > _EV_ACTIVE_MIN_W:
            commands.append({"action": "pause_ev"})
            remaining -= ev_power_w
            reasons.append(f"EV pausad ({ev_power_w:.0f}W)")
            self._actions_taken.append("ev_paused")

        # Step 7: Increase battery discharge (EMERGENCY only)
        if level == "EMERGENCY" and remaining > 0:
            total_available = sum(b.available_kwh for b in batteries)
            if total_available > 0.3:
                discharge_w = int(min(remaining, 5000))
                commands.append(
                    {
                        "action": "increase_discharge",
                        "watts": discharge_w,
                    }
                )
                remaining -= discharge_w
                reasons.append(f"Urladdning +{discharge_w}W")
                self._actions_taken.append("discharge_increased")

        reason = "; ".join(reasons) if reasons else "Inga resurser"
        return commands, reason

    # ── Internal ────────────────────────────────────────────────

    def _reset_hour(self, hour: int) -> None:
        """Reset accumulation at hour boundary."""
        self._hour = hour
        self._accumulated_viktat_wh = 0.0
        self._sample_count = 0
        self._last_update = 0.0  # PLAT-1160: must reset to avoid stale dt_s

    def _accumulate(self, grid_w: float, hour: int, ts: float) -> None:
        """Accumulate weighted energy for own projection."""
        if self._last_update > 0:
            dt_s = ts - self._last_update
            vikt = self._weight(hour)
            self._accumulated_viktat_wh += max(0, grid_w) * vikt * dt_s / 3600
        self._sample_count += 1
        self._last_grid_w = grid_w
        self._last_update = ts

    # ── PLAT-1095: Persistence helpers ──────────────────────────

    def get_persistent_state(self) -> dict[str, Any]:
        """Return state dict for persistence. Called by coordinator before saving."""
        return {
            "hour": self._hour,
            "accumulated_viktat_wh": self._accumulated_viktat_wh,
            "sample_count": self._sample_count,
            "last_grid_w": self._last_grid_w,
        }

    def restore_state(self, data: dict[str, Any], current_hour: int) -> None:
        """Restore state from persistence. Discards if hour has changed.

        _last_update is intentionally NOT restored — monotonic timestamps
        don't survive restarts. The first cycle after restore will skip
        accumulation (last_update=0) and resume normally from the second cycle.
        """
        if not data or data.get("hour", -1) != current_hour:
            return
        self._hour = current_hour
        self._accumulated_viktat_wh = float(data.get("accumulated_viktat_wh", 0.0))
        self._sample_count = int(data.get("sample_count", 0))
        self._last_grid_w = float(data.get("last_grid_w", 0.0))
