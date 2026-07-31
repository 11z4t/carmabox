"""Constants for Brain v0.3 — EV + bat cascading controller."""

from __future__ import annotations

from datetime import timedelta

DOMAIN: str = "brain"
VERSION: str = "0.4.0"

# Polling interval per spec (5s)
SCAN_INTERVAL: timedelta = timedelta(seconds=5)

# Entity IDs — reads (v0.1/v0.2 carryover)
ENTITY_GRID_POWER: str = "sensor.house_grid_power"
# 5-min rolling average of P1 grid power (neg=export) — IT-3599 binary cascade
ENTITY_BRAIN_GRID_5MIN_AVG: str = "sensor.brain_grid_w_5min_avg"
# 1-min rolling average — v0.4.5.3 F2 feed-forward (faster grid_null convergence)
ENTITY_BRAIN_GRID_1MIN_AVG: str = "sensor.brain_grid_w_1min_avg"
ENTITY_EV_SOC: str = "sensor.ev_soc_filtered"
ENTITY_EV_TARGET_SOC: str = "input_number.ev_target_soc"
ENTITY_FORCE_EV_ACTIVE: str = "input_boolean.brain_operator_force_ev_active"
ENTITY_FORCE_EV_AMPS: str = "input_number.brain_operator_force_ev_a"

# Night-window schedule helpers
ENTITY_NIGHT_CHARGE_START: str = "input_datetime.brain_night_charge_start"
ENTITY_NIGHT_CHARGE_END: str = "input_datetime.brain_night_charge_end"

# Default time-of-day bounds for night window (22:00–06:00)
DEFAULT_NIGHT_START_HOUR: int = 22
DEFAULT_NIGHT_END_HOUR: int = 6

# Default EV status entity (configurable via configuration.yaml)
ENTITY_EV_STATUS_DEFAULT: str = "sensor.easee_home_12840_status"
ENTITY_EV_ACTUAL_W: str = "sensor.easee_home_12840_power"

# Entity IDs — writes (v0.2)
ENTITY_TARGET_EV_W: str = "input_number.brain_target_ev_w"

# Easee status values that indicate cable is connected
EASEE_CONNECTED_STATES: frozenset[str] = frozenset(
    {
        "charging",
        "awaiting_start",
        "ready_to_charge",
        "paused",
        "completed",
    }
)

# Physics
PHASES: int = 3
VOLTAGE_V: int = 230

# Anti-export deadband — v0.1/v0.2 hardcoded default
DEADBAND_W: float = 100.0

# Night charge flat rate — 6A × 3-phase × 230V = 4140W
# Kept as named constant for backward-compat; v0.3 reads input_number.ev_min_charge_w
NIGHT_CHARGE_W: float = 4140.0

# Fallback defaults (used when HA entity is unavailable)
DEFAULT_EV_TARGET_SOC: float = 100.0
DEFAULT_FORCE_EV_AMPS: float = 6.0

# Decision log (inside HA /config for persistence across restarts)
DEFAULT_DECISION_LOG_PATH: str = "/config/brain_v02_decisions.jsonl"

# ── v0.3 additions ─────────────────────────────────────────────────────────

# EV minimum charge power (operator-configurable; default = NIGHT_CHARGE_W = 4140W)
ENTITY_EV_MIN_CHARGE_W: str = "input_number.ev_min_charge_w"
DEFAULT_EV_MIN_CHARGE_W: float = NIGHT_CHARGE_W  # 4140.0

# Bat-support master switch (False → v0.1 anti-export-only fallback, no bat)
ENTITY_BAT_SUPPORT_ENABLED: str = "input_boolean.brain_v03_bat_support_enabled"

# Bat balancer capability sensor (attrs: can_engage bool, max_w_now float)
ENTITY_BAT_BALANCER_CAPABILITY: str = "sensor.bat_balancer_capability"

# Bat average SoC — read for UPS-marginal check (Brain policy layer)
ENTITY_BAT_AVG_SOC_PCT: str = "sensor.bat_balancer_avg_soc_pct"

# UPS-marginal: operator policy — Brain stops bat-supplement below this SoC
# Distinct from BMS hard floor (bat_balancer owns that, default 10%)
ENTITY_EV_BAT_SUPPORT_MIN_SOC: str = "input_number.ev_bat_support_min_soc"
DEFAULT_EV_BAT_SUPPORT_MIN_SOC: float = 20.0

# Anti-export deadband as configurable helper (v0.3 injects via BrainInput.deadband_w)
ENTITY_DEADBAND_W: str = "input_number.brain_grid_neutral_deadband_w"
DEFAULT_DEADBAND_W: float = DEADBAND_W  # 100.0

# Bat target output — Brain sole writer, bat_balancer sole reader; 0=idle, negative=discharge
ENTITY_TARGET_BAT_W: str = "input_number.brain_target_bat_w"

# Decision log for v0.3
DEFAULT_DECISION_LOG_PATH_V03: str = "/config/brain_v03_decisions.jsonl"

# ── v0.3.1 additions ────────────────────────────────────────────────────────

# Cascade reason — Brain sole writer; human-readable decision text for dashboard
ENTITY_CASCADE_REASON: str = "input_text.brain_cascade_reason"

# PV2 manual mode targets — Brain writes Charge/Discharge/Standby/Auto per bat target
ENTITY_PV2_MANUAL_MODE_KONTOR: str = "input_select.pv2_manual_mode_kontor"
ENTITY_PV2_MANUAL_MODE_FORRAD: str = "input_select.pv2_manual_mode_forrad"

# PV2 reason sensors — Brain reads these to detect manual operator override
ENTITY_PV2_REASON_KONTOR: str = "sensor.pv2_reason_kontor"
ENTITY_PV2_REASON_FORRAD: str = "sensor.pv2_reason_forrad"

# PV2 mode option strings — match input_select options exactly
PV2_MODE_CHARGE: str = "Charge"
PV2_MODE_DISCHARGE: str = "Discharge"
PV2_MODE_STANDBY: str = "Standby"
PV2_MODE_AUTO: str = "Auto"

# PV2 mode switching deadband (ZG-13: named constant, no magic numbers)
# Brain maps: target_bat_w > PV2_MODE_DEADBAND_W → Charge
#             target_bat_w < -PV2_MODE_DEADBAND_W → Discharge
#             else → Standby (or Auto if brain not owning)
PV2_MODE_DEADBAND_W: float = 100.0

# ── v0.4 additions ──────────────────────────────────────────────────────────

# PV forecast remaining — Solcast integration (p10 pessimistic, direct state)
ENTITY_PV_REMAINING_KWH: str = "sensor.solcast_pv_forecast_forecast_remaining_today"

# Sun position
ENTITY_SUN_SUN: str = "sun.sun"

# Battery capacity helpers (operator-configurable; default 15+5=20 kWh)
ENTITY_BAT_CAPACITY_KONTOR: str = "input_number.bat_capacity_kontor_kwh"
ENTITY_BAT_CAPACITY_FORRAD: str = "input_number.bat_capacity_forrad_kwh"
DEFAULT_BAT_CAPACITY_KWH: float = 20.0

# House baseline (kW) — used for remaining-house-load prognosis
ENTITY_HOUSE_BASELINE_KW: str = "input_number.brain_house_baseline_kw"
DEFAULT_HOUSE_BASELINE_KW: float = 1.2

# Buffer gate master switch — off → cascade runs as v0.3.1 (no gate)
ENTITY_BUFFER_GATE_ENABLED: str = "input_boolean.brain_v04_buffer_gate_enabled"

# Brain output helpers for buffer values (Brain sole writer)
ENTITY_BRAIN_BAT_NEED_KWH: str = "input_number.brain_bat_need_kwh"
ENTITY_BRAIN_PV_SURPLUS_REMAINING_KWH: str = "input_number.brain_pv_surplus_remaining_kwh"
ENTITY_BRAIN_PV_BUFFER_KWH: str = "input_number.brain_pv_buffer_kwh"
ENTITY_BRAIN_STRATEGY: str = "input_text.brain_strategy"

# Skip writing buffer outputs if value change < epsilon (reduces HA event storm)
BUFFER_WRITE_EPSILON_KWH: float = 0.01

# Strategy string constants (match BufferSnapshot.strategy values)
STRATEGY_BAT_FULL: str = "BAT_FULL"
STRATEGY_BUFFER_AVAILABLE: str = "BUFFER_AVAILABLE"
STRATEGY_BAT_PRIORITY: str = "BAT_PRIORITY"
STRATEGY_NO_SUN: str = "NO_SUN"
STRATEGY_FORECAST_UNAVAILABLE: str = "FORECAST_UNAVAILABLE"

# ── v0.4.1 additions ────────────────────────────────────────────────────────

# Ellevio-tak entities (v0.4.1-A)
ENTITY_ELLEVIO_DYNAMISKT_TAK: str = "sensor.ellevio_dynamiskt_tak"
ENTITY_PS2_PEAK_SHAVING_LIMIT: str = "sensor.ps2_peak_shaving_limit"
ENTITY_BRAIN_GRID_SAFETY_MARGIN_W: str = "input_number.brain_grid_safety_margin_w"
DEFAULT_GRID_SAFETY_MARGIN_W: float = 500.0

# EV plug-in cooldown (v0.4.1-B)
ENTITY_EV_PLUGIN_COOLDOWN_S: str = "input_number.brain_ev_plugin_cooldown_s"
DEFAULT_EV_PLUGIN_COOLDOWN_S: float = 30.0

# Non-CARMA load + anticipation (v0.4.1-C)
ENTITY_HOUSE_TOTAL_W: str = "sensor.house_total_power_w"
ENTITY_BRAIN_NON_CARMA_ANTICIPATION_W: str = "input_number.brain_non_carma_anticipation_w"
DEFAULT_NON_CARMA_ANTICIPATION_W: float = 2500.0

# ── v0.4.1-D: Binary asset action publishing ────────────────────────────────
BINARY_ASSET_IDS: tuple[str, ...] = ("miner", "pool_elv", "pool_vp")
BINARY_ACTION_TMPL: str = "input_text.brain_action_{asset_id}_json"

# ── v0.4.1-E: EV Priority Operator override + EV sticky state ───────────────
ENTITY_EV_PRIORITY: str = "input_boolean.brain_ev_priority"
ENTITY_EV_TARGET_STICKY: str = "input_boolean.brain_ev_target_sticky"

# ── v0.6 prep: EV safe min SoC (NO_CHARGE threshold) ────────────────────────
ENTITY_EV_SAFE_MIN_SOC: str = "input_number.brain_ev_safe_min_soc"
DEFAULT_EV_SAFE_MIN_SOC: float = 30.0

# ── v0.4.2-F: Per-phase fuse gate ─────────────────────────────────────────────
ENTITY_HOUSE_L1_CURRENT_A: str = "sensor.house_l1_current_a"
ENTITY_HOUSE_L2_CURRENT_A: str = "sensor.house_l2_current_a"
ENTITY_HOUSE_L3_CURRENT_A: str = "sensor.house_l3_current_a"
ENTITY_PER_PHASE_FUSE_WARNING_A: str = "input_number.per_phase_fuse_warning_a"
DEFAULT_PER_PHASE_WARNING_A: float = 14.0

# ── v0.6a: NO_CHARGE operator gate ─────────────────────────────────────────────
ENTITY_BRAIN_SKIP_NIGHT_CHARGE: str = "input_boolean.brain_skip_night_charge"

# ── v0.4.3: Bat-as-active-buffer (house load compensation) ─────────────────
ENTITY_BAT_ACTIVE_BUFFER_ENABLED: str = "input_boolean.brain_bat_active_buffer_enabled"
ENTITY_BAT_FLOOR_DAY_PCT: str = "input_number.brain_bat_floor_day_pct"
ENTITY_BAT_FLOOR_EVENING_PCT: str = "input_number.brain_bat_floor_evening_pct"
ENTITY_BAT_MIN_DAILY_CYCLES_TARGET: str = "input_number.brain_bat_min_daily_cycles_target"
DEFAULT_HOUSE_LOAD_COMP_MARGIN_W: float = 500.0
DEFAULT_BAT_FLOOR_DAY_PCT: float = 50.0
DEFAULT_BAT_FLOOR_EVENING_PCT: float = 30.0
DEFAULT_PER_PHASE_BOOST_A: float = 13.0
BAT_PHASE_BOOST_W: float = 2000.0
BAT_EXPORT_DEADBAND_W: float = 100.0
DEFAULT_BAT_SOC_CHARGE_CEILING_PCT: float = (
    100.0  # Borje 2026-05-14: ladda till 100% enligt cascade tier 6
)
BAT_LOAN_MIN_SOC_PCT: float = 50.0
ENTITY_BAT_LOAN_MIN_SOC_PCT: str = "input_number.brain_bat_loan_min_soc_pct"
BAT_MAX_DISCHARGE_FALLBACK_W: float = 4000.0

# ── v0.4.4: F1/F2 closed-loop grid-null ─────────────────────────────────────
ENTITY_BRAIN_TARGET_GRID_W: str = "input_number.brain_target_grid_w"
DEFAULT_BRAIN_TARGET_GRID_W: float = 0.0
# Nordpool current price (SEK/kWh) — safety gate: if price < 0, accept configured import
ENTITY_NORDPOOL_CURRENT_PRICE: str = "sensor.nordpool_kwh_se3_sek_3_10_025"
# F2 deadband: |error| < this → target_bat_w = 0 (avoids chasing noise)
F2_CLOSED_LOOP_DEADBAND_W: float = 100.0
# IT-4218 anti-windup: max delta per cycle (= bat_balancer step_cap 1500W/15s)
F2_STEP_CAP_W: float = 1500.0
# IT-4218 Spec 1.5 P-controller: proportional gain and per-cycle ramp cap (N1 single-source-of-truth)
F2_KP: float = 0.2  # proportional gain: delta = F2_KP × grid_error
F2_DELTA_CAP_W: float = 300.0  # max delta per cycle from P-controller (anti-escalation)
F2_0VISION_ABSORB_MARGIN_W: float = (
    50.0  # 0-vision: extra absorb margin on top of |grid_5min| to guarantee charge
)

# v0.4.5 F2.1 feed-forward: bat actual power sensors (sum = house bat load).
# Used to convert F2 from proportional (-grid_5min) to feed-forward
# (-(bat_actual + grid_5min)) so steady-state grid_w → 0 instead of half-load.
ENTITY_BAT_POWER_KONTOR: str = "sensor.goodwe_battery_power_kontor"
ENTITY_BAT_POWER_FORRAD: str = "sensor.goodwe_battery_power_forrad"

# IT-4218 T2: PV instantaneous power for spike feed-forward.
# GoodWe-konvention: positiv = genererar, 0 = ingen produktion.
ENTITY_PV_POWER_KONTOR: str = "sensor.goodwe_pv_power_kontor"
ENTITY_PV_POWER_FORRAD: str = "sensor.goodwe_pv_power_forrad"

# IT-4218 T2: PV-spike feed-forward thresholds (Borje 2026-05-17 11:08).
# If PV rises by PV_SPIKE_THRESHOLD_W within ~15s → apply charge bias.
PV_SPIKE_THRESHOLD_W: float = 1000.0  # 1 kW delta triggers feed-forward
PV_SPIKE_MAX_BIAS_W: float = 2000.0  # cap on charge bias added to raw target

# ── v0.4.4: Period-aware cascade ─────────────────────────────────────────────

# Surplus window schedule helpers (when PV surplus is expected)
ENTITY_SURPLUS_START_TIME: str = "input_datetime.brain_surplus_start_time"
ENTITY_SURPLUS_END_TIME: str = "input_datetime.brain_surplus_end_time"

# Default surplus window (09:00–17:00 local time)
DEFAULT_SURPLUS_START_HOUR: int = 9
DEFAULT_SURPLUS_END_HOUR: int = 17

# Floor thresholds for period-specific bat discharge
ENTITY_BAT_FLOOR_MORNING_PCT: str = "input_number.brain_bat_floor_morning_pct"
ENTITY_BAT_FLOOR_AFTER_SURPLUS_PCT: str = "input_number.brain_bat_floor_after_surplus_pct"
DEFAULT_BAT_FLOOR_MORNING_PCT: float = 50.0  # preserve bat for solar charging
DEFAULT_BAT_FLOOR_AFTER_SURPLUS_PCT: float = 50.0  # protect reserve post-surplus

# Night cascade — absolute bat floor (discharge-to target when surplus_likely)
ENTITY_BAT_MIN_ABSOLUTE_PCT: str = "input_number.brain_bat_min_absolute_pct"
BAT_MIN_ABSOLUTE_PCT: float = 15.0

# Night cascade — grid-charge target (charge-to when no surplus expected)
ENTITY_BAT_NIGHT_TARGET_PCT: str = "input_number.brain_bat_night_target_pct"
BAT_NIGHT_TARGET_PCT: float = 80.0

# Night cascade — PV forecast threshold for surplus_likely decision
ENTITY_BAT_FORECAST_THRESHOLD_KWH: str = "input_number.brain_bat_forecast_threshold_kwh"
BAT_FORECAST_THRESHOLD_KWH: float = 30.0

# Solcast tomorrow forecast entity
ENTITY_PV_FORECAST_TOMORROW_KWH: str = "sensor.solcast_pv_forecast_forecast_tomorrow"

# Cheap grid-charge threshold (öre/kWh) — future grid-charge logic
ENTITY_CHEAP_CHARGE_THRESHOLD_ORE: str = "input_number.brain_cheap_charge_threshold_ore"
DEFAULT_CHEAP_CHARGE_THRESHOLD_ORE: float = 100.0

# ── IT-4218 Spec 1.6 Batch B ────────────────────────────────────────────────

# Issue 2: absolute SoC hard floor — discharge blocked below this level
ENTITY_BRAIN_BAT_HARD_FLOOR_PCT: str = "input_number.brain_bat_hard_floor_pct"
DEFAULT_BAT_HARD_FLOOR_PCT: float = 5.0

# Issue 4: consecutive write-fail counter (observability)
ENTITY_BRAIN_WRITE_FAIL_COUNT: str = "input_number.brain_write_fail_count"

# Period name constants (used in BrainOutput.reason)
PERIOD_MORNING: str = "MORNING"
PERIOD_SURPLUS: str = "SURPLUS"
PERIOD_AFTER_SURPLUS: str = "AFTER_SURPLUS"
PERIOD_NIGHT: str = "NIGHT"

# ── v0.5b D3: bat_available_kwh — individual bank SoC sensors ────────────────
ENTITY_BAT_SOC_KONTOR: str = "sensor.pv_battery_soc_kontor"
ENTITY_BAT_SOC_FORRAD: str = "sensor.pv_battery_soc_forrad"

# Cold floor: outdoor temperature sensor (logic not yet implemented)
# See PLAT-1843 (AC37 cold_floor_protect) for the related bat_balancer-side design —
# that ticket keys off battery temperature, not outdoor_temp, so confirm which signal
# this cascade should actually use before implementing.
ENTITY_OUTDOOR_TEMP: str = "sensor.home_temperature"
DEFAULT_OUTDOOR_TEMP_C: float = 15.0

# ── v0.5b D4: ev_need_kwh — car presence + capacity ──────────────────────────
ENTITY_CAR_TRACKER: str = "binary_sensor.frigate_xpeng_g9p_hemma"
ENTITY_CAR_BATTERY_CAPACITY_KWH: str = "input_number.car_battery_capacity_kwh"
ENTITY_CAR_TARGET_SOC: str = "input_number.car_target_soc"
DEFAULT_CAR_BATTERY_CAPACITY_KWH: float = 93.0  # Xpeng G9P brutto 93 kWh (Börje 2026-05-15)
DEFAULT_CAR_TARGET_SOC: float = 80.0
# EV SoC staleness: if ev_soc_filtered not updated in this many seconds → treat as None
EV_SOC_STALE_S: float = 7200.0  # 2 hours

# ── Phase 1: bat_balancer mode-routing (Brain owns mode, bat_balancer = slave) ──
ENTITY_BAT_BALANCER_MODE: str = "input_select.bat_balancer_mode"
ENTITY_BAT_BALANCER_TARGET_MANUAL_W: str = "input_number.bat_balancer_target_manual_w"
ENTITY_BAT_OFFER_SOURCE: str = "input_text.brain_bat_offer_source"
