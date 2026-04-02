"""Tests for RestoreEntity behaviour in CarmaboxSensor (PLAT-1208)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.sensor import CarmaboxSensor, CarmaboxSensorDescription


def _make_sensor(data: object = None) -> CarmaboxSensor:
    """Create a minimal CarmaboxSensor for testing."""
    coordinator = MagicMock()
    coordinator.data = data

    description = CarmaboxSensorDescription(
        key="test_sensor",
        value_fn=lambda coord: "live_value" if coord.data is not None else None,
    )

    entry = MagicMock()
    entry.entry_id = "test_entry"

    sensor = CarmaboxSensor(coordinator=coordinator, entry=entry, description=description)
    sensor.hass = MagicMock()
    return sensor


class TestCarmaboxSensorRestore:
    def test_inherits_restore_entity(self) -> None:
        """CarmaboxSensor must inherit RestoreEntity."""
        from homeassistant.helpers.restore_state import RestoreEntity

        assert issubclass(CarmaboxSensor, RestoreEntity)

    def test_live_value_when_data_present(self) -> None:
        """Returns live value_fn result when coordinator has data."""
        sensor = _make_sensor(data=object())
        assert sensor.native_value == "live_value"

    def test_none_when_no_data_no_restore(self) -> None:
        """Returns None (value_fn result) when no data and no restored state."""
        sensor = _make_sensor(data=None)
        assert sensor.native_value is None

    def test_restored_value_used_when_no_data(self) -> None:
        """Returns restored value when coordinator.data is None and state was restored."""
        sensor = _make_sensor(data=None)
        sensor._restored_native_value = "cached_state"
        assert sensor.native_value == "cached_state"

    def test_live_value_preferred_over_restored(self) -> None:
        """Live value_fn result takes priority when coordinator has data."""
        sensor = _make_sensor(data=object())
        sensor._restored_native_value = "old_cached_state"
        assert sensor.native_value == "live_value"

    @pytest.mark.asyncio
    async def test_async_added_restores_last_state(self) -> None:
        """async_added_to_hass restores last state if it was not unavailable/unknown."""
        sensor = _make_sensor(data=None)

        last_state = MagicMock()
        last_state.state = "discharging"

        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch.object(type(sensor).__mro__[1], "async_added_to_hass", new=AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor._restored_native_value == "discharging"

    @pytest.mark.asyncio
    async def test_unavailable_state_not_restored(self) -> None:
        """STATE_UNAVAILABLE is not stored as restored value."""
        sensor = _make_sensor(data=None)

        last_state = MagicMock()
        last_state.state = "unavailable"

        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch.object(type(sensor).__mro__[1], "async_added_to_hass", new=AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor._restored_native_value is None

    @pytest.mark.asyncio
    async def test_unknown_state_not_restored(self) -> None:
        """STATE_UNKNOWN is not stored as restored value."""
        sensor = _make_sensor(data=None)

        last_state = MagicMock()
        last_state.state = "unknown"

        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch.object(type(sensor).__mro__[1], "async_added_to_hass", new=AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor._restored_native_value is None

    @pytest.mark.asyncio
    async def test_no_last_state_graceful(self) -> None:
        """No crash when async_get_last_state returns None (new sensor)."""
        sensor = _make_sensor(data=None)
        sensor.async_get_last_state = AsyncMock(return_value=None)

        with patch.object(type(sensor).__mro__[1], "async_added_to_hass", new=AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor._restored_native_value is None
