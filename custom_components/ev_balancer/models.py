"""Pure dataclasses for ev_balancer — no HA dependency."""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    DEFAULT_CYCLE_S,
    DEFAULT_FAULT_COOLDOWN_S,
    DEFAULT_MAX_A,
    DEFAULT_MIN_A,
    DEFAULT_MIN_DWELL_S,
    DEFAULT_PAUSE_DWELL_S,
    DEFAULT_PHASE_HEADROOM_A,
    DEFAULT_POST_PAUSE_COOLDOWN_S,
    DEFAULT_SOFT_FUSE_RECOVERY_COOLDOWN_S,
    EV_MAX_A,
    LINE_VOLTAGE_DEFAULT,
    PHASE_COUNT_DEFAULT,
    EvAction,
    EvBalancerStatus,
    RefusalReason,
)


@dataclass(frozen=True)
class SensorSnapshot:
    """Immutable snapshot of all HA sensor values read in one cycle."""

    # Brain decision
    brain_target_ev_w: float = 0.0

    # EV state
    ev_soc: float = 0.0  # 0-100 %
    ev_target_soc: float = 90.0  # stop charging at this %
    ev_min_start_soc: float = 0.0  # don't start if below (0 = disabled)

    # Easee charger state
    easee_status: str = "disconnected"
    easee_phase_mode: str = "three"  # "three" | "one" | "unknown"
    easee_actual_a: float = 0.0  # actual measured current
    easee_power_w: float = 0.0
    easee_is_enabled: bool = False  # switch.easee_home_is_enabled

    # House phase currents (A per phase, for soft-fuse)
    house_phase_a_l1: float = 0.0
    house_phase_a_l2: float = 0.0
    house_phase_a_l3: float = 0.0

    # Balancer configuration (read from helpers)
    phase_count: int = PHASE_COUNT_DEFAULT
    line_voltage: float = LINE_VOLTAGE_DEFAULT
    house_main_fuse_a: float = 25.0
    ev_phase_headroom_a: float = DEFAULT_PHASE_HEADROOM_A
    ev_max_physical_fuse_a: float = EV_MAX_A
    min_a: int = DEFAULT_MIN_A
    max_a: int = DEFAULT_MAX_A
    cycle_s: float = DEFAULT_CYCLE_S
    min_dwell_s: float = DEFAULT_MIN_DWELL_S
    pause_dwell_s: float = DEFAULT_PAUSE_DWELL_S
    fault_cooldown_s: float = DEFAULT_FAULT_COOLDOWN_S
    soft_fuse_recovery_cooldown_s: float = DEFAULT_SOFT_FUSE_RECOVERY_COOLDOWN_S
    post_pause_cooldown_s: float = DEFAULT_POST_PAUSE_COOLDOWN_S

    # Shadow-deploy gate
    balancer_disabled: bool = False

    # Operator override mode (AC81)
    balancer_mode: str = "AUTO"  # AUTO | MANUAL_FIXED | MANUAL_DYNAMIC_NO_IMPORT | SHADOW
    target_manual_a: float = 0.0  # amps for MANUAL_FIXED mode (0 = stop)

    @property
    def cable_connected(self) -> bool:
        return self.easee_status not in ("disconnected", "error", "offline")

    @property
    def charger_fault(self) -> bool:
        return self.easee_status in ("error", "offline")

    @property
    def min_charge_w(self) -> float:
        return self.min_a * self.phase_count * self.line_voltage


@dataclass
class EvBalancerState:
    """Mutable runtime state for the balancer across cycles."""

    status: EvBalancerStatus = EvBalancerStatus.INITIALIZING
    status_reason: str = ""

    # Last confirmed HW write
    last_dynamic_a: int = 0
    last_action: EvAction | None = None
    last_hw_write_ts: float = 0.0  # epoch seconds

    # Metrics (rolling 24h — coordinator maintains the lists)
    amp_changes_24h: int = 0
    pause_resume_count_24h: int = 0
    fault_count_24h: int = 0
    soft_fuse_engagements_24h: int = 0

    # Transient flags set by coordinator, consumed by translation_logic
    cable_hot_unplug_detected: bool = False  # B→A mid-charge
    charger_offline: bool = False

    # Rejected target (for capability sensor — Brain must NOT see this cap)
    rejected_w: float = 0.0

    # Runtime-computed capability
    capability_max_w: float = 0.0
    capability_min_w: float = 0.0

    # Safety guard: how many times EaseeClient clamped a value above 16 A
    safety_guard_corrections: int = 0


@dataclass(frozen=True)
class EvDecision:
    """Immutable result of one translate_target_to_action() call."""

    action: EvAction
    dynamic_a: int = 0  # 0 means "don't write" unless action==SET_DYNAMIC/STOP
    reason: RefusalReason | None = None
    status: EvBalancerStatus = EvBalancerStatus.OK
    status_detail: str = ""
    rejected_w: float = 0.0  # how many W were cut by soft-fuse
    dwell_remaining_s: float = 0.0
