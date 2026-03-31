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
            {
                "period_start": "2026-03-18T08:00:00",
                "pv_estimate10": 1.0,
                "pv_estimate": 1.5,
            },
            {
                "period_start": "2026-03-18T09:00:00",
                "pv_estimate10": 2.0,
                "pv_estimate": 2.5,
            },
        ]
        hass = _make_hass(attrs={"detailedHourly": hourly})
        adapter = SolcastAdapter(hass)
        result = adapter.today_hourly_kw
        assert len(result) == 24
        assert result[8] == 1.0  # pv_estimate10 in kW
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
            {"period_start": "2026-03-18T10:00:00", "pv_estimate": 3.0},
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
        assert forecast == [0.0, 0.0]  # Always includes today + tomorrow

    def test_hourly_bad_period_start(self) -> None:
        """Invalid period_start should be skipped."""
        hourly = [
            {"period_start": "not-a-date", "pv_estimate10": 1.0},
            {"period_start": "2026-03-18T12:00:00", "pv_estimate10": 3.0},
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


class TestSolcastTomorrowHourly:
    """Tests for tomorrow_hourly_kw — PLAT-958."""

    def test_tomorrow_hourly_sunny_day(self) -> None:
        """Sunny day: significant PV forecast across daylight hours."""
        hourly_data = [
            {
                "period_start": "2026-03-22T07:00:00",
                "pv_estimate10": 0.5,
                "pv_estimate": 0.8,
            },
            {
                "period_start": "2026-03-22T08:00:00",
                "pv_estimate10": 1.5,
                "pv_estimate": 2.0,
            },
            {
                "period_start": "2026-03-22T09:00:00",
                "pv_estimate10": 3.0,
                "pv_estimate": 4.0,
            },
            {
                "period_start": "2026-03-22T10:00:00",
                "pv_estimate10": 4.5,
                "pv_estimate": 5.5,
            },
            {
                "period_start": "2026-03-22T11:00:00",
                "pv_estimate10": 5.0,
                "pv_estimate": 6.0,
            },
            {
                "period_start": "2026-03-22T12:00:00",
                "pv_estimate10": 5.2,
                "pv_estimate": 6.2,
            },
            {
                "period_start": "2026-03-22T13:00:00",
                "pv_estimate10": 4.8,
                "pv_estimate": 5.8,
            },
            {
                "period_start": "2026-03-22T14:00:00",
                "pv_estimate10": 3.5,
                "pv_estimate": 4.5,
            },
            {
                "period_start": "2026-03-22T15:00:00",
                "pv_estimate10": 2.0,
                "pv_estimate": 3.0,
            },
            {
                "period_start": "2026-03-22T16:00:00",
                "pv_estimate10": 0.8,
                "pv_estimate": 1.2,
            },
            {
                "period_start": "2026-03-22T17:00:00",
                "pv_estimate10": 0.2,
                "pv_estimate": 0.4,
            },
        ]
        tomorrow_state = MagicMock()
        tomorrow_state.state = "25.0"
        tomorrow_state.attributes = {"detailedHourly": hourly_data}

        hass = MagicMock()
        hass.states.get = lambda eid: tomorrow_state if "tomorrow" in eid else None

        adapter = SolcastAdapter(hass)
        result = adapter.tomorrow_hourly_kw

        assert len(result) == 24
        assert result[12] == 5.2  # Peak at noon
        assert result[0] == 0.0  # Night
        assert result[23] == 0.0  # Night
        assert sum(result) > 20  # Significant total

    def test_tomorrow_hourly_cloudy_day(self) -> None:
        """Cloudy day: low PV forecast."""
        hourly_data = [
            {
                "period_start": "2026-03-22T09:00:00",
                "pv_estimate10": 0.2,
                "pv_estimate": 0.4,
            },
            {
                "period_start": "2026-03-22T10:00:00",
                "pv_estimate10": 0.5,
                "pv_estimate": 0.8,
            },
            {
                "period_start": "2026-03-22T11:00:00",
                "pv_estimate10": 0.6,
                "pv_estimate": 0.9,
            },
            {
                "period_start": "2026-03-22T12:00:00",
                "pv_estimate10": 0.7,
                "pv_estimate": 1.0,
            },
            {
                "period_start": "2026-03-22T13:00:00",
                "pv_estimate10": 0.5,
                "pv_estimate": 0.7,
            },
            {
                "period_start": "2026-03-22T14:00:00",
                "pv_estimate10": 0.3,
                "pv_estimate": 0.5,
            },
        ]
        tomorrow_state = MagicMock()
        tomorrow_state.state = "3.0"
        tomorrow_state.attributes = {"detailedHourly": hourly_data}

        hass = MagicMock()
        hass.states.get = lambda eid: tomorrow_state if "tomorrow" in eid else None

        adapter = SolcastAdapter(hass)
        result = adapter.tomorrow_hourly_kw

        assert len(result) == 24
        assert result[12] == 0.7  # Low peak
        assert sum(result) < 5  # Low total

    def test_tomorrow_hourly_unavailable(self) -> None:
        """Tomorrow sensor unavailable → all zeros."""
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        adapter = SolcastAdapter(hass)
        result = adapter.tomorrow_hourly_kw
        assert len(result) == 24
        assert all(v == 0.0 for v in result)

    def test_tomorrow_hourly_no_detailed_attribute(self) -> None:
        """Tomorrow sensor exists but has no detailedHourly → all zeros."""
        tomorrow_state = MagicMock()
        tomorrow_state.state = "15.0"
        tomorrow_state.attributes = {}

        hass = MagicMock()
        hass.states.get = lambda eid: tomorrow_state if "tomorrow" in eid else None

        adapter = SolcastAdapter(hass)
        result = adapter.tomorrow_hourly_kw
        assert len(result) == 24
        assert all(v == 0.0 for v in result)

    def test_planner_receives_real_forecast(self) -> None:
        """Verify generate_plan gets real PV data, not zeros — core PLAT-958 test."""
        from custom_components.carmabox.optimizer.planner import generate_plan

        # Sunny tomorrow: significant solar 8-16h
        sunny_pv = [0.0] * 24
        for h in range(8, 17):
            sunny_pv[h] = 3.0  # 3 kW per hour

        # Starting at 20:00 today → 4 hours today (zeros) + 24 hours tomorrow
        pv_today_remaining = [0.0] * 4
        pv_forecast = pv_today_remaining + sunny_pv

        plan = generate_plan(
            num_hours=28,
            start_hour=20,
            target_weighted_kw=2.0,
            hourly_loads=[1.5] * 28,
            hourly_pv=pv_forecast,
            hourly_prices=[50.0] * 28,
            hourly_ev=[0.0] * 28,
            battery_soc=50.0,
            ev_soc=-1,
        )

        # Tomorrow's solar hours should show charge actions (pv surplus)
        # Hours 8-16 tomorrow = indices 12-20 in plan (offset by 4 today hours)
        solar_hours = list(plan[12:21])
        charge_actions = [p for p in solar_hours if p.action == "c"]
        assert len(charge_actions) > 0, "Planner should charge from PV on sunny hours"
        assert all(p.pv_kw == 3.0 for p in solar_hours), "PV forecast should be 3.0 kW"

        # Compare with zero-PV plan (old behavior)
        zero_plan = generate_plan(
            num_hours=28,
            start_hour=20,
            target_weighted_kw=2.0,
            hourly_loads=[1.5] * 28,
            hourly_pv=[0.0] * 28,
            hourly_prices=[50.0] * 28,
            hourly_ev=[0.0] * 28,
            battery_soc=50.0,
            ev_soc=-1,
        )
        zero_charge = [p for p in zero_plan[12:21] if p.action == "c"]
        assert len(zero_charge) == 0, "Zero PV should not trigger charge"
