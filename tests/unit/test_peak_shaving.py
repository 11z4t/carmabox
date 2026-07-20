"""EXP-03: Reactive peak-shaving control (core/peak_shaving.py).

Pure-function tests: formula, clamp, and mode gating.
"""

from __future__ import annotations

from custom_components.carmabox.const import PEAK_SHAVING_MAX_W, PEAK_SHAVING_MIN_W
from custom_components.carmabox.core.peak_shaving import (
    compute_reactive_peak_shaving_w,
    should_apply_reactive_peak_shaving,
)


class TestComputeReactivePeakShavingW:
    """AC2: peak_shaving_power_limit = actual_grid + target_headroom."""

    def test_formula_adds_headroom_to_actual_grid(self) -> None:
        """1500W grid + 200W headroom = 1700W limit."""
        assert compute_reactive_peak_shaving_w(1500.0, 200.0) == 1700

    def test_formula_tracks_lower_grid_reading(self) -> None:
        """House draws less → limit follows down (reactive compensation)."""
        assert compute_reactive_peak_shaving_w(300.0, 200.0) == 500

    def test_formula_tracks_higher_grid_reading(self) -> None:
        """House draws more → limit follows up (reactive compensation)."""
        assert compute_reactive_peak_shaving_w(4000.0, 200.0) == 4200

    def test_clamp_max_at_10000w(self) -> None:
        """Very high grid + headroom clamped to safety ceiling."""
        assert compute_reactive_peak_shaving_w(15000.0, 500.0) == PEAK_SHAVING_MAX_W

    def test_clamp_min_at_0w(self) -> None:
        """Exporting (negative grid) + small headroom still floors at 0W."""
        assert compute_reactive_peak_shaving_w(-500.0, 100.0) == PEAK_SHAVING_MIN_W

    def test_clamp_boundary_exactly_max(self) -> None:
        """Exactly at the ceiling is not clamped away."""
        assert compute_reactive_peak_shaving_w(9800.0, 200.0) == PEAK_SHAVING_MAX_W

    def test_zero_grid_returns_headroom_only(self) -> None:
        assert compute_reactive_peak_shaving_w(0.0, 200.0) == 200

    def test_result_is_int(self) -> None:
        """Register write must be an int, not a float."""
        result = compute_reactive_peak_shaving_w(1234.6, 200.4)
        assert isinstance(result, int)

    def test_custom_clamp_bounds_respected(self) -> None:
        """Caller-supplied max_w/min_w override the defaults."""
        assert compute_reactive_peak_shaving_w(1000.0, 0.0, max_w=500.0) == 500
        assert compute_reactive_peak_shaving_w(-1000.0, 0.0, min_w=50.0) == 50


class TestShouldApplyReactivePeakShaving:
    """AC3: only adjusts an ALREADY active discharge."""

    def test_true_for_discharge_pv(self) -> None:
        assert should_apply_reactive_peak_shaving("discharge_pv") is True

    def test_true_for_discharge_battery(self) -> None:
        assert should_apply_reactive_peak_shaving("discharge_battery") is True

    def test_false_for_charge_pv(self) -> None:
        """Never nudge peak-shaving while charging — protects charge invariants."""
        assert should_apply_reactive_peak_shaving("charge_pv") is False

    def test_false_for_battery_standby(self) -> None:
        assert should_apply_reactive_peak_shaving("battery_standby") is False

    def test_false_for_unknown_mode(self) -> None:
        assert should_apply_reactive_peak_shaving("") is False
        assert should_apply_reactive_peak_shaving("unknown_mode") is False
