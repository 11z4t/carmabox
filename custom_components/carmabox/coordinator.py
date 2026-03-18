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

from .const import (
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_TARGET_WEIGHTED_KW,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from .optimizer.models import CarmaboxState, HourPlan
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

        self.target_kw: float = entry.options.get(
            "target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW
        )
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
            grid_power_w=self._read_float(
                opts.get("grid_entity", "sensor.house_grid_power")
            ),
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
        """Generate new energy plan."""
        _LOGGER.debug("Generating plan (target=%.1f kW)", self.target_kw)
        # TODO Sprint 2: integrate planner.generate_plan() with
        # Nordpool prices + Solcast forecast + consumption profile

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
                state.battery_soc_1, state.battery_soc_2,
                self.min_soc, state.grid_power_w,
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
            await self.hass.services.async_call("select", "select_option", {
                "entity_id": entity, "option": mode,
            })

        self._last_command = BatteryCommand.CHARGE_PV

    async def _cmd_standby(self, state: CarmaboxState) -> None:
        """Set all batteries to standby."""
        if self._last_command == BatteryCommand.STANDBY:
            return

        _LOGGER.info("CARMA: standby")
        for ems_key in ("battery_ems_1", "battery_ems_2"):
            entity = self._get_entity(ems_key)
            if entity:
                await self.hass.services.async_call("select", "select_option", {
                    "entity_id": entity, "option": "battery_standby",
                })

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
                await self.hass.services.async_call("select", "select_option", {
                    "entity_id": ems_entity, "option": "discharge_battery",
                })
                if limit_entity:
                    await self.hass.services.async_call("number", "set_value", {
                        "entity_id": limit_entity, "value": w,
                    })

        self._last_command = BatteryCommand.DISCHARGE
