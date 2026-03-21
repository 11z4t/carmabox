"""Tests for CARMA Box weather learning module (PLAT-963)."""

from custom_components.carmabox.optimizer.weather_learning import (
    MIN_SAMPLES_PER_BIN,
    NUM_BINS,
    WeatherProfile,
    _temp_to_bin,
)


class TestTempToBin:
    """Test temperature binning."""

    def test_very_cold(self):
        assert _temp_to_bin(-25) == 0  # Clamped to -20

    def test_cold(self):
        assert _temp_to_bin(-15) == 1

    def test_zero(self):
        assert _temp_to_bin(0) == 4  # 0 is in bin [-0..5) → bin 4

    def test_warm(self):
        assert _temp_to_bin(15) == 7

    def test_hot(self):
        assert _temp_to_bin(30) == 10

    def test_very_hot(self):
        assert _temp_to_bin(40) == 10  # Clamped to 34


class TestWeatherProfile:
    """Test WeatherProfile learning and adjustment."""

    def test_initial_state(self):
        wp = WeatherProfile()
        assert len(wp.factors) == 24
        assert len(wp.factors[0]) == NUM_BINS
        assert wp.total_samples == 0
        assert wp.coverage_pct == 0.0

    def test_get_adjustment_no_data(self):
        wp = WeatherProfile()
        # Without data, should return 1.0 (no adjustment)
        assert wp.get_adjustment(12, 15.0) == 1.0

    def test_update_single_sample(self):
        wp = WeatherProfile()
        wp.update(12, 15.0, 3.0, 2.0)  # 50% more consumption
        # After one sample, bin count = 1 < MIN_SAMPLES_PER_BIN
        # So get_adjustment should interpolate/return 1.0
        assert wp.counts[12][_temp_to_bin(15.0)] == 1

    def test_update_many_samples_adjusts_factor(self):
        wp = WeatherProfile()
        # Feed 20 samples at hour=10, temp=-5°C, consumption=3.0, baseline=2.0
        # Factor should converge toward 1.5 (3.0/2.0)
        for _ in range(20):
            wp.update(10, -5.0, 3.0, 2.0)

        t_bin = _temp_to_bin(-5.0)
        assert wp.counts[10][t_bin] >= MIN_SAMPLES_PER_BIN
        factor = wp.get_adjustment(10, -5.0)
        # Factor should be close to 1.5 (EMA converges)
        assert 1.2 < factor < 1.6

    def test_adjust_prediction(self):
        wp = WeatherProfile()
        # Build up enough samples
        for _ in range(MIN_SAMPLES_PER_BIN + 5):
            wp.update(14, 20.0, 1.0, 2.0)  # Half normal consumption

        adjusted = wp.adjust_prediction(14, 2.0, 20.0)
        # Should reduce prediction (factor < 1.0)
        assert adjusted < 2.0
        assert adjusted >= 0.3  # Floor

    def test_invalid_hour(self):
        wp = WeatherProfile()
        wp.update(-1, 15.0, 2.0, 2.0)  # Should be ignored
        wp.update(24, 15.0, 2.0, 2.0)  # Should be ignored
        assert wp.total_samples == 0

    def test_zero_baseline_ignored(self):
        wp = WeatherProfile()
        wp.update(12, 15.0, 2.0, 0.0)  # Zero baseline → skip
        assert wp.total_samples == 0

    def test_interpolation(self):
        wp = WeatherProfile()
        # Fill two distant bins at hour 12
        for _ in range(MIN_SAMPLES_PER_BIN + 5):
            wp.update(12, -10.0, 4.0, 2.0)  # Factor ~2.0 at cold
            wp.update(12, 25.0, 1.5, 2.0)  # Factor ~0.75 at warm

        # Query a bin between them with no data
        factor = wp.get_adjustment(12, 10.0)
        # Should interpolate between ~2.0 and ~0.75
        assert 0.7 < factor < 2.0

    def test_serialization_roundtrip(self):
        wp = WeatherProfile()
        for _ in range(15):
            wp.update(8, 5.0, 2.5, 2.0)

        data = wp.to_dict()
        wp2 = WeatherProfile.from_dict(data)

        assert wp2.counts[8][_temp_to_bin(5.0)] == 15
        assert abs(wp2.factors[8][_temp_to_bin(5.0)] - wp.factors[8][_temp_to_bin(5.0)]) < 0.001

    def test_summary(self):
        wp = WeatherProfile()
        s = wp.summary()
        assert "total_samples" in s
        assert "coverage_pct" in s
        assert s["total_samples"] == 0

    def test_coverage_increases(self):
        wp = WeatherProfile()
        assert wp.coverage_pct == 0.0
        for _ in range(MIN_SAMPLES_PER_BIN + 1):
            wp.update(12, 15.0, 2.0, 2.0)
        assert wp.coverage_pct > 0.0
