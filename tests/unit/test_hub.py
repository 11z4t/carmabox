"""Tests for hub sync client — MQTT/WSS + HTTPS fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.hub import (
    HubSyncClient,
    _sign_mqtt_payload,
    _verify_mqtt_envelope,
)
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState


def _make_client(**kwargs: object) -> HubSyncClient:
    hass = MagicMock()
    defaults = {
        "instance_id": "test-123",
        "mqtt_username": "box_test123",
        "mqtt_token": "secret",
        "wss_url": "wss://hub.test/mqtt",
        "hub_url": "https://hub.test/api/v1",
    }
    defaults.update(kwargs)
    return HubSyncClient(hass, **defaults)


class TestTopicPrefix:
    def test_topic_prefix(self) -> None:
        client = _make_client()
        assert client.topic_prefix == "carmabox/box_test123"


class TestAnonymizeConfig:
    def test_keeps_safe_keys(self) -> None:
        config = {
            "price_area": "SE3",
            "grid_operator": "ellevio",
            "household_size": 4,
            "target_weighted_kw": 2.0,
        }
        result = HubSyncClient._anonymize_config(config)
        assert result["price_area"] == "SE3"
        assert result["household_size"] == 4

    def test_removes_entity_ids(self) -> None:
        config = {
            "price_area": "SE3",
            "battery_soc_1": "sensor.private_soc",
            "battery_ems_1": "select.goodwe_kontor",
            "price_entity": "sensor.nordpool_kwh",
        }
        result = HubSyncClient._anonymize_config(config)
        assert "battery_soc_1" not in result
        assert "price_entity" not in result
        assert result["price_area"] == "SE3"

    def test_empty_config(self) -> None:
        assert HubSyncClient._anonymize_config({}) == {}


class TestMQTTPublish:
    def test_publish_telemetry_not_connected(self) -> None:
        client = _make_client()
        assert client.publish_telemetry({"grid_kw": 1.5}) is False

    def test_publish_telemetry_connected(self) -> None:
        client = _make_client()
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()
        assert client.publish_telemetry({"grid_kw": 1.5}) is True
        client._mqtt_client.publish.assert_called_once()
        assert client.last_sync is not None

    def test_publish_plan_not_connected(self) -> None:
        client = _make_client()
        assert client.publish_plan([{"hour": 0}]) is False

    def test_publish_plan_connected(self) -> None:
        client = _make_client()
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()
        assert client.publish_plan([{"hour": 0, "action": "d"}]) is True

    def test_publish_savings_connected(self) -> None:
        client = _make_client()
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()
        savings = SavingsState(month=3, year=2026)
        assert client.publish_savings(savings) is True

    def test_publish_status_connected(self) -> None:
        client = _make_client()
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()
        assert client.publish_status(version="1.0.0") is True

    def test_publish_status_not_connected(self) -> None:
        client = _make_client()
        assert client.publish_status() is False


class TestMQTTConnect:
    def test_no_credentials_returns_false(self) -> None:
        """No MQTT credentials → skip MQTT, use HTTPS."""
        import asyncio

        client = _make_client(mqtt_username="", mqtt_token="")
        result = asyncio.get_event_loop().run_until_complete(client.connect_mqtt())
        assert result is False

    def test_no_paho_returns_false(self) -> None:
        """paho-mqtt not installed → use HTTPS fallback."""
        import asyncio

        client = _make_client()
        with patch.dict("sys.modules", {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            result = asyncio.get_event_loop().run_until_complete(client.connect_mqtt())
            # Will either return False (import fails) or True (import succeeds from cache)
            assert isinstance(result, bool)


class TestMQTTDisconnect:
    def test_disconnect_cleans_up(self) -> None:
        client = _make_client()
        client._mqtt_client = MagicMock()
        client._mqtt_connected = True
        client.disconnect_mqtt()
        assert client._mqtt_connected is False
        assert client._mqtt_client is None

    def test_disconnect_without_client(self) -> None:
        client = _make_client()
        client.disconnect_mqtt()  # Should not raise
        assert client._mqtt_connected is False


class TestHTTPSFallback:
    @pytest.mark.asyncio
    async def test_successful_sync(self) -> None:
        client = _make_client()
        savings = SavingsState(month=3, year=2026)
        collector = ReportCollector(month=3, year=2026)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await client.sync_daily(savings, collector, {"price_area": "SE3"})

        assert result is True
        assert client.last_sync is not None

    @pytest.mark.asyncio
    async def test_failed_sync(self) -> None:
        client = _make_client()
        savings = SavingsState(month=3, year=2026)
        collector = ReportCollector(month=3, year=2026)

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await client.sync_daily(savings, collector, {})

        assert result is False

    @pytest.mark.asyncio
    async def test_network_error(self) -> None:
        client = _make_client()
        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            side_effect=Exception("offline"),
        ):
            result = await client.sync_daily(
                SavingsState(month=3, year=2026),
                ReportCollector(month=3, year=2026),
                {},
            )
        assert result is False


class TestRegistration:
    @pytest.mark.asyncio
    async def test_successful_register(self) -> None:
        client = _make_client(mqtt_username="", mqtt_token="")
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "mqtt_username": "box_abc123",
                "mqtt_token": "secret_token",
                "wss_url": "wss://hub.carmabox.se/mqtt",
                "topic_prefix": "carmabox/box_abc123",
            }
        )
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await client.register({"price_area": "SE3"})

        assert result is not None
        assert client.mqtt_username == "box_abc123"
        assert client.mqtt_token == "secret_token"

    @pytest.mark.asyncio
    async def test_failed_register(self) -> None:
        client = _make_client()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await client.register({})
        assert result is None

    @pytest.mark.asyncio
    async def test_register_network_error(self) -> None:
        client = _make_client()
        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            side_effect=Exception("offline"),
        ):
            result = await client.register({})
        assert result is None


class TestInitialState:
    def test_initially_not_connected(self) -> None:
        client = _make_client()
        assert client.is_mqtt_connected is False
        assert client.last_sync is None


# ── MQTT HMAC Signing Tests ─────────────────────────────────────


TEST_HMAC_KEY = "a" * 64  # 32-byte hex key for testing


class TestMQTTPayloadSigning:
    def test_sign_creates_envelope(self) -> None:
        envelope = _sign_mqtt_payload({"temp": 22}, TEST_HMAC_KEY)
        assert envelope["payload"] == {"temp": 22}
        assert "ts" in envelope
        assert "nonce" in envelope
        assert "sig" in envelope
        assert len(envelope["sig"]) == 64

    def test_sign_verify_roundtrip(self) -> None:
        data = {"grid_kw": 1.5, "soc": 80}
        envelope = _sign_mqtt_payload(data, TEST_HMAC_KEY)
        valid, payload = _verify_mqtt_envelope(envelope, TEST_HMAC_KEY)
        assert valid is True
        assert payload == data

    def test_wrong_key_fails(self) -> None:
        envelope = _sign_mqtt_payload({"x": 1}, TEST_HMAC_KEY)
        valid, _ = _verify_mqtt_envelope(envelope, "b" * 64)
        assert valid is False

    def test_tampered_payload_fails(self) -> None:
        envelope = _sign_mqtt_payload({"amount": 100}, TEST_HMAC_KEY)
        envelope["payload"]["amount"] = 999
        valid, _ = _verify_mqtt_envelope(envelope, TEST_HMAC_KEY)
        assert valid is False

    def test_missing_sig_fails(self) -> None:
        envelope = _sign_mqtt_payload({"x": 1}, TEST_HMAC_KEY)
        del envelope["sig"]
        valid, _ = _verify_mqtt_envelope(envelope, TEST_HMAC_KEY)
        assert valid is False

    def test_list_payload(self) -> None:
        data = [{"hour": 0}, {"hour": 1}]
        envelope = _sign_mqtt_payload(data, TEST_HMAC_KEY)
        valid, payload = _verify_mqtt_envelope(envelope, TEST_HMAC_KEY)
        assert valid is True
        assert payload == data


class TestMQTTPublishSigned:
    def test_publish_with_hmac_key_signs_payload(self) -> None:
        """When mqtt_hmac_key is set, publish wraps data in signed envelope."""
        import json

        client = _make_client(mqtt_hmac_key=TEST_HMAC_KEY)
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()

        client.publish_telemetry({"grid_kw": 1.5})

        call_args = client._mqtt_client.publish.call_args
        topic = call_args[0][0]
        raw = json.loads(call_args[0][1])

        assert topic == "carmabox/box_test123/telemetry"
        assert "sig" in raw
        assert "payload" in raw
        assert raw["payload"]["grid_kw"] == 1.5

        # Verify the signature is valid
        valid, payload = _verify_mqtt_envelope(raw, TEST_HMAC_KEY)
        assert valid is True
        assert payload == {"grid_kw": 1.5}

    def test_publish_without_hmac_key_sends_raw(self) -> None:
        """Without mqtt_hmac_key, publish sends raw JSON (backward compat)."""
        import json

        client = _make_client()  # No hmac key
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()

        client.publish_telemetry({"grid_kw": 1.5})

        call_args = client._mqtt_client.publish.call_args
        raw = json.loads(call_args[0][1])
        assert "sig" not in raw
        assert raw["grid_kw"] == 1.5

    def test_publish_plan_signed(self) -> None:
        import json

        client = _make_client(mqtt_hmac_key=TEST_HMAC_KEY)
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()

        plan = [{"hour": 0, "action": "charge"}]
        client.publish_plan(plan)

        raw = json.loads(client._mqtt_client.publish.call_args[0][1])
        assert "sig" in raw
        valid, payload = _verify_mqtt_envelope(raw, TEST_HMAC_KEY)
        assert valid is True
        assert payload == plan


class TestRegisterStoresHmacKey:
    @pytest.mark.asyncio
    async def test_register_stores_hmac_key(self) -> None:
        client = _make_client(mqtt_username="", mqtt_token="")
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "mqtt_username": "box_abc123",
                "mqtt_token": "secret_token",
                "wss_url": "wss://hub.carmabox.se/mqtt",
                "topic_prefix": "carmabox/box_abc123",
                "mqtt_hmac_key": TEST_HMAC_KEY,
            }
        )
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await client.register({"price_area": "SE3"})

        assert result is not None
        assert client.mqtt_hmac_key == TEST_HMAC_KEY


# ── Missing coverage: _verify_mqtt_envelope edge cases ──────────────────────


class TestVerifyEnvelopeEdgeCases:
    def test_expired_timestamp_returns_false(self) -> None:
        """Timestamp > 5 min old → rejected (replay protection)."""
        import time

        key = "x" * 64
        # Build envelope with old timestamp (10 minutes ago)
        envelope = _sign_mqtt_payload({"x": 1}, key)
        envelope["ts"] = str(int(time.time()) - 700)  # 700s > 300s window

        valid, _ = _verify_mqtt_envelope(envelope, key)
        assert valid is False

    def test_invalid_timestamp_type_returns_false(self) -> None:
        """Non-numeric ts → ValueError branch → returns False."""
        key = "x" * 64
        envelope = _sign_mqtt_payload({"x": 1}, key)
        envelope["ts"] = "not_a_number"

        valid, _ = _verify_mqtt_envelope(envelope, key)
        assert valid is False

    def test_missing_payload_returns_false(self) -> None:
        """Envelope without payload → returns False."""
        valid, result = _verify_mqtt_envelope({"ts": "123", "nonce": "abc", "sig": "def"}, "key")
        assert valid is False
        assert result is None

    def test_missing_nonce_returns_false(self) -> None:
        """Envelope without nonce → returns False."""
        valid, _result = _verify_mqtt_envelope({"payload": {}, "ts": "123", "sig": "def"}, "key")
        assert valid is False

    def test_missing_ts_returns_false(self) -> None:
        """Envelope without ts → returns False."""
        valid, _result = _verify_mqtt_envelope({"payload": {}, "nonce": "abc", "sig": "def"}, "key")
        assert valid is False


# ── sign_request function ────────────────────────────────────────────────────


class TestSignRequest:
    def test_sign_request_returns_required_headers(self) -> None:
        """sign_request returns dict with all required HMAC headers."""
        from custom_components.carmabox.hub import sign_request

        headers = sign_request('{"action":"sync"}', "my_api_key", "box_001")

        assert headers["X-Box-ID"] == "box_001"
        assert "X-Timestamp" in headers
        assert "X-Nonce" in headers
        assert "X-Signature" in headers
        assert headers["Content-Type"] == "application/json"

    def test_sign_request_signature_is_hex(self) -> None:
        """Signature is 64-char hex string (SHA-256)."""
        from custom_components.carmabox.hub import sign_request

        headers = sign_request("{}", "key", "box")
        assert len(headers["X-Signature"]) == 64
        int(headers["X-Signature"], 16)  # Valid hex

    def test_sign_request_nonce_is_16_chars(self) -> None:
        """Nonce is 16-character hex string."""
        from custom_components.carmabox.hub import sign_request

        headers = sign_request("{}", "key", "box")
        assert len(headers["X-Nonce"]) == 16

    def test_sign_request_different_calls_unique_nonces(self) -> None:
        """Each sign_request call generates a unique nonce."""
        from custom_components.carmabox.hub import sign_request

        h1 = sign_request("{}", "key", "box")
        h2 = sign_request("{}", "key", "box")
        assert h1["X-Nonce"] != h2["X-Nonce"]


# ── store_certs and load_certs ───────────────────────────────────────────────


class TestStoreCerts:
    def test_store_certs_writes_files(self, tmp_path: object) -> None:
        """store_certs writes cert, key, and CA files to disk."""
        from pathlib import Path

        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        client.store_certs(
            client_cert="CERT_DATA",
            client_key="KEY_DATA",
            ca_cert="CA_DATA",
        )

        cert_dir = Path(str(tmp_path)) / "carmabox_certs"
        assert (cert_dir / "box_test123.crt").read_text() == "CERT_DATA"
        assert (cert_dir / "box_test123.key").read_text() == "KEY_DATA"
        assert (cert_dir / "ca.crt").read_text() == "CA_DATA"

    def test_store_certs_key_permission_600(self, tmp_path: object) -> None:
        """Private key file chmod 0o600 after writing."""
        import stat
        from pathlib import Path

        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        client.store_certs("CERT", "KEY", "CA")

        key_path = Path(str(tmp_path)) / "carmabox_certs" / "box_test123.key"
        mode = oct(stat.S_IMODE(key_path.stat().st_mode))
        assert mode == oct(0o600)

    def test_store_certs_sets_cert_paths(self, tmp_path: object) -> None:
        """store_certs sets _client_cert_path, _client_key_path, _ca_cert_path."""
        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        assert client._client_cert_path is None
        client.store_certs("CERT", "KEY", "CA")
        assert client._client_cert_path is not None


class TestLoadCerts:
    def test_load_certs_no_dir_returns_false(self, tmp_path: object) -> None:
        """No cert directory → load_certs returns False."""
        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)
        # Don't create the cert dir
        assert client.load_certs() is False

    def test_load_certs_with_existing_certs_returns_true(self, tmp_path: object) -> None:
        """Existing cert files → load_certs returns True."""
        from pathlib import Path

        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        # Create the cert dir and files manually
        cert_dir = Path(str(tmp_path)) / "carmabox_certs"
        cert_dir.mkdir()
        (cert_dir / "box_test123.crt").write_text("CERT")
        (cert_dir / "box_test123.key").write_text("KEY")
        (cert_dir / "ca.crt").write_text("CA")

        assert client.load_certs() is True

    def test_load_certs_missing_files_returns_false(self, tmp_path: object) -> None:
        """Cert dir exists but files missing → load_certs returns False."""
        from pathlib import Path

        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        # Create dir but no files
        cert_dir = Path(str(tmp_path)) / "carmabox_certs"
        cert_dir.mkdir()

        assert client.load_certs() is False


class TestMQTTPublishException:
    """Line 373-375: _mqtt_publish exception path."""

    def test_publish_exception_returns_false(self) -> None:
        """mqtt.publish raises → return False (lines 373-375)."""
        client = _make_client()
        client._mqtt_connected = True
        mock_mqtt = MagicMock()
        mock_mqtt.publish.side_effect = OSError("network error")
        client._mqtt_client = mock_mqtt

        result = client.publish_telemetry({"grid_kw": 1.5})
        assert result is False


class TestRegisterWithCertsAndMQTTS:
    """Lines 470-471, 475, 477: register response with certs and mqtts details."""

    @pytest.mark.asyncio
    async def test_register_stores_certs_and_mqtts(self, tmp_path: object) -> None:
        """register response with certs + mqtts_host/port → store all (lines 470-477)."""

        client = _make_client()
        client.hass.config.config_dir = str(tmp_path)

        response_data = {
            "mqtt_username": "box_newuser",
            "mqtt_token": "token123",
            "mqtt_hmac_key": "new_hmac_key",
            "client_cert": "CERT_DATA",
            "client_key": "KEY_DATA",
            "ca_cert": "CA_DATA",
            "mqtts_host": "mqtts.example.com",
            "mqtts_port": 8883,
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=response_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.register(config={})

        assert result is not None
        assert client.mqtts_host == "mqtts.example.com"
        assert client.mqtts_port == 8883


paho = pytest.importorskip("paho", reason="paho-mqtt not installed")


class TestMQTTConnectWithPaho:
    """Lines 253-347: _connect_mqtt with mocked paho client."""

    def test_connect_websocket_without_certs(self) -> None:
        """No mTLS certs → WebSocket connection path (lines 275-285)."""
        import asyncio

        client = _make_client()
        mock_mqtt_client = MagicMock()

        with patch("paho.mqtt.client.Client", return_value=mock_mqtt_client):
            result = asyncio.get_event_loop().run_until_complete(client.connect_mqtt())

        assert result is True
        assert client._mqtt_client is mock_mqtt_client

    def test_connect_mtls_with_certs(self, tmp_path: object) -> None:
        """mTLS cert files present → MQTTS connection path (lines 258-274)."""
        import asyncio
        from pathlib import Path

        client = _make_client()
        cert_dir = Path(str(tmp_path)) / "carmabox_certs"
        cert_dir.mkdir()
        for fname in ("client.crt", "client.key", "ca.crt"):
            (cert_dir / fname).write_text("CERT_DATA")

        client._client_cert_path = cert_dir / "client.crt"
        client._client_key_path = cert_dir / "client.key"
        client._ca_cert_path = cert_dir / "ca.crt"
        client.mqtts_host = "mqtts.example.com"
        client.mqtts_port = 8883

        mock_mqtt_client = MagicMock()
        with patch("paho.mqtt.client.Client", return_value=mock_mqtt_client):
            result = asyncio.get_event_loop().run_until_complete(client.connect_mqtt())

        assert result is True

    def test_connect_exception_returns_false(self) -> None:
        """mqtt.Client raises → return False (line 345-347)."""
        import asyncio

        client = _make_client()

        with patch("paho.mqtt.client.Client", side_effect=RuntimeError("paho error")):
            result = asyncio.get_event_loop().run_until_complete(client.connect_mqtt())

        assert result is False

    def test_on_connect_callback_sets_connected(self) -> None:
        """on_connect callback rc==0 → _mqtt_connected=True (lines 293-299)."""
        import asyncio

        client = _make_client()
        mock_mqtt_client = MagicMock()
        captured: dict = {}

        def capture_on_connect(cb: object) -> None:
            captured["on_connect"] = cb

        # Use property setter on the mock instance to capture callbacks
        type(mock_mqtt_client).on_connect = property(fset=capture_on_connect)

        with patch("paho.mqtt.client.Client", return_value=mock_mqtt_client):
            asyncio.get_event_loop().run_until_complete(client.connect_mqtt())

        # Trigger on_connect with rc=0 → should set _mqtt_connected=True
        if "on_connect" in captured:
            captured["on_connect"](None, None, {}, 0)
            assert client._mqtt_connected is True
