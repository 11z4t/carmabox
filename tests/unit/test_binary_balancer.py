"""Tests for binary_balancer — Typ D binary on/off asset balancer.

Coverage:
- models: AssetConfig, ActionMessage, FeedbackMessage, BalancerState,
          SensorSnapshot, TranslateResult, FaultState, SwitchAction
- translator: translate(), parse_action_message(), _hysteresis(), _force_off()
- All 3 assets (pool_vp, pool_elv, miner)
- All FaultState and SwitchAction enum values
- Hysteresis edge cases + boundary conditions
- min_dwell_s enforcement
- Guardian safe-state (bypasses dwell)
- season_active = False
- mode == "off"
- stale action
- No action (None)
- switch_unavailable
- JSONL log format
- FeedbackMessage compact dict / JSON ≤255 chars
- ActionMessage.from_dict defaults
- TranslateResult immutability
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from custom_components.binary_balancer.const import (
    ASSET_MINER,
    ASSET_POOL_ELV,
    ASSET_POOL_VP,
    DEFAULT_HYSTERESIS_OFF_PCT,
    DEFAULT_HYSTERESIS_ON_PCT,
    DEFAULT_MIN_DWELL_S,
    DEFAULT_TYPICAL_DRAG_W,
    VALID_ASSET_IDS,
)
from custom_components.binary_balancer.models import (
    ActionMessage,
    AssetConfig,
    BalancerState,
    FaultState,
    FeedbackMessage,
    SensorSnapshot,
    SwitchAction,
    TranslateResult,
)
from custom_components.binary_balancer.translator import (
    MIN_DEADLINE_S,
    VALID_MODES,
    _hysteresis,
    parse_action_message,
    translate,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW_TS: float = 1_000_000.0  # stable monotonic reference


def _cfg(
    asset_id: str = ASSET_POOL_VP,
    typical_drag_w: float = 2500.0,
    switch_entity: str = "switch.pool_vp",
    season_active_entity: str | None = None,
    min_dwell_s: int = 60,
    hysteresis_on_pct: float = 80.0,
    hysteresis_off_pct: float = 70.0,
) -> AssetConfig:
    return AssetConfig(
        asset_id=asset_id,
        typical_drag_w=typical_drag_w,
        switch_entity=switch_entity,
        season_active_entity=season_active_entity,
        min_dwell_s=min_dwell_s,
        hysteresis_on_pct=hysteresis_on_pct,
        hysteresis_off_pct=hysteresis_off_pct,
    )


def _snap(
    switch_is_on: bool = False,
    switch_unavailable: bool = False,
    season_active: bool = True,
    guardian_safe_state: bool = False,
    action_age_s: float = 5.0,
) -> SensorSnapshot:
    return SensorSnapshot(
        switch_is_on=switch_is_on,
        switch_unavailable=switch_unavailable,
        season_active=season_active,
        guardian_safe_state=guardian_safe_state,
        action_age_s=action_age_s,
    )


def _state(
    switch_on: bool = False,
    last_change_ts: float = 0.0,
    last_cycle_id: int = -1,
    offline_cycles: int = 0,
    uptime_s: float = 0.0,
    stale_cycles: int = 0,
) -> BalancerState:
    return BalancerState(
        switch_on=switch_on,
        last_change_ts=last_change_ts,
        last_cycle_id=last_cycle_id,
        offline_cycles=offline_cycles,
        uptime_s=uptime_s,
        stale_cycles=stale_cycles,
    )


def _action(
    asset_id: str = ASSET_POOL_VP,
    target_w: float = 2000.0,
    mode: str = "on",
    deadline_s: int = 120,
    reason: str = "test",
    cycle_id: int = 1,
    source: str = "brain",
    ts: str = "2026-05-09T10:00:00+00:00",
) -> ActionMessage:
    return ActionMessage(
        asset_id=asset_id,
        target_w=target_w,
        mode=mode,
        deadline_s=deadline_s,
        reason=reason,
        cycle_id=cycle_id,
        source=source,
        ts=ts,
    )


def _feedback(
    asset_id: str = ASSET_POOL_VP,
    actual_w: float = 2500.0,
    fault_state: str = "OK",
    fault_detail: str = "",
    cap_engage: bool = True,
    cap_max_w: float = 2500.0,
    cap_min_w: float = 0.0,
    rejected_w: float = 0.0,
    rejected_reason: str = "",
    stale_action: bool = False,
    ts: str = "2026-05-09T10:00:00+00:00",
) -> FeedbackMessage:
    return FeedbackMessage(
        asset_id=asset_id,
        actual_w=actual_w,
        fault_state=fault_state,
        fault_detail=fault_detail,
        cap_engage=cap_engage,
        cap_max_w=cap_max_w,
        cap_min_w=cap_min_w,
        rejected_w=rejected_w,
        rejected_reason=rejected_reason,
        stale_action=stale_action,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# 1. Constants
# ---------------------------------------------------------------------------


class TestConst:
    def test_valid_asset_ids(self) -> None:
        assert frozenset({"pool_vp", "pool_elv", "miner"}) == VALID_ASSET_IDS

    def test_default_typical_drag_pool_vp(self) -> None:
        assert DEFAULT_TYPICAL_DRAG_W[ASSET_POOL_VP] == 2500.0

    def test_default_typical_drag_pool_elv(self) -> None:
        assert DEFAULT_TYPICAL_DRAG_W[ASSET_POOL_ELV] == 3000.0

    def test_default_typical_drag_miner(self) -> None:
        assert DEFAULT_TYPICAL_DRAG_W[ASSET_MINER] == 1200.0

    def test_default_hysteresis_on_pct(self) -> None:
        assert DEFAULT_HYSTERESIS_ON_PCT == 80.0

    def test_default_hysteresis_off_pct(self) -> None:
        assert DEFAULT_HYSTERESIS_OFF_PCT == 70.0

    def test_default_min_dwell_s(self) -> None:
        assert DEFAULT_MIN_DWELL_S == 60

    def test_min_deadline_s(self) -> None:
        assert MIN_DEADLINE_S == 60

    def test_valid_modes(self) -> None:
        assert frozenset({"on", "off"}) == VALID_MODES


# ---------------------------------------------------------------------------
# 2. Enums
# ---------------------------------------------------------------------------


class TestFaultStateEnum:
    def test_ok(self) -> None:
        assert FaultState.OK == "OK"

    def test_partial(self) -> None:
        assert FaultState.PARTIAL == "PARTIAL"

    def test_fault(self) -> None:
        assert FaultState.FAULT == "FAULT"

    def test_offline(self) -> None:
        assert FaultState.OFFLINE == "OFFLINE"

    def test_safe_state_active(self) -> None:
        assert FaultState.SAFE_STATE_ACTIVE == "SAFE_STATE_ACTIVE"

    def test_all_values(self) -> None:
        values = {f.value for f in FaultState}
        assert values == {"OK", "PARTIAL", "FAULT", "OFFLINE", "SAFE_STATE_ACTIVE"}


class TestSwitchActionEnum:
    def test_turn_on(self) -> None:
        assert SwitchAction.TURN_ON == "turn_on"

    def test_turn_off(self) -> None:
        assert SwitchAction.TURN_OFF == "turn_off"

    def test_hold(self) -> None:
        assert SwitchAction.HOLD == "hold"

    def test_all_values(self) -> None:
        values = {a.value for a in SwitchAction}
        assert values == {"turn_on", "turn_off", "hold"}


# ---------------------------------------------------------------------------
# 3. AssetConfig — thresholds
# ---------------------------------------------------------------------------


class TestAssetConfigThresholds:
    def test_pool_vp_on_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        assert cfg.on_threshold_w == pytest.approx(2000.0)

    def test_pool_vp_off_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        assert cfg.off_threshold_w == pytest.approx(1750.0)

    def test_pool_elv_on_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_POOL_ELV, typical_drag_w=3000.0, hysteresis_on_pct=80.0)
        assert cfg.on_threshold_w == pytest.approx(2400.0)

    def test_pool_elv_off_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_POOL_ELV, typical_drag_w=3000.0, hysteresis_off_pct=70.0)
        assert cfg.off_threshold_w == pytest.approx(2100.0)

    def test_miner_on_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_MINER, typical_drag_w=1200.0, hysteresis_on_pct=80.0)
        assert cfg.on_threshold_w == pytest.approx(960.0)

    def test_miner_off_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_MINER, typical_drag_w=1200.0, hysteresis_off_pct=70.0)
        assert cfg.off_threshold_w == pytest.approx(840.0)

    def test_custom_pct(self) -> None:
        cfg = _cfg(typical_drag_w=1000.0, hysteresis_on_pct=90.0, hysteresis_off_pct=60.0)
        assert cfg.on_threshold_w == pytest.approx(900.0)
        assert cfg.off_threshold_w == pytest.approx(600.0)

    def test_frozen_immutable(self) -> None:
        cfg = _cfg()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            cfg.typical_drag_w = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. ActionMessage.from_dict
# ---------------------------------------------------------------------------


class TestActionMessageFromDict:
    def test_full_dict(self) -> None:
        d = {
            "a": "pool_vp",
            "w": 2000.0,
            "m": "on",
            "dl": 120,
            "r": "cheap",
            "c": 5,
            "s": "brain",
            "t": "2026-05-09T10:00:00+00:00",
        }
        msg = ActionMessage.from_dict(d)
        assert msg.asset_id == "pool_vp"
        assert msg.target_w == pytest.approx(2000.0)
        assert msg.mode == "on"
        assert msg.deadline_s == 120
        assert msg.reason == "cheap"
        assert msg.cycle_id == 5
        assert msg.source == "brain"

    def test_defaults_applied(self) -> None:
        d = {"a": "miner", "m": "off", "c": 1}
        msg = ActionMessage.from_dict(d)
        assert msg.target_w == pytest.approx(0.0)
        assert msg.deadline_s == 120
        assert msg.reason == ""
        assert msg.source == "unknown"
        assert msg.ts == ""

    def test_missing_required_key_raises(self) -> None:
        with pytest.raises((KeyError, TypeError)):
            ActionMessage.from_dict({"m": "on", "c": 1})  # missing "a"

    def test_mode_off(self) -> None:
        msg = ActionMessage.from_dict({"a": "miner", "m": "off", "c": 1})
        assert msg.mode == "off"

    def test_frozen_immutable(self) -> None:
        msg = _action()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            msg.mode = "off"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. FeedbackMessage.to_compact_dict / to_json
# ---------------------------------------------------------------------------


class TestFeedbackMessage:
    def test_compact_dict_keys(self) -> None:
        fb = _feedback()
        d = fb.to_compact_dict()
        assert set(d.keys()) == {"a", "w", "fs", "fd", "ce", "mx", "mn", "rw", "rr", "st", "t"}

    def test_compact_dict_values(self) -> None:
        fb = _feedback(asset_id=ASSET_MINER, actual_w=1200.0, fault_state="OK")
        d = fb.to_compact_dict()
        assert d["a"] == ASSET_MINER
        assert d["w"] == pytest.approx(1200.0)
        assert d["fs"] == "OK"

    def test_to_json_length_lte_255(self) -> None:
        fb = _feedback()
        json_str = fb.to_json()
        assert len(json_str) <= 255

    def test_fault_detail_truncated_to_40(self) -> None:
        long_detail = "x" * 100
        fb = _feedback(fault_detail=long_detail)
        d = fb.to_compact_dict()
        assert len(d["fd"]) == 40  # type: ignore[arg-type]

    def test_rejected_reason_truncated_to_30(self) -> None:
        long_reason = "y" * 60
        fb = _feedback(rejected_reason=long_reason)
        d = fb.to_compact_dict()
        assert len(d["rr"]) == 30  # type: ignore[arg-type]

    def test_to_json_valid_json(self) -> None:
        fb = _feedback()
        parsed = json.loads(fb.to_json())
        assert isinstance(parsed, dict)

    def test_all_assets_feedback_json_length(self) -> None:
        for asset_id in [ASSET_POOL_VP, ASSET_POOL_ELV, ASSET_MINER]:
            fb = _feedback(asset_id=asset_id)
            assert len(fb.to_json()) <= 255

    def test_stale_action_true(self) -> None:
        fb = _feedback(stale_action=True)
        d = fb.to_compact_dict()
        assert d["st"] is True

    def test_cap_engage_false(self) -> None:
        fb = _feedback(cap_engage=False)
        d = fb.to_compact_dict()
        assert d["ce"] is False


# ---------------------------------------------------------------------------
# 6. TranslateResult immutability
# ---------------------------------------------------------------------------


class TestTranslateResultImmutable:
    def test_frozen(self) -> None:
        res = TranslateResult(
            action=SwitchAction.HOLD,
            reason="test",
            fault_state=FaultState.OK,
            fault_detail="",
            actual_w=0.0,
            rejected_w=0.0,
            rejected_reason="",
            stale_action=False,
            cap_engage=False,
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            res.action = SwitchAction.TURN_ON  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. _hysteresis() pure function
# ---------------------------------------------------------------------------


class TestHysteresisFunction:
    def test_off_below_on_threshold_stays_off(self) -> None:
        # target_w < on_threshold → remains off
        assert _hysteresis(1999.9, False, 2000.0, 1750.0) is False

    def test_off_at_on_threshold_turns_on(self) -> None:
        # target_w == on_threshold → turns on
        assert _hysteresis(2000.0, False, 2000.0, 1750.0) is True

    def test_off_above_on_threshold_turns_on(self) -> None:
        assert _hysteresis(2500.0, False, 2000.0, 1750.0) is True

    def test_on_above_off_threshold_stays_on(self) -> None:
        assert _hysteresis(1750.1, True, 2000.0, 1750.0) is True

    def test_on_at_off_threshold_stays_on(self) -> None:
        # At exactly off_threshold while ON → stays on
        assert _hysteresis(1750.0, True, 2000.0, 1750.0) is True

    def test_on_below_off_threshold_turns_off(self) -> None:
        assert _hysteresis(1749.9, True, 2000.0, 1750.0) is False

    def test_hysteresis_gap_prevents_oscillation(self) -> None:
        # In the gap (1750-2000): OFF stays off, ON stays on
        target_in_gap = 1900.0
        assert _hysteresis(target_in_gap, False, 2000.0, 1750.0) is False
        assert _hysteresis(target_in_gap, True, 2000.0, 1750.0) is True

    def test_zero_target_always_off(self) -> None:
        assert _hysteresis(0.0, False, 2000.0, 1750.0) is False
        assert _hysteresis(0.0, True, 2000.0, 1750.0) is False


# ---------------------------------------------------------------------------
# 8. parse_action_message
# ---------------------------------------------------------------------------


class TestParseActionMessage:
    def _mk_json(self, **kwargs: object) -> str:
        base = {
            "a": "pool_vp",
            "w": 2000.0,
            "m": "on",
            "dl": 120,
            "r": "test",
            "c": 1,
            "s": "brain",
            "t": "2026-05-09T10:00:00+00:00",
        }
        base.update(kwargs)
        return json.dumps(base)

    def test_valid_on_message(self) -> None:
        msg = parse_action_message(self._mk_json(), "pool_vp")
        assert msg is not None
        assert msg.mode == "on"

    def test_valid_off_message(self) -> None:
        msg = parse_action_message(self._mk_json(m="off"), "pool_vp")
        assert msg is not None
        assert msg.mode == "off"

    def test_wrong_asset_id_returns_none(self) -> None:
        msg = parse_action_message(self._mk_json(a="miner"), "pool_vp")
        assert msg is None

    def test_invalid_mode_returns_none(self) -> None:
        msg = parse_action_message(self._mk_json(m="hold"), "pool_vp")
        assert msg is None

    def test_invalid_mode_auto_returns_none(self) -> None:
        msg = parse_action_message(self._mk_json(m="auto"), "pool_vp")
        assert msg is None

    def test_deadline_below_min_returns_none(self) -> None:
        msg = parse_action_message(self._mk_json(dl=59), "pool_vp")
        assert msg is None

    def test_deadline_exactly_min_is_valid(self) -> None:
        msg = parse_action_message(self._mk_json(dl=60), "pool_vp")
        assert msg is not None
        assert msg.deadline_s == 60

    def test_invalid_json_returns_none(self) -> None:
        msg = parse_action_message("not json!", "pool_vp")
        assert msg is None

    def test_empty_string_returns_none(self) -> None:
        msg = parse_action_message("", "pool_vp")
        assert msg is None

    def test_missing_required_key_returns_none(self) -> None:
        # missing "c"
        msg = parse_action_message('{"a":"pool_vp","m":"on"}', "pool_vp")
        assert msg is None

    def test_miner_asset(self) -> None:
        j = json.dumps({"a": "miner", "m": "on", "c": 2, "w": 1000.0, "dl": 120})
        msg = parse_action_message(j, "miner")
        assert msg is not None
        assert msg.asset_id == "miner"

    def test_pool_elv_asset(self) -> None:
        j = json.dumps({"a": "pool_elv", "m": "off", "c": 3, "dl": 120})
        msg = parse_action_message(j, "pool_elv")
        assert msg is not None
        assert msg.asset_id == "pool_elv"

    def test_null_input_returns_none(self) -> None:
        msg = parse_action_message("null", "pool_vp")
        assert msg is None

    def test_array_input_returns_none(self) -> None:
        msg = parse_action_message("[1,2,3]", "pool_vp")
        assert msg is None


# ---------------------------------------------------------------------------
# 9. translate() — Guardian safe state
# ---------------------------------------------------------------------------


class TestTranslateGuardianSafeState:
    def test_safe_state_forces_off_regardless_of_mode(self) -> None:
        cfg = _cfg()
        action = _action(mode="on", target_w=3000.0)
        snap = _snap(guardian_safe_state=True, switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 5)  # within dwell
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.fault_state == FaultState.SAFE_STATE_ACTIVE

    def test_safe_state_bypasses_dwell(self) -> None:
        cfg = _cfg(min_dwell_s=120)
        action = _action(mode="on")
        snap = _snap(guardian_safe_state=True, switch_is_on=True)
        # last_change just 1s ago — normally dwell would hold
        st = _state(switch_on=True, last_change_ts=NOW_TS - 1)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF

    def test_safe_state_when_already_off(self) -> None:
        cfg = _cfg()
        snap = _snap(guardian_safe_state=True, switch_is_on=False)
        st = _state(switch_on=False)
        result = translate(None, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.rejected_w == pytest.approx(0.0)

    def test_safe_state_rejected_w_when_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        snap = _snap(guardian_safe_state=True, switch_is_on=True)
        st = _state(switch_on=True)
        result = translate(None, cfg, snap, st, NOW_TS)
        assert result.rejected_w == pytest.approx(2500.0)


# ---------------------------------------------------------------------------
# 10. translate() — No action (Brain silent)
# ---------------------------------------------------------------------------


class TestTranslateNoAction:
    def test_returns_hold_with_offline(self) -> None:
        cfg = _cfg()
        snap = _snap()
        st = _state()
        result = translate(None, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.fault_state == FaultState.OFFLINE
        assert result.stale_action is True

    def test_actual_w_when_switch_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True)
        result = translate(None, cfg, snap, st, NOW_TS)
        assert result.actual_w == pytest.approx(2500.0)

    def test_actual_w_when_switch_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False)
        result = translate(None, cfg, snap, st, NOW_TS)
        assert result.actual_w == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 11. translate() — Stale action
# ---------------------------------------------------------------------------


class TestTranslateStaleAction:
    def test_stale_action_returns_hold(self) -> None:
        cfg = _cfg()
        action = _action(deadline_s=120)
        snap = _snap(action_age_s=121.0)  # older than deadline
        st = _state()
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.stale_action is True
        assert result.cap_engage is True

    def test_not_stale_exactly_at_deadline(self) -> None:
        cfg = _cfg()
        action = _action(deadline_s=120)
        snap = _snap(action_age_s=120.0)  # exactly at deadline — NOT stale
        st = _state(switch_on=False)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.stale_action is False

    def test_stale_fault_state_ok(self) -> None:
        action = _action(deadline_s=60)
        snap = _snap(action_age_s=65.0)
        st = _state()
        result = translate(action, _cfg(), snap, st, NOW_TS)
        assert result.fault_state == FaultState.OK

    def test_stale_actual_w_when_on(self) -> None:
        cfg = _cfg(typical_drag_w=3000.0)
        action = _action(deadline_s=120)
        snap = _snap(action_age_s=200.0)
        st = _state(switch_on=True)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.actual_w == pytest.approx(3000.0)


# ---------------------------------------------------------------------------
# 12. translate() — mode == "off"
# ---------------------------------------------------------------------------


class TestTranslateModeOff:
    def test_mode_off_turns_off(self) -> None:
        cfg = _cfg()
        action = _action(mode="off")
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.reason == "mode_off"

    def test_mode_off_respects_dwell_when_on(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(mode="off")
        snap = _snap(switch_is_on=True)
        # turned on 10s ago — dwell not expired
        st = _state(switch_on=True, last_change_ts=NOW_TS - 10)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert "min_dwell" in result.reason

    def test_mode_off_after_dwell_expires_turns_off(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(mode="off")
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 61)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF

    def test_mode_off_while_already_off_no_dwell_needed(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(mode="off")
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 5)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF


# ---------------------------------------------------------------------------
# 13. translate() — Season inactive
# ---------------------------------------------------------------------------


class TestTranslateSeasonInactive:
    def test_season_inactive_forces_off(self) -> None:
        cfg = _cfg()
        action = _action(mode="on", target_w=3000.0)
        snap = _snap(season_active=False)
        st = _state(switch_on=False)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.reason == "season_inactive"

    def test_season_inactive_respects_dwell_when_on(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(mode="on")
        snap = _snap(season_active=False, switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 30)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert "min_dwell" in result.reason

    def test_season_inactive_already_off_immediate(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(mode="on")
        snap = _snap(season_active=False, switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 5)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF

    def test_season_inactive_rejected_reason(self) -> None:
        action = _action(mode="on")
        snap = _snap(season_active=False)
        st = _state()
        result = translate(action, _cfg(), snap, st, NOW_TS)
        assert result.rejected_reason == "season_inactive"


# ---------------------------------------------------------------------------
# 14. translate() — Switch unavailable
# ---------------------------------------------------------------------------


class TestTranslateSwitchUnavailable:
    def test_unavailable_returns_fault(self) -> None:
        cfg = _cfg()
        action = _action(mode="on", target_w=3000.0)
        snap = _snap(switch_unavailable=True)
        st = _state()
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.fault_state == FaultState.FAULT
        assert result.actual_w == pytest.approx(0.0)

    def test_unavailable_rejected_w_equals_target(self) -> None:
        cfg = _cfg()
        action = _action(mode="on", target_w=2200.0)
        snap = _snap(switch_unavailable=True)
        result = translate(action, cfg, snap, _state(), NOW_TS)
        assert result.rejected_w == pytest.approx(2200.0)
        assert result.rejected_reason == "switch_unavailable"


# ---------------------------------------------------------------------------
# 15. translate() — Hysteresis turn-on path
# ---------------------------------------------------------------------------


class TestTranslateHysteresisTurnOn:
    def test_target_at_on_threshold_turns_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        action = _action(target_w=cfg.on_threshold_w)  # 2000.0
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON
        assert result.actual_w == pytest.approx(2500.0)

    def test_target_above_on_threshold_turns_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=2500.0)
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON

    def test_target_below_on_threshold_stays_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        action = _action(target_w=1999.9)  # below 2000.0
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD

    def test_excess_w_reported_when_target_gt_typical(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=3000.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON
        assert result.rejected_w == pytest.approx(500.0)
        assert result.rejected_reason == "binary_excess"

    def test_no_excess_when_target_equals_typical(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=2500.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.rejected_w == pytest.approx(0.0)
        assert result.rejected_reason == ""

    def test_turn_on_fault_state_ok(self) -> None:
        cfg = _cfg()
        action = _action(target_w=2500.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.fault_state == FaultState.OK

    def test_miner_turn_on_at_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_MINER, typical_drag_w=1200.0, hysteresis_on_pct=80.0)
        action = _action(asset_id=ASSET_MINER, target_w=960.0)  # exact on_threshold
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON


# ---------------------------------------------------------------------------
# 16. translate() — Hysteresis turn-off path
# ---------------------------------------------------------------------------


class TestTranslateHysteresisTurnOff:
    def test_target_below_off_threshold_turns_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        action = _action(target_w=1749.9)  # below 1750.0
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.fault_state == FaultState.PARTIAL

    def test_target_exactly_at_off_threshold_stays_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        action = _action(target_w=1750.0)  # at threshold → stays on
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.HOLD

    def test_rejected_w_on_turn_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=1500.0)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.rejected_w == pytest.approx(1500.0)
        assert result.rejected_reason == "below_off_threshold"

    def test_pool_elv_turn_off_below_threshold(self) -> None:
        cfg = _cfg(asset_id=ASSET_POOL_ELV, typical_drag_w=3000.0, hysteresis_off_pct=70.0)
        action = _action(asset_id=ASSET_POOL_ELV, target_w=2000.0)  # below 2100.0
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF


# ---------------------------------------------------------------------------
# 17. translate() — No change (HOLD)
# ---------------------------------------------------------------------------


class TestTranslateNoChange:
    def test_hold_when_on_and_above_off_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=2000.0)  # in the "keep on" zone
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.actual_w == pytest.approx(2500.0)

    def test_hold_when_off_and_below_on_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=1000.0)  # below 2000.0 on threshold
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.actual_w == pytest.approx(0.0)

    def test_hold_off_partial_fault(self) -> None:
        cfg = _cfg()
        action = _action(target_w=500.0)  # positive but below threshold
        st = _state(switch_on=False)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.fault_state == FaultState.PARTIAL

    def test_hold_off_zero_target_ok_fault(self) -> None:
        cfg = _cfg()
        action = _action(target_w=0.0)
        st = _state(switch_on=False)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.fault_state == FaultState.OK


# ---------------------------------------------------------------------------
# 18. translate() — min_dwell_s enforcement
# ---------------------------------------------------------------------------


class TestTranslateMinDwell:
    def test_dwell_blocks_turn_off(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(target_w=100.0)  # below off threshold
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 30)  # 30s ago
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert "min_dwell" in result.reason
        assert "30" in result.reason  # remaining ~30s

    def test_dwell_blocks_turn_on(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(target_w=3000.0)  # above on threshold
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 10)  # 10s ago
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD

    def test_dwell_allows_after_expiry(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(target_w=100.0)  # triggers turn-off
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 61)  # expired
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF

    def test_dwell_allows_turn_on_after_expiry(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        action = _action(target_w=3000.0)
        snap = _snap(switch_is_on=False)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 60)  # exactly expired
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON

    def test_rapid_toggle_prevented(self) -> None:
        cfg = _cfg(min_dwell_s=60)
        # Turn on
        a1 = _action(target_w=3000.0)
        st0 = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        r1 = translate(a1, cfg, _snap(), st0, NOW_TS)
        assert r1.action == SwitchAction.TURN_ON

        # Immediately try to turn off — dwell blocks it
        a2 = _action(target_w=0.0)
        st1 = _state(switch_on=True, last_change_ts=NOW_TS)  # just changed
        r2 = translate(a2, cfg, _snap(switch_is_on=True), st1, NOW_TS + 1)
        assert r2.action == SwitchAction.HOLD

    def test_dwell_actual_w_reflects_current_switch_state(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, min_dwell_s=120)
        action = _action(target_w=0.0)
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 10)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.actual_w == pytest.approx(2500.0)


# ---------------------------------------------------------------------------
# 19. translate() — transitions
# ---------------------------------------------------------------------------


class TestTranslateTransitions:
    def test_off_to_on_transition(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, min_dwell_s=60)
        # Start off for >60s
        action = _action(target_w=2500.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON
        assert result.actual_w == pytest.approx(2500.0)

    def test_on_to_off_transition(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, min_dwell_s=60)
        action = _action(target_w=500.0)  # below off threshold
        snap = _snap(switch_is_on=True)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 120)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF
        assert result.actual_w == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 20. JSONL log format validation
# ---------------------------------------------------------------------------


class TestJsonlLogFormat:
    def test_log_entry_is_valid_json(self) -> None:
        entry = {
            "ts": "2026-05-09T10:00:00+00:00",
            "cycle_id": 123,
            "asset_id": "miner",
            "input": {"target_w": 1500.0, "mode": "on", "action_age_s": 5.2},
            "output": {"action": "turn_on", "reason": "target_w:1500>=on_threshold:960"},
            "fault_state": "OK",
            "actual_w": 1200.0,
            "rejected_w": 300.0,
            "stale_action": False,
            "switch_on": True,
        }
        json_str = json.dumps(entry)
        parsed = json.loads(json_str)
        assert parsed["asset_id"] == "miner"
        assert parsed["output"]["action"] == "turn_on"
        assert parsed["fault_state"] == "OK"
        assert parsed["actual_w"] == pytest.approx(1200.0)
        assert parsed["stale_action"] is False

    def test_log_entry_all_required_fields_present(self) -> None:
        required = {
            "ts",
            "cycle_id",
            "asset_id",
            "input",
            "output",
            "fault_state",
            "actual_w",
            "rejected_w",
            "stale_action",
            "switch_on",
        }
        entry = {
            "ts": "2026-05-09T10:00:00+00:00",
            "cycle_id": 1,
            "asset_id": "pool_vp",
            "input": {"target_w": 0.0, "mode": None, "action_age_s": 0.0},
            "output": {"action": "hold", "reason": "no_action"},
            "fault_state": "OFFLINE",
            "actual_w": 0.0,
            "rejected_w": 0.0,
            "stale_action": True,
            "switch_on": False,
        }
        assert required.issubset(set(entry.keys()))


# ---------------------------------------------------------------------------
# 21. All 3 assets — end-to-end translate smoke tests
# ---------------------------------------------------------------------------


class TestAllAssetsSmoke:
    @pytest.mark.parametrize(
        "asset_id,drag_w,target_w",
        [
            (ASSET_POOL_VP, 2500.0, 2500.0),
            (ASSET_POOL_ELV, 3000.0, 3000.0),
            (ASSET_MINER, 1200.0, 1200.0),
        ],
    )
    def test_turn_on_above_threshold(self, asset_id: str, drag_w: float, target_w: float) -> None:
        cfg = _cfg(asset_id=asset_id, typical_drag_w=drag_w)
        action = _action(asset_id=asset_id, target_w=target_w)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON
        assert result.actual_w == pytest.approx(drag_w)

    @pytest.mark.parametrize(
        "asset_id,drag_w",
        [
            (ASSET_POOL_VP, 2500.0),
            (ASSET_POOL_ELV, 3000.0),
            (ASSET_MINER, 1200.0),
        ],
    )
    def test_stay_off_below_threshold(self, asset_id: str, drag_w: float) -> None:
        cfg = _cfg(asset_id=asset_id, typical_drag_w=drag_w, hysteresis_on_pct=80.0)
        action = _action(asset_id=asset_id, target_w=drag_w * 0.5)  # well below
        st = _state(switch_on=False)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.HOLD


# ---------------------------------------------------------------------------
# 22. BalancerState defaults
# ---------------------------------------------------------------------------


class TestBalancerStateDefaults:
    def test_default_switch_off(self) -> None:
        st = BalancerState()
        assert st.switch_on is False

    def test_default_offline_cycles_zero(self) -> None:
        st = BalancerState()
        assert st.offline_cycles == 0

    def test_default_uptime_zero(self) -> None:
        st = BalancerState()
        assert st.uptime_s == pytest.approx(0.0)

    def test_frozen(self) -> None:
        st = BalancerState()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            st.switch_on = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 23. SensorSnapshot creation
# ---------------------------------------------------------------------------


class TestSensorSnapshot:
    def test_fields(self) -> None:
        snap = SensorSnapshot(
            switch_is_on=True,
            switch_unavailable=False,
            season_active=True,
            guardian_safe_state=False,
            action_age_s=10.5,
        )
        assert snap.switch_is_on is True
        assert snap.action_age_s == pytest.approx(10.5)

    def test_frozen(self) -> None:
        snap = _snap()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            snap.season_active = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 24. Edge cases — target_w exactly at boundaries
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_target_w_zero_stays_off(self) -> None:
        cfg = _cfg()
        action = _action(target_w=0.0)
        st = _state(switch_on=False)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.fault_state == FaultState.OK  # zero target → no partial

    def test_target_w_slightly_below_on_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        action = _action(target_w=1999.99)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.HOLD

    def test_target_w_slightly_above_on_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        action = _action(target_w=2000.01)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert result.action == SwitchAction.TURN_ON

    def test_target_w_at_off_threshold_on_device_stays_on(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        action = _action(target_w=1750.0)  # exactly at off threshold
        st = _state(switch_on=True, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.HOLD  # stays on at threshold

    def test_target_w_slightly_below_off_threshold_turns_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        action = _action(target_w=1749.99)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.TURN_OFF


# ---------------------------------------------------------------------------
# 25. AssetConfig.season_active_entity = None always active
# ---------------------------------------------------------------------------


class TestSeasonActiveNone:
    def test_none_season_always_active(self) -> None:
        cfg = _cfg(season_active_entity=None)
        action = _action(target_w=3000.0)
        snap = _snap(season_active=True)  # coordinator should return True when entity is None
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, snap, st, NOW_TS)
        # Should proceed to hysteresis (turn on)
        assert result.action == SwitchAction.TURN_ON


# ---------------------------------------------------------------------------
# 26. Reason string content in translate results
# ---------------------------------------------------------------------------


class TestReasonStrings:
    def test_safe_state_reason(self) -> None:
        snap = _snap(guardian_safe_state=True)
        result = translate(None, _cfg(), snap, _state(), NOW_TS)
        assert result.reason == "SAFE_STATE_ACTIVE"

    def test_no_action_reason(self) -> None:
        result = translate(None, _cfg(), _snap(), _state(), NOW_TS)
        assert result.reason == "no_action"

    def test_stale_action_reason(self) -> None:
        action = _action(deadline_s=120)
        snap = _snap(action_age_s=200.0)
        result = translate(action, _cfg(), snap, _state(), NOW_TS)
        assert result.reason == "stale_action"

    def test_mode_off_reason(self) -> None:
        action = _action(mode="off")
        result = translate(action, _cfg(), _snap(), _state(), NOW_TS)
        assert result.reason == "mode_off"

    def test_season_inactive_reason(self) -> None:
        action = _action(mode="on")
        snap = _snap(season_active=False)
        result = translate(action, _cfg(), snap, _state(), NOW_TS)
        assert result.reason == "season_inactive"

    def test_turn_on_reason_contains_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_on_pct=80.0)
        action = _action(target_w=2500.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(), st, NOW_TS)
        assert "on_threshold" in result.reason
        assert "2000" in result.reason

    def test_turn_off_reason_contains_threshold(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0, hysteresis_off_pct=70.0)
        action = _action(target_w=1000.0)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert "off_threshold" in result.reason
        assert "1750" in result.reason


# ---------------------------------------------------------------------------
# 27. AssetConfig str fields
# ---------------------------------------------------------------------------


class TestAssetConfigFields:
    def test_season_active_entity_none(self) -> None:
        cfg = _cfg(season_active_entity=None)
        assert cfg.season_active_entity is None

    def test_season_active_entity_set(self) -> None:
        cfg = _cfg(season_active_entity="binary_sensor.pool_season")
        assert cfg.season_active_entity == "binary_sensor.pool_season"

    def test_switch_entity_stored(self) -> None:
        cfg = _cfg(switch_entity="switch.miner_power")
        assert cfg.switch_entity == "switch.miner_power"


# ---------------------------------------------------------------------------
# 28. Additional edge cases — cap_engage and fault_state paths
# ---------------------------------------------------------------------------


class TestCapEngageAndFaultPaths:
    def test_cap_engage_false_for_none_action(self) -> None:
        result = translate(None, _cfg(), _snap(), _state(), NOW_TS)
        assert result.cap_engage is False

    def test_cap_engage_false_for_safe_state(self) -> None:
        snap = _snap(guardian_safe_state=True)
        result = translate(None, _cfg(), snap, _state(), NOW_TS)
        assert result.cap_engage is False

    def test_cap_engage_true_for_stale(self) -> None:
        action = _action(deadline_s=60)
        snap = _snap(action_age_s=70.0)
        result = translate(action, _cfg(), snap, _state(), NOW_TS)
        assert result.cap_engage is True

    def test_cap_engage_false_for_mode_off(self) -> None:
        action = _action(mode="off")
        st = _state(switch_on=False)
        result = translate(action, _cfg(), _snap(), st, NOW_TS)
        assert result.cap_engage is False

    def test_cap_engage_false_for_switch_unavailable(self) -> None:
        action = _action(mode="on", target_w=3000.0)
        snap = _snap(switch_unavailable=True)
        result = translate(action, _cfg(), snap, _state(), NOW_TS)
        assert result.cap_engage is False

    def test_fault_state_partial_when_on_turning_off(self) -> None:
        cfg = _cfg(typical_drag_w=2500.0)
        action = _action(target_w=1000.0)
        st = _state(switch_on=True, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.fault_state == FaultState.PARTIAL

    def test_no_stale_action_on_normal_decision(self) -> None:
        cfg = _cfg()
        action = _action(target_w=3000.0, deadline_s=120)
        snap = _snap(action_age_s=5.0)
        st = _state(switch_on=False, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, snap, st, NOW_TS)
        assert result.stale_action is False

    def test_fault_detail_non_empty_for_safe_state(self) -> None:
        snap = _snap(guardian_safe_state=True)
        result = translate(None, _cfg(), snap, _state(), NOW_TS)
        assert len(result.fault_detail) > 0

    def test_fault_detail_non_empty_for_switch_unavailable(self) -> None:
        action = _action(mode="on")
        snap = _snap(switch_unavailable=True)
        result = translate(action, _cfg(), snap, _state(), NOW_TS)
        assert len(result.fault_detail) > 0

    def test_no_change_hold_cap_engage_true(self) -> None:
        cfg = _cfg()
        action = _action(target_w=3000.0)
        # Switch already on and above off threshold
        st = _state(switch_on=True, last_change_ts=NOW_TS - 200)
        result = translate(action, cfg, _snap(switch_is_on=True), st, NOW_TS)
        assert result.action == SwitchAction.HOLD
        assert result.cap_engage is True


# ---------------------------------------------------------------------------
# 29. parse_action_message — target_w defaults and types
# ---------------------------------------------------------------------------


class TestParseActionMessageTypes:
    def test_target_w_coerced_from_int(self) -> None:
        j = json.dumps({"a": "pool_vp", "m": "on", "c": 1, "w": 2000, "dl": 120})
        msg = parse_action_message(j, "pool_vp")
        assert msg is not None
        assert isinstance(msg.target_w, float)

    def test_cycle_id_coerced_from_float(self) -> None:
        j = json.dumps({"a": "pool_vp", "m": "on", "c": 1.0, "dl": 120})
        msg = parse_action_message(j, "pool_vp")
        assert msg is not None
        assert isinstance(msg.cycle_id, int)

    def test_deadline_exactly_at_min_valid(self) -> None:
        j = json.dumps({"a": "miner", "m": "off", "c": 1, "dl": MIN_DEADLINE_S})
        msg = parse_action_message(j, "miner")
        assert msg is not None

    def test_asset_id_must_match_exactly(self) -> None:
        j = json.dumps({"a": "pool_vp", "m": "on", "c": 1, "dl": 120})
        assert parse_action_message(j, "pool_elv") is None
        assert parse_action_message(j, "miner") is None
        assert parse_action_message(j, "pool_vp") is not None


# ---------------------------------------------------------------------------
# 30. FeedbackMessage — edge values
# ---------------------------------------------------------------------------


class TestFeedbackEdgeValues:
    def test_zero_actual_w(self) -> None:
        fb = _feedback(actual_w=0.0)
        d = fb.to_compact_dict()
        assert d["w"] == pytest.approx(0.0)

    def test_large_actual_w_json_still_valid(self) -> None:
        fb = _feedback(actual_w=99999.99, cap_max_w=99999.99)
        json_str = fb.to_json()
        parsed = json.loads(json_str)
        assert parsed["w"] == pytest.approx(99999.99)

    def test_empty_fault_detail(self) -> None:
        fb = _feedback(fault_detail="")
        d = fb.to_compact_dict()
        assert d["fd"] == ""

    def test_empty_rejected_reason(self) -> None:
        fb = _feedback(rejected_reason="")
        d = fb.to_compact_dict()
        assert d["rr"] == ""
