"""The aggregate writer: what it publishes, what it withholds, and what it re-examines.

The floors are the easy half and the tests for them are here mostly so a later
change cannot quietly loosen them. The half worth reading is everything about the
recompute: which windows the writer touches, which it refuses to touch, and what
it hands the upsert when a figure it once published is no longer the figure the
data supports.

What is *not* asserted here is the differencing defence itself. That rule lives
inside a single `ON CONFLICT DO UPDATE`, evaluated by Postgres against the row it
is overwriting, and a fake session that returned whatever it was told would prove
nothing about it. The integration file next door pins it against a real database.
What these tests pin is the Python side that statement depends on: the values
offered to it, and the fact that no other statement writes this table.

A fake session keyed on the leading verb, matching the sweeps' own unit tests.
"""

from __future__ import annotations

import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from contextplane.retention import policies
from contextplane.service.memory.learning_reads import BUCKET_OTHER, COHORT_TENANT, Floors
from contextplane.signals import aggregates
from contextplane.signals.feedback import KIND_DIAGNOSTIC

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: The most recent complete window at `_NOW`: yesterday, whole.
_YESTERDAY_START = datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC)
_YESTERDAY_END = datetime.datetime(2026, 8, 9, tzinfo=datetime.UTC)


def _mix(labels: dict[str, tuple[int, int]], *, actors: int) -> list[Any]:
    """One grouping-set answer: a row per label, then the window's own total row.

    `labels` maps a label to its (event_count, actor_count). `actors` is the
    window's distinct-actor count, which is deliberately not the sum of the
    per-label counts — one reporter appears under every label they used.
    """
    rows = [
        SimpleNamespace(label=label, event_count=events, actor_count=actor_count, is_total=0)
        for label, (events, actor_count) in labels.items()
    ]
    total_events = sum(events for events, _ in labels.values())
    rows.append(SimpleNamespace(label=None, event_count=total_events, actor_count=actors, is_total=1))
    return rows


class _AsyncCM:
    """The `async with session_factory() as session` shape, and nothing more."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Answers each of the writer's five statements from declared state."""

    def __init__(
        self,
        *,
        tenants: list[uuid.UUID] | None = None,
        feedback: list[Any] | None = None,
        signals: list[Any] | None = None,
        watermark: datetime.datetime | None = None,
        suspect: list[Any] | None = None,
        stored_suppressed: bool | None = None,
    ) -> None:
        self._tenants = tenants if tenants is not None else [_TENANT]
        self._feedback = feedback if feedback is not None else _mix({}, actors=0)
        self._signals = signals if signals is not None else _mix({}, actors=0)
        self._watermark = watermark
        self._suspect = suspect or []
        # What the upsert's RETURNING gives back. None means "whatever was
        # offered", which is the no-conflict case.
        self._stored_suppressed = stored_suppressed
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))

        if sql.startswith("SELECT tenant_id FROM tenants"):
            return SimpleNamespace(all=lambda: [(tenant,) for tenant in self._tenants])
        if sql.startswith("SELECT max(effective_at)"):
            return SimpleNamespace(scalar=lambda: self._watermark)
        if sql.startswith("SELECT metric, window_start, window_end"):
            return SimpleNamespace(all=lambda: list(self._suspect))
        if sql.startswith("SELECT rating AS label"):
            return SimpleNamespace(all=lambda: list(self._feedback))
        if sql.startswith("SELECT source_system AS label"):
            return SimpleNamespace(all=lambda: list(self._signals))
        if sql.startswith("INSERT INTO privacy_aggregates"):
            offered = bool((params or {}).get("suppressed"))
            stored = offered if self._stored_suppressed is None else self._stored_suppressed
            return SimpleNamespace(scalar=lambda: stored)
        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1

    def writes(self, metric: str | None = None) -> list[dict[str, Any]]:
        return [
            params
            for sql, params in self.executed
            if sql.startswith("INSERT INTO privacy_aggregates") and (metric is None or params["metric"] == metric)
        ]

    def statements(self, prefix: str) -> list[dict[str, Any]]:
        return [params for sql, params in self.executed if sql.startswith(prefix)]


class _FixedClock:
    def now(self) -> datetime.datetime:
        return _NOW


def _writer(session: _FakeSession, **kwargs: Any) -> aggregates.PrivacyAggregateWriter:
    kwargs.setdefault("trailing_windows", 1)
    return aggregates.PrivacyAggregateWriter(lambda: _AsyncCM(session), clock=_FixedClock(), **kwargs)


def _yesterdays(writes: list[dict[str, Any]]) -> dict[str, Any]:
    """The one write for yesterday's window, which is what a one-window pass makes."""
    matching = [w for w in writes if w["window_start"] == _YESTERDAY_START]
    assert len(matching) == 1
    return matching[0]


# ---------------------------------------------------------------------------
# What gets published
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_that_clears_the_floors_is_published_with_its_breakdown() -> None:
    """The ordinary case, and the shape everything else is a refusal of."""
    session = _FakeSession(feedback=_mix({"selected": (9, 7), "ignored": (6, 5)}, actors=8))

    report = await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert write["suppressed"] is False
    assert json.loads(write["value"]) == {"cells": {"selected": 9, "ignored": 6}, "total": 15}
    assert write["actor_count"] == 8
    assert report.written >= 1


@pytest.mark.asyncio
async def test_the_actor_count_is_the_windows_own_and_not_the_sum_of_its_labels() -> None:
    """Summing per-label distinct counts counts one reporter once per label they used.

    That inflated number would then be the one the actor floor is tested against, so
    a cell three people wrote under four labels would read as twelve contributors and
    clear a floor it never reached.
    """
    session = _FakeSession(feedback=_mix({"selected": (9, 4), "ignored": (6, 4)}, actors=5))

    await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    # 4 + 4 would be 8. The window's own distinct count is 5.
    assert write["actor_count"] == 5


@pytest.mark.asyncio
async def test_a_thin_label_is_folded_into_a_remainder_and_the_total_says_it_is_partial() -> None:
    """Suppression alone discloses the thin cell when a true total is published beside it.

    So the withheld label is combined into a remainder that clears the floors itself,
    and the total is recomputed over what is actually shown.
    """
    session = _FakeSession(feedback=_mix({"selected": (20, 9), "ignored": (3, 2), "stale": (4, 4)}, actors=11))

    await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    stored = json.loads(write["value"])
    assert stored["cells"] == {"selected": 20, BUCKET_OTHER: 7}
    assert stored["total"] == 27
    assert write["partial"] is True


@pytest.mark.asyncio
async def test_both_metrics_are_written_for_every_window_in_the_pass() -> None:
    """A metric computed by one pass and forgotten by the next is a hole in the series."""
    session = _FakeSession(
        feedback=_mix({"selected": (9, 7)}, actors=7),
        signals=_mix({"github": (11, 6)}, actors=6),
    )

    await _writer(session, trailing_windows=3).run_once()

    windows = {(w["metric"], w["window_start"]) for w in session.writes()}
    assert len(windows) == 3 * len(aggregates.AGGREGATE_METRICS)
    assert {metric for metric, _ in windows} == set(aggregates.AGGREGATE_METRICS)


@pytest.mark.asyncio
async def test_only_complete_windows_are_computed() -> None:
    """The window still accepting rows must never be published.

    It would be published, legitimately recomputed to a larger figure on the next
    tick, and the upsert would read that growth as a removal — turning the defence
    into a machine that withholds every cell it has ever written.
    """
    session = _FakeSession(feedback=_mix({"selected": (9, 7)}, actors=7))

    await _writer(session, trailing_windows=5).run_once()

    latest_end = max(w["window_end"] for w in session.writes())
    assert latest_end == _YESTERDAY_END
    assert all(w["window_end"] <= _YESTERDAY_END for w in session.writes())


@pytest.mark.asyncio
async def test_a_cell_inherits_the_retention_of_the_class_it_was_built_from() -> None:
    """An aggregate is a derivative: it may not outlive the records it summarizes.

    Anchored at the window's end rather than at the computation, so recomputing an
    old window cannot extend the life of an aggregate over old records.
    """
    session = _FakeSession(
        feedback=_mix({"selected": (9, 7)}, actors=7),
        signals=_mix({"github": (11, 6)}, actors=6),
    )

    await _writer(session).run_once()

    feedback = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    signals = _yesterdays(session.writes(aggregates.METRIC_SIGNAL_SOURCE_MIX))
    assert feedback["expires_at"] == policies.expiry_deadline(policies.RECORD_CONTEXT_FEEDBACK, _YESTERDAY_END)
    assert signals["expires_at"] == policies.expiry_deadline(policies.RECORD_EXTERNAL_SIGNAL, _YESTERDAY_END)


@pytest.mark.asyncio
async def test_every_cell_is_tenant_cohorted_and_stamped_with_the_policy_version() -> None:
    """There is no cohort finer than the tenant, and a stored figure names its rule."""
    session = _FakeSession(feedback=_mix({"selected": (9, 7)}, actors=7))

    await _writer(session).run_once()

    assert {w["cohort"] for w in session.writes()} == {COHORT_TENANT}
    assert {w["policy_version"] for w in session.writes()} == {policies.POLICY_VERSION}


@pytest.mark.asyncio
async def test_diagnostics_are_excluded_from_the_feedback_mix() -> None:
    """A report about the system's own plumbing is not a verdict on served context.

    Excluded in the statement rather than filtered afterwards, so there is no
    aggregate whose exclusion depends on a later caller remembering to ask.
    """
    session = _FakeSession(feedback=_mix({"selected": (9, 7)}, actors=7))

    await _writer(session).run_once()

    asked = session.statements("SELECT rating AS label")
    assert asked
    assert all(params["diagnostic_kind"] == KIND_DIAGNOSTIC for params in asked)


# ---------------------------------------------------------------------------
# What gets withheld
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_below_the_actor_floor_carries_no_figures_at_all() -> None:
    """Not just no value: no actor count either.

    A cell that withheld its value while reporting that it covers four actors has
    disclosed a figure two versions of the cell can be subtracted from, which is the
    disclosure the withholding was for.
    """
    session = _FakeSession(feedback=_mix({"selected": (20, 4)}, actors=4))

    report = await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert write["suppressed"] is True
    assert write["value"] is None
    assert write["actor_count"] == 0
    assert write["partial"] is False
    assert report.withheld >= 1


@pytest.mark.asyncio
async def test_a_breakdown_whose_remainder_cannot_clear_the_floors_is_withheld_entire() -> None:
    """Survivors plus a known population is the subtraction attack in a different shape.

    So when the combined remainder is itself too thin, not even the shape of the
    distribution is stored.
    """
    session = _FakeSession(feedback=_mix({"selected": (30, 12), "ignored": (2, 1)}, actors=13))

    await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert write["suppressed"] is True
    assert write["value"] is None


@pytest.mark.asyncio
async def test_an_empty_window_is_stored_as_a_withheld_cell_rather_than_skipped() -> None:
    """ "Computed, nothing to report" and "never computed" are different facts.

    Without the row they are indistinguishable, and a window whose every row was
    erased would read as one the writer simply had not reached.
    """
    session = _FakeSession()

    await _writer(session).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert write["suppressed"] is True
    assert write["value"] is None


@pytest.mark.asyncio
async def test_the_database_can_withhold_a_cell_this_side_considers_reportable() -> None:
    """The floors here cannot see the figure already published; the statement can.

    So the outcome the report counts is what the upsert returned, not what the
    computation offered — otherwise a cell withheld by the differencing defence
    would be logged as published.
    """
    session = _FakeSession(feedback=_mix({"selected": (9, 7)}, actors=7), stored_suppressed=True)

    report = await _writer(session).run_once()

    offered = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert offered["suppressed"] is False
    assert report.written == 0
    assert report.withheld == len(aggregates.AGGREGATE_METRICS)


@pytest.mark.asyncio
async def test_a_floor_below_the_approved_minimum_is_refused_at_construction() -> None:
    """A deployment that configured three actors and got five would keep believing it had three."""
    with pytest.raises(Exception, match="below the approved minimum"):
        aggregates.PrivacyAggregateWriter(lambda: _AsyncCM(_FakeSession()), floors=Floors(min_actors=3))


@pytest.mark.asyncio
async def test_a_stricter_floor_is_honoured() -> None:
    """Code may be stricter than the schema CHECK; the CHECK is the looser bound."""
    session = _FakeSession(feedback=_mix({"selected": (20, 7)}, actors=7))

    await _writer(session, floors=Floors(min_actors=9, min_events=5)).run_once()

    write = _yesterdays(session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX))
    assert write["suppressed"] is True


# ---------------------------------------------------------------------------
# What an erasure makes the writer re-examine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tenant_with_no_tombstone_re_examines_nothing() -> None:
    """The recompute is erasure-triggered. With no erasure there is nothing to retract."""
    session = _FakeSession(feedback=_mix({"selected": (9, 7)}, actors=7))

    report = await _writer(session).run_once()

    assert report.recomputed == 0
    assert session.statements("SELECT metric, window_start, window_end") == []


@pytest.mark.asyncio
async def test_cells_computed_before_the_newest_erasure_are_recomputed() -> None:
    """The trigger is coarse because it has to be.

    A tombstone says a record was erased and when, never which window it sat in —
    the row is gone, so nothing can be joined back to it. Every cell computed before
    the newest erasure is therefore suspect, and the comparison inside the upsert is
    what decides which of them actually moved.
    """
    erased_at = datetime.datetime(2026, 8, 9, 6, 0, tzinfo=datetime.UTC)
    old_start = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    session = _FakeSession(
        feedback=_mix({"selected": (9, 7)}, actors=7),
        watermark=erased_at,
        suspect=[
            SimpleNamespace(
                metric=aggregates.METRIC_FEEDBACK_RATING_MIX,
                window_start=old_start,
                window_end=old_start + aggregates.WINDOW,
            )
        ],
    )

    report = await _writer(session).run_once()

    assert report.recomputed == 1
    asked = session.statements("SELECT metric, window_start, window_end")[0]
    assert asked["watermark"] == erased_at
    assert asked["cohort"] == COHORT_TENANT
    assert old_start in {w["window_start"] for w in session.writes()}


@pytest.mark.asyncio
async def test_only_erasures_of_the_classes_these_cells_are_built_from_trigger_a_recompute() -> None:
    """A tombstone for an unrelated class is real and irrelevant here.

    Treating one as a trigger would recompute every window on every erasure anywhere
    in the system, which is how a bounded pass becomes an unbounded one.
    """
    session = _FakeSession(watermark=_NOW)

    await _writer(session).run_once()

    asked = session.statements("SELECT max(effective_at)")[0]
    assert set(asked["classes"]) == {policies.RECORD_CONTEXT_FEEDBACK, policies.RECORD_EXTERNAL_SIGNAL}


@pytest.mark.asyncio
async def test_a_suspect_cell_that_is_also_a_trailing_window_is_computed_once() -> None:
    """Harmless twice, but a second write of the same cell reads as a second version."""
    session = _FakeSession(
        feedback=_mix({"selected": (9, 7)}, actors=7),
        watermark=_NOW,
        suspect=[
            SimpleNamespace(
                metric=aggregates.METRIC_FEEDBACK_RATING_MIX,
                window_start=_YESTERDAY_START,
                window_end=_YESTERDAY_END,
            )
        ],
    )

    await _writer(session).run_once()

    for_that_cell = [
        w for w in session.writes(aggregates.METRIC_FEEDBACK_RATING_MIX) if w["window_start"] == _YESTERDAY_START
    ]
    assert len(for_that_cell) == 1


@pytest.mark.asyncio
async def test_a_recompute_backlog_past_the_batch_ceiling_is_said_out_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tenant whose backlog exceeds one batch still has cells carrying an invalidated figure.

    Left to catch up silently, the series would look current while publishing a
    number an erasure had already retracted.
    """
    old_start = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    suspect = [
        SimpleNamespace(
            metric=aggregates.METRIC_FEEDBACK_RATING_MIX,
            window_start=old_start + aggregates.WINDOW * index,
            window_end=old_start + aggregates.WINDOW * (index + 1),
        )
        for index in range(2)
    ]
    session = _FakeSession(watermark=_NOW, suspect=suspect)

    with caplog.at_level("WARNING"):
        await _writer(session, suspect_batch=2).run_once()

    assert "privacy_aggregates.recompute_truncated" in caplog.text


@pytest.mark.asyncio
async def test_every_write_lands_in_one_committed_pass_per_tenant() -> None:
    """One writer, one statement, one transaction: nothing else may write this table."""
    session = _FakeSession(tenants=[_TENANT, uuid.uuid4()], feedback=_mix({"selected": (9, 7)}, actors=7))

    report = await _writer(session).run_once()

    assert report.tenants == 2
    assert session.commits == 2
    other = [sql for sql, _ in session.executed if "privacy_aggregates" in sql and not sql.startswith("SELECT metric")]
    assert all(sql.startswith("INSERT INTO privacy_aggregates") for sql in other)


def test_the_mapping_from_an_erased_class_to_the_metrics_it_moves_is_readable() -> None:
    """Asked of this module rather than kept as a second copy by whoever needs it."""
    assert aggregates.metrics_for_source(policies.RECORD_CONTEXT_FEEDBACK) == (aggregates.METRIC_FEEDBACK_RATING_MIX,)
    assert aggregates.metrics_for_source(policies.RECORD_EXTERNAL_SIGNAL) == (aggregates.METRIC_SIGNAL_SOURCE_MIX,)
    assert aggregates.metrics_for_source(policies.RECORD_AUDIT_LOG) == ()
