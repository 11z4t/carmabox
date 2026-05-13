"""
translate_target_to_action() — core logic from spec §7 + §7a.

Pure function: no HA dependency, no side effects. 100% pytest-able.

Decision tree (in order of priority):
  P1. Balancer disabled → HOLD (shadow pass-through)
  P2. Charger offline / fault → STOP / FAULT_COOLDOWN
  P3. Cable disconnected → HOLD (cable_disconnected)
  P4. SoC gate: ev_soc >= ev_target_soc → PAUSE/HOLD (soc_reached)
  P5. SoC gate: ev_soc < ev_min_start_soc → HOLD (soc_too_low) [if min>0]
  P6. Phase mode not three → PAUSE/HOLD (phase_mode_not_three)
  P7. Brain target == 0 → PAUSE (brain_target_zero)
  P8. Dwell/cooldown blocked → HOLD (dwell_hold / cooldown)
  P9. Compute W→A, apply R3 (never start >6A), R5 (cap), soft-fuse
  P10. Anti-reset guard (never drop running charge to 0 without explicit pause)
  P11. Emit SET_DYNAMIC if A changed, else HOLD (no-op)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    EvAction,
    EvBalancerStatus,
    RefusalReason,
)
from .models import EvBalancerState, EvDecision, SensorSnapshot

if TYPE_CHECKING:
    from .cooldown_manager import CooldownManager


def max_phase_load_a(snap: SensorSnapshot) -> float:
    """Return maximum current on any single phase (L1/L2/L3).

    Used for soft-fuse cap: EV dynamic_a + max_phase_load_a + headroom_a ≤ main_fuse_a.
    """
    return max(snap.house_phase_a_l1, snap.house_phase_a_l2, snap.house_phase_a_l3)


def w_to_a(watts: float, phase_count: int, line_voltage: float) -> int:
    """Convert Watts to Amps (floor), clamped to [0, EV_MAX_A].

    V2-bug was W / (230 × √3) ≈ W / 398.4 for 3-phase — WRONG.
    Correct: W // (phases × voltage_per_phase).
    """
    if phase_count <= 0 or line_voltage <= 0:
        return 0
    return int(watts // (phase_count * line_voltage))


def soft_fuse_cap_a(snap: SensorSnapshot) -> int:
    """Compute maximum allowed dynamic_a due to soft-fuse protection.

    Cap = floor(main_fuse_a - max_phase_load_a - headroom_a).
    Result is clamped to [0, snap.max_a].
    """
    max_load = max_phase_load_a(snap)
    cap = snap.house_main_fuse_a - max_load - snap.ev_phase_headroom_a
    return max(0, min(snap.max_a, int(cap)))


def translate_target_to_action(
    snap: SensorSnapshot,
    state: EvBalancerState,
    cooldowns: CooldownManager,
) -> EvDecision:
    """Translate brain_target_ev_w + sensor state → EvDecision.

    Implements spec §7 (translation) + §7a (W→A) + R3 + R4 + R5.
    Never has side effects — all state mutation is the caller's responsibility.
    """
    from .const import CooldownType  # local import avoids circular

    # ── P1: Shadow / disabled ─────────────────────────────────────────────
    if snap.balancer_disabled:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.BALANCER_DISABLED,
            status=EvBalancerStatus.SHADOW_MODE,
            status_detail="shadow mode: ev_balancer_disable=on",
        )

    # ── P2: Charger offline / persistent fault ────────────────────────────
    if snap.charger_fault:
        if cooldowns.is_blocked(CooldownType.FAULT_COOLDOWN):
            return EvDecision(
                action=EvAction.HOLD,
                reason=RefusalReason.FAULT_COOLDOWN,
                status=EvBalancerStatus.FAULT,
                dwell_remaining_s=cooldowns.remaining(CooldownType.FAULT_COOLDOWN),
            )
        return EvDecision(
            action=EvAction.STOP,
            dynamic_a=0,
            reason=RefusalReason.CHARGER_FAULT,
            status=EvBalancerStatus.FAULT,
        )

    if state.charger_offline:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.CHARGER_OFFLINE,
            status=EvBalancerStatus.OFFLINE,
        )

    # ── P3: Cable disconnected ────────────────────────────────────────────
    if not snap.cable_connected:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.CABLE_DISCONNECTED,
            status=EvBalancerStatus.CABLE_DISCONNECTED,
        )

    # ── P4: SoC target reached ────────────────────────────────────────────
    if snap.ev_soc >= snap.ev_target_soc:
        if state.last_action in (EvAction.SET_DYNAMIC, EvAction.RESUME):
            return EvDecision(
                action=EvAction.PAUSE,
                dynamic_a=0,
                reason=RefusalReason.SOC_REACHED,
                status=EvBalancerStatus.SOC_REACHED,
            )
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.SOC_REACHED,
            status=EvBalancerStatus.SOC_REACHED,
        )

    # ── P5: SoC too low to start (ev_min_start_soc guard) ────────────────
    if snap.ev_min_start_soc > 0 and snap.ev_soc < snap.ev_min_start_soc:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.SOC_TOO_LOW,
            status=EvBalancerStatus.PAUSED,
            status_detail=f"soc {snap.ev_soc:.0f}% < min_start {snap.ev_min_start_soc:.0f}%",
        )

    # ── P6: Phase mode not three ──────────────────────────────────────────
    if snap.easee_phase_mode != "three":
        if state.last_dynamic_a > 0:
            return EvDecision(
                action=EvAction.STOP,
                dynamic_a=0,
                reason=RefusalReason.PHASE_MODE_NOT_THREE,
                status=EvBalancerStatus.FAULT,
                status_detail=f"phase_mode={snap.easee_phase_mode}",
            )
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.PHASE_MODE_NOT_THREE,
            status=EvBalancerStatus.FAULT,
        )

    # ── P7: Brain target == 0 (pause) ────────────────────────────────────
    if snap.brain_target_ev_w <= 0:
        if cooldowns.is_blocked(CooldownType.PAUSE_DWELL):
            return EvDecision(
                action=EvAction.HOLD,
                reason=RefusalReason.PAUSE_DWELL,
                status=EvBalancerStatus.PAUSED,
                dwell_remaining_s=cooldowns.remaining(CooldownType.PAUSE_DWELL),
            )
        if state.last_action in (EvAction.SET_DYNAMIC, EvAction.RESUME) or state.last_dynamic_a > 0:
            return EvDecision(
                action=EvAction.PAUSE,
                dynamic_a=0,
                reason=RefusalReason.BRAIN_TARGET_ZERO,
                status=EvBalancerStatus.PAUSED,
            )
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.BRAIN_TARGET_ZERO,
            status=EvBalancerStatus.PAUSED,
        )

    # ── P8: Post-pause cooldown (after PAUSE action) ──────────────────────
    if cooldowns.is_blocked(CooldownType.POST_PAUSE):
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.POST_PAUSE_COOLDOWN,
            status=EvBalancerStatus.PAUSED,
            dwell_remaining_s=cooldowns.remaining(CooldownType.POST_PAUSE),
        )

    # ── P8b: Soft-fuse recovery cooldown ─────────────────────────────────
    if cooldowns.is_blocked(CooldownType.SOFT_FUSE_RECOVERY):
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.SOFT_FUSE_RECOVERY_COOLDOWN,
            status=EvBalancerStatus.SOFT_FUSE_THROTTLE,
            dwell_remaining_s=cooldowns.remaining(CooldownType.SOFT_FUSE_RECOVERY),
        )

    # ── P9a: Compute desired_a from brain_target_ev_w (W→A) ──────────────
    desired_a = w_to_a(snap.brain_target_ev_w, snap.phase_count, snap.line_voltage)

    # ── P9b: Soft-fuse cap (spec §7a) ─────────────────────────────────────
    fuse_cap = soft_fuse_cap_a(snap)
    rejected_w = 0.0
    soft_fuse_engaged = False
    if desired_a > fuse_cap:
        rejected_w = (desired_a - fuse_cap) * snap.phase_count * snap.line_voltage
        desired_a = fuse_cap
        soft_fuse_engaged = True

    # ── P9c: Hard cap at physical fuse ───────────────────────────────────
    desired_a = min(desired_a, int(snap.ev_max_physical_fuse_a), snap.max_a)

    # ── P9c+: Brain-offer invariant clamp ────────────────────────────────
    # Balancer NEVER commands more than brain_target_ev_w + 100W.
    # brain_target_ev_w is already zeroed upstream if stale (coordinator).
    _brain_cap_a = max(
        0, int((snap.brain_target_ev_w + 100.0) / max(1.0, snap.line_voltage * snap.phase_count))
    )
    if desired_a > _brain_cap_a:
        _clamped_w = (desired_a - _brain_cap_a) * snap.phase_count * snap.line_voltage
        rejected_w += _clamped_w
        desired_a = _brain_cap_a

    # ── P9d: If soft-fuse cap is below min_a → HOLD (cannot safely charge) ──
    if 0 < desired_a < snap.min_a:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.SOFT_FUSE_CAP,
            status=EvBalancerStatus.SOFT_FUSE_THROTTLE,
            rejected_w=snap.brain_target_ev_w,
            status_detail=f"soft_fuse_cap={fuse_cap}A < min_a={snap.min_a}A",
        )

    # ── P9e: R3 — first charge after pause/stop always starts at min_a ───
    # Governs: last_action ∈ {stop, pause, None} OR last_dynamic_a == 0
    is_first_after_pause = state.last_dynamic_a == 0 or state.last_action in (
        EvAction.STOP,
        EvAction.PAUSE,
        None,
    )

    if is_first_after_pause:
        # R3: never start > min_a (default 6A)
        desired_a = min(desired_a, snap.min_a) if desired_a > 0 else 0
        # If we have a positive target but fuse/soft-fuse dropped it to 0: hold
        if snap.brain_target_ev_w > 0 and desired_a == 0:
            return EvDecision(
                action=EvAction.HOLD,
                reason=RefusalReason.SOFT_FUSE_CAP,
                status=EvBalancerStatus.SOFT_FUSE_THROTTLE,
                rejected_w=rejected_w,
            )
        # R3 forces 6A as start — emit RESUME then SET_DYNAMIC
        return EvDecision(
            action=EvAction.RESUME if state.last_action == EvAction.PAUSE else EvAction.SET_DYNAMIC,
            dynamic_a=max(desired_a, snap.min_a) if desired_a > 0 else snap.min_a,
            status=EvBalancerStatus.SOFT_FUSE_THROTTLE
            if soft_fuse_engaged
            else EvBalancerStatus.OK,
            rejected_w=rejected_w,
        )

    # ── P9f: R4 — ramp up at most 1A per min_dwell_s period ──────────────
    current_a = state.last_dynamic_a
    # 1A-per-dwell ramp up; ramp down is immediate (safety)
    target_a = (
        min(current_a + 1, desired_a) if current_a > 0 and desired_a > current_a else desired_a
    )

    # ── P8c: Amp dwell hold (between consecutive amp changes) ────────────
    if target_a != current_a:
        if cooldowns.is_blocked(CooldownType.AMP_DWELL):
            return EvDecision(
                action=EvAction.HOLD,
                reason=RefusalReason.DWELL_HOLD,
                status=EvBalancerStatus.SOFT_FUSE_THROTTLE
                if soft_fuse_engaged
                else EvBalancerStatus.OK,
                dwell_remaining_s=cooldowns.remaining(CooldownType.AMP_DWELL),
                rejected_w=rejected_w,
            )

    # ── P10: Anti-reset guard ─────────────────────────────────────────────
    # Active charge must never drop to 0 without explicit pause reason.
    # This catches the v2.9 bug where template reset ev_balancer_desired_a→0 mid-cycle.
    if current_a > 0 and target_a == 0 and snap.brain_target_ev_w > 0:
        return EvDecision(
            action=EvAction.HOLD,
            reason=RefusalReason.ANTI_RESET_GUARD,
            status=EvBalancerStatus.OK,
            status_detail="anti-reset guard: refused to zero active charge without pause reason",
        )

    # ── P11: BMS detection ────────────────────────────────────────────────
    bms_limited = (
        current_a > 0
        and snap.easee_actual_a > 0
        and snap.easee_actual_a < current_a - 1.5  # >1.5A below commanded
    )

    # ── Emit decision ──────────────────────────────────────────────────────
    if target_a == current_a:
        # No change needed
        return EvDecision(
            action=EvAction.HOLD,
            status=EvBalancerStatus.BMS_LIMITED
            if bms_limited
            else (
                EvBalancerStatus.SOFT_FUSE_THROTTLE if soft_fuse_engaged else EvBalancerStatus.OK
            ),
            rejected_w=rejected_w,
        )

    return EvDecision(
        action=EvAction.SET_DYNAMIC,
        dynamic_a=target_a,
        status=EvBalancerStatus.BMS_LIMITED
        if bms_limited
        else (EvBalancerStatus.SOFT_FUSE_THROTTLE if soft_fuse_engaged else EvBalancerStatus.OK),
        rejected_w=rejected_w,
    )
