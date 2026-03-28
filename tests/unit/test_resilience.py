"""Tests for Resilience — fallbacks, circuit breaker, rate limiter."""

from __future__ import annotations

from custom_components.carmabox.core.resilience import ResilienceManager


class TestSensorFallback:
    def test_current_value_returned(self):
        r = ResilienceManager()
        r.register_sensor("sensor.grid", default=2000)
        val, fb = r.get_value("sensor.grid", 1500, ts=100)
        assert val == 1500
        assert fb is False

    def test_unavailable_uses_last_known(self):
        r = ResilienceManager()
        r.register_sensor("sensor.grid", default=2000, margin=0.1)
        r.update_sensor("sensor.grid", 1500, ts=100)
        val, fb = r.get_value("sensor.grid", None, ts=150)
        assert abs(val - 1650) < 1  # 1500 * 1.1 (float precision)
        assert fb is True

    def test_stale_uses_default(self):
        r = ResilienceManager()
        r.register_sensor("sensor.grid", default=2000, margin=0.1)
        r.update_sensor("sensor.grid", 1500, ts=100)
        val, fb = r.get_value("sensor.grid", None, ts=500)  # 400s > 300s max_age
        assert val == 2000
        assert fb is True

    def test_nan_treated_as_unavailable(self):
        r = ResilienceManager()
        r.register_sensor("sensor.grid", default=2000)
        r.update_sensor("sensor.grid", 1500, ts=100)
        val, fb = r.get_value("sensor.grid", float("nan"), ts=150)
        assert fb is True

    def test_unregistered_sensor(self):
        r = ResilienceManager()
        val, fb = r.get_value("sensor.unknown", 500)
        assert val == 500
        assert fb is False


class TestCircuitBreaker:
    def test_closed_by_default(self):
        r = ResilienceManager()
        r.register_breaker("goodwe_kontor")
        assert not r.is_breaker_open("goodwe_kontor")

    def test_trips_after_max_errors(self):
        r = ResilienceManager()
        r.register_breaker("goodwe_kontor", max_errors=3)
        r.record_error("goodwe_kontor", ts=100)
        r.record_error("goodwe_kontor", ts=101)
        assert not r.is_breaker_open("goodwe_kontor", ts=102)
        r.record_error("goodwe_kontor", ts=102)
        assert r.is_breaker_open("goodwe_kontor", ts=103)

    def test_resets_after_cooldown(self):
        r = ResilienceManager()
        r.register_breaker("goodwe_kontor", max_errors=3, cooldown_s=60)
        for i in range(3):
            r.record_error("goodwe_kontor", ts=float(i))
        assert r.is_breaker_open("goodwe_kontor", ts=10)
        assert not r.is_breaker_open("goodwe_kontor", ts=70)

    def test_success_resets_counter(self):
        r = ResilienceManager()
        r.register_breaker("goodwe_kontor", max_errors=3)
        r.record_error("goodwe_kontor")
        r.record_error("goodwe_kontor")
        r.record_success("goodwe_kontor")
        r.record_error("goodwe_kontor")
        assert not r.is_breaker_open("goodwe_kontor")


class TestRateLimiter:
    def test_allows_within_limit(self):
        r = ResilienceManager()
        r._rate_limiter.max_per_hour = 5
        for _ in range(5):
            assert r.check_rate_limit(14)

    def test_blocks_over_limit(self):
        r = ResilienceManager()
        r._rate_limiter.max_per_hour = 3
        for _ in range(3):
            r.check_rate_limit(14)
        assert not r.check_rate_limit(14)

    def test_resets_on_new_hour(self):
        r = ResilienceManager()
        r._rate_limiter.max_per_hour = 3
        for _ in range(3):
            r.check_rate_limit(14)
        assert r.check_rate_limit(15)  # New hour


class TestDegradedMode:
    def test_normal(self):
        r = ResilienceManager()
        assert r.degraded_level == 0
        assert r.status == "Normal"

    def test_adapter_offline(self):
        r = ResilienceManager()
        r.register_breaker("gw", max_errors=2)
        r.record_error("gw", ts=100)
        r.record_error("gw", ts=101)
        assert r.degraded_level == 2
        assert "adapter" in r.status.lower()
