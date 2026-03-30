"""CARMA Box — Config Flow.

GUI-based setup wizard for CARMA Box integration.
Auto-detects inverters, EV chargers, price sources, and PV forecasts.
No YAML editing required.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    APPLIANCE_CATEGORIES,
    APPLIANCE_EXCLUDE_PREFIXES,
    APPLIANCE_HINTS,
    BATTERY_BRANDS,
    CONTRACT_TYPES,
    DEFAULT_APPLIANCE_THRESHOLD_W,
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_DAILY_BATTERY_NEED_KWH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_EV_FULL_CHARGE_DAYS,
    DEFAULT_EV_NIGHT_TARGET_SOC,
    DEFAULT_FALLBACK_PRICE_ORE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_PEAK_COST_PER_KW,
    DEFAULT_TARGET_WEIGHTED_KW,
    DOMAIN,
    ELECTRICITY_RETAILERS,
    HEATING_TYPES,
    SOLAR_DIRECTIONS,
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
        "cost_per_kw": 75,
        "top_n": 3,
        "night_weight": 0.5,
    },
    "eon": {
        "name": "E.ON Energidistribution",
        "cost_per_kw": 70,
        "top_n": 1,
        "night_weight": 1.0,
    },
    "goteborg_energi": {
        "name": "Göteborg Energi",
        "cost_per_kw": 78,
        "top_n": 3,
        "night_weight": 0.5,
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
        self._detected_appliances: dict[str, dict[str, str]] = {}
        self._user_input: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                "detected": ("\n".join(detected_text) if detected_text else "Inget hittad"),
            },
        )

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 3: EV configuration."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_grid()

        has_ev = bool(self._detected.get("ev_chargers"))

        return self.async_show_form(
            step_id="ev",
            data_schema=vol.Schema(
                {
                    vol.Required("ev_enabled", default=has_ev): bool,
                    vol.Optional("ev_model", default="XPENG G9"): vol.In(list(EV_MODELS.keys())),
                    vol.Optional("ev_capacity_kwh", default=98): vol.Coerce(int),
                    vol.Optional(
                        "ev_night_target_soc", default=DEFAULT_EV_NIGHT_TARGET_SOC
                    ): vol.All(vol.Coerce(int), vol.Range(min=20, max=100)),
                    vol.Optional(
                        "ev_full_charge_days", default=DEFAULT_EV_FULL_CHARGE_DAYS
                    ): vol.All(vol.Coerce(int), vol.Range(min=3, max=14)),
                }
            ),
        )

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
            data_schema=vol.Schema(
                {
                    vol.Required("price_area", default=default_area): vol.In(PRICE_AREAS),
                    vol.Required("grid_operator", default="ellevio"): vol.In(
                        {k: v["name"] for k, v in GRID_OPERATORS.items()}
                    ),
                    vol.Optional("peak_cost_per_kw", default=DEFAULT_PEAK_COST_PER_KW): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=200)
                    ),
                    vol.Optional("fallback_price_ore", default=DEFAULT_FALLBACK_PRICE_ORE): vol.All(
                        vol.Coerce(float), vol.Range(min=10, max=500)
                    ),
                }
            ),
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: Household info + mode."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_household_profile()

        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(
                {
                    vol.Required("household_size", default=4): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=10)
                    ),
                    vol.Optional("has_pool_pump", default=False): bool,
                    vol.Optional("executor_enabled", default=False): bool,
                }
            ),
        )

    async def async_step_household_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5b: Detailed household profile for benchmarking."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_appliances()

        # Auto-detect battery brand from inverter domains
        detected_brand = "other"
        for inv in self._detected.get("inverters", []):
            domain = inv.get("domain", "")
            if domain in BATTERY_BRANDS:
                detected_brand = domain
                break

        # Count detected batteries
        battery_count = len(self._detected.get("inverters", []))

        return self.async_show_form(
            step_id="household_profile",
            data_schema=vol.Schema(
                {
                    vol.Optional("house_size_m2", default=130): vol.All(
                        vol.Coerce(int), vol.Range(min=20, max=500)
                    ),
                    vol.Optional("heating_type", default="vp"): vol.In(dict(HEATING_TYPES.items())),
                    vol.Optional("has_hot_water_heater", default=False): bool,
                    vol.Optional("solar_kwp", default=0.0): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=50)
                    ),
                    vol.Optional("solar_direction", default="S"): vol.In(
                        dict(SOLAR_DIRECTIONS.items())
                    ),
                    vol.Optional("solar_tilt", default=30): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=90)
                    ),
                    vol.Optional("battery_brand", default=detected_brand): vol.In(
                        dict(BATTERY_BRANDS.items())
                    ),
                    vol.Optional("battery_count", default=battery_count): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=10)
                    ),
                    vol.Optional("postal_code", default=""): str,
                    vol.Optional("contract_type", default="variable"): vol.In(
                        dict(CONTRACT_TYPES.items())
                    ),
                    vol.Optional("electricity_retailer", default="other"): vol.In(
                        dict(ELECTRICITY_RETAILERS.items())
                    ),
                }
            ),
        )

    async def async_step_appliances(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5b: Auto-detected appliance sensors — user categorizes them."""
        if user_input is not None:
            # Parse user selections into appliances list
            appliances: list[dict[str, Any]] = []
            for entity_id, info in self._detected_appliances.items():
                enabled_key = f"enable_{entity_id}"
                category_key = f"category_{entity_id}"
                if user_input.get(enabled_key, True):
                    appliances.append(
                        {
                            "entity_id": entity_id,
                            "name": info["name"],
                            "category": user_input.get(category_key, info["category"]),
                            "threshold_w": DEFAULT_APPLIANCE_THRESHOLD_W,
                        }
                    )
            self._user_input["appliances"] = appliances
            return await self.async_step_summary()

        # Auto-detect power sensors
        self._detected_appliances = self._detect_appliances()

        if not self._detected_appliances:
            # No appliances found — skip to summary
            self._user_input["appliances"] = []
            return await self.async_step_summary()

        # Build dynamic schema: enable checkbox + category selector per sensor
        category_options = dict(APPLIANCE_CATEGORIES.items())
        schema_dict: dict[Any, Any] = {}
        for entity_id, info in self._detected_appliances.items():
            # Checkbox to include/exclude
            schema_dict[vol.Optional(f"enable_{entity_id}", default=True)] = bool
            # Category selector
            schema_dict[vol.Optional(f"category_{entity_id}", default=info["category"])] = vol.In(
                category_options
            )

        return self.async_show_form(
            step_id="appliances",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "detected_count": str(len(self._detected_appliances)),
                "sensor_list": "\n".join(
                    f"⚡ {info['name']}"
                    f" ({APPLIANCE_CATEGORIES.get(info['category'], info['category'])})"
                    for info in self._detected_appliances.values()
                ),
            },
        )

    def _detect_appliances(self) -> dict[str, dict[str, str]]:
        """Auto-detect power sensors that look like appliances.

        Scans all sensor.* entities with unit_of_measurement W or kW,
        filters out known system sensors, and suggests categories.

        Returns {entity_id: {"name": friendly_name, "category": category_key}}.
        """
        result: dict[str, dict[str, str]] = {}
        for state in self.hass.states.async_all("sensor"):
            unit = (state.attributes.get("unit_of_measurement") or "").lower()
            if unit not in ("w", "kw"):
                continue

            entity_id = state.entity_id
            name_part = entity_id.replace("sensor.", "")

            # Exclude known system sensors
            if any(name_part.startswith(prefix) for prefix in APPLIANCE_EXCLUDE_PREFIXES):
                continue

            # Friendly name from attributes, or cleaned entity_id
            friendly_name = (
                state.attributes.get("friendly_name") or name_part.replace("_", " ").title()
            )

            # Guess category from name hints
            category = "other"
            name_lower = name_part.lower()
            for hint, cat in APPLIANCE_HINTS.items():
                if hint in name_lower:
                    category = cat
                    break

            result[entity_id] = {"name": friendly_name, "category": category}

        return result

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 7: Onboarding summary — show what was found and what happens next."""
        if user_input is not None:
            return self._create_entry()

        # ── Build equipment summary lines ──
        equipment_lines: list[str] = []
        for inv in self._detected.get("inverters", []):
            soc_str = self._read_soc(inv.get("prefix", ""))
            label = inv["name"]
            if inv.get("prefix"):
                label += f" {inv['prefix'].replace('_', ' ').title()}"
            equipment_lines.append(f"{label} ({soc_str})" if soc_str else label)

        for ev in self._detected.get("ev_chargers", []):
            status = self._read_ev_status(ev.get("prefix", ""))
            equipment_lines.append(f"{ev['name']} ({status})")

        for price in self._detected.get("price_sources", []):
            current = self._read_current_price(price.get("entity_id", ""))
            equipment_lines.append(f"{price['name']} ({current})" if current else price["name"])

        for pv in self._detected.get("pv_forecasts", []):
            equipment_lines.append(pv["name"])

        # ── Build strategy summary ──
        target_kw = self._user_input.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW)
        ev_enabled = self._user_input.get("ev_enabled", False)
        ev_target = self._user_input.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC)
        executor = self._user_input.get("executor_enabled", False)

        strategy_parts: list[str] = []
        strategy_parts.append(f"{target_kw} kW target")
        if ev_enabled:
            strategy_parts.append(f"EV {ev_target}% varje natt")
        strategy_parts.append("styrning aktiv" if executor else "analysläge")

        # ── Appliances summary ──
        appliances = self._user_input.get("appliances", [])
        if appliances:
            equipment_lines.append("")  # blank line separator
            for app in appliances:
                cat_label = APPLIANCE_CATEGORIES.get(app["category"], app["category"])
                equipment_lines.append(f"⚡ {app['name']} ({cat_label})")

        equipment_text = "\n".join(equipment_lines) if equipment_lines else "-"
        strategy_text = ", ".join(strategy_parts)

        return self.async_show_form(
            step_id="summary",
            description_placeholders={
                "equipment": equipment_text,
                "strategy": strategy_text,
            },
        )

    def _read_soc(self, prefix: str) -> str:
        """Read battery SoC for an inverter prefix, returns e.g. '57%' or ''."""
        if not prefix:
            return ""
        entity_id = f"sensor.pv_battery_soc_{prefix}"
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                return f"{int(float(state.state))}%"
            except (ValueError, TypeError):
                pass
        return ""

    def _read_ev_status(self, prefix: str) -> str:
        """Read EV charger status, returns e.g. 'ansluten' or 'okänd'."""
        if not prefix:
            return "okänd"
        entity_id = f"sensor.{prefix}_status"
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            return state.state.lower()
        return "okänd"

    def _read_current_price(self, entity_id: str) -> str:
        """Read current electricity price, returns e.g. '141 öre' or ''."""
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                price = float(state.state)
                # S2: Don't auto-convert — Nordpool already reports in öre
                # (the old < 20 heuristic corrupted prices like 19.5 öre → 1950)
                return f"{int(round(price))} öre"
            except (ValueError, TypeError):
                pass
        return ""

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry with all collected data.

        All config is stored in BOTH data and options for maximum compatibility.
        HA guaranteed persistence is via data; options can be live-updated.
        """
        config = {
            # Battery
            "battery_1_kwh": self._user_input.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH),
            "battery_2_kwh": self._user_input.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH),
            "min_soc": DEFAULT_BATTERY_MIN_SOC,
            "target_weighted_kw": DEFAULT_TARGET_WEIGHTED_KW,
            # Consumption
            "daily_consumption_kwh": self._user_input.get(
                "daily_consumption_kwh", DEFAULT_DAILY_CONSUMPTION_KWH
            ),
            "daily_battery_need_kwh": self._user_input.get(
                "daily_battery_need_kwh", DEFAULT_DAILY_BATTERY_NEED_KWH
            ),
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
            "fallback_price_ore": self._user_input.get(
                "fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE
            ),
            # Grid charge
            "grid_charge_price_threshold": self._user_input.get(
                "grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD
            ),
            "grid_charge_max_soc": self._user_input.get(
                "grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC
            ),
            # Household
            "household_size": self._user_input.get("household_size", 4),
            "has_pool_pump": self._user_input.get("has_pool_pump", False),
            # Household profile (PLAT-962)
            "house_size_m2": self._user_input.get("house_size_m2", 0),
            "heating_type": self._user_input.get("heating_type", ""),
            "has_hot_water_heater": self._user_input.get("has_hot_water_heater", False),
            "solar_kwp": self._user_input.get("solar_kwp", 0.0),
            "solar_direction": self._user_input.get("solar_direction", ""),
            "solar_tilt": self._user_input.get("solar_tilt", 0),
            "battery_brand": self._user_input.get("battery_brand", ""),
            "battery_count": self._user_input.get("battery_count", 0),
            "postal_code": self._user_input.get("postal_code", ""),
            "contract_type": self._user_input.get("contract_type", ""),
            "electricity_retailer": self._user_input.get("electricity_retailer", ""),
            # Mode — analyzer only by default (no battery commands sent)
            "executor_enabled": self._user_input.get("executor_enabled", False),
            # Appliances (from auto-detect + user categorization)
            "appliances": self._user_input.get("appliances", []),
            # Entity mappings (from auto-detect, user can change in options)
            **self._build_entity_mappings(),
        }

        data = {
            "detected": self._detected,
            **config,
        }

        title = "CARMA Box"
        inv = self._detected.get("inverters", [])
        if inv:
            title = f"CARMA Box ({inv[0]['name']})"

        return self.async_create_entry(title=title, data=data, options=config)

    def _build_entity_mappings(self) -> dict[str, str]:
        """Build entity ID mappings from detected integrations.

        Uses real HA entity scanning instead of guessing names from prefixes.
        """
        mappings: dict[str, str] = {}

        # ── Inverter entities (scan real states, fallback to prefix) ──
        inverters = self._detected.get("inverters", [])
        # Exclude computed/aggregate sensors (total, imbalance, etc.)
        _exclude = (
            "total",
            "imbalance",
            "available",
            "trend",
            "charging",
            "discharging",
            "mode",
        )
        battery_soc_entities = [
            e
            for e in self._find_entities("sensor", "pv_battery_soc_")
            if not any(x in e for x in _exclude)
        ]
        battery_power_entities = [
            e
            for e in self._find_entities("sensor", "goodwe_battery_power_")
            if not any(x in e for x in _exclude)
        ]
        battery_ems_entities = self._find_entities("select", "goodwe_", "_ems_mode")
        battery_limit_entities = self._find_entities("number", "goodwe_", "_peak_shaving_power")

        if battery_soc_entities:
            # Real entities found — use them
            for i, soc_eid in enumerate(battery_soc_entities[:2], 1):
                suffix = soc_eid.split("pv_battery_soc_")[-1]
                mappings[f"battery_soc_{i}"] = soc_eid
                mappings[f"battery_power_{i}"] = self._find_by_suffix(
                    battery_power_entities,
                    suffix,
                    f"sensor.goodwe_battery_power_{suffix}",
                )
                mappings[f"battery_ems_{i}"] = self._find_by_suffix(
                    battery_ems_entities, suffix, f"select.goodwe_{suffix}_ems_mode"
                )
                mappings[f"battery_limit_{i}"] = self._find_by_suffix(
                    battery_limit_entities,
                    suffix,
                    f"number.goodwe_{suffix}_peak_shaving_power",
                )
        else:
            # Fallback: build from detected prefixes
            for i, inv in enumerate(inverters[:2], 1):
                prefix = inv.get("prefix", "")
                if not prefix:
                    continue
                mappings[f"battery_soc_{i}"] = f"sensor.pv_battery_soc_{prefix}"
                mappings[f"battery_power_{i}"] = f"sensor.goodwe_battery_power_{prefix}"
                mappings[f"battery_ems_{i}"] = f"select.goodwe_{prefix}_ems_mode"
                mappings[f"battery_limit_{i}"] = f"number.goodwe_{prefix}_peak_shaving_power"

        # Adapter keys: prefix + device_id for GoodWeAdapter
        if battery_soc_entities:
            for i, soc_eid in enumerate(battery_soc_entities[:2], 1):
                suffix = soc_eid.split("pv_battery_soc_")[-1]
                mappings[f"inverter_{i}_prefix"] = suffix
                # Find device_id from device registry via detected inverters
                device_id = self._resolve_inverter_device_id(suffix, inverters)
                if device_id:
                    mappings[f"inverter_{i}_device_id"] = device_id
        elif inverters:
            for i, inv in enumerate(inverters[:2], 1):
                mappings[f"inverter_{i}_prefix"] = inv.get("prefix", "")
                # Use first device registry ID, or fall back to entry_id
                dev_ids = inv.get("device_ids", [])
                dev_id = dev_ids[0] if dev_ids else inv.get("entry_id", "")
                mappings[f"inverter_{i}_device_id"] = dev_id

        # Battery temperature
        temp_entity = self._find_first_entity("sensor", "pv_battery_min_temperature")
        if temp_entity:
            mappings["battery_temp_entity"] = temp_entity

        # Grid
        mappings["grid_entity"] = (
            self._find_first_entity("sensor", "house_grid_power") or "sensor.house_grid_power"
        )

        # PV
        mappings["pv_entity"] = (
            self._find_first_entity("sensor", "pv_solar_total") or "sensor.pv_solar_total"
        )

        # ── EV ─────────────────────────────────────────────────
        ev_chargers = self._detected.get("ev_chargers", [])
        if ev_chargers:
            ev = ev_chargers[0]
            ev_prefix = ev.get("prefix", "easee")
            mappings["ev_prefix"] = ev_prefix
            mappings["ev_status_entity"] = f"sensor.{ev_prefix}_status"
            mappings["ev_current_entity"] = f"sensor.{ev_prefix}_current"
            mappings["ev_power_entity"] = f"sensor.{ev_prefix}_power"
            # Device ID from device registry
            ev_dev_ids = ev.get("device_ids", [])
            if ev_dev_ids:
                mappings["ev_device_id"] = ev_dev_ids[0]
            # Charger ID (serial) for native Easee service calls
            charger_id = self._detect_easee_charger_id(ev_prefix)
            if charger_id:
                mappings["ev_charger_id"] = charger_id

        # EV SoC — find sensor with "battery_soc" that has a valid numeric state
        ev_soc = self._find_ev_soc_entity()
        if ev_soc:
            mappings["ev_soc_entity"] = ev_soc

        # ── Price — prefer sources with actual entity_id ──────
        price_sources = self._detected.get("price_sources", [])
        # Sort: sources with entity_id first
        price_with_entity = [p for p in price_sources if p.get("entity_id")]
        price_without = [p for p in price_sources if not p.get("entity_id")]
        sorted_prices = price_with_entity + price_without

        if sorted_prices:
            mappings["price_entity"] = sorted_prices[0].get("entity_id", "")
            if len(sorted_prices) > 1:
                mappings["price_entity_fallback"] = sorted_prices[1].get("entity_id", "")

        return mappings

    def _find_entities(self, domain: str, prefix: str, suffix: str = "") -> list[str]:
        """Find all entity_ids matching domain.prefix*suffix pattern.

        Sorted reverse-alphabetically so 'kontor' comes before 'forrad'
        (primary/larger battery first).
        """
        results = []
        for state in self.hass.states.async_all(domain):
            eid = state.entity_id.replace(f"{domain}.", "")
            if eid.startswith(prefix) and (not suffix or eid.endswith(suffix)):
                results.append(state.entity_id)
        return sorted(results, reverse=True)

    def _find_first_entity(self, domain: str, pattern: str, exclude: str = "") -> str:
        """Find first entity_id containing pattern, preferring exact match."""
        exact = f"{domain}.{pattern}"
        candidates: list[str] = []
        for state in self.hass.states.async_all(domain):
            if pattern in state.entity_id and (not exclude or exclude not in state.entity_id):
                if state.entity_id == exact:
                    return exact
                candidates.append(state.entity_id)
        return candidates[0] if candidates else ""

    def _find_ev_soc_entity(self) -> str:
        """Find EV battery SoC entity — prefer numeric + longer (more specific) names."""
        numeric: list[str] = []
        non_numeric: list[str] = []
        for state in self.hass.states.async_all("sensor"):
            eid = state.entity_id
            if "pv_battery" in eid or "goodwe" in eid:
                continue
            if "battery_soc" in eid or "ev_soc" in eid:
                try:
                    float(state.state)
                    numeric.append(eid)
                except (ValueError, TypeError):
                    non_numeric.append(eid)
        # Longer names are more specific (e.g. xpeng_g9_xpeng_g9_battery_soc)
        numeric.sort(key=len, reverse=True)
        non_numeric.sort(key=len, reverse=True)
        candidates = numeric + non_numeric
        return candidates[0] if candidates else ""

    @staticmethod
    def _find_by_suffix(entities: list[str], suffix: str, fallback: str) -> str:
        """Find entity containing suffix, or return fallback."""
        for eid in entities:
            if suffix in eid:
                return eid
        return fallback

    def _resolve_inverter_device_id(
        self, entity_suffix: str, inverters: list[dict[str, Any]]
    ) -> str:
        """Resolve device registry ID for an inverter by matching entity suffix.

        Tries multiple strategies:
        1. Find entity in entity registry → get its device_id directly
        2. Match suffix against detected inverter prefixes → use device_ids
        3. Fall back to first device_id from any detected inverter
        """
        # Strategy 1: Look up entity in entity registry for exact device_id
        try:
            from homeassistant.helpers import entity_registry as er

            ent_reg = er.async_get(self.hass)
            soc_entity = f"sensor.pv_battery_soc_{entity_suffix}"
            entry = ent_reg.async_get(soc_entity)
            if entry and entry.device_id:
                return entry.device_id
        except Exception:
            pass

        # Strategy 2: Match suffix against detected inverter device_ids
        for inv in inverters:
            det_prefix = inv.get("prefix", "")
            match = (
                det_prefix == entity_suffix
                or entity_suffix in det_prefix
                or det_prefix in entity_suffix
            )
            if match:
                dev_ids = inv.get("device_ids", [])
                if dev_ids:
                    return str(dev_ids[0])

        # Strategy 3: If only one inverter detected, use its first device
        if len(inverters) == 1:
            dev_ids = inverters[0].get("device_ids", [])
            if dev_ids:
                return str(dev_ids[0])

        return ""

    def _detect_ev_prefix(self, domain: str) -> str:
        """Detect EV charger entity prefix by scanning real entities.

        entry.title can be an email (borje@malmgrens.me) which is NOT
        a valid entity_id component. Instead, find a _status sensor.
        """
        for state in self.hass.states.async_all("sensor"):
            eid = state.entity_id
            if domain in eid and "_status" in eid:
                # e.g. sensor.easee_home_12840_status → easee_home_12840
                prefix = eid.replace("sensor.", "").replace("_status", "")
                return prefix
        # Fallback: scan for any entity with domain prefix
        for state in self.hass.states.async_all("sensor"):
            if state.entity_id.startswith(f"sensor.{domain}_"):
                parts = state.entity_id.replace("sensor.", "").rsplit("_", 1)
                if len(parts) == 2:
                    return parts[0]
        return domain

    def _detect_easee_charger_id(self, ev_prefix: str) -> str:
        """Auto-detect Easee charger ID (serial) from entity attributes.

        Tries multiple attribute names and extraction strategies.
        """
        status_entity = f"sensor.{ev_prefix}_status"
        state = self.hass.states.get(status_entity)
        if state and state.attributes:
            # Try common attribute names for charger serial
            for attr_name in ("id", "charger_id", "serial_number", "serial"):
                charger_id = state.attributes.get(attr_name, "")
                if charger_id:
                    return str(charger_id)

        # Fallback: extract from entity prefix pattern (e.g., "easee_home_12840" → "EH12840")
        # Only if prefix matches known Easee naming convention
        if ev_prefix.startswith("easee_home_"):
            serial_part = ev_prefix.replace("easee_home_", "")
            if serial_part.isdigit():
                return f"EH{serial_part}"

        return ""

    def _resolve_device_ids(self, entry_id: str) -> list[str]:
        """Resolve HA device registry IDs for a config entry.

        Returns list of device_id strings from the device registry
        that are associated with the given config entry.
        """
        try:
            from homeassistant.helpers import device_registry as dr

            dev_reg = dr.async_get(self.hass)
            return [
                device.id
                for device in dev_reg.devices.values()
                if entry_id in device.config_entries
            ]
        except Exception:
            _LOGGER.debug("Could not resolve device IDs for entry %s", entry_id)
            return []

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
                device_ids = self._resolve_device_ids(entry.entry_id)
                detected["inverters"].append(
                    {
                        "domain": domain,
                        "name": INVERTER_DOMAINS[domain],
                        "entry_id": entry.entry_id,
                        "device_ids": device_ids,
                        "prefix": (
                            entry.title.lower().replace(" ", "_") if entry.title else domain
                        ),
                    }
                )

            elif domain in EV_DOMAINS:
                device_ids = self._resolve_device_ids(entry.entry_id)
                # Scan entities to find real prefix (entry.title can be email/name)
                ev_prefix = self._detect_ev_prefix(domain)
                detected["ev_chargers"].append(
                    {
                        "domain": domain,
                        "name": EV_DOMAINS[domain],
                        "entry_id": entry.entry_id,
                        "device_ids": device_ids,
                        "prefix": ev_prefix,
                    }
                )

            elif domain in PRICE_DOMAINS:
                # Find the main sensor entity
                entity_id = ""
                for state in self.hass.states.async_all("sensor"):
                    if domain in state.entity_id and "kwh" in state.entity_id:
                        entity_id = state.entity_id
                        break
                detected["price_sources"].append(
                    {
                        "domain": domain,
                        "name": PRICE_DOMAINS[domain],
                        "entity_id": entity_id,
                    }
                )

            elif domain in PV_DOMAINS:
                detected["pv_forecasts"].append(
                    {
                        "domain": domain,
                        "name": PV_DOMAINS[domain],
                    }
                )

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

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Main options page."""
        if user_input is not None:
            # Merge with existing options to preserve entity mappings
            return self.async_create_entry(data={**self.entry.options, **user_input})

        opts = self.entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "battery_1_kwh",
                        default=opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        "battery_2_kwh",
                        default=opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        "target_weighted_kw",
                        default=opts.get("target_weighted_kw", DEFAULT_TARGET_WEIGHTED_KW),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=10.0)),
                    vol.Required(
                        "min_soc",
                        default=opts.get("min_soc", DEFAULT_BATTERY_MIN_SOC),
                    ): vol.All(vol.Coerce(float), vol.Range(min=5, max=50)),
                    vol.Required(
                        "daily_consumption_kwh",
                        default=opts.get("daily_consumption_kwh", DEFAULT_DAILY_CONSUMPTION_KWH),
                    ): vol.All(vol.Coerce(float), vol.Range(min=1, max=100)),
                    vol.Required(
                        "daily_battery_need_kwh",
                        default=opts.get("daily_battery_need_kwh", DEFAULT_DAILY_BATTERY_NEED_KWH),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=50)),
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
                        "fallback_price_ore",
                        default=opts.get("fallback_price_ore", DEFAULT_FALLBACK_PRICE_ORE),
                    ): vol.All(vol.Coerce(float), vol.Range(min=10, max=500)),
                    vol.Required(
                        "grid_charge_price_threshold",
                        default=opts.get(
                            "grid_charge_price_threshold",
                            DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        "grid_charge_max_soc",
                        default=opts.get("grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC),
                    ): vol.All(vol.Coerce(float), vol.Range(min=50, max=100)),
                    vol.Required(
                        "household_size",
                        default=opts.get("household_size", 4),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                    vol.Optional(
                        "has_pool_pump",
                        default=opts.get("has_pool_pump", False),
                    ): bool,
                    vol.Optional(
                        "executor_enabled",
                        default=opts.get("executor_enabled", False),
                    ): bool,
                    # Household profile (PLAT-962)
                    vol.Optional(
                        "house_size_m2",
                        default=opts.get("house_size_m2", 0),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=500)),
                    vol.Optional(
                        "heating_type",
                        default=opts.get("heating_type", ""),
                    ): vol.In({**HEATING_TYPES, "": "Ej valt"}),
                    vol.Optional(
                        "has_hot_water_heater",
                        default=opts.get("has_hot_water_heater", False),
                    ): bool,
                    vol.Optional(
                        "solar_kwp",
                        default=opts.get("solar_kwp", 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=50)),
                    vol.Optional(
                        "solar_direction",
                        default=opts.get("solar_direction", ""),
                    ): vol.In({**SOLAR_DIRECTIONS, "": "Ej valt"}),
                    vol.Optional(
                        "postal_code",
                        default=opts.get("postal_code", ""),
                    ): str,
                    vol.Optional(
                        "contract_type",
                        default=opts.get("contract_type", ""),
                    ): vol.In({**CONTRACT_TYPES, "": "Ej valt"}),
                    vol.Optional(
                        "electricity_retailer",
                        default=opts.get("electricity_retailer", ""),
                    ): vol.In({**ELECTRICITY_RETAILERS, "": "Ej valt"}),
                }
            ),
        )
