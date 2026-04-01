"""StateManager — reads HA entity state and assembles CarmaboxState snapshots.

PLAT-1140: Extraherad ur coordinator.py (COORD-01).

Pure state-reading concern: no commands, no decisions, no side effects.
Coordinator delegates all entity reads via self._state_mgr.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..adapters import EVAdapter, InverterAdapter
    from ..optimizer.models import CarmaboxState, HourPlan

_LOGGER = logging.getLogger(__name__)

_SENSOR_SANITY_MAX_W = 100_000  # W — values above this are sensor errors (>100 kW)


class StateManager:
    """Reads HA entity state and assembles CarmaboxState snapshots.

    Responsibilities:
    - Low-level entity reads with validation (read_float, read_str, …)
    - Battery temperature resolution (adapters → entity fallback)
    - Full system state snapshot via collect_state()

    Not responsible for:
    - Issuing any commands or writing to HA
    - Tracking runtime state or history
    - Any decision logic
    """

    def __init__(self, hass: HomeAssistant, cfg: dict[str, Any]) -> None:
        """Initialize StateManager.

        Args:
            hass: Home Assistant instance.
            cfg: Merged config entry data+options dict.
        """
        self._hass = hass
        self._cfg = cfg

    # ── Low-level entity readers ──────────────────────────────────────────────

    def read_float(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA entity with validation.

        Returns default for missing, unknown, unavailable or out-of-range values.
        Values with abs > 100 000 are considered sensor errors and rejected.

        Args:
            entity_id: HA entity ID to read.
            default: Value returned on any failure (default 0.0).
        """
        if not entity_id:
            return default
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            val = float(state.state)
            if abs(val) > _SENSOR_SANITY_MAX_W:  # >100 kW = sensor error
                _LOGGER.warning("Unreasonable value %s from %s", val, entity_id)
                return default
            return val
        except (ValueError, TypeError):
            return default

    def read_float_or_none(self, entity_id: str) -> float | None:
        """Read float state, returning None if entity is missing/unknown/unavailable.

        Used for battery power readings where None signals unreliable data
        (e.g. at HA start before first sensor reading). PLAT-946.

        Args:
            entity_id: HA entity ID to read.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            val = float(state.state)
            if abs(val) > _SENSOR_SANITY_MAX_W:
                return None
            return val
        except (ValueError, TypeError):
            return None

    def read_str(self, entity_id: str, default: str = "") -> str:
        """Read string state from HA entity.

        Returns default for missing, unknown or unavailable entities.

        Args:
            entity_id: HA entity ID to read.
            default: Value returned on any failure (default "").
        """
        if not entity_id:
            return default
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        return state.state

    # ── Composite readers ─────────────────────────────────────────────────────

    def read_battery_temp(self, inverter_adapters: list[InverterAdapter]) -> float | None:
        """Read battery temperature — adapters take priority over legacy entity.

        Uses minimum cell temperature across all adapters when available,
        otherwise falls back to the configured battery_temp_entity.

        Args:
            inverter_adapters: Active inverter adapters (may be empty list).
        """
        if inverter_adapters:
            temps = [a.temperature_c for a in inverter_adapters if a.temperature_c is not None]
            return min(temps) if temps else None
        temp_entity = str(self._cfg.get("battery_temp_entity", ""))
        if not temp_entity:
            return None
        val = self.read_float(temp_entity, -999)
        return val if val > -999 else None

    def collect_state(
        self,
        inverter_adapters: list[InverterAdapter],
        ev_adapter: EVAdapter | None,
        target_kw: float,
        plan: list[HourPlan],
    ) -> CarmaboxState:
        """Collect current state from all HA entities.

        Uses inverter/EV adapters when configured, falls back to raw entity reads.

        Args:
            inverter_adapters: Active inverter adapters (may be empty list).
            ev_adapter: Active EV charger adapter, or None.
            target_kw: Current weighted kW target (from coordinator).
            plan: Current hour plan list (from coordinator).
        """
        from ..optimizer.models import CarmaboxState

        opts = self._cfg
        a1 = inverter_adapters[0] if len(inverter_adapters) >= 1 else None
        a2 = inverter_adapters[1] if len(inverter_adapters) >= 2 else None

        # Battery 1 — adapter or legacy config
        battery_soc_1 = a1.soc if a1 else self.read_float(opts.get("battery_soc_1", ""))
        battery_power_1 = a1.power_w if a1 else self.read_float(opts.get("battery_power_1", ""))
        battery_ems_1 = a1.ems_mode if a1 else self.read_str(opts.get("battery_ems_1", ""))

        # Battery 2 — adapter or legacy config
        battery_soc_2 = a2.soc if a2 else self.read_float(opts.get("battery_soc_2", ""), -1)
        battery_power_2 = a2.power_w if a2 else self.read_float(opts.get("battery_power_2", ""))
        battery_ems_2 = a2.ems_mode if a2 else self.read_str(opts.get("battery_ems_2", ""))

        # PLAT-946: Check if battery power sensors are actually available.
        # At HA start, sensors report unknown/unavailable → read_float returns 0.0
        # which masks potential crosscharge. Track validity separately.
        bp1_entity = (
            f"sensor.goodwe_battery_power_{a1.prefix}" if a1 else opts.get("battery_power_1", "")
        )
        bp2_entity = (
            f"sensor.goodwe_battery_power_{a2.prefix}" if a2 else opts.get("battery_power_2", "")
        )
        bp1_valid = self.read_float_or_none(bp1_entity) is not None
        bp2_valid = self.read_float_or_none(bp2_entity) is not None if bp2_entity else True

        # EV — adapter or legacy config
        ev_power_w = (
            ev_adapter.power_w if ev_adapter else self.read_float(opts.get("ev_power_entity", ""))
        )
        ev_current_a = (
            ev_adapter.current_a
            if ev_adapter
            else self.read_float(opts.get("ev_current_entity", ""))
        )
        ev_status = (
            ev_adapter.status if ev_adapter else self.read_str(opts.get("ev_status_entity", ""))
        )

        return CarmaboxState(
            grid_power_w=self.read_float(opts.get("grid_entity", "sensor.house_grid_power")),
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
            pv_power_w=self.read_float(opts.get("pv_entity", "sensor.pv_solar_total")),
            ev_soc=self.read_float(opts.get("ev_soc_entity", ""), -1),
            ev_power_w=ev_power_w,
            ev_current_a=ev_current_a,
            ev_status=ev_status,
            battery_temp_c=self.read_battery_temp(inverter_adapters),
            battery_min_cell_temp_1=a1.temperature_c if a1 else None,
            battery_min_cell_temp_2=a2.temperature_c if a2 else None,
            # Weather (Tempest — prefer local MQTT, fallback to cloud)
            outdoor_temp_c=self.read_float(
                opts.get("outdoor_temp_entity", "sensor.sanduddsvagen_60_temperature")
            ),
            solar_radiation_wm2=self.read_float(
                opts.get("solar_radiation_entity", "sensor.tempest_solar_radiation")
            ),
            illuminance_lx=self.read_float(
                opts.get("illuminance_entity", "sensor.tempest_illuminance")
            ),
            barometric_pressure_hpa=self.read_float(
                opts.get("pressure_entity", "sensor.sanduddsvagen_60_pressure_barometric")
            ),
            rain_mm=self.read_float(
                opts.get("rain_entity", "sensor.sanduddsvagen_60_rain_last_hour")
            ),
            wind_speed_kmh=self.read_float(
                opts.get("wind_speed_entity", "sensor.sanduddsvagen_60_wind_speed")
            ),
            current_price=self.read_float(opts.get("price_entity", "")),
            target_weighted_kw=target_kw,
            plan=plan,
        )
