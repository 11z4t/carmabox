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
from enum import Enum
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .adapters import EVAdapter, InverterAdapter
from .adapters.easee import EaseeAdapter
from .adapters.goodwe import GoodWeAdapter
from .adapters.nordpool import NordpoolAdapter
from .adapters.solcast import SolcastAdapter
from .const import (
    COLD_LOCK_CELL_TEMP_C,
    COLD_LOCK_POWER_THRESHOLD_W,
    COLD_MIN_SOC_PCT,
    COLD_TEMP_THRESHOLD_C,
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_DAILY_BATTERY_NEED_KWH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_EV_MAX_AMPS,
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
    DEFAULT_PRICE_CHEAP_ORE,
    DEFAULT_PRICE_EXPENSIVE_ORE,
    DEFAULT_TARGET_DAY_KW,
    DEFAULT_TARGET_NIGHT_KW,
    DEFAULT_TARGET_WEIGHTED_KW,
    DEFAULT_VOLTAGE,
    DEFAULT_WATCHDOG_DISCHARGE_MIN_W,
    DEFAULT_WATCHDOG_EV_IMPORT_W,
    DEFAULT_WATCHDOG_EXPORT_W,
    DEFAULT_WATCHDOG_MIN_SOC_PCT,
    DISCHARGE_LIMIT_HIGH_SOC_W,
    DISCHARGE_LIMIT_LOW_SOC_W,
    DISCHARGE_LIMIT_MID_SOC_W,
    DISCHARGE_LIMIT_VERY_LOW_SOC_W,
    DISCHARGE_NIGHT_FACTOR,
    EV_RAMP_INTERVAL_S,
    PEAK_MIN_MEANINGFUL_KW,
    PEAK_RANK_COUNT,
    PEAK_UPDATE_INTERVAL_S,
    PEAK_WARNING_MARGIN_KW,
    PLAN_INTERVAL_SECONDS,
    RESERVE_OFFSET_NEUTRAL_PCT,
    RESERVE_OFFSET_STRONG_PCT,
    RESERVE_OFFSET_WEAK_PCT,
    RESERVE_PV_STRONG_KWH,
    RESERVE_PV_WEAK_KWH,
    SCAN_INTERVAL_SECONDS,
    SPIKE_COOLDOWN_S,
    SPIKE_DEFAULT_PS_LIMIT_W,
    SPIKE_DETECTION_THRESHOLD_W,
    SPIKE_HISTORY_WINDOW_S,
    SPIKE_PS_LIMIT_W,
    SPIKE_SAFETY_TIMEOUT_S,
    TAPER_EV_SURPLUS_W,
    TAPER_EXIT_EXPORT_W,
    TAPER_EXIT_PV_KW,
    TAPER_EXPORT_THRESHOLD_W,
    TAPER_VP_SURPLUS_W,
)
from .notifications import CarmaNotifier
from .optimizer.consumption import ConsumptionProfile, calculate_house_consumption
from .optimizer.ev_strategy import calculate_ev_schedule
from .optimizer.grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .optimizer.hourly_ledger import EnergyLedger
from .optimizer.models import CarmaboxState, Decision, HourActual, HourPlan, ShadowComparison
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


class BatteryCommand(Enum):
    """Battery command state — replaces fragile string comparison."""

    IDLE = "idle"
    CHARGE_PV = "charge_pv"
    STANDBY = "standby"
    DISCHARGE = "discharge"


class CarmaboxCoordinator(DataUpdateCoordinator[CarmaboxState]):
    """CARMA Box coordinator — the brain."""

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
        # Start at threshold-1 so first update generates a plan immediately
        self._plan_counter = (PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS) - 1
        self._last_command = BatteryCommand.IDLE
        self._last_discharge_w = 0

        # EV executor state (PLAT-949)
        self._ev_enabled: bool = False
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
        if self._cfg.get("ev_enabled", False):
            ev_prefix = self._cfg.get("ev_prefix", "easee_home_12840")
            ev_device_id = self._cfg.get("ev_device_id", "")
            ev_charger_id = self._cfg.get("ev_charger_id", "")
            if ev_prefix:
                self.ev_adapter = EaseeAdapter(
                    hass, ev_device_id, str(ev_prefix), charger_id=ev_charger_id
                )

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
        self._plan_last_correction_time: float = 0.0

        # PLAT-940: Shadow mode — CARMA vs v6 comparison
        self.shadow: ShadowComparison = ShadowComparison()
        self.shadow_log: list[ShadowComparison] = []
        self._shadow_savings_kr: float = 0.0

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
        config_executor = bool(self._cfg.get("executor_enabled", False))
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
        self._miner_on: bool = False

        # IT-1939: BMS taper detection — tracks when batteries are in charge_pv
        # but barely accepting power due to BMS taper at high SoC
        self._taper_active: bool = False

        # IT-1948: BMS cold lock detection — tracks when BMS blocks ALL charging
        # because min cell temperature is below lithium plating protection threshold
        self._cold_lock_active: bool = False

        # ── IT-2067: Peak Tracking (rolling top-3 monthly peaks) ───
        self._peak_ranks: list[float] = [0.0] * PEAK_RANK_COUNT
        self._peak_month: int = init_now.month
        self._peak_last_update: float = 0.0  # monotonic

        # ── IT-2067: Appliance Spike Detection & Response ──────────
        self._spike_active: bool = False
        self._spike_activated_at: float = 0.0  # monotonic
        self._spike_cooldown_started: float = 0.0  # monotonic
        self._grid_power_history: deque[tuple[float, float]] = deque(
            maxlen=120
        )  # (monotonic_time, grid_w) — ~60s at 30s intervals + safety margin

        # ── IT-2067: Reserve Target (Solcast-based dynamic min_soc) ─
        self._reserve_target_pct: float = self.min_soc
        self._reserve_last_calc: float = 0.0  # monotonic

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
        import time as _time

        now = _time.monotonic()
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

                    # Update executor based on new license
                    config_exec = bool(self._cfg.get("executor_enabled", False))
                    self.executor_enabled = config_exec and self._has_feature("executor")

                    _LOGGER.info(
                        "License validated: tier=%s, features=%d, valid_until=%s",
                        self._license_tier,
                        len(self._license_features),
                        self._license_valid_until,
                    )
                else:
                    _LOGGER.warning("License check failed: HTTP %d", resp.status)
        except Exception:
            _LOGGER.debug("License check failed — using cached license", exc_info=True)

    @property
    def cable_locked_entity(self) -> str:
        """Entity ID for EV cable locked sensor (for state change listener)."""
        ev_prefix = self._cfg.get("ev_prefix", "")
        if ev_prefix:
            return f"binary_sensor.{ev_prefix}_cable_locked"
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
                "CARMA: Cable connected + PV %.1f kW → starting EV at 6A",
                pv_kw,
            )
            await self._cmd_ev_start(6)
        else:
            _LOGGER.info("CARMA: Cable connected, no PV surplus — waiting for next cycle")

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
        """Read float state from HA entity with validation."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            val = float(state.state)
            # Validate reasonable ranges
            if abs(val) > 100000:  # >100kW = nonsense
                _LOGGER.warning("Unreasonable value %s from %s", val, entity_id)
                return default
            return val
        except (ValueError, TypeError):
            return default

    def _read_float_or_none(self, entity_id: str) -> float | None:
        """Read float state, returning None if entity is missing/unknown/unavailable.

        Used for battery power readings where None signals unreliable data
        (e.g. at HA start before first sensor reading). PLAT-946.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            val = float(state.state)
            if abs(val) > 100000:
                return None
            return val
        except (ValueError, TypeError):
            return None

    def _read_str(self, entity_id: str, default: str = "") -> str:
        """Read string state from HA entity."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        return state.state

    async def _async_restore_savings(self) -> None:
        """Restore savings state from persistent storage."""
        try:
            data = await self._savings_store.async_load()
            if data and isinstance(data, dict):
                restored = state_from_dict(data)
                now = datetime.now()
                restored = reset_if_new_month(restored, now)
                self.savings = restored
                _LOGGER.info(
                    "Restored savings: month=%d, trend=%d days, total=%.1f kr",
                    restored.month,
                    len(restored.daily_savings),
                    restored.discharge_savings_kr + restored.grid_charge_savings_kr,
                )
        except Exception:
            _LOGGER.warning("Failed to restore savings, starting fresh", exc_info=True)

    async def _async_save_savings(self) -> None:
        """Persist savings state (rate-limited to every 5 minutes)."""
        import time

        now = time.monotonic()
        if now - self._savings_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._savings_last_save = now
        try:
            await self._savings_store.async_save(state_to_dict(self.savings))
        except Exception:
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
        except Exception:
            _LOGGER.warning("Failed to restore consumption profile, starting fresh", exc_info=True)

    async def _async_save_consumption(self) -> None:
        """Persist consumption profile (rate-limited to every 5 minutes)."""
        import time

        now = time.monotonic()
        if now - self._consumption_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._consumption_last_save = now
        try:
            await self._consumption_store.async_save(self.consumption_profile.to_dict())
        except Exception:
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
        except Exception:
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
                self._ev_current_amps = int(data.get("ev_current_amps", 6))
                # Restore miner state
                self._miner_on = bool(data.get("miner_on", False))
                # IT-2067: Restore peak tracking data
                peak_data = data.get("peak_ranks", [])
                if isinstance(peak_data, list) and len(peak_data) == PEAK_RANK_COUNT:
                    self._peak_ranks = [float(p) for p in peak_data]
                self._peak_month = int(data.get("peak_month", datetime.now().month))
                _LOGGER.info(
                    "Restored runtime: plan=%d hours, cmd=%s, ev=%s@%dA, miner=%s, "
                    "peaks=%s (month=%d)",
                    len(self.plan),
                    cmd_str,
                    self._ev_enabled,
                    self._ev_current_amps,
                    self._miner_on,
                    [f"{p:.2f}" for p in self._peak_ranks],
                    self._peak_month,
                )
        except Exception:
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
                "miner_on": self._miner_on,
                # IT-2067: Peak tracking persistence
                "peak_ranks": self._peak_ranks,
                "peak_month": self._peak_month,
            }
            await self._runtime_store.async_save(data)
        except Exception:
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
        except Exception:
            _LOGGER.warning("Failed to restore ledger, starting fresh", exc_info=True)

    async def _async_save_ledger(self) -> None:
        """Persist ledger state (rate-limited to every 5 minutes, CARMA-P0-FIXES Task 4)."""
        import time

        now = time.monotonic()
        if now - self._ledger_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._ledger_last_save = now
        try:
            await self._ledger_store.async_save(self.ledger.to_dict())
        except Exception:
            _LOGGER.debug("Failed to save ledger", exc_info=True)

    async def _async_save_predictor(self) -> None:
        """Persist predictor state (rate-limited to every 5 minutes)."""
        import time

        now = time.monotonic()
        if now - self._predictor_last_save < SAVINGS_SAVE_INTERVAL:
            return
        self._predictor_last_save = now
        try:
            await self._predictor_store.async_save(self.predictor.to_dict())
        except Exception:
            _LOGGER.debug("Failed to save predictor", exc_info=True)

    async def _async_fetch_benchmarking(self) -> None:
        """PLAT-962: Fetch benchmarking data from hub (rate-limited to every hour)."""
        import time

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
        except Exception:
            _LOGGER.debug("Benchmarking fetch failed", exc_info=True)

    async def _async_update_data(self) -> CarmaboxState:
        """Fetch data, run optimizer, execute plan."""
        try:
            now = datetime.now()

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
                _LOGGER.info("CARMA: EV startup — setting 6A fallback + disabling charger")
                await self._cmd_ev_stop()

            self.safety.update_heartbeat()

            # License check (every 6h — Hub handshake)
            await self._check_license()

            # PLAT-972: Self-healing — check GoodWe config entries
            await self._self_heal_goodwe_entries()
            # PLAT-972: Self-healing — detect external EV changes
            self._self_heal_ev_tamper()

            # K3 (PLAT-945): Deferred write-verify — check pending verifications
            # from the previous cycle (Modbus has had 30s to propagate).
            self._run_deferred_write_verifies()

            state = self._collect_state()

            self._plan_counter += 1
            if self._plan_counter >= PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS:
                self._plan_counter = 0
                self._generate_plan(state)
                self._check_repair_issues()

            # Plan self-correction — adjust if actual deviates >50% from plan for 3+ cycles
            self._check_plan_correction(state)

            await self._execute(state)
            await self._watchdog(state)
            self._track_shadow(state)
            self._track_savings(state)
            self._track_appliances()
            await self._async_save_savings()
            await self._async_save_consumption()
            await self._async_save_predictor()
            await self._async_fetch_benchmarking()
            return state

        except Exception as err:
            _LOGGER.error("CARMA Box update failed: %s", err, exc_info=True)
            raise UpdateFailed(f"Update failed: {err}") from err

    def _collect_state(self) -> CarmaboxState:
        """Collect current state from all HA entities.

        Uses inverter/EV adapters when configured, falls back to raw entity reads.
        """
        opts = self._cfg
        adapters = self.inverter_adapters
        a1 = adapters[0] if len(adapters) >= 1 else None
        a2 = adapters[1] if len(adapters) >= 2 else None

        # Battery 1 — adapter or legacy config
        battery_soc_1 = a1.soc if a1 else self._read_float(opts.get("battery_soc_1", ""))
        battery_power_1 = a1.power_w if a1 else self._read_float(opts.get("battery_power_1", ""))
        battery_ems_1 = a1.ems_mode if a1 else self._read_str(opts.get("battery_ems_1", ""))

        # Battery 2 — adapter or legacy config
        battery_soc_2 = a2.soc if a2 else self._read_float(opts.get("battery_soc_2", ""), -1)
        battery_power_2 = a2.power_w if a2 else self._read_float(opts.get("battery_power_2", ""))
        battery_ems_2 = a2.ems_mode if a2 else self._read_str(opts.get("battery_ems_2", ""))

        # PLAT-946: Check if battery power sensors are actually available
        # At HA start, sensors report unknown/unavailable → _read_float returns 0.0
        # which masks potential crosscharge. Track validity separately.
        bp1_entity = (
            f"sensor.goodwe_battery_power_{a1.prefix}" if a1 else opts.get("battery_power_1", "")
        )
        bp2_entity = (
            f"sensor.goodwe_battery_power_{a2.prefix}" if a2 else opts.get("battery_power_2", "")
        )
        bp1_valid = self._read_float_or_none(bp1_entity) is not None
        bp2_valid = self._read_float_or_none(bp2_entity) is not None if bp2_entity else True

        # EV — adapter or legacy config
        ev = self.ev_adapter
        ev_power_w = ev.power_w if ev else self._read_float(opts.get("ev_power_entity", ""))
        ev_current_a = ev.current_a if ev else self._read_float(opts.get("ev_current_entity", ""))
        ev_status = ev.status if ev else self._read_str(opts.get("ev_status_entity", ""))

        return CarmaboxState(
            grid_power_w=self._read_float(opts.get("grid_entity", "sensor.house_grid_power")),
            battery_soc_1=battery_soc_1,
            battery_power_1=battery_power_1,
            battery_power_1_valid=bp1_valid,
            battery_ems_1=battery_ems_1,
            battery_cap_1_kwh=float(opts.get("battery_1_kwh", 15.0)),
            battery_soc_2=battery_soc_2,
            battery_power_2=battery_power_2,
            battery_power_2_valid=bp2_valid,
            battery_ems_2=battery_ems_2,
            battery_cap_2_kwh=float(opts.get("battery_2_kwh", 5.0)),
            pv_power_w=self._read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            ev_soc=self._read_float(opts.get("ev_soc_entity", ""), -1),
            ev_power_w=ev_power_w,
            ev_current_a=ev_current_a,
            ev_status=ev_status,
            battery_temp_c=self._read_battery_temp(),
            # IT-1948: Per-battery min cell temperature for cold lock detection
            battery_cell_temp_1=a1.temperature_c if a1 else None,
            battery_cell_temp_2=a2.temperature_c if a2 else None,
            # Weather (Tempest — prefer local MQTT, fallback to cloud)
            outdoor_temp_c=self._read_float(
                opts.get("outdoor_temp_entity", "sensor.sanduddsvagen_60_temperature")
            ),
            solar_radiation_wm2=self._read_float(
                opts.get("solar_radiation_entity", "sensor.tempest_solar_radiation")
            ),
            illuminance_lx=self._read_float(
                opts.get("illuminance_entity", "sensor.tempest_illuminance")
            ),
            barometric_pressure_hpa=self._read_float(
                opts.get("pressure_entity", "sensor.sanduddsvagen_60_pressure_barometric")
            ),
            rain_mm=self._read_float(
                opts.get("rain_entity", "sensor.sanduddsvagen_60_rain_last_hour")
            ),
            wind_speed_kmh=self._read_float(
                opts.get("wind_speed_entity", "sensor.sanduddsvagen_60_wind_speed")
            ),
            current_price=self._read_float(opts.get("price_entity", "")),
            target_weighted_kw=self.target_kw,
            plan=self.plan,
        )

    def _generate_plan(self, state: CarmaboxState) -> None:
        """Generate energy plan from Nordpool + Solcast + consumption."""
        try:
            now = datetime.now()
            start_hour = now.hour

            # Collect prices — try primary, fallback to secondary
            price_entity = self._get_entity("price_entity", "")
            price_entity_fallback = self._get_entity("price_entity_fallback", "")
            fallback_price = float(self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
            price_adapter = NordpoolAdapter(self.hass, price_entity, fallback_price)
            today_prices = price_adapter.today_prices

            # If primary returns all-fallback, try secondary
            if price_entity_fallback and all(p == fallback_price for p in today_prices):
                _LOGGER.info("Primary price source offline, trying fallback")
                price_adapter = NordpoolAdapter(self.hass, price_entity_fallback, fallback_price)
                today_prices = price_adapter.today_prices

            tomorrow_prices = price_adapter.tomorrow_prices
            prices = today_prices[start_hour:] + (tomorrow_prices or today_prices)

            # Collect PV forecast — today remaining + tomorrow hourly
            solcast = SolcastAdapter(self.hass)
            pv_today = solcast.today_hourly_kw
            pv_tomorrow = solcast.tomorrow_hourly_kw
            pv_forecast = pv_today[start_hour:] + pv_tomorrow

            # PLAT-965: Use predictor if trained, else fallback to profile
            base = self.consumption_profile.get_profile_for_date(now)
            if self.predictor.is_trained:
                consumption = self.predictor.predict_24h(
                    start_hour=start_hour,
                    weekday=now.weekday(),
                    month=now.month,
                    fallback_profile=base,
                )
                # Pad to match prices length (predict_24h returns exactly 24)
                consumption = consumption + base
            else:
                consumption = base[start_hour:] + base

            # EV demand — dynamic schedule based on prices + SoC
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

            if ev_enabled and state.ev_soc >= 0:
                ev_demand = calculate_ev_schedule(
                    start_hour=start_hour,
                    num_hours=len(prices),
                    ev_soc_pct=state.ev_soc,
                    ev_capacity_kwh=ev_capacity,
                    hourly_prices=prices,
                    hourly_loads=consumption[: len(prices)],
                    target_weighted_kw=self.target_kw,
                    morning_target_soc=ev_morning_target,
                    full_charge_interval_days=ev_full_days,
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
            target = calculate_target(
                battery_kwh_available=battery_kwh - (self.min_soc / 100 * total_bat_kwh),
                hourly_loads=consumption[: len(prices)],
                hourly_weights=[
                    ellevio_weight((start_hour + i) % 24, night_weight=night_weight)
                    for i in range(len(prices))
                ],
                reserve_kwh=reserve,
            )
            self.target_kw = target

            # Trim to same length
            n = min(len(prices), len(pv_forecast), len(consumption))
            prices = prices[:n]
            pv_forecast = pv_forecast[:n]
            consumption = consumption[:n]
            ev_demand = ev_demand[:n]

            # Grid charge config
            grid_charge_threshold = float(
                opts.get("grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD)
            )
            grid_charge_max_soc = float(
                opts.get("grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC)
            )

            # Generate plan
            battery_efficiency = float(opts.get("battery_efficiency", DEFAULT_BATTERY_EFFICIENCY))
            max_discharge_kw = float(opts.get("max_discharge_kw", DEFAULT_MAX_DISCHARGE_KW))
            max_grid_charge_kw = float(opts.get("max_grid_charge_kw", DEFAULT_MAX_GRID_CHARGE_KW))

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
            )

            self._daily_plans += 1
            _LOGGER.info(
                "CARMA plan: %d hours, target=%.1f kW, %d charge, %d discharge, %d grid_charge",
                len(self.plan),
                target,
                sum(1 for h in self.plan if h.action == "c"),
                sum(1 for h in self.plan if h.action == "d"),
                sum(1 for h in self.plan if h.action == "g"),
            )
            # CARMA-P0-FIXES Task 4: Mark runtime as dirty — will be saved in next async_update_data
            self._runtime_dirty = True

        except Exception:
            _LOGGER.exception("Plan generation failed — keeping old plan")

    async def _execute(self, state: CarmaboxState) -> None:
        """Execute current action based on state.

        ALL commands go through SafetyGuard. No exceptions.

        Core rules (in priority order):
        1. Never discharge during export
        2. SoC 100% → standby
        3. Load > target → discharge to fill gap
        4. Load < target → idle (grid handles it)
        """
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

        # ── IT-2067: Update subsystems ────────────────────────
        hour = datetime.now().hour
        is_night = hour >= 22 or hour < 6
        net_w = max(0, state.grid_power_w)
        grid_kw = net_w / 1000

        # Peak tracking: update rolling top-3
        self._track_peaks(grid_kw)

        # Appliance spike: check recovery first, then detect new spikes
        await self._handle_spike_recovery(state.grid_power_w)
        if not self._spike_active and self._detect_appliance_spike(state.grid_power_w):
            await self._handle_appliance_spike(state)

        # Reserve target: dynamic min_soc based on forecast + temperature
        effective_min_soc = self._effective_min_soc()

        # ── Compute metrics for decision ──────────────────────
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_weight)
        # H5: grid_power_w IS what Ellevio sees (net import after PV + battery)
        # Don't adjust for EV/PV — they're already in the meter reading
        weighted_net = net_w * weight
        # IT-2067: Use peak-risk-adjusted target instead of flat target_kw
        active_target_kw = self._adjusted_target_kw(is_night, grid_kw)
        target_w = active_target_kw * 1000
        pv_kw = state.pv_power_w / 1000

        # IT-1939: Clear taper if conditions no longer met
        if self._taper_active and (
            not state.is_exporting or state.all_batteries_full or pv_kw < TAPER_EXIT_PV_KW
        ):
            self._taper_active = False
            _LOGGER.info(
                "CARMA: BMS taper cleared (exporting=%s, full=%s, pv=%.1f kW)",
                state.is_exporting,
                state.all_batteries_full,
                pv_kw,
            )

        # IT-1948: Detect/clear BMS cold lock based on min cell temperature
        cell_temp_1 = state.battery_cell_temp_1
        cell_temp_2 = state.battery_cell_temp_2
        cold_lock_threshold = float(self._cfg.get("cold_lock_temp_c", COLD_LOCK_CELL_TEMP_C))
        # Cold lock = any battery has min cell temp below threshold
        any_cold = (cell_temp_1 is not None and cell_temp_1 < cold_lock_threshold) or (
            cell_temp_2 is not None and cell_temp_2 < cold_lock_threshold
        )
        if self._cold_lock_active and not any_cold:
            self._cold_lock_active = False
            _LOGGER.info(
                "CARMA: BMS cold lock cleared — cell temps: kontor=%s°C, förråd=%s°C",
                f"{cell_temp_1:.1f}" if cell_temp_1 is not None else "?",
                f"{cell_temp_2:.1f}" if cell_temp_2 is not None else "?",
            )

        # ── Build reasoning chain ─────────────────────────────
        reasoning: list[str] = []
        chain: list[dict[str, str]] = []
        period = "natt" if is_night else "dag"
        allowed_import = active_target_kw / weight if weight > 0 else active_target_kw

        # Step 1: Tidpunkt + Ellevio-vikt → tillåten import
        # IT-2067: Include peak risk in reasoning
        peak_risk = self._peak_risk_status(grid_kw)
        step1 = (
            f"Kl {hour:02d}, {period}, Ellevio-vikt {weight:.1f}, "
            f"peak-risk={peak_risk} "
            f"→ tillåten import {allowed_import:.1f} kW "
            f"(mål {active_target_kw:.1f} kW)"
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

        # ── IT-1948: RULE 0.4: BMS cold lock → skip battery, max surplus ──
        # When min cell temp < threshold, BMS blocks ALL charging (lithium plating
        # protection). Charging commands are ignored — battery power stays at ~0W.
        # Different from taper: taper = battery accepts SOME power, cold lock = NONE.
        if any_cold and pv_kw > 0.5 and not state.all_batteries_full and state.is_exporting:
            # Confirm cold lock: battery should be accepting ~0W despite charge conditions
            total_bat_power = abs(state.battery_power_1) + abs(
                state.battery_power_2 if state.has_battery_2 else 0
            )
            if total_bat_power < COLD_LOCK_POWER_THRESHOLD_W or self._cold_lock_active:
                self._cold_lock_active = True
                cold_temps = []
                if cell_temp_1 is not None:
                    cold_temps.append(f"kontor {cell_temp_1:.1f}°C")
                if cell_temp_2 is not None:
                    cold_temps.append(f"förråd {cell_temp_2:.1f}°C")
                cold_str = ", ".join(cold_temps)

                reasoning.append(
                    f"BMS kall-blockering — min cell {cold_str}, laddning pausad, surplus chain MAX"
                )
                _LOGGER.info(
                    "CARMA: BMS cold lock — cell temps: %s, bat_power=%.0fW, "
                    "routing all PV to surplus chain",
                    cold_str,
                    total_bat_power,
                )
                self._record_decision(
                    state,
                    "bms_cold_lock",
                    f"BMS kall-blockering — min cell {cold_str}, "
                    f"laddning pausad, PV {pv_kw:.1f} kW → surplus chain",
                    reasoning=reasoning,
                )
                # Set standby — charging is pointless during cold lock
                await self._cmd_standby(state)
                # Maximize surplus chain: absorb all PV export locally
                export_w = abs(state.grid_power_w) if state.is_exporting else 0
                await self._execute_taper_surplus(state, export_w)
                return

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

                # ── IT-1939: BMS taper detection ──────────────────
                # When charge_pv is set but batteries barely accept power
                # (BMS taper at SoC > 95%), we still export to grid.
                # INVARIANT: NEVER export when SoC < 100%.
                # Detect taper: exporting > threshold while charging.
                export_w = abs(state.grid_power_w)
                taper_now = export_w > TAPER_EXPORT_THRESHOLD_W and state.total_battery_soc < 100

                if taper_now:
                    self._taper_active = True
                    reasoning.append(
                        f"BMS taper: export {export_w:.0f}W trots charge_pv, "
                        f"SoC {state.total_battery_soc:.0f}% < 100% "
                        f"→ aktiverar surplus chain aggressivt"
                    )
                    _LOGGER.info(
                        "CARMA: BMS taper detected — export %.0fW, SoC %.0f%%, "
                        "activating surplus chain",
                        export_w,
                        state.total_battery_soc,
                    )
                    self._record_decision(
                        state,
                        "charge_pv_taper",
                        f"Solladdar (taper) — export {export_w:.0f}W, "
                        f"PV {pv_kw:.1f} kW, batteri {state.total_battery_soc:.0f}%, "
                        f"surplus chain aktiv",
                        reasoning=reasoning,
                    )
                    # Aggressive surplus chain: absorb ALL export locally
                    await self._execute_taper_surplus(state, export_w)
                else:
                    # Check taper exit conditions
                    if self._taper_active and (
                        state.total_battery_soc >= 100
                        or export_w < TAPER_EXIT_EXPORT_W
                        or pv_kw < TAPER_EXIT_PV_KW
                    ):
                        self._taper_active = False
                        _LOGGER.info(
                            "CARMA: BMS taper ended — SoC %.0f%%, export %.0fW, PV %.1f kW",
                            state.total_battery_soc,
                            export_w,
                            pv_kw,
                        )
                    self._record_decision(
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
                    self._record_decision(
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
                    self._record_decision(
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
                self._record_decision(
                    state,
                    "standby",
                    f"Standby — batterier fulla ({state.battery_soc_1:.0f}%), exporterar",
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
            return

        # ── RULE 1.5: Grid charge at very cheap price ────────
        grid_charge_threshold = float(
            self._cfg.get("grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD)
        )
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
                await self._cmd_grid_charge(
                    state
                )  # CARMA-P0-FIXES Task 2: Use dedicated grid charge
                self._record_decision(
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
            _proactive_min_grid_w = 50.0
            _proactive_soc_threshold = max(effective_min_soc + 10, 40.0)
        elif not is_night:
            # Daytime but cloudy/rainy — moderate
            _proactive_min_grid_w = 200.0
            _proactive_soc_threshold = 80.0
        else:
            _proactive_min_grid_w = 300.0
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
            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                effective_min_soc,
                state.grid_power_w,
                temp_c,
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
                self._record_decision(
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

        # ── RULE 2: Load > target → discharge (even at 100%) ──
        # Hysteresis: if already discharging, keep going until grid drops
        # 10% BELOW target (prevents oscillation at boundary).
        hysteresis = 0.9 if self._last_command == BatteryCommand.DISCHARGE else 1.0
        if weighted_net > target_w * hysteresis and weight > 0:
            discharge_w = int((weighted_net - target_w) / weight)
            reasoning.append(
                f"Grid {weighted_net / 1000:.1f} kW viktat > target {active_target_kw:.1f} kW "
                f"→ batteri kompenserar {discharge_w}W"
            )
            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                effective_min_soc,
                state.grid_power_w,
                temp_c,
            )
            if result.ok:
                peak_kr = float(self._cfg.get("peak_cost_per_kw", DEFAULT_PEAK_COST_PER_KW))
                ellevio_saving = (weighted_net / 1000 - active_target_kw) * peak_kr
                step5 = (
                    f"Urladdning {discharge_w}W → Ellevio ser {active_target_kw:.1f} kW "
                    f"istf {weighted_net / 1000:.1f} kW, sparar ~{ellevio_saving:.0f} kr/mån"
                )
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_discharge(state, discharge_w)
                self._record_decision(
                    state,
                    "discharge",
                    f"Urladdning {discharge_w}W — grid {weighted_net / 1000:.1f} kW viktat "
                    f"> target {active_target_kw:.1f} kW "
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
                self._record_decision(
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
            f"Ellevio ser {weighted_net / 1000:.1f} kW (mål {active_target_kw:.1f} kW)"
        )
        reasoning.append(
            f"Grid {weighted_net / 1000:.2f} kW viktat < target {active_target_kw:.1f} kW "
            f"→ {headroom_val:.1f} kW headroom, batteriet vilar"
        )
        reasoning.append(step5)
        chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
        # R5: Actively set standby so batteries don't stay in previous mode
        await self._cmd_standby(state)
        self._record_decision(
            state,
            "idle",
            f"Vila — grid {weighted_net / 1000:.2f} kW viktat "
            f"< target {active_target_kw:.1f} kW "
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
        except Exception:
            _LOGGER.warning("CARMA: VP control failed for %s", entity_id, exc_info=True)

    async def _execute_taper_surplus(self, state: CarmaboxState, export_w: float) -> None:
        """IT-1939: Aggressively activate surplus chain during BMS taper.

        When batteries are in charge_pv but BMS taper prevents full absorption,
        we must consume the surplus locally. INVARIANT: never export at SoC < 100%.

        Priority chain: Battery (already charging) → Miner → VP → EV → Pool → Export.
        Lower thresholds than normal surplus — we want to absorb EVERYTHING.
        """
        # 1. Miner: always ON during taper (instant ~400W absorption)
        if self._miner_entity and not self._miner_on:
            _LOGGER.info("CARMA taper: miner ON (absorb %.0fW export)", export_w)
            await self._cmd_miner(True)

        # 2. VP: pre-heat/cool at lower threshold than normal
        if export_w > TAPER_VP_SURPLUS_W:
            await self._execute_climate(state)

        # 3. EV: charge from surplus at lower threshold (6A min = ~1380W)
        if export_w > TAPER_EV_SURPLUS_W:
            await self._execute_ev(state)
        elif self._ev_enabled:
            # Not enough surplus for EV — let normal EV logic handle
            await self._execute_ev(state)

        # 4. Pool: activate if surplus remains
        await self._execute_pool(state)

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
                    "CARMA: Pool ON (surplus %.0fW, temp %s)", abs(state.grid_power_w), temp_str
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
        except Exception:
            _LOGGER.warning("CARMA: pool switch failed: %s", entity_id, exc_info=True)

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
        wd_export_w = float(self._cfg.get("watchdog_export_w", DEFAULT_WATCHDOG_EXPORT_W))
        wd_discharge_min = float(
            self._cfg.get("watchdog_discharge_min_w", DEFAULT_WATCHDOG_DISCHARGE_MIN_W)
        )
        wd_ev_import_w = float(self._cfg.get("watchdog_ev_import_w", DEFAULT_WATCHDOG_EV_IMPORT_W))
        wd_min_soc = float(self._cfg.get("watchdog_min_soc_pct", DEFAULT_WATCHDOG_MIN_SOC_PCT))
        price_expensive = float(self._cfg.get("price_expensive_ore", DEFAULT_PRICE_EXPENSIVE_ORE))
        if (
            state.is_exporting
            and abs(state.grid_power_w) > wd_export_w
            and not state.all_batteries_full
            and action not in ("charge_pv", "grid_charge")
        ):
            _LOGGER.warning(
                "WATCHDOG W1: exporting %.0fW, bat %s%%, action=%s → correcting to charge_pv",
                abs(state.grid_power_w),
                state.total_battery_soc,
                action,
            )
            await self._cmd_charge_pv(state)
            self._record_decision(
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
                    await self._cmd_discharge(state, discharge_w)
                    self._record_decision(
                        state,
                        "discharge",
                        f"Watchdog: grid {weighted_net / 1000:.1f} kW "
                        f"> target {self.target_kw:.1f} kW "
                        f"men var {action} → urladdning {discharge_w}W",
                        discharge_w=discharge_w,
                    )
                    # CARMA-P0-FIXES Task 3d: Call miner from watchdog discharge path too
                    await self._execute_ev(state)
                    await self._execute_miner(state)
                    await self._execute_climate(state)
                    return

        # W4: EV charging + grid importing during day
        if (
            not is_night
            and self._ev_enabled
            and not state.is_exporting
            and state.grid_power_w > wd_ev_import_w
        ):
            _LOGGER.warning(
                "WATCHDOG W4: EV charging but grid importing %.0fW → stopping EV",
                state.grid_power_w,
            )
            await self._cmd_ev_stop()

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

    async def _execute_ev(self, state: CarmaboxState) -> None:
        """Execute EV charging decisions (PLAT-949).

        Runs AFTER battery rules. Controls Easee enable/disable + amps.
        Always starts at 6A, ramps gradually, reduces immediately.
        """
        import time as _time

        if not self.ev_adapter:
            return

        hour = datetime.now().hour
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        night_weight = float(self._cfg.get("night_weight", DEFAULT_NIGHT_WEIGHT))

        # ── EV-1: Not connected → stop ───────────────────────
        if not self.ev_adapter.cable_locked or state.ev_soc < 0:
            if self._ev_enabled:
                await self._cmd_ev_stop()
            return

        # ── EV-2: Target SoC reached → stop ──────────────────
        ev_target = float(self._cfg.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC))
        if state.ev_soc >= ev_target:
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
                    and state.ev_soc >= 0
                    and state.ev_soc < ev_target
                    and current_price < price_expensive
                ):
                    # Charge at 6A minimum — better than doing nothing
                    if not self._ev_enabled:
                        await self._cmd_ev_start(6)
                    elif self._ev_current_amps != 6:
                        await self._cmd_ev_adjust(6)
                    return
                # No fallback applicable — stop EV
                if self._ev_enabled:
                    await self._cmd_ev_stop()
                return

            # Calculate optimal amps from grid headroom
            ev_load_kw = state.ev_power_w / 1000
            house_only_kw = max(0, max(0, state.grid_power_w) / 1000 - ev_load_kw)
            weight = night_weight if is_night else 1.0
            ev_max_hw = float(self._cfg.get("ev_night_headroom_kw", DEFAULT_EV_NIGHT_HEADROOM_KW))
            headroom_kw = (self.target_kw / weight - house_only_kw) if weight > 0 else ev_max_hw
            optimal_amps = max(0, int(headroom_kw * 1000 / DEFAULT_VOLTAGE))
            optimal_amps = min(optimal_amps, DEFAULT_EV_MAX_AMPS)

            if optimal_amps >= 6:
                if not self._ev_enabled:
                    await self._cmd_ev_start(6)
                elif optimal_amps > self._ev_current_amps:
                    now = _time.monotonic()
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
        # EV dagtid laddar ENBART från export — aldrig nätimport.
        # Grid < 0 = vi exporterar = EV kan ta den effekten.
        # Grid ≥ 0 = vi importerar redan = EV får INTE starta/öka.
        if state.is_exporting:
            export_kw = abs(state.grid_power_w) / 1000
            # If EV already charging, available = export + current EV load
            # (stopping EV would increase export by that amount)
            if self._ev_enabled:
                export_kw += state.ev_power_w / 1000
            solar_amps = max(0, int(export_kw * 1000 / DEFAULT_VOLTAGE))
            solar_amps = min(solar_amps, DEFAULT_EV_MAX_AMPS)
            if solar_amps >= 6:
                if not self._ev_enabled:
                    await self._cmd_ev_start(6)
                else:
                    await self._cmd_ev_adjust(solar_amps)
                return
            # Export < 6A worth → stop EV to avoid grid import
            if self._ev_enabled and export_kw < 1.0:
                await self._cmd_ev_stop()
                return
        elif self._ev_enabled:
            # Grid ≥ 0 (importing) + EV charging → EV causes grid import → stop
            await self._cmd_ev_stop()
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
        is_daytime = not is_night
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

        # ── (a) High SoC + daytime → keep ON (batteries support) ──
        if state.total_battery_soc > 80 and is_daytime:
            if not self._miner_on:
                _LOGGER.info(
                    "CARMA: High battery %.0f%% + daytime → miner ON (battery supports)",
                    state.total_battery_soc,
                )
                await self._cmd_miner(True)
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
        except Exception:
            _LOGGER.warning("CARMA: miner control failed", exc_info=True)

    async def _cmd_ev_start(self, amps: int = 6) -> None:
        """Start EV: set current FIRST, then enable (prevent 16A burst)."""
        amps = max(6, min(amps, DEFAULT_EV_MAX_AMPS))
        if self._ev_enabled and self._ev_current_amps == amps:
            return
        if not self.ev_adapter:
            return
        _LOGGER.info("CARMA: EV start %dA", amps)
        ok = await self.ev_adapter.set_current(amps)
        if not ok:
            return
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
        """Stop EV: disable + reset to 6A."""
        if not self.ev_adapter:
            return
        _LOGGER.info("CARMA: EV stop")
        await self.ev_adapter.disable()
        await self.ev_adapter.reset_to_default()
        self._ev_enabled = False
        self._ev_current_amps = 0
        # CARMA-P0-FIXES Task 4: Save runtime after EV state change
        await self._async_save_runtime()

    async def _cmd_ev_adjust(self, amps: int) -> None:
        """Adjust EV amps without enable/disable."""
        if not self.ev_adapter or not self._ev_enabled:
            return
        amps = max(6, min(amps, DEFAULT_EV_MAX_AMPS))
        if amps == self._ev_current_amps:
            return
        _LOGGER.info("CARMA: EV adjust %dA → %dA", self._ev_current_amps, amps)
        ok = await self.ev_adapter.set_current(amps)
        if ok:
            self._ev_current_amps = amps
            # CARMA-P0-FIXES Task 4: Save runtime after EV amps change
            await self._async_save_runtime()

    def _record_decision(
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
        self.hass.async_create_task(
            self._log_decision(reason),
            "carmabox_logbook_entry",
        )

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
        """Read battery temperature — uses adapters when available, else legacy entity."""
        if self.inverter_adapters:
            temps = [a.temperature_c for a in self.inverter_adapters if a.temperature_c is not None]
            return min(temps) if temps else None
        temp_entity = self._get_entity("battery_temp_entity", "")
        if not temp_entity:
            return None
        val = self._read_float(temp_entity, -999)
        return val if val > -999 else None

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
        import time as _time

        if _time.monotonic() < self._ems_pause_until:
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
            return {"score_today": None, "score_7d": None, "score_30d": None, "trend": "stable"}

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

        # R1: If max > 2× target → high confidence suggestion
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

        # R3: If most expensive hour > 2× cheapest → shift consumption
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
        grid_kw = max(0, d.grid_kw) if d else 0.0
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
        eff_min_soc = self._reserve_target_pct
        guards = [
            {
                "id": "guard_crosscharge",
                "label": "Korsladdningsskydd",
                "icon": "mdi:shield-check",
                "status": "ok",
            },
            {
                "id": "guard_min_soc",
                "label": f"Min batteri {eff_min_soc:.0f}%",
                "icon": "mdi:battery-alert",
                "status": "ok" if (d and d.battery_soc > eff_min_soc) else "warning",
            },
            {
                "id": "guard_peak_risk",
                "label": f"Peak-risk: {self._peak_risk_status(grid_kw)}",
                "icon": "mdi:transmission-tower-export",
                "status": ("ok" if self._peak_risk_status(grid_kw) == "safe" else "warning"),
            },
            {
                "id": "guard_spike",
                "label": "Vitvaru-skydd",
                "icon": "mdi:washing-machine-alert",
                "status": "active" if self._spike_active else "ok",
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
        except Exception:
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
            # Accumulate energy (Wh) = power_w × interval_hours
            self.appliance_energy_wh[category] = (
                self.appliance_energy_wh.get(category, 0.0) + power_w * interval_h
            )

        self.appliance_power = category_power

    # ── IT-2067: Peak Tracking System ─────────────────────────────

    def _track_peaks(self, grid_kw: float) -> None:
        """Update rolling top-3 monthly peaks (called every cycle).

        Actual update happens every PEAK_UPDATE_INTERVAL_S (5 min) to avoid
        noise from transient spikes. Resets on 1st of each month.
        """
        now_mono = time.monotonic()
        now_dt = datetime.now()

        # Monthly reset
        if now_dt.month != self._peak_month:
            old_peaks = list(self._peak_ranks)
            self._peak_ranks = [0.0] * PEAK_RANK_COUNT
            self._peak_month = now_dt.month
            self._runtime_dirty = True
            _LOGGER.info("CARMA peak tracking: monthly reset (old peaks: %s)", old_peaks)

        # Only update every 5 min
        if now_mono - self._peak_last_update < PEAK_UPDATE_INTERVAL_S:
            return
        self._peak_last_update = now_mono

        if grid_kw <= 0:
            return

        # Insert into sorted top-3 (descending)
        for i in range(PEAK_RANK_COUNT):
            if grid_kw > self._peak_ranks[i]:
                # Shift down
                self._peak_ranks.insert(i, grid_kw)
                self._peak_ranks = self._peak_ranks[:PEAK_RANK_COUNT]
                self._runtime_dirty = True
                _LOGGER.info(
                    "CARMA peak tracking: new rank %d = %.2f kW (peaks: %s)",
                    i + 1,
                    grid_kw,
                    [f"{p:.2f}" for p in self._peak_ranks],
                )
                break

    def _peak_risk_status(self, current_grid_kw: float) -> str:
        """Calculate peak risk: safe/warning/risk.

        Returns 'safe' if rank_3 < PEAK_MIN_MEANINGFUL_KW to avoid
        false positives during normal house consumption (2-3 kW).
        """
        rank_3 = self._peak_ranks[-1] if self._peak_ranks else 0.0
        if rank_3 < PEAK_MIN_MEANINGFUL_KW:
            return "safe"
        margin = float(self._cfg.get("peak_warning_margin_kw", PEAK_WARNING_MARGIN_KW))
        if current_grid_kw >= rank_3:
            return "risk"
        if current_grid_kw >= (rank_3 - margin):
            return "warning"
        return "safe"

    def _adjusted_target_kw(self, is_night: bool, current_grid_kw: float) -> float:
        """Dynamic grid import target adjusted by peak risk status.

        Base targets from config (day/night), reduced when approaching peaks.
        """
        if is_night:
            base = float(self._cfg.get("target_night_kw", DEFAULT_TARGET_NIGHT_KW))
        else:
            base = float(self._cfg.get("target_day_kw", DEFAULT_TARGET_DAY_KW))

        margin = float(self._cfg.get("peak_warning_margin_kw", PEAK_WARNING_MARGIN_KW))
        risk = self._peak_risk_status(current_grid_kw)

        if risk == "risk":
            return max(0.5, base - margin)
        if risk == "warning":
            return max(0.5, base - margin / 2)
        return base

    # ── IT-2067: Appliance Spike Detection & Response ─────────────

    def _detect_appliance_spike(self, grid_w: float) -> bool:
        """Detect sudden grid power spike (appliance start).

        Compares current grid_w to the minimum in the last 60s window.
        If delta > threshold → spike detected.
        """
        now_mono = time.monotonic()
        self._grid_power_history.append((now_mono, grid_w))

        # Find min in window
        cutoff = now_mono - SPIKE_HISTORY_WINDOW_S
        min_w = grid_w
        for ts, w in self._grid_power_history:
            if ts >= cutoff and w < min_w:
                min_w = w

        delta = grid_w - min_w
        return delta > SPIKE_DETECTION_THRESHOLD_W

    async def _handle_appliance_spike(self, state: CarmaboxState) -> None:
        """Respond to detected appliance spike.

        Lowers peak_shaving_power_limit to force battery discharge
        to compensate for the spike. Also reduces EV amps at night.
        """
        if self._spike_active:
            return  # Already handling a spike

        # Check discharge is allowed (SoC + temp)
        temp_c = self._read_battery_temp()
        discharge_ok = self.safety.check_discharge(
            state.battery_soc_1,
            state.battery_soc_2,
            self.min_soc,
            state.grid_power_w,
            temp_c,
        )
        if not discharge_ok.ok:
            _LOGGER.debug("Spike detected but discharge blocked: %s", discharge_ok.reason)
            return

        self._spike_active = True
        self._spike_activated_at = time.monotonic()
        self._spike_cooldown_started = 0.0

        spike_limit = int(self._cfg.get("spike_ps_limit_w", SPIKE_PS_LIMIT_W))
        _LOGGER.warning(
            "CARMA: Appliance spike detected — grid %.0fW, "
            "lowering PS limit to %dW for battery compensation",
            state.grid_power_w,
            spike_limit,
        )

        # Lower PS limit on all inverters
        for adapter in self.inverter_adapters:
            await adapter.set_discharge_limit(spike_limit)

        # At night with EV charging: reduce EV amps
        hour = datetime.now().hour
        is_night = hour >= 22 or hour < 6
        if is_night and self._ev_enabled and self.ev_adapter and self._ev_current_amps > 6:
            _LOGGER.info("CARMA: Spike at night — reducing EV to 6A")
            await self._cmd_ev_adjust(6)

    async def _handle_spike_recovery(self, grid_w: float) -> None:
        """Recover from appliance spike when grid power normalizes.

        Waits for cooldown period, then restores normal PS limit.
        Also handles safety timeout (10 min max).
        """
        if not self._spike_active:
            return

        now_mono = time.monotonic()

        # Safety timeout — force reset after 10 min
        if now_mono - self._spike_activated_at > SPIKE_SAFETY_TIMEOUT_S:
            _LOGGER.warning(
                "CARMA: Spike safety timeout (>%ds) — force resetting",
                SPIKE_SAFETY_TIMEOUT_S,
            )
            await self._restore_spike_ps_limit()
            return

        # Check if spike condition has ended
        spike_still_active = self._detect_appliance_spike(grid_w)
        if spike_still_active:
            self._spike_cooldown_started = 0.0  # Reset cooldown
            return

        # Start cooldown timer
        cooldown_s = int(self._cfg.get("spike_cooldown_s", SPIKE_COOLDOWN_S))
        if self._spike_cooldown_started == 0.0:
            self._spike_cooldown_started = now_mono
            return

        # Wait for cooldown
        if now_mono - self._spike_cooldown_started < cooldown_s:
            return

        # Cooldown complete — restore
        _LOGGER.info("CARMA: Spike cooldown complete — restoring normal PS limit")
        await self._restore_spike_ps_limit()

    async def _restore_spike_ps_limit(self) -> None:
        """Restore PS limit after spike, respecting current battery state."""
        self._spike_active = False
        self._spike_cooldown_started = 0.0

        # If currently discharging, use dynamic limit; otherwise default (20000W)
        if self._last_command == BatteryCommand.DISCHARGE:
            limit = self._dynamic_discharge_limit_w()
        else:
            limit = SPIKE_DEFAULT_PS_LIMIT_W

        for adapter in self.inverter_adapters:
            await adapter.set_discharge_limit(limit)

    # ── IT-2067: Reserve Target (Solcast-based) ──────────────────

    def _calculate_reserve_target(self) -> float:
        """Calculate dynamic min SoC based on Solcast forecast + temperature.

        Strong sun (>20 kWh tomorrow) → min_soc + 0% (batteries refill from sun)
        Weak sun (<5 kWh tomorrow)    → min_soc + 10% (need grid backup)
        Neutral (5-20 kWh)            → min_soc + 5%

        Cold temperature → use cold_min_soc (20%) as base instead of 15%.
        """
        # Update every 5 min (same as plan interval)
        now_mono = time.monotonic()
        if now_mono - self._reserve_last_calc < PLAN_INTERVAL_SECONDS:
            return self._reserve_target_pct
        self._reserve_last_calc = now_mono

        # Temperature-adjusted base
        cold_threshold = float(self._cfg.get("cold_temp_threshold_c", COLD_TEMP_THRESHOLD_C))
        temp_1 = None
        temp_2 = None
        if self.inverter_adapters:
            a1 = self.inverter_adapters[0] if len(self.inverter_adapters) >= 1 else None
            a2 = self.inverter_adapters[1] if len(self.inverter_adapters) >= 2 else None
            temp_1 = a1.temperature_c if a1 else None
            temp_2 = a2.temperature_c if a2 else None

        is_cold = False
        if temp_1 is not None and temp_1 < cold_threshold:
            is_cold = True
        if temp_2 is not None and temp_2 < cold_threshold:
            is_cold = True

        base_min_soc = COLD_MIN_SOC_PCT if is_cold else self.min_soc

        # Solcast forecast for tomorrow
        solcast_entity = self._get_entity(
            "solcast_tomorrow_entity",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
        )
        forecast_kwh = self._read_float(solcast_entity, -1.0)

        strong_threshold = float(self._cfg.get("reserve_pv_strong_kwh", RESERVE_PV_STRONG_KWH))
        weak_threshold = float(self._cfg.get("reserve_pv_weak_kwh", RESERVE_PV_WEAK_KWH))

        if forecast_kwh < 0:
            # Forecast unavailable → neutral
            offset = RESERVE_OFFSET_NEUTRAL_PCT
            scenario = "forecast_unavailable"
        elif forecast_kwh >= strong_threshold:
            offset = RESERVE_OFFSET_STRONG_PCT
            scenario = "strong_sun"
        elif forecast_kwh < weak_threshold:
            offset = RESERVE_OFFSET_WEAK_PCT
            scenario = "weak_sun"
        else:
            offset = RESERVE_OFFSET_NEUTRAL_PCT
            scenario = "neutral"

        target = base_min_soc + offset
        if abs(target - self._reserve_target_pct) > 0.5:
            _LOGGER.info(
                "CARMA reserve target: %.1f%% (%s, forecast=%.1f kWh, "
                "base=%.0f%%, offset=+%.0f%%, cold=%s)",
                target,
                scenario,
                forecast_kwh,
                base_min_soc,
                offset,
                is_cold,
            )
        self._reserve_target_pct = target
        return target

    # ── IT-2067: Dynamic Discharge Limit ─────────────────────────

    def _dynamic_discharge_limit_w(self) -> int:
        """Calculate PS limit for discharge based on SoC and time of day.

        Higher SoC = lower PS limit = more aggressive discharge.
        Night: ×2 factor (Ellevio weights night at ×0.5).

        Returns peak_shaving_power_limit in watts.
        """
        # Average SoC across batteries
        state = getattr(self, "data", None)
        if state and hasattr(state, "battery_soc_1"):
            if state.has_battery_2:
                avg_soc = (state.battery_soc_1 + state.battery_soc_2) / 2
            else:
                avg_soc = state.battery_soc_1
        else:
            avg_soc = 50.0  # Safe default

        if avg_soc > 60:
            base = DISCHARGE_LIMIT_HIGH_SOC_W
        elif avg_soc > 40:
            base = DISCHARGE_LIMIT_MID_SOC_W
        elif avg_soc > 20:
            base = DISCHARGE_LIMIT_LOW_SOC_W
        else:
            base = DISCHARGE_LIMIT_VERY_LOW_SOC_W

        hour = datetime.now().hour
        is_night = hour >= 22 or hour < 6
        factor = DISCHARGE_NIGHT_FACTOR if is_night else 1.0

        return int(base * factor)

    # ── IT-2067: Cold Temperature Per-Battery Protection ──────────

    def _effective_min_soc(self) -> float:
        """Get effective minimum SoC considering temperature and forecast.

        Uses reserve target (Solcast-adjusted) which already includes
        cold temperature protection.
        """
        return self._calculate_reserve_target()

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
        # Annual cost impact (very rough): delta × peak_cost / samples_per_hour
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
            self._peak_hour_samples: list[tuple[float, float]] = []  # (actual, baseline)
            self._peak_last_hour: int = -1
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
            # IT-1948: Min cell temperature across both batteries
            cell_temp_min_c=state.battery_temp_c,
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

    async def _self_heal_goodwe_entries(self) -> None:
        """PLAT-972: Self-healing — check GoodWe config entries and reload if needed."""
        import time as _time

        # Skip if paused after repeated failures
        if _time.monotonic() < self._ems_pause_until:
            return

        for adapter in self.inverter_adapters:
            if not isinstance(adapter, GoodWeAdapter):
                continue
            # Check if EMS mode entity is unavailable (integration not loaded)
            ems_entity = f"select.goodwe_{adapter.prefix}_ems_mode"
            ems_state = self.hass.states.get(ems_entity)
            if ems_state is not None and ems_state.state not in ("unavailable", "unknown"):
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
                self._ems_pause_until = _time.monotonic() + SELF_HEALING_PAUSE_SECONDS
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
            except Exception:
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

    async def _cmd_charge_pv(self, state: CarmaboxState) -> None:
        """Set batteries to charge from solar.

        SafetyGuard: heartbeat + rate limit + charge check.
        """
        if self._last_command == BatteryCommand.CHARGE_PV:
            return

        # ── SafetyGuard gates (defense-in-depth) ─────────────
        heartbeat = self.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked charge_pv: %s", heartbeat.reason)
            self._daily_safety_blocks += 1
            return

        rate = self.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked charge_pv: %s", rate.reason)
            self._daily_safety_blocks += 1
            return

        temp_c = self._read_battery_temp()
        charge_check = self.safety.check_charge(state.battery_soc_1, state.battery_soc_2, temp_c)
        if not charge_check.ok:
            _LOGGER.info("SafetyGuard blocked charge_pv: %s", charge_check.reason)
            self._daily_safety_blocks += 1
            return

        _LOGGER.info("CARMA: charge_pv (solar surplus)")
        success = False
        failed = False

        if self.inverter_adapters:
            for adapter in self.inverter_adapters:
                if adapter.soc >= 100:
                    ok = await adapter.set_ems_mode("battery_standby")
                else:
                    ok = await adapter.set_ems_mode("charge_pv")
                    # Enable fast charging for max PV absorption
                    if ok and isinstance(adapter, GoodWeAdapter):
                        await adapter.set_fast_charging(on=True, power_pct=100, soc_target=100)
                if ok:
                    success = True
                else:
                    failed = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning("Partial charge_pv failure — rolling back all to standby")
                for adapter in self.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                self._daily_safety_blocks += 1
                success = False
        else:
            # Legacy: raw entity-based control
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._get_entity(ems_key)
                if not entity:
                    continue
                soc_key = ems_key.replace("ems", "soc")
                soc = self._read_float(self._get_entity(soc_key))
                mode = "battery_standby" if soc >= 100 else "charge_pv"
                if await self._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": mode}
                ):
                    if self.executor_enabled:
                        self._check_write_verify(entity, mode)
                    success = True
                else:
                    failed = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning("Partial charge_pv failure — rolling back all to standby (legacy)")
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._get_entity(ems_key)
                    if entity:
                        await self._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._daily_safety_blocks += 1
                success = False

        if success:
            self._last_command = BatteryCommand.CHARGE_PV
            self.safety.record_mode_change()
            # CARMA-P0-FIXES Task 4: Save runtime after command change
            await self._async_save_runtime()

    async def _cmd_grid_charge(self, state: CarmaboxState) -> None:
        """Set batteries to charge from grid (CARMA-P0-FIXES Task 2).

        GoodWe: charge_pv + fast_charging = charges from grid when no PV.
        SafetyGuard: heartbeat + rate limit + charge check.
        """
        if self._last_command == BatteryCommand.CHARGE_PV:
            # Already in charge mode — just ensure fast charging is on
            if self.inverter_adapters:
                for adapter in self.inverter_adapters:
                    if isinstance(adapter, GoodWeAdapter) and adapter.soc < 100:
                        await adapter.set_fast_charging(on=True, power_pct=100, soc_target=100)
            return

        # ── SafetyGuard gates (defense-in-depth) ─────────────
        heartbeat = self.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked grid_charge: %s", heartbeat.reason)
            self._daily_safety_blocks += 1
            return

        rate = self.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked grid_charge: %s", rate.reason)
            self._daily_safety_blocks += 1
            return

        temp_c = self._read_battery_temp()
        charge_check = self.safety.check_charge(state.battery_soc_1, state.battery_soc_2, temp_c)
        if not charge_check.ok:
            _LOGGER.info("SafetyGuard blocked grid_charge: %s", charge_check.reason)
            self._daily_safety_blocks += 1
            return

        _LOGGER.info("CARMA: grid_charge (cheap price)")
        success = False
        failed = False

        if self.inverter_adapters:
            for adapter in self.inverter_adapters:
                if adapter.soc >= 100:
                    ok = await adapter.set_ems_mode("battery_standby")
                else:
                    ok = await adapter.set_ems_mode("charge_pv")
                    # Enable fast charging with grid import — GoodWe charges from grid when no PV
                    if ok and isinstance(adapter, GoodWeAdapter):
                        ok = await adapter.set_fast_charging(on=True, power_pct=100, soc_target=100)
                if ok:
                    success = True
                else:
                    failed = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning("Partial grid_charge failure — rolling back all to standby")
                for adapter in self.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                    if isinstance(adapter, GoodWeAdapter):
                        await adapter.set_fast_charging(on=False, power_pct=0, soc_target=100)
                self._daily_safety_blocks += 1
                success = False
        else:
            # Legacy: raw entity-based control
            # Note: legacy mode doesn't have fast_charging — grid charge won't work properly
            _LOGGER.warning(
                "Grid charge requested but no GoodWe adapter — using charge_pv (may not work)"
            )
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._get_entity(ems_key)
                if not entity:
                    continue
                soc_key = ems_key.replace("ems", "soc")
                soc = self._read_float(self._get_entity(soc_key))
                mode = "battery_standby" if soc >= 100 else "charge_pv"
                if await self._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": mode}
                ):
                    if self.executor_enabled:
                        self._check_write_verify(entity, mode)
                    success = True
                else:
                    failed = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning(
                    "Partial grid_charge failure — rolling back all to standby (legacy)"
                )
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._get_entity(ems_key)
                    if entity:
                        await self._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._daily_safety_blocks += 1
                success = False

        if success:
            self._last_command = BatteryCommand.CHARGE_PV  # Same command enum
            self.safety.record_mode_change()
            # CARMA-P0-FIXES Task 4: Save runtime after command change
            await self._async_save_runtime()

    async def _cmd_standby(self, state: CarmaboxState, force: bool = False) -> None:
        """Set all batteries to standby.

        SafetyGuard: heartbeat + rate limit (skipped when force=True
        since forced standby is itself a safety action).
        """
        if not force and self._last_command == BatteryCommand.STANDBY:
            return

        if not force:
            # ── SafetyGuard gates (defense-in-depth) ─────────────
            heartbeat = self.safety.check_heartbeat()
            if not heartbeat.ok:
                _LOGGER.warning("SafetyGuard blocked standby: %s", heartbeat.reason)
                self._daily_safety_blocks += 1
                return

            rate = self.safety.check_rate_limit()
            if not rate.ok:
                _LOGGER.info("SafetyGuard blocked standby: %s", rate.reason)
                self._daily_safety_blocks += 1
                return

        _LOGGER.info("CARMA: standby%s", " (forced)" if force else "")
        success = False

        if self.inverter_adapters:
            for adapter in self.inverter_adapters:
                ok = await adapter.set_ems_mode("battery_standby")
                if ok:
                    success = True
                    # Turn off fast charging — don't charge from grid
                    if isinstance(adapter, GoodWeAdapter):
                        await adapter.set_fast_charging(on=False)
        else:
            # Legacy: raw entity-based control
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._get_entity(ems_key)
                if entity and await self._safe_service_call(
                    "select",
                    "select_option",
                    {"entity_id": entity, "option": "battery_standby"},
                ):
                    if self.executor_enabled:
                        self._check_write_verify(entity, "battery_standby")
                    success = True

        if success:
            self._last_command = BatteryCommand.STANDBY
            self.safety.record_mode_change()
            # CARMA-P0-FIXES Task 4: Save runtime after command change
            await self._async_save_runtime()

    async def _cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Set batteries to discharge at specified wattage.

        SafetyGuard: heartbeat + rate limit + discharge check.
        """
        # K1: Skip if already discharging at similar wattage (±100W tolerance)
        if (
            self._last_command == BatteryCommand.DISCHARGE
            and abs(watts - self._last_discharge_w) < 100
        ):
            _LOGGER.debug(
                "K1: skip redundant discharge (%dW ≈ %dW)",
                watts,
                self._last_discharge_w,
            )
            return

        # ── SafetyGuard gates (defense-in-depth) ─────────────
        heartbeat = self.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked discharge: %s", heartbeat.reason)
            self._daily_safety_blocks += 1
            return

        rate = self.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked discharge: %s", rate.reason)
            self._daily_safety_blocks += 1
            return

        temp_c = self._read_battery_temp()
        discharge_check = self.safety.check_discharge(
            state.battery_soc_1,
            state.battery_soc_2,
            self.min_soc,
            state.grid_power_w,
            temp_c,
        )
        if not discharge_check.ok:
            _LOGGER.info("SafetyGuard blocked discharge: %s", discharge_check.reason)
            self._daily_safety_blocks += 1
            return

        _LOGGER.info("CARMA: discharge %dW (target %.1f kW)", watts, self.target_kw)

        if self.inverter_adapters:
            # GoodWe peak_shaving_power_limit = max grid import threshold.
            # To force discharge: set limit to 0 (zero grid import allowed).
            # GoodWe will discharge whatever is needed to keep grid <= limit.
            # The 'watts' parameter is informational (how much we EXPECT to discharge).
            #
            # Each inverter gets limit=0 (target zero import).
            # GoodWe internally splits discharge proportional to its capacity.
            opts = self._cfg
            defaults = [DEFAULT_BATTERY_1_KWH, DEFAULT_BATTERY_2_KWH]
            caps = [
                float(opts.get(f"battery_{i}_kwh", defaults[i - 1]))
                for i in range(1, len(self.inverter_adapters) + 1)
            ]
            stored = [max(0, a.soc) * caps[idx] for idx, a in enumerate(self.inverter_adapters)]
            total_soc = sum(stored)
            if total_soc <= 0:
                return

            success = False
            failed = False
            for idx, adapter in enumerate(self.inverter_adapters):
                if stored[idx] <= 0:
                    continue
                # IT-998: Use peak_shaving mode + low limit to force discharge.
                # discharge_battery mode does NOT respond to ems_power_limit.
                # peak_shaving mode actively discharges to keep grid <= limit.
                ems_ok = await adapter.set_ems_mode("peak_shaving")
                if not ems_ok:
                    failed = True
                    continue
                # IT-2067: Use dynamic PS limit based on SoC (not always 0).
                # Higher SoC = lower limit = more aggressive discharge.
                # During spike: spike handler manages the limit separately.
                ps_limit = 0 if self._spike_active else self._dynamic_discharge_limit_w()
                limit_ok = await adapter.set_discharge_limit(ps_limit)
                if not limit_ok:
                    # K2: Rollback EMS if limit failed — avoid stale discharge
                    _LOGGER.error("Discharge limit failed — rolling back to standby")
                    await adapter.set_ems_mode("battery_standby")
                    failed = True
                    continue
                success = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning("Partial discharge failure — rolling back all to standby")
                for adapter in self.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                self._daily_safety_blocks += 1
                success = False

            if success:
                self._last_command = BatteryCommand.DISCHARGE
                self._last_discharge_w = watts
                self.safety.record_mode_change()
                # CARMA-P0-FIXES Task 4: Save runtime after command change
                await self._async_save_runtime()
                # CARMA-P0-FIXES Task 4: Save runtime after command change
                await self._async_save_runtime()
        else:
            # Legacy: raw entity-based control
            # Energy-proportional split (SoC × capacity)
            opts = self._cfg
            cap1 = float(opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH))
            cap2 = float(opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH))
            energy_1 = state.battery_soc_1 * cap1
            energy_2 = max(0, state.battery_soc_2) * cap2
            total_energy = energy_1 + energy_2
            if total_energy <= 0:
                return

            ratio_1 = energy_1 / total_energy
            w1 = int(watts * ratio_1)
            w2 = watts - w1

            success = False
            failed = False
            for ems_key, limit_key, w in [
                ("battery_ems_1", "battery_limit_1", w1),
                ("battery_ems_2", "battery_limit_2", w2),
            ]:
                ems_entity = self._get_entity(ems_key)
                limit_entity = self._get_entity(limit_key)
                if ems_entity and w > 0:
                    ems_ok = await self._safe_service_call(
                        "select",
                        "select_option",
                        {"entity_id": ems_entity, "option": "discharge_battery"},
                    )
                    if not ems_ok:
                        # Fail-safe: do NOT set discharge limit if EMS mode failed
                        failed = True
                        continue
                    if self.executor_enabled:
                        self._check_write_verify(ems_entity, "discharge_battery")
                    if limit_entity:
                        await self._safe_service_call(
                            "number", "set_value", {"entity_id": limit_entity, "value": w}
                        )
                        success = True

            # R3: Rollback on partial failure — force ALL to standby
            if failed and success:
                _LOGGER.warning("Partial discharge failure — rolling back all to standby (legacy)")
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._get_entity(ems_key)
                    if entity:
                        await self._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._daily_safety_blocks += 1
                success = False

            if success:
                self._last_command = BatteryCommand.DISCHARGE
                self._last_discharge_w = watts
                self.safety.record_mode_change()
                # CARMA-P0-FIXES Task 4: Save runtime after command change
                await self._async_save_runtime()
