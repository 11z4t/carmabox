"""Constants for Brain v0.3 — EV + bat cascading controller."""

from __future__ import annotations

from datetime import timedelta

DOMAIN: str = "brain"
VERSION: str = "0.4.0"

# Polling interval per spec (5s)
SCAN_INTERVAL: timedelta = timedelta(seconds=5)

# Entity IDs — reads (v0.1/v0.2 carryover)
ENTITY_GRID_POWER: str = "sensor.house_grid_power"
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

# PV forecast remaining (forecast.solar integration)
ENTITY_PV_REMAINING_KWH: str = "sensor.energy_production_today_remaining"

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
BINARY_ASSET_IDS: tuple[str, ...] = ("miner", "pool_elv")
BINARY_ACTION_TMPL: str = "input_text.brain_action_{asset_id}_json"

# ── v0.4.1-E: EV Priority Operator override ──────────────────────────────────
ENTITY_EV_PRIORITY: str = "input_boolean.brain_ev_priority"

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
DEFAULT_BAT_SOC_CHARGE_CEILING_PCT: float = 95.0
BAT_LOAN_MIN_SOC_PCT: float = 50.0
BAT_MAX_DISCHARGE_FALLBACK_W: float = 4000.0

# ── F2 cascade-ramp control (cascade.py imports) ────────────────────────────
# F2_KP: proportional gain for grid-null delta scaling (delta_scaled = K_p × delta_raw)
# F2_DELTA_CAP_W: max ramp per cycle (clamp delta to ±300W to avoid step changes)
# F2_STEP_CAP_W: anti-windup ramp limit (Brain target step ±1500W per cycle) — prevents
#                Brain from outpacing bat_balancer's ramp-up
F2_KP: float = 0.2
F2_DELTA_CAP_W: float = 300.0
F2_STEP_CAP_W: float = 1500.0

# ── Phase 1: bat_balancer mode-routing (Brain owns mode, bat_balancer = slave) ──
ENTITY_BAT_BALANCER_MODE: str = "input_select.bat_balancer_mode"
ENTITY_BAT_BALANCER_TARGET_MANUAL_W: str = "input_number.bat_balancer_target_manual_w"
ENTITY_BAT_OFFER_SOURCE: str = "input_text.brain_bat_offer_source"
