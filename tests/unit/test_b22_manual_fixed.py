"""Tests for B22: ev_balancer P0 (MANUAL_FIXED) and P0b (SHADOW) modes.

Covers translate_target_to_action() P0/P0b in translation_logic.py:
  TC-B22-1: MANUAL_FIXED target=10A, last_a=0 → SET_DYNAMIC 10A, status=OK
  TC-B22-2: MANUAL_FIXED target=0A, last_a=8 → PAUSE, dynamic_a=0, status=PAUSED
  TC-B22-3: MANUAL_FIXED target=0A, last_a=0 → HOLD, status=PAUSED (already stopped)
  TC-B22-4: MANUAL_FIXED target=10A, last_a=10 → HOLD, status=OK (no-op)
  TC-B22-5: MANUAL_FIXED target=40A (> physical_fuse=32A) → clamped → SET_DYNAMIC 32A
  TC-B22-6: SHADOW mode → HOLD SHADOW_MODE (no Brain/fuse checks — P0b fires first)
  TC-B22-7: MANUAL_FIXED fires before P1 — balancer_disabled=True doesn't block it
"""

from __future__ import annotations

from custom_components.ev_balancer.const import EvAction, EvBalancerStatus
from custom_components.ev_balancer.models import EvBalancerState, SensorSnapshot
from custom_components.ev_balancer.translation_logic import translate_target_to_action


class FakeCooldowns:
    """No active cooldowns — pure mode logic under test."""

    def is_blocked(self, _kind: object) -> bool:
        return False

    def remaining(self, _kind: object) -> float:
        return 0.0

    def tick(self, _elapsed: float) -> None:
        pass


def _snap(
    *,
    balancer_mode: str = "AUTO",
    target_manual_a: float = 0.0,
    ev_max_physical_fuse_a: float = 16.0,
    balancer_disabled: bool = False,
    brain_target_ev_w: float = 0.0,
    easee_status: str = "charging",
    easee_phase_mode: str = "three",
    ev_soc: float = 40.0,
    ev_target_soc: float = 90.0,
) -> SensorSnapshot:
    return SensorSnapshot(
        balancer_mode=balancer_mode,
        target_manual_a=target_manual_a,
        ev_max_physical_fuse_a=ev_max_physical_fuse_a,
        balancer_disabled=balancer_disabled,
        brain_target_ev_w=brain_target_ev_w,
        easee_status=easee_status,
        easee_phase_mode=easee_phase_mode,
        ev_soc=ev_soc,
        ev_target_soc=ev_target_soc,
        ev_min_start_soc=0.0,
        house_phase_a_l1=0.0,
        house_phase_a_l2=0.0,
        house_phase_a_l3=0.0,
        phase_count=3,
        line_voltage=230.0,
        house_main_fuse_a=25.0,
        ev_phase_headroom_a=2.0,
        min_a=6,
        max_a=16,
        cycle_s=5.0,
        min_dwell_s=60.0,
        pause_dwell_s=120.0,
        fault_cooldown_s=60.0,
        soft_fuse_recovery_cooldown_s=30.0,
        post_pause_cooldown_s=30.0,
    )


def _state(last_dynamic_a: int = 0, last_action: EvAction | None = None) -> EvBalancerState:
    s = EvBalancerState()
    s.last_dynamic_a = last_dynamic_a
    s.last_action = last_action
    return s


class TestManualFixed:
    def test_tc_b22_1_set_dynamic_when_target_differs(self) -> None:
        """TC-B22-1: target_a=10, last_a=0 → SET_DYNAMIC 10A."""
        snap = _snap(balancer_mode="MANUAL_FIXED", target_manual_a=10.0)
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.SET_DYNAMIC
        assert decision.dynamic_a == 10
        assert decision.status == EvBalancerStatus.OK
        assert "MANUAL_FIXED 10A" in decision.status_detail

    def test_tc_b22_2_pause_when_target_zero_and_charging(self) -> None:
        """TC-B22-2: target_a=0, last_a=8 (was charging) → PAUSE."""
        snap = _snap(balancer_mode="MANUAL_FIXED", target_manual_a=0.0)
        decision = translate_target_to_action(
            snap, _state(last_dynamic_a=8, last_action=EvAction.SET_DYNAMIC), FakeCooldowns()
        )
        assert decision.action == EvAction.PAUSE
        assert decision.dynamic_a == 0
        assert decision.status == EvBalancerStatus.PAUSED
        assert "target=0A" in decision.status_detail

    def test_tc_b22_3_hold_when_target_zero_already_stopped(self) -> None:
        """TC-B22-3: target_a=0, last_a=0 → HOLD PAUSED (no re-pause)."""
        snap = _snap(balancer_mode="MANUAL_FIXED", target_manual_a=0.0)
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.status == EvBalancerStatus.PAUSED
        assert "holding stopped" in decision.status_detail

    def test_tc_b22_4_hold_when_target_unchanged(self) -> None:
        """TC-B22-4: target_a=10, last_a=10 (no change) → HOLD OK."""
        snap = _snap(balancer_mode="MANUAL_FIXED", target_manual_a=10.0)
        decision = translate_target_to_action(
            snap, _state(last_dynamic_a=10, last_action=EvAction.SET_DYNAMIC), FakeCooldowns()
        )
        assert decision.action == EvAction.HOLD
        assert decision.status == EvBalancerStatus.OK
        assert "holding 10A" in decision.status_detail

    def test_tc_b22_5_clamps_target_at_physical_fuse(self) -> None:
        """TC-B22-5: target_a=40A > physical_fuse=32A → clamped to 32A → SET_DYNAMIC 32A."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED", target_manual_a=40.0, ev_max_physical_fuse_a=32.0
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.SET_DYNAMIC
        assert decision.dynamic_a == 32
        assert decision.status == EvBalancerStatus.OK

    def test_tc_b22_7_manual_fixed_fires_before_p1_disabled(self) -> None:
        """TC-B22-7: balancer_disabled=True + MANUAL_FIXED → P0 fires first, not P1."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED",
            target_manual_a=10.0,
            balancer_disabled=True,
            brain_target_ev_w=0.0,
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.SET_DYNAMIC
        assert decision.dynamic_a == 10


class TestShadowMode:
    def test_tc_b22_6_shadow_returns_hold_shadow_mode(self) -> None:
        """TC-B22-6: SHADOW mode → HOLD, status=SHADOW_MODE, no HW write."""
        snap = _snap(balancer_mode="SHADOW")
        decision = translate_target_to_action(snap, _state(), FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.status == EvBalancerStatus.SHADOW_MODE
        assert "SHADOW mode" in decision.status_detail
        assert decision.dynamic_a == 0

    def test_shadow_bypasses_cable_check(self) -> None:
        """SHADOW fires before P3 — disconnected cable doesn't prevent SHADOW response."""
        snap = _snap(balancer_mode="SHADOW", easee_status="disconnected")
        decision = translate_target_to_action(snap, _state(), FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.status == EvBalancerStatus.SHADOW_MODE

    def test_shadow_bypasses_soc_gate(self) -> None:
        """SHADOW fires before P4 — SoC reached doesn't prevent SHADOW response."""
        snap = _snap(balancer_mode="SHADOW", ev_soc=95.0, ev_target_soc=80.0)
        decision = translate_target_to_action(snap, _state(), FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.status == EvBalancerStatus.SHADOW_MODE


class TestEvBalancerManualInvariant4:
    """ev_balancer Invariant #4: MANUAL_FIXED = operator äger laddaren.
    Brain-target 0W ska INTE stänga av laddaren om operator satt target > 0A.
    """

    def test_manual_fixed_ignores_brain_zero_target(self) -> None:
        """Brain target_ev_w=0 + MANUAL_FIXED target=10A → SET_DYNAMIC 10A (INTE PAUSE)."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED",
            target_manual_a=10.0,
            brain_target_ev_w=0.0,
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert (
            decision.action == EvAction.SET_DYNAMIC
        ), f"Brain=0W ska inte stänga laddaren i MANUAL_FIXED: fick {decision.action}"
        assert decision.dynamic_a == 10

    def test_manual_fixed_ignores_brain_negative_target(self) -> None:
        """Brain target_ev_w=-500 (urladdning) + MANUAL_FIXED target=8A → SET_DYNAMIC 8A."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED",
            target_manual_a=8.0,
            brain_target_ev_w=-500.0,
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.SET_DYNAMIC
        assert decision.dynamic_a == 8

    def test_manual_fixed_target_zero_pauses_on_operator_intent(self) -> None:
        """Operator sätter target=0A i MANUAL_FIXED → PAUSE (operatörens intention att stoppa)."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED",
            target_manual_a=0.0,
            brain_target_ev_w=3000.0,
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=10), FakeCooldowns())
        assert (
            decision.action == EvAction.PAUSE
        ), "Operator satte 0A = intention att stoppa → ska PAUSE"

    def test_manual_fixed_not_blocked_by_balancer_disabled(self) -> None:
        """MANUAL_FIXED kör trots balancer_disabled=True — P0 före P1."""
        snap = _snap(
            balancer_mode="MANUAL_FIXED",
            target_manual_a=10.0,
            brain_target_ev_w=0.0,
            balancer_disabled=True,
        )
        decision = translate_target_to_action(snap, _state(last_dynamic_a=0), FakeCooldowns())
        assert decision.action == EvAction.SET_DYNAMIC
        assert decision.dynamic_a == 10
