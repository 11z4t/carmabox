"""Constants for binary_balancer."""

from __future__ import annotations

DOMAIN = "binary_balancer"
PLATFORMS = ["sensor"]

# Config entry keys
CONF_ASSET_ID = "asset_id"
CONF_TYPICAL_DRAG_W = "typical_drag_w"
CONF_SWITCH_ENTITY = "switch_entity"
CONF_SEASON_ACTIVE_ENTITY = "season_active_entity"  # optional
CONF_MIN_DWELL_S = "min_dwell_s"
CONF_HYSTERESIS_ON_PCT = "hysteresis_on_pct"
CONF_HYSTERESIS_OFF_PCT = "hysteresis_off_pct"

# Asset IDs
ASSET_POOL_VP = "pool_vp"
ASSET_POOL_ELV = "pool_elv"
ASSET_MINER = "miner"
VALID_ASSET_IDS = frozenset({ASSET_POOL_VP, ASSET_POOL_ELV, ASSET_MINER})

# Default typical drag watts per asset (from user spec)
DEFAULT_TYPICAL_DRAG_W: dict[str, float] = {
    ASSET_POOL_VP: 2500.0,
    ASSET_POOL_ELV: 3000.0,
    ASSET_MINER: 1200.0,
}

DEFAULT_HYSTERESIS_ON_PCT = 80.0
DEFAULT_HYSTERESIS_OFF_PCT = 70.0
DEFAULT_MIN_DWELL_S = 60

# HA input_text helper names (Brain writes, balancer reads)
ACTION_HELPER_TMPL = "input_text.brain_action_{asset_id}_json"
FEEDBACK_HELPER_TMPL = "input_text.balancer_feedback_{asset_id}_json"

OFFLINE_CYCLES_THRESHOLD = 3

# JSONL log
DECISION_LOG_PATH_TMPL = "/config/logs/binary_balancer_{asset_id}_decisions.jsonl"
DECISION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotation

# Update interval
COORDINATOR_INTERVAL_S = 10

BRAIN_STALE_THRESHOLD_S: int = 60  # universal max age for brain action

# HA input_select / input_number helpers for override mode and manual target
OVERRIDE_MODE_HELPER_TMPL = "input_select.{asset_id}_balancer_mode"
MANUAL_TARGET_HELPER_TMPL = "input_number.{asset_id}_manual_target_w"
