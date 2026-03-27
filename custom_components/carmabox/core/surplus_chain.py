"""Surplus Chain — knapsack allocation of PV surplus to consumers.

Pure Python. No HA imports. Fully testable.

Goal: ZERO export. Every watt of PV surplus should be consumed locally.
Export is the ABSOLUTE last resort.

Key principles:
  - Minimize export > follow priority list
  - Increase existing variable consumer BEFORE starting new one
  - Knapsack: if high-prio doesn't fit, try lower-prio that DOES fit
  - Bump: stop low-prio to make room for high-prio when surplus grows
  - Hysteresis: prevent oscillation at cloud edges
  - Dependencies: VP pool requires cirkpump ON (user-started)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ConsumerType(Enum):
    VARIABLE = "variable"  # Can adjust power (EV amps, battery charge rate)
    ON_OFF = "on_off"  # Only on or off (miner, pool heater)
    CLIMATE = "climate"  # HVAC setpoint control


@dataclass
class SurplusConsumer:
    """A controllable consumer in the surplus chain."""

    id: str
    name: str
    priority: int  # Lower = higher priority at surplus
    type: ConsumerType
    min_w: float  # Minimum operating power
    max_w: float  # Maximum power
    current_w: float = 0.0  # Current actual power draw
    is_running: bool = False
    entity_switch: str = ""
    entity_climate: str = ""
    phase_count: int = 1  # For EV: 1 or 3
    requires_active: str = ""  # Dependency: must be ON (e.g. cirkpump)
    dependency_met: bool = True  # Is dependency satisfied?
    auto_start_dependency: bool = False  # Can we start dependency?


@dataclass
class SurplusAllocation:
    """Result for one consumer."""

    id: str
    action: str  # "start" | "stop" | "increase" | "decrease" | "none"
    target_w: float  # Target power (0 = stop)
    current_w: float
    reason: str


@dataclass
class SurplusResult:
    """Overall surplus chain result."""

    allocations: list[SurplusAllocation]
    surplus_w: float  # Input surplus
    allocated_w: float  # Total allocated
    export_w: float  # Remaining export (goal: 0)
    actions_taken: int


@dataclass
class HysteresisState:
    """Track timing for hysteresis decisions."""

    surplus_above_since: dict[str, float] = field(default_factory=dict)
    surplus_below_since: dict[str, float] = field(default_factory=dict)


@dataclass
class SurplusConfig:
    """Parameterstyrd konfiguration."""

    start_delay_s: float = 60.0  # Wait before starting consumer
    stop_delay_s: float = 180.0  # Wait before stopping consumer
    bump_delay_s: float = 60.0  # Wait before bumping low→high prio
    min_surplus_w: float = 50.0  # Ignore surplus below this


def allocate_surplus(
    surplus_w: float,
    consumers: list[SurplusConsumer],
    hysteresis: HysteresisState | None = None,
    config: SurplusConfig | None = None,
    now: float | None = None,
) -> SurplusResult:
    """Allocate PV surplus to consumers. Minimize export.

    Algorithm:
    1. Increase existing variable consumers (before starting new ones)
    2. Fill consumers that FIT, priority order (knapsack)
    3. Bump: stop low-prio to make room for high-prio
    4. Hysteresis: respect timing delays

    Args:
        surplus_w: Available PV surplus (W). Positive = exporting.
        consumers: All controllable consumers.
        hysteresis: Timing state for start/stop delays.
        config: Configuration.
        now: Current timestamp (monotonic).

    Returns:
        SurplusResult with per-consumer allocations.
    """
    cfg = config or SurplusConfig()
    hyst = hysteresis or HysteresisState()
    ts = now if now is not None else time.monotonic()

    if surplus_w < cfg.min_surplus_w:
        # Clear start timers — surplus dropped
        for c in consumers:
            hyst.surplus_above_since.pop(c.id, None)
        return SurplusResult(
            allocations=[
                SurplusAllocation(c.id, "none", c.current_w, c.current_w, "")
                for c in consumers
            ],
            surplus_w=surplus_w,
            allocated_w=0,
            export_w=max(0, surplus_w),
            actions_taken=0,
        )

    # Sort by priority (lower number = higher surplus priority)
    sorted_consumers = sorted(consumers, key=lambda c: c.priority)
    remaining = surplus_w
    allocations: list[SurplusAllocation] = []
    actions = 0

    # ── Pass 1: Increase existing variable consumers ────────────
    for c in sorted_consumers:
        if remaining <= 0:
            break
        if not c.is_running or c.type != ConsumerType.VARIABLE:
            continue
        if c.current_w >= c.max_w:
            continue

        headroom = c.max_w - c.current_w

        # For EV: check if increase is meaningful (full amp steps)
        if c.phase_count > 1:
            w_per_step = 230 * c.phase_count
            steps = int(remaining / w_per_step)
            if steps <= 0:
                continue
            increase = steps * w_per_step
        else:
            increase = min(remaining, headroom)

        increase = min(increase, headroom)
        if increase > 50:
            allocations.append(SurplusAllocation(
                c.id, "increase", c.current_w + increase, c.current_w,
                f"Öka {c.name} +{increase:.0f}W",
            ))
            remaining -= increase
            actions += 1
        else:
            allocations.append(SurplusAllocation(
                c.id, "none", c.current_w, c.current_w, "",
            ))

    # ── Pass 2: Start new consumers that fit (knapsack) ─────────
    for c in sorted_consumers:
        if remaining <= 0:
            break
        if c.is_running:
            continue  # Already handled in pass 1
        if not c.dependency_met:
            continue  # Dependency not met (e.g. cirkpump off)

        if remaining >= c.min_w:
            # Check hysteresis: has surplus been above min_w long enough?
            if not _hysteresis_start_ok(c.id, remaining, c.min_w, hyst, cfg, ts):
                allocations.append(SurplusAllocation(
                    c.id, "none", 0, 0, f"Väntar {cfg.start_delay_s:.0f}s",
                ))
                continue

            alloc_w = min(c.max_w, remaining)
            allocations.append(SurplusAllocation(
                c.id, "start", alloc_w, 0,
                f"Starta {c.name} {alloc_w:.0f}W",
            ))
            remaining -= alloc_w
            actions += 1
        else:
            # Track that surplus is below this consumer's min
            _hysteresis_reset_start(c.id, hyst)
            allocations.append(SurplusAllocation(
                c.id, "none", 0, 0, f"Överskott {remaining:.0f}W < min {c.min_w:.0f}W",
            ))

    # ── Pass 3: Bump — stop low-prio to make room for high-prio ─
    if remaining < 0:
        # We over-allocated — shouldn't happen
        pass
    elif remaining > cfg.min_surplus_w:
        # Still have surplus. Can we bump a running low-prio
        # to make room for a higher-prio that doesn't fit?
        not_started = [
            c for c in sorted_consumers
            if not c.is_running
            and c.dependency_met
            and not any(a.id == c.id and a.action == "start" for a in allocations)
        ]
        running_low = [
            c for c in sorted_consumers
            if c.is_running
            and not any(a.id == c.id and a.action in ("increase", "start") for a in allocations)
        ]

        for high in not_started:
            if remaining >= high.min_w:
                continue  # Already fits — should have been started in pass 2

            # Can we free enough by stopping lower-prio consumers?
            freeable = sum(
                c.current_w for c in running_low
                if c.priority > high.priority
            )
            if remaining + freeable >= high.min_w:
                # Check hysteresis for bump
                if not _hysteresis_start_ok(
                    f"bump_{high.id}", remaining + freeable,
                    high.min_w, hyst, cfg, ts,
                ):
                    continue

                # Stop low-prio consumers until we have enough
                freed = 0
                for low in sorted(running_low, key=lambda c: -c.priority):
                    if low.priority <= high.priority:
                        continue
                    if remaining + freed >= high.min_w:
                        break
                    # Check stop hysteresis
                    if _hysteresis_stop_ok(low.id, hyst, cfg, ts):
                        freed += low.current_w
                        # Update allocation for stopped consumer
                        _update_allocation(
                            allocations, low.id, "stop", 0,
                            f"Bump: stoppa {low.name} för {high.name}",
                        )
                        actions += 1

                if remaining + freed >= high.min_w:
                    alloc_w = min(high.max_w, remaining + freed)
                    allocations.append(SurplusAllocation(
                        high.id, "start", alloc_w, 0,
                        f"Bump: starta {high.name} {alloc_w:.0f}W",
                    ))
                    remaining = remaining + freed - alloc_w
                    actions += 1

    # ── Fill remaining consumers with "none" ────────────────────
    handled_ids = {a.id for a in allocations}
    for c in consumers:
        if c.id not in handled_ids:
            allocations.append(SurplusAllocation(
                c.id, "none", c.current_w if c.is_running else 0,
                c.current_w, "",
            ))

    allocated = surplus_w - remaining
    return SurplusResult(
        allocations=allocations,
        surplus_w=surplus_w,
        allocated_w=allocated,
        export_w=max(0, remaining),
        actions_taken=actions,
    )


def should_reduce_consumers(
    deficit_w: float,
    consumers: list[SurplusConsumer],
    hysteresis: HysteresisState | None = None,
    config: SurplusConfig | None = None,
    now: float | None = None,
) -> list[SurplusAllocation]:
    """When importing, reduce/stop consumers in reverse priority.

    Called when grid is importing and we need to reduce load.

    Args:
        deficit_w: How much we need to reduce (positive = importing).
        consumers: All consumers.

    Returns:
        List of reduction allocations.
    """
    cfg = config or SurplusConfig()
    hyst = hysteresis or HysteresisState()
    ts = now if now is not None else time.monotonic()

    # Sort by priority DESCENDING (stop lowest-prio first)
    sorted_desc = sorted(consumers, key=lambda c: -c.priority)
    remaining = deficit_w
    reductions: list[SurplusAllocation] = []

    for c in sorted_desc:
        if remaining <= 0:
            break
        if not c.is_running:
            continue

        if _hysteresis_stop_ok(c.id, hyst, cfg, ts):
            if c.type == ConsumerType.VARIABLE and c.current_w > c.min_w:
                # Reduce variable consumer
                reduce = min(c.current_w - c.min_w, remaining)
                reductions.append(SurplusAllocation(
                    c.id, "decrease", c.current_w - reduce, c.current_w,
                    f"Minska {c.name} -{reduce:.0f}W",
                ))
                remaining -= reduce
            else:
                # Stop on/off consumer
                reductions.append(SurplusAllocation(
                    c.id, "stop", 0, c.current_w,
                    f"Stoppa {c.name} ({c.current_w:.0f}W)",
                ))
                remaining -= c.current_w

    return reductions


# ── Hysteresis helpers ──────────────────────────────────────────


def _hysteresis_start_ok(
    consumer_id: str,
    surplus_w: float,
    min_w: float,
    hyst: HysteresisState,
    cfg: SurplusConfig,
    ts: float,
) -> bool:
    """Check if surplus has been above min_w long enough to start."""
    if cfg.start_delay_s <= 0:
        return surplus_w >= min_w  # No delay → immediate
    if surplus_w >= min_w:
        if consumer_id not in hyst.surplus_above_since:
            hyst.surplus_above_since[consumer_id] = ts
            return False  # First time — start timer
        elapsed = ts - hyst.surplus_above_since[consumer_id]
        return elapsed >= cfg.start_delay_s
    else:
        hyst.surplus_above_since.pop(consumer_id, None)
        return False


def _hysteresis_reset_start(consumer_id: str, hyst: HysteresisState) -> None:
    """Reset start timer when surplus drops below min_w."""
    hyst.surplus_above_since.pop(consumer_id, None)


def _hysteresis_stop_ok(
    consumer_id: str,
    hyst: HysteresisState,
    cfg: SurplusConfig,
    ts: float,
) -> bool:
    """Check if consumer has been below threshold long enough to stop."""
    if cfg.stop_delay_s <= 0:
        return True  # No delay → immediate
    if consumer_id not in hyst.surplus_below_since:
        hyst.surplus_below_since[consumer_id] = ts
        return False
    elapsed = ts - hyst.surplus_below_since[consumer_id]
    return elapsed >= cfg.stop_delay_s


def _update_allocation(
    allocations: list[SurplusAllocation],
    consumer_id: str,
    action: str,
    target_w: float,
    reason: str,
) -> None:
    """Update or add allocation for a consumer."""
    for a in allocations:
        if a.id == consumer_id:
            a.action = action
            a.target_w = target_w
            a.reason = reason
            return
    allocations.append(SurplusAllocation(
        consumer_id, action, target_w, 0, reason,
    ))
