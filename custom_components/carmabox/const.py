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
EV_RAMP_INTERVAL_S = 300       # 5 min between ramp-up steps
EV_RAMP_STEPS = [6, 8, 10]    # Gradual: 6A → 8A → 10A
EV_FALLBACK_AMPS = 6           # Safe fallback (not 16A!)

# Grid
DEFAULT_VOLTAGE = 230.0
DEFAULT_MIN_CHARGE_THRESHOLD_KW = 0.3
DEFAULT_SPIKE_THRESHOLD_KW = 1.0
DEFAULT_BATTERY_FULL_FOR_EV_PCT = 95.0
DEFAULT_BATTERY_USABLE_RATIO = 0.85

# Safety
CROSSCHARGE_THRESHOLD_W = 500
EXPORT_GUARD_THRESHOLD_W = -1000
TEMPERATURE_MIN_C = 0
TEMPERATURE_MAX_C = 45
MAX_MODE_CHANGES_PER_HOUR = 60  # 1/min — standby counts, so 10 was too low

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

# Config keys
CONF_BATTERIES = "batteries"
CONF_EV = "ev"
CONF_GRID_OPERATOR = "grid_operator"
CONF_PRICE_AREA = "price_area"
CONF_HOUSEHOLD_SIZE = "household_size"
