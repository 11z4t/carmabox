"""CARMA Box — Coordinator Bridge.

Part 1: State collection + command execution.
Part 2: Plan generation + persistent state.

Wraps CoordinatorV2 (pure Python) and connects it to Home Assistant:
1. Collects HA sensor state into SystemState
2. Runs V2 cycle
3. Executes CycleResult commands via adapters
4. Returns CarmaboxState for sensor.py compatibility
5. Generates energy plans via core/planner.py every 5 min
6. Persists state to .storage/carmabox_bridge
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .adapters.easee import EaseeAdapter
from .adapters.goodwe import GoodWeAdapter
from .adapters.nordpool import NordpoolAdapter
from .adapters.solcast import SolcastAdapter
from .adapters.tempest import TempestAdapter
from .const import (
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_CONSUMPTION_PROFILE,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_FALLBACK_PRICE_ORE,
    DEFAULT_TARGET_WEIGHTED_KW,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .core.coordinator_v2 import (
    CoordinatorConfig,
    CoordinatorV2,
    CycleResult,
    SystemState,
)
from .core.planner import (
    PlannerConfig,
    PlannerInput,
    build_price_schedule,
    generate_carma_plan,
)
from .optimizer.hourly_ledger import EnergyLedger
from .optimizer.models import (
    BreachCorrection,
    CarmaboxState,
    Decision,
    HourActual,
    HourlyMeterState,
    HourPlan,
    ShadowComparison,
)
from .optimizer.savings import SavingsState

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .adapters import EVAdapter, InverterAdapter

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "carmabox_bridge"
STORAGE_VERSION = 1


class CoordinatorBridge(DataUpdateCoordinator[CarmaboxState]):
    """Bridge between CoordinatorV2 (pure Python) and Home Assistant.

    Owns a CoordinatorV2 instance, collects state from HA sensors,
    runs the V2 cycle, and executes resulting commands via adapters.
    """

    # Class-level defaults for mock/spec compatibility (matches legacy)
    _taper_active: bool = False
    _cold_lock_active: bool = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize bridge coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="carmabox",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._cfg: dict[str, Any] = {**entry.data, **entry.options}

        # ── Feature flag: V2 cycle enable ──────────────────────
        # Shadow mode default: V2 runs but does NOT execute commands
        self._use_v2: bool = bool(self._cfg.get("use_coordinator_v2", True))

        # ── Build V2 config from HA config entry ───────────────
        v2_config = CoordinatorConfig(
            ellevio_tak_kw=float(self._cfg.get("ellevio_tak_kw", 2.0)),
            ellevio_night_weight=float(self._cfg.get("ellevio_night_weight", 0.5)),
            grid_guard_margin=float(self._cfg.get("grid_guard_margin", 0.85)),
            battery_1_kwh=float(self._cfg.get("battery_1_kwh", 15.0)),
            battery_2_kwh=float(self._cfg.get("battery_2_kwh", 5.0)),
            battery_min_soc=float(self._cfg.get("min_soc", DEFAULT_BATTERY_MIN_SOC)),
            battery_min_soc_cold=float(self._cfg.get("min_soc_cold", 20.0)),
            cold_lock_temp_c=float(self._cfg.get("cold_lock_temp_c", 4.0)),
            max_discharge_kw=float(self._cfg.get("max_discharge_kw", 5.0)),
            ev_phase_count=int(self._cfg.get("ev_phase_count", 3)),
            ev_min_amps=int(self._cfg.get("ev_min_amps", DEFAULT_EV_MIN_AMPS)),
            ev_max_amps=int(self._cfg.get("ev_max_amps", DEFAULT_EV_MAX_AMPS)),
            ev_target_soc=float(self._cfg.get("ev_target_soc", 75.0)),
            ev_departure_hour=int(self._cfg.get("ev_departure_hour", 6)),
            ev_capacity_kwh=float(self._cfg.get("ev_capacity_kwh", 92.0)),
            grid_charge_price_threshold=float(self._cfg.get("grid_charge_price_threshold", 15.0)),
        )
        self._v2 = CoordinatorV2(v2_config)

        # ── Inverter adapters ──────────────────────────────────
        self.inverter_adapters: list[InverterAdapter] = []
        for i in (1, 2):
            prefix = self._cfg.get(f"inverter_{i}_prefix", "")
            device_id = self._cfg.get(f"inverter_{i}_device_id", "")
            if prefix:
                self.inverter_adapters.append(GoodWeAdapter(hass, device_id, prefix))

        # ── EV adapter ─────────────────────────────────────────
        self.ev_adapter: EVAdapter | None = None
        if self._cfg.get("ev_enabled", False):
            ev_prefix = self._cfg.get("ev_prefix", "easee_home_12840")
            ev_device_id = self._cfg.get("ev_device_id", "")
            ev_charger_id = self._cfg.get("ev_charger_id", "")
            if ev_prefix:
                self.ev_adapter = EaseeAdapter(
                    hass, ev_device_id, str(ev_prefix), charger_id=ev_charger_id
                )

        # ── Weather adapter ────────────────────────────────────
        self.weather_adapter = None
        if self._cfg.get("weather_enabled", True):
            self.weather_adapter = TempestAdapter(hass)

        # ── Executor mode ──────────────────────────────────────
        config_executor = bool(self._cfg.get("executor_enabled", False))
        # TEMPORARY: dev/owner mode — no hub = all features
        hub_url = self._cfg.get("hub_url", "")
        if not hub_url:
            self.executor_enabled = config_executor
        else:
            self.executor_enabled = config_executor

        # Propagate dry_run to adapters
        for adapter in self.inverter_adapters:
            adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]
        if self.ev_adapter:
            self.ev_adapter._analyze_only = not self.executor_enabled  # type: ignore[attr-defined]

        # ── Miner entity ───────────────────────────────────────
        self._miner_entity: str = str(self._cfg.get("miner_entity", ""))

        # ── Target kW ──────────────────────────────────────────
        self.target_kw: float = float(
            self._cfg.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        )

        # ── Stub attributes for sensor.py compatibility ────────
        # These are populated by Part 2 (savings, ML, ledger, etc.)
        self.plan: list[HourPlan] = []
        self.savings: SavingsState = SavingsState(
            month=datetime.now().month, year=datetime.now().year
        )
        self.last_decision: Decision = Decision()
        self.decision_log: deque[Decision] = deque(maxlen=48)
        self.shadow: ShadowComparison = ShadowComparison()
        self.shadow_log: list[ShadowComparison] = []

        # PlanSummary-compatible attributes for sensor.py
        self.scheduler_plan = self  # alias: sensor.py uses coord.scheduler_plan.X
        self.target_weighted_kw: float = self.target_kw
        self.max_weighted_kw: float = 0.0
        self.total_charge_kwh: float = 0.0
        self.total_discharge_kwh: float = 0.0
        self.total_ev_kwh: float = 0.0
        self.estimated_cost_kr: float = 0.0
        self.ev_soc_at_06: int | None = None

        # Legacy compatibility stubs
        self.breaches: list = []
        self.breach_count_month: int = 0
        self.learnings: list = []
        self.idle_analysis = None
        self.ev_next_full_charge_date = None

        # ── Missing attributes for sensor.py (PLAT-1074) ──────
        self.hourly_actuals: list[HourActual] = []
        self.ledger: EnergyLedger = EnergyLedger()
        self.min_soc: float = float(self._cfg.get("min_soc", DEFAULT_BATTERY_MIN_SOC))
        self.benchmark_data: dict[str, Any] | None = None
        self._bat_daily_idle_seconds: int = 0
        self._daily_avg_price: float = float(
            self._cfg.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE)
        )
        self._ellevio_hour_samples: list[tuple[float, float]] = []
        self._ellevio_monthly_hourly_peaks: list[float] = []
        self._meter_state: HourlyMeterState = HourlyMeterState()
        self._shadow_savings_kr: float = 0.0
        self._breach_load_shed_active: bool = False
        self.appliance_power: dict[str, float] = {}
        self.appliance_energy_wh: dict[str, float] = {}
        self._appliances: list[dict[str, Any]] = list(self._cfg.get("appliances") or [])

        # Consecutive error tracking
        self._consecutive_errors: int = 0

        # ── Startup safety ─────────────────────────────────────
        self._startup_safety_confirmed: bool = False

        # ── Plan generation timing ───────────────────────────
        self._last_plan_time: float = 0.0  # monotonic timestamp
        self._plan_generated: bool = False

        # ── Persistent state store ────────────────────────────
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._state_restored: bool = False
        self._last_save_time: float = 0.0

        # ── Night EV state (persisted) ────────────────────────
        self.night_ev_active: bool = False
        self._last_command: str = "STANDBY"
        self._ev_enabled: bool = False
        self._ev_current_amps: int = DEFAULT_EV_MIN_AMPS

        _LOGGER.info(
            "CoordinatorBridge initialized: v2=%s, executor=%s, adapters=%d, ev=%s",
            self._use_v2,
            self.executor_enabled,
            len(self.inverter_adapters),
            self.ev_adapter is not None,
        )

    # ── Sensor read helpers ────────────────────────────────────

    def _read_float(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA entity with validation."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            val = float(state.state)
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

    # ── State collection ───────────────────────────────────────

    def _collect_system_state(self) -> SystemState:
        """Collect current HA sensor readings into V2 SystemState."""
        opts = self._cfg
        adapters = self.inverter_adapters
        a1 = adapters[0] if len(adapters) >= 1 else None
        a2 = adapters[1] if len(adapters) >= 2 else None
        ev = self.ev_adapter

        now = datetime.now()

        # Battery 1
        bat_soc_1 = a1.soc if a1 else self._read_float(opts.get("battery_soc_1", ""))
        bat_power_1 = a1.power_w if a1 else self._read_float(opts.get("battery_power_1", ""))
        bat_temp_1 = (a1.temperature_c if a1 else None) or 15.0
        ems_mode_1 = a1.ems_mode if a1 else self._read_str(opts.get("battery_ems_1", ""))
        fast_charging_1 = a1.fast_charging_on if a1 and isinstance(a1, GoodWeAdapter) else False

        # Battery 2
        bat_soc_2 = a2.soc if a2 else self._read_float(opts.get("battery_soc_2", ""), -1)
        bat_power_2 = a2.power_w if a2 else self._read_float(opts.get("battery_power_2", ""))
        bat_temp_2 = (a2.temperature_c if a2 else None) or 15.0
        ems_mode_2 = a2.ems_mode if a2 else self._read_str(opts.get("battery_ems_2", ""))
        fast_charging_2 = a2.fast_charging_on if a2 and isinstance(a2, GoodWeAdapter) else False

        # EV
        ev_soc = self._read_float(opts.get("ev_soc_entity", ""), -1)
        ev_power_w = ev.power_w if ev else self._read_float(opts.get("ev_power_entity", ""))
        ev_connected = ev.cable_locked if ev and isinstance(ev, EaseeAdapter) else False
        ev_enabled = ev.is_enabled if ev and isinstance(ev, EaseeAdapter) else False

        # Ellevio weighted average (from helper entity or default 0)
        ellevio_viktat_kw = self._read_float(opts.get("ellevio_viktat_entity", ""), 0.0)

        # Surplus consumers
        disk_power_w = self._read_float(opts.get("disk_power_entity", ""), 0.0)
        tvatt_power_w = self._read_float(opts.get("tvatt_power_entity", ""), 0.0)
        miner_power_w = 0.0
        if self._miner_entity:
            # Miner entity might be a switch; power comes from sensor variant
            miner_sensor = self._miner_entity.replace("switch.", "sensor.")
            if not miner_sensor.endswith("_power"):
                miner_sensor += "_power"
            miner_power_w = self._read_float(miner_sensor, 0.0)

        return SystemState(
            grid_import_w=self._read_float(opts.get("grid_entity", "sensor.house_grid_power")),
            ellevio_viktat_kw=ellevio_viktat_kw,
            pv_power_w=self._read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            battery_soc_1=bat_soc_1,
            battery_soc_2=bat_soc_2,
            battery_power_1=bat_power_1,
            battery_power_2=bat_power_2,
            battery_temp_1=bat_temp_1,
            battery_temp_2=bat_temp_2,
            ems_mode_1=ems_mode_1,
            ems_mode_2=ems_mode_2,
            fast_charging_1=fast_charging_1,
            fast_charging_2=fast_charging_2,
            ev_soc=ev_soc,
            ev_power_w=ev_power_w,
            ev_connected=ev_connected,
            ev_enabled=ev_enabled,
            current_price=self._read_float(opts.get("price_entity", "")),
            disk_power_w=disk_power_w,
            tvatt_power_w=tvatt_power_w,
            miner_power_w=miner_power_w,
            hour=now.hour,
            minute=now.minute,
        )

    def _collect_ha_state(self) -> CarmaboxState:
        """Collect current state into CarmaboxState for sensor.py."""
        opts = self._cfg
        adapters = self.inverter_adapters
        a1 = adapters[0] if len(adapters) >= 1 else None
        a2 = adapters[1] if len(adapters) >= 2 else None
        ev = self.ev_adapter

        return CarmaboxState(
            grid_power_w=self._read_float(opts.get("grid_entity", "sensor.house_grid_power")),
            battery_soc_1=(a1.soc if a1 else self._read_float(opts.get("battery_soc_1", ""))),
            battery_power_1=(
                a1.power_w if a1 else self._read_float(opts.get("battery_power_1", ""))
            ),
            battery_ems_1=(a1.ems_mode if a1 else self._read_str(opts.get("battery_ems_1", ""))),
            battery_cap_1_kwh=float(opts.get("battery_1_kwh", 15.0)),
            battery_soc_2=(a2.soc if a2 else self._read_float(opts.get("battery_soc_2", ""), -1)),
            battery_power_2=(
                a2.power_w if a2 else self._read_float(opts.get("battery_power_2", ""))
            ),
            battery_ems_2=(a2.ems_mode if a2 else self._read_str(opts.get("battery_ems_2", ""))),
            battery_cap_2_kwh=float(opts.get("battery_2_kwh", 5.0)),
            pv_power_w=self._read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            ev_soc=self._read_float(opts.get("ev_soc_entity", ""), -1),
            ev_power_w=(ev.power_w if ev else self._read_float(opts.get("ev_power_entity", ""))),
            ev_current_a=(
                ev.current_a if ev else self._read_float(opts.get("ev_current_entity", ""))
            ),
            ev_status=(ev.status if ev else self._read_str(opts.get("ev_status_entity", ""))),
            battery_min_cell_temp_1=a1.temperature_c if a1 else None,
            battery_min_cell_temp_2=a2.temperature_c if a2 else None,
            current_price=self._read_float(opts.get("price_entity", "")),
            target_weighted_kw=self.target_kw,
            plan=self.plan,
        )

    # ── Command execution ──────────────────────────────────────

    async def _execute_battery_commands(self, commands: list[dict]) -> None:
        """Execute battery commands from CycleResult via GoodWeAdapters."""
        for cmd in commands:
            bat_id: int = cmd.get("id", 0)
            if bat_id >= len(self.inverter_adapters):
                _LOGGER.warning(
                    "Battery command for id=%d but only %d adapters",
                    bat_id,
                    len(self.inverter_adapters),
                )
                continue

            adapter = self.inverter_adapters[bat_id]
            mode: str = cmd.get("mode", "")
            power_limit: int = int(cmd.get("power_limit", 0))
            fast_charging: bool = cmd.get("fast_charging", False)

            # Set EMS mode
            if mode:
                ok = await adapter.set_ems_mode(mode)
                if not ok:
                    _LOGGER.error(
                        "Failed to set EMS mode %s on adapter %d",
                        mode,
                        bat_id,
                    )

            # Set discharge limit if mode implies discharge
            if mode == "discharge_pv" and power_limit > 0:
                ok = await adapter.set_discharge_limit(power_limit)
                if not ok:
                    _LOGGER.error(
                        "Failed to set discharge limit %dW on adapter %d",
                        power_limit,
                        bat_id,
                    )

            # Handle fast charging (GoodWe-specific)
            if isinstance(adapter, GoodWeAdapter) and fast_charging != adapter.fast_charging_on:
                ok = await adapter.set_fast_charging(
                    on=fast_charging,
                    authorized=fast_charging,
                )
                if not ok:
                    _LOGGER.error(
                        "Failed to set fast_charging=%s on adapter %d",
                        fast_charging,
                        bat_id,
                    )

    async def _execute_ev_command(self, ev_cmd: dict | None) -> None:
        """Execute EV command from CycleResult via EaseeAdapter."""
        if ev_cmd is None or self.ev_adapter is None:
            return

        action = ev_cmd.get("action", "")
        amps = int(ev_cmd.get("amps", DEFAULT_EV_MIN_AMPS))

        try:
            if action == "start":
                await self.ev_adapter.enable()
                if amps > 0:
                    await self.ev_adapter.set_current(amps)
                _LOGGER.info("EV command: start at %dA", amps)
            elif action == "stop":
                await self.ev_adapter.disable()
                _LOGGER.info("EV command: stop")
            elif action == "set_current":
                await self.ev_adapter.set_current(amps)
                _LOGGER.info("EV command: set current %dA", amps)
        except Exception:
            _LOGGER.exception("EV command failed: %s", ev_cmd)

    async def _execute_surplus_actions(self, actions: list[dict]) -> None:
        """Execute surplus chain actions via HA service calls."""
        for action in actions:
            action_id: str = action.get("id", "")
            action_type: str = action.get("action", "none")

            if action_type == "none":
                continue

            # Map surplus consumer IDs to HA entities
            entity_id = ""
            if action_id == "miner" and self._miner_entity:
                entity_id = self._miner_entity

            if not entity_id:
                _LOGGER.debug("No entity mapping for surplus action %s", action_id)
                continue

            try:
                domain = entity_id.split(".")[0]
                if action_type == "on":
                    await self.hass.services.async_call(domain, "turn_on", {"entity_id": entity_id})
                    _LOGGER.info("Surplus: turned ON %s", entity_id)
                elif action_type == "off":
                    await self.hass.services.async_call(
                        domain, "turn_off", {"entity_id": entity_id}
                    )
                    _LOGGER.info("Surplus: turned OFF %s", entity_id)
            except Exception:
                _LOGGER.exception("Surplus action failed: %s %s", action_type, entity_id)

    # ── Plan generation ──────────────────────────────────────

    async def _generate_plan(self) -> None:
        """Generate energy plan from Nordpool prices + Solcast PV + consumption.

        Called every PLAN_INTERVAL_SECONDS (5 min) from _async_update_data.
        Uses generate_carma_plan() from core/planner.py.
        """
        try:
            now = datetime.now()
            start_hour = now.hour
            opts = self._cfg

            # ── Prices from Nordpool ──────────────────────────
            price_entity = opts.get("price_entity", "")
            fallback_price = float(opts.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE))
            nordpool = NordpoolAdapter(self.hass, price_entity, fallback_price)
            today_prices = nordpool.today_prices
            tomorrow_prices = nordpool.tomorrow_prices or []

            prices = build_price_schedule(
                today_prices=today_prices,
                tomorrow_prices=tomorrow_prices,
                current_hour=start_hour,
                plan_hours=24,
            )

            # ── PV forecast from Solcast ──────────────────────
            solcast = SolcastAdapter(self.hass)
            pv_today = solcast.today_hourly_kw
            pv_tomorrow = solcast.tomorrow_hourly_kw
            pv_forecast = pv_today[start_hour:] + pv_tomorrow
            # Pad/trim to match prices length
            while len(pv_forecast) < len(prices):
                pv_forecast.append(0.0)
            pv_forecast = pv_forecast[: len(prices)]

            # ── Consumption profile (static default) ──────────
            consumption = list(DEFAULT_CONSUMPTION_PROFILE)
            consumption = consumption[start_hour:] + consumption
            while len(consumption) < len(prices):
                consumption.extend(DEFAULT_CONSUMPTION_PROFILE)
            consumption = consumption[: len(prices)]

            # ── Battery state ─────────────────────────────────
            bat1_kwh = float(opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH))
            bat2_kwh = float(opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH))
            total_bat_kwh = bat1_kwh + bat2_kwh

            adapters = self.inverter_adapters
            a1 = adapters[0] if len(adapters) >= 1 else None
            a2 = adapters[1] if len(adapters) >= 2 else None

            bat_soc_1 = a1.soc if a1 else self._read_float(opts.get("battery_soc_1", ""))
            bat_soc_2 = a2.soc if a2 else self._read_float(opts.get("battery_soc_2", ""), -1)
            bat_soc_2_safe = max(0, bat_soc_2)

            # Weighted average SoC
            if total_bat_kwh > 0:
                weighted_soc = (bat_soc_1 * bat1_kwh + bat_soc_2_safe * bat2_kwh) / total_bat_kwh
            else:
                weighted_soc = bat_soc_1

            # Battery temperatures
            bat_temps: list[float] = []
            if a1 and a1.temperature_c is not None:
                bat_temps.append(a1.temperature_c)
            if a2 and a2.temperature_c is not None:
                bat_temps.append(a2.temperature_c)

            # ── EV state ──────────────────────────────────────
            ev_soc = self._read_float(opts.get("ev_soc_entity", ""), -1)
            ev_cap_kwh = float(opts.get("ev_capacity_kwh", 92.0))

            # ── PV tomorrow total ─────────────────────────────
            pv_tomorrow_kwh = solcast.tomorrow_kwh

            # ── Build PlannerInput ────────────────────────────
            planner_input = PlannerInput(
                start_hour=start_hour,
                hourly_prices=prices,
                hourly_pv=pv_forecast,
                hourly_loads=consumption,
                hourly_ev=[0.0] * len(prices),  # EV handled by V2 cycle
                battery_soc=weighted_soc,
                battery_cap_kwh=total_bat_kwh,
                ev_soc=ev_soc,
                ev_cap_kwh=ev_cap_kwh,
                pv_forecast_tomorrow_kwh=pv_tomorrow_kwh,
                battery_temps=bat_temps if bat_temps else None,
            )

            # ── Build PlannerConfig from coordinator config ───
            planner_config = PlannerConfig(
                ellevio_tak_kw=float(opts.get("ellevio_tak_kw", 2.0)),
                ellevio_night_weight=float(opts.get("ellevio_night_weight", 0.5)),
                grid_guard_margin=float(opts.get("grid_guard_margin", 0.85)),
                battery_min_soc=float(opts.get("min_soc", DEFAULT_BATTERY_MIN_SOC)),
                battery_min_soc_cold=float(opts.get("min_soc_cold", 20.0)),
                cold_temp_c=float(opts.get("cold_lock_temp_c", 4.0)),
                grid_charge_price_threshold=float(opts.get("grid_charge_price_threshold", 15.0)),
                max_discharge_kw=float(opts.get("max_discharge_kw", 5.0)),
            )

            # ── Generate plan ─────────────────────────────────
            plan_actions = generate_carma_plan(planner_input, planner_config)

            # ── Convert PlanAction → HourPlan for sensor.py ───
            self.plan = [
                HourPlan(
                    hour=pa.hour,
                    action=pa.action,
                    battery_kw=pa.battery_kw,
                    grid_kw=pa.grid_kw,
                    weighted_kw=0.0,
                    pv_kw=pv_forecast[i] if i < len(pv_forecast) else 0.0,
                    consumption_kw=consumption[i] if i < len(consumption) else 0.0,
                    ev_kw=0.0,
                    ev_soc=pa.ev_soc,
                    battery_soc=pa.battery_soc,
                    price=pa.price,
                )
                for i, pa in enumerate(plan_actions)
            ]

            # ── Update plan summary attributes ────────────────
            self.total_charge_kwh = sum(hp.battery_kw for hp in self.plan if hp.battery_kw > 0)
            self.total_discharge_kwh = sum(
                abs(hp.battery_kw) for hp in self.plan if hp.battery_kw < 0
            )
            self.max_weighted_kw = max(
                (hp.grid_kw for hp in self.plan),
                default=0.0,
            )
            self.estimated_cost_kr = sum(
                hp.grid_kw * hp.price / 100 for hp in self.plan if hp.grid_kw > 0
            )

            self._plan_generated = True
            _LOGGER.debug(
                "Plan generated: %d hours, charge=%.1f kWh, discharge=%.1f kWh",
                len(self.plan),
                self.total_charge_kwh,
                self.total_discharge_kwh,
            )

        except Exception:
            _LOGGER.exception("Plan generation failed, keeping previous plan")

    # ── Persistent state ──────────────────────────────────────

    async def _async_save_state(self) -> None:
        """Save coordinator state to .storage/carmabox_bridge.

        Rate-limited: saves at most every PLAN_INTERVAL_SECONDS.
        """
        now = time.monotonic()
        if now - self._last_save_time < PLAN_INTERVAL_SECONDS:
            return
        self._last_save_time = now

        try:
            plan_data = [
                {
                    "hour": hp.hour,
                    "action": hp.action,
                    "battery_kw": hp.battery_kw,
                    "grid_kw": hp.grid_kw,
                    "weighted_kw": hp.weighted_kw,
                    "pv_kw": hp.pv_kw,
                    "consumption_kw": hp.consumption_kw,
                    "ev_kw": hp.ev_kw,
                    "ev_soc": hp.ev_soc,
                    "battery_soc": hp.battery_soc,
                    "price": hp.price,
                }
                for hp in self.plan
            ]
            state = {
                "plan": plan_data,
                "night_ev_active": self.night_ev_active,
                "last_command": self._last_command,
                "ev_enabled": self._ev_enabled,
                "ev_current_amps": self._ev_current_amps,
                "saved_at": datetime.now().isoformat(),
            }
            await self._store.async_save(state)
            _LOGGER.debug("State saved to storage")
        except Exception:
            _LOGGER.debug("Failed to save state", exc_info=True)

    async def _async_restore_state(self) -> None:
        """Restore coordinator state from .storage/carmabox_bridge on startup."""
        if self._state_restored:
            return
        self._state_restored = True

        try:
            data = await self._store.async_load()
            if not data or not isinstance(data, dict):
                _LOGGER.info("No stored bridge state found, starting fresh")
                return

            # Restore plan
            plan_data = data.get("plan", [])
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

            # Restore EV/command state
            self.night_ev_active = bool(data.get("night_ev_active", False))
            self._last_command = str(data.get("last_command", "STANDBY"))
            self._ev_enabled = bool(data.get("ev_enabled", False))
            self._ev_current_amps = int(data.get("ev_current_amps", DEFAULT_EV_MIN_AMPS))

            _LOGGER.info(
                "Restored bridge state: %d plan hours, last_cmd=%s, ev=%s",
                len(self.plan),
                self._last_command,
                self._ev_enabled,
            )
        except Exception:
            _LOGGER.warning("Failed to restore bridge state, starting fresh", exc_info=True)

    # ── Main update loop ───────────────────────────────────────

    async def _async_update_data(self) -> CarmaboxState:
        """Fetch data, run V2 cycle, execute commands, return state."""
        try:
            # ── Restore state on first run ─────────────────────
            if not self._state_restored:
                await self._async_restore_state()

            # ── Startup safety: ensure fast_charging OFF ───────
            if not self._startup_safety_confirmed:
                all_off = True
                for adapter in self.inverter_adapters:
                    try:
                        if isinstance(adapter, GoodWeAdapter) and adapter.fast_charging_on:
                            _LOGGER.warning(
                                "STARTUP SAFETY: %s fast_charging=ON -> turning off",
                                adapter.prefix,
                            )
                            await adapter.set_fast_charging(on=False)
                            await adapter.set_ems_mode("battery_standby")
                            all_off = False
                        elif isinstance(adapter, GoodWeAdapter):
                            # Check if sensor is ready
                            fc_entity = f"switch.goodwe_fast_charging_switch_{adapter.prefix}"
                            fc_state = self.hass.states.get(fc_entity)
                            if fc_state is None:
                                all_off = False
                    except Exception:
                        _LOGGER.error(
                            "STARTUP SAFETY: adapter %s not ready",
                            getattr(adapter, "prefix", "?"),
                        )
                        all_off = False
                if all_off:
                    self._startup_safety_confirmed = True
                    _LOGGER.info("STARTUP SAFETY: confirmed all fast_charging OFF")

            # ── Collect HA state ───────────────────────────────
            ha_state = self._collect_ha_state()

            # ── Run V2 cycle if enabled ────────────────────────
            if self._use_v2:
                try:
                    sys_state = self._collect_system_state()
                    result: CycleResult = self._v2.cycle(sys_state)

                    # Execute commands (or log in shadow mode)
                    if self.executor_enabled:
                        await self._execute_battery_commands(result.battery_commands)
                        await self._execute_ev_command(result.ev_command)
                        await self._execute_surplus_actions(result.surplus_actions)
                    else:
                        # Shadow mode: log what WOULD happen
                        if result.battery_commands or result.ev_command or result.surplus_actions:
                            _LOGGER.info(
                                "SHADOW: V2 would: bat=%s, ev=%s, surplus=%s, reason=%s",
                                [c.get("mode", "?") for c in result.battery_commands]
                                if result.battery_commands
                                else "none",
                                result.ev_command.get("action", "none")
                                if result.ev_command
                                else "none",
                                [a.get("action", "?") for a in result.surplus_actions]
                                if result.surplus_actions
                                else "none",
                                result.reason[:80] if result.reason else "",
                            )

                    # Update decision log
                    self.last_decision = Decision(
                        timestamp=datetime.now().isoformat(),
                        action=result.plan_action,
                        reason=result.reason,
                        target_kw=self.target_kw,
                        grid_kw=ha_state.grid_power_w / 1000,
                        battery_soc=ha_state.battery_soc_1,
                        ev_soc=ha_state.ev_soc,
                        pv_kw=ha_state.pv_power_w / 1000,
                        price_ore=ha_state.current_price,
                    )
                    self.decision_log.append(self.last_decision)

                    # Sync V2 plan to bridge plan for sensor.py
                    self.plan = [
                        HourPlan(
                            hour=p.hour,
                            action=p.action,
                            battery_kw=p.battery_kw,
                            grid_kw=p.grid_kw,
                            weighted_kw=0.0,
                            pv_kw=0.0,
                            consumption_kw=0.0,
                            ev_kw=0.0,
                            ev_soc=p.ev_soc,
                            battery_soc=p.battery_soc,
                            price=p.price,
                        )
                        for p in self._v2.plan
                    ]
                    ha_state.plan = self.plan

                    _LOGGER.debug(
                        "V2 cycle: guard=%s, action=%s, bat_cmds=%d, ev=%s",
                        result.grid_guard_status,
                        result.plan_action,
                        len(result.battery_commands),
                        result.ev_command,
                    )
                except Exception:
                    _LOGGER.exception("V2 cycle failed, returning state without execution")

            # ── Plan generation (every PLAN_INTERVAL_SECONDS) ──
            now_mono = time.monotonic()
            if now_mono - self._last_plan_time >= PLAN_INTERVAL_SECONDS:
                self._last_plan_time = now_mono
                await self._generate_plan()
                ha_state.plan = self.plan

                # Pass plan to V2 coordinator if available
                if self._use_v2 and self.plan:
                    self._v2.plan = [
                        type(
                            "PlanSlot",
                            (),
                            {
                                "hour": hp.hour,
                                "action": hp.action,
                                "battery_kw": hp.battery_kw,
                                "grid_kw": hp.grid_kw,
                                "price": hp.price,
                                "battery_soc": hp.battery_soc,
                                "ev_soc": hp.ev_soc,
                            },
                        )()
                        for hp in self.plan
                    ]

            # ── Persist state ──────────────────────────────────
            await self._async_save_state()

            self._consecutive_errors = 0
            return ha_state

        except Exception as err:
            self._consecutive_errors += 1
            _LOGGER.exception("Update failed (%d consecutive)", self._consecutive_errors)
            if self._consecutive_errors >= 5:
                raise UpdateFailed(f"Too many consecutive errors: {err}") from err
            # Return last known state or empty state
            if self.data is not None:
                return self.data
            return CarmaboxState()

    # ── Properties for sensor.py compatibility ─────────────────

    async def on_ev_cable_connected(self) -> None:
        """Handle EV cable plug-in event."""
        _LOGGER.info("BRIDGE: EV cable connected — V2 will handle in next cycle")
        # V2 handles EV in its cycle — no immediate action needed in shadow mode

    @property
    def cable_locked_entity(self) -> str:
        """Entity ID for EV cable locked sensor."""
        ev_prefix = self._cfg.get("ev_prefix", "")
        if ev_prefix:
            return f"binary_sensor.{ev_prefix}_plug"
        return ""

    @property
    def system_health(self) -> dict[str, str]:
        """System health for transparency sensor."""
        health: dict[str, str] = {}

        for i, adapter in enumerate(self.inverter_adapters, 1):
            name = getattr(adapter, "prefix", f"inverter_{i}")
            if isinstance(adapter, GoodWeAdapter):
                ems_entity = f"select.goodwe_{name}_ems_mode"
                ems_state = self.hass.states.get(ems_entity)
                if ems_state is None or ems_state.state in ("unavailable", "unknown"):
                    health[name] = "offline"
                elif adapter.soc < 0:
                    health[name] = "ingen data"
                else:
                    health[name] = "ok"

        if self.ev_adapter and isinstance(self.ev_adapter, EaseeAdapter):
            if self.ev_adapter.status in ("", "unavailable", "unknown"):
                health["ev"] = "offline"
            elif self.ev_adapter.cable_locked:
                health["ev"] = "laddar" if self.ev_adapter.is_charging else "ansluten"
            else:
                health["ev"] = "ej ansluten"

        health["styrning"] = "ok"
        health["sakerhet"] = "ok"

        return health

    @property
    def status_text(self) -> str:
        """User-friendly one-liner status."""
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
        if not issues:
            return "Allt fungerar"
        return ", ".join(issues)

    @property
    def hourly_meter_projected(self) -> float:
        """Projected hourly weighted kW average."""
        return self._meter_state.projected_avg

    @property
    def hourly_meter_pct(self) -> float:
        """Projected hourly meter as % of target."""
        if self.target_kw <= 0:
            return 0.0
        return round(self._meter_state.projected_avg / self.target_kw * 100, 1)

    @property
    def breach_monitor_active(self) -> bool:
        """Whether breach load shedding is active."""
        return self._breach_load_shed_active

    @property
    def daily_insight(self) -> dict[str, Any]:
        """Daily energy insight summary (stub)."""
        return {"status": "no_data"}

    def plan_score(self) -> dict[str, Any]:
        """Score how well plans matched actual outcomes (stub)."""
        return {
            "score_today": None,
            "score_7d": None,
            "score_30d": None,
            "trend": "stable",
        }

    def get_active_corrections(self, hour: int | None = None) -> list[BreachCorrection]:
        """Get active breach corrections (stub)."""
        return []


# Alias for sensor.py compatibility if needed
CarmaboxCoordinator = CoordinatorBridge
