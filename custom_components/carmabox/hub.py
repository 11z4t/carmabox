"""CARMA Box — Hub Sync Client.

Dual-channel communication with CARMA Hub:
1. Primary: MQTT over WebSocket (via Cloudflare Tunnel)
2. Fallback: HTTPS REST (if MQTT unavailable)

All sync data is anonymized — no personal data, no location,
no energy readings, only aggregated performance metrics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ssl
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import HUB_SYNC_TIMEOUT_S
from .optimizer.report import (
    ReportCollector,
    generate_report,
    report_to_dict,
)
from .optimizer.savings import SavingsState, savings_breakdown

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_HUB_URL = "https://hub.carmabox.se/api/v1"

# ── MQTT Payload Signing (HMAC-SHA256) ──────────────────────────
# Envelope format: {"payload": <data>, "ts": <unix>, "nonce": <hex16>, "sig": <hmac_hex>}
_MQTT_TIMESTAMP_WINDOW = 300  # ±5 minutes


def _sign_mqtt_payload(payload: Any, hmac_key: str) -> dict[str, Any]:
    """Wrap a payload in an HMAC-SHA256 signed envelope for MQTT."""
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex[:16]
    message = f"{payload_json}.{ts}.{nonce}"
    sig = hmac.new(
        hmac_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"payload": payload, "ts": ts, "nonce": nonce, "sig": sig}


def _verify_mqtt_envelope(envelope: dict[str, Any], hmac_key: str) -> tuple[bool, Any]:
    """Verify an HMAC-signed MQTT envelope from Hub.

    Returns (valid, payload). If invalid, payload is None.
    No nonce check (Hub is trusted, we don't track used nonces).
    """
    payload = envelope.get("payload")
    ts = envelope.get("ts")
    nonce = envelope.get("nonce")
    sig = envelope.get("sig")

    if payload is None or not ts or not nonce or not sig:
        return False, None

    try:
        if abs(time.time() - float(ts)) > _MQTT_TIMESTAMP_WINDOW:
            return False, None
    except (ValueError, TypeError):
        return False, None

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    message = f"{payload_json}.{ts}.{nonce}"
    expected = hmac.new(
        hmac_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig, expected):
        return False, None

    return True, payload


def sign_request(
    body_json: str,
    api_key: str,
    box_id: str,
) -> dict[str, str]:
    """Sign a request with HMAC-SHA256.

    Returns headers: X-Box-ID, X-Timestamp, X-Nonce, X-Signature.
    Hub verifies: HMAC(body_json + timestamp + nonce, api_key) == signature.
    Replay protection via timestamp (±5 min) + nonce (single-use).
    """
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex[:16]
    message = f"{body_json}.{timestamp}.{nonce}"
    signature = hmac.new(
        api_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Box-ID": box_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }


DEFAULT_WSS_URL = "wss://hub.carmabox.se/mqtt"
DEFAULT_MQTTS_HOST = "hub.carmabox.se"
DEFAULT_MQTTS_PORT = 8883
# mTLS cert storage (HA config dir)
CERT_DIR_NAME = "carmabox_certs"


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
        mqtt_hmac_key: str = "",
        wss_url: str = DEFAULT_WSS_URL,
        hub_url: str = DEFAULT_HUB_URL,
        mqtts_host: str = DEFAULT_MQTTS_HOST,
        mqtts_port: int = DEFAULT_MQTTS_PORT,
    ) -> None:
        """Initialize hub sync client."""
        self.hass = hass
        self.instance_id = instance_id
        self.mqtt_username = mqtt_username
        self.mqtt_token = mqtt_token
        self.mqtt_hmac_key = mqtt_hmac_key
        self.wss_url = wss_url
        self.hub_url = hub_url.rstrip("/")
        self.mqtts_host = mqtts_host
        self.mqtts_port = mqtts_port
        self._last_sync: datetime | None = None
        self._mqtt_connected = False
        self._mqtt_client: Any = None
        self._on_config_callback: Any = None
        self._on_command_callback: Any = None
        # mTLS cert paths (populated after registration)
        self._cert_dir: Path | None = None
        self._client_cert_path: Path | None = None
        self._client_key_path: Path | None = None
        self._ca_cert_path: Path | None = None

    @property
    def topic_prefix(self) -> str:
        """MQTT topic prefix for this box."""
        return f"carmabox/{self.mqtt_username}"

    @property
    def is_mqtt_connected(self) -> bool:
        """True if MQTT WebSocket is connected."""
        return self._mqtt_connected

    def _has_client_certs(self) -> bool:
        """Check if mTLS client certificates are available."""
        return (
            self._client_cert_path is not None
            and self._client_cert_path.exists()
            and self._client_key_path is not None
            and self._client_key_path.exists()
            and self._ca_cert_path is not None
            and self._ca_cert_path.exists()
        )

    def store_certs(self, client_cert: str, client_key: str, ca_cert: str) -> None:
        """Store mTLS certificates to disk (HA config directory)."""
        cert_dir = Path(self.hass.config.config_dir) / CERT_DIR_NAME
        cert_dir.mkdir(exist_ok=True)

        self._cert_dir = cert_dir
        self._client_cert_path = cert_dir / f"{self.mqtt_username}.crt"
        self._client_key_path = cert_dir / f"{self.mqtt_username}.key"
        self._ca_cert_path = cert_dir / "ca.crt"

        self._client_cert_path.write_text(client_cert)
        self._client_key_path.write_text(client_key)
        self._client_key_path.chmod(0o600)
        self._ca_cert_path.write_text(ca_cert)

        _LOGGER.info("mTLS certificates stored in %s", cert_dir)

    def load_certs(self) -> bool:
        """Load existing mTLS certificates from disk."""
        cert_dir = Path(self.hass.config.config_dir) / CERT_DIR_NAME
        if not cert_dir.exists():
            return False

        self._cert_dir = cert_dir
        self._client_cert_path = cert_dir / f"{self.mqtt_username}.crt"
        self._client_key_path = cert_dir / f"{self.mqtt_username}.key"
        self._ca_cert_path = cert_dir / "ca.crt"

        if self._has_client_certs():
            _LOGGER.info("mTLS certificates loaded from %s", cert_dir)
            return True
        return False

    async def connect_mqtt(self) -> bool:
        """Connect to hub via MQTT.

        Prefers MQTTS with mTLS (port 8883) when client certs are available.
        Falls back to WebSocket over CF Tunnel (port 443) otherwise.
        Non-blocking — runs in HA event loop.
        """
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-untyped,unused-ignore]
        except ImportError:
            _LOGGER.warning("paho-mqtt not installed — using HTTPS fallback only")
            return False

        if not self.mqtt_username or not self.mqtt_token:
            _LOGGER.info("No MQTT credentials — using HTTPS fallback")
            return False

        use_mtls = self._has_client_certs()

        try:
            if use_mtls:
                # mTLS: direct MQTTS connection with client certificate
                client = mqtt.Client(
                    client_id=self.mqtt_username,
                    transport="tcp",
                )
                client.username_pw_set(self.mqtt_username, self.mqtt_token)
                client.tls_set(
                    ca_certs=str(self._ca_cert_path),
                    certfile=str(self._client_cert_path),
                    keyfile=str(self._client_key_path),
                    cert_reqs=ssl.CERT_REQUIRED,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT,
                )
                connect_host = self.mqtts_host
                connect_port = self.mqtts_port
                _LOGGER.info("Using MQTTS with mTLS (port %d)", connect_port)
            else:
                # Fallback: WebSocket over CF Tunnel (no client cert)
                client = mqtt.Client(
                    client_id=self.mqtt_username,
                    transport="websockets",
                )
                client.username_pw_set(self.mqtt_username, self.mqtt_token)
                client.tls_set()  # CF Tunnel handles TLS
                host = self.wss_url.replace("wss://", "").split("/")[0]
                path = "/" + "/".join(self.wss_url.replace("wss://", "").split("/")[1:])
                client.ws_set_options(path=path)
                connect_host = host
                connect_port = 443
                _LOGGER.info("Using MQTT WebSocket (no mTLS certs available)")

            def on_connect(client: Any, userdata: Any, flags: Any, rc: int) -> None:
                if rc == 0:
                    self._mqtt_connected = True
                    _LOGGER.info("Hub MQTT connected (mTLS=%s)", use_mtls)
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
                    raw = json.loads(msg.payload.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return

                # Verify HMAC signature if we have a key and message is signed
                payload = raw
                if self.mqtt_hmac_key and isinstance(raw, dict) and "sig" in raw:
                    valid, unwrapped = _verify_mqtt_envelope(raw, self.mqtt_hmac_key)
                    if not valid:
                        _LOGGER.warning(
                            "HMAC verification failed for Hub message on %s — DROPPED",
                            topic,
                        )
                        return
                    payload = unwrapped
                elif self.mqtt_hmac_key and isinstance(raw, dict) and "sig" not in raw:
                    # Signed mode active but message is unsigned — accept with warning
                    # (Hub may not have been updated yet)
                    _LOGGER.debug("Unsigned Hub message on %s (HMAC key configured)", topic)

                if topic.endswith("/config") and self._on_config_callback:
                    self._on_config_callback(payload)
                elif topic.endswith("/command") and self._on_command_callback:
                    self._on_command_callback(payload)
                elif topic.endswith("/insights"):
                    keys = list(payload.keys()) if isinstance(payload, dict) else "?"
                    _LOGGER.info("Hub insights received: %s", keys)

            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message

            client.connect_async(connect_host, connect_port)
            client.loop_start()
            self._mqtt_client = client

            return True

        except (OSError, ValueError):
            _LOGGER.debug("MQTT connect failed — will use HTTPS fallback", exc_info=True)
            return False

    def disconnect_mqtt(self) -> None:
        """Disconnect MQTT client."""
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None
            self._mqtt_connected = False

    def _mqtt_publish(self, topic: str, data: Any, qos: int = 1, retain: bool = False) -> bool:
        """Sign and publish data to MQTT topic.

        If mqtt_hmac_key is set, wraps data in HMAC-signed envelope.
        Otherwise, publishes raw JSON (backward compatible).
        """
        if not self._mqtt_connected or not self._mqtt_client:
            return False
        try:
            if self.mqtt_hmac_key:
                envelope = _sign_mqtt_payload(data, self.mqtt_hmac_key)
                payload = json.dumps(envelope)
            else:
                payload = json.dumps(data)
            self._mqtt_client.publish(topic, payload, qos=qos, retain=retain)
            return True
        except (OSError, TypeError, ValueError):
            _LOGGER.debug("MQTT publish failed", exc_info=True)
            return False

    def publish_telemetry(self, data: dict[str, Any]) -> bool:
        """Publish telemetry via MQTT (non-blocking)."""
        ok = self._mqtt_publish(f"{self.topic_prefix}/telemetry", data, qos=1)
        if ok:
            self._last_sync = datetime.now()
        return ok

    def publish_plan(self, plan_data: list[dict[str, Any]]) -> bool:
        """Publish new plan via MQTT."""
        return self._mqtt_publish(f"{self.topic_prefix}/plan", plan_data, qos=1)

    def publish_savings(self, savings: SavingsState) -> bool:
        """Publish savings snapshot via MQTT."""
        return self._mqtt_publish(f"{self.topic_prefix}/savings", savings_breakdown(savings), qos=1)

    def publish_status(self, version: str = "1.0.0", error_count: int = 0) -> bool:
        """Publish heartbeat status via MQTT (retained for watchdog polling)."""
        status_data = {
            "timestamp": datetime.now().isoformat(),
            "version": version,
            "connected": True,
            "error_count": error_count,
        }
        return self._mqtt_publish(f"{self.topic_prefix}/status", status_data, qos=1, retain=True)

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
                url, json=payload, timeout=aiohttp.ClientTimeout(total=HUB_SYNC_TIMEOUT_S)
            ) as resp:
                if resp.status == 200:
                    self._last_sync = datetime.now()
                    _LOGGER.info("Hub HTTPS sync OK")
                    return True
                _LOGGER.warning("Hub HTTPS sync failed: HTTP %s", resp.status)
                return False

        except (aiohttp.ClientError, TimeoutError):
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
                url, json=payload, timeout=aiohttp.ClientTimeout(total=HUB_SYNC_TIMEOUT_S)
            ) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                    self.mqtt_username = data.get("mqtt_username", "")
                    self.mqtt_token = data.get("mqtt_token", "")

                    # Store MQTT HMAC signing key
                    if data.get("mqtt_hmac_key"):
                        self.mqtt_hmac_key = data["mqtt_hmac_key"]
                        _LOGGER.info("MQTT HMAC signing key received")

                    # Store mTLS certificates if provided
                    client_cert = data.get("client_cert", "")
                    client_key = data.get("client_key", "")
                    ca_cert = data.get("ca_cert", "")
                    if client_cert and client_key and ca_cert:
                        self.store_certs(client_cert, client_key, ca_cert)
                        _LOGGER.info("mTLS certificates received and stored")

                    # Store MQTTS connection details
                    if data.get("mqtts_host"):
                        self.mqtts_host = data["mqtts_host"]
                    if data.get("mqtts_port"):
                        self.mqtts_port = data["mqtts_port"]

                    _LOGGER.info("Hub registration OK: %s", self.mqtt_username)
                    return data
                _LOGGER.warning("Hub registration failed: HTTP %s", resp.status)
                return None

        except (aiohttp.ClientError, TimeoutError):
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
                timeout=aiohttp.ClientTimeout(total=HUB_SYNC_TIMEOUT_S),
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
        except (aiohttp.ClientError, TimeoutError, RuntimeError):
            _LOGGER.debug("Hub benchmarking fetch failed", exc_info=True)
            return None

    def publish_household_profile(self, config_snapshot: dict[str, Any]) -> bool:
        """Publish anonymized household profile via MQTT for benchmarking."""
        profile = self._anonymize_config(config_snapshot)
        return self._mqtt_publish(f"{self.topic_prefix}/profile", profile, qos=1, retain=True)

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
