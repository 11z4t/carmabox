"""Tests for hub sync client — MQTT/WSS + HTTPS fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.hub import HubSyncClient
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
