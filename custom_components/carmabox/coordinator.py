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
import contextlib
import logging
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
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_DAILY_BATTERY_NEED_KWH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_FALLBACK_PRICE_ORE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_MAX_GRID_CHARGE_KW,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_TARGET_WEIGHTED_KW,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .optimizer.consumption import ConsumptionProfile, calculate_house_consumption
from .optimizer.ev_strategy import calculate_ev_schedule
from .optimizer.grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .optimizer.models import CarmaboxState, Decision, HourActual, HourPlan, ShadowComparison
from .optimizer.planner import generate_plan
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
        self.plan: list[HourPlan] = []
        # Start at threshold-1 so first update generates a plan immediately
        self._plan_counter = (PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS) - 1
        self._last_command = BatteryCommand.IDLE
        self._last_discharge_w = 0

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
        self.decision_log: list[Decision] = []

        # Plan accuracy tracking
        self.hourly_actuals: list[HourActual] = []
        self._last_tracked_hour: int = -1

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
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        self._daily_avg_price: float = float(
            self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE)
        )
        self._avg_price_initialized = False
        self.executor_enabled: bool = bool(self._cfg.get("executor_enabled", False))

        # PLAT-943: Appliance tracking
        self._appliances: list[dict[str, Any]] = list(self._cfg.get("appliances") or [])
        # Current power per category (W), updated every scan
        self.appliance_power: dict[str, float] = {}
        # Daily energy per category (Wh), reset at midnight
        self.appliance_energy_wh: dict[str, float] = {}

        # Propagate dry_run to adapters
        for adapter in self.inverter_adapters:
            adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]
        if self.ev_adapter:
            self.ev_adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]

        if not self.executor_enabled:
            _LOGGER.warning("CARMA Box running in ANALYZER mode — no commands will be sent")

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

            old_month = self.savings.month
            self.savings = reset_if_new_month(self.savings, now)
            if self.savings.month != old_month:
                self._ellevio_monthly_hourly_peaks = []
            self.report_collector = reset_report_month(self.report_collector, now)
            self._reset_daily_counters_if_new_day(now)
            if not self._avg_price_initialized:
                self._update_daily_avg_price()
                self._avg_price_initialized = True
            self.safety.update_heartbeat()
            state = self._collect_state()

            self._plan_counter += 1
            if self._plan_counter >= PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS:
                self._plan_counter = 0
                self._generate_plan(state)
                self._check_repair_issues()

            await self._execute(state)
            self._track_shadow(state)
            self._track_savings(state)
            self._track_appliances()
            await self._async_save_savings()
            await self._async_save_consumption()
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

        # EV — adapter or legacy config
        ev = self.ev_adapter
        ev_power_w = ev.power_w if ev else self._read_float(opts.get("ev_power_entity", ""))
        ev_current_a = ev.current_a if ev else self._read_float(opts.get("ev_current_entity", ""))
        ev_status = ev.status if ev else self._read_str(opts.get("ev_status_entity", ""))

        return CarmaboxState(
            grid_power_w=self._read_float(opts.get("grid_entity", "sensor.house_grid_power")),
            battery_soc_1=battery_soc_1,
            battery_power_1=battery_power_1,
            battery_ems_1=battery_ems_1,
            battery_cap_1_kwh=float(opts.get("battery_1_kwh", 15.0)),
            battery_soc_2=battery_soc_2,
            battery_power_2=battery_power_2,
            battery_ems_2=battery_ems_2,
            battery_cap_2_kwh=float(opts.get("battery_2_kwh", 5.0)),
            pv_power_w=self._read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            ev_soc=self._read_float(opts.get("ev_soc_entity", ""), -1),
            ev_power_w=ev_power_w,
            ev_current_a=ev_current_a,
            ev_status=ev_status,
            battery_temp_c=self._read_battery_temp(),
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

            # Collect PV forecast
            solcast = SolcastAdapter(self.hass)
            pv_hourly = solcast.today_hourly_kw
            pv_forecast = pv_hourly[start_hour:] + [0.0] * 24  # Pad tomorrow

            # Consumption profile from const.py (configurable via options)
            # Use learned profile if available, else static default
            base = self.consumption_profile.get_profile_for_date(now)
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
            pv_tomorrow = solcast.tomorrow_kwh

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
                    pv_tomorrow_kwh=pv_tomorrow,
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
                battery_soc=state.battery_soc_1,
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

        # Crosscharge check every cycle
        crosscharge = self.safety.check_crosscharge(state.battery_power_1, state.battery_power_2)
        if not crosscharge.ok:
            _LOGGER.warning("SafetyGuard crosscharge: %s", crosscharge.reason)
            self._daily_safety_blocks += 1
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
        is_night = hour >= 22 or hour < 6

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
        if state.current_price < 30:
            tier = "billigt"
            intensity = "passiv — spara batteri"
        elif state.current_price < 80:
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
                    step5 = f"Blockerad: {charge_result.reason}"
                    reasoning.append(f"Laddning blockerad: {charge_result.reason}")
                    reasoning.append(step5)
                    chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                    self._record_decision(
                        state,
                        "blocked",
                        f"Laddning blockerad — {charge_result.reason}",
                        safety_blocked=True,
                        safety_reason=charge_result.reason,
                        reasoning=reasoning,
                        reasoning_chain=chain,
                    )
                    self._daily_safety_blocks += 1
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
        grid_charge_threshold = float(self._cfg.get(
            "grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD
        ))
        grid_charge_max_soc = float(self._cfg.get("grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC))
        if (
            state.current_price > 0
            and state.current_price < grid_charge_threshold
            and state.total_battery_soc < grid_charge_max_soc
            and not state.is_exporting
        ):
            reasoning.append(
                f"Pris {state.current_price:.0f} öre < {grid_charge_threshold:.0f} → nätladda batteri"
            )
            charge_result = self.safety.check_charge(
                state.battery_soc_1, state.battery_soc_2, temp_c
            )
            if charge_result.ok:
                await self._cmd_charge_pv(state)  # charge_pv works for grid charge too
                self._record_decision(
                    state,
                    "grid_charge",
                    f"Nätladdning — {state.current_price:.0f} öre (billigt), "
                    f"batteri {state.total_battery_soc:.0f}%",
                    reasoning=reasoning,
                )
                return

        # ── RULE 2: SoC 100% → standby ──────────────────────
        if state.all_batteries_full:
            step5 = f"Standby — alla batterier fulla, Ellevio ser {weighted_net / 1000:.1f} kW"
            reasoning.append("Alla batterier 100% → standby, spara för kväll/natt")
            reasoning.append(step5)
            chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
            await self._cmd_standby(state)
            self._record_decision(
                state,
                "standby",
                "Standby — alla batterier 100%",
                reasoning=reasoning,
                reasoning_chain=chain,
            )
            return

        # ── RULE 3: Load > target → discharge ────────────────
        if weighted_net > target_w and weight > 0:
            discharge_w = int((weighted_net - target_w) / weight)
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
                ellevio_saving = (weighted_net / 1000 - self.target_kw) * 80
                step5 = (
                    f"Urladdning {discharge_w}W → Ellevio ser {self.target_kw:.1f} kW "
                    f"istf {weighted_net / 1000:.1f} kW, sparar ~{ellevio_saving:.0f} kr/mån"
                )
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                await self._cmd_discharge(state, discharge_w)
                self._record_decision(
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
                step5 = f"Urladdning blockerad: {result.reason}"
                reasoning.append(step5)
                chain.append({"step": "resultat", "label": "Resultat", "detail": step5})
                self._record_decision(
                    state,
                    "blocked",
                    f"Urladdning blockerad — {result.reason}",
                    safety_blocked=True,
                    safety_reason=result.reason,
                    reasoning=reasoning,
                    reasoning_chain=chain,
                )
                self._daily_safety_blocks += 1
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
        self._record_decision(
            state,
            "idle",
            f"Vila — grid {weighted_net / 1000:.2f} kW viktat "
            f"< target {self.target_kw:.1f} kW "
            f"({state.current_price:.0f} öre/kWh)",
            reasoning=reasoning,
            reasoning_chain=chain,
        )

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

        # Keep last 48 decisions (24h at 30min intervals)
        self.decision_log.append(decision)
        if len(self.decision_log) > 48:
            self.decision_log = self.decision_log[-48:]

        _LOGGER.info("CARMA decision: %s — %s", action, reason)

        # HA logbook entry for transparency (best-effort)
        self.hass.async_create_task(
            self._log_decision(reason),
            "carmabox_logbook_entry",
        )

    async def _log_decision(self, reason: str) -> None:
        """Log decision to system_log (best-effort, silently ignores missing service)."""
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

        # Record peak sample
        record_peak(self.savings, weighted_kw, baseline_kw)

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

    def _check_write_verify(self, ems_entity: str, expected_mode: str) -> bool:
        """Read back EMS mode and verify it matches expected. Returns True if OK."""
        actual = self._read_str(ems_entity)
        if actual != expected_mode:
            _LOGGER.error(
                "Write-verify FAILED: %s expected=%s actual=%s",
                ems_entity,
                expected_mode,
                actual,
            )
            self._daily_safety_blocks += 1
            return False
        return True

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
                mode = "battery_standby" if adapter.soc >= 100 else "charge_pv"
                ok = await adapter.set_ems_mode(mode)
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
                            "select", "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._daily_safety_blocks += 1
                success = False

        if success:
            self._last_command = BatteryCommand.CHARGE_PV
            self.safety.record_mode_change()

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
        else:
            # Legacy: raw entity-based control
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._get_entity(ems_key)
                if entity and await self._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": "battery_standby"}
                ):
                    if self.executor_enabled:
                        self._check_write_verify(entity, "battery_standby")
                    success = True

        if success:
            self._last_command = BatteryCommand.STANDBY
            self.safety.record_mode_change()

    async def _cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Set batteries to discharge at specified wattage.

        SafetyGuard: heartbeat + rate limit + discharge check.
        """
        # K1: Skip if already discharging at similar wattage
        if (
            self._last_command == BatteryCommand.DISCHARGE
            and hasattr(self, "_last_discharge_w")
            and abs(watts - self._last_discharge_w) < 100
        ):
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
            # Calculate energy-proportional split across adapters
            # Use SoC × capacity (kWh) so different-sized batteries
            # discharge proportional to stored energy, not just SoC %
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
            remaining_w = watts
            for idx, adapter in enumerate(self.inverter_adapters):
                if idx == len(self.inverter_adapters) - 1:
                    w = remaining_w  # Last adapter gets remainder
                else:
                    w = int(watts * stored[idx] / total_soc)
                    remaining_w -= w
                if w <= 0:
                    continue
                ems_ok = await adapter.set_ems_mode("discharge_battery")
                if not ems_ok:
                    failed = True
                    continue
                limit_ok = await adapter.set_discharge_limit(w)
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
                            "select", "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._daily_safety_blocks += 1
                success = False

            if success:
                self._last_command = BatteryCommand.DISCHARGE
                self._last_discharge_w = watts
                self.safety.record_mode_change()
