"""PLAT-1241 (EXP-07): WoltaFeedbackTuner.

Continuous, self-adjusting parameter tuning driven by Wolta's own
optimeringsbetyg feedback.

Pure Python. No HA imports. Fully testable, per the core/optimizer pattern
used elsewhere in this package (see optimizer/bayesian_tuner.py,
optimizer/safety_guard.py).

Wolta exposes ``sensor.wolta_optimeringsbetyg`` with an attribute
``components``: a list of ``{key, label, captured, possible}`` entries,
covering at minimum ``selfuse`` (egenanvändning), ``solar_export`` (spara
sol till pristopp) and ``arbitrage`` (nätarbitrage). The gap
(``possible - captured``) per component shows where the largest untapped
potential currently sits.

This module implements a gradient-free "nudge" controller:

* Every tuning cycle looks at which component has the largest gap.
* It nudges the ONE parameter mapped to that component by exactly one
  ``step`` — never more, to avoid overreaction/oscillation on real
  batteries.
* If the score improved (or held steady) since the previous measurement,
  it continues in the same direction as last time. If it got worse, it
  reverses direction ("backar") — simple gradient-free hill-climbing.
* It NEVER exceeds the caller-supplied ``[min, max]`` bounds — an
  out-of-bounds nudge is clamped to the boundary, never beyond it.
* It NEVER adjusts anything on the very first run (no prior measurement
  to compare against) — that cycle only records the baseline.
* It NEVER adjusts anything when the rating is missing/null or stale
  (older than ``max_staleness``) — returns an explicit ``no_action``.

``auto_apply`` defaults to ``False`` everywhere in this module, matching
the shadow/live pattern used elsewhere (proposal-only by default; a
caller must opt in explicitly to have a proposal considered "applied").
This module never talks to hardware — it is a pure decision function
plus a small stateful wrapper for trend-tracking and an auditable
decision log.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from ..const import (
    WOLTA_TUNER_DEFAULT_MAX_STALENESS_S,
    WOLTA_TUNER_MAX_LOG_ENTRIES,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ComponentTarget",
    "ParameterAdjustment",
    "PreviousTuning",
    "TunableParameter",
    "TuningResult",
    "WoltaComponent",
    "WoltaFeedbackTuner",
    "WoltaRating",
    "suggest_tuning",
]

_LOGGER = logging.getLogger(__name__)

Direction = Literal[1, -1]
TuningStatus = Literal["baseline", "no_action", "adjusted"]

_DEFAULT_MAX_STALENESS = timedelta(seconds=WOLTA_TUNER_DEFAULT_MAX_STALENESS_S)


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WoltaComponent:
    """One entry of Wolta's ``components`` attribute."""

    key: str
    label: str
    captured: float
    possible: float

    @property
    def gap(self) -> float:
        """Untapped potential: possible - captured, floored at 0."""
        return max(0.0, self.possible - self.captured)


@dataclass(frozen=True)
class WoltaRating:
    """A single ``sensor.wolta_optimeringsbetyg`` snapshot.

    ``score`` is ``None`` when the sensor is unknown/unavailable — callers
    should pass ``None`` rather than a sentinel numeric value so the
    missing-data guard triggers correctly.
    """

    score: float | None
    components: tuple[WoltaComponent, ...]
    timestamp: datetime


@dataclass(frozen=True)
class TunableParameter:
    """A single adjustable control parameter and its allowed range.

    Mirrors the shape from the spec, e.g.::

        {"discharge_percentile": {"value": 85, "min": 50, "max": 95, "step": 2}}
    """

    value: float
    min: float
    max: float
    step: float

    def __post_init__(self) -> None:
        if self.min > self.max:
            raise ValueError(f"TunableParameter: min ({self.min}) > max ({self.max})")
        if self.step <= 0:
            raise ValueError(f"TunableParameter: step must be > 0, got {self.step}")


@dataclass(frozen=True)
class ComponentTarget:
    """Maps a Wolta component key to the parameter that primarily controls it.

    This mapping is domain/site knowledge supplied BY THE CALLER — this
    module hardcodes neither the mapping nor the direction, per the "never
    hardcode values" rule. ``positive_direction`` is only used to pick an
    initial direction the very first time this specific parameter is
    nudged; every subsequent cycle uses the gradient-free continue/reverse
    rule instead (see ``suggest_tuning``).
    """

    param: str
    positive_direction: Direction = 1


@dataclass(frozen=True)
class ParameterAdjustment:
    """One proposed (or applied) parameter change."""

    param: str
    old_value: float
    new_value: float
    direction: Direction
    clamped: bool


@dataclass(frozen=True)
class PreviousTuning:
    """State carried forward from prior cycles.

    ``rating`` is the last rating that was actually evaluated (used to
    compute the improve/worsen trend) — it is always a rating with a
    non-null score that was not stale at the time it was recorded.

    ``last_adjustment`` is the most recently PROPOSED adjustment (persists
    across intervening no_action/baseline cycles, so a single missing or
    stale reading does not erase direction memory).
    """

    rating: WoltaRating
    last_adjustment: ParameterAdjustment | None


@dataclass(frozen=True)
class TuningResult:
    """Outcome of one ``suggest_tuning`` call."""

    status: TuningStatus
    reason: str
    target_component: str | None
    component_gap: float | None
    adjustment: ParameterAdjustment | None
    auto_apply: bool
    applied: bool
    effective_parameters: dict[str, float]
    log_entry: dict[str, Any]


# ── Pure logic ───────────────────────────────────────────────────────────────


def _select_target(
    rating: WoltaRating,
    component_targets: Mapping[str, ComponentTarget],
    parameters: Mapping[str, TunableParameter],
) -> tuple[WoltaComponent, ComponentTarget] | None:
    """Return the (component, target) pair with the largest gap.

    Only components that have a mapped target AND whose mapped parameter
    actually exists in ``parameters`` are eligible. Ties are broken by
    the order components appear in ``rating.components`` (first wins).
    Returns ``None`` if there is no eligible candidate at all.
    """
    candidates = [
        (comp, component_targets[comp.key])
        for comp in rating.components
        if comp.key in component_targets and component_targets[comp.key].param in parameters
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0].gap)


def _component_gaps(rating: WoltaRating | None) -> dict[str, float]:
    if rating is None:
        return {}
    return {c.key: c.gap for c in rating.components}


def _build_result(
    *,
    status: TuningStatus,
    reason: str,
    now: datetime,
    rating: WoltaRating | None,
    parameters: Mapping[str, TunableParameter],
    target_component: str | None,
    component_gap: float | None,
    adjustment: ParameterAdjustment | None,
    auto_apply: bool,
) -> TuningResult:
    applied = auto_apply and adjustment is not None and status == "adjusted"
    effective_parameters = {name: p.value for name, p in parameters.items()}
    if applied and adjustment is not None:
        effective_parameters[adjustment.param] = adjustment.new_value

    log_entry: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "status": status,
        "reason": reason,
        "score": rating.score if rating is not None else None,
        "component_gaps": _component_gaps(rating),
        "target_component": target_component,
        "component_gap": component_gap,
        "param": adjustment.param if adjustment is not None else None,
        "old_value": adjustment.old_value if adjustment is not None else None,
        "new_value": adjustment.new_value if adjustment is not None else None,
        "direction": adjustment.direction if adjustment is not None else None,
        "clamped": adjustment.clamped if adjustment is not None else None,
        "auto_apply": auto_apply,
        "applied": applied,
    }
    _LOGGER.info("WoltaFeedbackTuner %s: %s", status, reason)

    return TuningResult(
        status=status,
        reason=reason,
        target_component=target_component,
        component_gap=component_gap,
        adjustment=adjustment,
        auto_apply=auto_apply,
        applied=applied,
        effective_parameters=effective_parameters,
        log_entry=log_entry,
    )


def suggest_tuning(
    rating: WoltaRating | None,
    previous: PreviousTuning | None,
    parameters: Mapping[str, TunableParameter],
    component_targets: Mapping[str, ComponentTarget],
    *,
    now: datetime | None = None,
    max_staleness: timedelta = _DEFAULT_MAX_STALENESS,
    auto_apply: bool = False,
) -> TuningResult:
    """Propose (never silently apply) the next parameter nudge.

    Parameters
    ----------
    rating:
        Latest Wolta rating (score + components), or ``None``/score
        ``None`` if the sensor is unavailable.
    previous:
        State from the previous tuning cycle (``None`` on the very first
        call ever made for this site/tuner instance).
    parameters:
        Current adjustable parameters, keyed by name, each carrying its
        own allowed ``[min, max]`` and ``step``. Never hardcoded here —
        entirely caller-supplied.
    component_targets:
        Maps a Wolta component key to the ``ComponentTarget`` (parameter
        name + baseline direction) that governs it. Caller-supplied
        domain knowledge; this function has no built-in opinion about
        which parameter helps which component.
    now:
        Clock override for testing; defaults to ``datetime.now(UTC)``.
    max_staleness:
        Ratings older than this are treated as missing (no_action).
    auto_apply:
        If ``True``, ``effective_parameters`` reflects the proposed new
        value and ``applied`` is ``True``. Defaults to ``False``
        (propose-only / shadow mode) — this function never has side
        effects regardless, ``auto_apply`` only changes what the return
        value reports as "effective" for the caller to act on.
    """
    now = now if now is not None else datetime.now(UTC)

    # ── Guard: missing / null rating ────────────────────────────────────────
    if rating is None or rating.score is None:
        return _build_result(
            status="no_action",
            reason="Wolta rating missing or score is null — no adjustment",
            now=now,
            rating=rating,
            parameters=parameters,
            target_component=None,
            component_gap=None,
            adjustment=None,
            auto_apply=auto_apply,
        )

    # ── Guard: staleness ─────────────────────────────────────────────────────
    age = now - rating.timestamp
    if age > max_staleness:
        return _build_result(
            status="no_action",
            reason=(f"Wolta rating stale ({age} old, max {max_staleness}) — no adjustment"),
            now=now,
            rating=rating,
            parameters=parameters,
            target_component=None,
            component_gap=None,
            adjustment=None,
            auto_apply=auto_apply,
        )

    target = _select_target(rating, component_targets, parameters)

    # ── Guard: first-ever measurement — baseline only ───────────────────────
    if previous is None:
        target_key = target[0].key if target is not None else None
        target_gap = target[0].gap if target is not None else None
        return _build_result(
            status="baseline",
            reason="First measurement — logging baseline only, no adjustment",
            now=now,
            rating=rating,
            parameters=parameters,
            target_component=target_key,
            component_gap=target_gap,
            adjustment=None,
            auto_apply=auto_apply,
        )

    # ── Guard: no controllable parameter mapped to any component ───────────
    if target is None:
        return _build_result(
            status="no_action",
            reason="No component with a mapped, known parameter to adjust",
            now=now,
            rating=rating,
            parameters=parameters,
            target_component=None,
            component_gap=None,
            adjustment=None,
            auto_apply=auto_apply,
        )

    target_comp, target_map = target

    # ── Guard: nothing left to capture on the top-gap component ────────────
    if target_comp.gap <= 0:
        return _build_result(
            status="no_action",
            reason=(
                f"'{target_comp.label}' (largest-gap component) is already fully "
                "captured — no adjustment needed"
            ),
            now=now,
            rating=rating,
            parameters=parameters,
            target_component=target_comp.key,
            component_gap=target_comp.gap,
            adjustment=None,
            auto_apply=auto_apply,
        )

    param_name = target_map.param
    tunable = parameters[param_name]
    prior_adjustment = previous.last_adjustment

    # ── Gradient-free direction selection ───────────────────────────────────
    if prior_adjustment is not None and prior_adjustment.param == param_name:
        score_delta = rating.score - previous.rating.score
        if score_delta >= 0:
            direction: Direction = prior_adjustment.direction
            trend_reason = (
                f"score improved/held (Δ{score_delta:+.2f} since last measurement) "
                "— continuing same direction"
            )
        else:
            direction = -1 if prior_adjustment.direction == 1 else 1
            trend_reason = (
                f"score worsened (Δ{score_delta:+.2f} since last measurement) — reversing direction"
            )
    else:
        direction = target_map.positive_direction
        trend_reason = (
            f"no prior adjustment for '{param_name}' — using configured baseline direction"
        )

    raw_new_value = tunable.value + direction * tunable.step
    new_value = min(max(raw_new_value, tunable.min), tunable.max)
    clamped = new_value != raw_new_value

    adjustment = ParameterAdjustment(
        param=param_name,
        old_value=tunable.value,
        new_value=new_value,
        direction=direction,
        clamped=clamped,
    )

    clamp_note = " (clamped at bound)" if clamped else ""
    reason = (
        f"largest gap: '{target_comp.label}' ({target_comp.gap:.2f} pts); {trend_reason}; "
        f"nudged {param_name}: {tunable.value:g} -> {new_value:g}{clamp_note}"
    )

    return _build_result(
        status="adjusted",
        reason=reason,
        now=now,
        rating=rating,
        parameters=parameters,
        target_component=target_comp.key,
        component_gap=target_comp.gap,
        adjustment=adjustment,
        auto_apply=auto_apply,
    )


# ── Stateful wrapper ─────────────────────────────────────────────────────────


class WoltaFeedbackTuner:
    """Stateful wrapper around ``suggest_tuning`` for repeated call sites.

    Holds:

    * the last evaluated rating + last actual adjustment, needed for the
      gradient-free continue/reverse rule across calls, and
    * a bounded, structured decision log for observability/audit — every
      call (adjusted OR no_action OR baseline) appends exactly one entry.

    ``auto_apply`` defaults to ``False`` here too. This class NEVER writes
    to hardware — applying a proposal to a real inverter/EMS is entirely
    the caller's responsibility. Flipping ``auto_apply=True`` on a
    production site must never happen without prior 901 QC sign-off for
    that specific site's safety bounds.
    """

    def __init__(
        self,
        *,
        max_staleness: timedelta = _DEFAULT_MAX_STALENESS,
        max_log_entries: int = WOLTA_TUNER_MAX_LOG_ENTRIES,
    ) -> None:
        self._max_staleness = max_staleness
        self._previous: PreviousTuning | None = None
        self._log: deque[dict[str, Any]] = deque(maxlen=max_log_entries)

    def tune(
        self,
        rating: WoltaRating | None,
        parameters: Mapping[str, TunableParameter],
        component_targets: Mapping[str, ComponentTarget],
        *,
        auto_apply: bool = False,
        now: datetime | None = None,
    ) -> TuningResult:
        """Evaluate one tuning cycle and advance internal trend state."""
        result = suggest_tuning(
            rating,
            self._previous,
            parameters,
            component_targets,
            now=now,
            max_staleness=self._max_staleness,
            auto_apply=auto_apply,
        )
        self._log.append(result.log_entry)
        self._advance_state(rating, result)
        return result

    def _advance_state(self, rating: WoltaRating | None, result: TuningResult) -> None:
        # A no_action cycle (missing/stale data) must not corrupt the trend
        # baseline — keep whatever state we already had.
        if result.status == "no_action" or rating is None:
            return
        next_adjustment = (
            result.adjustment
            if result.adjustment is not None
            else (self._previous.last_adjustment if self._previous is not None else None)
        )
        self._previous = PreviousTuning(rating=rating, last_adjustment=next_adjustment)

    def get_log(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the *n* most recent decision log entries, oldest first."""
        entries = list(self._log)
        return entries[-n:]

    def reset(self) -> None:
        """Clear all learned state (e.g. after a manual parameter override)."""
        self._previous = None
        self._log.clear()
