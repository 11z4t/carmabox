"""Constants for ev_balancer custom component."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "ev_balancer"

# Physical limits (Easee / IEC 61851)
EV_MIN_A = 6
EV_MAX_A = 16
PHASE_COUNT_DEFAULT = 3
LINE_VOLTAGE_DEFAULT = 230

# Min charge power at 6A / 3-phase / 230V
EV_MIN_CHARGE_W = EV_MIN_A * PHASE_COUNT_DEFAULT * LINE_VOLTAGE_DEFAULT  # 4140 W

# Default tuning parameters (Borje 2026-05-09)
DEFAULT_CYCLE_S = 5
BRAIN_STALE_THRESHOLD_S: int = 60  # seconds — brain offer treated as 0 if older
BRAIN_OFFER_TOLERANCE_W: float = 100.0  # balancer may not exceed brain offer by more than this
DEFAULT_PHASE_HEADROOM_A = 6
DEFAULT_PAUSE_DWELL_S = 120
DEFAULT_FAULT_COOLDOWN_S = 60
DEFAULT_SOFT_FUSE_RECOVERY_COOLDOWN_S = 30
DEFAULT_POST_PAUSE_COOLDOWN_S = 30
DEFAULT_MIN_DWELL_S = 60  # min time between amp changes
DEFAULT_MIN_A = EV_MIN_A
DEFAULT_MAX_A = EV_MAX_A

# Brand-agnostic canonical entity IDs (resolved at runtime from helpers)
ENTITY_BRAIN_TARGET_EV_W = "input_number.brain_target_ev_w"
ENTITY_EV_SOC_FILTERED = "sensor.ev_soc_filtered"
ENTITY_EV_TARGET_SOC = "input_number.ev_target_soc"
ENTITY_EV_MIN_START_SOC = "input_number.ev_min_start_soc"
ENTITY_EV_BALANCER_DISABLE = "input_boolean.ev_balancer_disable"
ENTITY_EV_BALANCER_MODE = "input_select.ev_balancer_mode"
ENTITY_EV_BALANCER_TARGET_MANUAL_A = "input_number.ev_balancer_target_manual_a"
ENTITY_EV_PHASE_COUNT = "input_number.ev_phase_count"
ENTITY_EV_LINE_VOLTAGE = "input_number.ev_line_voltage_v"
ENTITY_HOUSE_MAIN_FUSE_A = "input_number.house_main_fuse_a"
ENTITY_EV_PHASE_HEADROOM_A = "input_number.ev_phase_headroom_a"
ENTITY_EV_MAX_PHYSICAL_FUSE_A = "input_number.ev_max_physical_fuse_a"
ENTITY_EV_MIN_DWELL_S = "input_number.ev_balancer_min_dwell_s"
ENTITY_EV_PAUSE_DWELL_S = "input_number.ev_balancer_pause_dwell_s"
ENTITY_EV_FAULT_COOLDOWN_S = "input_number.ev_balancer_fault_cooldown_s"
ENTITY_EV_SOFT_FUSE_RECOVERY_COOLDOWN_S = "input_number.ev_balancer_soft_fuse_recovery_cooldown_s"
ENTITY_EV_POST_PAUSE_COOLDOWN_S = "input_number.ev_balancer_post_pause_cooldown_s"
ENTITY_EV_CYCLE_S = "input_number.ev_balancer_cycle_s"
ENTITY_EV_SOC_SENSOR = "input_text.ev_soc_sensor"  # configurable fallback

# Easee integration — charger_id sourced from helper (hw-binding)
ENTITY_EASEE_CHARGER_ID = "input_text.easee_charger_id"
ENTITY_EASEE_DEVICE_ID = "input_text.easee_device_id"

# Easee service calls
EASEE_SERVICE_SET_CHARGER_DYNAMIC_LIMIT = "easee.set_charger_dynamic_limit"
EASEE_SERVICE_ACTION_COMMAND = "easee.action_command"

# Easee charger status codes
EASEE_STATUS_DISCONNECTED = "disconnected"  # cable not connected (A)
EASEE_STATUS_AWAITING_START = "awaiting_start"
EASEE_STATUS_CHARGING = "charging"
EASEE_STATUS_COMPLETED = "completed"
EASEE_STATUS_ERROR = "error"  # E-state
EASEE_STATUS_READY_TO_CHARGE = "ready_to_charge"

# House current sensors (brand-agnostic) — phase 1/2/3 in Amperes
ENTITY_HOUSE_PHASE_A_1 = "sensor.house_l1_current_a"
ENTITY_HOUSE_PHASE_A_2 = "sensor.house_l2_current_a"
ENTITY_HOUSE_PHASE_A_3 = "sensor.house_l3_current_a"
# Actual charger current (for BMS-limit detection)
ENTITY_EASEE_ACTUAL_A = "sensor.easee_home_actual_current"
ENTITY_EASEE_POWER_W = "sensor.easee_home_power"
ENTITY_EASEE_STATUS = "sensor.easee_home_status"
ENTITY_EASEE_PHASE_MODE = "sensor.easee_home_phase_mode"

# Decision-log path (relative to hass.config.config_dir)
DECISION_LOG_SUBDIR = "logs"
DECISION_LOG_FILENAME = "ev_balancer_decisions.jsonl"
DECISION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DECISION_LOG_MAX_AGE_DAYS = 7
DECISION_LOG_RATE_LIMIT_S = 60  # max 1 identical log entry per minute


class EvAction(StrEnum):
    """Actions that ev_balancer can issue to Easee."""

    SET_DYNAMIC = "set_dynamic"  # write new dynamic_limit amps
    STOP = "stop"  # is_enabled=False + dynamic=0 (cable hot-unplug / fault)
    PAUSE = "pause"  # action_command=pause (cable connected, no charging)
    RESUME = "resume"  # action_command=resume (start after pause)
    HOLD = "hold"  # no HW write this cycle


class RefusalReason(StrEnum):
    """Reasons why ev_balancer refused to charge or change amps."""

    BALANCER_DISABLED = "balancer_disabled"
    SOC_REACHED = "soc_reached"
    SOC_TOO_LOW = "soc_too_low"
    CABLE_DISCONNECTED = "cable_disconnected"
    CHARGER_FAULT = "charger_fault"
    CHARGER_OFFLINE = "charger_offline"
    PHASE_MODE_NOT_THREE = "phase_mode_not_three"
    SOFT_FUSE_CAP = "soft_fuse_cap"
    BRAIN_TARGET_ZERO = "brain_target_zero"
    DWELL_HOLD = "dwell_hold"
    PAUSE_DWELL = "pause_dwell"
    FAULT_COOLDOWN = "fault_cooldown"
    SOFT_FUSE_RECOVERY_COOLDOWN = "soft_fuse_recovery_cooldown"
    POST_PAUSE_COOLDOWN = "post_pause_cooldown"
    ANTI_RESET_GUARD = "anti_reset_guard"
    BMS_LIMITED = "bms_limited"


class CooldownType(StrEnum):
    """Named cooldown timers managed by CooldownManager."""

    AMP_DWELL = "amp_dwell"
    PAUSE_DWELL = "pause_dwell"
    FAULT_COOLDOWN = "fault_cooldown"
    SOFT_FUSE_RECOVERY = "soft_fuse_recovery"
    POST_PAUSE = "post_pause"


class EvBalancerStatus(StrEnum):
    """High-level balancer status (published as sensor state)."""

    OK = "ok"
    PAUSED = "paused"
    SOFT_FUSE_THROTTLE = "soft_fuse_throttle"
    BMS_LIMITED = "bms_limited"
    FAULT = "fault"
    CABLE_DISCONNECTED = "cable_disconnected"
    SOC_REACHED = "soc_reached"
    SHADOW_MODE = "shadow_mode"
    OFFLINE = "offline"
    INITIALIZING = "initializing"
