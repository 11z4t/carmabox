"""Surplus Planner — thin wrapper around surplus_chain for SurplusPlan output.

Pure Python. No HA imports. Fully testable.

Responsibilities:
  - Apply switch-rate limiting (MAX_SURPLUS_SWITCHES_PER_WINDOW per window)
  - Delegate actual allocation to surplus_chain.allocate_surplus()
  - Compute self_consumption_ratio
  - Return SurplusPlan dataclass
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..const import MAX_SURPLUS_SWITCHES_PER_WINDOW, SURPLUS_SWITCH_WINDOW_MIN
from ..core.surplus_chain import (
    HysteresisState,
    SurplusAllocation,
    SurplusConfig,
    SurplusConsumer,
    SwitchTracker,
    allocate_surplus,
    build_default_consumers,
)


@dataclass
class SurplusPlan:
    """Result of a SurplusPlanner allocation cycle."""

    allocations: list[SurplusAllocation]
    self_consumption_ratio: float  # 0.0-1.0: fraction of surplus consumed locally
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "allocations": [
                {
                    "id": a.id,
                    "action": a.action,
                    "target_w": a.target_w,
                    "current_w": a.current_w,
                    "reason": a.reason,
                }
                for a in self.allocations
            ],
            "self_consumption_ratio": self.self_consumption_ratio,
            "timestamp": self.timestamp.isoformat(),
        }


class SurplusPlanner:
    """Wrapper that adds switch-rate limiting and plan output to surplus_chain.

    Args:
        scenario_engine: Optional ScenarioEngine (stored, not used in basic flow).
        cost_model: Optional CostModel (stored, not used in basic flow).
        consumers: Consumer list; defaults to build_default_consumers().
        config: Surplus chain config; defaults to SurplusConfig().
    """

    def __init__(
        self,
        scenario_engine: object | None = None,
        cost_model: object | None = None,
        consumers: list[SurplusConsumer] | None = None,
        config: SurplusConfig | None = None,
    ) -> None:
        self._scenario_engine = scenario_engine
        self._cost_model = cost_model
        self._consumers: list[SurplusConsumer] = (
            consumers if consumers is not None else build_default_consumers()
        )
        self._config = config if config is not None else SurplusConfig()
        self._hysteresis = HysteresisState()
        self._switch_tracker = SwitchTracker()

    # ── Public API ───────────────────────────────────────────────

    def allocate_surplus(
        self,
        available_kw: float,
        device_states: dict[str, Any],
        now: datetime | None = None,
    ) -> SurplusPlan:
        """Allocate PV surplus to consumers and return a SurplusPlan.

        Args:
            available_kw: Available PV surplus in kW (positive = exporting).
            device_states: Mapping of consumer_id → state dict with optional keys:
                - is_running (bool)
                - current_w (float)
                - dependency_met (bool)
            now: Override current time (for testing).

        Returns:
            SurplusPlan with allocations, self_consumption_ratio, and timestamp.
        """
        ts = now if now is not None else datetime.now()

        # Update live consumer state from caller
        self._apply_device_states(device_states)

        available_w = available_kw * 1000.0

        # Switch-rate limiting: if limit exceeded, return no-action plan
        if not self._switch_tracker._check_switch_limit(
            window_min=SURPLUS_SWITCH_WINDOW_MIN,
            max_switches=MAX_SURPLUS_SWITCHES_PER_WINDOW,
        ):
            allocations = [
                SurplusAllocation(
                    c.id,
                    "none",
                    c.current_w if c.is_running else 0.0,
                    c.current_w,
                    "switch limit exceeded",
                )
                for c in self._consumers
            ]
            return SurplusPlan(
                allocations=allocations,
                self_consumption_ratio=self._ratio(available_w, 0.0),
                timestamp=ts,
            )

        result = allocate_surplus(
            available_w,
            self._consumers,
            hysteresis=self._hysteresis,
            config=self._config,
        )

        if result.actions_taken > 0:
            self._switch_tracker.record_switch(ts)

        return SurplusPlan(
            allocations=result.allocations,
            self_consumption_ratio=result.self_consumption_ratio,
            timestamp=ts,
        )

    # ── Internals ────────────────────────────────────────────────

    def _apply_device_states(self, device_states: dict[str, Any]) -> None:
        for consumer in self._consumers:
            state = device_states.get(consumer.id)
            if not isinstance(state, dict):
                continue
            if "is_running" in state:
                consumer.is_running = bool(state["is_running"])
            if "current_w" in state:
                consumer.current_w = float(state["current_w"])
            if "dependency_met" in state:
                consumer.dependency_met = bool(state["dependency_met"])

    @staticmethod
    def _ratio(available_w: float, allocated_w: float) -> float:
        if available_w <= 0:
            return 1.0
        return min(1.0, allocated_w / available_w)
