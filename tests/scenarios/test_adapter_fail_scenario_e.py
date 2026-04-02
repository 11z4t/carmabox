"""Scenario E — Adapter failure and recovery.

State: GoodWe adapter becomes unavailable 01:00-01:05
Expected:
  - SafetyGuard blocks discharge when SoC unavailable (-1)
  - Circuit breaker activates after 3 consecutive failures
  - System continues in degraded mode (no crash)
  - Recovery: circuit breaker resets when adapter comes back
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.models import CarmaboxState
from custom_components.carmabox.optimizer.safety_guard import SafetyGuard


class TestScenarioE:
    """Scenario E: GoodWe adapter failure and recovery."""

    def test_safety_blocks_discharge_with_unavailable_soc(self) -> None:
        """SafetyGuard blocks discharge when SoC=-1 (unavailable)."""
        guard = SafetyGuard()
        result = guard.check_discharge(
            soc_1=-1.0,
            soc_2=-1.0,
            min_soc=15.0,
            grid_power_w=2000.0,
        )
        assert result.ok is False, "Should block discharge with unavailable SoC"

    def test_safety_blocks_discharge_below_min_soc(self) -> None:
        """SafetyGuard blocks discharge when SoC < min_soc."""
        guard = SafetyGuard()
        result = guard.check_discharge(
            soc_1=10.0,
            soc_2=50.0,
            min_soc=15.0,
            grid_power_w=1000.0,
        )
        assert result.ok is False

    def test_safety_allows_discharge_above_min_soc(self) -> None:
        """SafetyGuard allows discharge when SoC > min_soc."""
        guard = SafetyGuard()
        result = guard.check_discharge(
            soc_1=50.0,
            soc_2=50.0,
            min_soc=15.0,
            grid_power_w=1000.0,
        )
        assert result.ok is True

    def test_degraded_state_does_not_crash(self) -> None:
        """CarmaboxState with all defaults doesn't crash any property."""
        state = CarmaboxState()
        # These should all work without exception
        assert state.is_exporting is False  # grid=0 → neither importing nor exporting
        assert state.total_battery_soc == 0.0
        assert state.has_battery_2 is False  # soc_2=-1

    def test_circuit_breaker_constants_defined(self) -> None:
        """Circuit breaker constants exist for GoodWe self-healing."""
        from custom_components.carmabox.coordinator import (
            SELF_HEALING_MAX_FAILURES,
            SELF_HEALING_PAUSE_SECONDS,
        )

        assert SELF_HEALING_MAX_FAILURES == 3
        assert SELF_HEALING_PAUSE_SECONDS == 300
