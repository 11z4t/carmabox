"""Tests for PLAT-962: Household profile, benchmarking, and energy advisory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.hub import HubSyncClient
from custom_components.carmabox.optimizer.models import BenchmarkData, HouseholdProfile


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


class TestHouseholdProfile:
    """Test HouseholdProfile dataclass."""

    def test_defaults(self) -> None:
        profile = HouseholdProfile()
        assert profile.house_size_m2 == 0
        assert profile.heating_type == ""
        assert profile.solar_kwp == 0.0
        assert profile.battery_count == 0
        assert profile.postal_code == ""
        assert profile.contract_type == ""

    def test_full_profile(self) -> None:
        profile = HouseholdProfile(
            house_size_m2=140,
            heating_type="vp",
            has_hot_water_heater=True,
            solar_kwp=10.5,
            solar_direction="S",
            solar_tilt=30,
            battery_brand="goodwe",
            battery_count=2,
            battery_total_kwh=20.0,
            ev_brand="XPENG G9",
            ev_capacity_kwh=98,
            ev_charge_speed_kw=7.4,
            postal_code="12345",
            municipality="Sollentuna",
            price_area="SE3",
            grid_operator="ellevio",
            contract_type="variable",
            electricity_retailer="tibber",
            grid_fee_kr_per_kw=80.0,
        )
        assert profile.house_size_m2 == 140
        assert profile.heating_type == "vp"
        assert profile.solar_kwp == 10.5
        assert profile.battery_total_kwh == 20.0
        assert profile.postal_code == "12345"


class TestBenchmarkData:
    """Test BenchmarkData dataclass."""

    def test_defaults(self) -> None:
        bench = BenchmarkData()
        assert bench.similar_households == 0
        assert bench.tips == []
        assert bench.diff_pct == 0.0

    def test_full_benchmark(self) -> None:
        bench = BenchmarkData(
            similar_households=25,
            comparison_group="120-160m², VP, SE3",
            your_monthly_kwh=850,
            avg_monthly_kwh=920,
            diff_pct=-7.6,
            trend_3m="improving",
            your_savings_kr=340,
            avg_savings_kr=280,
            savings_rank_pct=75,
            tips=["Bra jobbat!"],
            battery_roi_months=84,
            solar_roi_months=96,
            updated="2026-03-21T12:00:00",
        )
        assert bench.similar_households == 25
        assert bench.diff_pct == -7.6
        assert len(bench.tips) == 1


class TestAnonymizeConfigHouseholdProfile:
    """Test that new household profile keys are included in anonymization."""

    def test_household_profile_keys_included(self) -> None:
        config = {
            "price_area": "SE3",
            "house_size_m2": 140,
            "heating_type": "vp",
            "has_hot_water_heater": True,
            "solar_kwp": 10.5,
            "solar_direction": "S",
            "solar_tilt": 30,
            "battery_brand": "goodwe",
            "battery_count": 2,
            "contract_type": "variable",
            "electricity_retailer": "tibber",
        }
        result = HubSyncClient._anonymize_config(config)
        assert result["house_size_m2"] == 140
        assert result["heating_type"] == "vp"
        assert result["solar_kwp"] == 10.5
        assert result["battery_brand"] == "goodwe"
        assert result["contract_type"] == "variable"
        assert result["electricity_retailer"] == "tibber"

    def test_postal_code_anonymized_to_3_digits(self) -> None:
        config = {"postal_code": "12345"}
        result = HubSyncClient._anonymize_config(config)
        assert "postal_code" not in result
        assert result["postal_area"] == "123"

    def test_short_postal_code_excluded(self) -> None:
        config = {"postal_code": "12"}
        result = HubSyncClient._anonymize_config(config)
        assert "postal_area" not in result
        assert "postal_code" not in result

    def test_empty_postal_code(self) -> None:
        config = {"postal_code": ""}
        result = HubSyncClient._anonymize_config(config)
        assert "postal_area" not in result

    def test_entity_ids_still_excluded(self) -> None:
        config = {
            "house_size_m2": 140,
            "battery_soc_1": "sensor.private",
            "price_entity": "sensor.nordpool",
            "ev_soc_entity": "sensor.ev_soc",
        }
        result = HubSyncClient._anonymize_config(config)
        assert "battery_soc_1" not in result
        assert "price_entity" not in result
        assert "ev_soc_entity" not in result
        assert result["house_size_m2"] == 140


class TestPublishHouseholdProfile:
    """Test MQTT profile publishing."""

    def test_publish_not_connected(self) -> None:
        client = _make_client()
        assert client.publish_household_profile({"house_size_m2": 140}) is False

    def test_publish_connected(self) -> None:
        client = _make_client()
        client._mqtt_connected = True
        client._mqtt_client = MagicMock()
        result = client.publish_household_profile({"house_size_m2": 140, "price_area": "SE3"})
        assert result is True
        client._mqtt_client.publish.assert_called_once()
        call_args = client._mqtt_client.publish.call_args
        assert "/profile" in call_args[0][0]
        assert call_args[1].get("retain") is True


class TestFetchBenchmarking:
    """Test benchmarking data fetching from hub."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self) -> None:
        client = _make_client()
        bench_data = {
            "similar_households": 25,
            "comparison_group": "120-160m², VP, SE3",
            "avg_monthly_kwh": 920,
            "diff_pct": -7.6,
            "tips": ["Bra jobbat!"],
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=bench_data)
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
            result = await client.fetch_benchmarking({"price_area": "SE3"})

        assert result is not None
        assert result["similar_households"] == 25

    @pytest.mark.asyncio
    async def test_fewer_than_10_still_returns(self) -> None:
        client = _make_client()
        bench_data = {"similar_households": 5}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=bench_data)
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
            result = await client.fetch_benchmarking({"price_area": "SE3"})

        assert result is not None
        assert result["similar_households"] == 5

    @pytest.mark.asyncio
    async def test_hub_offline(self) -> None:
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
            result = await client.fetch_benchmarking({})

        assert result is None

    @pytest.mark.asyncio
    async def test_network_error(self) -> None:
        client = _make_client()
        with patch(
            "custom_components.carmabox.hub.async_get_clientsession",
            side_effect=RuntimeError("offline"),
        ):
            result = await client.fetch_benchmarking({})
        assert result is None
