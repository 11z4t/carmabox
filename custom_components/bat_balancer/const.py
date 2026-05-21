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
SOC_EQ_THRESHOLD_DEFAULT_PCT: float = 1.0
SOC_EQ_MAX_BIAS_DEFAULT_W: float = 1000.0

# ZG-12: anti-export band
ZG12_BAND_W: float = 50.0

# Hardware stale threshold — if sensor.last_changed > this → use static fallback
HW_STALE_THRESHOLD_S: int = 300

# Sensor offline threshold — mark bank offline after this many seconds unavailable
BANK_OFFLINE_THRESHOLD_S: int = 60

# INV-23: jämnfördelning tolerance (1%)
INV23_TOLERANCE_FRACTION: float = 0.01

# Brand-agnostic entity IDs (resolved at runtime from helpers / hw-bindings)
ENTITY_BRAIN_TARGET_BAT_W = "input_number.brain_target_bat_w"
ENTITY_SHADOW_MODE = "input_boolean.bat_balancer_shadow_mode"
ENTITY_BAT_BALANCER_MODE = "input_select.bat_balancer_mode"
ENTITY_BAT_BALANCER_TARGET_MANUAL_W = "input_number.bat_balancer_target_manual_w"
ENTITY_SOC_EQ_THRESHOLD_PCT = "input_number.bat_balancer_soc_equalization_threshold_pct"
ENTITY_SOC_EQ_MAX_BIAS_W = "input_number.bat_balancer_soc_equalization_max_bias_w"
ENTITY_MIN_SOC_BUFFER_PCT = "input_number.bat_balancer_min_soc_buffer_pct"
ENTITY_GRID_ZERO_BAND_W = "input_number.bat_balancer_grid_zero_band_w"

# Per-bank entity patterns (format with bank_id)
ENTITY_BAT_SOC = "sensor.bat_soc_filtered_{bank_id}"
ENTITY_BAT_CHARGE_MAX_W = "sensor.bat_charge_max_w_{bank_id}"
ENTITY_BAT_DISCHARGE_MAX_W = "sensor.bat_discharge_max_w_{bank_id}"
ENTITY_BAT_BATTERY_MODE = "sensor.bat_battery_mode_{bank_id}"
ENTITY_GOODWE_POWER_LIMIT = "number.goodwe_{bank_id}_ems_power_limit"
ENTITY_GOODWE_OPERATION_MODE = "select.goodwe_inverter_operation_mode_{bank_id}"
GOODWE_MODE_PEAK_SHAVING = "peak_shaving"
GOODWE_MODE_BATTERY_STANDBY = "battery_standby"
ENTITY_HOUSE_GRID_POWER = "sensor.house_grid_power"

# Decision-log
DECISION_LOG_SUBDIR = "logs"
DECISION_LOG_FILENAME = "bat_balancer_decisions.jsonl"
DECISION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DECISION_LOG_MAX_AGE_DAYS = 7
DECISION_LOG_RATE_LIMIT_S = 60


BRAIN_STALE_THRESHOLD_S: int = 60  # seconds
BRAIN_OFFER_TOLERANCE_W: float = 100.0  # balancer sum ≤ brain_offer + this


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


# v0.4.3.3: per-bank SoC charge ceiling (charging only)
BANK_SOC_CHARGE_CEILING_PCT: float = 95.0
