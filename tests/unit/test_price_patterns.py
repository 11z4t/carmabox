"""Tests for CARMA Box price patterns module (PLAT-963)."""

from custom_components.carmabox.optimizer.price_patterns import (
    MIN_SAMPLES_FOR_PREDICTION,
    PriceProfile,
)


class TestPriceProfile:
    """Test price pattern learning."""

    def test_initial_state(self):
        p = PriceProfile()
        assert not p.has_sufficient_data
        assert p.weekday_samples[1] == 0

    def test_record_day(self):
        p = PriceProfile()
        prices = [30 + h * 2 for h in range(24)]
        p.record_day(prices, 3, False, "2026-03-21")
        assert p.weekday_samples[3] == 1

    def test_record_weekend(self):
        p = PriceProfile()
        prices = [25 + h for h in range(24)]
        p.record_day(prices, 6, True, "2026-06-21")
        assert p.weekend_samples[6] == 1

    def test_sufficient_data_threshold(self):
        p = PriceProfile()
        prices = [50.0] * 24
        for i in range(MIN_SAMPLES_FOR_PREDICTION):
            p.record_day(prices, 3, i % 7 >= 5, f"2026-03-{i + 1:02d}")
        assert p.has_sufficient_data

    def test_predict_24h_shape(self):
        p = PriceProfile()
        pred = p.predict_24h(6, False)
        assert len(pred) == 24

    def test_predict_learns_pattern(self):
        p = PriceProfile()
        # Morning peak pattern
        prices = [20.0] * 6 + [80.0] * 3 + [40.0] * 8 + [70.0] * 5 + [30.0] * 2
        for _ in range(20):
            p.record_day(prices, 3, False)

        pred = p.predict_24h(3, False)
        # Morning (h6-8) should be higher than night (h0-5)
        morning_avg = sum(pred[6:9]) / 3
        night_avg = sum(pred[0:6]) / 6
        assert morning_avg > night_avg

    def test_predict_multiday(self):
        p = PriceProfile()
        result = p.predict_multiday(3, 0, days=5)
        assert len(result) == 5
        assert all(len(day) == 24 for day in result)

    def test_scale_factor(self):
        p = PriceProfile()
        normal = p.predict_24h(3, False, scale_factor=1.0)
        scaled = p.predict_24h(3, False, scale_factor=1.5)
        # Scaled should be 50% higher
        assert all(abs(s - n * 1.5) < 0.2 for s, n in zip(scaled, normal, strict=False))

    def test_charge_threshold(self):
        p = PriceProfile()
        threshold = p.charge_threshold(3)
        assert threshold > 0

    def test_discharge_threshold(self):
        p = PriceProfile()
        ct = p.charge_threshold(3)
        dt = p.discharge_threshold(3)
        assert dt >= ct

    def test_expected_spread(self):
        p = PriceProfile()
        spread = p.expected_spread(6)
        assert spread == 30.0  # Default

    def test_spread_learns(self):
        p = PriceProfile()
        # High spread day
        prices = [10.0] * 12 + [200.0] * 12
        for _ in range(20):
            p.record_day(prices, 6, False)
        spread = p.expected_spread(6)
        assert spread > 100  # Should have learned high spread

    def test_invalid_month(self):
        p = PriceProfile()
        prices = [50.0] * 24
        p.record_day(prices, 0, False)  # Invalid month
        p.record_day(prices, 13, False)  # Invalid month
        assert sum(p.weekday_samples.values()) == 0

    def test_short_prices_rejected(self):
        p = PriceProfile()
        p.record_day([50.0] * 10, 3, False)  # Too short
        assert p.weekday_samples[3] == 0

    def test_serialization_roundtrip(self):
        p = PriceProfile()
        prices = [30 + h * 2 for h in range(24)]
        for i in range(5):
            p.record_day(prices, 3, False, f"2026-03-{i + 1:02d}")

        data = p.to_dict()
        p2 = PriceProfile.from_dict(data)

        assert p2.weekday_samples[3] == 5
        assert len(p2.daily_records) == 5

    def test_summary(self):
        p = PriceProfile()
        s = p.summary()
        assert "total_weekday_samples" in s
        assert "has_sufficient_data" in s

    def test_daily_records_capped(self):
        p = PriceProfile()
        prices = [50.0] * 24
        for i in range(200):
            p.record_day(prices, 3, False, f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}")
        assert len(p.daily_records) <= 180
