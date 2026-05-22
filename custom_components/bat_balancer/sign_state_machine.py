"""SignStateMachine — INV-34 ramp via 0 at direction change (spec BAL-10-BAT §5).

Brain SHOULD send target via 0 on direction change, but bat_balancer enforces
the ramp as a failsafe. If a direct sign flip is detected, it:
  1. Sets power_limit = 0 this tick (ramp through zero)
  2. Logs inv34_sign_flip_detected (Brain-side alert)
  3. Next tick allows full magnitude in new direction

States:
  CHARGING        — last confirmed write was negative (charge = negative)
  DISCHARGING     — last confirmed write was positive (discharge = positive)
  IDLE            — last confirmed write was 0
  RAMPING_THROUGH_ZERO — one zero-tick inserted (returns to normal next tick)
"""

from __future__ import annotations

import logging
from enum import StrEnum

_LOGGER = logging.getLogger(__name__)

_FLOAT_TOL = 1e-6


class SignState(StrEnum):
    CHARGING = "charging"
    DISCHARGING = "discharging"
    IDLE = "idle"
    RAMPING_THROUGH_ZERO = "ramping_through_zero"


class SignStateMachine:
    """Enforces INV-34: no direct sign flip without a zero-tick intermediate."""

    def __init__(self) -> None:
        self._state: SignState = SignState.IDLE
        self._ramp_target_w: float = 0.0  # destination after zero-tick

    @property
    def state(self) -> SignState:
        return self._state

    @property
    def sign_flip_pending(self) -> bool:
        return self._state == SignState.RAMPING_THROUGH_ZERO

    def tick(self, new_target_w: float) -> float:
        """Process new target and return the clamped target for this tick.

        If a sign flip is detected (+ → - or - → +), returns 0 this tick
        and stores new_target_w for the next tick.
        """
        if self._state == SignState.RAMPING_THROUGH_ZERO:
            # Zero-tick was issued last call — now allow full new direction
            destination = self._ramp_target_w
            self._ramp_target_w = 0.0
            self._state = _sign_to_state(destination)
            _LOGGER.debug("bat_balancer: INV-34 ramp complete → %.0fW", destination)
            return destination

        current_direction = _state_direction(self._state)
        new_direction = _target_direction(new_target_w)

        is_sign_flip = (
            current_direction != 0 and new_direction != 0 and current_direction != new_direction
        )

        if is_sign_flip:
            _LOGGER.warning(
                "bat_balancer: inv34_sign_flip_detected — %.0fW → %.0fW; "
                "inserting zero-tick (INV-34). Brain should send via 0.",
                _direction_to_sign(current_direction) * abs(new_target_w - new_target_w),
                new_target_w,
            )
            self._state = SignState.RAMPING_THROUGH_ZERO
            self._ramp_target_w = new_target_w
            return 0.0

        # Normal transition (no flip)
        self._state = _sign_to_state(new_target_w)
        return new_target_w

    def reset(self) -> None:
        self._state = SignState.IDLE
        self._ramp_target_w = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _target_direction(w: float) -> int:
    if w > _FLOAT_TOL:
        return 1
    if w < -_FLOAT_TOL:
        return -1
    return 0


def _state_direction(state: SignState) -> int:
    if state == SignState.CHARGING:
        return -1
    if state == SignState.DISCHARGING:
        return 1
    return 0


def _sign_to_state(w: float) -> SignState:
    if w < -_FLOAT_TOL:
        return SignState.CHARGING
    if w > _FLOAT_TOL:
        return SignState.DISCHARGING
    return SignState.IDLE


def _direction_to_sign(direction: int) -> float:
    return float(direction)
