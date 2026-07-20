"""PLAT-1241 (EXP-07): Tests for optimizer/wolta_feedback_tuner.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.carmabox.optimizer.wolta_feedback_tuner import (
    ComponentTarget,
    ParameterAdjustment,
    PreviousTuning,
    TunableParameter,
    WoltaComponent,
    WoltaFeedbackTuner,
    WoltaRating,
    suggest_tuning,
)

_T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


# ── helpers ──────────────────────────────────────────────────────────────────


def _component(
    key: str, captured: float, possible: float, label: str | None = None
) -> WoltaComponent:
    return WoltaComponent(key=key, label=label or key, captured=captured, possible=possible)


def _rating(
    score: float | None = 70.0,
    components: tuple[WoltaComponent, ...] | None = None,
    timestamp: datetime | None = None,
) -> WoltaRating:
    if components is None:
        components = (
            _component("selfuse", 60.0, 80.0),
            _component("solar_export", 50.0, 90.0),
            _component("arbitrage", 30.0, 40.0),
        )
    # Default to "now" (rather than a fixed instant) so callers that don't
    # care about staleness never accidentally trip the staleness guard —
    # tests that DO care pass an explicit timestamp (+ explicit `now=`).
    resolved_timestamp = timestamp if timestamp is not None else datetime.now(UTC)
    return WoltaRating(score=score, components=components, timestamp=resolved_timestamp)


def _params() -> dict[str, TunableParameter]:
    return {
        "discharge_percentile": TunableParameter(value=85, min=50, max=95, step=2),
        "export_reserve_soc": TunableParameter(value=40, min=20, max=60, step=5),
        "arbitrage_threshold": TunableParameter(value=1.0, min=0.5, max=2.0, step=0.1),
    }


_TARGETS = {
    "selfuse": ComponentTarget(param="discharge_percentile", positive_direction=1),
    "solar_export": ComponentTarget(param="export_reserve_soc", positive_direction=1),
    "arbitrage": ComponentTarget(param="arbitrage_threshold", positive_direction=-1),
}


# ── WoltaComponent.gap ───────────────────────────────────────────────────────


class TestComponentGap:
    def test_gap_is_possible_minus_captured(self) -> None:
        c = _component("selfuse", 60.0, 80.0)
        assert c.gap == pytest.approx(20.0)

    def test_gap_floors_at_zero_when_captured_exceeds_possible(self) -> None:
        c = _component("selfuse", 90.0, 80.0)
        assert c.gap == 0.0


# ── TunableParameter validation ──────────────────────────────────────────────


class TestTunableParameterValidation:
    def test_raises_when_min_exceeds_max(self) -> None:
        with pytest.raises(ValueError, match="min"):
            TunableParameter(value=10, min=90, max=10, step=1)

    def test_raises_when_step_not_positive(self) -> None:
        with pytest.raises(ValueError, match="step"):
            TunableParameter(value=10, min=0, max=20, step=0)


# ── First run / baseline ─────────────────────────────────────────────────────


class TestFirstRunBaseline:
    def test_first_run_makes_no_adjustment(self) -> None:
        result = suggest_tuning(_rating(), None, _params(), _TARGETS)
        assert result.status == "baseline"
        assert result.adjustment is None

    def test_first_run_effective_parameters_unchanged(self) -> None:
        params = _params()
        result = suggest_tuning(_rating(), None, params, _TARGETS)
        assert result.effective_parameters == {k: p.value for k, p in params.items()}

    def test_first_run_still_reports_largest_gap_component_for_logging(self) -> None:
        result = suggest_tuning(_rating(), None, _params(), _TARGETS)
        # solar_export has the largest gap (40) among the default fixture
        assert result.target_component == "solar_export"


# ── Missing / null / stale data guards ───────────────────────────────────────


class TestNoActionGuards:
    def test_none_rating_is_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(None, previous, _params(), _TARGETS)
        assert result.status == "no_action"
        assert result.adjustment is None

    def test_null_score_is_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(score=None), previous, _params(), _TARGETS)
        assert result.status == "no_action"
        assert "null" in result.reason or "missing" in result.reason

    def test_stale_rating_is_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0, timestamp=_T0), last_adjustment=None)
        stale = _rating(score=70.0, timestamp=_T0 + timedelta(hours=2))
        result = suggest_tuning(
            stale,
            previous,
            _params(),
            _TARGETS,
            now=_T0 + timedelta(hours=4),
            max_staleness=timedelta(hours=1),
        )
        assert result.status == "no_action"
        assert "stale" in result.reason

    def test_fresh_rating_at_staleness_boundary_is_not_stale(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0, timestamp=_T0), last_adjustment=None)
        exactly_at_boundary = _rating(score=70.0, timestamp=_T0)
        result = suggest_tuning(
            exactly_at_boundary,
            previous,
            _params(),
            _TARGETS,
            now=_T0 + timedelta(hours=1),
            max_staleness=timedelta(hours=1),
        )
        assert result.status == "adjusted"

    def test_no_mapped_component_target_is_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(), previous, _params(), {})
        assert result.status == "no_action"

    def test_mapped_param_missing_from_parameters_is_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(), previous, {}, _TARGETS)
        assert result.status == "no_action"

    def test_fully_captured_top_component_is_no_action(self) -> None:
        components = (
            _component("selfuse", 80.0, 80.0),
            _component("solar_export", 90.0, 90.0),
            _component("arbitrage", 40.0, 40.0),
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(components=components), previous, _params(), _TARGETS)
        assert result.status == "no_action"
        assert result.adjustment is None


# ── Component selection with multiple gaps ───────────────────────────────────


class TestTargetSelection:
    def test_selects_component_with_largest_gap(self) -> None:
        components = (
            _component("selfuse", 78.0, 80.0),  # gap 2
            _component("solar_export", 50.0, 90.0),  # gap 40 (largest)
            _component("arbitrage", 35.0, 40.0),  # gap 5
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(components=components), previous, _params(), _TARGETS)
        assert result.target_component == "solar_export"
        assert result.adjustment is not None
        assert result.adjustment.param == "export_reserve_soc"

    def test_tie_breaks_to_first_component_in_order(self) -> None:
        components = (
            _component("selfuse", 60.0, 80.0),  # gap 20
            _component("solar_export", 70.0, 90.0),  # gap 20 (tie)
            _component("arbitrage", 30.0, 40.0),  # gap 10
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(components=components), previous, _params(), _TARGETS)
        assert result.target_component == "selfuse"

    def test_component_without_target_mapping_is_ignored(self) -> None:
        components = (
            _component("unmapped_component", 0.0, 100.0),  # huge gap, but no mapping
            _component("arbitrage", 35.0, 40.0),  # gap 5, mapped
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(components=components), previous, _params(), _TARGETS)
        assert result.target_component == "arbitrage"


# ── Trend direction: improve / unchanged / worsen ────────────────────────────


class TestTrendDirection:
    def test_improving_trend_continues_same_direction(self) -> None:
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=65.0), previous, _params(), _TARGETS)
        assert result.status == "adjusted"
        assert result.adjustment is not None
        assert result.adjustment.direction == 1
        assert result.adjustment.new_value == pytest.approx(45.0)  # 40 + step(5)

    def test_unchanged_score_continues_same_direction(self) -> None:
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=65.0), previous, _params(), _TARGETS)
        assert result.adjustment is not None
        assert result.adjustment.direction == 1

    def test_worsening_trend_reverses_direction(self) -> None:
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=60.0), previous, _params(), _TARGETS)
        assert result.status == "adjusted"
        assert result.adjustment is not None
        assert result.adjustment.direction == -1
        assert result.adjustment.new_value == pytest.approx(35.0)  # 40 - step(5)
        assert "revers" in result.reason.lower() or "backa" in result.reason.lower()

    def test_new_target_param_uses_baseline_direction_not_reversal_logic(self) -> None:
        # Previous cycle nudged discharge_percentile; this cycle the largest
        # gap has shifted to a DIFFERENT component/parameter. Even though the
        # score worsened, the new parameter has no prior direction of its
        # own, so it should use its configured baseline direction rather
        # than "reverse" (there's nothing to reverse for this param yet).
        prior_adj = ParameterAdjustment(
            param="discharge_percentile", old_value=83, new_value=85, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=prior_adj)
        components = (
            _component("selfuse", 78.0, 80.0),  # small gap now
            _component("arbitrage", 10.0, 40.0),  # largest gap now
        )
        result = suggest_tuning(
            _rating(score=60.0, components=components), previous, _params(), _TARGETS
        )
        assert result.adjustment is not None
        assert result.adjustment.param == "arbitrage_threshold"
        # arbitrage's configured positive_direction is -1
        assert result.adjustment.direction == -1


# ── Clamping ─────────────────────────────────────────────────────────────────


class TestClamping:
    def test_clamps_at_max_bound(self) -> None:
        params = _params()
        params["export_reserve_soc"] = TunableParameter(value=58, min=20, max=60, step=5)
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=53, new_value=58, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=65.0), previous, params, _TARGETS)
        assert result.adjustment is not None
        assert result.adjustment.new_value == 60.0
        assert result.adjustment.clamped is True

    def test_clamps_at_min_bound(self) -> None:
        params = _params()
        params["export_reserve_soc"] = TunableParameter(value=23, min=20, max=60, step=5)
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=28, new_value=23, direction=-1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        # Score worsened -> would reverse to +1, but let's test min clamp via
        # a continuing-negative-direction scenario instead (improving trend).
        result = suggest_tuning(_rating(score=65.0), previous, params, _TARGETS)
        assert result.adjustment is not None
        assert result.adjustment.new_value == 20.0
        assert result.adjustment.clamped is True

    def test_never_exceeds_bounds_even_when_clamped(self) -> None:
        params = _params()
        params["export_reserve_soc"] = TunableParameter(value=59, min=20, max=60, step=5)
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=54, new_value=59, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=70.0), previous, params, _TARGETS)
        assert result.adjustment is not None
        assert result.adjustment.new_value <= 60.0

    def test_adjustment_never_exceeds_step_size(self) -> None:
        params = _params()
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        result = suggest_tuning(_rating(score=70.0), previous, params, _TARGETS)
        assert result.adjustment is not None
        delta = abs(result.adjustment.new_value - result.adjustment.old_value)
        assert delta <= params["export_reserve_soc"].step + 1e-9


# ── auto_apply behaviour ──────────────────────────────────────────────────────


class TestAutoApply:
    def test_auto_apply_false_leaves_effective_parameters_unchanged(self) -> None:
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        params = _params()
        result = suggest_tuning(_rating(score=65.0), previous, params, _TARGETS, auto_apply=False)
        assert result.applied is False
        assert (
            result.effective_parameters["export_reserve_soc"] == params["export_reserve_soc"].value
        )

    def test_auto_apply_true_updates_effective_parameters(self) -> None:
        prior_adj = ParameterAdjustment(
            param="export_reserve_soc", old_value=35, new_value=40, direction=1, clamped=False
        )
        previous = PreviousTuning(rating=_rating(score=60.0), last_adjustment=prior_adj)
        params = _params()
        result = suggest_tuning(_rating(score=65.0), previous, params, _TARGETS, auto_apply=True)
        assert result.applied is True
        assert result.adjustment is not None
        assert result.effective_parameters["export_reserve_soc"] == result.adjustment.new_value

    def test_auto_apply_true_has_no_effect_when_no_action(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(score=None), previous, _params(), _TARGETS, auto_apply=True)
        assert result.applied is False
        assert result.status == "no_action"


# ── Structured logging / observability ───────────────────────────────────────


class TestLogEntry:
    def test_log_entry_contains_required_fields(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(score=70.0), previous, _params(), _TARGETS)
        for key in ("component_gap", "old_value", "new_value", "reason", "status", "timestamp"):
            assert key in result.log_entry

    def test_log_entry_records_no_action_reason(self) -> None:
        previous = PreviousTuning(rating=_rating(score=65.0), last_adjustment=None)
        result = suggest_tuning(_rating(score=None), previous, _params(), _TARGETS)
        assert result.log_entry["status"] == "no_action"
        assert result.log_entry["reason"]


# ── WoltaFeedbackTuner (stateful wrapper) ────────────────────────────────────


class TestWoltaFeedbackTuner:
    def test_first_call_is_baseline(self) -> None:
        tuner = WoltaFeedbackTuner()
        result = tuner.tune(_rating(score=60.0), _params(), _TARGETS)
        assert result.status == "baseline"

    def test_second_call_continues_improving_trend(self) -> None:
        tuner = WoltaFeedbackTuner()
        tuner.tune(_rating(score=60.0, timestamp=_T0), _params(), _TARGETS, now=_T0)
        result = tuner.tune(
            _rating(score=65.0, timestamp=_T0 + timedelta(minutes=15)),
            _params(),
            _TARGETS,
            now=_T0 + timedelta(minutes=15),
        )
        assert result.status == "adjusted"
        assert result.adjustment is not None

    def test_worsening_across_two_calls_reverses(self) -> None:
        tuner = WoltaFeedbackTuner()
        tuner.tune(_rating(score=60.0, timestamp=_T0), _params(), _TARGETS, now=_T0)
        first = tuner.tune(
            _rating(score=65.0, timestamp=_T0 + timedelta(minutes=15)),
            _params(),
            _TARGETS,
            now=_T0 + timedelta(minutes=15),
        )
        assert first.adjustment is not None
        first_direction = first.adjustment.direction
        second = tuner.tune(
            _rating(score=55.0, timestamp=_T0 + timedelta(minutes=30)),
            _params(),
            _TARGETS,
            now=_T0 + timedelta(minutes=30),
        )
        assert second.adjustment is not None
        assert second.adjustment.direction == -first_direction

    def test_no_action_cycle_preserves_direction_memory(self) -> None:
        tuner = WoltaFeedbackTuner()
        tuner.tune(_rating(score=60.0, timestamp=_T0), _params(), _TARGETS, now=_T0)
        first = tuner.tune(
            _rating(score=65.0, timestamp=_T0 + timedelta(minutes=15)),
            _params(),
            _TARGETS,
            now=_T0 + timedelta(minutes=15),
        )
        assert first.adjustment is not None
        # A missing reading in between must not reset the trend memory.
        no_action = tuner.tune(
            _rating(score=None), _params(), _TARGETS, now=_T0 + timedelta(minutes=20)
        )
        assert no_action.status == "no_action"
        third = tuner.tune(
            _rating(score=70.0, timestamp=_T0 + timedelta(minutes=30)),
            _params(),
            _TARGETS,
            now=_T0 + timedelta(minutes=30),
        )
        assert third.adjustment is not None
        assert third.adjustment.direction == first.adjustment.direction

    def test_get_log_accumulates_entries(self) -> None:
        tuner = WoltaFeedbackTuner()
        tuner.tune(_rating(score=60.0), _params(), _TARGETS)
        tuner.tune(_rating(score=65.0), _params(), _TARGETS)
        log = tuner.get_log()
        assert len(log) == 2

    def test_get_log_respects_maxlen(self) -> None:
        tuner = WoltaFeedbackTuner(max_log_entries=2)
        for i in range(5):
            tuner.tune(_rating(score=60.0 + i), _params(), _TARGETS)
        assert len(tuner.get_log(n=10)) == 2

    def test_reset_clears_state_so_next_call_is_baseline_again(self) -> None:
        tuner = WoltaFeedbackTuner()
        tuner.tune(_rating(score=60.0), _params(), _TARGETS)
        tuner.reset()
        result = tuner.tune(_rating(score=65.0), _params(), _TARGETS)
        assert result.status == "baseline"
