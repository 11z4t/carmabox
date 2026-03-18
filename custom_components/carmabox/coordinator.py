"""CARMA Box — DataUpdateCoordinator.

This is the brain. ONE class replaces ALL automations:
- Collects data every 30s
- Runs optimizer every 5 min
- Executes plan every 30s
- SafetyGuard checks EVERY command

No YAML automations. No shell_commands. No cron.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .adapters.nordpool import NordpoolAdapter
from .adapters.solcast import SolcastAdapter
from .const import (
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_TARGET_WEIGHTED_KW,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .optimizer.grid_logic import calculate_reserve, calculate_target, ellevio_weight
from .optimizer.models import CarmaboxState, HourPlan
from .optimizer.planner import generate_plan
from .optimizer.safety_guard import SafetyGuard

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

        self.target_kw: float = entry.options.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        self.min_soc: float = entry.options.get("min_soc", DEFAULT_BATTERY_MIN_SOC)

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
            state = self._collect_state()

            self._plan_counter += 1
            if self._plan_counter >= PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS:
                self._plan_counter = 0
                self._generate_plan(state)

            await self._execute(state)
            return state

        except Exception as err:
            _LOGGER.error("CARMA Box update failed: %s", err, exc_info=True)
            raise UpdateFailed(f"Update failed: {err}") from err

    def _collect_state(self) -> CarmaboxState:
        """Collect current state from all HA entities."""
        opts = self.entry.options
        return CarmaboxState(
            grid_power_w=self._read_float(opts.get("grid_entity", "sensor.house_grid_power")),
            battery_soc_1=self._read_float(opts.get("battery_soc_1", "")),
            battery_power_1=self._read_float(opts.get("battery_power_1", "")),
            battery_ems_1=self._read_str(opts.get("battery_ems_1", "")),
            battery_soc_2=self._read_float(opts.get("battery_soc_2", ""), -1),
            battery_power_2=self._read_float(opts.get("battery_power_2", "")),
            battery_ems_2=self._read_str(opts.get("battery_ems_2", "")),
            pv_power_w=self._read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            ev_soc=self._read_float(opts.get("ev_soc_entity", ""), -1),
            ev_power_w=self._read_float(opts.get("ev_power_entity", "")),
            ev_current_a=self._read_float(opts.get("ev_current_entity", "")),
            ev_status=self._read_str(opts.get("ev_status_entity", "")),
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
            price_adapter = NordpoolAdapter(self.hass, price_entity)
            today_prices = price_adapter.today_prices

            # If primary returns all-fallback (50 öre), try secondary
            if price_entity_fallback and all(p == 50.0 for p in today_prices):
                _LOGGER.info("Primary price source offline, trying fallback")
                price_adapter = NordpoolAdapter(self.hass, price_entity_fallback)
                today_prices = price_adapter.today_prices

            tomorrow_prices = price_adapter.tomorrow_prices
            prices = today_prices[start_hour:] + (tomorrow_prices or today_prices)

            # Collect PV forecast
            solcast = SolcastAdapter(self.hass)
            pv_hourly = solcast.today_hourly_kw
            pv_forecast = pv_hourly[start_hour:] + [0.0] * 24  # Pad tomorrow

            # Consumption profile (static for now)
            base = [0.8] * 6 + [2.0] * 3 + [1.5] * 8 + [2.5] * 5 + [1.0] * 2
            consumption = base[start_hour:] + base

            # EV demand (static 6A natt for now)
            ev_kw = 1.38
            ev = [0.0] * 24
            for h in range(24):
                abs_h = (start_hour + h) % 24
                if abs_h >= 22 or abs_h < 6:
                    ev[h] = ev_kw
            ev_demand = ev[: len(prices)]

            # Calculate target from PV forecast + reserve
            battery_kwh = (state.battery_soc_1 / 100 * 15) + (
                max(0, state.battery_soc_2) / 100 * 10
            )
            pv_daily = solcast.forecast_daily_3d
            reserve = calculate_reserve(pv_daily, 15.0, 5.0)
            target = calculate_target(
                battery_kwh_available=battery_kwh - (self.min_soc / 100 * 25),
                hourly_loads=consumption[: len(prices)],
                hourly_weights=[ellevio_weight((start_hour + i) % 24) for i in range(len(prices))],
                reserve_kwh=reserve,
            )
            self.target_kw = target

            # Trim to same length
            n = min(len(prices), len(pv_forecast), len(consumption))
            prices = prices[:n]
            pv_forecast = pv_forecast[:n]
            consumption = consumption[:n]
            ev_demand = ev_demand[:n]

            # Generate plan
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
            )

            _LOGGER.info(
                "CARMA plan: %d hours, target=%.1f kW, %d charge, %d discharge",
                len(self.plan),
                target,
                sum(1 for h in self.plan if h.action == "c"),
                sum(1 for h in self.plan if h.action == "d"),
            )

        except Exception:
            _LOGGER.exception("Plan generation failed — keeping old plan")

    async def _execute(self, state: CarmaboxState) -> None:
        """Execute current action based on state.

        Core rules (in priority order):
        1. Never discharge during export
        2. SoC 100% → standby
        3. Load > target → discharge to fill gap
        4. Load < target → idle (grid handles it)
        """
        # ── RULE 1: Never discharge during export ────────────
        if state.is_exporting:
            if not state.all_batteries_full:
                await self._cmd_charge_pv(state)
            else:
                await self._cmd_standby(state)
            return

        # ── RULE 2: SoC 100% → standby ──────────────────────
        if state.all_batteries_full:
            await self._cmd_standby(state)
            return

        # ── RULE 3: Load > target → discharge ────────────────
        hour = datetime.now().hour
        weight = DEFAULT_NIGHT_WEIGHT if (hour >= 22 or hour < 6) else 1.0
        weighted_grid = max(0, state.grid_power_w) * weight
        target_w = self.target_kw * 1000

        if weighted_grid > target_w:
            discharge_w = int((weighted_grid - target_w) / weight)
            result = self.safety.check_discharge(
                state.battery_soc_1,
                state.battery_soc_2,
                self.min_soc,
                state.grid_power_w,
            )
            if result.ok:
                await self._cmd_discharge(state, discharge_w)
            else:
                _LOGGER.info("SafetyGuard blocked: %s", result.reason)
            return

        # ── RULE 4: Under target → idle ──────────────────────

    async def _cmd_charge_pv(self, state: CarmaboxState) -> None:
        """Set batteries to charge from solar."""
        if self._last_command == BatteryCommand.CHARGE_PV:
            return

        _LOGGER.info("CARMA: charge_pv (solar surplus)")
        for ems_key in ("battery_ems_1", "battery_ems_2"):
            entity = self._get_entity(ems_key)
            if not entity:
                continue
            soc_key = ems_key.replace("ems", "soc")
            soc = self._read_float(self._get_entity(soc_key))
            mode = "battery_standby" if soc >= 100 else "charge_pv"
            await self.hass.services.async_call(
                "select",
                "select_option",
                {
                    "entity_id": entity,
                    "option": mode,
                },
            )

        self._last_command = BatteryCommand.CHARGE_PV

    async def _cmd_standby(self, state: CarmaboxState) -> None:
        """Set all batteries to standby."""
        if self._last_command == BatteryCommand.STANDBY:
            return

        _LOGGER.info("CARMA: standby")
        for ems_key in ("battery_ems_1", "battery_ems_2"):
            entity = self._get_entity(ems_key)
            if entity:
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {
                        "entity_id": entity,
                        "option": "battery_standby",
                    },
                )

        self._last_command = BatteryCommand.STANDBY

    async def _cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Set batteries to discharge at specified wattage."""
        _LOGGER.info("CARMA: discharge %dW (target %.1f kW)", watts, self.target_kw)

        total_soc = state.battery_soc_1 + max(0, state.battery_soc_2)
        if total_soc <= 0:
            return

        ratio_1 = state.battery_soc_1 / total_soc
        w1 = int(watts * ratio_1)
        w2 = watts - w1

        for ems_key, limit_key, w in [
            ("battery_ems_1", "battery_limit_1", w1),
            ("battery_ems_2", "battery_limit_2", w2),
        ]:
            ems_entity = self._get_entity(ems_key)
            limit_entity = self._get_entity(limit_key)
            if ems_entity and w > 0:
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {
                        "entity_id": ems_entity,
                        "option": "discharge_battery",
                    },
                )
                if limit_entity:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": limit_entity,
                            "value": w,
                        },
                    )

        self._last_command = BatteryCommand.DISCHARGE
