"""What this aggregate surface serves, and what it still does not.

**Most of this file used to be about floors, and eight of its tests went with
them.** The floor decision removed `MIN_COHORT_ACTORS`, `MIN_CELL_EVENTS`, suppression,
remainder-combination and partial totals, uniformly and for every actor kind.
Every assertion about those is gone rather than softened.

What remains is worth keeping and is a smaller claim than it was. These are
still structural absence checks -- no route names an actor, none takes a cohort
parameter, the response model carries no contributor counts, the cohort is the
tenant -- and absence still has to be checked structurally, because a test that
exercised the paths somebody wrote would say nothing about the path somebody adds
next month, which is exactly when a leaderboard appears.

**But they are now properties of these three routes, not a policy the system
enforces.** The module they guard used to make a per-actor cell unconstructible;
it does not any more. So a failure here means somebody widened *this* surface,
not that they breached a rule -- and nothing outside this file prevents a new
surface from doing what these routes do not.
"""

from __future__ import annotations

import datetime

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


def _cell(label: str, *, actors: int, events: int) -> learning_reads.Cell:
    return learning_reads.Cell.measured(label, actor_count=actors, event_count=events, value=events)


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


def test_the_response_model_carries_no_contributor_counts() -> None:
    """The service keeps the counts; the response model has nowhere to put them.

    That asymmetry outlives the floors it was built for. `Cell` keeps
    `actor_count` and `event_count` because a reader of the service layer may
    want to know how much a figure rests on -- but an actor count of two, served
    to an API caller, is a disclosure whether or not a floor would have hidden
    the figure beside it. Widening `CellOut` is a decision somebody should have
    to make deliberately, so this fails if it happens by tidy-up.

    `suppressed` is gone from the model with the mechanism it reported.
    """
    fields = set(router_module.CellOut.model_fields)
    assert fields == {"label", "value"}
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
    breakdown = learning_reads.build_breakdown(
        "reuse",
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        cells=[_cell("a", actors=10, events=10)],
    )
    assert breakdown.cohort_key == learning_reads.COHORT_TENANT


# --- What every response must carry --------------------------------------------


def test_every_response_carries_its_window_denominator_and_classification() -> None:
    """Required fields, not annotations. A rate without its denominator is read as
    a count.

    `floors`, `partial` and `withheld` were required here too, and are gone with
    the mechanism they described. Asserted as *absent* rather than simply
    dropped, because a response that still advertised floors while enforcing
    none would be the worst of the three states.
    """
    required = {"metric", "cohort_key", "window_start", "window_end", "classification", "cells"}
    fields = set(router_module.BreakdownOut.model_fields)
    assert required <= fields
    assert {"total", "denominator"} <= fields
    assert not (
        {"floors", "partial", "withheld"} & fields
    ), "the response still advertises a suppression mechanism this surface no longer has"

    for name in required:
        assert router_module.BreakdownOut.model_fields[name].is_required(), (
            f"{name} is optional; a response that can omit it eventually will, and the "
            "figure is then unreadable rather than merely unlabelled"
        )


def test_the_denominator_is_the_total_the_reader_was_given() -> None:
    """Naming it separately is for legibility, and the two must not diverge.

    That mattered more when the total was recomputed over surviving cells: a
    denominator that reported the *true* population beside a suppressed cell was
    the subtraction channel. With nothing suppressed the total is the true
    population, so this now pins the weaker property that the two names mean the
    same number -- still worth having, because a reader who sees both assumes
    they can differ.
    """
    breakdown = learning_reads.build_breakdown(
        "context_quality",
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        cells=[_cell("a", actors=10, events=10)],
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
