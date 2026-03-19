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
    "eon": {"name": "E.ON Energidistribution", "cost_per_kw": 70, "top_n": 1, "night_weight": 1.0},
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
                "detected": "\n".join(detected_text) if detected_text else "Inget hittad",
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
            return await self.async_step_summary()

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

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 6: Onboarding summary — show what was found and what happens next."""
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
                # Nordpool reports in SEK/kWh — convert to öre
                if price < 20:
                    price = price * 100
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
            # Mode — analyzer only by default (no battery commands sent)
            "executor_enabled": self._user_input.get("executor_enabled", False),
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
        _exclude = ("total", "imbalance", "available", "trend", "charging", "discharging", "mode")
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
                    battery_power_entities, suffix, f"sensor.goodwe_battery_power_{suffix}"
                )
                mappings[f"battery_ems_{i}"] = self._find_by_suffix(
                    battery_ems_entities, suffix, f"select.goodwe_{suffix}_ems_mode"
                )
                mappings[f"battery_limit_{i}"] = self._find_by_suffix(
                    battery_limit_entities, suffix, f"number.goodwe_{suffix}_peak_shaving_power"
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
            ev_prefix = ev_chargers[0].get("prefix", "easee")
            mappings["ev_prefix"] = ev_prefix
            mappings["ev_status_entity"] = f"sensor.{ev_prefix}_status"
            mappings["ev_current_entity"] = f"sensor.{ev_prefix}_current"
            mappings["ev_power_entity"] = f"sensor.{ev_prefix}_power"
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

    def _detect_easee_charger_id(self, ev_prefix: str) -> str:
        """Auto-detect Easee charger ID from status entity attributes."""
        status_entity = f"sensor.{ev_prefix}_status"
        state = self.hass.states.get(status_entity)
        if state and state.attributes:
            charger_id = state.attributes.get("id", "")
            if charger_id:
                return str(charger_id)
        return ""

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
                detected["inverters"].append(
                    {
                        "domain": domain,
                        "name": INVERTER_DOMAINS[domain],
                        "entry_id": entry.entry_id,
                        "prefix": entry.title.lower().replace(" ", "_") if entry.title else domain,
                    }
                )

            elif domain in EV_DOMAINS:
                detected["ev_chargers"].append(
                    {
                        "domain": domain,
                        "name": EV_DOMAINS[domain],
                        "entry_id": entry.entry_id,
                        "prefix": f"{domain}_{entry.title}" if entry.title else domain,
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
                            "grid_charge_price_threshold", DEFAULT_GRID_CHARGE_PRICE_THRESHOLD
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
                }
            ),
        )
