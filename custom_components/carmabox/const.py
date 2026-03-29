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
DEFAULT_BAT_MIN_CHARGE_W: int = 300   # Minimalt meningsfullt laddeffekt
DEFAULT_BAT_MAX_CHARGE_W: int = 6000  # Max total batteri-laddeffekt (2x GoodWe)
DEFAULT_PROACTIVE_MIN_GRID_W: float = 300.0  # Min grid-import för proaktiv urladdning
DEFAULT_MAX_GRID_CHARGE_KW = 3.0
DEFAULT_GRID_CHARGE_PRICE_THRESHOLD = 15.0
DEFAULT_GRID_CHARGE_MAX_SOC = 90.0

# Consumption
DEFAULT_DAILY_CONSUMPTION_KWH = 15.0
DEFAULT_DAILY_BATTERY_NEED_KWH = 5.0

# Hourly consumption profile (24h, kW) — night/morning/day/evening/late
DEFAULT_CONSUMPTION_PROFILE: list[float] = [0.8] * 6 + [2.0] * 3 + [1.5] * 8 + [2.5] * 5 + [1.0] * 2

# EV
DEFAULT_EV_EFFICIENCY = 0.92
EV_RAMP_INTERVAL_S = 300  # 5 min between ramp-up steps
EV_RAMP_STEPS = [6, 8, 10]  # Gradual: 6A → 8A → 10A
EV_FALLBACK_AMPS = 6  # Safe fallback (not 16A!)

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
DISCHARGE_NIGHT_FACTOR = 2.0  # Night: ×2 (Ellevio weights ×0.5)

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
DEFAULT_SOLAR_GOOD_KWH = 30.0    # Above this = good sun day
DEFAULT_SOLAR_OK_KWH = 20.0      # 20-30 = OK, below 20 = bad
DEFAULT_EV_SOC_MIN_TARGET = 75.0  # Bad sun = conservative
DEFAULT_EV_SOC_MAX_TARGET = 100.0 # Good sun or bad forecast ahead = charge full
DEFAULT_EV_SOC_DERATING = 10.0    # Subtract from last known SoC (conservative)
