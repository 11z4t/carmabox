"""CARMA Box — Constants."""

DOMAIN = "carmabox"
PLATFORMS = ["sensor"]

# Defaults
DEFAULT_TARGET_WEIGHTED_KW = 2.0
DEFAULT_BATTERY_MIN_SOC = 15.0
DEFAULT_EV_NIGHT_TARGET_SOC = 75.0
DEFAULT_EV_FULL_CHARGE_DAYS = 7
DEFAULT_EV_MIN_AMPS = 6
DEFAULT_EV_MAX_AMPS = 10  # 16A spräcker säkring + timmedel
MAX_EV_CURRENT: int = 10  # Hard current limit (A): 10A x 3ph x 230V = 6.9 kW
DEFAULT_FALLBACK_PRICE_ORE = 100.0

# Ellevio defaults
DEFAULT_PEAK_COST_PER_KW = 80.0
DEFAULT_PEAK_TOP_N = 3
DEFAULT_NIGHT_WEIGHT = 0.5
DEFAULT_DAY_WEIGHT = 1.0
DEFAULT_NIGHT_START = 22
DEFAULT_NIGHT_END = 6

# Update intervals
SCAN_INTERVAL_SECONDS = 30
PLAN_INTERVAL_SECONDS = 300
EXECUTOR_INTERVAL_SECONDS = 30

# Battery
DEFAULT_BATTERY_1_KWH = 15.0
DEFAULT_BATTERY_2_KWH = 5.0
DEFAULT_BATTERY_CAP_KWH = DEFAULT_BATTERY_1_KWH + DEFAULT_BATTERY_2_KWH
DEFAULT_BATTERY_EFFICIENCY = 0.90
DEFAULT_MAX_DISCHARGE_KW = 5.0
DEFAULT_BAT_MIN_CHARGE_W: int = 300  # Minimalt meningsfullt laddeffekt
DEFAULT_BAT_MAX_CHARGE_W: int = 6000  # Max total batteri-laddeffekt (2x GoodWe)
DEFAULT_PROACTIVE_MIN_GRID_W: float = 300.0  # Min grid-import för proaktiv urladdning (natt)
DEFAULT_PROACTIVE_MIN_GRID_W_SUN: float = 50.0  # Solrikt dagtid — urladdning vid minimal import
DEFAULT_PROACTIVE_MIN_GRID_W_CLOUDY: float = 200.0  # Mulet dagtid — måttlig tröskel
# Surplus chain
CONSUMER_NEAR_MAX_RATIO = 0.95  # Variable consumer at ≥95% of max_w = "near max"

# Surplus chain — consumer power defaults (W)
DEFAULT_SURPLUS_MINER_W: float = 500.0  # Miner power draw
DEFAULT_SURPLUS_VP_KONTOR_W: float = 1500.0  # VP kontor (heat pump office) power draw
DEFAULT_SURPLUS_VP_POOL_W: float = 3000.0  # VP pool (heat pump pool) power draw
DEFAULT_SURPLUS_POOL_HEATER_W: float = 3000.0  # Pool heater power draw

# Surplus chain — hysteresis / timing defaults
DEFAULT_SURPLUS_START_DELAY_S: float = 60.0  # Wait before starting a new consumer
DEFAULT_SURPLUS_STOP_DELAY_S: float = 180.0  # Wait before stopping a consumer
DEFAULT_SURPLUS_BUMP_DELAY_S: float = 60.0  # Wait before bumping low→high priority
DEFAULT_SURPLUS_MIN_W: float = 50.0  # Ignore surplus below this (noise floor)

# Surplus chain — climate boost defaults
DEFAULT_CLIMATE_BOOST_DEGREES: float = 2.0  # Max setpoint boost offset (°C)
DEFAULT_CLIMATE_BOOST_MIN_SURPLUS_W: float = 500.0  # Min surplus to activate climate boost

# Surplus chain — switch rate limiting (SurplusPlanner level)
MAX_SURPLUS_SWITCHES_PER_WINDOW: int = 2  # Max switch events per rate-limit window
SURPLUS_SWITCH_WINDOW_MIN: int = 30  # Rate-limit window (minutes)
SURPLUS_START_THRESHOLD_KW: float = 1.0  # Min surplus to start consumers (kW)
SURPLUS_STOP_THRESHOLD_KW: float = 0.5  # Surplus below this → stop consumers (kW)
SURPLUS_START_DELAY_MIN: int = 5  # Start delay at planner level (minutes)
SURPLUS_STOP_DELAY_MIN: int = 3  # Stop delay at planner level (minutes)

# Planner — night reserve calculation
DEFAULT_PLANNER_HOUSE_BASELOAD_KW: float = 2.5  # Measured night baseload (kW)
DEFAULT_PLANNER_NIGHT_HOURS: int = 8  # Night window hours (22:00-06:00)
DEFAULT_PLANNER_APPLIANCE_MARGIN_KWH: float = 3.0  # Reserve for dishwasher/appliances

# P10 safety discharge rates (core/planner.py apply_p10_safety)
P10_DISCHARGE_CONSERVATIVE_KW = 0.5  # p10 < threshold → minimal urladdning
P10_DISCHARGE_MODERATE_KW = 1.0  # low confidence → moderate urladdning
P10_DISCHARGE_NORMAL_KW = 2.0  # normal confidence → full urladdning

DEFAULT_MAX_GRID_CHARGE_KW = 3.0
DEFAULT_GRID_CHARGE_PRICE_THRESHOLD = 15.0
DEFAULT_GRID_CHARGE_MAX_SOC = 90.0

# Planning horizon (PLAT-969: Multi-day planning)
DEFAULT_PLAN_HORIZON_HOURS = 72  # 3 days, configurable 24-168 via input_number

# Consumption
DEFAULT_DAILY_CONSUMPTION_KWH = 15.0
DEFAULT_DAILY_BATTERY_NEED_KWH = 5.0

# Hourly consumption profile (24h, kW) — night/morning/day/evening/late
DEFAULT_CONSUMPTION_PROFILE: list[float] = [0.8] * 6 + [2.0] * 3 + [1.5] * 8 + [2.5] * 5 + [1.0] * 2

# EV
DEFAULT_EV_EFFICIENCY = 0.92
EV_RAMP_INTERVAL_S = 300  # 5 min between ramp-up steps
EV_STUCK_TIMEOUT_S = 6 * 3600  # W6 watchdog: stop EV if SoC unchanged for this long
EV_RAMP_STEPS = [6, 8, 10]  # Gradual: 6A → 8A → 10A
EV_FALLBACK_AMPS = 6  # Safe fallback (not 16A!)

# Discharge drift-guard (RC3)
DRIFT_MIN_DISCHARGE_W = 100  # Ignore drift when discharge command < this
DRIFT_GRID_MARGIN_FACTOR = 1.1  # Grid must exceed target * this to trigger
DRIFT_MIN_EXPECTED_W = 500  # Min expected discharge to evaluate drift
DRIFT_ACTUAL_RATIO_THRESHOLD = 0.3  # Actual < expected * this = drift detected
DRIFT_ESCALATION_CYCLES = 3  # Consecutive drift cycles before P1 escalation

# Grid
DEFAULT_VOLTAGE = 230.0
DEFAULT_MIN_CHARGE_THRESHOLD_KW = 0.3
DEFAULT_SPIKE_THRESHOLD_KW = 1.0
DEFAULT_BATTERY_FULL_FOR_EV_PCT = 95.0
DEFAULT_BATTERY_USABLE_RATIO = 0.85

# Price tiers (öre/kWh) — used for intensity decisions
DEFAULT_PRICE_CHEAP_ORE = 30.0
DEFAULT_PRICE_EXPENSIVE_ORE = 80.0

# Miner control thresholds (W)
DEFAULT_MINER_START_EXPORT_W = 200  # Start miner when exporting > this
DEFAULT_MINER_STOP_IMPORT_W = 500  # Stop miner when importing > this

# BMS taper detection (IT-1939)
TAPER_EXPORT_THRESHOLD_W = 200  # Export > this while charge_pv → taper detected
TAPER_EXIT_EXPORT_W = 100  # Export < this → exit taper
TAPER_EXIT_PV_KW = 0.5  # PV < this → exit taper (sun going down)
TAPER_VP_SURPLUS_W = 500  # Surplus > this → start VP pre-heat/cool
TAPER_EV_SURPLUS_W = 1000  # Surplus > this → start EV charging

# BMS cold lock detection (IT-1948)
# When min cell temperature < threshold, BMS blocks ALL charging (lithium plating protection).
# This is NOT taper — battery accepts ZERO power regardless of EMS mode.
COLD_LOCK_CELL_TEMP_C = 10.0  # Min cell temp below which BMS blocks charging
COLD_LOCK_POWER_THRESHOLD_W = 50  # |battery_power| < this confirms cold lock (≈ 0W)

# Watchdog thresholds
DEFAULT_WATCHDOG_EXPORT_W = 500  # W1: export threshold for charge correction
DEFAULT_WATCHDOG_DISCHARGE_MIN_W = 200  # W2: minimum discharge to correct
DEFAULT_WATCHDOG_EV_IMPORT_W = 500  # W4: grid import threshold to stop EV
DEFAULT_WATCHDOG_MIN_SOC_PCT = 50.0  # W5: min battery for expensive-hour alert

# EV night headroom fallback (kW)
DEFAULT_EV_NIGHT_HEADROOM_KW = 4.0

# Climate / VP control
DEFAULT_CLIMATE_COOL_TARGET_C = 23.0
DEFAULT_CLIMATE_HEAT_TARGET_C = 21.0

# Pool control
DEFAULT_POOL_MIN_TEMP_C = 24.0  # Below this → heat if surplus
DEFAULT_POOL_MAX_TEMP_C = 28.0  # Above this → stop heating

# Safety
CROSSCHARGE_THRESHOLD_W = 500
EXPORT_GUARD_THRESHOLD_W = -1000
TEMPERATURE_MIN_C = 0  # Legacy — use TEMPERATURE_MIN_CHARGE_C / _DISCHARGE_C
TEMPERATURE_MIN_CHARGE_C = 2  # PLAT-1019: Never charge LFP below 2°C
TEMPERATURE_MIN_DISCHARGE_C = 0  # PLAT-1019: Discharge OK above 0°C
TEMPERATURE_MAX_C = 45
MAX_MODE_CHANGES_PER_HOUR = 30  # 0.5/min — prevents oscillation flooding

# ── IT-2067: Peak Tracking ─────────────────────────────────────
PEAK_UPDATE_INTERVAL_S = 300  # Check for new peaks every 5 min
PEAK_RANK_COUNT = 3  # Top-N monthly peaks (Ellevio billing)
PEAK_MIN_MEANINGFUL_KW = 3.0  # Ignore peaks below normal house load
PEAK_WARNING_MARGIN_KW = 1.0  # Warning threshold = rank_3 - margin
DEFAULT_TARGET_DAY_KW = 3.0  # Base daytime grid import target
DEFAULT_TARGET_NIGHT_KW = 5.0  # Base nighttime grid import target

# ── IT-2067: Appliance Spike Detection ─────────────────────────
SPIKE_DETECTION_THRESHOLD_W = 1000  # Grid power jump > this = spike
SPIKE_HISTORY_WINDOW_S = 60  # Window for min-power baseline
SPIKE_PS_LIMIT_W = 1500  # PS limit during spike compensation
SPIKE_COOLDOWN_S = 60  # Seconds after spike ends before restoring
SPIKE_SAFETY_TIMEOUT_S = 600  # Force reset if spike_active > 10 min
SPIKE_DEFAULT_PS_LIMIT_W = 20000  # Normal PS limit (no restriction)

# ── IT-2067: Reserve Target (Solcast-based) ────────────────────
RESERVE_PV_STRONG_KWH = 20.0  # Strong sun → low reserve
RESERVE_PV_WEAK_KWH = 5.0  # Weak sun → high reserve
RESERVE_OFFSET_STRONG_PCT = 0.0  # Add 0% to min_soc on sunny days
RESERVE_OFFSET_WEAK_PCT = 10.0  # Add 10% on cloudy days
RESERVE_OFFSET_NEUTRAL_PCT = 5.0  # Add 5% for average days

# ── IT-2067: Dynamic Discharge Limit ──────────────────────────
# SoC-based: higher SoC = lower PS limit (more aggressive discharge)
DISCHARGE_LIMIT_HIGH_SOC_W = 1000  # SoC > 60%: aggressive
DISCHARGE_LIMIT_MID_SOC_W = 1500  # SoC 40-60%: moderate
DISCHARGE_LIMIT_LOW_SOC_W = 2000  # SoC 20-40%: conservative
DISCHARGE_LIMIT_VERY_LOW_SOC_W = 3000  # SoC < 20%: very conservative
DISCHARGE_NIGHT_FACTOR = 2.0  # Night: x2 (Ellevio weights x0.5)

# ── IT-2067: Cold Temperature Protection ──────────────────────
COLD_TEMP_THRESHOLD_C = 4.0  # Below this = cold condition
COLD_MIN_SOC_PCT = 20.0  # Min SoC when cold (vs 15% normal)

# Appliance categories
APPLIANCE_CATEGORIES = {
    "laundry": "Vitvaror",
    "heating": "Värme/VP",
    "pool": "Pool",
    "miner": "Miner",
    "lighting": "Belysning",
    "ups": "UPS",
    "other": "Övrigt",
}

# Appliance name → category heuristics
APPLIANCE_HINTS: dict[str, str] = {
    "tvattmaskin": "laundry",
    "tvatt": "laundry",
    "torktumlare": "laundry",
    "tork": "laundry",
    "disk": "laundry",
    "varmepump": "heating",
    "varmeflakt": "heating",
    "cirkulationspump": "heating",
    "fjv": "heating",
    "golvvarme": "heating",
    "pool": "pool",
    "miner": "miner",
    "mining": "miner",
    "ups": "ups",
    "eaton": "ups",
    "led": "lighting",
    "belysning": "lighting",
    "lampor": "lighting",
    "spot": "lighting",
}

# System sensor prefixes to exclude from appliance detection
APPLIANCE_EXCLUDE_PREFIXES = (
    "goodwe",
    "pv_",
    "grid_",
    "house_grid",
    "nordpool",
    "solar",
    "battery",
    "ems_",
    "peak_shaving",
    "carmabox",
    "easee",
    "zaptec",
    "wallbox",
    "solcast",
    "forecast_solar",
    "tibber",
    "entsoe",
    "sun_",
    "weather_",
)

# Default appliance power threshold (W) — below this is considered off/standby
DEFAULT_APPLIANCE_THRESHOLD_W = 10

# Household profile — heating types
HEATING_TYPES = {
    "fjv": "Fjärrvärme",
    "vp": "Värmepump",
    "direct": "Direktverkande el",
    "other": "Övrigt",
}

# Household profile — solar directions
SOLAR_DIRECTIONS = {
    "S": "Söder",
    "SO": "Sydost",
    "SV": "Sydväst",
    "O": "Öster",
    "V": "Väster",
    "N": "Norr",
}

# Household profile — contract types
CONTRACT_TYPES = {
    "variable": "Rörligt",
    "fixed": "Fast",
}

# Household profile — electricity retailers
ELECTRICITY_RETAILERS = {
    "tibber": "Tibber",
    "vattenfall": "Vattenfall",
    "fortum": "Fortum",
    "eon": "E.ON",
    "greenely": "Greenely",
    "bixia": "Bixia",
    "other": "Annan",
}

# Household profile — battery brands
BATTERY_BRANDS = {
    "goodwe": "GoodWe",
    "huawei": "Huawei",
    "solaredge": "SolarEdge",
    "byd": "BYD",
    "tesla": "Tesla Powerwall",
    "other": "Annan",
}

# ── IT-2378: Intelligent Scheduler ────────────────────────────────
SCHEDULER_INTERVAL_SECONDS = 900  # 15 min
SCHEDULER_PLAN_HOURS = 24
SCHEDULER_CONSTRAINT_MARGIN = 0.85  # Warn at 85% of target
SCHEDULER_EV_DEPARTURE_HOUR = 6  # Morning departure
SCHEDULER_APPLIANCE_WINDOW_START = 22  # Dishwasher/laundry typical start
SCHEDULER_APPLIANCE_WINDOW_END = 1  # Dishwasher/laundry typical end
SCHEDULER_APPLIANCE_LOAD_KW = 2.0  # Assumed appliance load during window
SCHEDULER_EV_GRID_CHARGE_SPREAD_ORE = 30.0  # Min price spread for grid charge
SCHEDULER_BREACH_MINOR_PCT = 0.10  # <10% over = minor
SCHEDULER_BREACH_MAJOR_PCT = 0.25  # 10-25% over = major, >25% = critical
SCHEDULER_LEARNING_CONFIDENCE_STEP = 0.2  # Confidence increase per occurrence
SCHEDULER_MINER_EXPORT_MIN_W = 500  # Min export before miner can run
SCHEDULER_EV_100_INTERVAL_DAYS = 7  # Days between 100% charges
SCHEDULER_EV_100_PV_THRESHOLD_KWH = 25.0  # Sunny day threshold for PV-based 100%
SCHEDULER_MAX_LEARNINGS = 50  # Cap learning entries

# Config keys
CONF_BATTERIES = "batteries"
CONF_EV = "ev"
CONF_GRID_OPERATOR = "grid_operator"
CONF_PRICE_AREA = "price_area"
CONF_HOUSEHOLD_SIZE = "household_size"

# IT-1965: EV SoC target based on 3-day solar forecast
DEFAULT_SOLAR_GOOD_KWH = 30.0  # Above this = good sun day
DEFAULT_SOLAR_OK_KWH = 20.0  # 20-30 = OK, below 20 = bad
DEFAULT_EV_SOC_MIN_TARGET = 75.0  # Bad sun = conservative
DEFAULT_EV_SOC_MAX_TARGET = 100.0  # Good sun or bad forecast ahead = charge full
DEFAULT_EV_SOC_DERATING = 10.0  # Subtract from last known SoC (conservative)

# Ellevio grid import cap — default used when not configured via opts
GRID_LIMIT_DEFAULT_KW = 2.0  # kW; typical Ellevio daytime tak (LAG 1)

# Night EV state machine (PLAN-03 runtime)
APPLIANCE_PAUSE_THRESHOLD_W = 500  # Pause EV when appliance > this
APPLIANCE_RESUME_THRESHOLD_W = 100  # Resume EV when appliance < this
NEV_DISCHARGE_RAMP_S = 5.0  # Seconds to wait for battery stabilization
NEV_GRID_OVERSHOOT_FACTOR = 1.05  # Increase discharge if grid > target * this

# EV PV surplus charging hysteresis
EV_PV_START_THRESHOLD_KW = 1.5  # Must export > this before starting EV
EV_PV_START_DELAY_S = 120.0  # Export must be stable for 2 min
EV_PV_STOP_DELAY_S = 120.0  # Import must persist 2 min before stopping
EV_PV_AMPS_INTERVAL_S = 60.0  # Min interval between amp changes

# EV stuck detection
EV_STUCK_MAX_HOURS = 6.0  # Stop EV if SoC unchanged for this many hours

# Battery full hysteresis (PLAT-948)
BATTERY_FULL_HYSTERESIS_PCT = 99  # all_batteries_full threshold (avoids 100% flicker)

# Battery SoC imbalance alert (PLAT-1077)
SOC_IMBALANCE_THRESHOLD_PCT = 15  # W8 alert when K/F SoC diff exceeds this

# Crosscharge detection (PLAT-1213)
CROSSCHARGE_DETECTION_THRESHOLD_W = 200  # Min battery power to detect crosscharge

# Hub API timeout (PLAT-1214)
HUB_SYNC_TIMEOUT_S: int = 10  # Max seconds for Hub API calls (HA manifest requires ≤10s)

# ── PLAT-1209: Plan Scoring (plan_scoring.py) ──────────────────────
SCORE_ACTION_PTS = 30.0  # Points awarded for correct action match (0-100 composite)
SCORE_GRID_PTS = 40.0  # Points for grid accuracy
SCORE_SOC_PTS = 30.0  # Points for SoC accuracy
SCORE_GRID_MAX_ERROR_KW = 3.0  # Grid error ≥ this → 0 grid points
SCORE_SOC_MAX_ERROR_PCT = 20.0  # SoC error ≥ this % → 0 SoC points
SCORE_EMA_INITIAL = 50.0  # Starting EMA score (neutral midpoint)
SCORE_EMA_ALPHA = 0.1  # EMA smoothing factor (lower = more memory)
SCORE_HISTORY_MAX_DAYS = 90  # Keep at most this many days of history
SCORE_TREND_MIN_DAYS = 14  # Min days of history required to compute trend
SCORE_TREND_WINDOW_DAYS = 7  # Compare last N days vs previous N days for trend
SCORE_TREND_THRESHOLD_PTS = 3.0  # Score delta ≥ this → "improving" / "declining"
SCORE_WORST_HOURS_WINDOW_DAYS = 30  # Analyse last N days when finding worst hours
SCORE_SUMMARY_WINDOW_DAYS = 7  # Days included in summary average

# ── PLAT-1209: Scheduler Battery/EV thresholds (scheduler.py) ──────
SCHEDULER_EV_SOC_UNKNOWN_DEFAULT = 50.0  # Fallback EV SoC when unavailable/negative
SCHEDULER_EV_BMS_OVERNIGHT_FACTOR = 0.9  # BMS discharges ~10% overnight
SCHEDULER_EV_MIN_ENERGY_KWH = 0.5  # Skip EV scheduling if < this energy needed
SCHEDULER_EV_LEARNING_MIN_CONFIDENCE = 0.5  # Ignore breach learnings below this
SCHEDULER_PV_SURPLUS_FULL_BUDGET_KWH = 10.0  # PV surplus > this → full battery as EV
SCHEDULER_BATTERY_BUDGET_LOW_RATIO = 0.3  # Battery budget fraction when no PV surplus
SCHEDULER_MEDIAN_PRICE_FALLBACK_ORE = 50.0  # Fallback median price
SCHEDULER_DISCHARGE_FLOOR_ORE = 40.0  # Min discharge price threshold
SCHEDULER_DISCHARGE_MEDIAN_FACTOR = 0.9  # Discharge threshold = median x this
SCHEDULER_AGGRESSIVE_FLOOR_ORE = 60.0  # Min aggressive discharge threshold
SCHEDULER_AGGRESSIVE_MEDIAN_FACTOR = 1.3  # Aggressive threshold = median x this
SCHEDULER_SOLAR_STRONG_KWH = 25.0  # Tomorrow PV > this → drain aggressively
SCHEDULER_SOLAR_MODERATE_KWH = 15.0  # Tomorrow PV > this → drain moderately
SCHEDULER_SUNRISE_TARGET_MODERATE_PCT = 30.0  # SoC target at sunrise: moderate sun
SCHEDULER_SUNRISE_TARGET_WEAK_PCT = 50.0  # SoC target at sunrise: weak/no sun
SCHEDULER_SUNRISE_PV_DETECT_KW = 1.0  # PV output > this kW marks sunrise
SCHEDULER_ANTI_IDLE_SOC_RATIO = 0.8  # Discharge anti-idle when SoC > cap x this
SCHEDULER_ANTI_IDLE_MAX_KW = 1.5  # Max anti-idle discharge rate (kW)
CHEAP_CHARGE_PRICE_ORE: float = 20.0  # Price <= this = cheap grid charge
CHEAP_CHARGE_SOC_THRESHOLD_PCT: float = 80.0  # SoC below this qualifies

# Scheduler — idle opportunity analysis (PLAT-1065)
SCHEDULER_IDLE_PV_MIN_SURPLUS_KW: float = 0.5  # Min PV surplus to flag missed charge
SCHEDULER_IDLE_PV_MAX_SOC_PCT: float = 95.0  # SoC below this = PV charge opportunity
SCHEDULER_IDLE_PV_MAX_CHARGE_KW: float = 3.0  # Cap on counted PV charge rate
SCHEDULER_IDLE_CHEAP_CHARGE_KW: float = 2.0  # Assumed charge rate during cheap hours
SCHEDULER_IDLE_DISCHARGE_PRICE_RATIO: float = 1.3  # Price > avg*this = discharge opportunity
SCHEDULER_IDLE_DISCHARGE_SOC_BUFFER_PCT: float = 10.0  # SoC above min to qualify for discharge
SCHEDULER_IDLE_MAX_AVAIL_KW: float = 2.0  # Cap on available discharge kW per hour
SCHEDULER_IDLE_MISSED_CHARGE_THRESHOLD_KWH: float = 2.0  # Report missed charge if > this
SCHEDULER_IDLE_MISSED_DISCHARGE_THRESHOLD_KWH: float = 1.0  # Report missed discharge if > this
SCHEDULER_IDLE_CHEAP_PRICE_RATIO: float = 0.6  # Price < avg*this = cheap hour tip
SCHEDULER_IDLE_EXPENSIVE_PRICE_RATIO: float = 1.5  # Price > avg*this = expensive hour tip
SCHEDULER_IDLE_EXPENSIVE_SOC_BUFFER_PCT: float = 15.0  # SoC above min for expensive discharge tip
SCHEDULER_IDLE_HIGH_PCT: float = 70.0  # Idle >this% = high warning
SCHEDULER_IDLE_MOD_PCT: float = 50.0  # Idle >this% = moderate warning

# Scheduler — data fallbacks (PLAT-1065)
SCHEDULER_PRICE_FALLBACK_ORE: float = 100.0  # Default price when hourly data unavailable
SCHEDULER_LOAD_FALLBACK_KW: float = 2.0  # Default house load when hourly data unavailable
SCHEDULER_LOAD_ALT_FALLBACK_KW: float = 1.5  # Alt load fallback for battery arbitrage loop
SCHEDULER_CORRECTION_DISCHARGE_KW: float = 2.0  # Default discharge kW for breach corrections
SCHEDULER_CORRECTION_MIN_AVAIL_KWH: float = 1.0  # Min available kWh to apply discharge correction

# Noise thresholds (PLAT-1086)
POWER_NOISE_THRESHOLD_W = 50  # Min power to consider device active
GRID_EXPORT_NOISE_W = 100  # Min export before acting on it
PV_ACTIVE_THRESHOLD_W = 200  # Min PV to consider solar producing
# PLAT-1076: seconds PV must stay below threshold before leaving solar-charge mode
PV_LOW_STANDBY_DELAY_S = 300

# Illuminance transitions (PLAT-1086)
LUX_DAYLIGHT = 5000  # Above = bright daylight
LUX_DARK = 500  # Below = dark / night

# Error tracking (PLAT-1086)
CONSECUTIVE_ERROR_LOG_INTERVAL = 10  # Log degraded state every N errors


# Law Guardian constants (PLAT-1219)
LAW_GUARDIAN_MAX_BREACH_HISTORY = 500  # Max breach records kept in history
LAG1_CRITICAL_BREACH_THRESHOLD = 3  # LAG 1 breaches/h before Slack critical alert
LAW_GUARDIAN_TAK_MARGIN_FACTOR = 0.85  # Safety margin applied to Ellevio tak in LAG 1
LAW_GUARDIAN_BATTERY_IDLE_W = 50  # Below this W = battery considered idle (LAG 2)
LAG2_SOC_HYSTERESIS_PCT = 5  # SoC above min_soc required to count as non-idle (LAG 2)
LAG2_IDLE_HOURS_THRESHOLD = 4  # Hours of idle before LAG 2 breach (LAG 2)

# ── PLAT-1224: Cost Model ──────────────────────────────────────────────────
ELLEVIO_RATE_KR_PER_KW_MONTH: float = 81.25  # Ellevio capacity tariff (kr/kW/month)
EXPORT_SPOT_FACTOR: float = 0.8  # Export value as fraction of spot price

# ── PLAT-1225: Device Profiles ─────────────────────────────────────────────
DISHWASHER_AVG_KW: float = 1.2  # Nominell diskmaskin-effekt (kW)
DISHWASHER_COOLDOWN_MIN: int = 0  # Ingen väntetid efter stopp
DISHWASHER_PEAK_KW: float = 1.8  # Max diskmaskin-effekt vid uppvärmning (kW)
DISHWASHER_RUNTIME_H: float = 2.0  # Min körtid per cykel (h)
EV_DAILY_ROLLING_DAYS: int = 7  # Rolling window för daglig förbrukningsstatistik
SCENARIO_MAX_COUNT: int = 15  # Max antal scenarios att hålla i minnet
SCENARIO_MIN_COUNT: int = 5  # Min antal scenarios för meningsfull jämförelse

# ── PLAT-1226: Night Planner ────────────────────────────────────────────────
MAX_NIGHTLY_SOC_DELTA_PCT: int = 20  # Max EV SoC increase per night (%)

# ── PLAT-1229: Plan Feedback ───────────────────────────────────────────────
FEEDBACK_RETENTION_DAYS: int = 30  # Keep HourRecords for this many days
OUTLIER_STD_FACTOR: float = 2.0  # Reject EV sample if > mean + N*std
BASELOAD_MIN_TRAINING_DAYS: int = 7  # Min days of records for reliable baseload
FEEDBACK_ACCURACY_TOLERANCE: float = 0.20  # |planned-actual|/planned <= this = accurate
FEEDBACK_PLANNED_FLOOR_KWH: float = 0.1  # Floor for planned_kwh in accuracy division

# Grid Guard braking thresholds (IT-2064)
GRID_GUARD_BRAKE_THRESHOLD_PCT: float = 0.80  # Braking activates at 80% of tak
GRID_GUARD_BRAKE_RELEASE_PCT: float = 0.70  # Braking releases at 70% of tak (hysteresis)

# ── PLAT-1221: QC constants ─────────────────────────────────────────────────
EV_CAPACITY_KWH: float = 82.0  # EV battery capacity (kWh)
EV_PHASE_COUNT: int = 3  # EV charger phase count
GOODWE_KONTOR_CHARGE_KW: float = 3.6  # GoodWe kontor max charge rate (kW)
GOODWE_FORRAD_CHARGE_KW: float = 1.8  # GoodWe förråd max charge rate (kW)
DEFAULT_BATTERY_TARGET_SOC: float = 80.0  # Default battery target SoC (%)
NIGHT_DEFER_PRICE_FACTOR: float = 0.9  # Defer if tomorrow night <= factor x tonight
EV_TACTICAL_DELTA_PCT: float = 5.0  # SoC delta for tactical trajectory estimate (%)

# ── ML Predictor (PLAT-975) ─────────────────────────────────────────────────
ML_EMA_ALPHA: float = 0.3                  # EMA weight for appliance profile updates
ML_CONFIDENCE_SATURATION_SAMPLES: int = 10  # Sample count at which confidence saturates to 1.0
ML_MAX_SAMPLES_PER_BUCKET: int = 30        # Rolling window size per weekday/hour bucket
ML_MAX_PRESSURE_SAMPLES: int = 100         # Max stored pressure→PV observations
ML_MAX_DECISION_OUTCOMES: int = 200        # Max stored decision outcome records
ML_DEFAULT_APPLIANCE_RISK: float = 0.1     # Baseline appliance risk when no data
ML_MIN_PLAN_CORRECTION_SAMPLES: int = 3    # Min samples needed for plan correction factor
ML_MIN_PLANNED_THRESHOLD_KW: float = 0.1   # Floor for planned_kw in accuracy ratio
ML_SERIALIZE_LIMIT: int = 50               # Max records kept in serialized snapshot
ML_MIN_TRAINED_BUCKETS: int = 24           # Min weekday/hour buckets for is_trained=True
ML_DEFAULT_TEMPERATURE_C: float = 15.0     # Default ambient temp when not measured
ML_PRESSURE_HIGH_HPA: float = 1015.0       # High pressure threshold for PV correction
ML_PRESSURE_LOW_HPA: float = 1005.0        # Low pressure threshold for PV correction
