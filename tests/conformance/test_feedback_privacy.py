"""What the aggregate surface may serve, and what must not be constructible.

These aggregates are computed over people's reports, so the interesting assertions
are the negative ones. A floor that is enforced in one of two modules, a total
published beside a suppressed cell, or one per-actor endpoint added later, each
defeats the whole design while every positive test keeps passing.

So this gate is mostly about absence, and absence has to be checked structurally.
A test that exercised the paths somebody wrote would say nothing about the path
somebody adds next month, which is exactly when a leaderboard appears. The route
table and the response models are therefore read directly, and the floor rules are
tested at the one place both surfaces import them from.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.api.routers import learning_reads as router_module
from contextplane.service.memory import learning_reads
from contextplane.signals import reads as feedback_reads

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_WINDOW = (_NOW - datetime.timedelta(days=30), _NOW)

#: Words that name a person-level or ranking view. Any of these appearing in a
#: path on this surface is the forbidden shape arriving under a new name.
_FORBIDDEN_PATH_WORDS = (
    "actor",
    "reporter",
    "user",
    "person",
    "member",
    "leaderboard",
    "ranking",
    "rank",
    "top",
    "worst",
    "best",
    "team",
    "individual",
    "performance",
)


def _routes() -> list[str]:
    return [route.path for route in router_module.router.routes]  # type: ignore[attr-defined]


def _cell(label: str, *, actors: int, events: int, floors: learning_reads.Floors) -> learning_reads.Cell:
    return learning_reads.Cell.measured(label, actor_count=actors, event_count=events, value=events, floors=floors)


# --- The floors themselves ------------------------------------------------------


def test_the_approved_floors_are_five_actors_and_five_events() -> None:
    """Pinned as values, because these are the numbers that were approved. A
    change to either is a policy change and has to be argued, not merged."""
    assert learning_reads.MIN_COHORT_ACTORS == 5
    assert learning_reads.MIN_CELL_EVENTS == 5

    default = learning_reads.Floors()
    assert (default.min_actors, default.min_events) == (5, 5)


def test_a_stricter_floor_is_accepted_and_a_looser_one_is_refused() -> None:
    """Stricter is a deployment's business; looser is not available at any layer.
    Refused rather than clamped, so a deployment that asked for three does not go
    on believing it configured three."""
    stricter = learning_reads.Floors(min_actors=10, min_events=25)
    assert (stricter.min_actors, stricter.min_events) == (10, 25)

    with pytest.raises(learning_reads.FloorsTooLoose, match="below the approved minimum"):
        learning_reads.Floors(min_actors=4)
    with pytest.raises(learning_reads.FloorsTooLoose, match="below the approved minimum"):
        learning_reads.Floors(min_events=4)


def test_both_aggregate_surfaces_enforce_the_same_floors_from_one_definition() -> None:
    """Not "the two agree": the two are the same object. Two definitions that agree
    today is the shape that drifts, and a drifted floor is a leak in whichever
    surface kept the looser one."""
    assert feedback_reads.Floors is learning_reads.Floors
    assert feedback_reads.build_breakdown is learning_reads.build_breakdown
    assert feedback_reads.Cell is learning_reads.Cell


def test_a_cell_below_either_floor_carries_no_value() -> None:
    """Both floors, independently. A cell with hundreds of events from two people
    is as identifying as one with two events."""
    floors = learning_reads.Floors()

    assert _cell("ok", actors=5, events=5, floors=floors).value == 5
    assert _cell("thin_actors", actors=4, events=500, floors=floors).value is None
    assert _cell("thin_events", actors=500, events=4, floors=floors).value is None
    assert _cell("thin_actors", actors=4, events=500, floors=floors).suppressed is True


# --- Suppression, combination, and the subtraction attack -----------------------


def test_a_total_beside_a_suppressed_cell_is_recomputed_and_labelled_partial() -> None:
    """The rule a floor alone does not give you. If the true total were served
    beside the survivors, the withheld cell is the difference and the floor bought
    nothing."""
    floors = learning_reads.Floors()
    cells = [
        _cell("reported_a", actors=10, events=40, floors=floors),
        _cell("reported_b", actors=10, events=30, floors=floors),
        # Thin on both counts, and thin enough that the remainder stays thin.
        _cell("thin", actors=2, events=3, floors=floors),
    ]
    breakdown = learning_reads.build_breakdown(
        "context_quality", window_start=_WINDOW[0], window_end=_WINDOW[1], cells=cells, floors=floors
    )

    # The remainder could not clear the floors, so nothing is served at all --
    # not the survivors, not the shape.
    assert breakdown.withheld is True
    assert breakdown.cells == ()
    assert breakdown.total is None


def test_a_remainder_that_clears_the_floors_is_combined_rather_than_dropped() -> None:
    """The "other" bucket exists so a distribution with thin tails is still
    reportable — but only when the bucket itself clears both floors."""
    floors = learning_reads.Floors()
    cells = [
        _cell("reported", actors=20, events=100, floors=floors),
        _cell("thin_one", actors=3, events=4, floors=floors),
        _cell("thin_two", actors=4, events=4, floors=floors),
    ]
    breakdown = learning_reads.build_breakdown(
        "reuse", window_start=_WINDOW[0], window_end=_WINDOW[1], cells=cells, floors=floors
    )

    assert breakdown.withheld is False
    labels = [cell.label for cell in breakdown.cells]
    assert labels == ["reported", learning_reads.BUCKET_OTHER]
    # 7 actors and 8 events across the two thin cells clears both floors.
    other = breakdown.cells[-1]
    assert (other.value, other.suppressed) == (8, False)
    # Partial, because cells were suppressed before being combined; and the total
    # is over what is reported, which is the combined figure.
    assert (breakdown.partial, breakdown.total) == (True, 108)


def test_a_whole_breakdown_with_nothing_reportable_is_withheld_not_zeroed() -> None:
    """Serving zeros would assert that nothing happened, when what happened is
    that everything was below a floor."""
    floors = learning_reads.Floors()
    breakdown = learning_reads.build_breakdown(
        "adequacy", window_start=_WINDOW[0], window_end=_WINDOW[1], cells=[], floors=floors
    )
    assert (breakdown.withheld, breakdown.total, breakdown.cells) == (True, None, ())


def test_an_unsuppressed_breakdown_is_not_labelled_partial() -> None:
    """The label has to mean something: if every cell is reported, `partial` is
    false and the total is the real one."""
    floors = learning_reads.Floors()
    cells = [
        _cell("a", actors=10, events=10, floors=floors),
        _cell("b", actors=10, events=20, floors=floors),
    ]
    breakdown = learning_reads.build_breakdown(
        "handoff_success", window_start=_WINDOW[0], window_end=_WINDOW[1], cells=cells, floors=floors
    )
    assert (breakdown.partial, breakdown.withheld, breakdown.total) == (False, False, 30)


# --- No individual or team view exists -----------------------------------------


def test_no_route_on_this_surface_names_an_actor_or_a_ranking() -> None:
    """Structural, over the whole route table. A per-actor path is the forbidden
    use arriving as a feature, and this catches it whatever it is called."""
    offenders = {
        path: [word for word in _FORBIDDEN_PATH_WORDS if word in path.lower()]
        for path in _routes()
        if any(word in path.lower() for word in _FORBIDDEN_PATH_WORDS)
    }
    assert not offenders, (
        f"aggregate routes naming a person-level or ranking view: {offenders}. "
        "Individual surveillance and team-performance evaluation are forbidden uses, "
        "so such a path is not a feature awaiting a floor -- it must not exist."
    )


def test_no_route_takes_an_actor_or_cohort_parameter() -> None:
    """A single aggregate path with an actor query parameter is a per-actor view
    with extra steps, and it would pass the path-name check above."""
    for route in router_module.router.routes:
        path = getattr(route, "path", "")
        assert "{" not in path, (
            f"{path} takes a path parameter. Every aggregate here covers the whole "
            "tenant; a parameter is how a caller narrows to a group small enough to "
            "identify, one request at a time."
        )


def test_the_response_model_cannot_carry_a_count_behind_a_suppressed_cell() -> None:
    """The service keeps the counts so a recompute can re-test the floors. The
    response model has nowhere to put them, and that asymmetry is deliberate: an
    actor count of two is the disclosure the floor prevents."""
    fields = set(router_module.CellOut.model_fields)
    assert fields == {"label", "value", "suppressed"}
    assert "actor_count" not in fields
    assert "event_count" not in fields

    # `extra="forbid"` so a field cannot be smuggled in by a caller-supplied key.
    assert router_module.CellOut.model_config["extra"] == "forbid"
    assert router_module.BreakdownOut.model_config["extra"] == "forbid"


def test_the_only_cohort_is_the_tenant() -> None:
    """No membership model exists to group by, and building one would be the
    team-performance surface. So the cohort is a constant, and it is asserted here
    rather than left to whatever a future query passes."""
    assert learning_reads.COHORT_TENANT == "tenant"
    floors = learning_reads.Floors()
    breakdown = learning_reads.build_breakdown(
        "reuse",
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        cells=[_cell("a", actors=10, events=10, floors=floors)],
        floors=floors,
    )
    assert breakdown.cohort_key == learning_reads.COHORT_TENANT


# --- What every response must carry --------------------------------------------


def test_every_response_carries_its_window_denominator_classification_and_floors() -> None:
    """Required fields, not annotations. A rate without its denominator is read as
    a count; a partial total without its label is read as the truth; a suppressed
    cell without the floors looks like missing data rather than a rule."""
    required = {"metric", "cohort_key", "window_start", "window_end", "classification", "floors", "cells"}
    fields = set(router_module.BreakdownOut.model_fields)
    assert required <= fields
    assert {"total", "denominator", "partial", "withheld"} <= fields

    for name in required:
        assert router_module.BreakdownOut.model_fields[name].is_required(), (
            f"{name} is optional; a response that can omit it eventually will, and the "
            "figure is then unreadable rather than merely unlabelled"
        )


def test_the_denominator_is_the_total_the_reader_was_given() -> None:
    """Not the true population. Naming it separately is for legibility, and if it
    diverged from the served total it would reintroduce the subtraction channel."""
    floors = learning_reads.Floors()
    breakdown = learning_reads.build_breakdown(
        "context_quality",
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        cells=[_cell("a", actors=10, events=10, floors=floors)],
        floors=floors,
    )
    assert breakdown.denominator == breakdown.total


def test_the_served_metric_set_is_closed_and_matches_what_is_computed() -> None:
    """A metric advertised but not computed is a broken endpoint; one computed but
    unreachable is dead code the next reader trusts."""
    assert set(router_module.SERVED_METRICS) == set(feedback_reads.FEEDBACK_METRICS) | set(
        learning_reads.LEARNING_METRICS
    )
    assert len(router_module.SERVED_METRICS) == len(set(router_module.SERVED_METRICS))
    assert router_module.AGGREGATE_CLASSIFICATION == "internal"


def test_the_window_is_bounded() -> None:
    """An unbounded window clears the floors by accumulating years of reports,
    which inverts what the floors are for."""
    assert router_module.MAX_WINDOW_DAYS == 400
    assert router_module.DEFAULT_WINDOW_DAYS <= router_module.MAX_WINDOW_DAYS

    start, end = router_module._window(10_000, _NOW)
    assert (end - start).days == router_module.MAX_WINDOW_DAYS
    start, end = router_module._window(0, _NOW)
    assert (end - start).days == 1


# --- Diagnostics are not quality signals ---------------------------------------


def test_diagnostic_observations_are_excluded_by_every_feedback_statement() -> None:
    """A diagnostic reports on the plumbing and cites no served content, so
    counting it as a quality verdict would let a burst of plumbing reports read as
    a collapse in context quality. Excluded in the statement, not by a caller
    remembering to ask."""
    assert "kind <> :diagnostic_kind" in feedback_reads._RATING_BREAKDOWN_SQL


def test_the_reporter_id_is_only_ever_counted_distinctly() -> None:
    """The floor is tested against how many independent people reported, so the
    column has to be read. This asserts it is read for nothing else: no grouping by
    it, no selecting it, no returning it."""
    statement = feedback_reads._RATING_BREAKDOWN_SQL
    occurrences = statement.count("reporter_id")
    assert occurrences == 1, f"reporter_id appears {occurrences} times; it may only appear inside count(DISTINCT ...)"
    assert "count(DISTINCT reporter_id)" in statement
    assert "GROUP BY reporter_id" not in statement


def test_every_feedback_statement_scopes_to_one_tenant() -> None:
    """No cross-tenant aggregate exists in this surface, and the way to keep that
    true is that the only statement cannot express one."""
    assert "tenant_id = :tenant" in feedback_reads._RATING_BREAKDOWN_SQL
    for statement in (
        learning_reads._CLAIM_AGING_SQL,
        learning_reads._CONTRADICTION_BACKLOG_SQL,
        learning_reads._PROMOTION_YIELD_SQL,
    ):
        assert "tenant_id = :tenant" in statement


def test_the_backlog_counts_only_unresolved_cases() -> None:
    """A resolved case is throughput, not backlog. Counting it would make a
    cleared queue look like a growing one."""
    assert "status IN ('open', 'routed')" in learning_reads._CONTRADICTION_BACKLOG_SQL
    assert "resolved" not in learning_reads._CONTRADICTION_BACKLOG_SQL


def test_age_buckets_are_coarse() -> None:
    """A per-day aging curve over a small population is a timeline of one person's
    activity. Four buckets is the resolution the question actually needs."""
    labels = [label for label, _, _ in learning_reads._AGE_BUCKETS]
    assert labels == ["0-6d", "7-29d", "30-89d", "90d+"]
