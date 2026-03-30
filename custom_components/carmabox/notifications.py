"""CARMA Box — Notification engine.

Sends notifications via HA services (notify, rest_command).
All energy-related Slack/push messages should originate here,
NOT from separate HA automations.

For customer deployments: notifications go via Hub (MQTT → Hub → email/push).
For our dev HA: notifications go directly to Slack + push.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Throttle: max 1 notification per type per interval (seconds)
_THROTTLE: dict[str, float] = {}
_THROTTLE_INTERVALS: dict[str, int] = {
    "battery_full": 3600,  # 1h
    "low_soc": 3600,  # 1h
    "discharge_blocked": 14400,  # 4h
    "miner_started": 1800,  # 30m
    "miner_stopped": 1800,  # 30m
    "morning_report": 86400,  # 24h
    "ev_started": 3600,  # 1h
    "ev_stopped": 3600,  # 1h
    "ev_target_reached": 3600,  # 1h
    "crosscharge_alert": 300,  # 5m
    "safety_block": 1800,  # 30m
    "proactive_discharge": 3600,  # 1h
}


def _throttled(msg_type: str) -> bool:
    """Check if this message type is throttled."""
    now = datetime.now().timestamp()
    interval = _THROTTLE_INTERVALS.get(msg_type, 1800)
    last = _THROTTLE.get(msg_type, 0)
    if now - last < interval:
        return True
    _THROTTLE[msg_type] = now
    return False


class CarmaNotifier:
    """Sends CARMA Box notifications via HA services."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self._push_entity = config.get("notify_push_entity", "notify.mobile_app_bmq_iphone")
        self._slack_service = config.get("notify_slack_service", "rest_command.slack_pv_notify")
        self._enabled = config.get("notifications_enabled", True)

    async def _send_push(self, title: str, message: str) -> None:
        """Send push notification."""
        if not self._enabled:
            return
        try:
            domain, service = self._push_entity.split(".", 1)
            await self.hass.services.async_call(
                domain,
                service,
                {"title": title, "message": message},
            )
        except Exception as e:
            _LOGGER.debug("Push notification failed: %s", e)

    async def _send_slack(self, message: str, icon: str = ":zap:") -> None:
        """Send Slack notification via rest_command."""
        if not self._enabled:
            return
        try:
            parts = self._slack_service.split(".", 1)
            if len(parts) == 2:
                await self.hass.services.async_call(
                    parts[0],
                    parts[1],
                    {"message": message, "icon": icon},
                )
        except Exception as e:
            _LOGGER.debug("Slack notification failed: %s", e)

    # ── Battery notifications ──────────────────────────────

    async def battery_full(
        self,
        inverter: str,
        soc: float,
    ) -> None:
        """Battery reached 100%."""
        if _throttled("battery_full"):
            return
        msg = f":white_check_mark: *{inverter} batteri fullt*\nSoC: {soc:.0f}%"
        await self._send_slack(msg, ":white_check_mark:")

    async def low_soc_warning(
        self,
        inverter: str,
        soc: float,
        min_soc: float,
    ) -> None:
        """Battery SoC dropped below minimum."""
        if _throttled("low_soc"):
            return
        msg = (
            f":warning: *Låg SoC — {inverter}*\n"
            f"SoC: {soc:.0f}% (min: {min_soc:.0f}%)\n"
            f"Urladdning stoppad"
        )
        await self._send_slack(msg, ":warning:")
        await self._send_push("Lågt batteri", f"{inverter}: {soc:.0f}%")

    async def discharge_blocked(self, reason: str) -> None:
        """Discharge blocked by safety guard."""
        if _throttled("discharge_blocked"):
            return
        msg = f":no_entry_sign: *Urladdning blockerad*\n{reason}"
        await self._send_slack(msg, ":no_entry_sign:")

    async def proactive_discharge_started(
        self,
        watts: int,
        soc: float,
        grid_w: float,
        pv_kw: float,
    ) -> None:
        """Proactive discharge started (high SoC + grid import)."""
        if _throttled("proactive_discharge"):
            return
        msg = (
            f":battery: *Proaktiv urladdning {watts}W*\n"
            f"SoC: {soc:.0f}%, Grid: {grid_w:.0f}W, PV: {pv_kw:.1f} kW\n"
            f"Eliminerar nätimport — solen fyller tillbaka"
        )
        await self._send_slack(msg, ":battery:")

    # ── Miner notifications ────────────────────────────────

    async def miner_started(
        self,
        reason: str,
        soc: float,
        price_ore: float,
    ) -> None:
        """Miner turned on."""
        if _throttled("miner_started"):
            return
        msg = f":pick: *Miner startad*\nOrsak: {reason}\nSoC: {soc:.0f}%, Pris: {price_ore:.0f} öre"
        await self._send_slack(msg, ":pick:")

    async def miner_stopped(
        self,
        reason: str,
        soc: float,
        price_ore: float,
    ) -> None:
        """Miner turned off."""
        if _throttled("miner_stopped"):
            return
        msg = (
            f":octagonal_sign: *Miner stoppad*\n"
            f"Orsak: {reason}\n"
            f"SoC: {soc:.0f}%, Pris: {price_ore:.0f} öre"
        )
        await self._send_slack(msg, ":octagonal_sign:")

    # ── EV notifications ───────────────────────────────────

    async def ev_started(
        self,
        amps: int,
        soc: float,
        target: float,
    ) -> None:
        """EV charging started."""
        if _throttled("ev_started"):
            return
        msg = f":electric_plug: *EV-laddning startad*\n{amps}A, SoC: {soc:.0f}% → mål {target:.0f}%"
        await self._send_slack(msg, ":electric_plug:")

    async def ev_target_reached(self, soc: float) -> None:
        """EV reached target SoC."""
        if _throttled("ev_target_reached"):
            return
        msg = f":checkered_flag: *EV mål nått — {soc:.0f}%*"
        await self._send_slack(msg, ":checkered_flag:")

    # ── Safety notifications ───────────────────────────────

    async def crosscharge_alert(
        self,
        battery_1_w: float,
        battery_2_w: float,
    ) -> None:
        """Crosscharge detected."""
        if _throttled("crosscharge_alert"):
            return
        msg = (
            f":rotating_light: *Korsladding detekterad!*\n"
            f"Kontor: {battery_1_w:.0f}W, Förråd: {battery_2_w:.0f}W\n"
            f"Båda satta i Standby"
        )
        await self._send_slack(msg, ":rotating_light:")
        await self._send_push(
            "Korsladding!",
            f"Kontor: {battery_1_w:.0f}W, Förråd: {battery_2_w:.0f}W",
        )

    async def safety_block(self, reason: str) -> None:
        """Safety guard blocked an action."""
        if _throttled("safety_block"):
            return
        msg = f":shield: *SafetyGuard blockerade*\n{reason}"
        await self._send_slack(msg, ":shield:")

    # ── Morning report ─────────────────────────────────────

    async def morning_report(
        self,
        soc_kontor: float,
        soc_forrad: float,
        ev_soc: float,
        yesterday_cost_kr: float,
        yesterday_saved_kr: float,
        price_now_ore: float,
    ) -> None:
        """Daily morning battery report at 06:00."""
        if _throttled("morning_report"):
            return
        msg = (
            f":sunrise: *Morgonrapport — Batteristatus*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Kontor:* {soc_kontor:.0f}%\n"
            f"*Förråd:* {soc_forrad:.0f}%\n"
            f"*EV:* {ev_soc:.0f}%\n"
            f"*Igår:* {yesterday_cost_kr:.0f} kr "
            f"(sparat {yesterday_saved_kr:.0f} kr)\n"
            f"*Pris nu:* {price_now_ore:.0f} öre/kWh\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_"
        )
        await self._send_slack(msg, ":sunrise:")
