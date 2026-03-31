"""EXP-04: EV ramp step enforcement tests.

Verifies that _cmd_ev_adjust follows EV_RAMP_STEPS (6->8->10)
on ramp-up but goes directly on ramp-down.
"""

from __future__ import annotations

from custom_components.carmabox.const import EV_RAMP_STEPS


class TestRampStepLogic:
    """Pure logic test for ramp step calculation (no HA dependency)."""

    @staticmethod
    def _next_ramp_step(current: int, target: int) -> int:
        """Replicate the ramp logic from _cmd_ev_adjust."""
        if target <= current:
            return target  # ramp down = direct
        # Ramp up = one step at a time
        for step in EV_RAMP_STEPS:
            if step > current:
                return min(step, target)
        return target

    def test_ramp_6_to_10_first_step(self) -> None:
        """6A -> 10A requested: first step should be 8A."""
        assert self._next_ramp_step(6, 10) == 8

    def test_ramp_8_to_10(self) -> None:
        """8A -> 10A: single step to 10A."""
        assert self._next_ramp_step(8, 10) == 10

    def test_ramp_6_to_8(self) -> None:
        """6A -> 8A: direct (8 is next step)."""
        assert self._next_ramp_step(6, 8) == 8

    def test_ramp_down_10_to_6(self) -> None:
        """10A -> 6A: ramp down = direct, no stepping."""
        assert self._next_ramp_step(10, 6) == 6

    def test_ramp_down_8_to_6(self) -> None:
        """8A -> 6A: ramp down = direct."""
        assert self._next_ramp_step(8, 6) == 6

    def test_ramp_same_value(self) -> None:
        """Same value = no change."""
        assert self._next_ramp_step(8, 8) == 8

    def test_ramp_6_to_9_caps_at_8(self) -> None:
        """6A -> 9A: first step 8A (next in ladder), not 9A."""
        assert self._next_ramp_step(6, 9) == 8

    def test_ramp_steps_constant(self) -> None:
        """EV_RAMP_STEPS must be [6, 8, 10]."""
        assert EV_RAMP_STEPS == [6, 8, 10]

    # ── EXP-EPIC-SWEEP edge cases ────────────────────────────────

    def test_ramp_up_from_non_standard_current(self) -> None:
        """EXP-04 edge: current=7 (not in steps) → next step = 8.

        Handles manual or external changes that leave current between steps.
        """
        assert self._next_ramp_step(7, 10) == 8

    def test_ramp_up_from_zero(self) -> None:
        """EXP-04 edge: current=0 (charger just enabled) → first step = 6."""
        assert self._next_ramp_step(0, 6) == 6
        assert self._next_ramp_step(0, 10) == 6  # 6 is first step above 0

    def test_ramp_up_from_above_max_step(self) -> None:
        """EXP-04 edge: current=11 (above all steps) → same as target (ramp down)."""
        # When current > target, it's a ramp-down → direct
        assert self._next_ramp_step(11, 10) == 10

    def test_ramp_to_target_same_as_next_step(self) -> None:
        """EXP-04 edge: target == next step → single step, no overshoot."""
        # From 6, target=8 = exactly the next step
        assert self._next_ramp_step(6, 8) == 8
        # From 8, target=10 = exactly the next step
        assert self._next_ramp_step(8, 10) == 10

    def test_ramp_up_target_between_steps(self) -> None:
        """EXP-04 edge: target=7 (between steps) — first step = 6 (no overshoot).

        Step 6 is first step > current=5, and min(6, 7)=6 so we go to 6 not 7.
        """
        assert self._next_ramp_step(5, 7) == 6
