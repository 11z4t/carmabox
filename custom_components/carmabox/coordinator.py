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
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_TARGET_WEIGHTED_KW,
    DOMAIN,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .optimizer.models import CarmaboxState, HourPlan
from .optimizer.safety_guard import SafetyGuard

_LOGGER = logging.getLogger(__name__)


class CarmaboxCoordinator(DataUpdateCoordinator[CarmaboxState]):
    """CARMA Box coordinator — the brain.

    Runs every 30s:
    1. Collect data from all adapters (battery, EV, grid, prices, PV)
    2. Check if replanning needed (every 5 min)
    3. Execute current hour's plan action
    4. SafetyGuard validates EVERY command before sending
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.safety = SafetyGuard()
        self.plan: list[HourPlan] = []
        self._plan_counter = 0
        self._last_command: str = ""

        # Config from options_flow (live-updatable)
        self.target_kw = entry.options.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        self.min_soc = entry.options.get("min_soc", DEFAULT_BATTERY_MIN_SOC)

    async def _async_update_data(self) -> CarmaboxState:
        """Fetch data, run optimizer, execute plan.

        This method IS the entire CARMA Box logic.
        No automations, no YAML, no shell_commands.
        """
        try:
            # ─── 1. COLLECT DATA ──────────────────────────────────
            state = await self._collect_state()

            # ─── 2. REPLAN (every 5 min) ──────────────────────────
            self._plan_counter += 1
            if self._plan_counter >= PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS:
                self._plan_counter = 0
                await self._generate_plan(state)

            # ─── 3. EXECUTE ───────────────────────────────────────
            await self._execute(state)

            return state

        except Exception as err:
            _LOGGER.error("CARMA Box update failed: %s", err)
            raise UpdateFailed(f"Update failed: {err}") from err

    async def _collect_state(self) -> CarmaboxState:
        """Collect current state from all HA entities."""
        hass = self.hass

        def _state(entity_id: str, default: float = 0.0) -> float:
            s = hass.states.get(entity_id)
            if s is None or s.state in ("unknown", "unavailable", ""):
                return default
            try:
                return float(s.state)
            except (ValueError, TypeError):
                return default

        def _str_state(entity_id: str, default: str = "") -> str:
            s = hass.states.get(entity_id)
            if s is None or s.state in ("unknown", "unavailable"):
                return default
            return s.state

        # Read from config which entities to use
        opts = self.entry.options

        return CarmaboxState(
            # Grid
            grid_power_w=_state(opts.get("grid_entity", "sensor.house_grid_power")),
            # Battery (kontor)
            battery_soc_1=_state(opts.get("battery_soc_1", "sensor.pv_battery_soc_kontor")),
            battery_power_1=_state(opts.get("battery_power_1", "sensor.goodwe_battery_power_kontor")),
            battery_ems_1=_str_state(opts.get("battery_ems_1", "select.goodwe_kontor_ems_mode")),
            # Battery (förråd)
            battery_soc_2=_state(opts.get("battery_soc_2", "sensor.pv_battery_soc_forrad"), -1),
            battery_power_2=_state(opts.get("battery_power_2", "sensor.goodwe_battery_power_forrad"), 0),
            battery_ems_2=_str_state(opts.get("battery_ems_2", "select.goodwe_forrad_ems_mode")),
            # PV
            pv_power_w=_state(opts.get("pv_entity", "sensor.pv_solar_total")),
            # EV
            ev_soc=_state(opts.get("ev_soc_entity", "sensor.xpeng_g9_xpeng_g9_battery_soc"), -1),
            ev_power_w=_state(opts.get("ev_power_entity", "sensor.easee_home_12840_power")),
            ev_current_a=_state(opts.get("ev_current_entity", "sensor.easee_home_12840_current")),
            ev_status=_str_state(opts.get("ev_status_entity", "sensor.easee_home_12840_status")),
            # Price
            current_price=_state(opts.get("price_entity", "sensor.nordpool_kwh_se3_sek_3_10_025")),
            # Computed
            target_weighted_kw=self.target_kw,
            plan=self.plan,
        )

    async def _generate_plan(self, state: CarmaboxState) -> None:
        """Generate new energy plan based on current state + forecasts."""
        _LOGGER.debug("Generating new plan")
        # TODO: Read Nordpool prices, Solcast forecast, consumption profile
        # TODO: Call optimizer.optimize()
        # TODO: Store plan

    async def _execute(self, state: CarmaboxState) -> None:
        """Execute current action based on state and plan.

        Core logic — replaces ALL automations:
        - Solar surplus → charge batteries
        - Load > target → discharge batteries
        - Load < target → idle
        - SoC 100% → standby
        - Never discharge during export
        """
        grid_w = state.grid_power_w
        soc_1 = state.battery_soc_1
        soc_2 = state.battery_soc_2
        target_w = state.target_weighted_kw * 1000

        # ─── RULE 0: Never discharge during export ───────────
        if grid_w < 0:
            # Exporting — charge from solar if batteries not full
            if soc_1 < 100 or (soc_2 >= 0 and soc_2 < 100):
                await self._set_charge_pv(state)
            else:
                await self._set_standby(state)
            return

        # ─── RULE 1: SoC 100% → standby ─────────────────────
        all_full = soc_1 >= 100 and (soc_2 < 0 or soc_2 >= 100)
        if all_full:
            await self._set_standby(state)
            return

        # ─── RULE 2: Load > target → discharge ──────────────
        # Calculate Ellevio weight
        from datetime import datetime
        hour = datetime.now().hour
        weight = 0.5 if (hour >= 22 or hour < 6) else 1.0
        weighted_grid = grid_w * weight

        if weighted_grid > target_w:
            # Need battery support
            discharge_w = (weighted_grid - target_w) / weight
            # SafetyGuard check
            result = self.safety.check_discharge(soc_1, soc_2, self.min_soc, grid_w)
            if result.ok:
                await self._set_discharge(state, int(discharge_w))
            else:
                _LOGGER.info("SafetyGuard blocked discharge: %s", result.reason)
            return

        # ─── RULE 3: Load < target → idle ────────────────────
        # Nothing to do — grid handles it fine
        # Don't discharge just to discharge

    async def _set_charge_pv(self, state: CarmaboxState) -> None:
        """Set batteries to charge from solar."""
        if self._last_command == "charge_pv":
            return  # Already in this mode

        _LOGGER.info("CARMA: charge_pv (solar surplus)")
        opts = self.entry.options

        # Per-battery: skip if SoC 100%
        if state.battery_soc_1 < 100:
            await self.hass.services.async_call("select", "select_option", {
                "entity_id": opts.get("battery_ems_1", "select.goodwe_kontor_ems_mode"),
                "option": "charge_pv",
            })
        else:
            await self.hass.services.async_call("select", "select_option", {
                "entity_id": opts.get("battery_ems_1", "select.goodwe_kontor_ems_mode"),
                "option": "battery_standby",
            })

        if state.battery_soc_2 >= 0 and state.battery_soc_2 < 100:
            await self.hass.services.async_call("select", "select_option", {
                "entity_id": opts.get("battery_ems_2", "select.goodwe_forrad_ems_mode"),
                "option": "charge_pv",
            })
        elif state.battery_soc_2 >= 0:
            await self.hass.services.async_call("select", "select_option", {
                "entity_id": opts.get("battery_ems_2", "select.goodwe_forrad_ems_mode"),
                "option": "battery_standby",
            })

        self._last_command = "charge_pv"

    async def _set_standby(self, state: CarmaboxState) -> None:
        """Set all batteries to standby."""
        if self._last_command == "standby":
            return

        _LOGGER.info("CARMA: standby (all full or idle)")
        opts = self.entry.options

        for ems in ["battery_ems_1", "battery_ems_2"]:
            entity = opts.get(ems)
            if entity:
                await self.hass.services.async_call("select", "select_option", {
                    "entity_id": entity,
                    "option": "battery_standby",
                })

        self._last_command = "standby"

    async def _set_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Set batteries to discharge at specified wattage."""
        _LOGGER.info("CARMA: discharge %dW (target %s kW weighted)", watts, self.target_kw)
        opts = self.entry.options

        # Split between batteries proportionally
        total_soc = state.battery_soc_1 + max(0, state.battery_soc_2)
        if total_soc <= 0:
            return

        ratio_1 = state.battery_soc_1 / total_soc
        w1 = int(watts * ratio_1)
        w2 = watts - w1

        for ems, limit, w in [
            ("battery_ems_1", opts.get("battery_limit_1", "number.goodwe_kontor_ems_power_limit"), w1),
            ("battery_ems_2", opts.get("battery_limit_2", "number.goodwe_forrad_ems_power_limit"), w2),
        ]:
            entity_ems = opts.get(ems)
            if entity_ems and w > 0:
                await self.hass.services.async_call("select", "select_option", {
                    "entity_id": entity_ems,
                    "option": "discharge_battery",
                })
                if limit:
                    await self.hass.services.async_call("number", "set_value", {
                        "entity_id": limit,
                        "value": w,
                    })

        self._last_command = f"discharge_{watts}"
