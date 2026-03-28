"""Resilience — self-healing, sensor fallback, circuit breaker.

Pure Python. No HA imports. Fully testable.

Ensures CARMA Box NEVER stops. Degrades gracefully at each level:
  Level 1: Sensor unavailable → use fallback
  Level 2: Adapter offline → standby, pause EV
  Level 3: Coordinator crash → watchdog restart
  Level 4: HA down → external watchdog alarm (LXC 506)

Key principles:
  - Every sensor has a fallback value
  - Circuit breaker: 5 consecutive errors → pause 60s
  - Rate limiter: max 60 mode changes per hour
  - Startup safety: fast_charging OFF before anything else
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SensorFallback:
    """Fallback value for a sensor."""

    entity_id: str
    last_known: float = 0.0
    last_update: float = 0.0
    margin: float = 0.1  # Add 10% margin to last known
    max_age_s: float = 300.0  # Max age before considered stale
    default: float = 0.0  # Ultimate fallback


@dataclass
class CircuitBreakerState:
    """Circuit breaker for adapter calls."""

    consecutive_errors: int = 0
    tripped: bool = False
    trip_time: float = 0.0
    cooldown_s: float = 60.0
    max_errors: int = 5


@dataclass
class RateLimiterState:
    """Rate limiter for mode changes."""

    changes_this_hour: int = 0
    hour: int = -1
    max_per_hour: int = 60


class ResilienceManager:
    """Manages fallbacks, circuit breakers, and rate limiting."""

    def __init__(self) -> None:
        self._fallbacks: dict[str, SensorFallback] = {}
        self._circuit_breakers: dict[str, CircuitBreakerState] = {}
        self._rate_limiter = RateLimiterState()
        self._startup_done = False
        self._degraded_level = 0  # 0=normal, 1-4=degraded

    # ── Sensor Fallback ─────────────────────────────────────────

    def register_sensor(
        self, entity_id: str, default: float = 0.0, margin: float = 0.1,
    ) -> None:
        """Register a sensor with fallback configuration."""
        self._fallbacks[entity_id] = SensorFallback(
            entity_id=entity_id, default=default, margin=margin,
        )

    def update_sensor(self, entity_id: str, value: float, ts: float | None = None) -> None:
        """Update last known value for a sensor."""
        now = ts or time.monotonic()
        if entity_id not in self._fallbacks:
            self.register_sensor(entity_id)
        fb = self._fallbacks[entity_id]
        fb.last_known = value
        fb.last_update = now

    def get_value(
        self, entity_id: str, current: float | None, ts: float | None = None,
    ) -> tuple[float, bool]:
        """Get sensor value with fallback. Returns (value, is_fallback)."""
        now = ts or time.monotonic()
        fb = self._fallbacks.get(entity_id)
        if fb is None:
            if current is not None:
                return current, False
            return 0.0, True

        # Current value available and valid
        if current is not None:
            try:
                if not _is_unavailable(current):
                    fb.last_known = current
                    fb.last_update = now
                    return current, False
            except (TypeError, ValueError):
                pass  # Treat as unavailable

        # Fallback to last known
        if fb.last_update > 0:
            age = now - fb.last_update
            if age < fb.max_age_s:
                # Add margin for safety
                fallback = fb.last_known * (1 + fb.margin)
                return fallback, True

        # Ultimate fallback
        return fb.default, True

    # ── Circuit Breaker ─────────────────────────────────────────

    def register_breaker(
        self, adapter_id: str, max_errors: int = 5, cooldown_s: float = 60.0,
    ) -> None:
        """Register circuit breaker for an adapter."""
        self._circuit_breakers[adapter_id] = CircuitBreakerState(
            max_errors=max_errors, cooldown_s=cooldown_s,
        )

    def record_success(self, adapter_id: str) -> None:
        """Record successful adapter call."""
        cb = self._circuit_breakers.get(adapter_id)
        if cb:
            cb.consecutive_errors = 0
            if cb.tripped:
                cb.tripped = False

    def record_error(self, adapter_id: str, ts: float | None = None) -> None:
        """Record failed adapter call."""
        now = ts or time.monotonic()
        if adapter_id not in self._circuit_breakers:
            self.register_breaker(adapter_id)
        cb = self._circuit_breakers[adapter_id]
        cb.consecutive_errors += 1
        if cb.consecutive_errors >= cb.max_errors:
            cb.tripped = True
            cb.trip_time = now

    def is_breaker_open(self, adapter_id: str, ts: float | None = None) -> bool:
        """Check if circuit breaker is tripped."""
        now = ts or time.monotonic()
        cb = self._circuit_breakers.get(adapter_id)
        if cb is None:
            return False
        if not cb.tripped:
            return False
        # Check cooldown
        elapsed = now - cb.trip_time
        if elapsed >= cb.cooldown_s:
            cb.tripped = False
            cb.consecutive_errors = 0
            return False
        return True

    # ── Rate Limiter ────────────────────────────────────────────

    def check_rate_limit(self, hour: int) -> bool:
        """Check if mode change is allowed. Returns True if OK."""
        if hour != self._rate_limiter.hour:
            self._rate_limiter.hour = hour
            self._rate_limiter.changes_this_hour = 0
        if self._rate_limiter.changes_this_hour >= self._rate_limiter.max_per_hour:
            return False
        self._rate_limiter.changes_this_hour += 1
        return True

    def get_rate_usage(self) -> tuple[int, int]:
        """Returns (current, max) mode changes this hour."""
        return self._rate_limiter.changes_this_hour, self._rate_limiter.max_per_hour

    # ── Degraded Mode ───────────────────────────────────────────

    @property
    def degraded_level(self) -> int:
        """0=normal, 1=sensor fallback, 2=adapter offline, 3=coordinator issues."""
        open_breakers = sum(1 for cb in self._circuit_breakers.values() if cb.tripped)
        fallback_sensors = sum(
            1 for fb in self._fallbacks.values()
            if fb.last_update > 0 and (time.monotonic() - fb.last_update) > fb.max_age_s
        )
        if open_breakers > 0:
            return 2
        if fallback_sensors > 0:
            return 1
        return 0

    @property
    def status(self) -> str:
        """Human-readable status."""
        level = self.degraded_level
        if level == 0:
            return "Normal"
        if level == 1:
            return "Degraderad: sensor fallback"
        if level == 2:
            return "Degraderad: adapter offline"
        return f"Degraderad: nivå {level}"


def _is_unavailable(value: float) -> bool:
    """Check if value represents unavailable sensor."""
    import math
    return math.isnan(value) or value < -90000
