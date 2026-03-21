"""Tests for CARMA Box PV correction module (PLAT-963)."""

from custom_components.carmabox.optimizer.pv_correction import (
    MIN_SAMPLES_FOR_CORRECTION,
    PVCorrectionProfile,
)


class TestPVCorrectionProfile:
    """Test PV forecast correction learning."""

    def test_initial_state(self):
        p = PVCorrectionProfile()
        assert p.monthly_factor[6] == 1.0
        assert p.overall_accuracy == 0.0
        assert p.trend == "insufficient_data"

    def test_no_correction_without_data(self):
        p = PVCorrectionProfile()
        assert p.correct_daily(6, 20.0) == 20.0  # No correction
        assert p.correct_hourly(12, 5.0) == 5.0

    def test_record_daily_learns_bias(self):
        p = PVCorrectionProfile()
        # Forecast consistently overestimates by 20%
        for i in range(MIN_SAMPLES_FOR_CORRECTION + 5):
            p.record_daily(6, 20.0, 16.0, f"2026-06-{i + 1:02d}")

        # Factor should converge toward 0.8
        assert p.monthly_factor[6] < 1.0
        assert p.monthly_samples[6] >= MIN_SAMPLES_FOR_CORRECTION

    def test_correct_daily_applies_factor(self):
        p = PVCorrectionProfile()
        for _ in range(MIN_SAMPLES_FOR_CORRECTION + 5):
            p.record_daily(3, 15.0, 12.0)  # 80% of forecast

        corrected = p.correct_daily(3, 15.0)
        # Should reduce forecast
        assert corrected < 15.0
        assert corrected > 10.0

    def test_correct_daily_no_correction_other_month(self):
        p = PVCorrectionProfile()
        for _ in range(MIN_SAMPLES_FOR_CORRECTION + 5):
            p.record_daily(6, 20.0, 16.0)

        # Month 1 has no data — no correction
        assert p.correct_daily(1, 20.0) == 20.0

    def test_record_hourly(self):
        p = PVCorrectionProfile()
        for _ in range(MIN_SAMPLES_FOR_CORRECTION * 3 + 5):
            p.record_hourly(12, 5.0, 4.0)  # 80%

        corrected = p.correct_hourly(12, 5.0)
        assert corrected < 5.0

    def test_correct_profile_24h(self):
        p = PVCorrectionProfile()
        for _ in range(MIN_SAMPLES_FOR_CORRECTION + 5):
            p.record_daily(6, 20.0, 24.0)  # Under-estimates by 20%

        forecast = [0.0] * 6 + [2.0, 4.0, 6.0, 7.0, 7.0, 6.0, 4.0, 2.0] + [0.0] * 10
        corrected = p.correct_profile(6, forecast)
        assert len(corrected) == 24
        # Sunny hours should be increased
        assert corrected[9] > forecast[9]

    def test_skip_negligible_forecast(self):
        p = PVCorrectionProfile()
        p.record_daily(6, 0.3, 0.5)  # < 0.5 forecast → skip
        assert p.monthly_samples[6] == 0

    def test_skip_negative_actual(self):
        p = PVCorrectionProfile()
        p.record_daily(6, 10.0, -1.0)
        assert p.monthly_samples[6] == 0

    def test_ratio_clamping(self):
        p = PVCorrectionProfile()
        # Extreme ratio — should be clamped
        p.record_daily(6, 1.0, 10.0)  # Ratio 10 → clamped to 3.0
        assert p.monthly_factor[6] <= 1.3  # EMA from 1.0 toward capped ratio

    def test_overall_accuracy(self):
        p = PVCorrectionProfile()
        for i in range(30):
            p.record_daily(6, 20.0, 19.0, f"2026-06-{i + 1:02d}")
        # 5% error → 95% accuracy
        acc = p.overall_accuracy
        assert 90 < acc < 100

    def test_trend_improving(self):
        p = PVCorrectionProfile()
        # Old data: bad predictions
        for i in range(7):
            p.record_daily(6, 20.0, 10.0, f"2026-06-{i + 1:02d}")
        # Recent data: good predictions
        for i in range(7):
            p.record_daily(6, 20.0, 19.5, f"2026-06-{i + 8:02d}")

        assert p.trend == "improving"

    def test_serialization_roundtrip(self):
        p = PVCorrectionProfile()
        for i in range(10):
            p.record_daily(6, 20.0, 18.0, f"2026-06-{i + 1:02d}")

        data = p.to_dict()
        p2 = PVCorrectionProfile.from_dict(data)

        assert p2.monthly_samples[6] == 10
        assert abs(p2.monthly_factor[6] - p.monthly_factor[6]) < 0.001
        assert len(p2.daily_records) == 10

    def test_summary(self):
        p = PVCorrectionProfile()
        s = p.summary()
        assert "overall_accuracy_pct" in s
        assert "trend" in s
