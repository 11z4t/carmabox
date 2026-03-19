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
from datetime import datetime, timedelta
from enum import Enum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
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
    DEFAULT_CONSUMPTION_PROFILE,
    DEFAULT_DAILY_BATTERY_NEED_KWH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_FALLBACK_PRICE_ORE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_MAX_GRID_CHARGE_KW,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_TARGET_WEIGHTED_KW,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .optimizer.ev_strategy import calculate_ev_schedule
from .optimizer.grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .optimizer.models import CarmaboxState, HourPlan
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
    record_discharge,
    record_grid_charge,
    record_peak,
    reset_if_new_month,
)
from .repairs import (
    SAFETY_BLOCK_THRESHOLD,
    clear_issue,
    raise_hub_offline_issue,
    raise_safety_guard_issue,
)

_LOGGER = logging.getLogger(__name__)


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
        self.safety = SafetyGuard(
            min_soc=entry.options.get("min_soc", DEFAULT_BATTERY_MIN_SOC),
        )
        self.plan: list[HourPlan] = []
        self._plan_counter = 0
        self._last_command = BatteryCommand.IDLE

        # ── Inverter adapters ─────────────────────────────────
        self.inverter_adapters: list[InverterAdapter] = []
        opts = entry.options
        for i in (1, 2):
            prefix = opts.get(f"inverter_{i}_prefix", "")
            device_id = opts.get(f"inverter_{i}_device_id", "")
            if prefix:
                self.inverter_adapters.append(GoodWeAdapter(hass, device_id, prefix))

        # ── EV adapter ────────────────────────────────────────
        self.ev_adapter: EVAdapter | None = None
        if opts.get("ev_enabled", False):
            ev_prefix = opts.get("ev_prefix", "easee_home_12840")
            ev_device_id = opts.get("ev_device_id", "")
            if ev_prefix:
                self.ev_adapter = EaseeAdapter(hass, ev_device_id, str(ev_prefix))

        self.target_kw: float = opts.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        self.min_soc: float = opts.get("min_soc", DEFAULT_BATTERY_MIN_SOC)
        init_now = datetime.now()
        self.savings = SavingsState(month=init_now.month, year=init_now.year)
        self.report_collector = ReportCollector(month=init_now.month, year=init_now.year)
        self._daily_discharge_kwh = 0.0
        self._daily_safety_blocks = 0
        self._daily_plans = 0
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        self._daily_avg_price: float = float(
            opts.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE)
        )
        self._avg_price_initialized = False

    def _get_entity(self, key: str, default: str = "") -> str:
        """Get entity_id from config options."""
        return str(self.entry.options.get(key, default))

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

    async def _async_update_data(self) -> CarmaboxState:
        """Fetch data, run optimizer, execute plan."""
        try:
            now = datetime.now()
            self.savings = reset_if_new_month(self.savings, now)
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
            self._track_savings(state)
            return state

        except Exception as err:
            _LOGGER.error("CARMA Box update failed: %s", err, exc_info=True)
            raise UpdateFailed(f"Update failed: {err}") from err

    def _collect_state(self) -> CarmaboxState:
        """Collect current state from all HA entities.

        Uses inverter/EV adapters when configured, falls back to raw entity reads.
        """
        opts = self.entry.options
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
            battery_soc_2=battery_soc_2,
            battery_power_2=battery_power_2,
            battery_ems_2=battery_ems_2,
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
            fallback_price = float(
                self.entry.options.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE)
            )
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
            base = list(DEFAULT_CONSUMPTION_PROFILE)
            consumption = base[start_hour:] + base

            # EV demand — dynamic schedule based on prices + SoC
            opts = self.entry.options
            ev_enabled = opts.get("ev_enabled", False)
            ev_capacity = float(opts.get("ev_capacity_kwh", 98))
            ev_morning_target = float(opts.get("ev_night_target_soc", 75))
            ev_full_days = int(opts.get("ev_full_charge_days", 7))

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
                )
            else:
                ev_demand = [0.0] * len(prices)

            # Calculate target from PV forecast + reserve
            bat1_kwh = float(opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH))
            bat2_kwh = float(opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH))
            total_bat_kwh = bat1_kwh + bat2_kwh
            daily_consumption = float(
                opts.get("daily_consumption_kwh", DEFAULT_DAILY_CONSUMPTION_KWH)
            )
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

        # ── RULE 1: Never discharge during export ────────────
        if state.is_exporting:
            if not state.all_batteries_full:
                charge_result = self.safety.check_charge(
                    state.battery_soc_1, state.battery_soc_2, temp_c
                )
                if charge_result.ok:
                    await self._cmd_charge_pv(state)
                else:
                    _LOGGER.info("SafetyGuard blocked charge: %s", charge_result.reason)
                    self._daily_safety_blocks += 1
            else:
                await self._cmd_standby(state)
            return

        # ── RULE 2: SoC 100% → standby ──────────────────────
        if state.all_batteries_full:
            await self._cmd_standby(state)
            return

        # ── RULE 3: Load > target → discharge ────────────────
        hour = datetime.now().hour
        night_weight = float(self.entry.options.get("night_weight", DEFAULT_NIGHT_WEIGHT))
        weight = ellevio_weight(hour, night_weight=night_weight)
        # Net load = grid import + EV charging - PV production
        net_w = max(0, state.grid_power_w + state.ev_power_w - state.pv_power_w)
        weighted_net = net_w * weight
        target_w = self.target_kw * 1000

        if weighted_net > target_w:
            discharge_w = int((weighted_net - target_w) / weight)
            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                self.min_soc,
                state.grid_power_w,
                temp_c,
            )
            if result.ok:
                await self._cmd_discharge(state, discharge_w)
            else:
                _LOGGER.info("SafetyGuard blocked: %s", result.reason)
                self._daily_safety_blocks += 1
            return

        # ── RULE 4: Under target → idle ──────────────────────

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
            self._current_date = today
            self._update_daily_avg_price()

    def _update_daily_avg_price(self) -> None:
        """Calculate daily average price from Nordpool today_prices."""
        price_entity = self._get_entity("price_entity", "")
        if not price_entity:
            return
        fallback = float(self.entry.options.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
        adapter = NordpoolAdapter(self.hass, price_entity, fallback)
        prices = adapter.today_prices
        if prices and not all(p == fallback for p in prices):
            self._daily_avg_price = sum(prices) / len(prices)

    def _track_savings(self, state: CarmaboxState) -> None:
        """Track savings data from current state."""
        hour = datetime.now().hour
        night_weight = float(self.entry.options.get("night_weight", DEFAULT_NIGHT_WEIGHT))
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

        # Record daily sample for monthly report
        today = datetime.now().strftime("%Y-%m-%d")
        sample = DailySample(
            date=today,
            peak_kw=weighted_kw,
            baseline_peak_kw=baseline_kw,
            discharge_kwh=self._daily_discharge_kwh,
            safety_blocks=self._daily_safety_blocks,
            plans_generated=self._daily_plans,
        )
        record_daily_sample(self.report_collector, sample)

    async def _safe_service_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with error handling and retry. Returns True on success.

        - Catches ServiceNotFound, HomeAssistantError specifically
        - Max 1 retry with 5s delay on failure
        - Does NOT set _last_command on failure (caller handles that)
        """
        entity_id = data.get("entity_id", "?")
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

        if self.inverter_adapters:
            for adapter in self.inverter_adapters:
                mode = "battery_standby" if adapter.soc >= 100 else "charge_pv"
                ok = await adapter.set_ems_mode(mode)
                if ok:
                    if adapter.ems_mode != mode:
                        _LOGGER.error(
                            "Write-verify FAILED: expected=%s actual=%s", mode, adapter.ems_mode
                        )
                        self._daily_safety_blocks += 1
                    success = True
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
                    self._check_write_verify(entity, mode)
                    success = True

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
                    if adapter.ems_mode != "battery_standby":
                        _LOGGER.error(
                            "Write-verify FAILED: expected=battery_standby actual=%s",
                            adapter.ems_mode,
                        )
                        self._daily_safety_blocks += 1
                    success = True
        else:
            # Legacy: raw entity-based control
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._get_entity(ems_key)
                if entity and await self._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": "battery_standby"}
                ):
                    self._check_write_verify(entity, "battery_standby")
                    success = True

        if success:
            self._last_command = BatteryCommand.STANDBY
            self.safety.record_mode_change()

    async def _cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Set batteries to discharge at specified wattage.

        SafetyGuard: heartbeat + rate limit + discharge check.
        """
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
            # Calculate SoC-proportional split across adapters
            socs = [max(0, a.soc) for a in self.inverter_adapters]
            total_soc = sum(socs)
            if total_soc <= 0:
                return

            success = False
            remaining_w = watts
            for idx, adapter in enumerate(self.inverter_adapters):
                if idx == len(self.inverter_adapters) - 1:
                    w = remaining_w  # Last adapter gets remainder
                else:
                    w = int(watts * socs[idx] / total_soc)
                    remaining_w -= w
                if w <= 0:
                    continue
                ems_ok = await adapter.set_ems_mode("discharge_battery")
                if not ems_ok:
                    # Fail-safe: do NOT set discharge limit if EMS mode failed
                    continue
                if adapter.ems_mode != "discharge_battery":
                    _LOGGER.error(
                        "Write-verify FAILED: expected=discharge_battery actual=%s",
                        adapter.ems_mode,
                    )
                    self._daily_safety_blocks += 1
                await adapter.set_discharge_limit(w)
                success = True

            if success:
                self._last_command = BatteryCommand.DISCHARGE
                self.safety.record_mode_change()
        else:
            # Legacy: raw entity-based control
            total_soc = state.battery_soc_1 + max(0, state.battery_soc_2)
            if total_soc <= 0:
                return

            ratio_1 = state.battery_soc_1 / total_soc
            w1 = int(watts * ratio_1)
            w2 = watts - w1

            success = False
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
                        continue
                    self._check_write_verify(ems_entity, "discharge_battery")
                    if limit_entity:
                        await self._safe_service_call(
                            "number", "set_value", {"entity_id": limit_entity, "value": w}
                        )
                        success = True

            if success:
                self._last_command = BatteryCommand.DISCHARGE
                self.safety.record_mode_change()
