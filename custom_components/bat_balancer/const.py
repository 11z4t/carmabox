"""Constants for bat_balancer custom component (spec BAL-10-BAT v1.0)."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "bat_balancer"

# Bank identifiers (match hw-bindings/9X-bat-*.yaml)
BANKS: list[str] = ["kontor", "forrad"]

# Physical limits per bank (fallback when sensor stale > HW_STALE_THRESHOLD_S)
# Verified against GoodWe firmware — see Q6 in V3-BAT-IMPL-PLAN.json
KONTOR_MAX_CHARGE_W: float = 7589.0
KONTOR_MAX_DISCHARGE_W: float = 7589.0
FORRAD_MAX_CHARGE_W: float = 2473.0
FORRAD_MAX_DISCHARGE_W: float = 2473.0

# INV-35: max delta per balancer tick
RATE_LIMIT_W_PER_TICK: float = 1500.0

# Tick interval (must match coordinator update_interval)
TICK_S: int = 5

# INV-24 SoC-equalization defaults (overridden by helpers at runtime)
SOC_EQ_THRESHOLD_DEFAULT_PCT: float = 3.0  # D2: raised from 1.0 — suppress noise below 3pp gap
SOC_EQ_MAX_BIAS_DEFAULT_W: float = 1000.0
# A3: gap ≥ this → 100% to one bank (charge→lowest SoC, disch→highest SoC)
SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT: float = 15.0
# Fix-A (IT-5677 cross-dir RCA): max % of total offer any single bank can receive (0-100).
# Prevents 100% asymmetry when off_grid-locked banks run autonomously in opposite direction.
SOC_EQ_MAX_ASYMMETRY_DEFAULT_PCT: float = 80.0
ENTITY_SOC_EQ_MAX_ASYMMETRY_PCT = "input_number.bat_balancer_soc_max_asymmetry_pct"
# S2: hysteresis — exit full-bias only when divergence drops this far below entry threshold
ENTITY_SOC_EQ_FULL_BIAS_HYSTERESIS_PCT = "input_number.bat_balancer_soc_eq_full_bias_hysteresis_pct"
SOC_EQ_FULL_BIAS_HYSTERESIS_PCT: float = 5.0  # default — helpers override

# ZG-12: anti-export band
ZG12_BAND_W: float = 50.0

# Hardware stale threshold — if sensor.last_changed > this → use static fallback
ENTITY_HW_STALE_THRESHOLD_S = "input_number.bat_balancer_hw_stale_threshold_s"
HW_STALE_THRESHOLD_S: int = 300  # default — helpers override

# Sensor offline threshold — mark bank offline after this many seconds unavailable
ENTITY_BANK_OFFLINE_THRESHOLD_S = "input_number.bat_balancer_bank_offline_threshold_s"
BANK_OFFLINE_THRESHOLD_S: int = 60  # default — helpers override

# INV-23: jämnfördelning tolerance (1%)
INV23_TOLERANCE_FRACTION: float = 0.01

# Brand-agnostic entity IDs (resolved at runtime from helpers / hw-bindings)
ENTITY_BRAIN_TARGET_BAT_W = "input_number.brain_target_bat_w"
ENTITY_BAT_CYCLE_SECONDS = "input_number.bat_balancer_cycle_seconds"
ENTITY_SHADOW_MODE = "input_boolean.bat_balancer_shadow_mode"
ENTITY_BAT_BALANCER_MODE = "input_select.bat_balancer_mode"
ENTITY_BAT_BALANCER_TARGET_MANUAL_W = "input_number.bat_balancer_target_manual_w"
ENTITY_SOC_EQ_THRESHOLD_PCT = "input_number.bat_balancer_soc_equalization_threshold_pct"
ENTITY_SOC_EQ_MAX_BIAS_W = "input_number.bat_balancer_soc_equalization_max_bias_w"
ENTITY_SOC_EQ_FULL_BIAS_THRESHOLD_PCT = "input_number.bat_balancer_soc_full_bias_threshold_pct"
ENTITY_MIN_SOC_BUFFER_PCT = "input_number.bat_balancer_min_soc_buffer_pct"
ENTITY_GRID_ZERO_BAND_W = "input_number.bat_balancer_grid_zero_band_w"

# Per-bank entity patterns (format with bank_id)
ENTITY_BAT_SOC = "sensor.bat_soc_filtered_{bank_id}"
ENTITY_BAT_CHARGE_MAX_W = "sensor.bat_charge_max_w_{bank_id}"
ENTITY_BAT_DISCHARGE_MAX_W = "sensor.bat_discharge_max_w_{bank_id}"
ENTITY_BAT_BATTERY_MODE = "sensor.bat_battery_mode_{bank_id}"
ENTITY_GOODWE_POWER_LIMIT = "number.goodwe_{bank_id}_ems_power_limit"
ENTITY_GOODWE_OPERATION_MODE = "select.goodwe_inverter_operation_mode_{bank_id}"
ENTITY_GOODWE_EMS_MODE = "select.goodwe_{bank_id}_ems_mode"
# INV-19 (revised): op_mode MUST be written for GoodWe to accept EMS commands.
# Write idempotently via _last_goodwe_modes cache — only on mode transition, never every tick.
# The EPS glitch risk (2026-05-02) was caused by repeated writes; idempotent cache prevents that.
GOODWE_MODE_PEAK_SHAVING = "peak_shaving"  # required for EMS to engage
GOODWE_MODE_BATTERY_STANDBY = "battery_standby"  # Q1 OPT-A: prevents autonomous PV charge
GOODWE_EMS_MODE_CHARGE = "charge_battery"
GOODWE_EMS_MODE_DISCHARGE = "discharge_battery"
GOODWE_EMS_MODE_STANDBY = "battery_standby"
ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W = (
    "sensor.bat_balancer_target_effective_w"  # B25: template routes MANUAL/AUTO
)
ENTITY_HOUSE_GRID_POWER = "sensor.house_grid_power"
# A1: static rated cap per bank — used in MANUAL mode to bypass dynamic BMS sensor
ENTITY_BAT_INVERTER_RATED_W = "input_number.bat_inverter_rated_{bank_id}_w"

# HW autonomy disable (T1 init + T2 watchdog — AC-HW-1..AC-HW-4)
ENTITY_GOODWE_ECO_MODE_SOC = "number.goodwe_eco_mode_soc_{bank_id}"
ENTITY_GOODWE_DOD_HOLDING = "switch.goodwe_{bank_id}_dod_holding"
ENTITY_GOODWE_FAST_CHARGING = "switch.goodwe_fast_charging_switch_{bank_id}"
ENTITY_BAT_ALLOW_HW_AUTONOMY = "input_boolean.bat_balancer_allow_hw_autonomy"
ENTITY_BAT_ECO_MODE_DISABLE_SOC = "input_number.bat_balancer_eco_mode_disable_soc"
# GoodWe device registry IDs — needed for goodwe.set_parameter (eco_mode slot switches)
ENTITY_GOODWE_DEVICE_ID = "input_text.goodwe_device_id_{bank_id}"
# Eco_mode slot switches (disabled via goodwe.set_parameter, not number.set_value)
GOODWE_ECO_MODE_SLOTS = (
    "eco_mode_1_switch",
    "eco_mode_2_switch",
    "eco_mode_3_switch",
    "eco_mode_4_switch",
)
GOODWE_ECO_MODE_ENABLE = "eco_mode_enable"

# W6: HW feedback-clamp parameters (adaptive EMA — closed-loop HW-cap enforcement)
ENTITY_BAT_HW_CLAMP_TOLERANCE_W = "input_number.bat_balancer_hw_clamp_tolerance_w"
ENTITY_BAT_HW_CLAMP_EMA_ALPHA = "input_number.bat_balancer_hw_clamp_ema_alpha"
ENTITY_BAT_HW_CLAMP_ACTIVATION_TICKS = "input_number.bat_balancer_hw_clamp_activation_ticks"
ENTITY_BAT_HW_CLAMP_RESET_TICKS = "input_number.bat_balancer_hw_clamp_reset_ticks"
HW_CLAMP_TOLERANCE_DEFAULT_W: float = 50.0
HW_CLAMP_EMA_ALPHA_DEFAULT: float = 0.3
HW_CLAMP_ACTIVATION_TICKS_DEFAULT: int = 3
HW_CLAMP_RESET_TICKS_DEFAULT: int = 6

# HW-EMS mismatch watchdog (Q4 — RC1 follow-up from bat-balancer-pv-autonomy-rca-2026-05-22)
# positive = discharge, negative = charge (GoodWe sign convention)
ENTITY_GOODWE_BATTERY_POWER = "sensor.goodwe_battery_power_{bank_id}"
ENTITY_HW_MISMATCH_THRESHOLD_W = "input_number.bat_balancer_hw_mismatch_threshold_w"
HW_MISMATCH_THRESHOLD_DEFAULT_W: float = 200.0
ENTITY_HW_MISMATCH_TICKS = "input_number.bat_balancer_hw_mismatch_ticks"
HW_MISMATCH_TICKS_DEFAULT: int = (
    3  # default — consecutive ticks before alarm fires (~15s at 5s cycle)
)

# W1: Current watchdog — grid current > trip_a → emergency stop (P0 safety)
# Incident 2026-05-22 08:07: discharge_battery+0W → 24A peak on L1+L2 → fuse trip
ENTITY_HOUSE_L1_CURRENT_A = "sensor.house_l1_current_a"
ENTITY_HOUSE_L2_CURRENT_A = "sensor.house_l2_current_a"
ENTITY_HOUSE_L3_CURRENT_A = "sensor.house_l3_current_a"
ENTITY_BAT_CURRENT_TRIP_A = "input_number.bat_balancer_current_trip_a"
ENTITY_BAT_CURRENT_RESET_A = "input_number.bat_balancer_current_reset_a"
ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE = "input_boolean.bat_balancer_current_emergency_active"
CURRENT_TRIP_DEFAULT_A: float = 14.0
CURRENT_RESET_DEFAULT_A: float = 12.0
ENTITY_CURRENT_RESET_SUSTAIN_S = "input_number.bat_balancer_current_reset_sustain_s"
CURRENT_RESET_SUSTAIN_S: int = 60  # default — helpers override

# Decision-log
DECISION_LOG_SUBDIR = "logs"
DECISION_LOG_FILENAME = "bat_balancer_decisions.jsonl"
DECISION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DECISION_LOG_MAX_AGE_DAYS = 7
DECISION_LOG_RATE_LIMIT_S = 60


ENTITY_BRAIN_STALE_THRESHOLD_S = "input_number.bat_balancer_brain_stale_threshold_s"
BRAIN_STALE_THRESHOLD_S: int = 60  # default — helpers override
ENTITY_BRAIN_OFFER_TOLERANCE_W = "input_number.bat_balancer_brain_offer_tolerance_w"
BRAIN_OFFER_TOLERANCE_W: float = 100.0  # default — balancer sum ≤ brain_offer + this


class BatBalancerStatus(StrEnum):
    OK = "ok"
    OFFLINE_BANK = "offline_bank"
    ZG12_CAPPED = "zg12_capped"
    OVERFLOW_REDISTRIBUTED = "overflow_redistributed"
    ERROR = "error"
    SHADOW_MODE = "shadow_mode"
    INITIALIZING = "initializing"
    MANUAL_MODE = "manual_mode"


class RejectedReason(StrEnum):
    BMS_CAP_AGGREGATE = "bms_cap_aggregate"
    NO_AVAILABLE_BANKS = "no_available_banks"
    ZG12_CAP = "zg12_cap"
    OVERFLOW_UNRESOLVED = "overflow_unresolved"
    BRAIN_UNAVAILABLE = "brain_unavailable"
    DISTRIBUTION_ERROR = "distribution_error"
    BRAIN_OFFER_CLAMP = "brain_offer_clamp"


# Per-bank SoC charge ceiling (charging only) — default = physical max (100%).
# Operator tunes via helper. NEVER hardcode a lower default — BMS is force majeure.
ENTITY_SOC_CHARGE_CEILING_PCT = "input_number.bat_balancer_soc_charge_ceiling_pct"
BANK_SOC_CHARGE_CEILING_PCT: float = 100.0  # physical default — helpers override
