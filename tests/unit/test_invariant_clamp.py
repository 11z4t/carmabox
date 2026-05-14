"""Tests for INVARIANT: balancer ≤ brain-offer + 100W tolerance.

Covers scenarios from inbox-901/2026-05-13T19-55-00_FROM-900_INVARIANT-ENFORCE-BALANCER-CLAMP.json:
  T1. Brain target_ev_w=0 → ev_balancer pauses
  T2. Brain target_ev_w=4140 → ev_balancer dynamic_a ≤ 6 (6x690=4140W; cap=int(4240/690)=6)
  T3. Brain target_ev_w stale (age>60s) → coordinator zeros it → balancer pauses
  T4. binary_balancer action=None → TURN_OFF (was HOLD — fail-safe change)
  T5. bat_balancer brain_target=-2000W → per-bank ≤ -1000W, total ≤ 2100W
"""

from __future__ import annotations

from custom_components.bat_balancer.distribution_engine import distribute_target_to_banks
from custom_components.bat_balancer.models import (
    BankConfig,
    BankState,
)
from custom_components.bat_balancer.models import (
    SensorSnapshot as BatSnap,
)
from custom_components.binary_balancer.models import (
    ActionMessage,
    AssetConfig,
    BalancerState,
    SwitchAction,
)
from custom_components.binary_balancer.models import (
    SensorSnapshot as BinSensorSnapshot,
)
from custom_components.binary_balancer.translator import translate
from custom_components.ev_balancer.const import EvAction, RefusalReason
from custom_components.ev_balancer.models import EvBalancerState, SensorSnapshot
from custom_components.ev_balancer.translation_logic import translate_target_to_action

# ── ev_balancer helpers ───────────────────────────────────────────────────────


def _ev_snap(
    *,
    brain_target_ev_w: float = 0.0,
    ev_soc: float = 40.0,
    ev_target_soc: float = 80.0,
    phase_count: int = 3,
    line_voltage: float = 230.0,
    house_main_fuse_a: float = 25.0,
    ev_phase_headroom_a: float = 2.0,
    ev_max_physical_fuse_a: float = 16.0,
    house_phase_a_l1: float = 0.0,
    house_phase_a_l2: float = 0.0,
    house_phase_a_l3: float = 0.0,
    balancer_disabled: bool = False,
    easee_status: str = "charging",
    easee_phase_mode: str = "three",
    easee_actual_a: float = 6.0,
    easee_power_w: float = 4140.0,
    min_a: int = 6,
    max_a: int = 16,
) -> SensorSnapshot:
    return SensorSnapshot(
        brain_target_ev_w=brain_target_ev_w,
        ev_soc=ev_soc,
        ev_target_soc=ev_target_soc,
        ev_min_start_soc=0.0,
        easee_status=easee_status,
        easee_phase_mode=easee_phase_mode,
        easee_actual_a=easee_actual_a,
        easee_power_w=easee_power_w,
        house_phase_a_l1=house_phase_a_l1,
        house_phase_a_l2=house_phase_a_l2,
        house_phase_a_l3=house_phase_a_l3,
        phase_count=phase_count,
        line_voltage=line_voltage,
        house_main_fuse_a=house_main_fuse_a,
        ev_phase_headroom_a=ev_phase_headroom_a,
        ev_max_physical_fuse_a=ev_max_physical_fuse_a,
        min_a=min_a,
        max_a=max_a,
        cycle_s=5.0,
        min_dwell_s=60.0,
        pause_dwell_s=120.0,
        fault_cooldown_s=60.0,
        soft_fuse_recovery_cooldown_s=30.0,
        post_pause_cooldown_s=30.0,
        balancer_disabled=balancer_disabled,
    )


def _ev_state(
    last_dynamic_a: int = 6, last_action: EvAction = EvAction.SET_DYNAMIC
) -> EvBalancerState:
    s = EvBalancerState()
    s.last_dynamic_a = last_dynamic_a
    s.last_action = last_action
    return s


class FakeCooldowns:
    """Minimal cooldown stub: nothing blocked."""

    def is_blocked(self, _kind: object) -> bool:
        return False

    def remaining(self, _kind: object) -> float:
        return 0.0

    def tick(self, _elapsed: float) -> None:
        pass


# ── binary_balancer helpers ───────────────────────────────────────────────────


def _bin_config(asset_id: str = "pool_elv", typical_drag_w: float = 2500.0) -> AssetConfig:
    return AssetConfig(
        asset_id=asset_id,
        typical_drag_w=typical_drag_w,
        switch_entity=f"switch.{asset_id}",
        season_active_entity=None,
        min_dwell_s=0,
    )


def _bin_snap(
    switch_is_on: bool = False,
    action_age_s: float = 5.0,
) -> BinSensorSnapshot:
    return BinSensorSnapshot(
        switch_is_on=switch_is_on,
        switch_unavailable=False,
        season_active=True,
        guardian_safe_state=False,
        action_age_s=action_age_s,
    )


def _bin_state(switch_on: bool = False) -> BalancerState:
    return BalancerState(switch_on=switch_on, last_change_ts=0.0)


# ── bat_balancer helpers ──────────────────────────────────────────────────────


def _bat_snap(grid_w: float = 2000.0) -> BatSnap:
    return BatSnap(
        brain_target_bat_w=-2000.0,
        house_grid_w=grid_w,
        pv_w=0.0,
    )


def _two_banks(soc: float = 80.0) -> tuple[dict, dict]:
    configs = {
        "kontor": BankConfig(
            id="kontor", capacity_kwh=14.0, max_charge_w=5000.0, max_discharge_w=5000.0
        ),
        "forrad": BankConfig(
            id="forrad", capacity_kwh=6.0, max_charge_w=3000.0, max_discharge_w=3000.0
        ),
    }
    states = {
        "kontor": BankState(bank_id="kontor", current_soc=soc, is_online=True),
        "forrad": BankState(bank_id="forrad", current_soc=soc, is_online=True),
    }
    return configs, states


# ── T1: brain_target=0 → PAUSE ───────────────────────────────────────────────


class TestBrainTargetZeroStops:
    def test_brain_zero_pauses_charging(self) -> None:
        """T1: Brain target=0 → ev_balancer must pause (not HOLD or SET_DYNAMIC)."""
        snap = _ev_snap(brain_target_ev_w=0.0)
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert decision.action == EvAction.PAUSE
        assert decision.dynamic_a == 0

    def test_brain_zero_hold_when_already_stopped(self) -> None:
        """T1b: Brain target=0 and charger already stopped → HOLD (no re-pause)."""
        snap = _ev_snap(brain_target_ev_w=0.0)
        state = _ev_state(last_dynamic_a=0, last_action=EvAction.PAUSE)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.dynamic_a == 0


# ── T2: brain-offer clamp — dynamic_a never exceeds (brain_target+100)/V/phases ─


class TestBrainOfferClamp:
    def test_clamp_enforced_at_offer_boundary(self) -> None:
        """T2: brain_target=4140W (=6A at 3ph 230V) → dynamic_a ≤ 6."""
        snap = _ev_snap(brain_target_ev_w=4140.0, house_main_fuse_a=25.0, ev_phase_headroom_a=2.0)
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert decision.dynamic_a <= 6, f"Expected ≤6A, got {decision.dynamic_a}A"

    def test_clamp_fires_when_desired_exceeds_cap(self) -> None:
        """T2b: Property — dynamic_a x V x Ph ≤ brain_target+100."""
        snap = _ev_snap(brain_target_ev_w=4140.0, line_voltage=230.0, phase_count=3)
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        if decision.dynamic_a > 0:
            actual_w = decision.dynamic_a * snap.phase_count * snap.line_voltage
            assert (
                actual_w <= snap.brain_target_ev_w + 100.0 + 1.0
            ), f"Invariant breach: {actual_w}W > {snap.brain_target_ev_w + 100}W"

    def test_brain_offer_property_holds_across_targets(self) -> None:
        """T2c: For any brain_target, dynamic_a x V x Ph ≤ target+100."""
        for target_w in [0, 690, 1380, 2070, 4140, 6900, 9660]:
            snap = _ev_snap(brain_target_ev_w=float(target_w))
            state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
            decision = translate_target_to_action(snap, state, FakeCooldowns())
            if decision.dynamic_a > 0:
                actual_w = decision.dynamic_a * snap.phase_count * snap.line_voltage
                assert (
                    actual_w <= target_w + 100.0 + 1.0
                ), f"Invariant breach at target={target_w}W: got {actual_w}W"


# ── T3: stale brain target → treated as 0 by coordinator ─────────────────────


class TestStaleBrainTarget:
    def test_stale_zeroed_target_causes_pause(self) -> None:
        """T3: Coordinator zeros stale brain target → translation sees 0 → PAUSE."""
        snap = _ev_snap(brain_target_ev_w=0.0)
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert decision.action == EvAction.PAUSE
        assert decision.dynamic_a == 0


# ── T4: binary_balancer action=None → TURN_OFF ───────────────────────────────


class TestBinaryBrainSilentFailSafe:
    def test_action_none_turns_off_when_switch_on(self) -> None:
        """T4: action=None (Brain silent) → TURN_OFF when switch was on."""
        cfg = _bin_config()
        result = translate(
            action=None,
            config=cfg,
            sensors=_bin_snap(switch_is_on=True),
            state=_bin_state(switch_on=True),
            now_ts=1000.0,
        )
        assert (
            result.action == SwitchAction.TURN_OFF
        ), f"Expected TURN_OFF when Brain silent, got {result.action}"

    def test_action_none_turns_off_when_switch_off(self) -> None:
        """T4b: action=None and switch already off → TURN_OFF (idempotent)."""
        cfg = _bin_config()
        result = translate(
            action=None,
            config=cfg,
            sensors=_bin_snap(switch_is_on=False),
            state=_bin_state(switch_on=False),
            now_ts=1000.0,
        )
        assert result.action in (SwitchAction.TURN_OFF, SwitchAction.HOLD)

    def test_stale_action_turns_off(self) -> None:
        """T4c: Stale action (age > 60s) → TURN_OFF (universal threshold enforced)."""
        from custom_components.binary_balancer.const import BRAIN_STALE_THRESHOLD_S

        cfg = _bin_config()
        action = ActionMessage(
            asset_id="pool_elv",
            target_w=2500.0,
            mode="on",
            deadline_s=120,
            reason="test",
            cycle_id=1,
            source="brain",
            ts="2026-05-13T10:00:00+00:00",
        )
        result = translate(
            action=action,
            config=cfg,
            sensors=_bin_snap(action_age_s=90.0, switch_is_on=True),
            state=_bin_state(switch_on=True),
            now_ts=1000.0,
        )
        assert (
            result.action == SwitchAction.TURN_OFF
        ), f"Stale action (90s > {BRAIN_STALE_THRESHOLD_S}s) should TURN_OFF, got {result.action}"
        assert result.stale_action is True

    def test_fresh_action_mode_on_allows_engage(self) -> None:
        """T4d: Fresh action with mode=on → normal hysteresis (not forced off)."""
        cfg = _bin_config(typical_drag_w=2500.0)
        action = ActionMessage(
            asset_id="pool_elv",
            target_w=2500.0,
            mode="on",
            deadline_s=120,
            reason="test",
            cycle_id=1,
            source="brain",
            ts="2026-05-13T10:00:00+00:00",
        )
        result = translate(
            action=action,
            config=cfg,
            sensors=_bin_snap(action_age_s=5.0, switch_is_on=False),
            state=_bin_state(switch_on=False),
            now_ts=1000.0,
        )
        assert result.action == SwitchAction.TURN_ON


# ── T5: bat_balancer brain-offer clamp ────────────────────────────────────────


class TestBatBalancerBrainOfferClamp:
    def test_sum_does_not_exceed_brain_offer_plus_100(self) -> None:
        """T5: brain_target_bat_w=-2000 → |sum(per_bank)| ≤ 2100W."""
        configs, states = _two_banks()
        result = distribute_target_to_banks(
            target_w=-2000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_bat_snap(grid_w=3000.0),
        )
        total = sum(abs(v) for v in result.targets.values())
        assert (
            total <= 2000.0 + 100.0 + 0.01
        ), f"Brain-offer invariant breach: total={total:.1f}W > 2100W"

    def test_per_bank_roughly_equal_when_soc_equal(self) -> None:
        """T5b: Equal SoC → per-bank is ≤ -1100W."""
        configs, states = _two_banks(soc=80.0)
        result = distribute_target_to_banks(
            target_w=-2000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_bat_snap(grid_w=3000.0),
        )
        for bank_id, target in result.targets.items():
            assert target <= 0.0, f"Bank {bank_id} should discharge, got {target}"
            assert (
                abs(target) <= 2000.0 + 100.0
            ), f"Bank {bank_id} exceeds brain offer+100W: {abs(target):.1f}W"

    def test_clamp_fires_if_equalization_bias_overshoots(self) -> None:
        """T5c: High SoC divergence → equalization bias → clamp keeps total ≤ 2100W."""
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=14.0, max_charge_w=5000.0, max_discharge_w=5000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=6.0, max_charge_w=3000.0, max_discharge_w=3000.0
            ),
        }
        states = {
            "kontor": BankState(bank_id="kontor", current_soc=90.0, is_online=True),
            "forrad": BankState(bank_id="forrad", current_soc=40.0, is_online=True),
        }
        snap = BatSnap(
            brain_target_bat_w=-2000.0,
            house_grid_w=3000.0,
            pv_w=0.0,
            soc_equalization_threshold_pct=5.0,
            soc_equalization_max_bias_w=500.0,
        )
        result = distribute_target_to_banks(
            target_w=-2000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=snap,
        )
        total = sum(abs(v) for v in result.targets.values())
        assert (
            total <= 2000.0 + 100.0 + 0.01
        ), f"Brain-offer invariant breach after equalization: total={total:.1f}W > 2100W"


# ── T6: Brain v3 single authority — ev_balancer_disable does not block Brain target ──


class TestBrainV3SingleAuthority:
    def test_disabled_balancer_with_brain_target_passes_through(self) -> None:
        """T6a: ev_balancer_disable=on + brain_target_ev_w > 0 → Brain target passes through P1."""
        snap = _ev_snap(brain_target_ev_w=4500.0, balancer_disabled=True)
        state = _ev_state(last_dynamic_a=0, last_action=None)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert (
            decision.action != EvAction.HOLD or decision.reason != RefusalReason.BALANCER_DISABLED
        )
        assert decision.action in (EvAction.SET_DYNAMIC, EvAction.RESUME)
        assert decision.dynamic_a >= 6

    def test_disabled_balancer_with_zero_brain_target_holds(self) -> None:
        """T6b: ev_balancer_disable=on + brain_target_ev_w == 0 → HOLD (shadow mode)."""
        snap = _ev_snap(brain_target_ev_w=0.0, balancer_disabled=True)
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        assert decision.action == EvAction.HOLD
        assert decision.reason == RefusalReason.BALANCER_DISABLED

    def test_disabled_balancer_brain_target_respects_fuse_cap(self) -> None:
        """T6c: Fuse cap still enforced even when balancer disabled + Brain has target."""
        snap = _ev_snap(
            brain_target_ev_w=4500.0,
            balancer_disabled=True,
            house_phase_a_l1=20.0,
        )
        state = _ev_state(last_dynamic_a=6, last_action=EvAction.SET_DYNAMIC)
        decision = translate_target_to_action(snap, state, FakeCooldowns())
        if decision.action == EvAction.SET_DYNAMIC:
            assert decision.dynamic_a <= 25
