"""CARMA Box — Hub Sync Client.

Dual-channel communication with CARMA Hub:
1. Primary: MQTT over WebSocket (via Cloudflare Tunnel)
2. Fallback: HTTPS REST (if MQTT unavailable)

All sync data is anonymized — no personal data, no location,
no energy readings, only aggregated performance metrics.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .optimizer.report import (
    ReportCollector,
    generate_report,
    report_to_dict,
)
from .optimizer.savings import SavingsState, savings_breakdown

_LOGGER = logging.getLogger(__name__)

DEFAULT_HUB_URL = "https://hub.carmabox.se/api/v1"
DEFAULT_WSS_URL = "wss://hub.carmabox.se/mqtt"
SYNC_TIMEOUT = 30


class HubSyncClient:
    """Client for CARMA Box hub sync.

    Primary channel: MQTT over WebSocket (bi-directional, real-time).
    Fallback: HTTPS REST (polling, if MQTT down).

    Sends:
    - Telemetry (every 5 min)
    - Plan (on change)
    - Savings (hourly)
    - Monthly report

    Receives (via MQTT subscribe):
    - Config updates
    - Commands (generate_report, force_replan)
    - AI insights
    """

    def __init__(
        self,
        hass: HomeAssistant,
        instance_id: str,
        mqtt_username: str = "",
        mqtt_token: str = "",
        wss_url: str = DEFAULT_WSS_URL,
        hub_url: str = DEFAULT_HUB_URL,
    ) -> None:
        """Initialize hub sync client."""
        self.hass = hass
        self.instance_id = instance_id
        self.mqtt_username = mqtt_username
        self.mqtt_token = mqtt_token
        self.wss_url = wss_url
        self.hub_url = hub_url.rstrip("/")
        self._last_sync: datetime | None = None
        self._mqtt_connected = False
        self._mqtt_client: Any = None
        self._on_config_callback: Any = None
        self._on_command_callback: Any = None

    @property
    def topic_prefix(self) -> str:
        """MQTT topic prefix for this box."""
        return f"carmabox/{self.mqtt_username}"

    @property
    def is_mqtt_connected(self) -> bool:
        """True if MQTT WebSocket is connected."""
        return self._mqtt_connected

    async def connect_mqtt(self) -> bool:
        """Connect to hub via MQTT over WebSocket.

        Uses paho-mqtt with websocket transport through CF Tunnel.
        Non-blocking — runs in HA event loop.
        """
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
        except ImportError:
            _LOGGER.warning("paho-mqtt not installed — using HTTPS fallback only")
            return False

        if not self.mqtt_username or not self.mqtt_token:
            _LOGGER.info("No MQTT credentials — using HTTPS fallback")
            return False

        try:
            client = mqtt.Client(
                client_id=self.mqtt_username,
                transport="websockets",
            )
            client.username_pw_set(self.mqtt_username, self.mqtt_token)
            client.tls_set()  # CF Tunnel handles TLS

            # Parse WSS URL
            # wss://hub.carmabox.se/mqtt → host=hub.carmabox.se, path=/mqtt
            host = self.wss_url.replace("wss://", "").split("/")[0]
            path = "/" + "/".join(self.wss_url.replace("wss://", "").split("/")[1:])

            client.ws_set_options(path=path)

            def on_connect(client: Any, userdata: Any, flags: Any, rc: int) -> None:
                if rc == 0:
                    self._mqtt_connected = True
                    _LOGGER.info("Hub MQTT connected")
                    # Subscribe to incoming topics
                    client.subscribe(f"{self.topic_prefix}/config")
                    client.subscribe(f"{self.topic_prefix}/command")
                    client.subscribe(f"{self.topic_prefix}/insights")
                else:
                    self._mqtt_connected = False
                    _LOGGER.warning("Hub MQTT connect failed: rc=%s", rc)

            def on_disconnect(client: Any, userdata: Any, rc: int) -> None:
                self._mqtt_connected = False
                _LOGGER.info("Hub MQTT disconnected: rc=%s", rc)

            def on_message(client: Any, userdata: Any, msg: Any) -> None:
                topic = msg.topic
                try:
                    payload = json.loads(msg.payload.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return

                if topic.endswith("/config") and self._on_config_callback:
                    self._on_config_callback(payload)
                elif topic.endswith("/command") and self._on_command_callback:
                    self._on_command_callback(payload)
                elif topic.endswith("/insights"):
                    _LOGGER.info("Hub insights received: %s", list(payload.keys()))

            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message

            client.connect_async(host, 443)
            client.loop_start()
            self._mqtt_client = client

            return True

        except Exception:
            _LOGGER.debug("MQTT connect failed — will use HTTPS fallback", exc_info=True)
            return False

    def disconnect_mqtt(self) -> None:
        """Disconnect MQTT client."""
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None
            self._mqtt_connected = False

    def publish_telemetry(self, data: dict[str, Any]) -> bool:
        """Publish telemetry via MQTT (non-blocking)."""
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            payload = json.dumps(data)
            self._mqtt_client.publish(f"{self.topic_prefix}/telemetry", payload, qos=1)
            self._last_sync = datetime.now()
            return True
        except Exception:
            _LOGGER.debug("MQTT publish failed", exc_info=True)
            return False

    def publish_plan(self, plan_data: list[dict[str, Any]]) -> bool:
        """Publish new plan via MQTT."""
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            payload = json.dumps(plan_data)
            self._mqtt_client.publish(f"{self.topic_prefix}/plan", payload, qos=1)
            return True
        except Exception:
            _LOGGER.debug("MQTT publish failed", exc_info=True)
            return False

    def publish_savings(self, savings: SavingsState) -> bool:
        """Publish savings snapshot via MQTT."""
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            payload = json.dumps(savings_breakdown(savings))
            self._mqtt_client.publish(f"{self.topic_prefix}/savings", payload, qos=1)
            return True
        except Exception:
            _LOGGER.debug("MQTT publish failed", exc_info=True)
            return False

    def publish_status(self, version: str = "1.0.0", error_count: int = 0) -> bool:
        """Publish heartbeat status via MQTT."""
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            payload = json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "version": version,
                    "connected": True,
                    "error_count": error_count,
                }
            )
            self._mqtt_client.publish(f"{self.topic_prefix}/status", payload, qos=0)
            return True
        except Exception:
            _LOGGER.debug("MQTT publish failed", exc_info=True)
            return False

    # ── HTTPS Fallback ──────────────────────────────────────

    async def sync_daily(
        self,
        savings: SavingsState,
        report_collector: ReportCollector,
        config_snapshot: dict[str, Any],
    ) -> bool:
        """HTTPS fallback: send daily sync."""
        try:
            breakdown = savings_breakdown(savings)
            report = generate_report(report_collector)
            report_data = report_to_dict(report)

            payload = {
                "instance_id": self.instance_id,
                "timestamp": datetime.now().isoformat(),
                "savings": breakdown,
                "report": report_data,
                "config": self._anonymize_config(config_snapshot),
            }

            session = async_get_clientsession(self.hass)
            url = f"{self.hub_url}/sync"

            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    self._last_sync = datetime.now()
                    _LOGGER.info("Hub HTTPS sync OK")
                    return True
                _LOGGER.warning("Hub HTTPS sync failed: HTTP %s", resp.status)
                return False

        except Exception:
            _LOGGER.debug("Hub HTTPS sync failed", exc_info=True)
            return False

    async def register(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """Register this box with the hub. Returns MQTT credentials."""
        try:
            payload = {
                "instance_id": self.instance_id,
                **self._anonymize_config(config),
            }

            session = async_get_clientsession(self.hass)
            url = f"{self.hub_url}/register"

            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                    self.mqtt_username = data.get("mqtt_username", "")
                    self.mqtt_token = data.get("mqtt_token", "")
                    _LOGGER.info("Hub registration OK: %s", self.mqtt_username)
                    return data
                _LOGGER.warning("Hub registration failed: HTTP %s", resp.status)
                return None

        except Exception:
            _LOGGER.debug("Hub registration failed", exc_info=True)
            return None

    @property
    def last_sync(self) -> datetime | None:
        """Last successful sync timestamp."""
        return self._last_sync

    async def fetch_benchmarking(self, config_snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch benchmarking data from hub for this box.

        Sends anonymized profile, receives comparison against similar households.
        Returns None if hub unreachable or <10 similar households.
        """
        try:
            profile = self._anonymize_config(config_snapshot)
            session = async_get_clientsession(self.hass)
            url = f"{self.hub_url}/benchmarking/{self.instance_id}"

            async with session.post(
                url,
                json={"profile": profile},
                timeout=aiohttp.ClientTimeout(total=SYNC_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                    if data.get("similar_households", 0) >= 10:
                        return data
                    _LOGGER.debug(
                        "Benchmarking: only %d similar households (need 10+)",
                        data.get("similar_households", 0),
                    )
                    return data
                return None
        except Exception:
            _LOGGER.debug("Hub benchmarking fetch failed", exc_info=True)
            return None

    def publish_household_profile(self, config_snapshot: dict[str, Any]) -> bool:
        """Publish anonymized household profile via MQTT for benchmarking."""
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            profile = self._anonymize_config(config_snapshot)
            payload = json.dumps(profile)
            self._mqtt_client.publish(f"{self.topic_prefix}/profile", payload, qos=1, retain=True)
            return True
        except Exception:
            _LOGGER.debug("MQTT profile publish failed", exc_info=True)
            return False

    @staticmethod
    def _anonymize_config(config: dict[str, Any]) -> dict[str, Any]:
        """Remove personally identifiable data from config."""
        safe_keys = {
            "price_area",
            "grid_operator",
            "household_size",
            "has_pool_pump",
            "ev_enabled",
            "ev_model",
            "ev_capacity_kwh",
            "ev_night_target_soc",
            "ev_full_charge_days",
            "target_weighted_kw",
            "min_soc",
            "peak_cost_per_kw",
            "fallback_price_ore",
            "grid_charge_price_threshold",
            "grid_charge_max_soc",
            # PLAT-962: Household profile (anonymized)
            "house_size_m2",
            "heating_type",
            "has_hot_water_heater",
            "solar_kwp",
            "solar_direction",
            "solar_tilt",
            "battery_brand",
            "battery_count",
            "contract_type",
            "electricity_retailer",
            # postal_code: only first 3 digits for privacy
        }
        result = {k: v for k, v in config.items() if k in safe_keys}
        # Anonymize postal code — keep only first 3 digits (area, not street)
        postal = str(config.get("postal_code", ""))
        if len(postal) >= 3:
            result["postal_area"] = postal[:3]
        return result
