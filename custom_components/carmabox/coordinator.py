"""CARMA Box — DataUpdateCoordinator.

This is the brain. ONE class replaces ALL automations:
- Collects data every 30s
- Runs optimizer every 5 min
- Executes plan every 30s
- SafetyGuard checks EVERY command

No YAML automations. No shell_commands. No cron.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .adapters import EVAdapter, InverterAdapter, WeatherAdapter
    from .core.grid_guard import GridGuardResult
    from .core.state_manager import StateManager
    from .optimizer.models import SchedulerHourSlot
    from .optimizer.multiday_planner import MultiDayPlan

import contextlib

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from .adapters.easee import EaseeAdapter
from .adapters.goodwe import GoodWeAdapter
from .adapters.nordpool import NordpoolAdapter
from .adapters.solcast import SolcastAdapter
from .adapters.tempest import TempestAdapter
from .const import (
    CONSECUTIVE_ERROR_LOG_INTERVAL,
    DEFAULT_BAT_MAX_CHARGE_W,
    DEFAULT_BAT_MIN_CHARGE_W,
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_DAILY_BATTERY_NEED_KWH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_NIGHT_HEADROOM_KW,
    DEFAULT_EV_NIGHT_TARGET_SOC,
    DEFAULT_FALLBACK_PRICE_ORE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_MAX_GRID_CHARGE_KW,
    DEFAULT_MINER_START_EXPORT_W,
    DEFAULT_MINER_STOP_IMPORT_W,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_PLAN_HORIZON_HOURS,
    DEFAULT_PRICE_CHEAP_ORE,
    DEFAULT_PRICE_EXPENSIVE_ORE,
    DEFAULT_PROACTIVE_MIN_GRID_W,
    DEFAULT_PROACTIVE_MIN_GRID_W_CLOUDY,
    DEFAULT_PROACTIVE_MIN_GRID_W_SUN,
    DEFAULT_TARGET_WEIGHTED_KW,
    DEFAULT_VOLTAGE,
    DEFAULT_WATCHDOG_DISCHARGE_MIN_W,
    DEFAULT_WATCHDOG_EV_IMPORT_W,
    DEFAULT_WATCHDOG_EXPORT_W,
    DEFAULT_WATCHDOG_MIN_SOC_PCT,
    DRIFT_ACTUAL_RATIO_THRESHOLD,
    DRIFT_ESCALATION_CYCLES,
    DRIFT_GRID_MARGIN_FACTOR,
    DRIFT_MIN_DISCHARGE_W,
    DRIFT_MIN_EXPECTED_W,
    EV_RAMP_INTERVAL_S,
    EV_RAMP_STEPS,
    EV_STUCK_TIMEOUT_S,
    LUX_DARK,
    LUX_DAYLIGHT,
    PLAN_INTERVAL_SECONDS,
    PV_ACTIVE_THRESHOLD_W,
    SCAN_INTERVAL_SECONDS,
)
from .core.audit import AuditLog
from .core.execution_engine import ExecutionEngine
from .notifications import CarmaNotifier
from .optimizer.consumption import ConsumptionProfile, calculate_house_consumption
from .optimizer.ev_strategy import calculate_ev_schedule
from .optimizer.grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .optimizer.hourly_ledger import EnergyLedger
from .optimizer.models import (
    BatteryCommand,
    BreachCorrection,
    CarmaboxState,
    Decision,
    HourActual,
    HourlyMeterState,
    HourPlan,
    ShadowComparison,
)
from .optimizer.multiday_planner import build_day_inputs, generate_multiday_plan
from .optimizer.planner import generate_plan
from .optimizer.predictor import ConsumptionPredictor, HourSample
from .optimizer.report import (
    DailySample,
    ReportCollector,
    record_daily_sample,
)
from .optimizer.report import reset_if_new_month as reset_report_month
from .optimizer.safety_guard import SafetyGuard
from .optimizer.savings import (
    SavingsState,
    record_cost_estimate,
    record_daily_snapshot,
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
    reset_savings,
    state_from_dict,
    state_to_dict,
)
from .repairs import (
    SAFETY_BLOCK_THRESHOLD,
    clear_issue,
    raise_hub_offline_issue,
    raise_safety_guard_issue,
)

_LOGGER = logging.getLogger(__name__)

SAVINGS_STORE_VERSION = 1
SAVINGS_STORE_KEY = "carmabox_savings"
SAVINGS_SAVE_INTERVAL = 300  # Save at most every 5 minutes
SAVINGS_STALE_DAYS = 30  # Reset savings if data is older than 30 days
SAVINGS_EXECUTOR_OFF_HOURS = 24  # Reset if executor was off for >24h

CONSUMPTION_STORE_VERSION = 1
CONSUMPTION_STORE_KEY = "carmabox_consumption_profile"

PREDICTOR_STORE_VERSION = 1
PREDICTOR_STORE_KEY = "carmabox_predictor"

# CARMA-P0-FIXES Task 4: Runtime persistence
RUNTIME_STORE_VERSION = 1
RUNTIME_STORE_KEY = "carmabox_runtime"

LEDGER_STORE_VERSION = 1
LEDGER_STORE_KEY = "carmabox_ledger"

# Self-healing constants (PLAT-972)
SELF_HEALING_MAX_FAILURES = 3
SELF_HEALING_PAUSE_SECONDS = 300  # 5 minutes


class CarmaboxCoordinator(DataUpdateCoordinator[CarmaboxState]):
    """CARMA Box coordinator — the brain."""

    # Class-level defaults for mock spec compatibility
    _taper_active: bool = False
    _cold_lock_active: bool = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="carmabox",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry

        # ── Config: options override data ─────────────────────
        self._cfg = {**entry.data, **entry.options}

        self.safety = SafetyGuard(
            min_soc=self._cfg.get("min_soc", DEFAULT_BATTERY_MIN_SOC),
        )
        self.notifier = CarmaNotifier(hass, self._cfg)
        self.plan: list[HourPlan] = []
        self._taper_active: bool = False
        self._cold_lock_active: bool = False
        # Compatibility attributes for sensor.py (scheduler_plan.X)
        self.breaches: list[Any] = []
        self.breach_count_month: int = 0
        self.learnings: list[Any] = []
        self.idle_analysis = None
        self.ev_next_full_charge_date = None
        self.scheduler_plan = self  # alias: sensor.py uses coord.scheduler_plan.X
        # PlanSummary-compatible attributes for sensor.py
        self.target_weighted_kw: float = 2.0
        self.max_weighted_kw: float = 0.0
        self.total_charge_kwh: float = 0.0
        self.total_discharge_kwh: float = 0.0
        self.total_ev_kwh: float = 0.0
        self.estimated_cost_kr: float = 0.0
        self.ev_soc_at_06: int | None = None
        # Runtime attributes referenced throughout coordinator
        self._pressure_history: list[tuple[float, float]] = []
        self._current_reserve_kwh: float = 0.0
        self._consecutive_errors: int = 0
        self._bat_active_samples: int = 0
        self._bat_total_samples: int = 0
        self._bat_day_min_soc: float = 100.0
        self._bat_day_max_soc: float = 0.0
        self._ev_usage_tracked_today: bool = False
        self._last_feedback_hour: int = -1
        self._peak_hour_samples: list[tuple[float, float]] = []
        self._peak_last_hour: int = -1
        # _MAX_CORRECTIONS and _MAX_HOUR_SAMPLES are class-level constants (defined below)
        # Grid Guard — LAG 1 enforcement (runs FIRST every cycle)
        from .core.grid_guard import GridGuard, GridGuardConfig

        self._grid_guard = GridGuard(
            GridGuardConfig(
                tak_kw=float(self._cfg.get("ellevio_tak_kw", 2.0)),
                night_weight=float(self._cfg.get("ellevio_night_weight", 0.5)),
                margin=float(self._cfg.get("grid_guard_margin", 0.85)),
                cold_lock_temp_c=float(self._cfg.get("cold_lock_temp_c", 4.0)),
                vp_min_temp_c=float(self._cfg.get("grid_guard_vp_min_temp_c", 10.0)),
            )
        )
        self._grid_guard_result: GridGuardResult | None = None  # Last evaluation result

        # Start at threshold-1 so first update generates a plan immediately
        self._plan_counter = (PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS) - 1
        self._last_command = BatteryCommand.IDLE
        self._last_battery_action = "charge_pv"  # PLAT-1099: safe default
        self._last_discharge_w = 0
        # PLAT-1198: Audit trail for all write commands
        self.audit_log = AuditLog()

        # EV executor state (PLAT-949)
        self._ev_enabled: bool = False
        self._last_known_ev_soc: float = -1.0
        self._last_known_ev_soc_time: float = 0.0  # monotonic timestamp (process-local)
        self._last_known_ev_soc_unix: float = 0.0  # PLAN-01: unix time for restart survival
        # CARMA-QUALITY: EV stuck detection (W6 watchdog)
        self._ev_last_soc_change_t: float = time.monotonic()
        self._ev_prev_soc_for_stuck: float = -1.0
        # IT-1965: Seed from persistent helper if available
        try:
            seed = self.hass.states.get("input_number.carma_ev_last_known_soc")
            if seed and seed.state not in ("unknown", "unavailable", ""):
                self._last_known_ev_soc = float(seed.state)
                # Use entity last_changed as age estimate
                if seed.last_changed:
                    age_s = (
                        datetime.now(seed.last_changed.tzinfo) - seed.last_changed
                    ).total_seconds()
                    if age_s < 43200:  # < 12h
                        self._last_known_ev_soc_time = time.monotonic() - age_s
                    else:
                        self._last_known_ev_soc = -1.0  # Too old
                        _LOGGER.info(
                            "CARMA EV: helper SoC %.0f%% too old (%.0fh)",
                            float(seed.state),
                            age_s / 3600,
                        )
                else:
                    self._last_known_ev_soc_time = time.monotonic()
                if self._last_known_ev_soc > 0:
                    _LOGGER.info(
                        "CARMA EV: seeded last_known_soc=%.0f%%",
                        self._last_known_ev_soc,
                    )
        except (AttributeError, ValueError, TypeError, RuntimeError):
            _LOGGER.debug("EV SoC seed failed", exc_info=True)
        self._ev_current_amps: int = 0
        self._ev_last_ramp_time: float = 0.0
        self._ev_initialized: bool = False

        # K3 (PLAT-945): Deferred write-verify — store (entity, expected_mode)
        # pairs after service calls, verify on NEXT update cycle (30s later)
        # instead of immediately (GoodWe Modbus takes 2-10s to propagate).
        self._pending_write_verifies: list[tuple[str, str]] = []

        # ── Inverter adapters ─────────────────────────────────
        self.inverter_adapters: list[InverterAdapter] = []
        for i in (1, 2):
            prefix = self._cfg.get(f"inverter_{i}_prefix", "")
            device_id = self._cfg.get(f"inverter_{i}_device_id", "")
            if prefix:
                self.inverter_adapters.append(GoodWeAdapter(hass, device_id, prefix))

        # ── EV adapter ────────────────────────────────────────
        self.ev_adapter: EVAdapter | None = None
        if self._cfg.get("ev_enabled", True):
            ev_prefix = self._cfg.get("ev_prefix", "easee_home_12840")
            ev_device_id = self._cfg.get("ev_device_id", "")
            ev_charger_id = self._cfg.get("ev_charger_id", "")
            if ev_prefix:
                self.ev_adapter = EaseeAdapter(
                    hass, ev_device_id, str(ev_prefix), charger_id=ev_charger_id
                )

        # ── Weather adapter ───────────────────────────────────
        # IT-1585: Tempest weather station integration (optional)
        # Provides: temperature (BMS cold lock), illuminance (PV sanity check)
        self.weather_adapter: WeatherAdapter | None = None
        if self._cfg.get("weather_enabled", True):  # Default enabled if Tempest exists
            self.weather_adapter = TempestAdapter(hass)

        # PLAT-1141 COORD-02: ExecutionEngine — hårdvarukontroll via composition
        self._execution_engine = ExecutionEngine(self)

        self.target_kw: float = self._cfg.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        self.min_soc: float = self._cfg.get("min_soc", DEFAULT_BATTERY_MIN_SOC)
        init_now = datetime.now()
        self.savings = SavingsState(month=init_now.month, year=init_now.year)
        self._savings_store: Store[dict[str, Any]] = Store(
            hass, SAVINGS_STORE_VERSION, SAVINGS_STORE_KEY
        )
        self._savings_loaded = False
        self._savings_last_save: float = 0.0
        self.report_collector = ReportCollector(month=init_now.month, year=init_now.year)
        self._daily_discharge_kwh = 0.0
        self._daily_safety_blocks = 0
        self._daily_plans = 0
        self.last_decision = Decision()
        # S4: Bounded deque prevents unbounded memory growth
        self.decision_log: deque[Decision] = deque(maxlen=48)

        # Plan accuracy tracking
        self.hourly_actuals: list[HourActual] = []
        self._last_tracked_hour: int = -1

        # Plan self-correction — track consecutive deviations >50%
        self._plan_deviation_count: int = 0

        # IT-1937: Rule tracking for sensor.carma_box_rules
        self._active_rule_id: str | None = None
        self._rule_triggers: dict[str, dict[str, Any]] = {}
        self._plan_last_correction_time: float = 0.0

        # PLAT-940: Shadow mode — CARMA vs v6 comparison
        self.shadow: ShadowComparison = ShadowComparison()
        self.shadow_log: list[ShadowComparison] = []
        self._shadow_savings_kr: float = 0.0

        # PV allocation plan (for dashboard)
        self._pv_allocation: dict[str, Any] = {}

        # PLAT-927: Ellevio realtime tracking
        self._ellevio_hour_samples: list[tuple[float, float]] = []
        self._ellevio_current_hour: int = -1
        self._ellevio_monthly_hourly_peaks: list[float] = []

        # Consumption learning (persistent via Store)
        self.consumption_profile = ConsumptionProfile()
        self._consumption_store: Store[dict[str, Any]] = Store(
            hass, CONSUMPTION_STORE_VERSION, CONSUMPTION_STORE_KEY
        )
        self._consumption_loaded = False
        self._consumption_last_save: float = 0.0
        self._consumption_last_hour: int = -1

        # PLAT-965: Consumption predictor (Level 2 AI, persistent)
        self.predictor = ConsumptionPredictor()
        self._predictor_store: Store[dict[str, Any]] = Store(
            hass, PREDICTOR_STORE_VERSION, PREDICTOR_STORE_KEY
        )
        self._predictor_loaded = False
        self._predictor_last_save: float = 0.0

        # CARMA-P0-FIXES Task 4: Runtime persistence
        self._runtime_store: Store[dict[str, Any]] = Store(
            hass, RUNTIME_STORE_VERSION, RUNTIME_STORE_KEY
        )
        self._runtime_loaded = False
        self._runtime_dirty = False  # Flag for deferred save after plan generation

        # CARMA-P0-FIXES Task 4: Ledger persistence
        self._ledger_store: Store[dict[str, Any]] = Store(
            hass, LEDGER_STORE_VERSION, LEDGER_STORE_KEY
        )
        self._ledger_loaded = False
        self._ledger_last_save: float = 0.0

        # PLAT-972: Self-healing state
        self._ems_consecutive_failures: int = 0
        self._ems_pause_until: float = 0.0  # monotonic time
        self._ev_last_known_enabled: bool | None = None

        # ── IT-2465: Method isolation — disable failing non-critical methods
        self._disabled_methods: dict[str, float] = {}  # method_name → re-enable time
        _disable_duration_s = 300  # 5 minutes

        # ── Breach Prevention Monitor ──────────────────────────────
        self._meter_state = HourlyMeterState()
        self._breach_corrections: list[BreachCorrection] = []
        self._breach_load_shed_active: bool = False

        # ── Battery standby tracking ──────────────────────────────
        self._bat_idle_seconds: int = 0
        self._bat_daily_idle_seconds: int = 0
        self._bat_idle_day: int = init_now.day

        self._current_date = datetime.now().strftime("%Y-%m-%d")
        self._daily_avg_price: float = float(
            self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE)
        )
        self._avg_price_initialized = False

        # License enforcement — Hub handshake controls ALL features
        # No free tier — all features require active subscription via Hub.
        # At startup: use cached license. Every 6h: re-validate with Hub.
        # Offline grace: 7 days with cached license, then disable all.
        self._license_tier: str = self._cfg.get("license_tier", "none")
        self._license_features: list[str] = list(self._cfg.get("license_features", []))
        self._license_valid_until: str = self._cfg.get("license_valid_until", "")
        self._license_last_check: float = 0.0
        self._license_check_interval: float = 6 * 3600  # 6 hours
        self._license_offline_grace_days: int = 7

        # TEMPORARY: If no hub configured yet, enable all (dev/owner mode)
        hub_url = self._cfg.get("hub_url", "")
        if not hub_url:
            # No hub = development/owner install — all features enabled
            self._license_tier = "premium"
            self._license_features = [
                "analyzer",
                "executor",
                "dashboard",
                "ev_control",
                "miner_control",
                "watchdog",
                "self_healing",
                "morning_email",
                "hourly_ledger",
                "rule_flow",
            ]

        # Features only active if licensed
        config_executor = bool(self._cfg.get("executor_enabled", True))
        # EXP-DEPLOY: Also check HA input_boolean for runtime toggle
        ha_toggle = self.hass.states.get("input_boolean.carma_ev_executor_enabled")
        if ha_toggle and ha_toggle.state == "on":
            config_executor = True
        elif ha_toggle is None:
            # States not loaded yet at init — defer to runtime check
            # Log so we can trace if this is the issue
            _LOGGER.info("CARMA Box: input_boolean not available at init — will check runtime")
        self.executor_enabled = config_executor and self._has_feature("executor")
        if config_executor and not self._has_feature("executor"):
            _LOGGER.warning(
                "CARMA Box: Executor kräver aktiv licens (tier=%s). "
                "Kontakta support för att aktivera.",
                self._license_tier,
            )

        # PLAT-998: Hourly energy ledger — actual cost tracking
        self.ledger = EnergyLedger()

        # PLAT-943: Appliance tracking
        self._appliances: list[dict[str, Any]] = list(self._cfg.get("appliances") or [])
        # Current power per category (W), updated every scan
        self.appliance_power: dict[str, float] = {}
        # Daily energy per category (Wh), reset at midnight
        self.appliance_energy_wh: dict[str, float] = {}

        # PLAT-992: Miner entity (Shelly switch) — config or auto-detect
        self._miner_entity: str = str(self._cfg.get("miner_entity", ""))
        if not self._miner_entity:
            self._miner_entity = self._detect_miner_entity()
        _LOGGER.info(
            "CARMA: miner_entity=%s (from config=%s)",
            self._miner_entity or "NONE",
            self._cfg.get("miner_entity", "NOT_IN_CONFIG"),
        )
        self._miner_on: bool = False
        # Opt #5: Flat line controller — rolling grid average
        self._grid_samples: list[float] = []
        self._grid_sample_max = 10  # 10 x 30s = 5 min rolling window
        self._ev_last_full_charge_date: str = ""
        self._ev_tonight_soc: float = -1.0
        self._estimated_house_base_kw: float = 2.0
        self._daily_goals: dict[str, Any] = {}
        # Breach statistics: {goal_name: [dates]} — escalates if repeated
        self._breach_history: dict[str, list[str]] = {}
        self._breach_escalation: dict[
            str, int
        ] = {}  # 0=normal, 1=warning, 2=critical  # ISO date of last 100% charge

        # PLAT-962: Household benchmarking data (from hub)
        self.benchmark_data: dict[str, Any] | None = None
        self._benchmark_last_fetch: float = 0.0

        # Propagate dry_run to adapters
        for adapter in self.inverter_adapters:
            adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]
        if self.ev_adapter:
            self.ev_adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]

        if not self.executor_enabled:
            _LOGGER.warning("CARMA Box running in ANALYZER mode — no commands will be sent")

        # StateManager: eagerly initialized so tests that call __init__ get it free.
        # Tests that bypass __init__ get lazy init via the _state_mgr property.
        from .core.state_manager import StateManager

        self._state_mgr_cache: StateManager = StateManager(hass, self._cfg)

    @property
    def _state_mgr(self) -> StateManager:
        """Return StateManager — lazy init for tests that bypass __init__."""
        try:
            return self._state_mgr_cache  # fast path (set in __init__)
        except AttributeError:
            from .core.state_manager import StateManager

            self._state_mgr_cache = StateManager(self.hass, self._cfg)
            return self._state_mgr_cache

    def _has_feature(self, feature: str) -> bool:
        """Check if a feature is enabled by current license."""
        return feature in self._license_features

    async def _check_license(self) -> None:
        """Validate license with Hub. Called every 6 hours.

        Hub handshake:
        1. CARMA Box sends: box_id + API key (HMAC-signed)
        2. Hub validates: customer active + subscription valid
        3. Hub returns: signed JWT with tier + features + expiry
        4. CARMA Box stores license locally (offline grace 7 days)
        """
        now = time.monotonic()
        if now - self._license_last_check < self._license_check_interval:
            return
        self._license_last_check = now

        hub_url = self._cfg.get("hub_url", "")
        api_key = self._cfg.get("hub_api_key", "")
        box_id = self._cfg.get("hub_box_id", "")

        if not hub_url or not api_key:
            return  # No hub configured — dev mode

        try:
            import aiohttp

            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    f"{hub_url}/api/v1/license/{box_id}",
                    headers={"X-API-Key": api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=True,
                ) as resp,
            ):
                if resp.status == 200:
                    data = await resp.json()
                    self._license_tier = data.get("tier", "none")
                    self._license_features = data.get("features", [])
                    self._license_valid_until = data.get("valid_until", "")

                    # Update executor based on new license + HA toggle
                    config_exec = bool(self._cfg.get("executor_enabled", True))
                    ha_toggle = self.hass.states.get("input_boolean.carma_ev_executor_enabled")
                    if ha_toggle and ha_toggle.state == "on":
                        config_exec = True
                    self.executor_enabled = config_exec and self._has_feature("executor")

                    _LOGGER.info(
                        "License validated: tier=%s, features=%d, valid_until=%s",
                        self._license_tier,
                        len(self._license_features),
                        self._license_valid_until,
                    )
                else:
                    _LOGGER.warning("License check failed: HTTP %d", resp.status)
        except (aiohttp.ClientError, TimeoutError, RuntimeError, OSError):
            _LOGGER.debug("License check failed — using cached license", exc_info=True)

    @property
    def cable_locked_entity(self) -> str:
        """Entity ID for EV cable locked sensor (for state change listener)."""
        ev_prefix = self._cfg.get("ev_prefix", "")
        if ev_prefix:
            return f"binary_sensor.{ev_prefix}_plug"
        return ""

    async def on_ev_cable_connected(self) -> None:
        """PLAT-992: Instant EV trigger when cable is plugged in.

        Called from state change listener — no 30s wait.
        Checks PV surplus and starts charging immediately if available.
        """
        if not self.ev_adapter or not self.executor_enabled:
            return

        state = self._collect_state()
        pv_kw = state.pv_power_w / 1000

        # If PV is producing and we have surplus → start EV immediately
        if pv_kw > 1.0:
            _LOGGER.info(
                "CARMA: Cable connected + PV %.1f kW → starting EV at %dA",
                pv_kw,
                DEFAULT_EV_MIN_AMPS,
            )
            await self._cmd_ev_start(DEFAULT_EV_MIN_AMPS)
        else:
            _LOGGER.info("CARMA: Cable connected, no PV surplus — waiting for next cycle")

    def _days_since_full_charge(self) -> int:
        """IT-2066: Days since EV was last at 100% SoC."""
        if not self._ev_last_full_charge_date:
            return 99  # Unknown → assume overdue
        try:
            last = datetime.strptime(self._ev_last_full_charge_date, "%Y-%m-%d")
            return (datetime.now() - last).days
        except ValueError:
            return 99

    def _ellevio_weight(self, hour: int) -> float:
        """Get Ellevio weight for given hour (0.5 night, 1.0 day)."""
        from .optimizer.grid_logic import ellevio_weight

        night_weight = float(self._cfg.get("night_weight", 0.5))
        return ellevio_weight(hour, night_weight=night_weight)

    def _read_cell_temp(self, prefix: str) -> float | None:
        """Read min cell temperature for a battery."""
        entity = f"sensor.goodwe_battery_min_cell_temperature_{prefix}"
        state = self.hass.states.get(entity)
        if state and state.state not in ("unavailable", "unknown", ""):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                _LOGGER.debug("Suppressed error", exc_info=True)
        return None

    def _detect_miner_entity(self) -> str:
        """Auto-detect miner switch from appliances config."""
        for app in self._appliances:
            if app.get("category") == "miner":
                eid = app.get("entity_id", "")
                # Convert power sensor to switch entity
                # sensor.shelly1pmg4_xxx_power → switch.shelly1pmg4_xxx
                if eid.startswith("sensor.") and "_power" in eid:
                    switch_id = eid.replace("sensor.", "switch.").replace("_power", "")
                    state = self.hass.states.get(switch_id)
                    if state is not None:
                        _LOGGER.info("CARMA: auto-detected miner switch %s", switch_id)
                        return str(switch_id)
        # Fallback: scan for known miner switches
        for state in self.hass.states.async_all("switch"):
            name = state.entity_id.lower()
            if "miner" in name or "mining" in name:
                _LOGGER.info("CARMA: found miner switch %s", state.entity_id)
                return str(state.entity_id)
        return ""

    def _get_entity(self, key: str, default: str = "") -> str:
        """Get entity_id from config options."""
        return str(self._cfg.get(key, default))

    def _read_float(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA entity — delegates to StateManager."""
        return self._state_mgr.read_float(entity_id, default)

    def _read_float_or_none(self, entity_id: str) -> float | None:
        """Read float or None — delegates to StateManager. PLAT-946."""
        return self._state_mgr.read_float_or_none(entity_id)

    def _read_str(self, entity_id: str, default: str = "") -> str:
        """Read string state from HA entity — delegates to StateManager."""
        return self._state_mgr.read_str(entity_id, default)

    async def _async_restore_savings(self) -> None:
        """Restore savings state from persistent storage."""
        try:
            data = await self._savings_store.async_load()
            if data and isinstance(data, dict):
                now = datetime.now()

                # Check staleness: reset if data is >30 days old
                # or executor was disabled for >24h
                stale = False
                last_save_str = data.get("_last_save_ts")
                if last_save_str:
                    try:
                        last_save_dt = datetime.fromisoformat(last_save_str)
                        age = now - last_save_dt
                        if age > timedelta(days=SAVINGS_STALE_DAYS):
                            _LOGGER.warning(
                                "Savings data is %d days old (>%d), resetting",
                                age.days,
                                SAVINGS_STALE_DAYS,
                            )
                            stale = True
                        elif not data.get("_executor_enabled", True) and age > timedelta(
                            hours=SAVINGS_EXECUTOR_OFF_HOURS
                        ):
                            _LOGGER.warning(
                                "Executor was disabled for %.1f hours (>%d), resetting savings",
                                age.total_seconds() / 3600,
                                SAVINGS_EXECUTOR_OFF_HOURS,
                            )
                            stale = True
                    except (ValueError, TypeError):
                        pass  # Can't parse timestamp, proceed normally

                if stale:
                    self.savings = reset_savings()
                    self.savings.month = now.month
                    self.savings.year = now.year
                    _LOGGER.info("Savings reset due to stale data")
                    return

                restored = state_from_dict(data)
                restored = reset_if_new_month(restored, now)
                self.savings = restored
                _LOGGER.info(
                    "Restored savings: month=%d, trend=%d days, total=%.1f kr",
                    restored.month,
                    len(restored.daily_savings),
                    restored.discharge_savings_kr + restored.grid_charge_savings_kr,
                )
        except (OSError, ValueError, KeyError, RuntimeError):
            _LOGGER.warning("Failed to restore savings, starting fresh", exc_info=True)

    async def _async_save_savings(self) -> None:
        """Persist savings state (rate-limited to every 5 minutes)."""
        now = time.monotonic()
        if now - self._savings_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._savings_last_save = now
        try:
            save_data = state_to_dict(self.savings)
            save_data["_last_save_ts"] = datetime.now().isoformat()
            save_data["_executor_enabled"] = getattr(self, "executor_enabled", False)
            await self._savings_store.async_save(save_data)
        except (OSError, ValueError, RuntimeError, AttributeError):
            _LOGGER.debug("Failed to save savings", exc_info=True)

    async def _async_restore_consumption(self) -> None:
        """Restore consumption profile from persistent storage."""
        try:
            data = await self._consumption_store.async_load()
            if data and isinstance(data, dict):
                self.consumption_profile = ConsumptionProfile.from_dict(data)
                _LOGGER.info(
                    "Restored consumption profile: %d weekday + %d weekend samples",
                    self.consumption_profile.samples_weekday,
                    self.consumption_profile.samples_weekend,
                )
            else:
                # Fall back to config entry options (migration from older versions)
                stored = self._cfg.get("consumption_profile", {})
                if isinstance(stored, dict) and stored:
                    self.consumption_profile = ConsumptionProfile.from_dict(stored)
                    _LOGGER.info("Migrated consumption profile from config entry options")
        except (OSError, ValueError, KeyError, RuntimeError):
            _LOGGER.warning("Failed to restore consumption profile, starting fresh", exc_info=True)

    async def _async_save_consumption(self) -> None:
        """Persist consumption profile (rate-limited to every 5 minutes)."""
        now = time.monotonic()
        if now - self._consumption_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._consumption_last_save = now
        try:
            await self._consumption_store.async_save(self.consumption_profile.to_dict())
        except (OSError, ValueError, RuntimeError, AttributeError):
            _LOGGER.debug("Failed to save consumption profile", exc_info=True)

    async def _async_restore_predictor(self) -> None:
        """Restore predictor from persistent storage (PLAT-965)."""
        try:
            data = await self._predictor_store.async_load()
            if data and isinstance(data, dict):
                self.predictor = ConsumptionPredictor.from_dict(data)
                _LOGGER.info(
                    "Restored predictor: %d samples, trained=%s",
                    self.predictor.total_samples,
                    self.predictor.is_trained,
                )
        except (OSError, ValueError, KeyError, RuntimeError):
            _LOGGER.warning("Failed to restore predictor, starting fresh", exc_info=True)

    async def _async_restore_runtime(self) -> None:
        """Restore runtime state from persistent storage (CARMA-P0-FIXES Task 4)."""
        try:
            data = await self._runtime_store.async_load()
            if data and isinstance(data, dict):
                # Restore plan
                plan_data = data.get("plan", [])
                from .optimizer.models import HourPlan

                self.plan = [
                    HourPlan(
                        hour=p.get("hour", 0),
                        action=p.get("action", "i"),
                        battery_kw=p.get("battery_kw", 0.0),
                        grid_kw=p.get("grid_kw", 0.0),
                        weighted_kw=p.get("weighted_kw", 0.0),
                        pv_kw=p.get("pv_kw", 0.0),
                        consumption_kw=p.get("consumption_kw", 0.0),
                        ev_kw=p.get("ev_kw", 0.0),
                        ev_soc=p.get("ev_soc", 0),
                        battery_soc=p.get("battery_soc", 0),
                        price=p.get("price", 0.0),
                    )
                    for p in plan_data
                ]
                # Restore last_command
                cmd_str = data.get("last_command", "STANDBY")
                try:
                    self._last_command = BatteryCommand[cmd_str]
                except (KeyError, ValueError):
                    self._last_command = BatteryCommand.STANDBY
                # Restore EV state
                self._ev_enabled = bool(data.get("ev_enabled", False))
                self._ev_current_amps = int(data.get("ev_current_amps", DEFAULT_EV_MIN_AMPS))
                # PLAN-01: Restore EV SoC with unix-based age check (survives HA restart)
                stored_soc = float(data.get("ev_soc", -1.0))
                stored_unix = float(data.get("ev_soc_unix_time", 0.0))
                if stored_soc > 0 and stored_unix > 0:
                    age_s = time.time() - stored_unix
                    if age_s < 14400:  # < 4h — still fresh
                        self._last_known_ev_soc = stored_soc
                        self._last_known_ev_soc_unix = stored_unix
                        self._last_known_ev_soc_time = time.monotonic() - age_s
                        _LOGGER.info(
                            "PLAN-01: Restored EV SoC %.0f%% (age %.0fmin)",
                            stored_soc,
                            age_s / 60,
                        )
                    else:
                        _LOGGER.info(
                            "PLAN-01: EV SoC %.0f%% too old (%.1fh) — discarded",
                            stored_soc,
                            age_s / 3600,
                        )
                # Restore miner state
                self._miner_on = bool(data.get("miner_on", False))
                # Restore night EV state (survives HA restart)
                self._night_ev_active = bool(data.get("night_ev_active", False))
                self._nev_state = str(data.get("nev_state", "IDLE"))
                # Restore Ellevio timmedel samples (PLAT-927 persistence)
                raw_samples = data.get("ellevio_hour_samples", [])
                self._ellevio_hour_samples = [
                    (float(s[0]), float(s[1]))
                    for s in raw_samples
                    if isinstance(s, list | tuple) and len(s) == 2
                ]
                self._ellevio_monthly_hourly_peaks = [
                    float(v) for v in data.get("ellevio_monthly_hourly_peaks", [])
                ]
                # Restore surplus hysteresis state
                hyst_data = data.get("surplus_hysteresis")
                if hyst_data and isinstance(hyst_data, dict):
                    from .core.surplus_chain import HysteresisState

                    hyst = HysteresisState()
                    above = hyst_data.get("above", {})
                    below = hyst_data.get("below", {})
                    if isinstance(above, dict):
                        hyst.surplus_above_since = {str(k): float(v) for k, v in above.items()}
                    if isinstance(below, dict):
                        hyst.surplus_below_since = {str(k): float(v) for k, v in below.items()}
                    self._surplus_hysteresis = hyst
                    _LOGGER.info(
                        "Restored surplus hysteresis: %d above, %d below timers",
                        len(hyst.surplus_above_since),
                        len(hyst.surplus_below_since),
                    )
                # PLAT-1095: Restore grid_guard accumulated viktat Wh
                gg_data = data.get("grid_guard")
                if gg_data and self._grid_guard is not None:
                    from datetime import datetime as _dt

                    self._grid_guard.restore_state(gg_data, _dt.now().hour)
                    _LOGGER.info(
                        "Restored grid_guard: hour=%s, accumulated=%.3f Wh",
                        gg_data.get("hour"),
                        gg_data.get("accumulated_viktat_wh", 0.0),
                    )
                _LOGGER.info(
                    "Restored: plan=%d, cmd=%s, ev=%s@%dA, miner=%s, night=%s, ellevio=%d/%d",
                    len(self.plan),
                    cmd_str,
                    self._ev_enabled,
                    self._ev_current_amps,
                    self._miner_on,
                    self._night_ev_active,
                    len(self._ellevio_hour_samples),
                    len(self._ellevio_monthly_hourly_peaks),
                )
        except (OSError, ValueError, KeyError, RuntimeError):
            _LOGGER.warning("Failed to restore runtime, starting fresh", exc_info=True)

    async def _async_save_runtime(self) -> None:
        """Persist runtime state (CARMA-P0-FIXES Task 4)."""
        try:
            data = {
                "plan": [
                    {
                        "hour": p.hour,
                        "action": p.action,
                        "battery_kw": p.battery_kw,
                        "grid_kw": p.grid_kw,
                        "weighted_kw": p.weighted_kw,
                        "pv_kw": p.pv_kw,
                        "consumption_kw": p.consumption_kw,
                        "ev_kw": p.ev_kw,
                        "ev_soc": p.ev_soc,
                        "battery_soc": p.battery_soc,
                        "price": p.price,
                    }
                    for p in self.plan
                ],
                "last_command": self._last_command.name,
                "ev_enabled": self._ev_enabled,
                "ev_current_amps": self._ev_current_amps,
                # PLAN-01: persist unix timestamp so age survives HA restart
                "ev_soc": getattr(self, "_last_known_ev_soc", -1.0),
                "ev_soc_unix_time": getattr(self, "_last_known_ev_soc_unix", 0.0),
                "miner_on": self._miner_on,
                "night_ev_active": getattr(self, "_night_ev_active", False),
                "nev_state": getattr(self, "_nev_state", "IDLE"),
                "ellevio_hour_samples": [
                    [ts, val] for ts, val in getattr(self, "_ellevio_hour_samples", [])
                ],
                "ellevio_monthly_hourly_peaks": list(
                    getattr(self, "_ellevio_monthly_hourly_peaks", [])
                ),
            }
            # Persist surplus hysteresis state
            hyst = getattr(self, "_surplus_hysteresis", None)
            if hyst is not None:
                data["surplus_hysteresis"] = {
                    "above": dict(hyst.surplus_above_since.items()),
                    "below": dict(hyst.surplus_below_since.items()),
                }
            # PLAT-1095: Persist grid_guard accumulated viktat Wh
            if hasattr(self, "_grid_guard") and self._grid_guard is not None:
                data["grid_guard"] = self._grid_guard.get_persistent_state()
            await self._runtime_store.async_save(data)
        except (OSError, ValueError, RuntimeError, AttributeError):
            _LOGGER.debug("Failed to save runtime", exc_info=True)

    async def _async_restore_ledger(self) -> None:
        """Restore ledger from persistent storage (CARMA-P0-FIXES Task 4)."""
        try:
            data = await self._ledger_store.async_load()
            if data and isinstance(data, dict):
                self.ledger = EnergyLedger.from_dict(data)
                _LOGGER.info(
                    "Restored ledger: %d entries, last=%s",
                    len(self.ledger.entries),
                    self.ledger.entries[-1].date if self.ledger.entries else "none",
                )
        except (OSError, ValueError, KeyError, RuntimeError):
            _LOGGER.warning("Failed to restore ledger, starting fresh", exc_info=True)

    async def _async_save_ledger(self) -> None:
        """Persist ledger state (rate-limited to every 5 minutes, CARMA-P0-FIXES Task 4)."""
        now = time.monotonic()
        if now - self._ledger_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._ledger_last_save = now
        try:
            await self._ledger_store.async_save(self.ledger.to_dict())
        except (OSError, ValueError, RuntimeError, AttributeError):
            _LOGGER.debug("Failed to save ledger", exc_info=True)

    async def _async_save_predictor(self) -> None:
        """Persist predictor state (rate-limited to every 5 minutes)."""
        now = time.monotonic()
        if now - self._predictor_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._predictor_last_save = now
        try:
            await self._predictor_store.async_save(self.predictor.to_dict())
        except (OSError, ValueError, RuntimeError, AttributeError):
            _LOGGER.debug("Failed to save predictor", exc_info=True)

    async def _async_fetch_benchmarking(self) -> None:
        """PLAT-962: Fetch benchmarking data from hub (rate-limited to every hour)."""

        now = time.monotonic()
        last_fetch = getattr(self, "_benchmark_last_fetch", 0.0)
        if now - last_fetch < 3600:  # Once per hour
            return
        self._benchmark_last_fetch = now

        hub = getattr(self, "_hub", None)
        if hub is None:
            return
        try:
            cfg = getattr(self, "_cfg", {})
            data = await hub.fetch_benchmarking(cfg)
            if data is not None:
                self.benchmark_data = data
        except (TimeoutError, AttributeError, OSError):
            _LOGGER.debug("Benchmarking fetch failed", exc_info=True)

    async def _execute_v2(self, state: CarmaboxState) -> None:
        """V2 executor — plan-driven, uses core modules.

        Flow: Plan Executor → Battery Balancer → Surplus Chain
        Grid Guard already ran (Layer 0).
        """
        from .core.battery_balancer import (
            BatteryInfo,
            calculate_proportional_discharge,
        )
        from .core.plan_executor import (
            ExecutorState,
            PlanAction,
            check_replan_needed,
            execute_plan_hour,
        )
        from .core.surplus_chain import (
            SurplusConfig,
            allocate_surplus,
            should_reduce_consumers,
        )

        now = datetime.now()
        hour = now.hour
        opts = self._cfg

        _LOGGER.warning("V2: h=%d soc=%.0f", hour, state.total_battery_soc)

        # ── Find plan action for current hour ───────────────────
        planned = next((p for p in self.plan if p.hour == hour), None)
        plan_action = None
        if planned:
            plan_action = PlanAction(
                hour=planned.hour,
                action=planned.action,
                battery_kw=planned.battery_kw,
                grid_kw=planned.grid_kw,
                price=planned.price,
                battery_soc=planned.battery_soc,
                ev_soc=planned.ev_soc,
            )

        # ── Build executor state ────────────────────────────────
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        weight = DEFAULT_NIGHT_WEIGHT if is_night else 1.0
        headroom = self._grid_guard.headroom_kw if self._grid_guard_result else 1.0
        ev_connected = bool(self.ev_adapter.cable_locked) if self.ev_adapter else False

        exec_state = ExecutorState(
            grid_import_w=state.grid_power_w,  # PLAT-1134: raw value, negative=export
            pv_power_w=state.pv_power_w,
            battery_soc_1=state.battery_soc_1,
            battery_soc_2=state.battery_soc_2,
            battery_power_1=state.battery_power_1,
            battery_power_2=state.battery_power_2,
            ev_power_w=state.ev_power_w,
            ev_soc=state.ev_soc,
            ev_connected=ev_connected,
            current_price=self._read_float(opts.get("price_entity", ""), 50.0),
            target_kw=self.target_kw,
            ellevio_weight=weight,
            headroom_kw=headroom,
        )

        # ── Plan Executor decides ───────────────────────────────
        from .core.plan_executor import ExecutorConfig

        ev_phase = int(opts.get("ev_phase_count", 3))
        exec_cfg = ExecutorConfig(
            ev_phase_count=ev_phase,
            ev_min_amps=int(opts.get("ev_min_amps", DEFAULT_EV_MIN_AMPS)),
            ev_max_amps=int(opts.get("ev_max_amps", DEFAULT_EV_MAX_AMPS)),
            grid_charge_price_threshold=float(opts.get("grid_charge_price_threshold", 15.0)),
        )
        cmd = execute_plan_hour(plan_action, exec_state, exec_cfg)

        _LOGGER.debug(
            "V2 EXEC: bat=%s %dW, ev=%s %dA, reason=%s",
            cmd.battery_action,
            cmd.battery_discharge_w,
            cmd.ev_action,
            cmd.ev_amps,
            cmd.reason,
        )

        # Track desired battery action for EMS enforcement (PLAT-1099)
        self._last_battery_action = cmd.battery_action

        # ── Execute battery command ─────────────────────────────
        # PLAT-1192: Skip plan executor battery commands when NEV is active.
        # NEV state machine (below) controls discharge_pv directly — letting
        # the plan executor set charge_pv first causes mode-flapping and
        # zeroes ems_power_limit, fighting the NEV discharge ramp.
        _nev_active = getattr(self, "_night_ev_active", False)
        if _nev_active:
            _LOGGER.debug(
                "V2: Skipping plan battery cmd '%s' — NEV active (state=%s)",
                cmd.battery_action,
                getattr(self, "_nev_state", "?"),
            )
            # Override _last_battery_action so _enforce_ems_modes() doesn't
            # fight NEV by enforcing the plan executor's charge_pv
            self._last_battery_action = "discharge"
        elif cmd.battery_action == "discharge" and cmd.battery_discharge_w > 0:
            # Proportional split
            bat1_kwh = float(opts.get("battery_1_kwh", 15.0))
            bat2_kwh = float(opts.get("battery_2_kwh", 5.0))
            min_soc = float(opts.get("battery_min_soc", 15.0))
            temp1 = getattr(state, "battery_min_cell_temp_1", 15.0) or 15.0
            temp2 = getattr(state, "battery_min_cell_temp_2", 15.0) or 15.0

            # EXP-02: BMS discharge current limits → max_discharge_w per battery
            adapters = self.inverter_adapters
            max_disch_1 = adapters[0].max_discharge_w if len(adapters) > 0 else 5000
            max_disch_2 = adapters[1].max_discharge_w if len(adapters) > 1 else 5000
            bats = [
                BatteryInfo(
                    "kontor",
                    state.battery_soc_1,
                    bat1_kwh,
                    temp1,
                    min_soc=min_soc,
                    max_discharge_w=max_disch_1 or 5000,
                ),
                BatteryInfo(
                    "forrad",
                    state.battery_soc_2,
                    bat2_kwh,
                    temp2,
                    min_soc=min_soc,
                    max_discharge_w=max_disch_2 or 5000,
                ),
            ]
            bal = calculate_proportional_discharge(bats, cmd.battery_discharge_w)

            adapters = self.inverter_adapters
            for i, alloc in enumerate(bal.allocations):
                if i < len(adapters) and alloc.watts > 50:
                    await adapters[i].set_ems_mode("discharge_pv")
                    await adapters[i].set_fast_charging(on=False)
                    # Set EMS power limit to control discharge rate
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": f"number.goodwe_{adapters[i].prefix}_ems_power_limit",
                            "value": alloc.watts,
                        },
                    )
                elif i < len(adapters):
                    await adapters[i].set_ems_mode("battery_standby")
                    await adapters[i].set_fast_charging(on=False)

        elif cmd.battery_action == "charge_pv":
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("charge_pv")
                await adapter.set_fast_charging(on=False)
                # PLAT-1099: Zero ems_power_limit immediately — defense-in-depth.
                # Non-zero limit in charge_pv causes autonomous grid discharge
                # by GoodWe firmware (PLAT-1040).
                with contextlib.suppress(Exception):
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": f"number.goodwe_{adapter.prefix}_ems_power_limit",
                            "value": 0,
                        },
                    )

        elif cmd.battery_action == "grid_charge":
            self._fast_charge_authorized = True
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("charge_pv")
                await adapter.set_fast_charging(
                    on=True,
                    power_pct=100,
                    soc_target=100,
                    authorized=True,
                )

        elif cmd.battery_action == "standby":
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("battery_standby")
                await adapter.set_fast_charging(on=False)

        # PLAT-1099: EMS enforcement moved to _enforce_ems_modes() — runs
        # from main update loop EVERY cycle, even when Grid Guard acts.
        await self._enforce_ems_modes()

        # P0-FIX: Record decision so sensor.carma_box_decision updates
        await self._record_decision(
            state,
            action=cmd.battery_action,
            reason=cmd.reason,
            discharge_w=cmd.battery_discharge_w,
        )

        # Discharge drift-guard: detect when battery doesn't deliver
        self._check_discharge_drift(state, cmd)

        # ── Natt-EV-workflow: starta EV + urladdning automatiskt ──
        is_night = now.hour >= DEFAULT_NIGHT_START or now.hour < DEFAULT_NIGHT_END
        # Ensure Easee initialized EVERY cycle when EV plugged
        # Easee Cloud re-enables smart_charging autonomously → force re-init
        if self.ev_adapter:
            plug_state = self.hass.states.get(f"binary_sensor.{self.ev_adapter.prefix}_plug")
            if plug_state and plug_state.state == "on":
                await self.ev_adapter.ensure_initialized(force=True)

        ev_connected = bool(self.ev_adapter.cable_locked) if self.ev_adapter else False
        ev_soc = state.ev_soc if state.ev_soc >= 0 else -1
        ev_phase = int(opts.get("ev_phase_count", 3))

        if not hasattr(self, "_night_ev_active"):
            self._night_ev_active = False
        if not hasattr(self, "_ev_paused_for_appliance"):
            self._ev_paused_for_appliance = False

        # ── Price-aware discharge: should we discharge NOW? ──
        try:
            from .core.planner import should_discharge_now

            nordpool = self.hass.states.get(
                opts.get("price_entity", "sensor.nordpool_kwh_se3_sek_3_10_025")
            )
            if nordpool:
                today_prices = nordpool.attributes.get("today", [])
                tomorrow_prices = nordpool.attributes.get("tomorrow", [])
                upcoming = [float(p) for p in (today_prices[hour:] + tomorrow_prices) if p][:24]
                current_price = (
                    float(nordpool.state)
                    if nordpool.state not in ["unknown", "unavailable"]
                    else 50.0
                )

                discharge_decision = should_discharge_now(
                    current_price_ore=current_price,
                    upcoming_prices_ore=upcoming,
                    battery_soc_pct=state.total_battery_soc,
                )
                if discharge_decision.get("discharge") and not self._night_ev_active:
                    _LOGGER.info(
                        "PRICE-DISCHARGE: %s (%.0f öre, avg_exp %.0f)",
                        discharge_decision.get("reason", "")[:60],
                        current_price,
                        discharge_decision.get("avg_expensive", 0),
                    )
                    # Set discharge via EMS + power limit
                    rate_kw = discharge_decision.get("recommended_kw", 2.0)
                    rate_w = int(rate_kw * 1000)
                    for adapter in self.inverter_adapters:
                        await adapter.set_ems_mode("discharge_pv")
                    # Set peak_shaving_limit to control grid import
                    # Lower limit = more aggressive discharge
                    ps_limit = max(500, int(2500 - rate_w / 2))
                    for adapter in self.inverter_adapters:
                        if hasattr(adapter, "device_id") and adapter.device_id:
                            with contextlib.suppress(Exception):
                                await self.hass.services.async_call(
                                    "goodwe",
                                    "set_parameter",
                                    {
                                        "device_id": adapter.device_id,
                                        "parameter": "peak_shaving_power_limit",
                                        "value": ps_limit,
                                    },
                                )
                    _LOGGER.info(
                        "PRICE-DISCHARGE: rate=%.1fkW PS=%dW",
                        rate_kw,
                        ps_limit,
                    )
                    self._price_discharge_active = True
                    self._last_battery_action = "discharge"
                elif getattr(self, "_price_discharge_active", False) and not discharge_decision.get(
                    "discharge"
                ):
                    _LOGGER.info("PRICE-DISCHARGE: Stopped — price no longer profitable")
                    for adapter in self.inverter_adapters:
                        await adapter.set_ems_mode("charge_pv")
                    self._price_discharge_active = False
                    self._last_battery_action = "charge_pv"
        except (AttributeError, ValueError, HomeAssistantError):
            _LOGGER.debug("Price-discharge check failed", exc_info=True)

        # ── Night EV State Machine ──────────────────────────────
        from .core.night_ev import NevState, decide_nev

        if not hasattr(self, "_nev_state"):
            self._nev_state = "IDLE"
        if not hasattr(self, "_nev_ramp_start"):
            self._nev_ramp_start = 0.0

        # Use last_known_soc if current unavailable
        effective_ev_soc = ev_soc if ev_soc >= 0 else self._last_known_ev_soc

        # Read appliance power
        _appliance_w = 0.0
        for _eid in (
            self._cfg.get("appliance_disk", "sensor.98_shelly_plug_s_power"),
            self._cfg.get("appliance_tvatt", "sensor.102_shelly_plug_g3_power"),
            self._cfg.get("appliance_tork", "sensor.103_shelly_plug_g3_power"),
        ):
            _appliance_w += self._read_float(_eid, 0.0)

        nev_input = NevState(
            is_night=is_night,
            ev_connected=ev_connected,
            ev_soc=effective_ev_soc,
            ev_target=float(opts.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC)),
            battery_soc=state.total_battery_soc,
            min_soc=self.min_soc,
            grid_w=max(0, state.grid_power_w),
            target_kw=self.target_kw,
            night_weight=float(opts.get("night_weight", DEFAULT_NIGHT_WEIGHT)),
            appliance_w=_appliance_w,
            hour=now.hour,
        )

        new_nev_state, nev_cmd = decide_nev(nev_input, self._nev_state, self._nev_ramp_start)

        if new_nev_state != self._nev_state:
            _LOGGER.info("NEV: %s → %s (%s)", self._nev_state, new_nev_state, nev_cmd.reason)
        self._nev_state = new_nev_state
        self._night_ev_active = new_nev_state in (
            "DISCHARGE_RAMP",
            "EV_CHARGING",
            "APPLIANCE_PAUSE",
        )

        # Execute command
        if nev_cmd.action == "start_discharge":
            self._nev_ramp_start = time.monotonic()
            _nw = float(opts.get("night_weight", DEFAULT_NIGHT_WEIGHT))
            actual_target_w = int(self.target_kw * 1000 / max(0.1, _nw))
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("discharge_pv")
                await adapter.set_fast_charging(on=False)
                with contextlib.suppress(Exception):
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": f"number.goodwe_{adapter.prefix}_ems_power_limit",
                            "value": actual_target_w,
                        },
                    )
        elif nev_cmd.action == "start_ev":
            with contextlib.suppress(Exception):
                await self.hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": "button.easee_home_12840_override_schedule"},
                )
            await self._cmd_ev_start(nev_cmd.ev_amps)
        elif nev_cmd.action == "stop_ev":
            await self._cmd_ev_stop()
        elif nev_cmd.action == "increase_discharge":
            _nw = float(opts.get("night_weight", DEFAULT_NIGHT_WEIGHT))
            actual_target_w = int(self.target_kw * 1000 / max(0.1, _nw))
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("discharge_pv")
                with contextlib.suppress(Exception):
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": f"number.goodwe_{adapter.prefix}_ems_power_limit",
                            "value": actual_target_w,
                        },
                    )

        # ── Execute EV command (from plan) — SKIP if night EV active ──
        if self._night_ev_active:
            pass  # Night EV has control — don't override
        elif cmd.ev_action == "start" and cmd.ev_amps >= DEFAULT_EV_MIN_AMPS:
            if not self._ev_enabled:
                await self._cmd_ev_start(cmd.ev_amps)
            elif cmd.ev_amps != self._ev_current_amps:
                await self._cmd_ev_adjust(cmd.ev_amps)
        elif cmd.ev_action == "stop" and self._ev_enabled:
            await self._cmd_ev_stop()

        # ── EXP-12: Real-time PV Surplus Allocation ──────────────────
        # Priority: House > EV (if home) > Battery > Consumers > Export (NEVER)
        if not is_night and not self._night_ev_active and state.pv_power_w > PV_ACTIVE_THRESHOLD_W:
            try:
                from .core.planner import allocate_pv_surplus

                # Get Solcast remaining hourly forecast
                solcast_today = self.hass.states.get("sensor.solcast_pv_forecast_forecast_today")
                hourly_pv_remaining: list[float] = []
                if solcast_today:
                    detail = solcast_today.attributes.get("detailedForecast", [])
                    for j in range(0, len(detail), 2):
                        kw = detail[j].get("pv_estimate", 0) if isinstance(detail[j], dict) else 0
                        hourly_pv_remaining.append(float(kw))
                if len(hourly_pv_remaining) > hour:
                    hourly_pv_remaining = hourly_pv_remaining[hour:]

                # EXP-13: Tempest PV confidence via core function
                from .core.planner import calculate_pv_confidence

                pv_conf = 1.0
                if self.weather_adapter:
                    try:
                        solcast_current = hourly_pv_remaining[0] if hourly_pv_remaining else 0
                        pv_conf = calculate_pv_confidence(
                            pressure_mbar=self.weather_adapter.pressure_mbar,
                            solar_radiation_wm2=self.weather_adapter.solar_radiation_wm2,
                            solcast_estimate_kw=solcast_current,
                            hour=hour,
                        )
                    except (AttributeError, ValueError, ZeroDivisionError):
                        _LOGGER.debug("Suppressed error", exc_info=True)

                # Weekend/workday detection
                is_workday = now.weekday() < 5  # Mon-Fri

                # Sunset hour estimate (March-Sep: ~19-21, Oct-Feb: ~15-17)
                sunset_h = 19 if now.month in (3, 4, 5, 6, 7, 8, 9) else 16
                hours_to_sunset = max(0, sunset_h - hour)

                # BMS max charge rate
                bat_max_charge = 5000
                if self.inverter_adapters:
                    bat_max_charge = sum(a.max_charge_w or 5000 for a in self.inverter_adapters)

                pv_alloc = allocate_pv_surplus(
                    pv_now_w=state.pv_power_w,
                    grid_now_w=state.grid_power_w,
                    house_consumption_w=max(500, state.grid_power_w + state.pv_power_w),
                    battery_soc_pct=state.total_battery_soc,
                    battery_cap_kwh=float(opts.get("battery_1_kwh", 15))
                    + float(opts.get("battery_2_kwh", 5)),
                    ev_soc_pct=float(ev_soc) if ev_soc >= 0 else -1,
                    ev_connected=ev_connected,
                    ev_target_pct=float(opts.get("ev_target_soc", 75)),
                    is_workday=is_workday,
                    hours_to_sunset=hours_to_sunset,
                    hourly_pv_remaining_kw=hourly_pv_remaining,
                    pv_confidence=pv_conf,
                    battery_max_charge_w=bat_max_charge,
                )

                # Save for dashboard
                self._pv_allocation = {
                    "timestamp": now.isoformat(),
                    "ev_action": pv_alloc.ev_action,
                    "ev_amps": pv_alloc.ev_amps,
                    "battery_action": pv_alloc.battery_action,
                    "battery_target_w": pv_alloc.battery_target_w,
                    "consumers_action": pv_alloc.consumers_action,
                    "surplus_w": round(pv_alloc.surplus_w),
                    "will_export": pv_alloc.will_export,
                    "reason": pv_alloc.reason,
                    "is_workday": is_workday,
                    "pv_confidence": round(pv_conf, 2),
                }

                # Execute EV decision
                if pv_alloc.ev_action == "charge" and pv_alloc.ev_amps >= DEFAULT_EV_MIN_AMPS:
                    if not self._ev_enabled:
                        _LOGGER.info(
                            "SOLAR-EV: start %dA (surplus %.0fW, bat %.0f%%)",
                            pv_alloc.ev_amps,
                            pv_alloc.surplus_w,
                            state.total_battery_soc,
                        )
                        await self._cmd_ev_start(pv_alloc.ev_amps)
                    elif pv_alloc.ev_amps != self._ev_current_amps:
                        await self._cmd_ev_adjust(pv_alloc.ev_amps)
                elif (
                    pv_alloc.ev_action != "charge"
                    and self._ev_enabled
                    and not self._night_ev_active
                ):
                    _LOGGER.info("SOLAR-EV: stop (reason: %s)", pv_alloc.reason)
                    await self._cmd_ev_stop()

                # EXP-10: Check for unexpected disconnect
                if self.ev_adapter:
                    disc_alert = self.ev_adapter.check_unexpected_disconnect(
                        was_charging=self._ev_enabled and self._ev_current_amps > 0
                    )
                    if disc_alert:
                        _LOGGER.warning("EV-ALERT: %s", disc_alert)

                # EXP-05: Auto-recover from blocked states
                if self.ev_adapter and ev_connected and self.ev_adapter.needs_recovery:
                    recovery = await self.ev_adapter.try_recover()
                    if recovery:
                        _LOGGER.warning("EV-RECOVERY: %s", recovery)

            except (HomeAssistantError, AttributeError, ValueError):
                _LOGGER.debug("SOLAR-EV: allocation failed", exc_info=True)

        # ── Surplus chain — ALWAYS runs ──────────────────────────
        if not hasattr(self, "_surplus_hysteresis"):
            from .core.surplus_chain import HysteresisState

            self._surplus_hysteresis = HysteresisState()

        consumers = self._build_surplus_consumers(state)
        surplus_cfg = SurplusConfig(start_delay_s=60, stop_delay_s=180)

        if state.grid_power_w < -100:
            # Exporting → allocate surplus to consumers
            surplus_w = abs(state.grid_power_w)
            result = allocate_surplus(
                surplus_w,
                consumers,
                self._surplus_hysteresis,
                surplus_cfg,
            )
            await self._execute_surplus_allocations(result.allocations)
        elif state.grid_power_w > 100:
            # Importing → reduce consumers if over target
            is_night = now.hour >= DEFAULT_NIGHT_START or now.hour < DEFAULT_NIGHT_END
            weight = DEFAULT_NIGHT_WEIGHT if is_night else 1.0
            viktat_kw = max(0, state.grid_power_w) / 1000 * weight
            if viktat_kw > self.target_kw * 1.05:
                deficit_w = (viktat_kw - self.target_kw) / weight * 1000
                reductions = should_reduce_consumers(
                    deficit_w,
                    consumers,
                    self._surplus_hysteresis,
                    surplus_cfg,
                )
                await self._execute_surplus_allocations(reductions)

        # ── Replan check ────────────────────────────────────────
        if not hasattr(self, "_replan_deviation_count"):
            self._replan_deviation_count = 0

        needs_replan, self._replan_deviation_count = check_replan_needed(
            plan_action,
            exec_state,
            self._replan_deviation_count,
        )
        if needs_replan:
            _LOGGER.info("V2 EXEC: Avvikelse → omplanering")
            self._plan_counter = PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS

    def _build_surplus_consumers(self, state: CarmaboxState) -> list[Any]:
        """Build surplus consumer list from HA entities."""
        from .core.surplus_chain import ConsumerType, SurplusConsumer

        opts = self._cfg
        ev_phase = int(opts.get("ev_phase_count", 3))

        consumers = []
        # EV — highest PV surplus priority
        ev_power = state.ev_power_w
        consumers.append(
            SurplusConsumer(
                "ev",
                "EV",
                priority=1,
                type=ConsumerType.VARIABLE,
                min_w=230 * ev_phase * DEFAULT_EV_MIN_AMPS,
                max_w=230 * ev_phase * DEFAULT_EV_MAX_AMPS,
                current_w=ev_power,
                is_running=ev_power > 100,
                phase_count=ev_phase,
            )
        )
        # Battery — charge from surplus
        bat_power = abs(min(0, state.battery_power_1)) + abs(min(0, state.battery_power_2))
        bat_full = state.battery_soc_1 >= 99 and (
            state.battery_soc_2 < 0 or state.battery_soc_2 >= 99
        )
        consumers.append(
            SurplusConsumer(
                "battery",
                "Batteri",
                priority=2,
                type=ConsumerType.VARIABLE,
                min_w=DEFAULT_BAT_MIN_CHARGE_W,
                max_w=DEFAULT_BAT_MAX_CHARGE_W,
                current_w=bat_power,
                # PLAT-1134: ready if charging OR not full
                is_running=bat_power > 100 or not bat_full,
            )
        )
        # Miner
        miner_entity = self._cfg.get("miner_power_entity", "sensor.shelly1pmg4_a085e3bd1e60_power")
        miner_w = self._read_float(miner_entity)
        consumers.append(
            SurplusConsumer(
                "miner",
                "Miner",
                priority=5,
                type=ConsumerType.ON_OFF,
                min_w=400,
                max_w=500,
                current_w=miner_w,
                is_running=miner_w > 50,
                entity_switch="switch.shelly1pmg4_a085e3bd1e60",
            )
        )
        return consumers

    async def _execute_surplus_allocations(self, allocations: list[Any]) -> None:
        """Execute surplus chain allocations."""
        await self._execution_engine.execute_surplus_allocations(allocations)

    # ── PLAT-1099: EMS Mode Enforcement — runs EVERY 30s cycle ──────
    # Extracted from _execute_v2() so it runs even when Grid Guard acts.
    # Previously, grid_guard_acted=True skipped _execute_v2() entirely,
    # leaving stale EMS modes (e.g., manual discharge_pv) uncorrected.

    async def _enforce_ems_modes(self) -> None:
        """Enforce EMS modes every cycle (PLAT-1099)."""
        await self._execution_engine.enforce_ems_modes()

    async def _execute_grid_guard_commands(
        self,
        commands: list[dict[str, Any]],
        state: CarmaboxState,
    ) -> None:
        """Execute Grid Guard commands — actually control hardware."""
        for cmd in commands:
            action = cmd.get("action", "")
            try:
                if action == "set_ems_mode":
                    bat_id = cmd.get("battery_id", "")
                    mode = cmd.get("mode", "battery_standby")
                    adapter = next(
                        (a for a in self.inverter_adapters if a.prefix == bat_id),
                        None,
                    )
                    if adapter:
                        await adapter.set_ems_mode(mode)
                        _LOGGER.info("GRID GUARD: %s → EMS %s", bat_id, mode)

                elif action == "set_fast_charging":
                    bat_id = cmd.get("battery_id", "")
                    on = cmd.get("on", False)
                    adapter = next(
                        (a for a in self.inverter_adapters if a.prefix == bat_id),
                        None,
                    )
                    if adapter:
                        await adapter.set_fast_charging(on=on)
                        _LOGGER.info("GRID GUARD: %s → fast_charging=%s", bat_id, on)

                elif action == "pause_ev":
                    if self._ev_enabled:
                        await self._cmd_ev_stop()
                        _LOGGER.info("GRID GUARD: EV pausad")

                elif action == "reduce_ev":
                    new_amps = cmd.get("amps", DEFAULT_EV_MIN_AMPS)
                    if self._ev_enabled and new_amps != self._ev_current_amps:
                        await self._cmd_ev_adjust(new_amps)
                        _LOGGER.info(
                            "GRID GUARD: EV sänkt till %dA",
                            new_amps,
                        )

                elif action == "increase_discharge":
                    watts = cmd.get("watts", 0)
                    _LOGGER.info("GRID GUARD: Öka urladdning %dW", watts)
                    # Proportional split handled by battery balancer (Fas 2)
                    # For now: split evenly
                    adapters = self.inverter_adapters
                    per_adapter = watts // max(1, len(adapters))
                    for adapter in adapters:
                        await adapter.set_ems_mode("discharge_pv")
                        # ems_power_limit styr max grid import
                        # Lägre limit = mer urladdning
                        await self.hass.services.async_call(
                            "number",
                            "set_value",
                            {
                                "entity_id": f"number.goodwe_{adapter.prefix}_ems_power_limit",
                                "value": max(
                                    0,
                                    int(state.grid_power_w / 1000 - per_adapter) * 1000,
                                ),
                            },
                        )

                elif action == "switch_off":
                    entity = cmd.get("entity", "")
                    if entity:
                        await self.hass.services.async_call(
                            "switch",
                            "turn_off",
                            {"entity_id": entity},
                        )
                        _LOGGER.info("GRID GUARD: %s → OFF", entity)

                elif action == "set_hvac_off":
                    entity = cmd.get("entity", "")
                    if entity:
                        await self.hass.services.async_call(
                            "climate",
                            "set_hvac_mode",
                            {"entity_id": entity, "hvac_mode": "off"},
                        )
                        _LOGGER.info("GRID GUARD: %s → OFF", entity)

            except Exception as err:
                _LOGGER.error(
                    "GRID GUARD: Kommando %s misslyckades: %s",
                    action,
                    err,
                    exc_info=True,
                )

    def _evaluate_grid_guard(self, state: CarmaboxState) -> GridGuardResult:
        """Build Grid Guard input from current state and evaluate."""
        from .core.grid_guard import BatteryState, Consumer

        now = datetime.now()
        opts = self._cfg

        # Read Ellevio weighted timmedel from HA sensor
        viktat_kw = self._read_float(
            self._cfg.get("ellevio_timmedel_entity", "sensor.ellevio_viktad_timmedel_pagaende"),
            0.0,
        )

        # Battery states
        adapters = self.inverter_adapters
        batteries = []
        for i, adapter in enumerate(adapters):
            bat_id = adapter.prefix if adapter else f"bat_{i}"
            soc = state.battery_soc_1 if i == 0 else state.battery_soc_2
            power = state.battery_power_1 if i == 0 else state.battery_power_2
            cap = float(opts.get(f"battery_{i + 1}_kwh", 15.0 if i == 0 else 5.0))
            min_soc = float(opts.get("battery_min_soc", 15))
            temp = (
                state.battery_min_cell_temp_1
                if i == 0
                else getattr(state, "battery_min_cell_temp_2", 15.0)
            ) or 15.0
            ems = adapter.ems_mode if adapter else ""
            fc = adapter.fast_charging_on if adapter else False
            avail = max(0, (soc - min_soc) / 100 * cap)

            batteries.append(
                BatteryState(
                    id=bat_id,
                    soc=soc,
                    power_w=power,
                    cell_temp_c=temp,
                    ems_mode=ems,
                    fast_charging_on=fc,
                    available_kwh=avail,
                )
            )

        # Controllable consumers for action ladder
        consumers = []
        consumer_defs = [
            (
                "vp_kontor",
                "sensor.kontor_varmepump_alltid_pa_switch_0_power",
                "",
                "climate.kontor_ac",
                1,
            ),
            (
                "miner",
                "sensor.shelly1pmg4_a085e3bd1e60_power",
                "switch.shelly1pmg4_a085e3bd1e60",
                "",
                2,
            ),
            (
                "elvarmare_pool",
                "sensor.shellypro1pm_30c6f7826520_power",
                "switch.shellypro1pm_30c6f7826520",
                "",
                3,
            ),
            (
                "vp_pool",
                "sensor.shellypro1pm_a0dd6c9ecfd8_power",
                "switch.shellypro1pm_a0dd6c9ecfd8",
                "",
                4,
            ),
        ]
        for cid, power_sensor, switch, climate, prio in consumer_defs:
            power = self._read_float(power_sensor, 0.0)
            consumers.append(
                Consumer(
                    id=cid,
                    name=cid,
                    power_w=power,
                    is_active=power > 50,
                    priority_shed=prio,
                    entity_switch=switch,
                    entity_climate=climate,
                )
            )

        # Kontor temperature
        kontor_temp = 20.0
        climate_state = self.hass.states.get("climate.kontor_ac")
        if climate_state:
            kontor_temp = float(climate_state.attributes.get("current_temperature", 20.0) or 20.0)

        ev_phase = int(opts.get("ev_phase_count", 3))

        return self._grid_guard.evaluate(
            viktat_timmedel_kw=viktat_kw,
            grid_import_w=max(0, state.grid_power_w),
            hour=now.hour,
            minute=now.minute,
            ev_power_w=state.ev_power_w,
            ev_amps=self._ev_current_amps,
            ev_phase_count=ev_phase,
            batteries=batteries,
            consumers=consumers,
            kontor_temp_c=kontor_temp,
            timestamp=time.monotonic(),
            fast_charge_authorized=getattr(self, "_fast_charge_authorized", False),
        )

    @property
    def slots(self) -> list[SchedulerHourSlot]:
        """Convert HourPlan -> SchedulerHourSlot-compatible for sensor.py."""
        from .optimizer.models import SchedulerHourSlot

        return [
            SchedulerHourSlot(
                hour=p.hour,
                action=p.action,
                battery_kw=p.battery_kw,
                ev_kw=p.ev_kw,
                ev_amps=0,
                miner_on=False,
                grid_kw=p.grid_kw,
                weighted_kw=p.weighted_kw,
                pv_kw=p.pv_kw,
                consumption_kw=p.consumption_kw,
                price=p.price,
                battery_soc=p.battery_soc,
                ev_soc=p.ev_soc,
                constraint_ok=True,
                reasoning="",
            )
            for p in self.plan
        ]

    async def _async_update_data(self) -> CarmaboxState:
        """Fetch data, run optimizer, execute plan."""
        try:
            async with asyncio.timeout(120):  # 120s watchdog — prevents hanging forever
                return await self._async_update_data_inner()
        except TimeoutError:
            self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
            self._last_error_msg = "TIMEOUT 120s"
            _LOGGER.error(
                "CARMA Box: update cycle TIMEOUT after 120s (%d consecutive)",
                self._consecutive_errors,
                exc_info=True,
            )
            return getattr(self, "data", None) or CarmaboxState()
        except Exception as err:
            self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
            self._last_error_msg = f"{type(err).__name__}: {str(err)[:100]}"
            _LOGGER.error(
                "CARMA Box update failed (%d consecutive): %s",
                self._consecutive_errors,
                err,
                exc_info=True,
            )
            # Write heartbeat with error info (inner didn't get to write it)
            with contextlib.suppress(Exception):
                import json as _ej

                _ehb = {
                    "timestamp": datetime.now().isoformat(),
                    "state": "ERROR",
                    "errors": self._consecutive_errors,
                    "last_error": getattr(self, "_last_error_msg", ""),
                    "plan_step": getattr(self, "_last_plan_step", ""),
                }
                with open("/config/carmabox-heartbeat.json", "w") as _ef:
                    _ej.dump(_ehb, _ef)
            # ALDRIG raise UpdateFailed — returnera degraded state, retry nästa cykel
            err_n = CONSECUTIVE_ERROR_LOG_INTERVAL
            if self._consecutive_errors >= err_n and self._consecutive_errors % err_n == 0:
                _LOGGER.error(
                    "CARMA Box: %d consecutive failures — DEGRADED but CONTINUING",
                    self._consecutive_errors,
                )
            return getattr(self, "data", None) or CarmaboxState()

    async def _async_update_data_inner(self) -> CarmaboxState:
        """Inner update — wrapped by timeout in _async_update_data."""
        try:
            now = datetime.now()

            # EXP-DEPLOY: Runtime executor toggle check (every cycle)
            ha_toggle = self.hass.states.get("input_boolean.carma_ev_executor_enabled")
            if ha_toggle:
                want_exec = ha_toggle.state == "on"
                if want_exec and not self.executor_enabled and self._has_feature("executor"):
                    self.executor_enabled = True
                    for adapter in self.inverter_adapters:
                        adapter._analyze_only = False  # type: ignore[attr-defined]
                    if self.ev_adapter:
                        self.ev_adapter._analyze_only = False  # type: ignore[attr-defined]
                    _LOGGER.warning("CARMA Box: Executor ACTIVATED via HA toggle")
                elif not want_exec and self.executor_enabled:
                    self.executor_enabled = False
                    for adapter in self.inverter_adapters:
                        adapter._analyze_only = True  # type: ignore[attr-defined]
                    if self.ev_adapter:
                        self.ev_adapter._analyze_only = True  # type: ignore[attr-defined]
                    _LOGGER.warning("CARMA Box: Executor DEACTIVATED via HA toggle")

            # ── RC-2: STARTUP SAFETY — fast_charging OFF + standby ──
            # Körs varje cykel tills BEKRÄFTAT att fast_charging=OFF
            if not getattr(self, "_startup_safety_confirmed", False):
                all_off = True
                for adapter in self.inverter_adapters:
                    try:
                        # Kolla om fast_charging fortfarande ON
                        fc_entity = f"switch.goodwe_fast_charging_switch_{adapter.prefix}"
                        fc_state = self.hass.states.get(fc_entity)
                        if fc_state and fc_state.state == "on":
                            _LOGGER.warning(
                                "STARTUP SAFETY: %s fast_charging=ON → stänger av",
                                adapter.prefix,
                            )
                            await adapter.set_fast_charging(on=False)
                            await adapter.set_ems_mode("battery_standby")
                            all_off = False
                        elif fc_state is None:
                            all_off = False  # Sensor inte redo ännu
                    except HomeAssistantError:
                        _LOGGER.error(
                            "STARTUP SAFETY: %s — adapter ej redo",
                            adapter.prefix,
                            exc_info=True,
                        )
                        all_off = False
                self._fast_charge_authorized = False
                if all_off:
                    self._startup_safety_confirmed = True
                    _LOGGER.info("STARTUP SAFETY: Bekräftat — alla fast_charging OFF")
                    # Recover night EV if it was active before restart
                    if getattr(self, "_night_ev_active", False):
                        _LOGGER.info("STARTUP SAFETY: Återställer natt-EV efter restart")
                        try:
                            await self.hass.services.async_call(
                                "button",
                                "press",
                                {"entity_id": "button.easee_home_12840_override_schedule"},
                            )
                            await self.hass.services.async_call(
                                "switch",
                                "turn_on",
                                {"entity_id": "switch.easee_home_12840_is_enabled"},
                            )
                            # PLAT-1032: max_limit removed
                            # adapter handles via ensure_initialized()
                        except HomeAssistantError:
                            _LOGGER.error("STARTUP SAFETY: EV recovery misslyckades", exc_info=True)

            # Restore persistent state on first run
            if not self._savings_loaded:
                self._savings_loaded = True
                await self._async_restore_savings()
            if not self._consumption_loaded:
                self._consumption_loaded = True
                await self._async_restore_consumption()
            if not self._predictor_loaded:
                self._predictor_loaded = True
                await self._async_restore_predictor()
            # CARMA-P0-FIXES Task 4: Restore runtime + ledger BEFORE first _execute cycle
            if not self._runtime_loaded:
                self._runtime_loaded = True
                await self._async_restore_runtime()
            if not self._ledger_loaded:
                self._ledger_loaded = True
                await self._async_restore_ledger()

            old_month = self.savings.month
            self.savings = reset_if_new_month(self.savings, now)
            if self.savings.month != old_month:
                self._ellevio_monthly_hourly_peaks = []
            self.report_collector = reset_report_month(self.report_collector, now)
            self._reset_daily_counters_if_new_day(now)
            if not self._avg_price_initialized:
                self._update_daily_avg_price()
                self._avg_price_initialized = True

            # EV startup: set safe fallback + disable (PLAT-949)
            if not self._ev_initialized and self.ev_adapter:
                self._ev_initialized = True
                # IT-2009: Restart-resilient EV startup
                # Wait for Easee integration to be available
                easee_ready = self.ev_adapter and self.ev_adapter.status != ""
                if not easee_ready:
                    _LOGGER.info("CARMA: EV startup — Easee not ready yet, deferring")
                    self._ev_initialized = False  # retry next cycle
                elif self._ev_enabled:
                    # Was charging before restart — RESUME, dont stop
                    _LOGGER.info(
                        "CARMA: EV was charging before restart — resuming %dA",
                        self._ev_current_amps or DEFAULT_EV_MIN_AMPS,
                    )
                    await self.ev_adapter.ensure_initialized()
                    await self.ev_adapter.set_current(self._ev_current_amps or DEFAULT_EV_MIN_AMPS)
                    await self.ev_adapter.enable()
                else:
                    # Was not charging — just initialize adapter safely
                    _LOGGER.info("CARMA: EV startup — idle, initializing adapter")
                    await self.ev_adapter.ensure_initialized()
            self.safety.update_heartbeat()

            # External heartbeat: write to /config/ for independent monitoring (LXC 506)
            # IT-2467: Changed from /mnt/solutions/ (not mounted in HA container)
            try:
                import json as _json

                _hb = {
                    "timestamp": datetime.now().isoformat(),
                    "state": (self._last_command.value if self._last_command else "starting"),
                    "target_kw": round(self.target_kw, 2),
                    "ev_enabled": self._ev_enabled,
                    "version": "4.6.0",
                    "plan_hours": len(self.plan),
                    "plan_step": getattr(self, "_last_plan_step", ""),
                    "errors": getattr(self, "_consecutive_errors", 0),
                    "last_error": getattr(self, "_last_error_msg", ""),
                    "disabled": list(self._disabled_methods.keys()),
                    "predictor_samples": self.predictor.total_samples,
                    "predictor_trained": self.predictor.is_trained,
                    "predictor_coverage_pct": self.predictor.data_coverage_pct,
                    "predictor_mae": self.predictor.mean_absolute_error,
                }
                with open("/config/carmabox-heartbeat.json", "w") as _f:
                    _json.dump(_hb, _f)
            except (OSError, TypeError, ValueError):
                _LOGGER.debug("Non-critical operation failed", exc_info=True)

            # IT-2467: MQTT heartbeat for external watchdog
            try:
                _hub = getattr(self, "_hub", None)
                if _hub:
                    _hub.publish_status(version="4.6.0")
            except (OSError, AttributeError):
                _LOGGER.debug("Non-critical operation failed", exc_info=True)

            # License check (every 6h — Hub handshake)
            await self._check_license()

            # PLAT-972: Self-healing — check GoodWe config entries
            await self._self_heal_goodwe_entries()
            # PLAT-972: Self-healing — detect external EV changes
            # _self_heal_ev_tamper() BORTTAGEN — motarbetade V2 natt-EV

            # K3 (PLAT-945): Deferred write-verify — check pending verifications
            # from the previous cycle (Modbus has had 30s to propagate).
            self._run_deferred_write_verifies()

            state = self._collect_state()

            # ── LAYER 0: Grid Guard — runs FIRST, every cycle ──
            self._grid_guard_result = self._evaluate_grid_guard(state)

            if self._grid_guard_result.invariant_violations:
                _LOGGER.warning(
                    "GRID GUARD FÖRBUD: %s",
                    "; ".join(self._grid_guard_result.invariant_violations),
                )
                await self._execute_grid_guard_commands(
                    self._grid_guard_result.commands,
                    state,
                )

            if self._grid_guard_result.status in ("WARNING", "CRITICAL"):
                _LOGGER.warning(
                    "GRID GUARD %s: projected=%.2f kW, headroom=%.2f kW, reason=%s",
                    self._grid_guard_result.status,
                    self._grid_guard_result.projected_kw,
                    self._grid_guard_result.headroom_kw,
                    self._grid_guard_result.reason,
                )
                await self._execute_grid_guard_commands(
                    self._grid_guard_result.commands,
                    state,
                )

            if self._grid_guard_result.replan_needed:
                _LOGGER.info("GRID GUARD: Triggar omplanering")
                self._plan_counter = PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS

            self._plan_counter += 1
            if self._plan_counter >= PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS:
                self._plan_counter = 0
                self._generate_plan(state)
                self._check_repair_issues()

            # Plan self-correction — adjust if actual deviates >50% from plan for 3+ cycles
            self._check_plan_correction(state)

            # Breach Prevention Monitor — runs every cycle (30s)
            self._safe_call("update_hourly_meter", self._update_hourly_meter, state)

            # _execute_v2 runs ALWAYS — contains night-EV, price discharge,
            # surplus chain, and PV allocation. Grid Guard restricts actions
            # inside _execute_v2 (via headroom), doesn't block the whole method.
            await self._execute_v2(state)

            # PLAT-1099: EMS enforcement runs EVERY cycle — even when Grid
            # Guard acted and _execute_v2 was skipped.  Catches stale
            # manual overrides (e.g., discharge_pv left from yesterday) and
            # INV-2 crosscharge at EMS level.
            await self._enforce_ems_modes()

            await self._watchdog(state)
            # IT-2465: Non-critical methods wrapped with isolation
            self._safe_call("track_shadow", self._track_shadow, state)
            self._safe_call("track_savings", self._track_savings, state)
            self._safe_call("track_appliances", self._track_appliances)
            self._safe_call("track_battery_idle", self._track_battery_idle, state)
            self._safe_call("feed_predictor_ml", self._feed_predictor_ml, state)
            self._safe_call("check_daily_goals", self._check_daily_goals, state)
            await self._async_save_savings()
            await self._async_save_consumption()
            await self._async_save_predictor()
            await self._async_fetch_benchmarking()
            self._consecutive_errors = 0
            return state

        except Exception as err:
            self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
            self._last_error_msg = f"{type(err).__name__}: {str(err)[:150]}"
            _LOGGER.error(
                "CARMA Box update failed (%d consecutive): %s",
                self._consecutive_errors,
                err,
                exc_info=True,
            )
            # Write error heartbeat immediately
            with contextlib.suppress(Exception):
                import json as _ej2

                with open("/config/carmabox-heartbeat.json", "w") as _ef2:
                    _ej2.dump(
                        {
                            "timestamp": datetime.now().isoformat(),
                            "state": "ERROR",
                            "errors": self._consecutive_errors,
                            "last_error": self._last_error_msg,
                        },
                        _ef2,
                    )
            # Degraded mode: return last known state instead of crashing
            # ALDRIG raise UpdateFailed.
            err_n = CONSECUTIVE_ERROR_LOG_INTERVAL
            if self._consecutive_errors >= err_n and self._consecutive_errors % err_n == 0:
                _LOGGER.error(
                    "CARMA Box: %d consecutive failures — DEGRADED but CONTINUING",
                    self._consecutive_errors,
                )
            _LOGGER.warning("CARMA Box: degraded mode — using last known state")
            return getattr(self, "data", None) or CarmaboxState()

    def _check_discharge_drift(self, state: CarmaboxState, cmd: Any) -> None:
        """Detect when battery doesn't deliver expected discharge.

        If grid stays above target despite discharge command for 3+ cycles
        (90s), log P1 alert. Grid ALDRIG over target without detection.
        """
        if not hasattr(cmd, "battery_action") or cmd.battery_action != "discharge":
            self._drift_count = 0
            return
        if cmd.battery_discharge_w < DRIFT_MIN_DISCHARGE_W:
            self._drift_count = 0
            return

        bat_power_1 = max(0, state.battery_power_1)  # positive = discharge
        bat_power_2 = max(0, state.battery_power_2)
        actual_discharge = bat_power_1 + bat_power_2
        expected = cmd.battery_discharge_w
        grid_w = max(0, state.grid_power_w)
        target_w = self.target_kw * 1000

        grid_over = grid_w > target_w * DRIFT_GRID_MARGIN_FACTOR
        discharge_low = actual_discharge < expected * DRIFT_ACTUAL_RATIO_THRESHOLD
        if grid_over and expected > DRIFT_MIN_EXPECTED_W and discharge_low:
            self._drift_count = getattr(self, "_drift_count", 0) + 1
            _LOGGER.error(
                "DISCHARGE DRIFT (%d/%d): expected %dW, actual %dW, grid %.0fW > target %.0fW",
                self._drift_count,
                DRIFT_ESCALATION_CYCLES,
                expected,
                int(actual_discharge),
                grid_w,
                target_w,
            )
            if self._drift_count >= DRIFT_ESCALATION_CYCLES:
                _LOGGER.error(
                    "DISCHARGE DRIFT P1: eskalerar efter %d cykler",
                    DRIFT_ESCALATION_CYCLES,
                )
                # Force discharge on all adapters
                if not hasattr(self, "_drift_tasks"):
                    self._drift_tasks: list[asyncio.Task[bool]] = []
                for adapter in self.inverter_adapters:
                    with contextlib.suppress(Exception):
                        self._drift_tasks.append(
                            asyncio.create_task(adapter.set_ems_mode("discharge_pv"))
                        )
        else:
            self._drift_count = 0

    def _safe_call(
        self, method_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        """IT-2465: Call a non-critical method with isolation.

        If the method raises, disable it for 5 minutes instead of
        crashing the entire coordinator. Re-enables automatically.
        """
        # Check if method is disabled
        re_enable_at = self._disabled_methods.get(method_name, 0)
        if re_enable_at > 0:
            if time.monotonic() < re_enable_at:
                return  # Still disabled
            # Re-enable
            del self._disabled_methods[method_name]
            _LOGGER.info("IT-2465: Re-enabling %s after cooldown", method_name)

        try:
            fn(*args, **kwargs)
        except Exception:  # Safety net: rate-limiter wraps arbitrary methods
            self._disabled_methods[method_name] = (
                time.monotonic() + 300  # 5 min cooldown
            )
            _LOGGER.error(
                "IT-2465: %s crashed — disabled for 5 min. Coordinator continues.",
                method_name,
                exc_info=True,
            )

    def _collect_state(self) -> CarmaboxState:
        """Collect current state from all HA entities — delegates to StateManager."""
        return self._state_mgr.collect_state(
            self.inverter_adapters,
            self.ev_adapter,
            self.target_kw,
            self.plan,
        )

    def _generate_plan(self, state: CarmaboxState) -> None:
        """Generate energy plan from Nordpool + Solcast + consumption."""
        _step = "init"
        try:
            now = datetime.now()
            start_hour = now.hour
            _LOGGER.info("PLANNER START: hour=%d, soc=%.0f%%", start_hour, state.total_battery_soc)
            # Skip plan if battery SoC not yet loaded (GoodWe startup lag)
            if state.total_battery_soc <= 0:
                _LOGGER.warning("PLANNER: SoC=0 — GoodWe not loaded, skipping")
                self._last_plan_step = "skip:soc=0"
                return

            # Collect prices — try primary, fallback to secondary
            _step = "prices"
            price_entity = self._get_entity("price_entity", "sensor.nordpool_kwh_se3_sek_3_10_025")
            price_entity_fallback = self._get_entity("price_entity_fallback", "")
            fallback_price = float(self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
            price_adapter = NordpoolAdapter(self.hass, price_entity, fallback_price)
            today_prices = price_adapter.today_prices
            _LOGGER.warning(
                "PLAN DATA: entity=%s, today[0:3]=%s, fallback=%s, N=%d",
                price_entity,
                today_prices[:3],
                fallback_price,
                len(today_prices),
            )

            # If primary returns all-fallback, try secondary or skip plan
            if all(p == fallback_price for p in today_prices):
                if price_entity_fallback:
                    _LOGGER.info("Primary price source offline, trying fallback")
                    price_adapter = NordpoolAdapter(
                        self.hass, price_entity_fallback, fallback_price
                    )
                    today_prices = price_adapter.today_prices
                if all(p == fallback_price for p in today_prices):
                    _LOGGER.warning(
                        "PLANNER: All prices are fallback (%.0f öre) — skipping plan generation",
                        fallback_price,
                    )
                    return  # Don't generate useless all-idle plan

            tomorrow_prices = price_adapter.tomorrow_prices
            prices = today_prices[start_hour:] + (tomorrow_prices or today_prices)
            self._last_plan_step = f"prices:{today_prices[start_hour]:.0f}ö"

            # Collect PV forecast — today remaining + tomorrow hourly
            _step = "solcast"
            solcast = SolcastAdapter(self.hass)
            pv_today = solcast.today_hourly_kw
            pv_tomorrow = solcast.tomorrow_hourly_kw
            pv_forecast = pv_today[start_hour:] + pv_tomorrow
            _LOGGER.info(
                "PLANNER [solcast]: today=%d entries, tomorrow=%d, today_sum=%.1fkW",
                len(pv_today),
                len(pv_tomorrow),
                sum(pv_today),
            )
            # Fallback: if adapter returns zeros, try reading raw from HA state
            if sum(pv_today) < 0.1:
                _sc = self.hass.states.get("sensor.solcast_pv_forecast_forecast_today")
                if _sc and _sc.state not in ("unavailable", "unknown", ""):
                    _hourly = _sc.attributes.get("detailedHourly", [])
                    if _hourly:
                        pv_today = [0.0] * 24
                        for _entry in _hourly:
                            try:
                                _h = datetime.fromisoformat(_entry["period_start"]).hour
                                pv_today[_h] = _entry.get(
                                    "pv_estimate10", _entry.get("pv_estimate", 0)
                                )
                            except (ValueError, KeyError, TypeError):
                                _LOGGER.debug("Solcast raw parse skip", exc_info=True)
                        pv_tomorrow = solcast.tomorrow_hourly_kw
                        pv_forecast = pv_today[start_hour:] + pv_tomorrow
                        _LOGGER.info(
                            "PLANNER: Solcast adapter=0, raw fallback=%.1fkW", sum(pv_today)
                        )

            if 6 <= start_hour <= 18 and sum(pv_today) < 0.1:
                _LOGGER.warning("PLANNER: Daytime Solcast=0 — continuing (price arbitrage)")
                # Don't skip — price arbitrage works without PV data

            # PLAT-965: Use predictor if trained, else fallback to profile
            _step = "consumption"
            base = self.consumption_profile.get_profile_for_date(now)
            # Pass outdoor temperature for temperature-aware prediction
            # Use last known value (updated by IT-2080 each cycle) — avoids
            # a duplicate sensor read with a hardcoded entity ID here.
            _outdoor_temp_c: float | None = getattr(self, "_last_known_outdoor_temp_c", None)
            if self.predictor.is_trained:
                consumption = self.predictor.predict_24h(
                    start_hour=start_hour,
                    weekday=now.weekday(),
                    month=now.month,
                    fallback_profile=base,
                    outdoor_temp_c=_outdoor_temp_c,
                )
                # Pad to match prices length (predict_24h returns exactly 24)
                consumption = (consumption or base[start_hour:]) + base
                _LOGGER.info(
                    "Plan: ML profile (trained, %d samples, MAE=%.2f, cov=%.0f%%)",
                    self.predictor.total_samples,
                    self.predictor.mean_absolute_error,
                    self.predictor.data_coverage_pct,
                )
            else:
                consumption = base[start_hour:] + base
                _LOGGER.info(
                    "Plan: using static consumption profile (%d/%d samples)",
                    self.predictor.total_samples,
                    24,
                )

            # EV demand — dynamic schedule based on prices + SoC
            _step = "ev_schedule"
            opts = self._cfg
            ev_enabled = opts.get("ev_enabled", False)
            ev_capacity = float(opts.get("ev_capacity_kwh", 98))
            ev_morning_target = float(opts.get("ev_night_target_soc", 75))
            ev_full_days = int(opts.get("ev_full_charge_days", 7))

            # Battery sizes
            bat1_kwh = float(opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH))
            bat2_kwh = float(opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH))
            total_bat_kwh = bat1_kwh + bat2_kwh

            # Battery available for EV support
            battery_kwh_available = max(
                0,
                (
                    (state.battery_soc_1 / 100 * bat1_kwh)
                    + (max(0, state.battery_soc_2) / 100 * bat2_kwh)
                    - (self.min_soc / 100 * total_bat_kwh)
                ),
            )

            # PV forecast for tomorrow (used by EV strategy)
            pv_tomorrow_kwh = solcast.tomorrow_kwh

            daily_consumption = float(
                opts.get("daily_consumption_kwh", DEFAULT_DAILY_CONSUMPTION_KWH)
            )

            # IT-1965: Use last known SoC with derating if current unavailable
            ev_soc_for_plan = state.ev_soc
            if ev_soc_for_plan < 0:
                # Try last known SoC with derating — max 12h old
                derating = float(self._cfg.get("ev_soc_derating", 10.0))
                age_s = time.monotonic() - self._last_known_ev_soc_time
                if self._last_known_ev_soc > 0 and age_s < 43200:  # < 12h
                    ev_soc_for_plan = max(0, self._last_known_ev_soc - derating)
                    _LOGGER.info(
                        "CARMA EV: last known SoC %.0f%% (%.0fh ago) - %.0f%% derating = %.0f%%",
                        self._last_known_ev_soc,
                        age_s / 3600,
                        derating,
                        ev_soc_for_plan,
                    )
                elif self._last_known_ev_soc > 0:
                    _LOGGER.warning(
                        "CARMA EV: last known SoC %.0f%% expired (%.0fh old, max 12h)",
                        self._last_known_ev_soc,
                        age_s / 3600,
                    )
            elif state.ev_soc > 0:
                self._last_known_ev_soc = state.ev_soc
                self._last_known_ev_soc_time = time.monotonic()
                self._last_known_ev_soc_unix = time.time()  # PLAN-01
                # CARMA-QUALITY: Track SoC changes for W6 stuck detection
                if abs(state.ev_soc - self._ev_prev_soc_for_stuck) >= 1.0:
                    self._ev_last_soc_change_t = time.monotonic()
                    self._ev_prev_soc_for_stuck = state.ev_soc
                # Persist to HA helper for restart survival
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "input_number",
                        "set_value",
                        {
                            "entity_id": "input_number.carma_ev_last_known_soc",
                            "value": state.ev_soc,
                        },
                    )
                )

            _step = "ev_calculate"
            if ev_enabled and ev_soc_for_plan >= 0:
                # ML: Log predicted EV usage for tomorrow to aid scheduling visibility
                tomorrow_weekday = (now.weekday() + 1) % 7
                ev_kwh_predicted = self.predictor.predict_ev_usage(tomorrow_weekday)
                _LOGGER.info(
                    "PLANNER [ev]: soc=%.0f%%, target=%.0f%%, ml_predicted_usage=%.1f kWh",
                    ev_soc_for_plan,
                    ev_morning_target,
                    ev_kwh_predicted,
                )
                ev_demand = calculate_ev_schedule(
                    start_hour=start_hour,
                    num_hours=len(prices),
                    ev_soc_pct=ev_soc_for_plan,
                    ev_capacity_kwh=ev_capacity,
                    hourly_prices=prices,
                    hourly_loads=consumption[: len(prices)],
                    target_weighted_kw=self.target_kw,
                    morning_target_soc=ev_morning_target,
                    full_charge_interval_days=ev_full_days,
                    days_since_full_charge=self._days_since_full_charge(),
                    battery_kwh_available=battery_kwh_available,
                    pv_tomorrow_kwh=pv_tomorrow_kwh,
                    daily_consumption_kwh=daily_consumption,
                )
            else:
                ev_demand = [0.0] * len(prices)

            # Calculate target from PV forecast + reserve
            daily_battery_need = float(
                opts.get("daily_battery_need_kwh", DEFAULT_DAILY_BATTERY_NEED_KWH)
            )
            night_weight = float(opts.get("night_weight", DEFAULT_NIGHT_WEIGHT))

            battery_kwh = (state.battery_soc_1 / 100 * bat1_kwh) + (
                max(0, state.battery_soc_2) / 100 * bat2_kwh
            )
            pv_daily = solcast.forecast_daily_3d
            reserve = calculate_reserve(pv_daily, daily_consumption, daily_battery_need)

            # IT-2078: Intraday reserve correction
            # If actual PV << forecast midday, increase reserve
            hour_now = datetime.now().hour
            if 10 <= hour_now <= 15:
                actual_pv_kw = state.pv_power_w / 1000
                forecast_now_kw = getattr(solcast, "power_now_kw", 0.0) or 0.0
                if forecast_now_kw > 1.0 and actual_pv_kw < forecast_now_kw * 0.5:
                    correction = reserve * 0.3  # increase reserve by 30%
                    reserve += correction
                    _LOGGER.info(
                        "CARMA: Intraday PV correction — actual %.1f kW << forecast %.1f kW "
                        "→ reserve +%.1f kWh (now %.1f)",
                        actual_pv_kw,
                        forecast_now_kw,
                        correction,
                        reserve,
                    )
            # IT-2081: Tempest solar radiation vs Solcast — independent cross-check
            tempest_radiation = self.hass.states.get("sensor.tempest_solar_radiation")
            if tempest_radiation and tempest_radiation.state not in (
                "unavailable",
                "unknown",
                "",
            ):
                try:
                    radiation_wm2 = float(tempest_radiation.state)
                    forecast_kw = getattr(solcast, "power_now_kw", 0.0) or 0.0
                    # Approximate: 1 kWp panel ≈ 1000 W/m² at STC
                    # Our panels ~10 kWp → at 500 W/m² expect ~5 kW
                    # Ratio: actual_radiation / expected_for_forecast
                    if forecast_kw > 0.5 and radiation_wm2 > 10:
                        expected_wm2 = forecast_kw / 10.0 * 1000  # rough conversion
                        ratio = radiation_wm2 / expected_wm2 if expected_wm2 > 0 else 1.0
                        if ratio < 0.5:
                            # Much less sun than forecast — increase reserve
                            tempest_correction = reserve * 0.2
                            reserve += tempest_correction
                            _LOGGER.info(
                                "CARMA Tempest: radiation %.0f W/m² vs expected %.0f "
                                "→ ratio %.2f → reserve +%.1f kWh",
                                radiation_wm2,
                                expected_wm2,
                                ratio,
                                tempest_correction,
                            )
                        elif ratio > 1.5:
                            # More sun than forecast — decrease reserve (more aggressive discharge)
                            tempest_reduction = reserve * 0.15
                            reserve = max(0, reserve - tempest_reduction)
                            _LOGGER.info(
                                "CARMA Tempest: radiation %.0f W/m² >> expected %.0f "
                                "→ ratio %.2f → reserve -%.1f kWh (more aggressive)",
                                radiation_wm2,
                                expected_wm2,
                                ratio,
                                tempest_reduction,
                            )
                except (ValueError, TypeError):
                    _LOGGER.debug("Suppressed error", exc_info=True)

            # IT-2080: Tempest pressure trend → weather prediction
            tempest_pressure = self.hass.states.get("sensor.tempest_pressure")
            if tempest_pressure and tempest_pressure.state not in (
                "unavailable",
                "unknown",
                "",
            ):
                try:
                    pressure_hpa = float(tempest_pressure.state)
                    now_ts = time.time()
                    self._pressure_history.append((now_ts, pressure_hpa))
                    cutoff = now_ts - 10800  # 3h
                    self._pressure_history = [
                        (t, p) for t, p in self._pressure_history if t > cutoff
                    ]
                    if len(self._pressure_history) >= 6:
                        oldest = self._pressure_history[0][1]
                        newest = self._pressure_history[-1][1]
                        trend_hpa = newest - oldest
                        if trend_hpa < -3:
                            pressure_correction = reserve * 0.15
                            reserve += pressure_correction
                            _LOGGER.info(
                                "CARMA Tempest: pressure falling %.1f hPa/3h → reserve +%.1f kWh",
                                trend_hpa,
                                pressure_correction,
                            )
                except (ValueError, TypeError):
                    _LOGGER.debug("Suppressed error", exc_info=True)

            self._current_reserve_kwh = reserve

            # IT-2080: Tempest temperature → dynamic house baseload estimate
            tempest_temp = self.hass.states.get("sensor.tempest_temperature")
            if tempest_temp and tempest_temp.state not in (
                "unavailable",
                "unknown",
                "",
            ):
                try:
                    outdoor_c = float(tempest_temp.state)
                    # House needs more power when cold: 1.5 kW base + 0.1 kW per degree below 15°C
                    dynamic_base_kw = 1.5 + max(0, (15.0 - outdoor_c) * 0.1)
                    self._estimated_house_base_kw = round(min(4.0, dynamic_base_kw), 2)
                    self._last_known_outdoor_temp_c: float | None = outdoor_c
                except (ValueError, TypeError):
                    _LOGGER.debug("Suppressed error", exc_info=True)

            ellevio_tak = float(opts.get("ellevio_tak_kw", 4.0))
            target = calculate_target(
                battery_kwh_available=battery_kwh - (self.min_soc / 100 * total_bat_kwh),
                hourly_loads=consumption[: len(prices)],
                hourly_weights=[
                    ellevio_weight((start_hour + i) % 24, night_weight=night_weight)
                    for i in range(len(prices))
                ],
                reserve_kwh=reserve,
            )
            # Target must respect Ellevio subscription limit — never go below
            # a safe margin so EV charging + house load can fit under the cap
            target = max(target, ellevio_tak * 0.85)
            _LOGGER.warning(
                "PLAN DEBUG: bat_soc1=%.1f bat_soc2=%.1f "
                "total=%.1f cap=%.1f target=%.1f prices[0:3]=%s",
                state.battery_soc_1,
                state.battery_soc_2,
                state.total_battery_soc,
                total_bat_kwh,
                target,
                prices[:3],
            )
            self.target_kw = target

            # Opt #1 + #6 + Tempest: Dynamic target based on illuminance + price
            target_day = float(self._cfg.get("target_kw_day", 2.0))
            target_night = float(self._cfg.get("target_kw_night", 4.0))
            hour_now = datetime.now().hour
            pv_kw = state.pv_power_w / 1000

            # Tempest illuminance for precise day/night detection
            tempest_lux = None
            lux_state = self.hass.states.get("sensor.tempest_illuminance")
            if lux_state and lux_state.state not in ("unavailable", "unknown", ""):
                with contextlib.suppress(ValueError, TypeError):
                    tempest_lux = float(lux_state.state)

            if tempest_lux is not None:
                # Illuminance-driven transition (overrides clock)
                if tempest_lux > LUX_DAYLIGHT:
                    target_cap = target_day  # Bright daylight
                elif tempest_lux < LUX_DARK:
                    target_cap = target_night  # Dark / night
                else:
                    # Twilight: linear interpolation LUX_DARK-LUX_DAYLIGHT
                    ratio = (tempest_lux - LUX_DARK) / (LUX_DAYLIGHT - LUX_DARK)
                    target_cap = target_night - ratio * (target_night - target_day)
                # Override: evening peak still gets tight target
                if hour_now >= 17 and state.current_price > 50:
                    target_cap = target_day
            else:
                # Fallback: clock + PV based (original Opt #6)
                if hour_now >= DEFAULT_NIGHT_START or hour_now < DEFAULT_NIGHT_END:
                    target_cap = target_night
                elif (pv_kw > 0.5 and hour_now >= 7 and hour_now < 20) or (
                    hour_now >= 17 and state.current_price > 50
                ):
                    target_cap = target_day
                elif hour_now >= 20:
                    target_cap = target_day + (target_night - target_day) * (hour_now - 20) / 2
                elif hour_now < 7:
                    target_cap = target_night - (target_night - target_day) * (hour_now - 6)
                else:
                    target_cap = target_day
            if self.target_kw > target_cap:
                _LOGGER.debug(
                    "CARMA: target %.1f > cap %.1f (%s) → capped",
                    self.target_kw,
                    target_cap,
                    (
                        "natt"
                        if (hour_now >= DEFAULT_NIGHT_START or hour_now < DEFAULT_NIGHT_END)
                        else "dag"
                    ),
                )
                self.target_kw = target_cap

            # Grid charge config
            grid_charge_threshold = float(
                opts.get("grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD)
            )
            grid_charge_max_soc = float(
                opts.get("grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC)
            )
            battery_efficiency = float(opts.get("battery_efficiency", DEFAULT_BATTERY_EFFICIENCY))
            max_discharge_kw = float(opts.get("max_discharge_kw", DEFAULT_MAX_DISCHARGE_KW))
            max_grid_charge_kw = float(opts.get("max_grid_charge_kw", DEFAULT_MAX_GRID_CHARGE_KW))

            # ── PLAT-969: Multi-day planning ──────────────────────────
            # Read plan horizon from HA helper (default 72h = 3 days)
            plan_horizon_h = DEFAULT_PLAN_HORIZON_HOURS
            horizon_state = self.hass.states.get("input_number.v6_plan_horizon_h")
            if horizon_state and horizon_state.state not in (
                "unknown",
                "unavailable",
                "",
            ):
                with contextlib.suppress(ValueError, TypeError):
                    plan_horizon_h = int(float(horizon_state.state))
            plan_horizon_h = max(24, min(168, plan_horizon_h))

            # AC6: Fail-closed — if Solcast unavailable, fall back to 24h conservative
            _today_kwh = getattr(solcast, "today_kwh", None)
            solcast_available = (isinstance(_today_kwh, int | float) and _today_kwh > 0) or any(
                p > 0 for p in pv_today
            )
            if not solcast_available:
                plan_horizon_h = 24
                pv_forecast = [0.0] * len(prices)  # p10=0 conservative
                _LOGGER.warning(
                    "CARMA PLAT-969: Solcast unavailable → fail-closed: "
                    "24h horizon, PV=0 (conservative)"
                )

            plan_days = max(1, (plan_horizon_h + 23) // 24)

            if plan_days > 1 and solcast_available:
                # Multi-day planning via multiday_planner
                # Build historical mean prices from known prices as fallback (AC3)
                hist_mean: list[float] | None = None
                if today_prices and len(today_prices) >= 24:
                    # Use today's prices as base for historical mean
                    hist_mean = list(today_prices)

                # Get price model if available
                price_model = getattr(self, "_price_model", None)
                pv_correction_model = getattr(self, "_pv_correction", None)

                # Solcast daily forecasts for days 3+
                pv_daily_forecasts = solcast.forecast_daily_3d

                # Consumption profiles
                base_profile = self.consumption_profile.get_profile_for_date(now)
                weekday_profile = base_profile
                weekend_profile = base_profile  # Simplified: same for now

                _step = "multiday_build"
                day_inputs = build_day_inputs(
                    days=plan_days,
                    start_hour=start_hour,
                    start_weekday=now.weekday(),
                    start_month=now.month,
                    known_prices_today=today_prices,
                    known_prices_tomorrow=tomorrow_prices if tomorrow_prices else None,
                    known_pv_today=pv_today,
                    known_pv_tomorrow=solcast.tomorrow_hourly_kw,
                    consumption_profile_weekday=weekday_profile,
                    consumption_profile_weekend=weekend_profile,
                    price_model=price_model,
                    pv_correction=pv_correction_model,
                    pv_daily_estimate=sum(pv_today) if pv_today else 10.0,
                    historical_mean_prices=hist_mean,
                    known_pv_daily=pv_daily_forecasts,
                )

                _step = "multiday_generate"
                multiday = generate_multiday_plan(
                    day_inputs=day_inputs,
                    start_hour=start_hour,
                    battery_soc=state.total_battery_soc,
                    ev_soc=max(0, state.ev_soc) if ev_soc_for_plan >= 0 else -1.0,
                    battery_cap_kwh=total_bat_kwh,
                    battery_min_soc=self.min_soc,
                    battery_efficiency=battery_efficiency,
                    ev_cap_kwh=ev_capacity if ev_enabled else 0.0,
                    night_weight=night_weight,
                    grid_charge_price_threshold=grid_charge_threshold,
                    grid_charge_max_soc=grid_charge_max_soc,
                    max_discharge_kw=max_discharge_kw,
                    max_grid_charge_kw=max_grid_charge_kw,
                )
                self.plan = multiday.hourly_plan

                _LOGGER.info(
                    "CARMA PLAT-969: multi-day plan: %d days, %d hours, "
                    "quality=%s, target=%.1f kW, cost≈%.1f kr, "
                    "%d charge, %d discharge, %d grid_charge",
                    multiday.days,
                    len(self.plan),
                    multiday.data_quality,
                    target,
                    multiday.total_cost_estimate_kr,
                    sum(1 for h in self.plan if h.action == "c"),
                    sum(1 for h in self.plan if h.action == "d"),
                    sum(1 for h in self.plan if h.action == "g"),
                )

                # Write multi-day plan to HA sensor (AC4)
                _step = "write_sensor"
                self._write_plan_to_sensor(multiday, start_hour)

            else:
                # Single-day fallback (original behavior)
                _step = "singleday_generate"
                n = min(len(prices), len(pv_forecast), len(consumption))
                prices = prices[:n]
                pv_forecast = pv_forecast[:n]
                consumption = consumption[:n]
                ev_demand = ev_demand[:n]

                self.plan = generate_plan(
                    num_hours=n,
                    start_hour=start_hour,
                    target_weighted_kw=target,
                    hourly_loads=consumption,
                    hourly_pv=pv_forecast,
                    hourly_prices=prices,
                    hourly_ev=ev_demand,
                    battery_soc=state.total_battery_soc,
                    ev_soc=max(0, state.ev_soc),
                    battery_cap_kwh=total_bat_kwh,
                    battery_min_soc=self.min_soc,
                    battery_efficiency=battery_efficiency,
                    ev_cap_kwh=ev_capacity if ev_enabled else 0.0,
                    night_weight=night_weight,
                    grid_charge_price_threshold=grid_charge_threshold,
                    grid_charge_max_soc=grid_charge_max_soc,
                    max_discharge_kw=max_discharge_kw,
                    max_grid_charge_kw=max_grid_charge_kw,
                    night_ev_active=getattr(self, "_night_ev_active", False),
                )

                _LOGGER.info(
                    "CARMA plan: %d hours, target=%.1f kW, %d charge, %d discharge, %d grid_charge",
                    len(self.plan),
                    target,
                    sum(1 for h in self.plan if h.action == "c"),
                    sum(1 for h in self.plan if h.action == "d"),
                    sum(1 for h in self.plan if h.action == "g"),
                )

                # Write single-day plan to sensor (compact format)
                self._write_plan_to_sensor_single(start_hour)

            self._daily_plans += 1
            self._last_plan_step = f"OK:{len(self.plan)}h"
            # CARMA-P0-FIXES Task 4: Mark runtime as dirty — will be saved in next async_update_data
            self._runtime_dirty = True

        except Exception as err:
            self._last_plan_step = f"CRASH@{_step}"
            _LOGGER.exception("PLANNER CRASH at step '%s': %s", _step, err)
            # Write error state so dashboard shows WHY plan failed
            with contextlib.suppress(Exception):
                import json as _json

                error_plan = _json.dumps(
                    {
                        "error": str(err)[:150],
                        "step": _step,
                        "state": "crashed",
                        "ts": datetime.now().isoformat(),
                    },
                )
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"entity_id": "input_text.v6_battery_plan", "value": error_plan[:255]},
                    )
                )

    def _write_plan_to_sensor(self, multiday: MultiDayPlan, start_hour: int) -> None:
        """Write multi-day plan to input_text.v6_battery_plan (AC4, PLAT-969).

        Compact JSON format optimized for 4096-char limit:
        {"d":"2026-03-29","sh":14,"hz":72,"q":"mixed",
         "h":"iicciiid...","p":[50,45,...],"soc":[80,78,...]}
        """
        import json
        from datetime import date as _date

        plan = multiday.hourly_plan
        if not plan:
            return

        actions = "".join(hp.action for hp in plan)
        prices = [int(hp.price) for hp in plan]
        socs = [hp.battery_soc for hp in plan]

        payload = {
            "d": _date.today().isoformat(),
            "s": "multiday",
            "sh": start_hour,
            "hz": len(plan),
            "q": multiday.data_quality,
            "h": actions,
            "p": prices,
            "soc": socs,
        }

        # Add day summaries if they fit
        if multiday.day_summaries:
            payload["ds"] = [
                {
                    "d": ds.get("day", 0),
                    "ps": ds.get("price_source", ""),
                    "ap": ds.get("avg_price", 0),
                    "es": ds.get("end_soc", 0),
                }
                for ds in multiday.day_summaries
            ]

        raw = json.dumps(payload, separators=(",", ":"))

        # Truncate if exceeds 4096 chars — drop day summaries first, then soc/prices
        if len(raw) > 4096:
            payload.pop("ds", None)
            raw = json.dumps(payload, separators=(",", ":"))
        if len(raw) > 4096:
            payload.pop("soc", None)
            raw = json.dumps(payload, separators=(",", ":"))
        if len(raw) > 4096:
            payload.pop("p", None)
            raw = json.dumps(payload, separators=(",", ":"))

        # input_text max 255 chars — write compact version
        compact = self._compact_plan_string(plan, start_hour)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "input_text",
                "set_value",
                {"entity_id": "input_text.v6_battery_plan", "value": compact[:255]},
            )
        )
        # Write human-readable display text
        self._write_plan_display(plan, start_hour)

    def _write_plan_to_sensor_single(self, start_hour: int) -> None:
        """Write single-day plan to input_text.v6_battery_plan (legacy compat)."""
        plan = self.plan
        if not plan:
            return

        compact = self._compact_plan_string(plan, start_hour)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "input_text",
                "set_value",
                {"entity_id": "input_text.v6_battery_plan", "value": compact[:255]},
            )
        )
        self._write_plan_display(plan, start_hour)

    def _compact_plan_string(self, plan: list[Any], start_hour: int) -> str:
        """Build compact plan string that fits in 255 chars."""
        import json
        from datetime import date as _date

        actions = "".join(hp.action for hp in plan)
        payload = {
            "d": _date.today().isoformat(),
            "sh": start_hour,
            "hz": len(plan),
            "h": actions,
        }
        return json.dumps(payload, separators=(",", ":"))

    def _write_plan_display(self, plan: list[Any], start_hour: int) -> None:
        """Write human-readable plan split by day (today/tomorrow/day3)."""
        # Calculate SoC progressively from ACTUAL current SoC
        opts = self._cfg
        bat_kwh = float(opts.get("battery_1_kwh", 15.0)) + float(opts.get("battery_2_kwh", 5.0))
        efficiency = float(opts.get("battery_efficiency", 0.92))
        # Read actual SoC from HA sensors (self.data may be None during plan generation)
        soc1 = self._read_float(opts.get("battery_soc_1", "sensor.pv_battery_soc_kontor"), 50.0)
        soc2 = self._read_float(opts.get("battery_soc_2", "sensor.pv_battery_soc_forrad"), 50.0)
        bat1_kwh = float(opts.get("battery_1_kwh", 15.0))
        bat2_kwh = float(opts.get("battery_2_kwh", 5.0))
        soc_pct = (soc1 * bat1_kwh + soc2 * bat2_kwh) / bat_kwh if bat_kwh > 0 else 50.0
        soc_kwh = soc_pct / 100 * bat_kwh

        days: dict[str, list[str]] = {"today": [], "tomorrow": [], "day3": []}
        for i, hp in enumerate(plan):
            hour = (start_hour + i) % 24
            hours_from_now = i
            if hours_from_now < (24 - start_hour):
                day_key = "today"
            elif hours_from_now < (24 - start_hour) + 24:
                day_key = "tomorrow"
            else:
                day_key = "day3"

            action_label = {"c": "⚡Ladda", "d": "🔋Urladda", "g": "🔌Nät", "i": "💤"}.get(
                hp.action, hp.action
            )

            # Update SoC based on action
            if hp.action == "c" and hp.battery_kw > 0:
                soc_kwh = min(bat_kwh, soc_kwh + hp.battery_kw * efficiency)
            elif hp.action == "d" and hp.battery_kw < 0:
                soc_kwh = max(0, soc_kwh + hp.battery_kw / efficiency)
            soc_pct = soc_kwh / bat_kwh * 100

            bat_str = f"{abs(hp.battery_kw):.1f}kW" if abs(hp.battery_kw) > 0.1 else ""
            soc_str = f"B{soc_pct:.0f}%"
            # EV: show charging power + expected absolute SoC
            ev_str = f" 🚗{hp.ev_kw:.0f}kW→{hp.ev_soc}%" if hp.ev_kw > 0.1 else ""
            price_str = f"{int(hp.price)}ö" if hp.price > 1 else ""

            # Show ALL hours — compact format to fit 255 chars
            parts = [f"{hour:02d}"]
            if hp.action != "i":
                parts.append(action_label)
            if bat_str:
                parts.append(bat_str)
            parts.append(soc_str)
            if ev_str:
                parts.append(ev_str.strip())
            if price_str:
                parts.append(price_str)
            days[day_key].append(" ".join(parts))

        # Write each day to its own input_text
        entities = {
            "today": "input_text.v6_battery_plan_today",
            "tomorrow": "input_text.v6_battery_plan_tomorrow",
            "day3": "input_text.v6_battery_plan_day3",
        }
        for day_key, entity_id in entities.items():
            entries = days.get(day_key, [])
            display = " | ".join(entries) if entries else "Idle"
            with contextlib.suppress(Exception):
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "input_text",
                        "set_value",
                        {"entity_id": entity_id, "value": display[:255]},
                    )
                )

    async def _execute(self, state: CarmaboxState) -> None:
        """Execute current action based on state.

        ALL commands go through SafetyGuard. No exceptions.

        Core rules (in priority order):
        1. Never discharge during export
        2. SoC 100% → standby
        3. Load > target → discharge to fill gap
        4. Load < target → idle (grid handles it)
        """
        # Reset transient state flags — set True below if conditions met this cycle
        self._taper_active = False
        self._cold_lock_active = False
        # ── GLOBAL SAFETY GATES (every cycle) ──────────────
        heartbeat = self.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard heartbeat stale: %s", heartbeat.reason)
            self._daily_safety_blocks += 1
            return

        rate = self.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard rate limit: %s", rate.reason)
            self._daily_safety_blocks += 1
            return

        # Crosscharge check every cycle (PLAT-946: pass validity flags)
        crosscharge = self.safety.check_crosscharge(
            state.battery_power_1,
            state.battery_power_2,
            power_1_valid=state.battery_power_1_valid,
            power_2_valid=state.battery_power_2_valid,
        )
        if not crosscharge.ok:
            _LOGGER.warning("SafetyGuard crosscharge: %s", crosscharge.reason)
            self._daily_safety_blocks += 1
            await self.notifier.crosscharge_alert(
                state.battery_power_1,
                state.battery_power_2,
            )
            await self._cmd_standby(state, force=True)
            return

        # Read temperature for safety checks
        temp_c = self._read_battery_temp()

        # ── Compute metrics for decision ──────────────────────
        hour = datetime.now().hour
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_weight)
        # H5: grid_power_w IS what Ellevio sees (net import after PV + battery)
        # Don't adjust for EV/PV — they're already in the meter reading
        net_w = max(0, state.grid_power_w)
        weighted_net = net_w * weight
        target_w = self.target_kw * 1000
        pv_kw = state.pv_power_w / 1000
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END

        # ── Build reasoning chain ─────────────────────────────
        reasoning: list[str] = []
        chain: list[dict[str, str]] = []
        period = "natt" if is_night else "dag"
        allowed_import = self.target_kw / weight if weight > 0 else self.target_kw

        # Step 1: Tidpunkt + Ellevio-vikt → tillåten import
        step1 = (
            f"Kl {hour:02d}, {period}, Ellevio-vikt {weight:.1f} "
            f"→ tillåten import {allowed_import:.1f} kW"
        )
        reasoning.append(step1)
        chain.append(
            {
                "step": "tidpunkt",
                "label": "Tidpunkt & Ellevio-vikt",
                "detail": step1,
            }
        )

        # Step 2: Husförbrukning + batteri-stöd = effektiv grid headroom
        house_kw = max(0, state.grid_power_w) / 1000 + pv_kw
        bat_support_kw = 0.0
        if state.battery_power_1 < 0:
            bat_support_kw += abs(state.battery_power_1) / 1000
        if state.battery_power_2 < 0:
            bat_support_kw += abs(state.battery_power_2) / 1000
        headroom_kw = allowed_import - (weighted_net / 1000)
        step2 = (
            f"Hus {house_kw:.1f} kW, batteri-stöd {bat_support_kw:.1f} kW "
            f"→ headroom {headroom_kw:.1f} kW"
        )
        reasoning.append(step2)
        chain.append(
            {
                "step": "headroom",
                "label": "Förbrukning & headroom",
                "detail": step2,
            }
        )

        # Step 3: Pris-tier = vald intensitet
        price_cheap = float(self._cfg.get("price_cheap_ore", DEFAULT_PRICE_CHEAP_ORE))
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))
        if state.current_price < price_cheap:
            tier = "billigt"
            intensity = "passiv — spara batteri"
        elif state.current_price < price_expensive:
            tier = "normalt"
            intensity = "balanserad peak shaving"
        else:
            tier = "dyrt"
            intensity = "aggressiv urladdning"
        step3 = f"Elpris {state.current_price:.0f} öre/kWh — {tier} → {intensity}"
        reasoning.append(step3)
        chain.append(
            {
                "step": "pris",
                "label": "Pris & intensitet",
                "detail": step3,
            }
        )

        # Step 4: SoC-status = behov vs tillgång
        soc_parts = [f"Batteri {state.total_battery_soc:.0f}%"]
        if state.has_battery_2:
            soc_parts.append(
                f"(kontor {state.battery_soc_1:.0f}%, förråd {state.battery_soc_2:.0f}%)"
            )
        if state.has_ev and state.ev_soc >= 0:
            soc_parts.append(f", EV {state.ev_soc:.0f}%")
        step4 = " ".join(soc_parts)
        reasoning.append(step4)
        chain.append(
            {
                "step": "soc",
                "label": "Energistatus",
                "detail": step4,
            }
        )

        # ── RULE 0.5: PV surplus + battery not full → charge_pv ──
        # ONLY charge batteries from PV if we are EXPORTING (PV > house load).
        # If grid is importing, PV doesn't cover house — don't add battery
        # charging load on top (it increases grid import).
        if pv_kw > 0.5 and not state.all_batteries_full and state.is_exporting:
            charge_result = self.safety.check_charge(
                state.battery_soc_1, state.battery_soc_2, temp_c
            )
            if charge_result.ok:
                reasoning.append(f"PV {pv_kw:.1f} kW aktiv, batteri ej fullt → solladda")
                await self._cmd_charge_pv(state)

                # IT-1948: Cold lock detection — BMS blocks ALL charging when cells < 10°C
                if self._is_cold_locked(state):
                    temps = []
                    if state.battery_min_cell_temp_1 is not None:
                        temps.append(f"kontor {state.battery_min_cell_temp_1:.1f}°C")
                    if state.battery_min_cell_temp_2 is not None:
                        temps.append(f"förråd {state.battery_min_cell_temp_2:.1f}°C")
                    temp_str = ", ".join(temps)
                    reasoning.append(f"BMS kall-blockering — min cell {temp_str}, laddning pausad")
                    self._track_rule("RULE_0_5", "bms_cold_lock")
                    await self._record_decision(
                        state,
                        "bms_cold_lock",
                        f"BMS cold lock — {temp_str}, överskott → surplus-kedja (MAX)",
                        reasoning=reasoning,
                    )
                    self._last_command = BatteryCommand.BMS_COLD_LOCK
                    self._cold_lock_active = True
                    # Force MAX surplus chain (target_kw=0) — all PV to loads
                    saved_target = self.target_kw
                    self.target_kw = 0.0
                    await self._execute_ev(state)
                    await self._execute_miner(state)
                    await self._execute_climate(state)
                    await self._execute_pool(state)
                    await self._execute_pool_circulation(state)
                    self.target_kw = saved_target
                    return

                self._cold_lock_active = False
                # IT-1939: Taper detection — if BMS can't accept charge, surplus to loads
                if self._is_in_taper(state):
                    export_w = abs(state.grid_power_w)
                    soc = state.total_battery_soc
                    reasoning.append(
                        f"BMS taper detekterad — {export_w:.0f}W export vid {soc:.0f}% SoC"
                    )
                    self._track_rule("RULE_0_5", "charge_pv_taper")
                    await self._record_decision(
                        state,
                        "charge_pv_taper",
                        f"Taper-mode — BMS tar lite laddning, {export_w:.0f}W → surplus-kedja",
                        reasoning=reasoning,
                    )
                    self._last_command = BatteryCommand.CHARGE_PV_TAPER
                    self._taper_active = True
                    # Force surplus chain with target_kw=0 to maximize absorption
                    saved_target = self.target_kw
                    self.target_kw = 0.0
                    await self._execute_ev(state)
                    await self._execute_miner(state)
                    await self._execute_climate(state)
                    await self._execute_pool(state)
                    await self._execute_pool_circulation(state)
                    self.target_kw = saved_target
                    return

                self._taper_active = False
                self._track_rule("RULE_0_5", "charge_pv")
                await self._record_decision(
                    state,
                    "charge_pv",
                    f"Solladdar — PV {pv_kw:.1f} kW, batteri {state.total_battery_soc:.0f}%",
                    reasoning=reasoning,
                )

                await self._execute_ev(state)
                await self._execute_miner(state)
                await self._execute_climate(state)
                return
            # Charge blocked (e.g. temperature) — not a user-facing issue,
            # just fall through to next rule. Self-healing handles it.
            reasoning.append(f"Laddning blockerad: {charge_result.reason}")

        # ── RULE 1: Never discharge during export ────────────
        if state.is_exporting:
            reasoning.append(f"Exporterar {abs(state.grid_power_w):.0f}W → sol driver allt")
            if not state.all_batteries_full:
                charge_result = self.safety.check_charge(
                    state.battery_soc_1, state.battery_soc_2, temp_c
                )
                if charge_result.ok:
                    step5 = "Solladdar — Ellevio-påverkan: 0 kW (exporterar)"
                    reasoning.append("Batteri ej fullt → solladda")
                    reasoning.append(step5)
                    chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                    await self._cmd_charge_pv(state)

                    # IT-1939: Taper detection in export path too
                    if self._is_in_taper(state):
                        export_w = abs(state.grid_power_w)
                        reasoning.append(
                            f"BMS taper — {export_w:.0f}W export vid {state.total_battery_soc:.0f}%"
                        )
                        self._track_rule("RULE_1", "charge_pv_taper")
                        await self._record_decision(
                            state,
                            "charge_pv_taper",
                            f"Taper — {export_w:.0f}W → surplus-kedja",
                            reasoning=reasoning,
                            reasoning_chain=chain,
                        )
                        self._last_command = BatteryCommand.CHARGE_PV_TAPER
                        self._taper_active = True
                        saved_target = self.target_kw
                        self.target_kw = 0.0
                        await self._execute_ev(state)
                        await self._execute_miner(state)
                        await self._execute_climate(state)
                        await self._execute_pool(state)
                        await self._execute_pool_circulation(state)
                        self.target_kw = saved_target
                        return

                    self._taper_active = False
                    self._track_rule("RULE_1", "charge_pv")
                    await self._record_decision(
                        state,
                        "charge_pv",
                        f"Solladdar — export {abs(state.grid_power_w):.0f}W, "
                        f"PV {pv_kw:.1f} kW, batteri {state.battery_soc_1:.0f}%",
                        reasoning=reasoning,
                        reasoning_chain=chain,
                    )
                else:
                    # Charge blocked during export (e.g. temperature) —
                    # fall through to standby. NOT a user-facing safety issue.
                    step5 = "Standby — laddning ej möjlig, exporterar överskott"
                    reasoning.append(step5)
                    chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                    await self._cmd_standby(state)
                    self._track_rule("RULE_1", "standby")
                    await self._record_decision(
                        state,
                        "standby",
                        f"Standby — {charge_result.reason}, exporterar",
                        reasoning=reasoning,
                        reasoning_chain=chain,
                    )
            else:
                step5 = "Standby — batterier 100%, exporterar överskott, Ellevio-påverkan: 0 kW"
                reasoning.append("Batterier 100% → standby, exporterar överskott")
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_standby(state)
                self._track_rule("RULE_1", "standby")
                await self._record_decision(
                    state,
                    "standby",
                    f"Standby — batterier fulla ({state.battery_soc_1:.0f}%), exporterar",
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
            return

        # ── RULE 1.5: Grid charge at very cheap price ────────
        static_threshold = float(
            self._cfg.get("grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD)
        )
        # IT-2077: Dynamic threshold = min(static, daily_avg * 0.4)
        # Catches cheap hours even in low-price seasons (summer avg ~15 öre)
        dynamic_threshold = self._daily_avg_price * 0.4 if self._daily_avg_price > 0 else 999
        grid_charge_threshold = min(static_threshold, max(5.0, dynamic_threshold))

        # Opt #8: Price arbitrage — if daily spread > 30 öre, charge at bottom 20%
        if len(self.plan) >= 8:
            plan_prices = sorted([h.price for h in self.plan if h.price > 0])
            if len(plan_prices) >= 4:
                cheapest_4 = sum(plan_prices[:4]) / 4
                dearest_4 = sum(plan_prices[-4:]) / 4
                spread = dearest_4 - cheapest_4
                if spread > 30:
                    arb_threshold = plan_prices[len(plan_prices) // 5]
                    if arb_threshold > grid_charge_threshold:
                        grid_charge_threshold = arb_threshold

        grid_charge_max_soc = float(
            self._cfg.get("grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC)
        )
        if (
            state.current_price > 0
            and state.current_price < grid_charge_threshold
            and state.total_battery_soc < grid_charge_max_soc
            and not state.is_exporting
        ):
            reasoning.append(
                f"Pris {state.current_price:.0f} öre "
                f"< {grid_charge_threshold:.0f} → nätladda batteri"
            )
            charge_result = self.safety.check_charge(
                state.battery_soc_1, state.battery_soc_2, temp_c
            )
            if charge_result.ok:
                await self._execution_engine.cmd_grid_charge(state)  # CARMA-P0-FIXES Task 2
                self._track_rule("RULE_1_5", "grid_charge")
                await self._record_decision(
                    state,
                    "grid_charge",
                    f"Nätladdning — {state.current_price:.0f} öre (billigt), "
                    f"batteri {state.total_battery_soc:.0f}%",
                    reasoning=reasoning,
                )
                return

        # ── RULE 1.8: Proactive discharge — eliminate grid import ──
        # Batteries should ALWAYS support the house when SoC is high enough.
        # Don't wait for grid to exceed target — ANY unnecessary grid import
        # when batteries have capacity is wasted money.
        #
        # Aggressiveness scales with SoC, PV, and solar radiation (Tempest):
        # - High radiation (>200 W/m²) OR PV active → aggressive: batteries refill from sun
        # - Low radiation + rain → conservative: save batteries
        # - No weather data → use PV as proxy
        _sun_available = (
            state.solar_radiation_wm2 > 100
            or pv_kw > 0.3
            or (not is_night and state.illuminance_lx > 20000)
        )
        _rain_active = state.rain_mm > 0.5
        if _sun_available and not _rain_active:
            _proactive_min_grid_w = DEFAULT_PROACTIVE_MIN_GRID_W_SUN
            _proactive_soc_threshold = max(self.min_soc + 10, 40.0)
        elif not is_night:
            # Daytime but cloudy/rainy — moderate
            _proactive_min_grid_w = DEFAULT_PROACTIVE_MIN_GRID_W_CLOUDY
            _proactive_soc_threshold = 80.0
        else:
            _proactive_min_grid_w = DEFAULT_PROACTIVE_MIN_GRID_W
            _proactive_soc_threshold = 90.0
        if (
            state.total_battery_soc >= _proactive_soc_threshold
            and net_w > _proactive_min_grid_w
            and weighted_net <= target_w  # NOT already handled by RULE 2
            and not state.is_exporting
            and not is_night  # Night: let grid charge / EV logic handle it
        ):
            # With PV: aggressively target 0W grid (sol fyller tillbaka)
            # Without PV: moderate — just reduce grid, don't drain battery
            proactive_w = int(min(net_w, 5000))  # Match grid import fully
            # IT-2075: Calculate available vs reserve for gating
            bat1_kwh = float(self._cfg.get("battery_1_kwh", 15.0))
            bat2_kwh = float(self._cfg.get("battery_2_kwh", 5.0))
            available_kwh = max(
                0,
                (state.battery_soc_1 - self.min_soc) / 100 * bat1_kwh
                + max(0, (state.battery_soc_2 - self.min_soc) / 100 * bat2_kwh),
            )
            reserve_kwh = getattr(self, "_current_reserve_kwh", 0.0)

            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                self.min_soc,
                state.grid_power_w,
                temp_c,
                reserve_kwh=reserve_kwh,
                available_kwh=available_kwh,
            )
            if result.ok:
                pv_note = (
                    f"PV {pv_kw:.1f} kW → solen fyller tillbaka"
                    if pv_kw > 0.3
                    else "ingen PV → moderat"
                )
                step5 = (
                    f"Proaktiv urladdning {proactive_w}W — SoC {state.total_battery_soc:.0f}% "
                    f"hög, eliminerar {net_w:.0f}W nätimport, {pv_note}"
                )
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_discharge(state, proactive_w)
                await self.notifier.proactive_discharge_started(
                    proactive_w,
                    state.total_battery_soc,
                    net_w,
                    pv_kw,
                )
                self._track_rule("RULE_1_8", "proactive_discharge")
                await self._execute_miner(state)
                await self._execute_ev(state)
                await self._record_decision(
                    state,
                    "discharge",
                    f"Proaktiv urladdning {proactive_w}W — "
                    f"SoC {state.total_battery_soc:.0f}%, grid {net_w:.0f}W, "
                    f"PV {pv_kw:.1f} kW "
                    f"({state.current_price:.0f} öre/kWh)",
                    discharge_w=proactive_w,
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
                return

        # ── Ellevio timmedel awareness ─────────────────────
        # Read actual Ellevio weighted timmedel (accumulated, not momentary)
        _ellevio_current = 0.0
        _ellevio_prognos = 0.0
        _ellevio_tak = float(self._cfg.get("ellevio_tak_kw", 4.0))

        ell_curr = self.hass.states.get("sensor.ellevio_viktad_timmedel_pagaende")
        if ell_curr and ell_curr.state not in ("unavailable", "unknown", ""):
            with contextlib.suppress(ValueError, TypeError):
                _ellevio_current = float(ell_curr.state)
        ell_prog = self.hass.states.get("sensor.ellevio_viktad_prognos_timmedel")
        if ell_prog and ell_prog.state not in ("unavailable", "unknown", ""):
            with contextlib.suppress(ValueError, TypeError):
                _ellevio_prognos = float(ell_prog.state)

        # If timmedel prognos approaching tak → aggressive action
        if _ellevio_prognos > _ellevio_tak * 0.85:
            _LOGGER.warning(
                "CARMA Ellevio: prognos %.2f kW > %.0f%% of tak %.1f → aggressive discharge",
                _ellevio_prognos,
                85,
                _ellevio_tak,
            )

        # ── Opt #5: Flat Line Controller — proactive grid smoothing ──
        # Track rolling 5-min average and start discharge BEFORE hitting target
        self._grid_samples.append(weighted_net / 1000)
        if len(self._grid_samples) > self._grid_sample_max:
            self._grid_samples = self._grid_samples[-self._grid_sample_max :]
        rolling_avg_kw = sum(self._grid_samples) / len(self._grid_samples)

        # Proactive: if rolling avg > target - 0.3 AND not yet discharging → start early
        if (
            rolling_avg_kw > self.target_kw - 0.3
            and self._last_command != BatteryCommand.DISCHARGE
            and weight > 0
        ):
            preemptive_w = int((rolling_avg_kw - (self.target_kw - 0.5)) * 1000 / weight)
            if preemptive_w > 50:
                temp_c = self._read_battery_temp()
                pre_check = self.safety.check_discharge(
                    state.battery_soc_1,
                    state.battery_soc_2,
                    self.min_soc,
                    state.grid_power_w,
                    temp_c,
                    reserve_kwh=getattr(self, "_current_reserve_kwh", 0.0),
                    available_kwh=max(
                        0,
                        (state.battery_soc_1 - self.min_soc)
                        / 100
                        * float(self._cfg.get("battery_1_kwh", 15.0))
                        + max(
                            0,
                            (state.battery_soc_2 - self.min_soc)
                            / 100
                            * float(self._cfg.get("battery_2_kwh", 5.0)),
                        ),
                    ),
                )
                if pre_check.ok:
                    reasoning.append(
                        f"Flat line: snitt {rolling_avg_kw:.2f} kW → target {self.target_kw:.1f} "
                        f"→ proaktiv urladdning {preemptive_w}W"
                    )
                    await self._cmd_discharge(state, preemptive_w)
                    self._track_rule("RULE_2", "proactive_flat_line")
                    await self._record_decision(
                        state,
                        "discharge",
                        f"Flat line proaktiv {preemptive_w}W"
                        f" — snitt {rolling_avg_kw:.1f} → {self.target_kw:.1f} kW",
                        discharge_w=preemptive_w,
                        reasoning=reasoning,
                        reasoning_chain=chain,
                    )
                    return

        # ── IT-2208: Proactive planned discharge ────────────
        # If plan says discharge this hour, start even if grid < target
        # This pre-positions the battery for upcoming peaks
        if self.plan and not is_night:
            current_plan = None
            for ph in self.plan:
                if ph.hour == hour:
                    current_plan = ph
                    break
            if current_plan and current_plan.action == "d" and current_plan.battery_kw < -0.1:
                planned_w = int(abs(current_plan.battery_kw) * 1000)
                # Only proactive if not already discharging enough
                if self._last_command != BatteryCommand.DISCHARGE:
                    temp_c = self._read_battery_temp()
                    bat1_kwh = float(self._cfg.get("battery_1_kwh", 15.0))
                    bat2_kwh = float(self._cfg.get("battery_2_kwh", 5.0))
                    avail = max(
                        0,
                        (state.battery_soc_1 - self.min_soc) / 100 * bat1_kwh
                        + max(0, (state.battery_soc_2 - self.min_soc) / 100 * bat2_kwh),
                    )
                    reserve = getattr(self, "_current_reserve_kwh", 0.0)
                    plan_check = self.safety.check_discharge(
                        state.battery_soc_1,
                        state.battery_soc_2,
                        self.min_soc,
                        state.grid_power_w,
                        temp_c,
                        reserve_kwh=reserve,
                        available_kwh=avail,
                    )
                    if plan_check.ok and planned_w >= 100:
                        reasoning.append(
                            f"Plan: discharge {planned_w}W kl {hour:02d} "
                            f"(pris {current_plan.price:.0f} öre, förbereder peak)"
                        )
                        await self._cmd_discharge(state, min(planned_w, 3000))
                        self._track_rule("RULE_2", "planned_discharge")
                        await self._record_decision(
                            state,
                            "discharge",
                            f"Planerad urladdning {planned_w}W — "
                            f"pris {current_plan.price:.0f} öre, plan förbereder peak",
                            discharge_w=planned_w,
                            reasoning=reasoning,
                            reasoning_chain=chain,
                        )
                        await self._execute_miner(state)
                        await self._execute_ev(state)
                        return

        # ── RULE 2: Load > target → discharge (even at 100%) ──
        # Hysteresis: if already discharging, keep going until grid drops
        # 10% BELOW target (prevents oscillation at boundary).
        hysteresis = 0.9 if self._last_command == BatteryCommand.DISCHARGE else 1.0
        if weighted_net > target_w * hysteresis and weight > 0:
            discharge_w = int((weighted_net - target_w) / weight)

            # IT-2074: Price-aware discharge throttling
            # If price drops >30% in next 2h, throttle to 50% (save kWh for later)
            current_price = (
                state.current_price if state.current_price > 0 else self._daily_avg_price
            )
            if current_price > 0 and len(self.plan) > 0:
                future_prices = []
                for ph in self.plan:
                    if (ph.hour == (hour + 1) % 24 or ph.hour == (hour + 2) % 24) and ph.price > 0:
                        future_prices.append(ph.price)
                if future_prices:
                    min_future = min(future_prices)
                    if min_future < current_price * 0.7:
                        # Price drops >30% soon — throttle discharge
                        old_discharge = discharge_w
                        discharge_w = max(100, discharge_w // 2)
                        reasoning.append(
                            f"Pris {current_price:.0f}→{min_future:.0f} öre inom 2h "
                            f"→ throttlad {old_discharge}→{discharge_w}W (sparar för dyrare)"
                        )

            reasoning.append(
                f"Grid {weighted_net / 1000:.1f} kW viktat > target {self.target_kw:.1f} kW "
                f"→ batteri kompenserar {discharge_w}W"
            )
            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                self.min_soc,
                state.grid_power_w,
                temp_c,
            )
            if result.ok:
                peak_kr = float(self._cfg.get("peak_cost_per_kw", DEFAULT_PEAK_COST_PER_KW))
                ellevio_saving = (weighted_net / 1000 - self.target_kw) * peak_kr
                step5 = (
                    f"Urladdning {discharge_w}W → Ellevio ser {self.target_kw:.1f} kW "
                    f"istf {weighted_net / 1000:.1f} kW, sparar ~{ellevio_saving:.0f} kr/mån"
                )
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_discharge(state, discharge_w)
                self._track_rule("RULE_2", "discharge")
                await self._record_decision(
                    state,
                    "discharge",
                    f"Urladdning {discharge_w}W — grid {weighted_net / 1000:.1f} kW viktat "
                    f"> target {self.target_kw:.1f} kW "
                    f"({state.current_price:.0f} öre/kWh, "
                    f"batteri {state.battery_soc_1:.0f}%)",
                    discharge_w=discharge_w,
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
            else:
                # Discharge not possible (SoC low, temp, etc.) — NOT a user issue.
                # Self-heal: fall through to standby instead of blocking.
                step5 = f"Vila — urladdning ej möjlig ({result.reason})"
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_standby(state)
                self._track_rule("RULE_2", "idle_blocked")
                await self._record_decision(
                    state,
                    "idle",
                    f"Vila — {result.reason}",
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
            # CARMA-P0-FIXES Task 3d: Call miner from discharge path too
            await self._execute_ev(state)
            await self._execute_miner(state)
            await self._execute_climate(state)
            return

        # ── RULE 4: Under target → idle ──────────────────────
        headroom_val = (target_w - weighted_net) / 1000
        step5 = (
            f"Vila — {headroom_val:.1f} kW headroom, "
            f"Ellevio ser {weighted_net / 1000:.1f} kW (mål {self.target_kw:.1f} kW)"
        )
        reasoning.append(
            f"Grid {weighted_net / 1000:.2f} kW viktat < target {self.target_kw:.1f} kW "
            f"→ {headroom_val:.1f} kW headroom, batteriet vilar"
        )
        reasoning.append(step5)
        chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
        # R5: Actively set standby so batteries don't stay in previous mode
        await self._cmd_standby(state)
        self._track_rule("RULE_4", "idle")
        await self._record_decision(
            state,
            "idle",
            f"Vila — grid {weighted_net / 1000:.2f} kW viktat "
            f"< target {self.target_kw:.1f} kW "
            f"({state.current_price:.0f} öre/kWh)",
            reasoning=reasoning,
            reasoning_chain=chain,
        )

        # ── SURPLUS PRIORITY: Battery → EV → Miner → Export ──
        await self._execute_ev(state)
        await self._execute_miner(state)
        await self._execute_climate(state)
        await self._execute_pool(state)

    async def _execute_climate(self, state: CarmaboxState) -> None:
        """Control VP/AC based on surplus and price.

        Surplus priority chain position: Battery → EV → Miner → VP → Pool → Export.
        VP as thermal storage: pre-cool/heat with surplus, pause during expensive import.
        """
        if not self._has_feature("executor"):
            return

        climate_entity = str(self._cfg.get("climate_entity", ""))
        if not climate_entity:
            # Auto-detect: look for climate.* entities
            for s in self.hass.states.async_all("climate"):
                if "ac" in s.entity_id or "vp" in s.entity_id or "heat" in s.entity_id:
                    climate_entity = s.entity_id
                    break
            if not climate_entity:
                return

        climate_state = self.hass.states.get(climate_entity)
        if climate_state is None:
            return

        current_mode = climate_state.state  # off, cool, heat, auto
        current_temp = climate_state.attributes.get("current_temperature")
        if current_temp is None:
            return

        hour = datetime.now().hour
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        is_summer = 5 <= datetime.now().month <= 9
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))

        # Comfort thresholds
        cool_target = float(self._cfg.get("climate_cool_target_c", 23.0))
        heat_target = float(self._cfg.get("climate_heat_target_c", 21.0))

        # ── VP-1: Night → don't touch (user comfort) ──
        if is_night:
            return

        # ── VP-2: Exporting surplus → thermal storage ──
        if state.is_exporting and abs(state.grid_power_w) > 500:
            if is_summer and current_temp > cool_target:
                # Pre-cool with free surplus
                if current_mode == "off":
                    await self._climate_call(climate_entity, "cool", cool_target - 1)
                    _LOGGER.info("CARMA: VP pre-cool (surplus %.0fW)", abs(state.grid_power_w))
            elif not is_summer and current_temp < heat_target + 2 and current_mode == "off":
                # Pre-heat with free surplus
                await self._climate_call(climate_entity, "heat", heat_target + 1)
                _LOGGER.info("CARMA: VP pre-heat (surplus %.0fW)", abs(state.grid_power_w))
            return

        # ── VP-3: Expensive + importing → pause if temp OK ──
        if (
            state.current_price > price_expensive
            and not state.is_exporting
            and current_mode != "off"
        ):
            temp_ok = (is_summer and current_temp < cool_target + 2) or (
                not is_summer and current_temp > heat_target - 1
            )
            if temp_ok:
                await self._climate_call(climate_entity, "off")
                _LOGGER.info(
                    "CARMA: VP pausad (pris %.0f öre, temp %.1f°C OK)",
                    state.current_price,
                    current_temp,
                )

    async def _climate_call(self, entity_id: str, mode: str, temp: float | None = None) -> None:
        """Set climate mode + temperature."""
        try:
            if mode == "off":
                await self.hass.services.async_call("climate", "turn_off", {"entity_id": entity_id})
            else:
                data: dict[str, Any] = {"entity_id": entity_id, "hvac_mode": mode}
                if temp is not None:
                    data["temperature"] = temp
                await self.hass.services.async_call("climate", "set_hvac_mode", data)
                if temp is not None:
                    await self.hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {"entity_id": entity_id, "temperature": temp},
                    )
        except (HomeAssistantError, RuntimeError):
            _LOGGER.warning("CARMA: VP control failed for %s", entity_id, exc_info=True)

    async def _execute_pool(self, state: CarmaboxState) -> None:
        """Control pool pump/heater based on surplus and temperature.

        Surplus chain: Battery → EV → Miner → VP → Pool → Export.
        """
        if not self._has_feature("executor"):
            return

        pool_entity = str(self._cfg.get("pool_entity", ""))
        pool_temp_entity = str(self._cfg.get("pool_temp_entity", ""))
        if not pool_entity:
            return

        pool_state = self.hass.states.get(pool_entity)
        if pool_state is None:
            return

        pool_on = pool_state.state == "on"

        # Read pool temperature
        pool_temp: float | None = None
        if pool_temp_entity:
            ts = self.hass.states.get(pool_temp_entity)
            if ts and ts.state not in ("unknown", "unavailable"):
                import contextlib

                with contextlib.suppress(ValueError, TypeError):
                    pool_temp = float(ts.state)

        pool_max = float(self._cfg.get("pool_max_temp_c", 28.0))

        # Too hot → always off
        if pool_temp is not None and pool_temp >= pool_max and pool_on:
            await self._pool_switch(pool_entity, False)
            _LOGGER.info("CARMA: Pool OFF (%.1f°C >= max %.1f°C)", pool_temp, pool_max)
            return

        # Surplus → heat pool if temp allows
        if state.is_exporting and abs(state.grid_power_w) > 300:
            if (pool_temp is None or pool_temp < pool_max) and not pool_on:
                await self._pool_switch(pool_entity, True)
                temp_str = f"{pool_temp:.1f}°C" if pool_temp else "okänd"
                _LOGGER.info(
                    "CARMA: Pool ON (surplus %.0fW, temp %s)",
                    abs(state.grid_power_w),
                    temp_str,
                )
            return

        # Importing → stop pool
        if not state.is_exporting and state.grid_power_w > 500 and pool_on:
            _LOGGER.info("CARMA: Pool OFF (importing %.0fW)", state.grid_power_w)
            await self._pool_switch(pool_entity, False)

    async def _pool_switch(self, entity_id: str, on: bool) -> None:
        """Turn pool switch on/off."""
        try:
            service = "turn_on" if on else "turn_off"
            await self.hass.services.async_call("switch", service, {"entity_id": entity_id})
        except (HomeAssistantError, RuntimeError):
            _LOGGER.warning("CARMA: pool switch failed: %s", entity_id, exc_info=True)

    async def _execute_pool_circulation(self, state: CarmaboxState) -> None:
        """Control pool circulation pump based on surplus.

        Surplus chain: Battery → EV → Miner → VP → Pool → Cirk → Export.
        Cirk pump runs when pool heater is on OR when surplus exists.
        """
        if not self._has_feature("executor"):
            return

        cirk_entity = str(self._cfg.get("pool_circulation_entity", ""))
        if not cirk_entity:
            # Auto-detect: look for circulation pump switch
            for s in self.hass.states.async_all("switch"):
                is_cirk = "cirk" in s.entity_id.lower() or "circulation" in s.entity_id.lower()
                is_pool = "pool" in s.entity_id.lower() or "gv" in s.entity_id.lower()
                if is_cirk and is_pool:
                    cirk_entity = s.entity_id
                    break
            if not cirk_entity:
                return

        cirk_state = self.hass.states.get(cirk_entity)
        if cirk_state is None:
            return

        cirk_on = cirk_state.state == "on"

        # Check if pool heater is running
        pool_entity = str(self._cfg.get("pool_entity", ""))
        pool_running = False
        if pool_entity:
            pool_state = self.hass.states.get(pool_entity)
            pool_running = pool_state.state == "on" if pool_state else False

        # Always run circulation when pool heater is on
        if pool_running and not cirk_on:
            await self._pool_switch(cirk_entity, True)
            _LOGGER.info("CARMA: Cirk ON (pool heater aktiv)")
            return

        # Surplus → run circulation to distribute heat
        if state.is_exporting and abs(state.grid_power_w) > 200:
            if not cirk_on:
                await self._pool_switch(cirk_entity, True)
                _LOGGER.info("CARMA: Cirk ON (surplus %.0fW)", abs(state.grid_power_w))
            return

        # Importing + pool not running → stop circulation
        if not state.is_exporting and state.grid_power_w > 500 and not pool_running and cirk_on:
            await self._pool_switch(cirk_entity, False)
            _LOGGER.info("CARMA: Cirk OFF (importing %.0fW, pool avstängd)", state.grid_power_w)

    def _check_plan_correction(self, state: CarmaboxState) -> None:
        """Plan self-correction — adjust action if actual deviates >50% from plan.

        If planned grid_kw deviates >50% from actual for 3+ consecutive cycles
        AND the plan action is not achieving its goal, switch to corrective action.

        Examples:
        - Plan: grid_charge, but grid_kw keeps exceeding target → switch to idle
        - Plan: idle, but grid_kw well below target → allow opportunistic grid_charge
        """
        now = time.time()
        # Rate limit corrections to once per 5 minutes to avoid oscillation
        if now - self._plan_last_correction_time < 300:
            return

        hour = datetime.now().hour
        planned = next((h for h in self.plan if h.hour == hour), None)
        if not planned:
            self._plan_deviation_count = 0
            return

        # Calculate actual grid_kw (same as used in hourly tracking)
        grid_kw = max(0, state.grid_power_w) / 1000
        planned_grid_kw = planned.grid_kw

        # Check for >50% deviation
        if planned_grid_kw > 0:
            deviation_pct = abs(grid_kw - planned_grid_kw) / planned_grid_kw
        else:
            # No plan expectation → no deviation
            deviation_pct = 0.0

        if deviation_pct > 0.5:
            self._plan_deviation_count += 1
        else:
            self._plan_deviation_count = 0
            return

        # Trigger correction after 3 consecutive deviations
        if self._plan_deviation_count < 3:
            return

        # Determine correction needed
        correction_needed = False
        new_action = planned.action

        # Case 1: Plan says grid_charge but grid_kw exceeds target significantly
        target_kw = self.target_kw
        if planned.action == "g" and grid_kw > target_kw * 1.5:
            new_action = "i"  # Switch to idle
            correction_needed = True
            _LOGGER.warning(
                "PLAN SELF-CORRECT: planned grid_charge but grid %.1f kW > target %.1f kW "
                "for %d cycles → switching to idle",
                grid_kw,
                target_kw,
                self._plan_deviation_count,
            )

        # Case 2: Plan says idle but grid_kw well below target (opportunity for cheap charge)
        elif planned.action == "i" and grid_kw < target_kw * 0.3 and state.current_price < 30:
            new_action = "g"  # Allow opportunistic grid_charge
            correction_needed = True
            _LOGGER.warning(
                "PLAN SELF-CORRECT: planned idle but grid %.1f kW << target %.1f kW "
                "and price %.0f öre cheap for %d cycles → allowing grid_charge",
                grid_kw,
                target_kw,
                state.current_price,
                self._plan_deviation_count,
            )

        if correction_needed:
            # Update planned action for current hour
            planned.action = new_action
            self._plan_last_correction_time = now
            self._plan_deviation_count = 0

    async def _watchdog(self, state: CarmaboxState) -> None:
        """Self-correction watchdog — catches obvious decision errors.

        Runs AFTER _execute(). Checks if the decision makes sense
        given the current state. If not, overrides with correct action.

        This is a safety net — if rule ordering or logic has a bug,
        the watchdog catches it within the same 30s cycle.

        Anomaly checks (priority order):
        W1: Exporting > 500W + battery not full + not charging → charge
        W2: Grid > target + battery has capacity + not discharging → discharge
        W3: Battery 100% + grid > target + standby → should discharge
        W4: EV charging + grid importing (day) → stop EV
        W5: High price (>80 öre) + battery >50% + idle → should discharge
        W6: EV charging but SoC unchanged for 6h (stuck) → stop EV
        """
        if not self.executor_enabled:
            return

        decision = self.last_decision
        action = decision.action if decision else "idle"
        hour = datetime.now().hour
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        night_wt = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_wt)
        net_w = max(0, state.grid_power_w)
        weighted_net = net_w * weight
        target_w = self.target_kw * 1000

        # W1: Exporting + battery not full + not charging
        # PLAT-1192: SKIP during night EV — transient export during discharge ramp-up is expected
        wd_export_w = float(self._cfg.get("watchdog_export_w", DEFAULT_WATCHDOG_EXPORT_W))
        wd_discharge_min = float(
            self._cfg.get("watchdog_discharge_min_w", DEFAULT_WATCHDOG_DISCHARGE_MIN_W)
        )
        wd_ev_import_w = float(self._cfg.get("watchdog_ev_import_w", DEFAULT_WATCHDOG_EV_IMPORT_W))
        wd_min_soc = float(self._cfg.get("watchdog_min_soc_pct", DEFAULT_WATCHDOG_MIN_SOC_PCT))
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))
        night_ev_active = getattr(self, "_night_ev_active", False)
        if (
            state.is_exporting
            and abs(state.grid_power_w) > wd_export_w
            and not state.all_batteries_full
            and action not in ("charge_pv", "charge_pv_taper", "grid_charge")
            and not night_ev_active
        ):
            _LOGGER.warning(
                "WATCHDOG W1: exporting %.0fW, bat %s%%, action=%s → correcting to charge_pv",
                abs(state.grid_power_w),
                state.total_battery_soc,
                action,
            )
            # V2: Use adapter directly, NEVER fast_charging
            for adapter in self.inverter_adapters:
                await adapter.set_ems_mode("charge_pv")
                await adapter.set_fast_charging(on=False)
            await self._record_decision(
                state,
                "charge_pv",
                f"Watchdog: exporterar {abs(state.grid_power_w):.0f}W men var {action} → solladdar",
            )
            return

        # W2/W3: Grid > target + not discharging (battery has capacity)
        # Add 10% hysteresis to prevent oscillation at boundary
        w2_threshold = target_w * 1.1
        if (
            weighted_net > w2_threshold
            and weight > 0
            and state.total_battery_soc > self.min_soc
            and action not in ("discharge", "grid_charge")
        ):
            discharge_w = int((weighted_net - w2_threshold) / weight)
            if discharge_w > wd_discharge_min:
                _LOGGER.warning(
                    "WATCHDOG W2: grid %.0fW > target %.0fW, bat %s%%, "
                    "action=%s → correcting to discharge %dW",
                    weighted_net,
                    target_w,
                    state.total_battery_soc,
                    action,
                    discharge_w,
                )
                result = self.safety.check_discharge(
                    state.battery_soc_1,
                    state.battery_soc_2,
                    self.min_soc,
                    state.grid_power_w,
                )
                if result.ok:
                    # V2: Use adapter directly, NEVER EMS auto
                    for adapter in self.inverter_adapters:
                        await adapter.set_ems_mode("discharge_pv")
                        await adapter.set_fast_charging(on=False)
                    await self._record_decision(
                        state,
                        "discharge",
                        f"Watchdog: grid {weighted_net / 1000:.1f} kW "
                        f"> target {self.target_kw:.1f} kW "
                        f"men var {action} → urladdning {discharge_w}W",
                        discharge_w=discharge_w,
                    )
                    # CARMA-P0-FIXES Task 3d: Call miner from watchdog discharge path too
                    if not getattr(self, "_night_ev_active", False):
                        await self._execute_ev(state)
                    await self._execute_miner(state)
                    await self._execute_climate(state)
                    return

        # W4: EV charging + grid importing during day — SKIP if night EV active
        if (
            not is_night
            and self._ev_enabled
            and not state.is_exporting
            and state.grid_power_w > wd_ev_import_w
            and not getattr(self, "_night_ev_active", False)
        ):
            _LOGGER.warning(
                "WATCHDOG W4: EV charging but grid importing %.0fW → stopping EV",
                state.grid_power_w,
            )
            await self._cmd_ev_stop()

        # W4b: EMERGENCY — battery depleted + EV still charging = STOP NOW
        if self._ev_enabled and state.ev_power_w > 100 and state.total_battery_soc <= self.min_soc:
            _LOGGER.error(
                "WATCHDOG W4b: Battery %.0f%% ≤ min %.0f%% + EV %.0fW → EMERGENCY STOP",
                state.total_battery_soc,
                self.min_soc,
                state.ev_power_w,
            )
            await self._cmd_ev_stop()
            self._night_ev_active = False
            self._nev_state = "BATTERY_DEPLETED"

        # W6: ABSOLUTE GRID GUARD — ALDRIG över target
        # Sista säkerhetsnät: om grid > target trots alla andra lager → nödstoppa EV
        _w6_nw = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        _w6_weight = _w6_nw if is_night else 1.0
        _w6_weighted = max(0, state.grid_power_w) / 1000 * _w6_weight
        if _w6_weighted > target_w / 1000 * 1.15 and self._ev_enabled:
            if night_ev_active and state.total_battery_soc > self.min_soc:
                # PLAT-1192: Night EV — increase discharge instead of stopping EV
                _LOGGER.warning(
                    "WATCHDOG W6: grid %.1f kW >> target %.1f kW"
                    " — night EV active, increasing discharge (not stopping EV)",
                    _w6_weighted,
                    target_w / 1000,
                )
                for _adp in self.inverter_adapters:
                    await _adp.set_ems_mode("discharge_pv")
            else:
                _LOGGER.error(
                    "WATCHDOG W6 ABSOLUTE: grid %.1f kW (weighted) >> target %.1f kW"
                    " — NÖDSTOPP EV (LAG 1 trumfar allt)",
                    _w6_weighted,
                    target_w / 1000,
                )
                await self._cmd_ev_stop()

        # W7: EV stuck — charging for hours without SoC change
        from .const import EV_STUCK_MAX_HOURS

        if self._ev_enabled and state.ev_power_w > 100:
            if not hasattr(self, "_ev_stuck_last_soc"):
                self._ev_stuck_last_soc = state.ev_soc
                self._ev_stuck_since = time.monotonic()
            if abs(state.ev_soc - self._ev_stuck_last_soc) > 1.0:
                # SoC changed — reset
                self._ev_stuck_last_soc = state.ev_soc
                self._ev_stuck_since = time.monotonic()
            elif time.monotonic() - self._ev_stuck_since > EV_STUCK_MAX_HOURS * 3600:
                _LOGGER.error(
                    "WATCHDOG W7: EV stuck — %.0fh charging, SoC unchanged (%.0f%%)",
                    EV_STUCK_MAX_HOURS,
                    state.ev_soc,
                )
                await self._cmd_ev_stop()
        else:
            if hasattr(self, "_ev_stuck_since"):
                del self._ev_stuck_since
                del self._ev_stuck_last_soc

        # W8: PLAT-1077 — SoC imbalance alert (kontor vs forrad)
        from .const import SOC_IMBALANCE_THRESHOLD_PCT

        if state.has_battery_2 and state.battery_soc_2 >= 0:
            soc_diff = abs(state.battery_soc_1 - state.battery_soc_2)
            if soc_diff > SOC_IMBALANCE_THRESHOLD_PCT:
                if not getattr(self, "_soc_imbalance_logged", False):
                    _LOGGER.warning(
                        "WATCHDOG W8: SoC imbalance — kontor %.0f%% vs forrad %.0f%% "
                        "(diff %.0f%% > %d%%)",
                        state.battery_soc_1,
                        state.battery_soc_2,
                        soc_diff,
                        SOC_IMBALANCE_THRESHOLD_PCT,
                    )
                    self._soc_imbalance_logged = True
            else:
                self._soc_imbalance_logged = False

        # W5: High price + battery capacity + idle
        if (
            state.current_price > price_expensive
            and state.total_battery_soc > wd_min_soc
            and action == "idle"
            and weighted_net > target_w * 0.8
        ):
            _LOGGER.info(
                "WATCHDOG W5: price %.0f öre, bat %s%%, idle → "
                "grid %.1f kW near target, monitoring",
                state.current_price,
                state.total_battery_soc,
                weighted_net / 1000,
            )

        # W6: EV stuck — charging but SoC unchanged for 6h → stop EV
        if (
            self._ev_enabled
            and state.ev_power_w > 500  # Actively drawing power
            and self._ev_prev_soc_for_stuck > 0  # Have at least one SoC reading
            and (time.monotonic() - self._ev_last_soc_change_t) > EV_STUCK_TIMEOUT_S
        ):
            _LOGGER.warning(
                "WATCHDOG W6: EV stuck — charging for 6h but SoC unchanged at %.0f%% → stopping EV",
                self._last_known_ev_soc,
            )
            await self._cmd_ev_stop()

    def _calculate_ev_target(self) -> float:
        """IT-1965: Dynamic EV SoC target based on 3-day solar forecast.

        Rules:
        - worst_3_days < SOLAR_OK → 100% (bad weather ahead, charge while we can!)
        - forecast_tomorrow > SOLAR_GOOD → 100% (good sun tomorrow)
        - forecast_tomorrow 20-30 → linear 75-100%
        - else → 75% (conservative)

        Thresholds configurable via config flow.
        """
        from .const import (
            DEFAULT_EV_SOC_MAX_TARGET,
            DEFAULT_EV_SOC_MIN_TARGET,
            DEFAULT_SOLAR_GOOD_KWH,
            DEFAULT_SOLAR_OK_KWH,
        )

        solar_good = float(self._cfg.get("solar_good_kwh", DEFAULT_SOLAR_GOOD_KWH))
        solar_ok = float(self._cfg.get("solar_ok_kwh", DEFAULT_SOLAR_OK_KWH))
        min_target = float(self._cfg.get("ev_soc_min_target", DEFAULT_EV_SOC_MIN_TARGET))
        max_target = float(self._cfg.get("ev_soc_max_target", DEFAULT_EV_SOC_MAX_TARGET))

        try:
            from .adapters.solcast import SolcastAdapter

            solcast = SolcastAdapter(self.hass)
            daily = solcast.forecast_daily_3d  # [today, tomorrow, day3, day4, ...]
        except (ImportError, AttributeError, RuntimeError):
            # Fallback if solcast unavailable
            return float(self._cfg.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC))

        if len(daily) < 3:
            return float(self._cfg.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC))

        tomorrow = daily[1] if len(daily) > 1 else 0
        worst_3_days = min(daily[1:4]) if len(daily) >= 4 else min(daily[1:])

        # Rule 1: Bad weather ahead → charge full while we can
        if worst_3_days < solar_ok:
            _LOGGER.info(
                "CARMA EV: worst 3-day forecast %.1f kWh < %.0f → target 100%%",
                worst_3_days,
                solar_ok,
            )
            return max_target

        # Rule 2: Good sun tomorrow → charge full
        if tomorrow > solar_good:
            _LOGGER.info(
                "CARMA EV: tomorrow %.1f kWh > %.0f → target 100%%",
                tomorrow,
                solar_good,
            )
            return max_target

        # Rule 3: OK sun → linear interpolation
        if tomorrow > solar_ok:
            ratio = (tomorrow - solar_ok) / (solar_good - solar_ok)
            target = min_target + ratio * (max_target - min_target)
            _LOGGER.info(
                "CARMA EV: tomorrow %.1f kWh (OK) → target %.0f%%",
                tomorrow,
                target,
            )
            return round(target, 0)

        # Rule 4: Bad sun tomorrow → conservative
        _LOGGER.info(
            "CARMA EV: tomorrow %.1f kWh < %.0f → target %.0f%%",
            tomorrow,
            solar_ok,
            min_target,
        )
        return min_target

    async def _execute_ev(self, state: CarmaboxState) -> None:
        """Execute EV charging decisions (PLAT-949).

        Runs AFTER battery rules. Controls Easee enable/disable + amps.
        Always starts at 6A, ramps gradually, reduces immediately.
        """
        if not self.ev_adapter:
            return

        hour = datetime.now().hour
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))

        # ── EV-1: Not connected → stop ───────────────────────
        if not self.ev_adapter.cable_locked:
            if self._ev_enabled:
                await self._cmd_ev_stop()
            return

        # EV SoC: use actual if available, else last_known - derating
        ev_soc = state.ev_soc
        if ev_soc < 0:
            derating = float(self._cfg.get("ev_soc_derating", 10.0))
            if self._last_known_ev_soc > 0:
                ev_soc = max(0, self._last_known_ev_soc * (1 - derating / 100))
                _LOGGER.debug(
                    "CARMA EV: using last_known %.0f%% x %.0f%% = %.0f%%",
                    self._last_known_ev_soc,
                    100 - derating,
                    ev_soc,
                )
            else:
                # Ultimate fallback: assume 50% (conservative)
                ev_soc = 50.0
                _LOGGER.warning("CARMA EV: no SoC data at all — assuming 50%%")
        elif ev_soc > 0:
            self._last_known_ev_soc = ev_soc
            # Track full charge for weekly full-charge logic
            if ev_soc >= 99:
                self._ev_last_full_charge_date = datetime.now().strftime("%Y-%m-%d")
            # Continuous EV plan: estimate tonight SoC = current x (1 - derating/100)
            ev_derating_pct = float(self._cfg.get("ev_soc_derating", 10.0))
            self._ev_tonight_soc = max(0, ev_soc * (1 - ev_derating_pct / 100))
            ev_capacity = float(self._cfg.get("ev_capacity_kwh", 87.5))
            ev_target = self._calculate_ev_target()
            if self._ev_tonight_soc < ev_target:
                need_kwh = (ev_target - self._ev_tonight_soc) / 100 * ev_capacity
                hours_6a = need_kwh / (DEFAULT_EV_MIN_AMPS * 3 * 230 / 1000)
                hours_8a = need_kwh / (8 * 230 * 3 / 1000)
                _LOGGER.info(
                    "CARMA EV plan: SoC %.0f%% → tonight ~%.0f%% → target %.0f%% "
                    "= %.1f kWh (%.1fh@6A, %.1fh@8A)",
                    ev_soc,
                    self._ev_tonight_soc,
                    ev_target,
                    need_kwh,
                    hours_6a,
                    hours_8a,
                )

        # ── IT-2066: Appliance detection (disk/tvätt/tork) ─────
        # Read appliance power for pause logic
        _appliance_w = 0.0
        for app_entity in (
            "sensor.98_shelly_plug_s_power",
            "sensor.102_shelly_plug_g3_power",
            "sensor.103_shelly_plug_g3_power",
        ):
            app_state = self.hass.states.get(app_entity)
            if app_state and app_state.state not in ("unavailable", "unknown", ""):
                with contextlib.suppress(ValueError, TypeError):
                    _appliance_w += float(app_state.state)

        # ── IT-2064+: Smart EV pause during appliances ────────
        # If appliances running AND enough time to reach target → pause EV
        if self._ev_enabled and self.ev_adapter and self.ev_adapter.power_w > 100:
            _app_total = 0.0
            for app_eid in (
                "sensor.98_shelly_plug_s_power",
                "sensor.102_shelly_plug_g3_power",
                "sensor.103_shelly_plug_g3_power",
            ):
                app_st = self.hass.states.get(app_eid)
                if app_st and app_st.state not in ("unavailable", "unknown", ""):
                    with contextlib.suppress(ValueError, TypeError):
                        _app_total += float(app_st.state)
            if _app_total > 500 and is_night:
                # Check if we have enough time to reach target without this hour
                hours_left = (
                    (DEFAULT_NIGHT_END - hour) % 24
                    if hour >= DEFAULT_NIGHT_START
                    else (DEFAULT_NIGHT_END - hour)
                )
                if hours_left < 0:
                    hours_left += 24
                ev_kw_rate = DEFAULT_EV_MIN_AMPS * 3 * 230 / 1000  # MIN_AMPS 3-fas
                ev_capacity = float(self._cfg.get("ev_capacity_kwh", 87.5))
                ev_need_kwh = max(0, (ev_target - ev_soc) / 100 * ev_capacity)
                ev_hours_needed = ev_need_kwh / ev_kw_rate if ev_kw_rate > 0 else 999
                if hours_left > ev_hours_needed + 1.5:  # 1.5h margin for disk
                    _LOGGER.info(
                        "CARMA EV: appliances %.0fW — pausing"
                        " (%.1fh needed, %.0fh left, margin OK)",
                        _app_total,
                        ev_hours_needed,
                        hours_left,
                    )
                    await self._cmd_ev_stop()
                    return

        # ── IT-2064: Ellevio emergency brake (uses prognos timmedel) ──
        # Uses actual Ellevio prognos (accumulated) instead of momentary grid
        # PLAT-1060: Read sensor locally — _ellevio_prognos is scoped to _execute()
        _ellevio_prognos = 0.0
        ell_prog = self.hass.states.get("sensor.ellevio_viktad_prognos_timmedel")
        if ell_prog and ell_prog.state not in ("unavailable", "unknown", ""):
            with contextlib.suppress(ValueError, TypeError):
                _ellevio_prognos = float(ell_prog.state)

        if self._ev_enabled and self.ev_adapter and self.ev_adapter.power_w > 100:
            tak_kw = float(self._cfg.get("ellevio_tak_kw", 4.0))
            # Prefer Ellevio prognos sensor (accumulated timmedel)
            weighted_kw = (
                _ellevio_prognos
                if _ellevio_prognos > 0
                else (max(0, state.grid_power_w) / 1000 * self._ellevio_weight(hour))
            )
            if weighted_kw > tak_kw * 0.85:
                # Emergency: reduce to 6A or stop
                if self._ev_current_amps > DEFAULT_EV_MIN_AMPS:
                    _LOGGER.warning(
                        "CARMA EV BRAKE: weighted %.1f kW > tak %.1f — reducing to %dA",
                        weighted_kw,
                        tak_kw,
                        DEFAULT_EV_MIN_AMPS,
                    )
                    await self._cmd_ev_adjust(DEFAULT_EV_MIN_AMPS)
                    return
                elif weighted_kw > tak_kw * 1.05:
                    _LOGGER.warning(
                        "CARMA EV BRAKE: weighted %.1f kW >> tak %.1f — stopping EV",
                        weighted_kw,
                        tak_kw,
                    )
                    await self._cmd_ev_stop()
                    return

        # ── EV-2: Target SoC reached → stop ──────────────────
        ev_target = self._calculate_ev_target()
        if ev_soc >= ev_target:
            if self._ev_enabled:
                _LOGGER.info(
                    "CARMA: EV SoC %.0f%% >= target %.0f%% — stop",
                    state.ev_soc,
                    ev_target,
                )
                await self._cmd_ev_stop()
            return

        # ── EV-3: Night → follow plan schedule ───────────────
        if is_night:
            planned_ev_kw = 0.0
            for h in self.plan:
                if h.hour == hour:
                    planned_ev_kw = h.ev_kw
                    break

            # CARMA-P0-FIXES Task 1: Fallback if plan has 0 but conditions are good
            if planned_ev_kw <= 0:
                # Fallback: if EV connected + SoC < target + cheap price → charge at 6A minimum
                price_expensive = float(
                    self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE)
                )
                current_price = (
                    state.current_price if state.current_price > 0 else self._daily_avg_price
                )
                if (
                    self.ev_adapter
                    and self.ev_adapter.cable_locked
                    and ev_soc >= 0
                    and ev_soc < ev_target
                    and current_price < price_expensive
                ):
                    # Check Ellevio headroom before starting
                    ev_kw = (
                        self.ev_adapter.charging_power_at_amps
                        if self.ev_adapter
                        else 6 * 230 * 3 / 1000
                    )  # 3-phase aware
                    grid_now_kw = max(0, state.grid_power_w) / 1000
                    weight = self._ellevio_weight(hour)
                    headroom_kw = self.target_kw / weight - grid_now_kw
                    # IT-2066: If appliances running, check combined headroom
                    if _appliance_w > 500:
                        combined_kw = ev_kw + _appliance_w / 1000
                        if headroom_kw < combined_kw:
                            _LOGGER.info(
                                "CARMA EV: appliances %.0fW running"
                                " — pausing EV (headroom %.1f < combined %.1f)",
                                _appliance_w,
                                headroom_kw,
                                combined_kw,
                            )
                            if self._ev_enabled:
                                await self._cmd_ev_stop()
                            return

                    if headroom_kw >= ev_kw * 0.5:
                        # Enough headroom — charge at min amps
                        if not self._ev_enabled:
                            await self._cmd_ev_start(DEFAULT_EV_MIN_AMPS)
                        elif self._ev_current_amps != DEFAULT_EV_MIN_AMPS:
                            await self._cmd_ev_adjust(DEFAULT_EV_MIN_AMPS)
                    else:
                        _LOGGER.info(
                            "CARMA EV: skipping — headroom %.1f kW < EV %.1f kW",
                            headroom_kw,
                            ev_kw,
                        )
                        if self._ev_enabled:
                            await self._cmd_ev_stop()
                    return
                # No fallback applicable — stop EV
                if self._ev_enabled:
                    await self._cmd_ev_stop()
                return

            # Calculate optimal amps from grid headroom WITH battery support
            ev_load_kw = state.ev_power_w / 1000
            house_only_kw = max(0, max(0, state.grid_power_w) / 1000 - ev_load_kw)
            weight = night_weight if is_night else 1.0
            ev_max_hw = float(self._cfg.get("ev_night_headroom_kw", DEFAULT_EV_NIGHT_HEADROOM_KW))
            headroom_kw = (self.target_kw / weight - house_only_kw) if weight > 0 else ev_max_hw

            # Weekday night: battery supports EV → more headroom
            is_weekday = datetime.now().weekday() < 5
            bat1_kwh = float(self._cfg.get("battery_1_kwh", 15.0))
            bat2_kwh = float(self._cfg.get("battery_2_kwh", 5.0))
            bat_available = max(
                0,
                (state.battery_soc_1 - self.min_soc) / 100 * bat1_kwh
                + max(0, (state.battery_soc_2 - self.min_soc) / 100 * bat2_kwh),
            )
            reserve = getattr(self, "_current_reserve_kwh", 0.0)

            if is_weekday and is_night and bat_available > reserve + 2.0:
                # Battery can support: add up to 2.5 kW headroom
                bat_support_kw = min(2.5, (bat_available - reserve) / 4)  # spread over ~4h
                headroom_kw += bat_support_kw
                _LOGGER.debug(
                    "CARMA EV: weekday night battery support"
                    " +%.1f kW headroom (bat %.1f kWh avail)",
                    bat_support_kw,
                    bat_available,
                )
            optimal_amps = max(0, int(headroom_kw * 1000 / DEFAULT_VOLTAGE))
            optimal_amps = min(optimal_amps, DEFAULT_EV_MAX_AMPS)

            if optimal_amps >= DEFAULT_EV_MIN_AMPS:
                if not self._ev_enabled:
                    await self._cmd_ev_start(DEFAULT_EV_MIN_AMPS)
                elif optimal_amps > self._ev_current_amps:
                    now = time.monotonic()
                    if now - self._ev_last_ramp_time >= EV_RAMP_INTERVAL_S:
                        await self._cmd_ev_adjust(optimal_amps)
                        self._ev_last_ramp_time = now
                elif optimal_amps < self._ev_current_amps:
                    await self._cmd_ev_adjust(optimal_amps)
            else:
                if self._ev_enabled:
                    await self._cmd_ev_stop()
            return

        # ── EV-4: Day → PV surplus ONLY (never cause grid import) ──
        # With hysteresis to prevent flapping at cloud edges.
        from .const import (
            EV_PV_AMPS_INTERVAL_S,
            EV_PV_START_DELAY_S,
            EV_PV_START_THRESHOLD_KW,
            EV_PV_STOP_DELAY_S,
        )

        if not hasattr(self, "_ev_pv_export_since"):
            self._ev_pv_export_since = 0.0  # monotonic time when export started
            self._ev_pv_import_since = 0.0
            self._ev_pv_last_amps_change = 0.0

        _now_mono = time.monotonic()

        if state.is_exporting:
            export_kw = abs(state.grid_power_w) / 1000
            if self._ev_enabled:
                export_kw += state.ev_power_w / 1000
            self._ev_pv_import_since = 0.0  # Reset import timer

            solar_amps = min(
                max(0, int(export_kw * 1000 / DEFAULT_VOLTAGE)),
                DEFAULT_EV_MAX_AMPS,
            )

            if not self._ev_enabled:
                # Start hysteresis: export > threshold for START_DELAY
                if export_kw >= EV_PV_START_THRESHOLD_KW:
                    if self._ev_pv_export_since == 0.0:
                        self._ev_pv_export_since = _now_mono
                    elif _now_mono - self._ev_pv_export_since >= EV_PV_START_DELAY_S:
                        await self._cmd_ev_start(DEFAULT_EV_MIN_AMPS)
                        self._ev_pv_export_since = 0.0
                        self._ev_pv_last_amps_change = _now_mono
                else:
                    self._ev_pv_export_since = 0.0  # Not enough export
            else:
                # Amps adjustment hysteresis
                if (
                    solar_amps != self._ev_current_amps
                    and _now_mono - self._ev_pv_last_amps_change >= EV_PV_AMPS_INTERVAL_S
                ):
                    await self._cmd_ev_adjust(solar_amps)
                    self._ev_pv_last_amps_change = _now_mono
            return

        # Grid importing
        self._ev_pv_export_since = 0.0  # Reset export timer
        if self._ev_enabled:
            # Stop hysteresis: import for STOP_DELAY before stopping
            if self._ev_pv_import_since == 0.0:
                self._ev_pv_import_since = _now_mono
            elif _now_mono - self._ev_pv_import_since >= EV_PV_STOP_DELAY_S:
                await self._cmd_ev_stop()
                self._ev_pv_import_since = 0.0
            return

        # Default: not charging → ensure disabled
        if self._ev_enabled and not is_night:
            await self._cmd_ev_stop()

    async def _execute_miner(self, state: CarmaboxState) -> None:
        """CARMA-P0-FIXES Task 3: Miner control with SoC/price awareness + state reconciliation.

        Priority chain: Battery → EV → Miner → VP → Pool → Export.

        Logic:
        a) High SoC (>80%) + daytime: keep ON (batteries support via discharge)
        b) Low SoC (<30%) + expensive price (>80 öre): turn OFF
        c) State reconciliation: read actual switch state and correct mismatch
        d) Export surplus: turn ON
        e) Import + not special conditions: turn OFF
        """
        # Lazy init: re-read from config if empty (reload may not trigger __init__)
        if not self._miner_entity:
            self._miner_entity = str(self._cfg.get("miner_entity", ""))
            if not self._miner_entity:
                self._miner_entity = self._detect_miner_entity()
            # Hardcoded fallback for known installation
            if not self._miner_entity:
                known = self.hass.states.get("switch.shelly1pmg4_a085e3bd1e60")
                if known and known.state not in ("unavailable", "unknown"):
                    self._miner_entity = "switch.shelly1pmg4_a085e3bd1e60"
            if self._miner_entity:
                _LOGGER.info("CARMA: miner_entity resolved → %s", self._miner_entity)
        if not self._miner_entity:
            return

        # ── State reconciliation: read actual switch state ────
        actual_state_obj = self.hass.states.get(self._miner_entity)
        actual_on = actual_state_obj.state == "on" if actual_state_obj else self._miner_on

        # Correct internal state if mismatch (e.g. manual toggle or HA restart)
        if actual_on != self._miner_on:
            _LOGGER.info(
                "CARMA: Miner state reconciliation — internal=%s actual=%s → correcting",
                self._miner_on,
                actual_on,
            )
            self._miner_on = actual_on

        hour = datetime.now().hour
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        is_winter = datetime.now().month in (10, 11, 12, 1, 2, 3)
        miner_heat_useful = bool(self._cfg.get("miner_heat_useful", False)) and is_winter

        miner_start_w = float(self._cfg.get("miner_start_export_w", DEFAULT_MINER_START_EXPORT_W))
        miner_stop_w = float(self._cfg.get("miner_stop_import_w", DEFAULT_MINER_STOP_IMPORT_W))
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))
        current_price = state.current_price if state.current_price > 0 else self._daily_avg_price

        # ── (b) Low SoC + expensive price → OFF ────────────────
        if state.total_battery_soc < 30 and current_price > price_expensive:
            if self._miner_on:
                _LOGGER.info(
                    "CARMA: Low battery %.0f%% + expensive price %.0f öre → miner OFF",
                    state.total_battery_soc,
                    current_price,
                )
                await self._cmd_miner(False)
            return

        # ── Opt #4: Miner ONLY at PV export (STRICT) ──────────
        # NEVER run miner during grid import — wastes 400W
        if state.grid_power_w >= 0:
            # Importing from grid — miner OFF
            if self._miner_on:
                _LOGGER.info(
                    "CARMA: Grid importing %.0fW — miner OFF (strict export-only)",
                    state.grid_power_w,
                )
                await self._cmd_miner(False)
            return

        # Exporting — miner can run if export > threshold
        if abs(state.grid_power_w) > miner_start_w:
            if not self._miner_on:
                _LOGGER.info(
                    "CARMA: PV export %.0fW > %.0f → miner ON",
                    abs(state.grid_power_w),
                    miner_start_w,
                )
                await self._cmd_miner(True)
            return

        # ── IT-2062: Miner OFF when EV is charging (night OR day) ──
        if self._ev_enabled and self.ev_adapter and self.ev_adapter.power_w > 100:
            if self._miner_on:
                _LOGGER.info(
                    "CARMA: EV charging %.0fW — miner OFF to reduce grid load",
                    self.ev_adapter.power_w,
                )
                await self._cmd_miner(False)
            return

        # ── Night: miner OFF (save grid power) — unless heat needed ──
        if is_night and self._miner_on and not miner_heat_useful:
            await self._cmd_miner(False)
            return

        # ── Exporting surplus → mine ───────────────────────────
        if state.is_exporting and abs(state.grid_power_w) > miner_start_w:
            if not self._miner_on:
                _LOGGER.info(
                    "CARMA: PV surplus %.0fW → miner ON",
                    abs(state.grid_power_w),
                )
                await self._cmd_miner(True)
        elif (
            not state.is_exporting
            and state.grid_power_w > miner_stop_w
            and self._miner_on
            and not miner_heat_useful
        ):
            # Importing → stop (unless miner heat is useful in winter)
            _LOGGER.info(
                "CARMA: Grid import %.0fW → miner OFF",
                state.grid_power_w,
            )
            await self._cmd_miner(False)

    async def _cmd_miner(self, on: bool) -> None:
        """Turn miner switch on/off."""
        if not self._miner_entity:
            return
        service = "turn_on" if on else "turn_off"
        _LOGGER.info("CARMA: miner %s → %s", self._miner_entity, service)
        try:
            await self.hass.services.async_call(
                "switch",
                service,
                {"entity_id": self._miner_entity},
            )
            self._miner_on = on
            # CARMA-P0-FIXES Task 4: Save runtime after miner state change
            await self._async_save_runtime()
        except (HomeAssistantError, RuntimeError):
            _LOGGER.warning("CARMA: miner control failed", exc_info=True)

    async def _cmd_ev_start(self, amps: int = DEFAULT_EV_MIN_AMPS) -> None:
        """Start EV: set current FIRST, then enable (prevent 16A burst)."""
        amps = max(DEFAULT_EV_MIN_AMPS, min(amps, DEFAULT_EV_MAX_AMPS))
        if self._ev_enabled and self._ev_current_amps == amps:
            return
        if not self.ev_adapter:
            return
        _LOGGER.info("CARMA: EV start %dA", amps)
        ok = await self.ev_adapter.set_current(amps)
        if not ok:
            return
        # FIX D: Also enforce dynamic_charger_limit (Easee Cloud may override)
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.{self.ev_adapter.prefix}_dynamic_charger_limit",
                    "value": amps,
                },
            )
        if not self._ev_enabled:
            ok = await self.ev_adapter.enable()
            if not ok:
                await self.ev_adapter.disable()
                return
        self._ev_enabled = True
        self._ev_current_amps = amps
        # CARMA-P0-FIXES Task 4: Save runtime after EV state change
        await self._async_save_runtime()

    async def _cmd_ev_stop(self) -> None:
        """Stop EV: disable + reset to min amps."""
        if not self.ev_adapter:
            return
        _LOGGER.info("CARMA: EV stop")
        await self.ev_adapter.disable()
        await self.ev_adapter.reset_to_default()
        # FIX D: Reset dynamic_charger_limit to min amps
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.{self.ev_adapter.prefix}_dynamic_charger_limit",
                    "value": DEFAULT_EV_MIN_AMPS,
                },
            )
        self._ev_enabled = False
        self._ev_current_amps = 0
        # CARMA-P0-FIXES Task 4: Save runtime after EV state change
        await self._async_save_runtime()

    async def _cmd_ev_adjust(self, amps: int) -> None:
        """Adjust EV amps without enable/disable.

        EXP-04: Ramp UP follows EV_RAMP_STEPS (6→8→10) one step at a time.
        Ramp DOWN goes directly to target (safe, no surge risk).
        """
        if not self.ev_adapter or not self._ev_enabled:
            return
        amps = max(DEFAULT_EV_MIN_AMPS, min(amps, DEFAULT_EV_MAX_AMPS))
        if amps == self._ev_current_amps:
            return
        # EXP-04: Ramp UP = one step at a time per EV_RAMP_STEPS
        if amps > self._ev_current_amps:
            next_step = amps  # default: direct
            for step in EV_RAMP_STEPS:
                if step > self._ev_current_amps:
                    next_step = min(step, amps)
                    break
            amps = next_step
        # Ramp DOWN = direct to target (no surge risk)
        _LOGGER.info("CARMA: EV adjust %dA -> %dA", self._ev_current_amps, amps)
        ok = await self.ev_adapter.set_current(amps)
        # FIX D: Enforce dynamic_charger_limit
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.{self.ev_adapter.prefix}_dynamic_charger_limit",
                    "value": amps,
                },
            )
        if ok:
            self._ev_current_amps = amps
            # CARMA-P0-FIXES Task 4: Save runtime after EV amps change
            await self._async_save_runtime()

    def _track_rule(self, rule_id: str, result: str) -> None:
        """IT-1937: Track active rule and last triggered timestamp for sensor.carma_box_rules."""
        self._active_rule_id = rule_id
        self._rule_triggers[rule_id] = {
            "timestamp": datetime.now().isoformat(),
            "result": result,
        }

    async def _record_decision(
        self,
        state: CarmaboxState,
        action: str,
        reason: str,
        discharge_w: int = 0,
        safety_blocked: bool = False,
        safety_reason: str = "",
        reasoning: list[str] | None = None,
        reasoning_chain: list[dict[str, str]] | None = None,
    ) -> None:
        """Record a decision for transparency + logging."""
        hour = datetime.now().hour
        night_wt = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_wt)
        decision = Decision(
            timestamp=datetime.now().isoformat(),
            action=action,
            reason=reason,
            target_kw=self.target_kw,
            grid_kw=round(max(0, state.grid_power_w) / 1000, 2),
            weighted_kw=round(max(0, state.grid_power_w) / 1000 * weight, 2),
            price_ore=round(state.current_price, 1),
            battery_soc=round(state.total_battery_soc, 0),
            ev_soc=round(state.ev_soc, 0) if state.has_ev else -1,
            pv_kw=round(state.pv_power_w / 1000, 2),
            discharge_w=discharge_w,
            safety_blocked=safety_blocked,
            safety_reason=safety_reason,
            reasoning=reasoning or [],
            reasoning_chain=reasoning_chain or [],
        )
        self.last_decision = decision

        # Keep last 48 decisions (24h at 30min intervals) — deque auto-evicts
        self.decision_log.append(decision)

        _LOGGER.info("CARMA decision: %s — %s", action, reason)

        # HA logbook entry for transparency (best-effort)
        await self._log_decision(reason)

    async def _log_decision(self, reason: str) -> None:
        """Log decision to system_log (best-effort, ignores missing service)."""
        import contextlib

        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "system_log",
                "write",
                {
                    "message": f"CARMA Box: {reason}",
                    "level": "info",
                    "logger": "carmabox.decision",
                },
            )

    def _read_battery_temp(self) -> float | None:
        """Read battery temperature — delegates to StateManager."""
        return self._state_mgr.read_battery_temp(self.inverter_adapters)

    @property
    def system_health(self) -> dict[str, str]:
        """PLAT-964: System health for transparency sensor.

        Returns user-friendly status per component. NEVER technical error messages.
        """
        health: dict[str, str] = {}

        # Inverter adapters
        for i, adapter in enumerate(self.inverter_adapters, 1):
            name = getattr(adapter, "prefix", f"inverter_{i}")
            is_gw = isinstance(adapter, GoodWeAdapter)
            ems_entity = f"select.goodwe_{name}_ems_mode" if is_gw else ""
            ems_state = self.hass.states.get(ems_entity) if ems_entity else None
            if ems_state is None or ems_state.state in ("unavailable", "unknown"):
                health[name] = "offline"
            elif adapter.soc < 0:
                health[name] = "ingen data"
            else:
                health[name] = "ok"

        # EV charger
        if self.ev_adapter:
            if isinstance(self.ev_adapter, EaseeAdapter):
                if self.ev_adapter.status in ("", "unavailable", "unknown"):
                    health["ev"] = "offline"
                elif self.ev_adapter.cable_locked:
                    if self.ev_adapter.is_charging:
                        health["ev"] = "laddar"
                    else:
                        health["ev"] = "ansluten"
                else:
                    health["ev"] = "ej ansluten"
            else:
                health["ev"] = "ok"

        # Safety guard
        has_rbc = hasattr(self.safety, "recent_block_count")
        blocks = self.safety.recent_block_count(3600) if has_rbc else 0
        if isinstance(blocks, int) and blocks >= SAFETY_BLOCK_THRESHOLD:
            health["sakerhet"] = "varning"
        else:
            health["sakerhet"] = "ok"

        # Self-healing pause
        if time.monotonic() < self._ems_pause_until:
            health["styrning"] = "pausad"
        else:
            health["styrning"] = "ok"

        return health

    @property
    def status_text(self) -> str:
        """PLAT-964: User-friendly one-liner status. Swedish, plain language."""
        health = self.system_health
        issues = []
        for component, status in health.items():
            if status == "offline":
                friendly = {
                    "kontor": "Kontor offline",
                    "forrad": "Forrad offline",
                }.get(component, f"{component} offline")
                issues.append(friendly)
            elif status == "pausad":
                issues.append("Styrning pausad")
            elif status == "varning":
                issues.append("Sakerhetsspaerr aktiv")

        if not issues:
            return "Allt fungerar"
        return ", ".join(issues)

    def plan_score(self) -> dict[str, Any]:
        """PLAT-966: Calculate how well the plan matched reality.

        Returns dict with score_today, score_7d, score_30d, trend.
        Score = 0-100 where 100 = perfect match.
        """
        actuals = self.hourly_actuals
        if len(actuals) < 2:
            return {
                "score_today": None,
                "score_7d": None,
                "score_30d": None,
                "trend": "stable",
            }

        # Today's score: min/max ratio across tracked hours
        scores: list[float] = []
        for a in actuals:
            p = abs(a.planned_weighted_kw)
            r = abs(a.actual_weighted_kw)
            if p < 0.01 and r < 0.01:
                scores.append(100.0)
            else:
                lo, hi = min(p, r), max(p, r)
                scores.append((lo / hi) * 100 if hi > 0 else 100.0)

        score_today: float | None = round(sum(scores) / len(scores), 1) if scores else None

        # 7d and 30d: use daily_savings trend as proxy for consistency
        daily = self.savings.daily_savings
        score_7d: float | None
        if len(daily) >= 7:
            recent_7 = daily[-7:]
            # Score based on consistency: low variance = good
            avg_7 = sum(d.total_kr for d in recent_7) / 7
            score_7d = round(min(100, max(0, 50 + avg_7 * 2)), 1)
        else:
            score_7d = score_today

        score_30d: float | None
        if len(daily) >= 30:
            recent_30 = daily[-30:]
            avg_30 = sum(d.total_kr for d in recent_30) / 30
            score_30d = round(min(100, max(0, 50 + avg_30 * 2)), 1)
        else:
            score_30d = score_7d

        # Trend: compare last 7d vs previous 7d
        trend = "stable"
        if len(daily) >= 14:
            recent = sum(d.total_kr for d in daily[-7:])
            previous = sum(d.total_kr for d in daily[-14:-7])
            if recent > previous * 1.1:
                trend = "improving"
            elif recent < previous * 0.9:
                trend = "declining"

        return {
            "score_today": score_today,
            "score_7d": score_7d,
            "score_30d": score_30d,
            "trend": trend,
        }

    @property
    def daily_insight(self) -> dict[str, Any]:
        """Daily insight report — Ellevio + Nordpool analysis.

        Deep analysis with >90% confidence recommendations only.
        Updated every hour, comprehensive at 07:55 for morning email.
        """
        now = datetime.now()

        # ── Ellevio weighted hourly averages (last 24h) ──────────
        peaks = list(self._ellevio_monthly_hourly_peaks)
        last_24 = peaks[-24:] if len(peaks) >= 24 else peaks

        if not last_24:
            return {"status": "collecting", "message": "Samlar data — behöver 24h"}

        ellevio_max = round(max(last_24), 2)
        ellevio_min = round(min(last_24), 2)
        ellevio_avg = round(sum(last_24) / len(last_24), 2)
        ellevio_gap = round(ellevio_max - ellevio_min, 2)

        # Find worst/best hours from decision log
        worst_hour = -1
        best_hour = -1
        worst_kw = 0.0
        best_kw = 999.0
        worst_reason = ""
        best_reason = ""

        for d in self.decision_log:
            if d.weighted_kw > worst_kw:
                worst_kw = d.weighted_kw
                worst_hour = int(d.timestamp.split("T")[1][:2]) if "T" in d.timestamp else -1
                worst_reason = d.reason
            if 0 < d.weighted_kw < best_kw:
                best_kw = d.weighted_kw
                best_hour = int(d.timestamp.split("T")[1][:2]) if "T" in d.timestamp else -1
                best_reason = d.reason

        # ── Nordpool cost analysis (last 24h) ─────────────────────
        hourly_costs: list[dict[str, float]] = []
        total_cost_kr = 0.0
        total_kwh = 0.0

        for d in self.decision_log:
            if d.grid_kw > 0 and d.price_ore > 0:
                # Each decision covers ~30 min (0.5h)
                kwh = d.grid_kw * 0.5
                cost_kr = kwh * d.price_ore / 100
                total_cost_kr += cost_kr
                total_kwh += kwh
                hr = int(d.timestamp.split("T")[1][:2]) if "T" in d.timestamp else 0
                hourly_costs.append(
                    {
                        "hour": hr,
                        "cost_kr": round(cost_kr, 2),
                        "price_ore": d.price_ore,
                        "kwh": round(kwh, 2),
                    }
                )

        avg_price = round(total_cost_kr / total_kwh * 100, 1) if total_kwh > 0 else 0.0
        cheapest = min(hourly_costs, key=lambda x: x["price_ore"]) if hourly_costs else {}
        most_expensive = max(hourly_costs, key=lambda x: x["price_ore"]) if hourly_costs else {}

        # ── Deep analysis: WHY worst/best hours happened ──────────
        worst_analysis = self._analyze_hour(worst_hour, "worst")
        best_analysis = self._analyze_hour(best_hour, "best")

        # ── Recommendations (only >90% confidence) ────────────────
        recommendations: list[dict[str, Any]] = []

        # R1: If max > 2x target → high confidence suggestion
        if ellevio_max > self.target_kw * 2:
            recommendations.append(
                {
                    "confidence": 95,
                    "category": "effekt",
                    "sv": (
                        f"Effekttoppen {ellevio_max:.1f} kW är dubbelt mot "
                        f"målet {self.target_kw:.1f} kW. "
                        f"Orsak: {worst_reason[:80]}. "
                        f"Åtgärd: Undvik att köra tunga laster "
                        f"(tork, ugn, EV) samtidigt kl {worst_hour:02d}."
                    ),
                }
            )

        # R2: If gap > 1.5 kW → spread loads
        if ellevio_gap > 1.5:
            recommendations.append(
                {
                    "confidence": 92,
                    "category": "effekt",
                    "sv": (
                        f"Gapet mellan bästa ({ellevio_min:.1f} kW) och "
                        f"sämsta ({ellevio_max:.1f} kW) timmen är {ellevio_gap:.1f} kW. "
                        f"Flytta tung last från kl {worst_hour:02d} till "
                        f"kl {best_hour:02d} för att jämna ut."
                    ),
                }
            )

        # R3: If most expensive hour > 2x cheapest → shift consumption
        if most_expensive and cheapest:
            price_ratio = most_expensive["price_ore"] / max(1, cheapest["price_ore"])
            if price_ratio > 2:
                savings_potential = most_expensive["cost_kr"] * 0.5
                recommendations.append(
                    {
                        "confidence": 93,
                        "category": "pris",
                        "sv": (
                            f"Dyraste timmen (kl {most_expensive['hour']:02d}, "
                            f"{most_expensive['price_ore']:.0f} öre) kostade "
                            f"{most_expensive['cost_kr']:.1f} kr. "
                            f"Billigaste (kl {cheapest['hour']:02d}, "
                            f"{cheapest['price_ore']:.0f} öre) kostade "
                            f"{cheapest['cost_kr']:.1f} kr. "
                            f"Flytta förbrukning → spara ~{savings_potential:.0f} kr/dag."
                        ),
                    }
                )

        # R4: If battery was idle during expensive hours
        expensive_idle = sum(
            1
            for d in self.decision_log
            if d.price_ore > 80 and d.action == "idle" and d.battery_soc > 30
        )
        if expensive_idle > 2:
            recommendations.append(
                {
                    "confidence": 91,
                    "category": "batteri",
                    "sv": (
                        f"Batteriet vilade {expensive_idle} gånger under dyra "
                        f"timmar (>80 öre) trots kapacitet. "
                        f"Sänk urladdningströskeln eller justera target."
                    ),
                }
            )

        return {
            "status": "ready",
            "generated": now.isoformat(),
            # Ellevio
            "ellevio_max_kw": ellevio_max,
            "ellevio_min_kw": ellevio_min,
            "ellevio_avg_kw": ellevio_avg,
            "ellevio_gap_kw": ellevio_gap,
            "worst_hour": worst_hour,
            "worst_kw": round(worst_kw, 2),
            "worst_reason": worst_reason[:100],
            "worst_analysis": worst_analysis,
            "best_hour": best_hour,
            "best_kw": round(best_kw, 2),
            "best_reason": best_reason[:100],
            "best_analysis": best_analysis,
            # Nordpool
            "total_cost_kr": round(total_cost_kr, 1),
            "total_kwh": round(total_kwh, 1),
            "avg_price_ore": avg_price,
            "cheapest_hour": cheapest.get("hour", -1),
            "cheapest_price_ore": cheapest.get("price_ore", 0),
            "cheapest_cost_kr": cheapest.get("cost_kr", 0),
            "most_expensive_hour": most_expensive.get("hour", -1),
            "most_expensive_price_ore": most_expensive.get("price_ore", 0),
            "most_expensive_cost_kr": most_expensive.get("cost_kr", 0),
            # Recommendations (only >90% confidence)
            "recommendations": recommendations,
            "recommendation_count": len(recommendations),
        }

    def _analyze_hour(self, hour: int, label: str) -> str:
        """Deep-analyze what caused a specific hour to be worst/best."""
        if hour < 0:
            return "Otillräcklig data"

        relevant = [
            d
            for d in self.decision_log
            if "T" in d.timestamp and int(d.timestamp.split("T")[1][:2]) == hour
        ]
        if not relevant:
            return f"Ingen data för kl {hour:02d}"

        d = relevant[-1]  # Most recent for that hour
        parts: list[str] = []

        if d.pv_kw > 0.5:
            parts.append(f"sol {d.pv_kw:.1f} kW")
        if d.grid_kw > 1.0:
            parts.append(f"nätimport {d.grid_kw:.1f} kW")
        if d.battery_soc < 20:
            parts.append("batteri lågt")
        elif d.battery_soc > 95:
            parts.append("batteri fullt")
        if d.action == "discharge":
            parts.append(f"urladdning {d.discharge_w}W")
        elif d.action == "charge_pv":
            parts.append("solladdar")
        elif d.action == "idle":
            parts.append("vilar")

        if label == "worst":
            if d.grid_kw > 2.0 and d.pv_kw < 0.5:
                parts.append("→ hög last utan sol")
            elif d.action == "idle" and d.battery_soc > 30:
                parts.append("→ batteri outnyttjat")
        elif label == "best":
            if d.pv_kw > 2.0:
                parts.append("→ sol drev förbrukningen")
            elif d.action == "discharge":
                parts.append("→ batteri sänkte toppen")

        return ", ".join(parts) if parts else "Normal drift"

    @property
    def rule_flow(self) -> dict[str, Any]:
        """Visual rule flow — how CARMA Box thinks, for dashboard display.

        Returns a structured representation of the decision tree
        that 901 can render as a visual flowchart.
        Each node has: id, label (Swedish), status (active/inactive/blocked),
        and connections to next nodes.
        """
        d = self.last_decision
        action = d.action if d else "idle"
        pv_active = d.pv_kw > 0.5 if d else False
        is_exporting = d.grid_kw <= 0 if d else False
        bat_full = d.battery_soc >= 99 if d else False
        price = d.price_ore if d else 0
        price_cheap = float(self._cfg.get("price_cheap_ore", DEFAULT_PRICE_CHEAP_ORE))
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))

        nodes = [
            {
                "id": "pv_check",
                "label": "Sol producerar?",
                "icon": "mdi:weather-sunny",
                "status": "active" if pv_active else "inactive",
                "value": f"{d.pv_kw:.1f} kW" if d else "0 kW",
            },
            {
                "id": "charge_battery",
                "label": "Ladda batteri",
                "icon": "mdi:battery-charging",
                "status": "active" if action == "charge_pv" else "inactive",
                "condition": "PV > 0.5 kW + batteri ej fullt",
            },
            {
                "id": "charge_ev",
                "label": "Ladda EV",
                "icon": "mdi:car-electric",
                "status": "active" if self._ev_enabled else "inactive",
                "condition": "Exporterar + kabel inkopplad",
            },
            {
                "id": "miner",
                "label": "Miner",
                "icon": "mdi:pickaxe",
                "status": "active" if self._miner_on else "inactive",
                "condition": (
                    "Export > "
                    f"{self._cfg.get('miner_start_export_w', DEFAULT_MINER_START_EXPORT_W)}W"
                ),
            },
            {
                "id": "export",
                "label": "Exportera",
                "icon": "mdi:transmission-tower-export",
                "status": "active" if is_exporting and bat_full else "inactive",
                "condition": "Allt fullt, inget att göra med elen",
            },
            {
                "id": "price_check",
                "label": "Priskontroll",
                "icon": "mdi:currency-usd",
                "status": "active",
                "value": f"{price:.0f} öre" if price else "?",
                "tier": (
                    "billigt"
                    if price < price_cheap
                    else "dyrt"
                    if price > price_expensive
                    else "normalt"
                ),
            },
            {
                "id": "discharge",
                "label": "Ladda ur batteri",
                "icon": "mdi:battery-arrow-down",
                "status": "active" if action == "discharge" else "inactive",
                "condition": f"Grid > mål {self.target_kw:.1f} kW",
            },
            {
                "id": "idle",
                "label": "Vila",
                "icon": "mdi:sleep",
                "status": "active" if action == "idle" else "inactive",
                "condition": "Grid under mål — batteriet vilar",
            },
        ]

        # Safety guards
        guards = [
            {
                "id": "guard_crosscharge",
                "label": "Korsladdningsskydd",
                "icon": "mdi:shield-check",
                "status": "ok",
            },
            {
                "id": "guard_min_soc",
                "label": f"Min batteri {self.min_soc:.0f}%",
                "icon": "mdi:battery-alert",
                "status": "ok" if (d and d.battery_soc > self.min_soc) else "warning",
            },
            {
                "id": "guard_ev_max",
                "label": f"EV max {DEFAULT_EV_MAX_AMPS}A",
                "icon": "mdi:flash-alert",
                "status": "ok",
            },
        ]

        # Active rule path
        active_path: list[str] = []
        if pv_active:
            active_path.append("pv_check")
            if action == "charge_pv":
                active_path.append("charge_battery")
            if self._ev_enabled:
                active_path.append("charge_ev")
            if self._miner_on:
                active_path.append("miner")
        else:
            active_path.append("price_check")
            if action == "discharge":
                active_path.append("discharge")
            elif action == "idle":
                active_path.append("idle")

        return {
            "nodes": nodes,
            "guards": guards,
            "active_path": active_path,
            "active_rule": action,
            "summary_sv": d.reason if d else "Startar...",
        }

    def _check_repair_issues(self) -> None:
        """Check conditions and raise/clear HA repair issues."""
        try:
            # SafetyGuard frequent blocks
            blocks = self.safety.recent_block_count(3600)
            if isinstance(blocks, int) and blocks >= SAFETY_BLOCK_THRESHOLD:
                raise_safety_guard_issue(self.hass, blocks)
            else:
                clear_issue(self.hass, "safety_guard_frequent_blocks")

            # Hub offline >24h (check if hub attribute exists on coordinator)
            hub = getattr(self, "_hub", None)
            if hub is not None:
                last_sync = hub.last_sync
                if last_sync is not None:
                    offline_seconds = (datetime.now() - last_sync).total_seconds()
                    if offline_seconds > 86400:
                        hours = int(offline_seconds / 3600)
                        raise_hub_offline_issue(self.hass, hours)
                    else:
                        clear_issue(self.hass, "hub_offline")
        except (HomeAssistantError, AttributeError, RuntimeError):
            _LOGGER.debug("Repair issue check failed", exc_info=True)

    def _reset_daily_counters_if_new_day(self, now: datetime) -> None:
        """Reset daily counters at midnight."""
        today = now.strftime("%Y-%m-%d")
        if today != self._current_date:
            _LOGGER.info("CARMA: new day %s — resetting daily counters", today)
            self._daily_discharge_kwh = 0.0
            self._daily_safety_blocks = 0
            self._daily_plans = 0
            self.appliance_energy_wh = {}
            self._current_date = today
            self._update_daily_avg_price()

        # Morning report at 06:00 (once per day)
        if now.hour == 6 and now.minute < 1:
            self.hass.async_create_task(
                self._send_morning_report(),
                "carmabox_morning_report",
            )

    async def _send_morning_report(self) -> None:
        """Send morning battery/energy report at 06:00."""
        try:
            soc_k = self._read_float(self._get_entity("battery_soc_1"))
            soc_f = self._read_float(self._get_entity("battery_soc_2"))
            ev_soc = self._read_float(self._get_entity("ev_soc_entity"), -1)
            # Yesterday's summary from ledger
            from datetime import timedelta

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            summary = self.ledger.daily_summary(yesterday)
            cost = summary.get("total_cost_kr", 0)
            saved = summary.get("battery_net_saving_kr", 0)
            price = self._read_float(self._get_entity("price_entity"), 0)
            await self.notifier.morning_report(
                soc_k,
                soc_f,
                ev_soc,
                cost,
                saved,
                price,
            )
        except Exception as e:
            _LOGGER.debug("Morning report failed: %s", e)

    def _update_daily_avg_price(self) -> None:
        """Calculate daily average price from Nordpool today_prices."""
        price_entity = self._get_entity("price_entity", "")
        if not price_entity:
            return
        fallback = float(self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
        adapter = NordpoolAdapter(self.hass, price_entity, fallback)
        prices = adapter.today_prices
        if prices and not all(p == fallback for p in prices):
            self._daily_avg_price = sum(prices) / len(prices)

    def _track_appliances(self) -> None:
        """PLAT-943: Read appliance power sensors and accumulate energy."""
        category_power: dict[str, float] = {}
        interval_h = SCAN_INTERVAL_SECONDS / 3600

        for app in self._appliances:
            entity_id = app.get("entity_id", "")
            category = app.get("category", "other")
            threshold = float(app.get("threshold_w", 10))
            power_w = self._read_float(entity_id)

            # Convert kW sensors to W
            unit = ""
            state = self.hass.states.get(entity_id)
            if state:
                unit = (state.attributes.get("unit_of_measurement") or "").lower()
            if unit == "kw":
                power_w = power_w * 1000

            if power_w < threshold:
                power_w = 0.0

            category_power[category] = category_power.get(category, 0.0) + power_w
            # Accumulate energy (Wh) = power_w x interval_hours
            self.appliance_energy_wh[category] = (
                self.appliance_energy_wh.get(category, 0.0) + power_w * interval_h
            )

        self.appliance_power = category_power

    # ── Breach Prevention Monitor ─────────────────────────────────

    # Max stored corrections to prevent unbounded memory growth (K1)
    _MAX_CORRECTIONS: int = 100
    # Max samples per hour — protects against extra refreshes (K3)
    _MAX_HOUR_SAMPLES: int = 150

    def _update_hourly_meter(self, state: CarmaboxState) -> None:
        """Track rolling hourly average and project where hour will end."""
        from .optimizer.grid_logic import ellevio_weight

        now = datetime.now()
        hour = now.hour
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_weight)
        grid_kw = max(0, state.grid_power_w) / 1000
        weighted_kw = grid_kw * weight

        if hour != self._meter_state.hour:
            # V5: Keep load shed active if projected was still high at hour end
            prev_projected = self._meter_state.projected_avg
            if self._meter_state.hour >= 0 and self._meter_state.samples:
                final_avg = sum(self._meter_state.samples) / len(self._meter_state.samples)
                if final_avg > self.target_kw:
                    _LOGGER.warning(
                        "Breach Monitor: kl %02d slutade på %.2f kW (target %.1f)",
                        self._meter_state.hour,
                        final_avg,
                        self.target_kw,
                    )
                    self._generate_breach_corrections(state, self._meter_state.hour, final_avg)
            self._meter_state = HourlyMeterState(hour=hour)
            # V5: Carry over load shed if previous hour ended high
            if prev_projected > self.target_kw * 0.90:
                self._breach_load_shed_active = True
                self._meter_state.load_shed_active = True
            else:
                self._breach_load_shed_active = False

        # K3: Cap samples to prevent unbounded growth from extra refreshes
        if len(self._meter_state.samples) < self._MAX_HOUR_SAMPLES:
            self._meter_state.samples.append(weighted_kw)
        if weighted_kw > self._meter_state.peak_sample:
            self._meter_state.peak_sample = weighted_kw

        n = len(self._meter_state.samples)
        current_avg = sum(self._meter_state.samples) / n
        # K3: Clamp remaining to avoid negative/zero when n > 120
        expected_total = 120  # 30s intervals x 60 min
        remaining = max(1, expected_total - min(n, expected_total))
        recent = self._meter_state.samples[-5:] if n >= 5 else self._meter_state.samples
        recent_avg = sum(recent) / len(recent)
        projected = (current_avg * n + recent_avg * remaining) / (n + remaining)
        self._meter_state.projected_avg = round(projected, 3)

        target = self.target_kw
        if projected > target * 0.80 and not self._meter_state.warning_issued:
            self._meter_state.warning_issued = True
            _LOGGER.warning(
                "Breach Monitor VARNING: kl %02d projiceras %.2f kW (target %.1f)",
                hour,
                projected,
                target,
            )
        if projected > target * 0.90 and not self._breach_load_shed_active and n > 10:
            self._breach_load_shed_active = True
            self._meter_state.load_shed_active = True
            _LOGGER.error(
                "Breach Monitor NÖDSTOPP: kl %02d projiceras %.2f kW (target %.1f)",
                hour,
                projected,
                target,
            )

    @property
    def breach_monitor_active(self) -> bool:
        """True if load shedding is active due to projected breach."""
        return self._breach_load_shed_active

    @property
    def hourly_meter_projected(self) -> float:
        """Current projected hourly weighted average (kW)."""
        return self._meter_state.projected_avg

    @property
    def hourly_meter_pct(self) -> float:
        """Current projected hour as % of target."""
        if self.target_kw <= 0:
            return 0.0
        return round(self._meter_state.projected_avg / self.target_kw * 100, 1)

    def _generate_breach_corrections(
        self,
        state: CarmaboxState,
        breach_hour: int,
        actual_avg: float,
    ) -> None:
        """Generate automatic corrections after a confirmed breach.

        V1 fix: target_hour = same hour TOMORROW (not already-passed hour).
        K1 fix: Hard cap on total corrections.
        K2 fix: Guard battery_power_2 with has_battery_2.
        """
        now = datetime.now()
        excess = actual_avg - self.target_kw
        corrections: list[BreachCorrection] = []
        # V1: Corrections target the SAME hour tomorrow, not the passed hour
        target_h = breach_hour  # Same clock hour, but scheduler plans 24h ahead

        if state.ev_power_w > 500:
            corrections.append(
                BreachCorrection(
                    created=now.isoformat(),
                    source_breach_hour=breach_hour,
                    action="reduce_ev",
                    target_hour=target_h,
                    param=f"ev_amps={DEFAULT_EV_MIN_AMPS}",
                    reason=(
                        f"EV {state.ev_power_w:.0f}W orsakade breach"
                        f" kl {breach_hour:02d} — sänk till {DEFAULT_EV_MIN_AMPS}A imorgon"
                    ),
                )
            )
        if self._miner_on:
            corrections.append(
                BreachCorrection(
                    created=now.isoformat(),
                    source_breach_hour=breach_hour,
                    action="reduce_load",
                    target_hour=target_h,
                    param="pause_miner",
                    reason=(f"Miner körde under breach kl {breach_hour:02d} — pausa imorgon"),
                )
            )
        # K2: Guard battery_power_2
        bat2 = state.battery_power_2 if getattr(state, "has_battery_2", True) else 0.0
        bat_total = state.battery_power_1 + bat2
        if bat_total >= -50:
            discharge_kw = min(excess + 0.5, 4.0)
            corrections.append(
                BreachCorrection(
                    created=now.isoformat(),
                    source_breach_hour=breach_hour,
                    action="add_discharge",
                    target_hour=target_h,
                    param=f"discharge_kw={discharge_kw:.1f}",
                    reason=(
                        f"Batteri idle kl {breach_hour:02d}"
                        f" — schemalägg {discharge_kw:.1f} kW urladdning"
                    ),
                )
            )

        # Expire old (>24h) with safe parsing
        cutoff = now.timestamp() - 86400
        kept: list[BreachCorrection] = []
        for c in self._breach_corrections:
            if c.expired:
                continue
            try:
                if datetime.fromisoformat(c.created).timestamp() > cutoff:
                    kept.append(c)
            except (ValueError, TypeError):
                c.expired = True  # Mark corrupt entries as expired
        self._breach_corrections = kept
        self._breach_corrections.extend(corrections)
        # K1: Hard cap on total corrections
        if len(self._breach_corrections) > self._MAX_CORRECTIONS:
            self._breach_corrections = self._breach_corrections[-self._MAX_CORRECTIONS :]
        if corrections:
            _LOGGER.warning(
                "Breach Monitor: %d korrigeringar för kl %02d (totalt %d aktiva)",
                len(corrections),
                breach_hour,
                len(self._breach_corrections),
            )

    def get_active_corrections(self, hour: int | None = None) -> list[BreachCorrection]:
        """Get active (non-expired, non-applied) corrections."""
        return [
            c
            for c in self._breach_corrections
            if not c.expired and not c.applied and (hour is None or c.target_hour == hour)
        ]

    def _track_battery_idle(self, state: CarmaboxState) -> None:
        """Track battery idle time and feed predictor with idle penalties.

        K2 fix: Guard battery_power_2 with has_battery_2.
        K4 fix: Clamp idle_pct in daily log.
        V3 fix: NordpoolAdapter imported at module level.
        """
        now = datetime.now()
        if now.day != self._bat_idle_day:
            idle_pct = min(100, self._bat_daily_idle_seconds * 100 // 86400)
            _LOGGER.info(
                "Battery idle yesterday: %d min (%d%%)",
                self._bat_daily_idle_seconds // 60,
                idle_pct,
            )
            self._bat_daily_idle_seconds = 0
            self._bat_idle_day = now.day

        # K2: Guard battery_power_2
        bat2 = state.battery_power_2 if getattr(state, "has_battery_2", True) else 0.0
        bat_power = abs(state.battery_power_1 + bat2)
        if bat_power < 50:
            self._bat_idle_seconds += SCAN_INTERVAL_SECONDS
            self._bat_daily_idle_seconds += SCAN_INTERVAL_SECONDS
        else:
            idle_secs = self._bat_idle_seconds
            if idle_secs > 1800:
                price_entity = self._get_entity("price_entity", "")
                fallback = float(self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
                # V3: NordpoolAdapter already imported at module level
                pa = NordpoolAdapter(self.hass, price_entity, fallback)
                cur = pa.current_price
                today = pa.today_prices
                avg = sum(today) / len(today) if today else 0
                if cur and avg and abs(cur - avg) > 15:
                    self.predictor.add_idle_penalty(
                        hour=now.hour,
                        weekday=now.weekday(),
                        idle_minutes=idle_secs // 60,
                        price_spread_ore=abs(cur - avg),
                    )
            self._bat_idle_seconds = 0

    def _track_shadow(self, state: CarmaboxState) -> None:
        """PLAT-940: Compare CARMA recommendation vs v6 actual behavior."""
        hour = datetime.now().hour
        night_wt = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_wt)
        interval_hours = SCAN_INTERVAL_SECONDS / 3600

        # Detect what v6 is ACTUALLY doing from battery power direction
        total_battery_w = state.battery_power_1 + state.battery_power_2
        if total_battery_w < -100:
            actual_action = "discharge"
            actual_discharge_w = int(abs(total_battery_w))
        elif total_battery_w > 100:
            actual_action = "charge"
            actual_discharge_w = 0
        else:
            actual_action = "idle"
            actual_discharge_w = 0

        # What CARMA recommends
        carma = self.last_decision
        carma_action = carma.action
        carma_discharge_w = carma.discharge_w

        # Actual weighted grid
        actual_grid_kw = max(0, state.grid_power_w) / 1000
        actual_weighted = actual_grid_kw * weight

        # What CARMA's weighted grid WOULD be
        # If CARMA says discharge X W → grid would be reduced by X W
        if carma_action == "discharge" and carma_discharge_w > 0:
            carma_grid_kw = max(
                0, actual_grid_kw - carma_discharge_w / 1000 + actual_discharge_w / 1000
            )
        elif carma_action == "standby" and actual_action == "discharge":
            # CARMA says standby but v6 discharges → grid would be higher
            carma_grid_kw = actual_grid_kw + actual_discharge_w / 1000
        else:
            carma_grid_kw = actual_grid_kw
        carma_weighted = carma_grid_kw * weight

        # Agreement?
        agreement = carma_action == actual_action

        # Value difference: lower weighted peak = savings
        # Ellevio cost per kW per month = peak_cost_per_kw
        # But we calculate per-sample contribution to hourly average
        peak_cost = float(self._cfg.get("peak_cost_per_kw", 80.0))
        # Rough: each 30s sample contributes 1/120 of an hour
        # If CARMA has lower weighted kW → it would reduce the peak → saves money
        delta_weighted = actual_weighted - carma_weighted  # Positive = CARMA is better
        # Annual cost impact (very rough): delta x peak_cost / samples_per_hour
        carma_better_kr = delta_weighted * peak_cost / 120 if delta_weighted > 0.01 else 0.0

        # Also price optimization: if CARMA says "don't discharge" at cheap price but v6 does
        if (
            actual_action == "discharge"
            and carma_action != "discharge"
            and state.current_price < self._daily_avg_price
        ):
            # v6 discharges at cheap price = waste. CARMA saved that.
            wasted_kwh = actual_discharge_w / 1000 * interval_hours
            carma_better_kr += wasted_kwh * (self._daily_avg_price - state.current_price) / 100

        self._shadow_savings_kr += carma_better_kr

        reason = ""
        if not agreement:
            if carma_action == "idle" and actual_action == "discharge":
                reason = (
                    f"v6 laddar ur vid {state.current_price:.0f} öre"
                    " — CARMA hade vilat (sparar batteri till dyrare timmar)"
                )
            elif carma_action == "discharge" and actual_action == "idle":
                reason = (
                    f"v6 vilar men grid {actual_grid_kw:.1f} kW > target"
                    f" — CARMA hade laddat ur {carma_discharge_w}W"
                )
            elif carma_action == "standby" and actual_action == "discharge":
                reason = "v6 laddar ur onödigt — batterier fulla, CARMA hade standby"
            else:
                reason = f"CARMA: {carma_action}, v6: {actual_action}"

        shadow = ShadowComparison(
            timestamp=datetime.now().isoformat(),
            carma_action=carma_action,
            actual_action=actual_action,
            carma_discharge_w=carma_discharge_w,
            actual_discharge_w=actual_discharge_w,
            carma_weighted_kw=round(carma_weighted, 2),
            actual_weighted_kw=round(actual_weighted, 2),
            price_ore=round(state.current_price, 1),
            agreement=agreement,
            carma_better_kr=round(carma_better_kr, 4),
            reason=reason,
        )
        self.shadow = shadow

        # Keep last 48 comparisons
        self.shadow_log.append(shadow)
        if len(self.shadow_log) > 48:
            self.shadow_log = self.shadow_log[-48:]

    def _track_savings(self, state: CarmaboxState) -> None:
        """Track savings data from current state."""
        hour = datetime.now().hour
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_weight)
        # Net load for peak tracking (what Ellevio meters see)
        grid_kw = max(0, state.grid_power_w) / 1000
        weighted_kw = grid_kw * weight

        # Baseline: what grid would be without battery (grid + battery discharge)
        battery_discharge_kw = 0.0
        if state.battery_power_1 < 0:
            battery_discharge_kw += abs(state.battery_power_1) / 1000
        if state.battery_power_2 < 0:
            battery_discharge_kw += abs(state.battery_power_2) / 1000
        baseline_kw = (grid_kw + battery_discharge_kw) * weight

        # Record peak sample — accumulate for hourly average
        # Ellevio measures HOURLY averages, not instantaneous peaks.
        # We collect 30s samples and record the hourly avg at hour change.
        if not hasattr(self, "_peak_hour_samples"):
            self._peak_hour_samples = []  # (actual, baseline) — annotated in __init__
            self._peak_last_hour = -1
        if hour != self._peak_last_hour:
            if self._peak_hour_samples and self._peak_last_hour >= 0:
                n = len(self._peak_hour_samples)
                avg_actual = sum(s[0] for s in self._peak_hour_samples) / n
                avg_baseline = sum(s[1] for s in self._peak_hour_samples) / n
                record_peak(self.savings, avg_actual, avg_baseline)
            self._peak_hour_samples = []
            self._peak_last_hour = hour
        self._peak_hour_samples.append((weighted_kw, baseline_kw))

        # Record discharge savings (30s interval → /120 for kWh)
        interval_hours = SCAN_INTERVAL_SECONDS / 3600
        if battery_discharge_kw > 0 and state.current_price > 0:
            record_discharge(
                self.savings,
                battery_discharge_kw * interval_hours,
                state.current_price,
                self._daily_avg_price,
            )
            self._daily_discharge_kwh += battery_discharge_kw * interval_hours
            # PLAT-924: Accumulate discharge value
            self.savings.discharge_offset_kwh += battery_discharge_kw * interval_hours
            self.savings.discharge_offset_value_ore += (
                battery_discharge_kw * interval_hours * state.current_price
            )

        # Record grid charge savings (battery charging while importing from grid)
        battery_charge_kw = 0.0
        if state.battery_power_1 > 0:
            battery_charge_kw += state.battery_power_1 / 1000
        if state.battery_power_2 > 0:
            battery_charge_kw += state.battery_power_2 / 1000
        if battery_charge_kw > 0 and state.grid_power_w > 0 and state.current_price > 0:
            grid_charge_kw = min(battery_charge_kw, state.grid_power_w / 1000)
            record_grid_charge(
                self.savings,
                grid_charge_kw * interval_hours,
                state.current_price,
                self._daily_avg_price,
            )
            # PLAT-924/926: Accumulate grid charge cost + price samples
            charge_kwh = grid_charge_kw * interval_hours
            self.savings.charge_from_grid_kwh += charge_kwh
            self.savings.charge_from_grid_cost_ore += charge_kwh * state.current_price
            self.savings.grid_charge_prices.append(state.current_price)
            if len(self.savings.grid_charge_prices) > 2000:
                self.savings.grid_charge_prices = self.savings.grid_charge_prices[-2000:]

        # What-if cost tracking
        consumption_kw = max(0, state.grid_power_w) / 1000 + battery_discharge_kw
        record_cost_estimate(
            self.savings,
            consumption_kw * interval_hours,
            state.current_price,
            battery_discharge_kw * interval_hours,
        )

        # Daily savings snapshot for trend graph
        today = datetime.now().strftime("%Y-%m-%d")
        cost = float(self._cfg.get("peak_cost_per_kw", DEFAULT_PEAK_COST_PER_KW))
        record_daily_snapshot(self.savings, today, cost)

        # Record daily sample for monthly report
        sample = DailySample(
            date=today,
            peak_kw=weighted_kw,
            baseline_peak_kw=baseline_kw,
            discharge_kwh=self._daily_discharge_kwh,
            safety_blocks=self._daily_safety_blocks,
            plans_generated=self._daily_plans,
        )
        record_daily_sample(self.report_collector, sample)

        # PLAT-998: Record to hourly energy ledger (actual cost tracking)
        total_battery_w = state.battery_power_1 + (
            state.battery_power_2 if state.has_battery_2 else 0
        )

        # CARMA-LEDGER-FIELDS: Calculate new fields
        solar_w = state.pv_power_w
        miner_w = self.appliance_power.get("miner", 0.0)

        # House consumption = grid + battery_discharge + pv - battery_charge - export
        # Approximate as total power flow into house
        battery_discharge_w = max(0, -total_battery_w)
        battery_charge_w = max(0, total_battery_w)
        grid_import_w = max(0, state.grid_power_w)
        grid_export_w = max(0, -state.grid_power_w)
        house_w = (
            grid_import_w
            + battery_discharge_w
            + state.pv_power_w
            - battery_charge_w
            - grid_export_w
        )

        # Battery SoC average
        battery_soc = state.total_battery_soc

        # EV SoC
        ev_soc = state.ev_soc if state.has_ev else 0.0

        # Current action from last decision
        action = self.last_decision.action if self.last_decision else "idle"

        # Outdoor temperature — read from sensor
        temp_entity = self._cfg.get("outdoor_temp_entity", "sensor.sanduddsvagen_60_temperature")
        temperature_c = self._read_float(temp_entity, 0.0)

        # IT-1936: Read per-appliance sensors (configurable via opts)
        _c = self._cfg
        tvatt_w = self._read_float(
            _c.get("appliance_tvatt", "sensor.102_shelly_plug_g3_power"), 0.0
        )
        tork_w = self._read_float(_c.get("appliance_tork", "sensor.103_shelly_plug_g3_power"), 0.0)
        disk_w = self._read_float(_c.get("appliance_disk", "sensor.98_shelly_plug_s_power"), 0.0)
        vp_kontor_w = self._read_float(
            _c.get("vp_kontor", "sensor.kontor_varmepump_alltid_pa_switch_0_power"), 0.0
        )
        vp_pool_w = self._read_float(_c.get("vp_pool", "sensor.poolvarmare_shelly_1pm_power"), 0.0)
        cirk_pool_w = self._read_float(
            _c.get("cirk_pump", "sensor.gv_cirkulationspump_effekt"), 0.0
        )

        self.ledger.record_sample(
            hour=hour,
            date_str=today,
            grid_w=state.grid_power_w,
            battery_w=total_battery_w,
            pv_w=state.pv_power_w,
            ev_w=state.ev_power_w,
            price_ore=state.current_price,
            weighted_kw=weighted_kw,
            is_exporting=state.is_exporting,
            interval_s=SCAN_INTERVAL_SECONDS,
            appliance_power=self.appliance_power,
            solar_w=solar_w,
            house_w=house_w,
            miner_w=miner_w,
            battery_soc=battery_soc,
            ev_soc=ev_soc,
            action=action,
            temperature_c=temperature_c,
            # IT-1936: Pass per-appliance power
            tvatt_w=tvatt_w,
            tork_w=tork_w,
            disk_w=disk_w,
            vp_kontor_w=vp_kontor_w,
            vp_pool_w=vp_pool_w,
            cirk_pool_w=cirk_pool_w,
            # IT-1948: Pass battery cell temperatures
            cell_temp_kontor_c=state.battery_min_cell_temp_1,
            cell_temp_forrad_c=state.battery_min_cell_temp_2,
        )
        # CARMA-P0-FIXES Task 4: Save ledger after recording (rate-limited)
        self.hass.async_create_task(
            self._async_save_ledger(),
            "carmabox_save_ledger",
        )

        # PLAT-927: Ellevio realtime — rolling hourly weighted average
        now_hour = datetime.now().hour
        if now_hour != self._ellevio_current_hour:
            if self._ellevio_hour_samples and self._ellevio_current_hour >= 0:
                total_w = sum(p * w for p, w in self._ellevio_hour_samples)
                total_wt = sum(w for _, w in self._ellevio_hour_samples)
                if total_wt > 0:
                    self._ellevio_monthly_hourly_peaks.append(total_w / total_wt)
                    if len(self._ellevio_monthly_hourly_peaks) > 800:
                        self._ellevio_monthly_hourly_peaks = self._ellevio_monthly_hourly_peaks[
                            -744:
                        ]
            self._ellevio_hour_samples = []
            self._ellevio_current_hour = now_hour
        self._ellevio_hour_samples.append((grid_kw, weight))

        # Track plan vs actual (once per hour)
        now_obj = datetime.now()
        if now_obj.hour != self._last_tracked_hour:
            self._last_tracked_hour = now_obj.hour
            planned = next((h for h in self.plan if h.hour == now_obj.hour), None)
            actual = HourActual(
                hour=now_obj.hour,
                planned_action=planned.action if planned else "?",
                actual_action=self.last_decision.action,
                planned_grid_kw=round(planned.grid_kw, 2) if planned else 0,
                actual_grid_kw=round(grid_kw, 2),
                planned_weighted_kw=round(planned.weighted_kw, 2) if planned else 0,
                actual_weighted_kw=round(weighted_kw, 2),
                planned_battery_soc=planned.battery_soc if planned else 0,
                actual_battery_soc=int(state.total_battery_soc),
                planned_ev_soc=planned.ev_soc if planned else 0,
                actual_ev_soc=int(state.ev_soc) if state.has_ev else -1,
                price=round(state.current_price, 1),
            )
            self.hourly_actuals.append(actual)
            if len(self.hourly_actuals) > 48:
                self.hourly_actuals = self.hourly_actuals[-48:]

        # Update consumption learning (once per hour to match 7-day learning period)
        now = datetime.now()
        if now.hour != self._consumption_last_hour:
            self._consumption_last_hour = now.hour
            house_kw = calculate_house_consumption(
                state.grid_power_w,
                state.battery_power_1,
                state.battery_power_2,
                state.pv_power_w,
                state.ev_power_w,
            )
            self.consumption_profile.update(
                hour=now.hour,
                consumption_kw=house_kw,
                is_weekend=now.weekday() >= 5,
            )
            # PLAT-965: Feed predictor with hourly consumption
            temp_c = self._read_battery_temp()
            self.predictor.add_sample(
                HourSample(
                    weekday=now.weekday(),
                    hour=now.hour,
                    month=now.month,
                    consumption_kw=house_kw,
                    temperature_c=temp_c,
                )
            )
            # ML-01/ML-03: Log sample count and training state every hour
            _LOGGER.info(
                "Predictor: %d samples, trained=%s",
                self.predictor.total_samples,
                self.predictor.is_trained,
            )
            if not self.predictor.is_trained:
                from .optimizer.predictor import MIN_TRAINING_SAMPLES

                _LOGGER.info(
                    "ML: %d/%d samples collected",
                    self.predictor.total_samples,
                    MIN_TRAINING_SAMPLES,
                )

    def _feed_predictor_ml(self, state: CarmaboxState) -> None:
        """Feed all ML data to predictor every cycle."""
        from datetime import datetime

        now = datetime.now()
        hour = now.hour
        weekday = now.weekday()

        # Appliance events
        for eid, name in [
            ("sensor.98_shelly_plug_s_power", "disk"),
            ("sensor.102_shelly_plug_g3_power", "tvatt"),
            ("sensor.103_shelly_plug_g3_power", "tork"),
        ]:
            st = self.hass.states.get(eid)
            if st and st.state not in ("unavailable", "unknown", ""):
                try:
                    power_w = float(st.state)
                    if power_w > 500:
                        self.predictor.add_appliance_event(name, power_w / 1000, hour, weekday)
                except (ValueError, TypeError):
                    _LOGGER.debug("Suppressed error", exc_info=True)

        # Temperature correlation
        temp_state = self.hass.states.get("sensor.tempest_temperature")
        if temp_state and temp_state.state not in ("unavailable", "unknown", ""):
            try:
                temp_c = float(temp_state.state)
                house_kw = getattr(self, "_estimated_house_base_kw", 2.0)
                self.predictor.add_temperature_sample(hour, temp_c, house_kw)
            except (ValueError, TypeError):
                _LOGGER.debug("Suppressed error", exc_info=True)

        # Plan feedback (once per hour)
        if hasattr(self, "_last_feedback_hour") and self._last_feedback_hour == hour:
            pass
        else:
            self._last_feedback_hour = hour
            for ph in self.plan:
                if ph.hour == hour:
                    actual_grid = max(0, state.grid_power_w) / 1000
                    self.predictor.add_plan_feedback(hour, ph.grid_kw, actual_grid)
                    # Auto-calibrate seasonal factor from actual vs predicted
                    if ph.grid_kw > 0.1 and self.predictor.is_trained:
                        from datetime import datetime as _dt

                        self.predictor.update_seasonal_factor(
                            _dt.now().month,
                            actual_grid,
                            ph.grid_kw,
                        )
                    break

        # EV usage (once per day at 22:00)
        if hour == 22 and not getattr(self, "_ev_usage_tracked_today", False):
            self._ev_usage_tracked_today = True
            if self._last_known_ev_soc > 0 and state.ev_soc > 0:
                drop = self._last_known_ev_soc - state.ev_soc
                if drop > 0:
                    ev_cap = float(self._cfg.get("ev_capacity_kwh", 98))
                    self.predictor.add_ev_usage(weekday, drop, capacity_kwh=ev_cap)
        elif hour == 0:
            self._ev_usage_tracked_today = False

    def _check_daily_goals(self, state: CarmaboxState) -> dict[str, Any]:
        """Check daily goals and generate root cause if breached.

        Goals:
        1. Ellevio: never exceed target (2 kW day / 4 kW night)
        2. EV SoC >= 75% at 06:00 daily
        3. EV SoC = 100% within 7 days
        4. Minimize PV export (maximize self-consumption)

        Returns dict with goal status + root cause if breached.
        """
        from datetime import datetime

        now = datetime.now()
        results: dict[str, Any] = {}

        # Goal 1: Ellevio max timmedel
        ell_max = self.hass.states.get("sensor.ellevio_dagens_max")
        target_day = float(self._cfg.get("target_kw_day", 2.0))
        float(self._cfg.get("target_kw_night", 4.0))
        if ell_max and ell_max.state not in ("unavailable", "unknown"):
            try:
                max_kw = float(ell_max.state)
                results["ellevio_max_kw"] = max_kw
                results["ellevio_target_kw"] = target_day
                results["ellevio_goal_met"] = max_kw <= target_day + 0.1
                if not results["ellevio_goal_met"]:
                    results["ellevio_breach_kw"] = round(max_kw - target_day, 2)
                    results["ellevio_root_cause"] = (
                        "EV+disk overlap"
                        if max_kw > 5
                        else (
                            "EV 10A burst"
                            if max_kw > 4
                            else "High base load"
                            if max_kw > 3
                            else "Unknown"
                        )
                    )
                    _LOGGER.warning(
                        "CARMA GOAL BREACH: Ellevio max %.2f kW > target %.1f (cause: %s)",
                        max_kw,
                        target_day,
                        results["ellevio_root_cause"],
                    )
            except (ValueError, TypeError):
                _LOGGER.debug("Suppressed error", exc_info=True)

        # Goal 2: EV SoC >= 75% at 06:00
        if now.hour == 6 and now.minute < 15:
            ev_soc = state.ev_soc if state.ev_soc >= 0 else self._last_known_ev_soc
            results["ev_soc_at_06"] = ev_soc
            results["ev_goal_met"] = ev_soc >= 75 or ev_soc < 0  # unknown = no car
            if not results["ev_goal_met"] and ev_soc >= 0:
                results["ev_root_cause"] = (
                    "Charging stopped by HA restart"
                    if ev_soc > 60
                    else ("Insufficient charging time" if ev_soc > 40 else "Car not connected")
                )
                _LOGGER.warning(
                    "CARMA GOAL BREACH: EV SoC %.0f%% < 75%% at 06:00 (cause: %s)",
                    ev_soc,
                    results["ev_root_cause"],
                )

        # Goal 3: EV 100% within 7 days
        days_since = self._days_since_full_charge()
        results["ev_days_since_full"] = days_since
        results["ev_full_charge_goal_met"] = days_since <= 7
        if days_since > 5:
            _LOGGER.info(
                "CARMA: EV full charge due in %d days (last full: %s)",
                7 - days_since,
                self._ev_last_full_charge_date or "unknown",
            )

        # Goal 4: PV self-consumption
        ledger = self.hass.states.get("sensor.carma_box_energy_ledger")
        if ledger:
            attrs: dict[str, Any] = dict(ledger.attributes or {})
            total_solar = attrs.get("total_solar_kwh", 0)
            total_export = attrs.get("total_export_kwh", 0)
            if total_solar > 1:
                self_consumption_pct = round((1 - total_export / total_solar) * 100, 1)
                results["pv_self_consumption_pct"] = self_consumption_pct
                results["pv_goal_met"] = self_consumption_pct >= 80
                results["pv_export_kwh"] = total_export
                if not results["pv_goal_met"]:
                    results["pv_root_cause"] = (
                        "Batteries cold locked"
                        if total_export > 5
                        else ("Battery full + no EV" if total_export > 2 else "Normal surplus")
                    )

        # Track breach statistics + escalation
        today = datetime.now().strftime("%Y-%m-%d")
        for goal in ["ellevio", "ev", "pv"]:
            met_key = f"{goal}_goal_met"
            if met_key in results and not results[met_key]:
                history = self._breach_history.setdefault(goal, [])
                if today not in history:
                    history.append(today)
                # Keep 30 days
                self._breach_history[goal] = history[-30:]
                # Count breaches in last 7 days
                recent = [
                    d
                    for d in history
                    if d
                    >= (datetime.now() - __import__("datetime").timedelta(days=7)).strftime(
                        "%Y-%m-%d"
                    )
                ]
                if len(recent) >= 3:
                    self._breach_escalation[goal] = 2  # CRITICAL
                    _LOGGER.error(
                        "CARMA ESCALATION: %s goal breached %d times in 7 days → CRITICAL",
                        goal,
                        len(recent),
                    )
                elif len(recent) >= 2:
                    self._breach_escalation[goal] = 1  # WARNING
                    _LOGGER.warning(
                        "CARMA ESCALATION: %s goal breached %d times in 7 days → WARNING",
                        goal,
                        len(recent),
                    )
                else:
                    self._breach_escalation[goal] = 0  # Normal (first time)

        results["breach_escalation"] = dict(self._breach_escalation)
        results["breach_history_7d"] = {
            goal: len(
                [
                    d
                    for d in dates
                    if d
                    >= (datetime.now() - __import__("datetime").timedelta(days=7)).strftime(
                        "%Y-%m-%d"
                    )
                ]
            )
            for goal, dates in self._breach_history.items()
        }

        # Goal 5: Electricity cost optimization
        ledger_state = self.hass.states.get("sensor.carma_box_energy_ledger")
        if ledger_state:
            la: dict[str, Any] = dict(ledger_state.attributes or {})
            total_cost = la.get("total_cost_kr", 0)
            without_bat = la.get("without_battery_kr", 0)
            if without_bat > 0.5:
                savings_pct = round((1 - total_cost / without_bat) * 100, 1)
                results["cost_savings_pct"] = savings_pct
                results["cost_actual_kr"] = round(total_cost, 2)
                results["cost_without_kr"] = round(without_bat, 2)
                results["cost_goal_met"] = savings_pct >= 15
                if not results["cost_goal_met"]:
                    results["cost_root_cause"] = (
                        "Batterier ej aktiva (cold lock?)"
                        if savings_pct < 5
                        else (
                            "Laddar vid dyra timmar"
                            if savings_pct < 10
                            else "Liten prisspread idag"
                        )
                    )

        # Goal 6: Battery utilization
        bat1_kwh = float(self._cfg.get("battery_1_kwh", 15.0))
        bat2_kwh = float(self._cfg.get("battery_2_kwh", 5.0))
        total_cap = bat1_kwh + bat2_kwh
        usable_cap = total_cap * (1 - self.min_soc / 100)

        # Track daily SoC min/max for swing
        soc_now = state.total_battery_soc
        day_min = getattr(self, "_bat_day_min_soc", soc_now)
        day_max = getattr(self, "_bat_day_max_soc", soc_now)
        self._bat_day_min_soc = min(day_min, soc_now)
        self._bat_day_max_soc = max(day_max, soc_now)

        swing_pct = self._bat_day_max_soc - self._bat_day_min_soc
        swing_kwh = swing_pct / 100 * total_cap
        capacity_util = round(swing_kwh / usable_cap * 100, 1) if usable_cap > 0 else 0

        # Track active hours (|power| > 100W)
        bat_power = abs(state.battery_power_1 + state.battery_power_2)
        active_samples = getattr(self, "_bat_active_samples", 0)
        total_samples = getattr(self, "_bat_total_samples", 0)
        self._bat_total_samples = total_samples + 1
        if bat_power > 100:
            active_samples += 1
        self._bat_active_samples = active_samples
        active_pct = round(active_samples / max(1, self._bat_total_samples) * 100, 1)
        idle_pct = 100 - active_pct

        # Arbitrage profit
        bat_saving = la.get("battery_net_saving_kr", 0) if ledger_state else 0

        # Combined score
        econ_score = min(1.0, abs(bat_saving) / 5.0) if bat_saving != 0 else 0  # 5 kr = perfect
        active_score = active_pct / 100
        cap_score = capacity_util / 100

        battery_score = round(0.30 * econ_score + 0.30 * active_score + 0.40 * cap_score, 2) * 100

        results["battery_score"] = battery_score
        results["battery_swing_pct"] = swing_pct
        results["battery_swing_kwh"] = round(swing_kwh, 1)
        results["battery_idle_pct"] = idle_pct
        results["battery_active_pct"] = active_pct
        results["battery_arbitrage_kr"] = (
            round(bat_saving, 2) if isinstance(bat_saving, int | float) else 0
        )
        results["battery_goal_met"] = battery_score >= 40
        if not results["battery_goal_met"]:
            results["battery_root_cause"] = (
                "Cold lock (cell temp < 10°C)"
                if swing_pct < 5
                else (
                    "Batterier vilar (ingen arbitrage-möjlighet?)"
                    if idle_pct > 80
                    else "Låg prisspread (ej lönsamt att cykla)"
                )
            )

        # Store for insight mail
        self._daily_goals = results
        return results

    async def _self_heal_goodwe_entries(self) -> None:
        """PLAT-972: Self-healing — check GoodWe config entries and reload if needed."""
        # Skip if paused after repeated failures
        if time.monotonic() < self._ems_pause_until:
            return

        for adapter in self.inverter_adapters:
            if not isinstance(adapter, GoodWeAdapter):
                continue
            # Check if EMS mode entity is unavailable (integration not loaded)
            ems_entity = f"select.goodwe_{adapter.prefix}_ems_mode"
            ems_state = self.hass.states.get(ems_entity)
            if ems_state is not None and ems_state.state not in (
                "unavailable",
                "unknown",
            ):
                # Integration is fine, reset failure counter
                self._ems_consecutive_failures = 0
                continue

            # Entity is missing or unavailable — try to reload
            self._ems_consecutive_failures += 1
            _LOGGER.warning(
                "CARMA self-heal: GoodWe %s entity %s unavailable (failure %d/%d)",
                adapter.prefix,
                ems_entity,
                self._ems_consecutive_failures,
                SELF_HEALING_MAX_FAILURES,
            )

            if self._ems_consecutive_failures >= SELF_HEALING_MAX_FAILURES:
                _LOGGER.warning(
                    "CARMA self-heal: %d consecutive failures — pausing EMS commands for %ds",
                    self._ems_consecutive_failures,
                    SELF_HEALING_PAUSE_SECONDS,
                )
                self._ems_pause_until = time.monotonic() + SELF_HEALING_PAUSE_SECONDS
                self._ems_consecutive_failures = 0
                return

            # Try config entry reload (best-effort)
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    "reload_config_entry",
                    {"entity_id": ems_entity},
                )
                _LOGGER.info("CARMA self-heal: triggered reload for GoodWe %s", adapter.prefix)
            except HomeAssistantError:
                _LOGGER.debug(
                    "CARMA self-heal: reload failed for %s",
                    adapter.prefix,
                    exc_info=True,
                )

    def _self_heal_ev_tamper(self) -> None:
        """PLAT-972: Detect if Easee is_enabled changed externally and log it."""
        if not self.ev_adapter or not isinstance(self.ev_adapter, EaseeAdapter):
            return

        current_enabled = self.ev_adapter.is_enabled

        if self._ev_last_known_enabled is None:
            # First check — just record
            self._ev_last_known_enabled = current_enabled
            return

        if current_enabled != self._ev_last_known_enabled:
            # External change detected
            _LOGGER.warning(
                "CARMA self-heal: EV charger is_enabled changed externally "
                "(%s → %s). CARMA will restore its own state on next cycle.",
                self._ev_last_known_enabled,
                current_enabled,
            )
            # If CARMA thinks EV should be off but it got enabled externally,
            # our _ev_enabled flag will cause the next _execute_ev to correct it.
            # If CARMA thinks EV should be on but it got disabled, same thing.
            self._ev_last_known_enabled = current_enabled

    async def _safe_service_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with error handling and retry. Returns True on success.

        In dry-run mode: logs the call but does NOT execute it.
        """
        entity_id = data.get("entity_id", "?")

        if not self.executor_enabled:
            _LOGGER.info(
                "DRY-RUN: would call %s.%s → %s %s",
                domain,
                service,
                entity_id,
                {k: v for k, v in data.items() if k != "entity_id"},
            )
            return True  # Pretend success so decision logging works

        for attempt in range(2):  # max 1 retry
            try:
                await self.hass.services.async_call(domain, service, data)
                return True
            except ServiceNotFound:
                _LOGGER.error(
                    "Service not found: %s.%s → %s (attempt %d/2)",
                    domain,
                    service,
                    entity_id,
                    attempt + 1,
                    exc_info=True,
                )
                break  # No point retrying a missing service
            except HomeAssistantError as err:
                _LOGGER.error(
                    "HA error on %s.%s → %s: %s (attempt %d/2)",
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                    exc_info=True,
                )
                if attempt == 0:
                    await asyncio.sleep(5)
                    continue
            except Exception as err:
                _LOGGER.exception(
                    "Unexpected error on %s.%s → %s: %s (attempt %d/2)",
                    domain,
                    service,
                    entity_id,
                    err,
                    attempt + 1,
                )
                if attempt == 0:
                    await asyncio.sleep(5)
                    continue
        self._daily_safety_blocks += 1
        return False

    def _check_write_verify(self, ems_entity: str, expected_mode: str) -> None:
        """Queue a write-verify check for the NEXT update cycle.

        PLAT-945 (K3): GoodWe Modbus takes 2-10s to propagate writes.
        Reading entity state immediately after a service call reads stale
        state and produces false lockup alerts. Instead, we defer the
        verification to the next coordinator cycle (30s later).
        """
        self._pending_write_verifies.append((ems_entity, expected_mode))

    def _run_deferred_write_verifies(self) -> None:
        """Execute all pending write-verify checks (called at cycle start)."""
        pending = self._pending_write_verifies
        self._pending_write_verifies = []
        for ems_entity, expected_mode in pending:
            actual = self._read_str(ems_entity)
            if actual != expected_mode:
                _LOGGER.error(
                    "Write-verify FAILED (deferred): %s expected=%s actual=%s",
                    ems_entity,
                    expected_mode,
                    actual,
                )
                self._daily_safety_blocks += 1

    def _is_in_taper(self, state: CarmaboxState) -> bool:
        """IT-1939: Detect BMS taper mode.

        Returns True if:
        - Current command is charge_pv OR charge_pv_taper (persist across cycles)
        - Exporting > 200W (BMS not accepting full charge)
        - Average SoC < 100% (batteries not yet full)
        - PV > 500W (still producing)

        BMS taper occurs when SoC > 95% — batteries slow charge acceptance,
        causing 2-3kW export at low prices. Solution: keep charge_pv active
        but route surplus to miner/VP/EV instead of exporting.

        BUG FIX: Must also check CHARGE_PV_TAPER, not only CHARGE_PV.
        Without this, taper mode exits on cycle 2 because _last_command is
        already CHARGE_PV_TAPER after first detection.
        """
        return (
            self._last_command in (BatteryCommand.CHARGE_PV, BatteryCommand.CHARGE_PV_TAPER)
            and state.is_exporting
            and abs(state.grid_power_w) > 200
            and state.total_battery_soc < 100
            and state.pv_power_w > 500
        )

    def _is_cold_locked(self, state: CarmaboxState) -> bool:
        """IT-1948: Detect BMS cold lock (cell temp < 10°C blocks ALL charging).

        Returns True if:
        - ANY battery min cell temp < 10°C
        - Charge command was requested (charge_pv or grid_charge)
        - Battery power is near zero (~0W, no actual charging happening)
        - PV > 500W OR importing (trying to charge but BMS blocks)

        BMS lithium plating protection blocks ALL charging when cells are cold.
        This is DIFFERENT from taper (which is SoC-based export at high SoC).
        Cold lock = zero charging despite surplus. Taper = some charging + export.

        Solution: Route surplus to loads immediately (MAX surplus chain).
        """
        # Check if any battery is below cold threshold
        temps = []
        if state.battery_min_cell_temp_1 is not None:
            temps.append(state.battery_min_cell_temp_1)
        if state.battery_min_cell_temp_2 is not None:
            temps.append(state.battery_min_cell_temp_2)

        if not temps:
            return False  # No temp data = can't detect cold lock

        min_temp = min(temps)

        # Cold lock criteria
        return (
            min_temp < 10.0
            and self._last_command in (BatteryCommand.CHARGE_PV, BatteryCommand.CHARGE_PV_TAPER)
            and abs(state.battery_power_1) < 100  # Battery 1 not charging
            and (
                state.battery_soc_2 < 0 or abs(state.battery_power_2) < 100
            )  # Battery 2 not charging (if exists)
            and (state.pv_power_w > 500 or not state.is_exporting)
        )

    async def _cmd_charge_pv(self, state: CarmaboxState) -> None:
        """Command battery to charge from PV."""
        await self._execution_engine.cmd_charge_pv(state)

    async def _cmd_standby(self, state: CarmaboxState, force: bool = False) -> None:
        """Command battery to standby mode."""
        await self._execution_engine.cmd_standby(state, force)

    async def _cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Command battery discharge at given watts."""
        await self._execution_engine.cmd_discharge(state, watts)
