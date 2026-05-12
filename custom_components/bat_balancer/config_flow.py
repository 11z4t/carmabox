"""Config flow for bat_balancer — auto-setup, no user input required."""

from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN


class BatBalancerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-step config flow: confirm and create entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Battery Balancer v3", data={})
        return self.async_show_form(step_id="user")
