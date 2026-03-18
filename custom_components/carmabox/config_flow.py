"""CARMA Box — Config Flow.

GUI-based setup wizard for CARMA Box integration.
Auto-detects inverters, EV chargers, price sources, and PV forecasts.
No YAML editing required.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_EV_FULL_CHARGE_DAYS,
    DEFAULT_EV_NIGHT_TARGET_SOC,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_TARGET_WEIGHTED_KW,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Known inverter integrations to auto-detect
INVERTER_DOMAINS = {
    "goodwe": "GoodWe",
    "huawei_solar": "Huawei",
    "solaredge": "SolarEdge",
}

# Known EV charger integrations
EV_DOMAINS = {
    "easee": "Easee",
    "zaptec": "Zaptec",
    "wallbox": "Wallbox",
}

# Known price integrations
PRICE_DOMAINS = {
    "nordpool": "Nordpool",
    "tibber": "Tibber",
    "entsoe": "ENTSO-E",
}

# Known PV forecast integrations
PV_DOMAINS = {
    "solcast_solar": "Solcast",
    "forecast_solar": "Forecast.Solar",
}

# Known EV car models with battery capacity
EV_MODELS = {
    "XPENG G9": 98,
    "XPENG G6": 87,
    "Tesla Model 3 SR": 60,
    "Tesla Model 3 LR": 75,
    "Tesla Model Y": 75,
    "Volvo EX30": 69,
    "Volvo EX90": 111,
    "VW ID.4 Pro": 77,
    "VW ID.Buzz": 82,
    "Polestar 2 LR": 78,
    "BMW iX3": 74,
    "Kia EV6 LR": 77,
    "Hyundai Ioniq 5 LR": 77,
    "MG4 Extended": 64,
    "Annan": 0,
}

# Grid operators
GRID_OPERATORS = {
    "ellevio": {"name": "Ellevio", "cost_per_kw": 80, "top_n": 3, "night_weight": 0.5},
    "vattenfall": {
        "name": "Vattenfall Eldistribution",
        "cost_per_kw": 75, "top_n": 3, "night_weight": 0.5,
    },
    "eon": {"name": "E.ON Energidistribution", "cost_per_kw": 70, "top_n": 1, "night_weight": 1.0},
    "goteborg_energi": {
        "name": "Göteborg Energi",
        "cost_per_kw": 78, "top_n": 3, "night_weight": 0.5,
    },
    "annan": {"name": "Annan", "cost_per_kw": 80, "top_n": 3, "night_weight": 0.5},
}

PRICE_AREAS = ["SE1", "SE2", "SE3", "SE4"]


class CarmaboxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for CARMA Box."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._detected: dict[str, Any] = {}
        self._user_input: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Welcome + auto-detect."""
        # Prevent duplicate installations
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Auto-detect integrations
        self._detected = await self._auto_detect()

        if not self._detected.get("inverters"):
            return self.async_show_form(
                step_id="no_inverter",
                description_placeholders={
                    "supported": ", ".join(INVERTER_DOMAINS.values()),
                },
            )

        return await self.async_step_confirm()

    async def async_step_no_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No supported inverter found."""
        return self.async_abort(reason="no_inverter")

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Confirm detected equipment."""
        if user_input is not None:
            return await self.async_step_ev()

        detected_text = []
        for inv in self._detected.get("inverters", []):
            detected_text.append(f"🔋 {inv['name']}")
        for ev in self._detected.get("ev_chargers", []):
            detected_text.append(f"🔌 {ev['name']}")
        for price in self._detected.get("price_sources", []):
            detected_text.append(f"⚡ {price['name']}")
        for pv in self._detected.get("pv_forecasts", []):
            detected_text.append(f"☀️ {pv['name']}")

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "detected": "\n".join(detected_text) if detected_text else "Inget hittad",
            },
        )

    async def async_step_ev(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: EV configuration."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_grid()

        has_ev = bool(self._detected.get("ev_chargers"))

        return self.async_show_form(
            step_id="ev",
            data_schema=vol.Schema({
                vol.Required("ev_enabled", default=has_ev): bool,
                vol.Optional("ev_model", default="XPENG G9"): vol.In(
                    list(EV_MODELS.keys())
                ),
                vol.Optional("ev_capacity_kwh", default=98): vol.Coerce(int),
                vol.Optional("ev_night_target_soc", default=DEFAULT_EV_NIGHT_TARGET_SOC): vol.All(
                    vol.Coerce(int), vol.Range(min=20, max=100)
                ),
                vol.Optional("ev_full_charge_days", default=DEFAULT_EV_FULL_CHARGE_DAYS): vol.All(
                    vol.Coerce(int), vol.Range(min=3, max=14)
                ),
            }),
        )

    async def async_step_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Grid operator + price area."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_household()

        # Auto-detect price area from HA location
        default_area = "SE3"
        if self.hass.config.latitude:
            lat = self.hass.config.latitude
            if lat > 63:
                default_area = "SE1"
            elif lat > 60:
                default_area = "SE2"
            elif lat > 56:
                default_area = "SE3"
            else:
                default_area = "SE4"

        return self.async_show_form(
            step_id="grid",
            data_schema=vol.Schema({
                vol.Required("price_area", default=default_area): vol.In(PRICE_AREAS),
                vol.Required("grid_operator", default="ellevio"): vol.In({
                    k: v["name"] for k, v in GRID_OPERATORS.items()
                }),
                vol.Optional("peak_cost_per_kw", default=DEFAULT_PEAK_COST_PER_KW): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=200)
                ),
            }),
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: Household info."""
        if user_input is not None:
            self._user_input.update(user_input)
            return self._create_entry()

        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema({
                vol.Required("household_size", default=4): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Optional("has_pool_pump", default=False): bool,
            }),
        )

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry with all collected data."""
        data = {
            "detected": self._detected,
        }
        options = {
            # Battery (from auto-detect)
            "min_soc": DEFAULT_BATTERY_MIN_SOC,
            "target_weighted_kw": DEFAULT_TARGET_WEIGHTED_KW,
            # EV
            "ev_enabled": self._user_input.get("ev_enabled", False),
            "ev_model": self._user_input.get("ev_model", ""),
            "ev_capacity_kwh": self._user_input.get("ev_capacity_kwh", 98),
            "ev_night_target_soc": self._user_input.get(
                "ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC
            ),
            "ev_full_charge_days": self._user_input.get(
                "ev_full_charge_days", DEFAULT_EV_FULL_CHARGE_DAYS
            ),
            # Grid
            "price_area": self._user_input.get("price_area", "SE3"),
            "grid_operator": self._user_input.get("grid_operator", "ellevio"),
            "peak_cost_per_kw": self._user_input.get("peak_cost_per_kw", DEFAULT_PEAK_COST_PER_KW),
            # Household
            "household_size": self._user_input.get("household_size", 4),
            "has_pool_pump": self._user_input.get("has_pool_pump", False),
            # Entity mappings (from auto-detect, user can change in options)
            **self._build_entity_mappings(),
        }

        title = "CARMA Box"
        inv = self._detected.get("inverters", [])
        if inv:
            title = f"CARMA Box ({inv[0]['name']})"

        return self.async_create_entry(title=title, data=data, options=options)

    def _build_entity_mappings(self) -> dict[str, str]:
        """Build entity ID mappings from detected integrations."""
        mappings: dict[str, str] = {}

        # Inverter entities
        inverters = self._detected.get("inverters", [])
        if inverters:
            inv = inverters[0]
            prefix = inv.get("prefix", "")
            mappings["battery_soc_1"] = f"sensor.pv_battery_soc_{prefix}" if prefix else ""
            mappings["battery_power_1"] = f"sensor.goodwe_battery_power_{prefix}" if prefix else ""
            mappings["battery_ems_1"] = f"select.goodwe_{prefix}_ems_mode" if prefix else ""
            mappings["battery_limit_1"] = (
                f"number.goodwe_{prefix}_ems_power_limit" if prefix else ""
            )

        if len(inverters) > 1:
            inv2 = inverters[1]
            prefix2 = inv2.get("prefix", "")
            mappings["battery_soc_2"] = f"sensor.pv_battery_soc_{prefix2}"
            mappings["battery_power_2"] = f"sensor.goodwe_battery_power_{prefix2}"
            mappings["battery_ems_2"] = f"select.goodwe_{prefix2}_ems_mode"
            mappings["battery_limit_2"] = f"number.goodwe_{prefix2}_ems_power_limit"

        # Grid
        mappings["grid_entity"] = "sensor.house_grid_power"

        # PV
        mappings["pv_entity"] = "sensor.pv_solar_total"

        # EV
        ev_chargers = self._detected.get("ev_chargers", [])
        if ev_chargers:
            ev_prefix = ev_chargers[0].get("prefix", "easee")
            mappings["ev_status_entity"] = f"sensor.{ev_prefix}_status"
            mappings["ev_current_entity"] = f"sensor.{ev_prefix}_current"
            mappings["ev_power_entity"] = f"sensor.{ev_prefix}_power"

        # Price
        price_sources = self._detected.get("price_sources", [])
        if price_sources:
            mappings["price_entity"] = price_sources[0].get("entity_id", "")

        return mappings

    async def _auto_detect(self) -> dict[str, list[dict[str, Any]]]:
        """Auto-detect supported integrations in HA."""
        detected: dict[str, list[dict[str, Any]]] = {
            "inverters": [],
            "ev_chargers": [],
            "price_sources": [],
            "pv_forecasts": [],
        }

        # Check loaded integrations
        entries = self.hass.config_entries.async_entries()

        for entry in entries:
            domain = entry.domain

            if domain in INVERTER_DOMAINS:
                detected["inverters"].append({
                    "domain": domain,
                    "name": INVERTER_DOMAINS[domain],
                    "entry_id": entry.entry_id,
                    "prefix": entry.title.lower().replace(" ", "_") if entry.title else domain,
                })

            elif domain in EV_DOMAINS:
                detected["ev_chargers"].append({
                    "domain": domain,
                    "name": EV_DOMAINS[domain],
                    "entry_id": entry.entry_id,
                    "prefix": f"{domain}_{entry.title}" if entry.title else domain,
                })

            elif domain in PRICE_DOMAINS:
                # Find the main sensor entity
                entity_id = ""
                for state in self.hass.states.async_all("sensor"):
                    if domain in state.entity_id and "kwh" in state.entity_id:
                        entity_id = state.entity_id
                        break
                detected["price_sources"].append({
                    "domain": domain,
                    "name": PRICE_DOMAINS[domain],
                    "entity_id": entity_id,
                })

            elif domain in PV_DOMAINS:
                detected["pv_forecasts"].append({
                    "domain": domain,
                    "name": PV_DOMAINS[domain],
                })

        _LOGGER.info(
            "CARMA Box auto-detect: %d inverters, %d EV, %d price, %d PV",
            len(detected["inverters"]),
            len(detected["ev_chargers"]),
            len(detected["price_sources"]),
            len(detected["pv_forecasts"]),
        )

        return detected

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get options flow handler."""
        return CarmaboxOptionsFlow(config_entry)


class CarmaboxOptionsFlow(OptionsFlow):
    """Options flow — change settings after installation.

    Live-updatable. No restart required.
    """

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Main options page."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "target_weighted_kw",
                    default=opts.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=10.0)),
                vol.Required(
                    "min_soc",
                    default=opts.get("min_soc", DEFAULT_BATTERY_MIN_SOC),
                ): vol.All(vol.Coerce(float), vol.Range(min=5, max=50)),
                vol.Required(
                    "ev_night_target_soc",
                    default=opts.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC),
                ): vol.All(vol.Coerce(int), vol.Range(min=20, max=100)),
                vol.Required(
                    "ev_full_charge_days",
                    default=opts.get("ev_full_charge_days", DEFAULT_EV_FULL_CHARGE_DAYS),
                ): vol.All(vol.Coerce(int), vol.Range(min=3, max=14)),
                vol.Required(
                    "peak_cost_per_kw",
                    default=opts.get("peak_cost_per_kw", DEFAULT_PEAK_COST_PER_KW),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=200)),
                vol.Required(
                    "household_size",
                    default=opts.get("household_size", 4),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                vol.Optional(
                    "has_pool_pump",
                    default=opts.get("has_pool_pump", False),
                ): bool,
            }),
        )
