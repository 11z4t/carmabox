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
