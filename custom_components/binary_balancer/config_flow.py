"""Config flow for Binary Balancer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    ASSET_MINER,
    ASSET_POOL_ELV,
    ASSET_POOL_VP,
    CONF_ASSET_ID,
    CONF_HYSTERESIS_OFF_PCT,
    CONF_HYSTERESIS_ON_PCT,
    CONF_MIN_DWELL_S,
    CONF_SEASON_ACTIVE_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_TYPICAL_DRAG_W,
    DEFAULT_HYSTERESIS_OFF_PCT,
    DEFAULT_HYSTERESIS_ON_PCT,
    DEFAULT_MIN_DWELL_S,
    DEFAULT_TYPICAL_DRAG_W,
    DOMAIN,
    VALID_ASSET_IDS,
)

ASSET_OPTIONS = [ASSET_POOL_VP, ASSET_POOL_ELV, ASSET_MINER]

STEP_ASSET_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ASSET_ID): vol.In(ASSET_OPTIONS),
    }
)


def _entity_schema(asset_id: str) -> vol.Schema:
    """Return entity step schema pre-filled with typical_drag_w for asset."""
    return vol.Schema(
        {
            vol.Required(
                CONF_TYPICAL_DRAG_W,
                default=DEFAULT_TYPICAL_DRAG_W[asset_id],
            ): vol.Coerce(float),
            vol.Required(CONF_SWITCH_ENTITY): str,
            vol.Optional(CONF_SEASON_ACTIVE_ENTITY, default=""): str,
        }
    )


_ADVANCED_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MIN_DWELL_S, default=DEFAULT_MIN_DWELL_S): vol.Coerce(int),
        vol.Required(CONF_HYSTERESIS_ON_PCT, default=DEFAULT_HYSTERESIS_ON_PCT): vol.Coerce(float),
        vol.Required(CONF_HYSTERESIS_OFF_PCT, default=DEFAULT_HYSTERESIS_OFF_PCT): vol.Coerce(
            float
        ),
    }
)


class BinaryBalancerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Binary Balancer."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: select asset_id."""
        errors: dict[str, str] = {}

        if user_input is not None:
            asset_id = user_input[CONF_ASSET_ID]
            if asset_id not in VALID_ASSET_IDS:
                errors[CONF_ASSET_ID] = "invalid_asset_id"
            else:
                self._data[CONF_ASSET_ID] = asset_id
                return await self.async_step_entity()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_ASSET_SCHEMA,
            errors=errors,
        )

    async def async_step_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: switch entity + optional season entity."""
        errors: dict[str, str] = {}
        asset_id: str = self._data[CONF_ASSET_ID]

        if user_input is not None:
            self._data[CONF_TYPICAL_DRAG_W] = user_input[CONF_TYPICAL_DRAG_W]
            self._data[CONF_SWITCH_ENTITY] = user_input[CONF_SWITCH_ENTITY]
            season = user_input.get(CONF_SEASON_ACTIVE_ENTITY, "").strip()
            self._data[CONF_SEASON_ACTIVE_ENTITY] = season if season else None
            return await self.async_step_advanced()

        return self.async_show_form(
            step_id="entity",
            data_schema=_entity_schema(asset_id),
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: dwell + hysteresis settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            on_pct: float = user_input[CONF_HYSTERESIS_ON_PCT]
            off_pct: float = user_input[CONF_HYSTERESIS_OFF_PCT]
            if on_pct <= off_pct:
                errors[CONF_HYSTERESIS_ON_PCT] = "on_pct_must_exceed_off_pct"
            else:
                self._data.update(user_input)
                asset_id = self._data[CONF_ASSET_ID]
                return self.async_create_entry(
                    title=f"Binary Balancer — {asset_id}",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="advanced",
            data_schema=_ADVANCED_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BinaryBalancerOptionsFlow:
        """Return options flow handler."""
        return BinaryBalancerOptionsFlow(config_entry)


class BinaryBalancerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Binary Balancer (edit dwell + hysteresis)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            on_pct: float = user_input[CONF_HYSTERESIS_ON_PCT]
            off_pct: float = user_input[CONF_HYSTERESIS_OFF_PCT]
            if on_pct <= off_pct:
                errors[CONF_HYSTERESIS_ON_PCT] = "on_pct_must_exceed_off_pct"
            else:
                return self.async_create_entry(title="", data=user_input)

        data = self._config_entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MIN_DWELL_S,
                    default=data.get(CONF_MIN_DWELL_S, DEFAULT_MIN_DWELL_S),
                ): vol.Coerce(int),
                vol.Required(
                    CONF_HYSTERESIS_ON_PCT,
                    default=data.get(CONF_HYSTERESIS_ON_PCT, DEFAULT_HYSTERESIS_ON_PCT),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_HYSTERESIS_OFF_PCT,
                    default=data.get(CONF_HYSTERESIS_OFF_PCT, DEFAULT_HYSTERESIS_OFF_PCT),
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
