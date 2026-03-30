"""CARMA Box — Repairs.

Self-healing repair flows for common issues:
1. SafetyGuard blocking frequently → suggest increasing min_soc or check temperature
2. Hub offline >24h → suggest checking internet connection
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from .const import DEFAULT_BATTERY_MIN_SOC, DOMAIN

if TYPE_CHECKING:
    from homeassistant import data_entry_flow
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Thresholds for issue detection
SAFETY_BLOCK_THRESHOLD = 20  # blocks/hour before raising issue
HUB_OFFLINE_THRESHOLD_S = 86400  # 24 hours

try:
    import voluptuous as vol
    from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow

    class SafetyGuardRepairFlow(RepairsFlow):
        """Repair flow for frequent SafetyGuard blocks."""

        async def async_step_init(
            self,
            user_input: dict[str, Any] | None = None,
        ) -> data_entry_flow.FlowResult:
            """Handle the first step."""
            return await self.async_step_confirm()

        async def async_step_confirm(
            self,
            user_input: dict[str, Any] | None = None,
        ) -> data_entry_flow.FlowResult:
            """Let user choose a fix action."""
            if user_input is not None:
                action = user_input.get("action", "acknowledge")
                if action == "increase_min_soc":
                    await self._increase_min_soc()
                return self.async_create_entry(data={})

            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required("action", default="acknowledge"): vol.In(
                            {
                                "increase_min_soc": "Increase minimum SoC by 5%",
                                "acknowledge": "I'll check manually",
                            }
                        ),
                    }
                ),
                description_placeholders=self._get_placeholders(),
            )

        async def _increase_min_soc(self) -> None:
            """Increase min_soc by 5% in config options."""
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if not entries:
                return
            entry = entries[0]
            current = float(entry.options.get("min_soc", DEFAULT_BATTERY_MIN_SOC))
            new_soc = min(current + 5.0, 50.0)
            new_options = {**entry.options, "min_soc": new_soc}
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info("Repair: increased min_soc from %.0f%% to %.0f%%", current, new_soc)

        def _get_placeholders(self) -> dict[str, str]:
            """Get description placeholders."""
            entries = self.hass.config_entries.async_entries(DOMAIN)
            current_soc = DEFAULT_BATTERY_MIN_SOC
            if entries:
                current_soc = float(entries[0].options.get("min_soc", DEFAULT_BATTERY_MIN_SOC))
            return {"current_min_soc": f"{current_soc:.0f}"}

    class HubOfflineRepairFlow(ConfirmRepairFlow):
        """Repair flow for hub offline >24h."""

    async def async_create_fix_flow(
        hass: HomeAssistant,
        issue_id: str,
        data: dict[str, Any] | None,
    ) -> RepairsFlow:
        """Create repair flows for CARMA Box issues."""
        if issue_id == "safety_guard_frequent_blocks":
            return SafetyGuardRepairFlow()
        return HubOfflineRepairFlow()

except ImportError:
    _LOGGER.debug("HA repairs platform not available — repair flows disabled")


def raise_safety_guard_issue(hass: HomeAssistant, blocks_per_hour: int) -> None:
    """Raise a repair issue when SafetyGuard blocks too frequently."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "safety_guard_frequent_blocks",
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="safety_guard_frequent_blocks",
        translation_placeholders={"blocks_per_hour": str(blocks_per_hour)},
    )


def raise_hub_offline_issue(hass: HomeAssistant, hours_offline: int) -> None:
    """Raise a repair issue when hub has been offline >24h."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "hub_offline",
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="hub_offline",
        translation_placeholders={"hours_offline": str(hours_offline)},
    )


def clear_issue(hass: HomeAssistant, issue_id: str) -> None:
    """Clear a repair issue when condition resolves."""
    ir.async_delete_issue(hass, DOMAIN, issue_id)
