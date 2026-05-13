"""Binary balancer translator — pure function, no HA imports."""

from __future__ import annotations

import json

from .const import BRAIN_STALE_THRESHOLD_S
from .models import (
    ActionMessage,
    AssetConfig,
    BalancerState,
    FaultState,
    SensorSnapshot,
    SwitchAction,
    TranslateResult,
)

VALID_MODES: frozenset[str] = frozenset({"on", "off"})
MIN_DEADLINE_S: int = 60


def translate(
    action: ActionMessage | None,
    config: AssetConfig,
    sensors: SensorSnapshot,
    state: BalancerState,
    now_ts: float,
) -> TranslateResult:
    """Translate Brain ACTION into switch action.

    Pure function: no side effects, no HA imports, deterministic.

    Decision order:
    1. Guardian SAFE_STATE → force off (no dwell bypass for safety)
    2. action is None → HOLD with OFFLINE
    3. Stale action (age > deadline_s) → HOLD
    4. mode == "off" → TURN_OFF (with dwell)
    5. season_active == False → TURN_OFF (with dwell)
    6. switch_unavailable → HOLD with FAULT
    7. Hysteresis decision
    8. No change needed → HOLD
    9. Dwell check → HOLD if too recent
    10. Execute TURN_ON or TURN_OFF
    """
    # 1. Guardian safe state — immediate, no dwell
    if sensors.guardian_safe_state:
        return TranslateResult(
            action=SwitchAction.TURN_OFF,
            reason="SAFE_STATE_ACTIVE",
            fault_state=FaultState.SAFE_STATE_ACTIVE,
            fault_detail="Guardian triggered safe state",
            actual_w=0.0,
            rejected_w=config.typical_drag_w if state.switch_on else 0.0,
            rejected_reason="safe_state",
            stale_action=False,
            cap_engage=False,
        )

    # 2. No action (Brain silent) — FAIL SAFE: off (invariant requires explicit ON)
    if action is None:
        return _force_off(
            config=config,
            state=state,
            now_ts=now_ts,
            reason="no_action_brain_silent",
            rejected_reason="brain_silent",
            rejected_w=config.typical_drag_w if state.switch_on else 0.0,
        )

    # 3. Stale action — FAIL SAFE: off. Universal 60s cap regardless of action.deadline_s.
    stale = sensors.action_age_s > min(action.deadline_s, BRAIN_STALE_THRESHOLD_S)
    if stale:
        return _force_off(
            config=config,
            state=state,
            now_ts=now_ts,
            reason=f"stale_action_age={sensors.action_age_s:.0f}s",
            rejected_reason="stale_action",
            rejected_w=config.typical_drag_w if state.switch_on else 0.0,
            stale_action=True,
        )

    # 4. mode == "off" — Brain explicitly says off
    if action.mode == "off":
        return _force_off(
            config=config,
            state=state,
            now_ts=now_ts,
            reason="mode_off",
            rejected_reason="brain_off",
            rejected_w=config.typical_drag_w if state.switch_on else 0.0,
        )

    # 5. Season gate
    if not sensors.season_active:
        return _force_off(
            config=config,
            state=state,
            now_ts=now_ts,
            reason="season_inactive",
            rejected_reason="season_inactive",
            rejected_w=action.target_w if state.switch_on else 0.0,
        )

    # 6. Switch unavailable → FAULT
    if sensors.switch_unavailable:
        return TranslateResult(
            action=SwitchAction.HOLD,
            reason="switch_unavailable",
            fault_state=FaultState.FAULT,
            fault_detail="Switch entity unavailable",
            actual_w=0.0,
            rejected_w=action.target_w,
            rejected_reason="switch_unavailable",
            stale_action=False,
            cap_engage=False,
        )

    # 7. Hysteresis
    target_w = action.target_w
    desired_on = _hysteresis(
        target_w=target_w,
        currently_on=state.switch_on,
        on_threshold=config.on_threshold_w,
        off_threshold=config.off_threshold_w,
    )

    # 8. No change
    if desired_on == state.switch_on:
        actual_w = config.typical_drag_w if state.switch_on else 0.0
        rejected_w = 0.0 if state.switch_on else target_w
        rejected_reason = "" if state.switch_on else "below_on_threshold"
        fault = (
            FaultState.OK
            if state.switch_on
            else (FaultState.PARTIAL if target_w > 0 else FaultState.OK)
        )
        return TranslateResult(
            action=SwitchAction.HOLD,
            reason="no_change",
            fault_state=fault,
            fault_detail="",
            actual_w=actual_w,
            rejected_w=rejected_w,
            rejected_reason=rejected_reason,
            stale_action=False,
            cap_engage=True,
        )

    # 9. Dwell check
    elapsed = now_ts - state.last_change_ts
    dwell_remaining = config.min_dwell_s - elapsed
    if dwell_remaining > 0:
        actual_w = config.typical_drag_w if state.switch_on else 0.0
        return TranslateResult(
            action=SwitchAction.HOLD,
            reason=f"min_dwell:{int(dwell_remaining)}s",
            fault_state=FaultState.OK,
            fault_detail="",
            actual_w=actual_w,
            rejected_w=0.0,
            rejected_reason="",
            stale_action=False,
            cap_engage=True,
        )

    # 10. Execute change
    if desired_on:
        extra_w = max(0.0, target_w - config.typical_drag_w)
        return TranslateResult(
            action=SwitchAction.TURN_ON,
            reason=f"target_w:{target_w:.0f}>=on_threshold:{config.on_threshold_w:.0f}",
            fault_state=FaultState.OK,
            fault_detail="",
            actual_w=config.typical_drag_w,
            rejected_w=extra_w,
            rejected_reason="binary_excess" if extra_w > 0 else "",
            stale_action=False,
            cap_engage=True,
        )
    else:
        return TranslateResult(
            action=SwitchAction.TURN_OFF,
            reason=f"target_w:{target_w:.0f}<off_threshold:{config.off_threshold_w:.0f}",
            fault_state=FaultState.PARTIAL,
            fault_detail="",
            actual_w=0.0,
            rejected_w=target_w,
            rejected_reason="below_off_threshold",
            stale_action=False,
            cap_engage=True,
        )


def _hysteresis(
    target_w: float,
    currently_on: bool,
    on_threshold: float,
    off_threshold: float,
) -> bool:
    """Hysteresis: ON at on_threshold%, OFF below off_threshold% of typical_drag_w."""
    if currently_on:
        return target_w >= off_threshold
    return target_w >= on_threshold


def _force_off(
    config: AssetConfig,
    state: BalancerState,
    now_ts: float,
    reason: str,
    rejected_reason: str,
    rejected_w: float,
    stale_action: bool = False,
) -> TranslateResult:
    """Build a TURN_OFF result, respecting min_dwell if device is currently on."""
    if state.switch_on:
        elapsed = now_ts - state.last_change_ts
        dwell_remaining = config.min_dwell_s - elapsed
        if dwell_remaining > 0:
            return TranslateResult(
                action=SwitchAction.HOLD,
                reason=f"min_dwell:{int(dwell_remaining)}s",
                fault_state=FaultState.OK,
                fault_detail="",
                actual_w=config.typical_drag_w,
                rejected_w=0.0,
                rejected_reason="",
                stale_action=stale_action,
                cap_engage=False,
            )
    return TranslateResult(
        action=SwitchAction.TURN_OFF,
        reason=reason,
        fault_state=FaultState.OK,
        fault_detail="",
        actual_w=0.0,
        rejected_w=rejected_w,
        rejected_reason=rejected_reason,
        stale_action=stale_action,
        cap_engage=False,
    )


def parse_action_message(json_str: str, asset_id: str) -> ActionMessage | None:
    """Parse ACTION message JSON. Returns None on any parse/validation error."""
    try:
        d: dict[str, object] = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        msg = ActionMessage.from_dict(d)
    except (KeyError, ValueError, TypeError):
        return None
    if msg.asset_id != asset_id:
        return None
    if msg.mode not in VALID_MODES:
        return None
    if msg.deadline_s < MIN_DEADLINE_S:
        return None
    return msg
