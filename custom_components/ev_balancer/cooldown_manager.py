"""CooldownManager — five independent dwell/cooldown timers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import CooldownType


@dataclass
class CooldownManager:
    """Tracks five independent dwell/cooldown timers.

    Usage:
        cm.start(CooldownType.AMP_DWELL, duration_s)
        cm.tick(elapsed_s)          # call every cycle
        cm.is_blocked(type) -> bool
        cm.remaining(type)  -> float (seconds left)
    """

    _timers: dict[CooldownType, float] = field(default_factory=dict)

    def start(self, cooldown: CooldownType, duration_s: float) -> None:
        self._timers[cooldown] = max(0.0, duration_s)

    def tick(self, elapsed_s: float) -> None:
        for key in list(self._timers):
            self._timers[key] = max(0.0, self._timers[key] - elapsed_s)

    def is_blocked(self, cooldown: CooldownType) -> bool:
        return self._timers.get(cooldown, 0.0) > 0.0

    def remaining(self, cooldown: CooldownType) -> float:
        return self._timers.get(cooldown, 0.0)

    def cancel(self, cooldown: CooldownType) -> None:
        self._timers.pop(cooldown, None)

    def active_cooldowns(self) -> list[tuple[str, float]]:
        return [(k.value, v) for k, v in self._timers.items() if v > 0.0]

    def __repr__(self) -> str:
        active = ", ".join(f"{k}={v:.1f}s" for k, v in self._timers.items() if v > 0)
        return f"CooldownManager({active or 'idle'})"
