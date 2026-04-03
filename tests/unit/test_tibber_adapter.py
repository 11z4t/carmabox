"""Tests for CARMA Box — Tibber price adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.adapters.tibber import TibberAdapter


def _make_hass(*entities: tuple[str, str, dict]) -> MagicMock:
    """Create mock hass with states and attributes."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value, attrs in entities:
        state = MagicMock()
        state.state = value
        state.attributes = attrs
        states[entity_id] = state

    hass.states.get = lambda eid: states.get(eid)
    return hass


def _price_entry(hour: int, price_sek: float) -> dict:
    """Create a Tibber price entry dict."""
    return {"start_time": f"2026-04-03T{hour:02d}:00:00+02:00", "price": price_sek}


class TestTibberAdapterRead:
    def test_current_price(self) -> None:
        """Current price in SEK/kWh → öre/kWh."""
        hass = _make_hass(("sensor.electricity_price_hem", "0.42", {}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        assert adapter.current_price == 42.0

    def test_current_price_unavailable(self) -> None:
        hass = _make_hass(("sensor.electricity_price_hem", "unavailable", {}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        assert adapter.current_price == 100.0  # Default fallback

    def test_current_price_missing_entity(self) -> None:
        hass = _make_hass()
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        assert adapter.current_price == 100.0

    def test_current_price_custom_fallback(self) -> None:
        hass = _make_hass()
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem", fallback_price=50.0)
        assert adapter.current_price == 50.0

    def test_today_prices(self) -> None:
        """24 hourly prices from Tibber dict format."""
        prices_today = [_price_entry(h, 0.30 + h * 0.01) for h in range(24)]
        hass = _make_hass(("sensor.electricity_price_hem", "0.30", {"prices_today": prices_today}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        result = adapter.today_prices
        assert len(result) == 24
        assert result[0] == 30.0  # 0.30 SEK → 30 öre
        assert result[23] == 53.0  # 0.53 SEK → 53 öre

    def test_today_prices_fallback(self) -> None:
        hass = _make_hass(("sensor.electricity_price_hem", "0.30", {}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        result = adapter.today_prices
        assert len(result) == 24
        assert all(p == 100.0 for p in result)  # All fallback

    def test_today_prices_incomplete(self) -> None:
        """Less than 24 prices → fallback."""
        prices = [_price_entry(h, 0.30) for h in range(10)]
        hass = _make_hass(("sensor.electricity_price_hem", "0.30", {"prices_today": prices}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        result = adapter.today_prices
        assert len(result) == 24
        assert all(p == 100.0 for p in result)

    def test_tomorrow_prices(self) -> None:
        prices_tomorrow = [_price_entry(h, 0.50 + h * 0.005) for h in range(24)]
        hass = _make_hass(
            ("sensor.electricity_price_hem", "0.50", {"prices_tomorrow": prices_tomorrow})
        )
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        result = adapter.tomorrow_prices
        assert result is not None
        assert len(result) == 24
        assert result[0] == 50.0

    def test_tomorrow_prices_none_when_unavailable(self) -> None:
        hass = _make_hass(("sensor.electricity_price_hem", "0.30", {}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        assert adapter.tomorrow_prices is None

    def test_tomorrow_prices_none_when_incomplete(self) -> None:
        prices = [_price_entry(h, 0.50) for h in range(5)]
        hass = _make_hass(("sensor.electricity_price_hem", "0.50", {"prices_tomorrow": prices}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        assert adapter.tomorrow_prices is None

    def test_flat_list_format(self) -> None:
        """Tibber might return flat float list instead of dict list."""
        prices = [0.30 + i * 0.01 for i in range(24)]
        hass = _make_hass(("sensor.electricity_price_hem", "0.30", {"prices_today": prices}))
        adapter = TibberAdapter(hass, "sensor.electricity_price_hem")
        result = adapter.today_prices
        assert len(result) == 24
        assert result[0] == 30.0
