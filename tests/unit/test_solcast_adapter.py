"""Tests for Solcast PV forecast adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.carmabox.adapters.solcast import SolcastAdapter


def _make_hass(
    entity_id: str = "sensor.solcast_pv_forecast_forecast_today",
    state: str = "30.0",
    attrs: dict[str, object] | None = None,
) -> MagicMock:
    """Create mock hass with Solcast entity."""
    hass = MagicMock()
    mock_state = MagicMock()
    mock_state.state = state
    mock_state.attributes = attrs or {}
    hass.states.get = MagicMock(return_value=mock_state)
    return hass


class TestSolcastRead:
    def test_today_kwh(self) -> None:
        hass = _make_hass(state="30.5")
        adapter = SolcastAdapter(hass)
        assert adapter.today_kwh == 30.5

    def test_tomorrow_kwh(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "20.0"
        state.attributes = {}

        def get(eid: str) -> MagicMock | None:
            if "tomorrow" in eid:
                return state
            return None

        hass.states.get = get
        adapter = SolcastAdapter(hass)
        assert adapter.tomorrow_kwh == 20.0

    def test_today_kwh_unavailable(self) -> None:
        hass = _make_hass(state="unavailable")
        adapter = SolcastAdapter(hass)
        assert adapter.today_kwh == 0.0

    def test_today_kwh_missing_entity(self) -> None:
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = SolcastAdapter(hass)
        assert adapter.today_kwh == 0.0

    def test_forecast_3d(self) -> None:
        hass = MagicMock()
        states = {
            "sensor.solcast_pv_forecast_forecast_today": _state("30"),
            "sensor.solcast_pv_forecast_forecast_tomorrow": _state("20"),
            "sensor.solcast_pv_forecast_forecast_day_3": _state("5"),
            "sensor.solcast_pv_forecast_forecast_day_4": _state("32"),
            "sensor.solcast_pv_forecast_forecast_day_5": _state("28"),
        }
        hass.states.get = lambda eid: states.get(eid)
        adapter = SolcastAdapter(hass)
        forecast = adapter.forecast_daily_3d
        assert len(forecast) >= 3
        assert forecast[0] == 30.0
        assert forecast[1] == 20.0
        assert forecast[2] == 5.0

    def test_forecast_3d_missing_days(self) -> None:
        hass = MagicMock()
        states = {
            "sensor.solcast_pv_forecast_forecast_today": _state("30"),
        }
        hass.states.get = lambda eid: states.get(eid)
        adapter = SolcastAdapter(hass)
        forecast = adapter.forecast_daily_3d
        assert len(forecast) >= 1
        assert forecast[0] == 30.0

    def test_hourly_forecast(self) -> None:
        hourly = [
            {"period_start": "2026-03-18T08:00:00", "pv_estimate10": 1000, "pv_estimate": 1500},
            {"period_start": "2026-03-18T09:00:00", "pv_estimate10": 2000, "pv_estimate": 2500},
        ]
        hass = _make_hass(attrs={"detailedHourly": hourly})
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert len(result) == 24
        assert result[8] == 1.0  # pv_estimate10 / 1000
        assert result[9] == 2.0

    def test_hourly_forecast_empty(self) -> None:
        hass = _make_hass(attrs={})
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert len(result) == 24
        assert all(v == 0.0 for v in result)

    def test_hourly_forecast_no_estimate10(self) -> None:
        """Falls back to pv_estimate if pv_estimate10 missing."""
        hourly = [
            {"period_start": "2026-03-18T10:00:00", "pv_estimate": 3000},
        ]
        hass = _make_hass(attrs={"detailedHourly": hourly})
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert result[10] == 3.0


def _state(value: str) -> MagicMock:
    """Create mock state object."""
    s = MagicMock()
    s.state = value
    s.attributes = {}
    return s


class TestSolcastEdgeCases:
    def test_today_kwh_invalid_string(self) -> None:
        hass = _make_hass(state="not_a_number")
        adapter = SolcastAdapter(hass)
        assert adapter.today_kwh == 0.0

    def test_forecast_3d_all_zero(self) -> None:
        """All zeros → returns [0.0]."""
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=_state("0"))
        adapter = SolcastAdapter(hass)
        forecast = adapter.forecast_daily_3d
        assert forecast == [0.0]

    def test_hourly_bad_period_start(self) -> None:
        """Invalid period_start should be skipped."""
        hourly = [
            {"period_start": "not-a-date", "pv_estimate10": 1000},
            {"period_start": "2026-03-18T12:00:00", "pv_estimate10": 3000},
        ]
        hass = _make_hass(attrs={"detailedHourly": hourly})
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert result[12] == 3.0  # Valid entry works
        # Invalid entry skipped, not crash

    def test_hourly_entity_missing(self) -> None:
        """Missing Solcast entity returns all zeros."""
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert len(result) == 24
        assert all(v == 0.0 for v in result)
