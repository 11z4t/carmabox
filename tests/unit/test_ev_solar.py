"""Tests for EV solar (PV surplus) charging."""

from __future__ import annotations

from custom_components.carmabox.optimizer.ev_solar import (
    calculate_solar_ev_amps,
    should_start_solar_ev,
    should_stop_solar_ev,
)


class TestShouldStartSolarEV:
    def test_all_conditions_met(self) -> None:
        assert (
            should_start_solar_ev(pv_surplus_kw=2.0, battery_soc=98, ev_connected=True, ev_soc=60)
            is True
        )

    def test_not_connected(self) -> None:
        assert (
            should_start_solar_ev(pv_surplus_kw=3.0, battery_soc=100, ev_connected=False, ev_soc=60)
            is False
        )

    def test_ev_full(self) -> None:
        assert (
            should_start_solar_ev(pv_surplus_kw=3.0, battery_soc=100, ev_connected=True, ev_soc=100)
            is False
        )

    def test_battery_not_full(self) -> None:
        """Battery gets priority — don't charge EV until battery >95%."""
        assert (
            should_start_solar_ev(pv_surplus_kw=3.0, battery_soc=80, ev_connected=True, ev_soc=60)
            is False
        )

    def test_insufficient_surplus(self) -> None:
        assert (
            should_start_solar_ev(pv_surplus_kw=0.5, battery_soc=100, ev_connected=True, ev_soc=60)
            is False
        )

    def test_ev_soc_unknown(self) -> None:
        assert (
            should_start_solar_ev(pv_surplus_kw=3.0, battery_soc=100, ev_connected=True, ev_soc=-1)
            is False
        )

    def test_custom_thresholds(self) -> None:
        assert (
            should_start_solar_ev(
                pv_surplus_kw=1.0,
                battery_soc=90,
                ev_connected=True,
                ev_soc=60,
                min_surplus_kw=0.8,
                battery_full_threshold=85.0,
            )
            is True
        )


class TestShouldStopSolarEV:
    def test_surplus_ok(self) -> None:
        assert should_stop_solar_ev(pv_surplus_kw=2.0) is False

    def test_brief_dip_not_stopped(self) -> None:
        """Brief cloud → don't stop immediately."""
        assert should_stop_solar_ev(pv_surplus_kw=0.3, consecutive_low_count=3) is False

    def test_sustained_low_stops(self) -> None:
        """5 min low → stop."""
        assert should_stop_solar_ev(pv_surplus_kw=0.3, consecutive_low_count=10) is True


class TestCalculateSolarEVAmps:
    def test_2kw_surplus(self) -> None:
        amps = calculate_solar_ev_amps(2.0)
        assert amps == 8  # 2000/230 = 8.7 → 8

    def test_4kw_surplus(self) -> None:
        amps = calculate_solar_ev_amps(4.0)
        assert amps == 16  # Capped at max

    def test_below_min(self) -> None:
        amps = calculate_solar_ev_amps(0.5)
        assert amps == 0  # 500/230 = 2 < 6 → 0

    def test_exact_6a(self) -> None:
        amps = calculate_solar_ev_amps(1.38)
        assert amps == 6
