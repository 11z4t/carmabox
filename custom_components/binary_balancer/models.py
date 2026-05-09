"""Pure data models — no HA imports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum


class FaultState(str, Enum):
    """Fault state for a binary balancer asset."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    FAULT = "FAULT"
    OFFLINE = "OFFLINE"
    SAFE_STATE_ACTIVE = "SAFE_STATE_ACTIVE"


class SwitchAction(str, Enum):
    """Action to apply to the switch entity."""

    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    HOLD = "hold"


@dataclass(frozen=True)
class AssetConfig:
    """Configuration for a single binary-balancer asset."""

    asset_id: str
    typical_drag_w: float
    switch_entity: str
    season_active_entity: str | None  # None = always active
    min_dwell_s: int
    hysteresis_on_pct: float = 80.0
    hysteresis_off_pct: float = 70.0

    @property
    def on_threshold_w(self) -> float:
        """Return the target_w at which the switch should turn ON."""
        return self.typical_drag_w * (self.hysteresis_on_pct / 100.0)

    @property
    def off_threshold_w(self) -> float:
        """Return the target_w below which the switch should turn OFF."""
        return self.typical_drag_w * (self.hysteresis_off_pct / 100.0)


@dataclass(frozen=True)
class ActionMessage:
    """Decoded Brain ACTION message for a binary asset."""

    asset_id: str
    target_w: float
    mode: str  # "on" | "off"
    deadline_s: int
    reason: str
    cycle_id: int
    source: str
    ts: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ActionMessage:
        """Construct from compact JSON dict."""
        return cls(
            asset_id=str(d["a"]),
            target_w=float(str(d.get("w", 0.0))),
            mode=str(d["m"]),
            deadline_s=int(str(d.get("dl", 120))),
            reason=str(d.get("r", "")),
            cycle_id=int(float(str(d["c"]))),
            source=str(d.get("s", "unknown")),
            ts=str(d.get("t", "")),
        )


@dataclass(frozen=True)
class SensorSnapshot:
    """Point-in-time sensor readings used by translate()."""

    switch_is_on: bool
    switch_unavailable: bool
    season_active: bool  # True if no entity configured OR entity state == "on"
    guardian_safe_state: bool
    action_age_s: float  # seconds since ACTION published


@dataclass(frozen=True)
class BalancerState:
    """Mutable balancer state tracked between cycles."""

    switch_on: bool = False
    last_change_ts: float = 0.0
    last_cycle_id: int = -1
    offline_cycles: int = 0
    uptime_s: float = 0.0
    stale_cycles: int = 0


@dataclass(frozen=True)
class TranslateResult:
    """Output of translate() — what action to take and why."""

    action: SwitchAction
    reason: str
    fault_state: FaultState
    fault_detail: str
    actual_w: float
    rejected_w: float
    rejected_reason: str
    stale_action: bool
    cap_engage: bool


@dataclass(frozen=True)
class FeedbackMessage:
    """FEEDBACK message written back to HA input_text helper."""

    asset_id: str
    actual_w: float
    fault_state: str
    fault_detail: str
    cap_engage: bool
    cap_max_w: float
    cap_min_w: float
    rejected_w: float
    rejected_reason: str
    stale_action: bool
    ts: str

    def to_compact_dict(self) -> dict[str, object]:
        """Return compact format for HA input_text (≤255 chars)."""
        return {
            "a": self.asset_id,
            "w": self.actual_w,
            "fs": self.fault_state,
            "fd": self.fault_detail[:40],
            "ce": self.cap_engage,
            "mx": self.cap_max_w,
            "mn": self.cap_min_w,
            "rw": self.rejected_w,
            "rr": self.rejected_reason[:30],
            "st": self.stale_action,
            "t": self.ts,
        }

    def to_json(self) -> str:
        """Serialise to JSON string (≤255 chars target)."""
        return json.dumps(self.to_compact_dict(), separators=(",", ":"))
