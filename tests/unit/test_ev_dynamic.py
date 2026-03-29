"""Tests for dynamic EV amperage adjustment."""

from __future__ import annotations

from custom_components.carmabox.optimizer.ev_dynamic import (
    calculate_dynamic_amps,
    calculate_spike_response,
    detect_appliance_spike,
)


class TestCalculateDynamicAmps:
    def test_full_headroom(self) -> None:
        """Low house load → max amps."""
        amps = calculate_dynamic_amps(
            house_load_kw=1.0,
            current_ev_amps=6,
            target_weighted_kw=2.0,
            night_weight=0.5,  # 4 kW actual max
            max_amps=16,
        )
        assert amps == 13  # (4-1)*1000/230 = 13

    def test_tight_headroom(self) -> None:
        """High house load → low amps."""
        amps = calculate_dynamic_amps(
            house_load_kw=3.5,
            current_ev_amps=10,
            target_weighted_kw=2.0,
            night_weight=0.5,
        )
        assert amps == 0  # Only 0.5kW headroom = 2A < 6A → pause

    def test_battery_support_increases_headroom(self) -> None:
        """Battery discharge adds headroom for EV."""
        amps_no_batt = calculate_dynamic_amps(
            house_load_kw=3.0,
            current_ev_amps=6,
            target_weighted_kw=2.0,
            night_weight=0.5,
        )
        amps_with_batt = calculate_dynamic_amps(
            house_load_kw=3.0,
            current_ev_amps=6,
            target_weighted_kw=2.0,
            night_weight=0.5,
            battery_discharge_kw=1.0,
        )
        assert amps_with_batt > amps_no_batt

    def test_daytime_weight_reduces_headroom(self) -> None:
        """Day weight 1.0 → less headroom than night 0.5."""
        amps_night = calculate_dynamic_amps(
            house_load_kw=1.5,
            current_ev_amps=10,
            target_weighted_kw=2.0,
            night_weight=0.5,
        )
        amps_day = calculate_dynamic_amps(
            house_load_kw=1.5,
            current_ev_amps=10,
            target_weighted_kw=2.0,
            night_weight=1.0,
        )
        assert amps_night > amps_day

    def test_below_6a_pauses(self) -> None:
        """Below 6A → pause (0A)."""
        amps = calculate_dynamic_amps(
            house_load_kw=3.2,
            current_ev_amps=6,
            target_weighted_kw=2.0,
            night_weight=0.5,
        )
        # Headroom = 0.8kW = 3A < 6A → 0
        assert amps == 0

    def test_max_amps_capped(self) -> None:
        """Never exceeds max_amps."""
        amps = calculate_dynamic_amps(
            house_load_kw=0.5,
            current_ev_amps=16,
            target_weighted_kw=5.0,
            night_weight=0.5,
            max_amps=16,
        )
        assert amps <= 16

    def test_zero_weight_returns_zero(self) -> None:
        amps = calculate_dynamic_amps(
            house_load_kw=1.0,
            current_ev_amps=6,
            target_weighted_kw=2.0,
            night_weight=0.0,
        )
        assert amps == 0


class TestDetectApplianceSpike:
    def test_spike_detected(self) -> None:
        assert detect_appliance_spike(3.5, 2.0, spike_threshold_kw=1.0) is True

    def test_no_spike(self) -> None:
        assert detect_appliance_spike(2.3, 2.0, spike_threshold_kw=1.0) is False

    def test_decrease_not_spike(self) -> None:
        assert detect_appliance_spike(1.0, 3.0, spike_threshold_kw=1.0) is False


class TestCalculateSpikeResponse:
    def test_reduces_amps(self) -> None:
        new_amps = calculate_spike_response(current_ev_amps=10, spike_kw=1.5)
        # Reduce by 1.5kW/230V + 1 = 7A + 1 = 8A reduction → 10-8 = 2 → <6 → 0
        assert new_amps == 0

    def test_small_spike_partial_reduction(self) -> None:
        new_amps = calculate_spike_response(current_ev_amps=16, spike_kw=0.5)
        # Reduce by 0.5kW/230V + 1 = 3A → 16-3 = 13
        assert new_amps == 13

    def test_cant_go_below_min(self) -> None:
        new_amps = calculate_spike_response(current_ev_amps=6, spike_kw=5.0, min_amps=0)
        assert new_amps == 0
