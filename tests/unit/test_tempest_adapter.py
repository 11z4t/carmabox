"""Tests for CARMA Box Tempest weather adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.adapters.tempest import TempestAdapter


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    """Create mock hass with states."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state

    def get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = get_state
    return hass


class TestTempestAdapter:
    """Test Tempest weather adapter."""

    def test_read_temperature(self) -> None:
        """Test reading temperature sensor."""
        hass = _make_hass(("sensor.tempest_temperature", "22.5"))
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 22.5

    def test_read_temperature_unavailable(self) -> None:
        """Test temperature fallback when unavailable."""
        hass = _make_hass(("sensor.tempest_temperature", "unavailable"))
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 15.0

    def test_read_temperature_missing_entity(self) -> None:
        """Test temperature fallback when entity missing."""
        hass = _make_hass()
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 15.0

    def test_read_illuminance(self) -> None:
        """Test reading illuminance sensor."""
        hass = _make_hass(("sensor.tempest_illuminance", "45000"))
        adapter = TempestAdapter(hass)
        assert adapter.illuminance_lux == 45000.0

    def test_read_illuminance_unavailable(self) -> None:
        """Test illuminance fallback when unavailable."""
        hass = _make_hass(("sensor.tempest_illuminance", "unavailable"))
        adapter = TempestAdapter(hass)
        assert adapter.illuminance_lux == 0.0

    def test_read_illuminance_missing_entity(self) -> None:
        """Test illuminance fallback when entity missing."""
        hass = _make_hass()
        adapter = TempestAdapter(hass)
        assert adapter.illuminance_lux == 0.0

    def test_read_wind_speed(self) -> None:
        """Test reading wind speed sensor."""
        hass = _make_hass(("sensor.tempest_wind_speed", "3.2"))
        adapter = TempestAdapter(hass)
        assert adapter.wind_speed_ms == 3.2

    def test_read_wind_speed_unavailable(self) -> None:
        """Test wind speed fallback when unavailable."""
        hass = _make_hass(("sensor.tempest_wind_speed", "unavailable"))
        adapter = TempestAdapter(hass)
        assert adapter.wind_speed_ms == 0.0

    def test_read_wind_gust(self) -> None:
        """Test reading wind gust sensor."""
        hass = _make_hass(("sensor.tempest_wind_gust", "8.5"))
        adapter = TempestAdapter(hass)
        assert adapter.wind_gust_ms == 8.5

    def test_read_wind_gust_unavailable(self) -> None:
        """Test wind gust fallback when unavailable."""
        hass = _make_hass(("sensor.tempest_wind_gust", "unavailable"))
        adapter = TempestAdapter(hass)
        assert adapter.wind_gust_ms == 0.0

    def test_read_all_sensors_valid(self) -> None:
        """Test reading all sensors with valid data."""
        hass = _make_hass(
            ("sensor.tempest_temperature", "18.3"),
            ("sensor.tempest_illuminance", "32000"),
            ("sensor.tempest_wind_speed", "2.1"),
            ("sensor.tempest_wind_gust", "5.7"),
        )
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 18.3
        assert adapter.illuminance_lux == 32000.0
        assert adapter.wind_speed_ms == 2.1
        assert adapter.wind_gust_ms == 5.7

    def test_read_all_sensors_unavailable(self) -> None:
        """Test reading all sensors when all unavailable."""
        hass = _make_hass(
            ("sensor.tempest_temperature", "unknown"),
            ("sensor.tempest_illuminance", "unknown"),
            ("sensor.tempest_wind_speed", "unknown"),
            ("sensor.tempest_wind_gust", "unknown"),
        )
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 15.0
        assert adapter.illuminance_lux == 0.0
        assert adapter.wind_speed_ms == 0.0
        assert adapter.wind_gust_ms == 0.0

    def test_invalid_numeric_state(self) -> None:
        """Test handling of invalid numeric states."""
        hass = _make_hass(("sensor.tempest_temperature", "not_a_number"))
        adapter = TempestAdapter(hass)
        assert adapter.temperature_c == 15.0
