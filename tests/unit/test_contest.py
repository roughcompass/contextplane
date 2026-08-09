"""Unit tests for contextplane.service.memory.contest.

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session --
no Postgres required, mirroring `tests/unit/test_promotion.py`'s pattern.
`values_compatible`, `is_near_duplicate`, and `intervals_overlap` run for
real (not mocked): the module docstring is explicit that this comparator
"has to agree with the one consolidation uses," and that claim is only
worth anything if the real comparator is what a test here actually drives.

Coverage -- `detect_for_claim`:
- an unlinked/absent subject and a multi-valued (non-`single`) predicate
  both short-circuit to an empty outcome before any neighbourhood query
- a genuine incompatible, overlapping pair is detected and marks *both*
  sides contested in one statement -- not just the newly-written claim
- a non-overlapping ("handover") pair is not a disagreement
- a near-duplicate pair (the same compatibility check consolidation uses)
  is agreement, not disagreement -- the exact regression this module's
  docstring names by its own incident ("twenty sessions... seventeen
  contested claims that no reviewer could resolve")
- an undecidable comparison (prose) and a type-mismatched neighbour are
  both skipped rather than treated as conflicts

Coverage -- `resolve`:
- an unknown resolution raises this codebase's `ValidationError`, matching
  every other rejection path in the service layer. **This function has
  zero production callers as of this suite** -- nothing in the service
  layer calls `resolve()`; only `resolve_contests_for` (a different
  function in this module) is wired in, from `consolidation.py`. It is
  real, reachable, and tested here because a caller may reasonably need
  one-contest-at-a-time resolution later; the absence of a production
  caller is exactly why rebasing this one raise site onto the shared
  exception tree carries no risk of a silently-uncaught type elsewhere.
- settling a contest that no longer has an open row is a no-op: no
  counterparty recompute fires
- settling a real, open contest recomputes both counterparty flags in one
  statement, scoped to exactly the two claims in the pair

Coverage -- `groups_for`:
- a three-way disagreement collapses to one group with three members, not
  the five a two-column SQL aggregate would report
- two contested predicates on one subject stay two groups, because they are
  two questions with possibly different answers and owners
- a group's age is its oldest detection, so re-detection does not reset the
  age of a contradiction nobody resolved
- both sides of a pair are tenant-scoped, so a pair straddling two tenants is
  omitted rather than half-served -- detection itself has no tenant filter
- only unresolved pairs are read, and the optional axis narrowing binds
  parameters rather than interpolating them
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.exceptions import ValidationError
from contextplane.service.catalog.global_vocabulary import CARDINALITY_MULTI, CARDINALITY_SINGLE
from contextplane.service.memory.contest import (
    RESOLUTION_SUPERSEDED,
    detect_for_claim,
    groups_for,
    resolve,
)

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _subject_row(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "subject_entity_id": uuid.uuid4(),
        "predicate": "owned_by_team",
        "value_jsonb": "platform",
        "value_type": "string",
        "value_cardinality": CARDINALITY_SINGLE,
        "value_entity_id": None,
        "asserted_valid_from": _NOW - datetime.timedelta(days=1),
        "asserted_valid_to": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _neighbour_row(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "value_jsonb": "platform",
        "value_type": "string",
        "value_entity_id": None,
        "asserted_valid_from": _NOW - datetime.timedelta(days=1),
        "asserted_valid_to": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _detect_router(*, subject: Any | None, neighbours: list[Any]) -> tuple[AsyncMock, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {"contest_insert": [], "mark_contested": [], "executed": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls["executed"].append(sql)
        result = MagicMock()
        if "FROM memory_claims WHERE claim_id" in sql and "status = 'staged'" in sql:
            result.one_or_none = MagicMock(return_value=subject)
            return result
        if "FROM memory_claims" in sql and "ORDER BY asserted_valid_from DESC" in sql:
            result.all = MagicMock(return_value=neighbours)
            return result
        if "INSERT INTO memory_claim_contest" in sql:
            calls["contest_insert"].append(params or {})
            return MagicMock()
        if "UPDATE memory_claims SET is_contested = TRUE" in sql:
            calls["mark_contested"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, calls


# ---------------------------------------------------------------------------
# detect_for_claim -- short-circuits before any neighbourhood query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_for_claim_with_no_resolved_subject_returns_an_empty_outcome() -> None:
    session, calls = _detect_router(subject=None, neighbours=[])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert outcome.is_contested is False
    # The one query ran; nothing about a neighbourhood was ever asked.
    assert len(calls["executed"]) == 1


@pytest.mark.asyncio
async def test_detect_for_claim_skips_a_multi_valued_predicate() -> None:
    """A set-valued predicate's differing values are two facts, not two
    answers to one question -- the sweep never even queries a neighbourhood
    for one."""
    subject = _subject_row(value_cardinality=CARDINALITY_MULTI)
    session, calls = _detect_router(subject=subject, neighbours=[])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert len(calls["executed"]) == 1


# ---------------------------------------------------------------------------
# detect_for_claim -- real disagreement, and the both-sides marking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_for_claim_finds_an_incompatible_overlapping_pair() -> None:
    claim_id = uuid.uuid4()
    subject = _subject_row(value_jsonb="platform")
    neighbour = _neighbour_row(value_jsonb="finance")
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=claim_id, now=_NOW)

    assert outcome.is_contested is True
    assert outcome.neighbourhood_size == 1
    assert len(outcome.detected) == 1
    pair = outcome.detected[0]
    assert {pair.lower_claim_id, pair.upper_claim_id} == {claim_id, neighbour.claim_id}
    assert len(calls["contest_insert"]) == 1


@pytest.mark.asyncio
async def test_detect_for_claim_marks_both_sides_of_the_pair_contested_in_one_statement() -> None:
    """Not just the new claim: leaving the older side looking uncontested
    while a disagreement row says otherwise is exactly the bug the module
    docstring calls out."""
    claim_id = uuid.uuid4()
    subject = _subject_row(value_jsonb="platform")
    neighbour = _neighbour_row(value_jsonb="finance")
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    await detect_for_claim(session, claim_id=claim_id, now=_NOW)

    assert len(calls["mark_contested"]) == 1
    marked_ids = set(calls["mark_contested"][0]["ids"])
    assert marked_ids == {claim_id, neighbour.claim_id}


@pytest.mark.asyncio
async def test_detect_for_claim_counterparties_returns_the_other_side_of_every_pair() -> None:
    claim_id = uuid.uuid4()
    subject = _subject_row(value_jsonb="platform")
    neighbour = _neighbour_row(value_jsonb="finance")
    session, _ = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=claim_id, now=_NOW)

    assert outcome.counterparties(claim_id) == (neighbour.claim_id,)


@pytest.mark.asyncio
async def test_detect_for_claim_ignores_a_non_overlapping_successor_claim() -> None:
    """A claim ending exactly when another begins is a handover, not a
    disagreement."""
    handover_at = _NOW
    subject = _subject_row(
        value_jsonb="platform",
        asserted_valid_from=handover_at - datetime.timedelta(days=30),
        asserted_valid_to=handover_at,
    )
    neighbour = _neighbour_row(value_jsonb="finance", asserted_valid_from=handover_at, asserted_valid_to=None)
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert calls["contest_insert"] == []
    assert calls["mark_contested"] == []


@pytest.mark.asyncio
async def test_detect_for_claim_treats_a_near_duplicate_as_agreement_not_disagreement() -> None:
    """Two phrasings of one assertion, differing only by folding and the
    noise word "the" -- `values_compatible` alone would call this
    INCOMPATIBLE, and this is the check that must agree with consolidation's
    duplicate-collapse logic rather than contesting it."""
    subject = _subject_row(value_jsonb="Platform Team")
    neighbour = _neighbour_row(value_jsonb="the platform")
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert calls["contest_insert"] == []
    assert calls["mark_contested"] == []


@pytest.mark.asyncio
async def test_detect_for_claim_skips_an_undecidable_prose_comparison() -> None:
    """Prose cannot be compared without a model, so `values_compatible`
    reports UNDECIDABLE -- calling that a disagreement would manufacture a
    contested claim out of a comparison nobody could make."""
    subject = _subject_row(value_type="prose", value_jsonb="a paragraph")
    neighbour = _neighbour_row(value_type="prose", value_jsonb="a different paragraph")
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert calls["contest_insert"] == []


@pytest.mark.asyncio
async def test_detect_for_claim_skips_a_type_mismatched_neighbour() -> None:
    """Should not happen while a predicate's type is immutable, but a
    mismatched pair is not comparable and must not be forced into either
    verdict."""
    subject = _subject_row(value_type="string", value_jsonb="platform")
    neighbour = _neighbour_row(value_type="integer", value_jsonb=5)
    session, calls = _detect_router(subject=subject, neighbours=[neighbour])

    outcome = await detect_for_claim(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.detected == ()
    assert calls["contest_insert"] == []


# ---------------------------------------------------------------------------
# resolve -- pinned as-is (zero production callers), and the counterparty
# recompute.
# ---------------------------------------------------------------------------


def _resolve_router(*, affected: tuple[uuid.UUID, uuid.UUID] | None) -> tuple[AsyncMock, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {"recompute": []}
    row = None
    if affected is not None:
        row = MagicMock(lower_claim_id=affected[0], upper_claim_id=affected[1])

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "UPDATE memory_claim_contest" in sql:
            result.one_or_none = MagicMock(return_value=row)
            return result
        if "UPDATE memory_claims SET is_contested = FALSE" in sql:
            calls["recompute"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, calls


@pytest.mark.asyncio
async def test_resolve_rejects_an_unknown_resolution_with_the_shared_validation_error() -> None:
    """Matches every other rejection path in the service layer -- this
    codebase's `ValidationError`, not a bare `ValueError`."""
    session, calls = _resolve_router(affected=None)

    with pytest.raises(ValidationError, match="unknown resolution"):
        await resolve(session, contest_id=uuid.uuid4(), resolution="not_a_real_resolution", now=_NOW)
    assert calls["recompute"] == []


@pytest.mark.asyncio
async def test_resolve_is_a_noop_when_no_open_contest_matches() -> None:
    """Already resolved, or the id does not exist: either way there is
    nothing to settle, so the counterparty recompute never fires."""
    session, calls = _resolve_router(affected=None)

    await resolve(session, contest_id=uuid.uuid4(), resolution=RESOLUTION_SUPERSEDED, now=_NOW)

    assert calls["recompute"] == []


@pytest.mark.asyncio
async def test_resolve_recomputes_both_counterparty_flags_scoped_to_the_pair() -> None:
    lower, upper = uuid.uuid4(), uuid.uuid4()
    session, calls = _resolve_router(affected=(lower, upper))

    await resolve(session, contest_id=uuid.uuid4(), resolution=RESOLUTION_SUPERSEDED, now=_NOW)

    assert len(calls["recompute"]) == 1
    assert set(calls["recompute"][0]["ids"]) == {lower, upper}


# ---------------------------------------------------------------------------
# groups_for -- pairs collapsed to one group per (subject, predicate) axis.
# ---------------------------------------------------------------------------


def _pair_row(
    *,
    lower: uuid.UUID,
    upper: uuid.UUID,
    subject_entity_id: uuid.UUID,
    predicate: str = "owned_by_team",
    subject_reference: str = "cap:checkout",
    detected_at: datetime.datetime | None = None,
    contest_id: uuid.UUID | None = None,
) -> MagicMock:
    return MagicMock(
        contest_id=contest_id or uuid.uuid4(),
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        lower_claim_id=lower,
        upper_claim_id=upper,
        detected_at=detected_at or _NOW,
        subject_reference=subject_reference,
    )


def _groups_router(rows: list[Any]) -> tuple[AsyncMock, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {"params": [], "sql": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls["sql"].append(sql)
        calls["params"].append(params or {})
        result = MagicMock()
        if "FROM memory_claim_contest k" in sql:
            result.all = MagicMock(return_value=rows)
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, calls


@pytest.mark.asyncio
async def test_groups_for_collapses_a_three_way_disagreement_to_three_members() -> None:
    """The regression the union exists to prevent: three claims disagreeing about
    one axis are three pairs, and aggregating the two id columns separately would
    report five members for a group that has three."""
    entity = uuid.uuid4()
    a, b, c = sorted((uuid.uuid4(), uuid.uuid4(), uuid.uuid4()), key=str)
    rows = [
        _pair_row(lower=a, upper=b, subject_entity_id=entity),
        _pair_row(lower=a, upper=c, subject_entity_id=entity),
        _pair_row(lower=b, upper=c, subject_entity_id=entity),
    ]
    session, _ = _groups_router(rows)

    groups = await groups_for(session, tenant_id=uuid.uuid4())

    assert len(groups) == 1
    group = groups[0]
    assert set(group.claim_ids) == {a, b, c}
    assert group.member_count == 3
    assert len(group.contest_ids) == 3


@pytest.mark.asyncio
async def test_groups_for_separates_distinct_axes() -> None:
    """One subject with two contested predicates is two questions for a
    reviewer, not one -- they may have different answers and different owners."""
    entity = uuid.uuid4()
    rows = [
        _pair_row(lower=uuid.uuid4(), upper=uuid.uuid4(), subject_entity_id=entity, predicate="owned_by_team"),
        _pair_row(lower=uuid.uuid4(), upper=uuid.uuid4(), subject_entity_id=entity, predicate="tier"),
    ]
    session, _ = _groups_router(rows)

    groups = await groups_for(session, tenant_id=uuid.uuid4())

    assert {g.predicate for g in groups} == {"owned_by_team", "tier"}
    assert all(g.member_count == 2 for g in groups)


@pytest.mark.asyncio
async def test_groups_for_reports_the_oldest_detection_as_the_groups_age() -> None:
    """A group's age is when the disagreement started, not when the most recent
    pair was noticed -- otherwise re-detection would keep resetting the age of a
    contradiction nobody has resolved."""
    entity = uuid.uuid4()
    first = _NOW - datetime.timedelta(days=3)
    rows = [
        _pair_row(lower=uuid.uuid4(), upper=uuid.uuid4(), subject_entity_id=entity, detected_at=first),
        _pair_row(lower=uuid.uuid4(), upper=uuid.uuid4(), subject_entity_id=entity, detected_at=_NOW),
    ]
    session, _ = _groups_router(rows)

    groups = await groups_for(session, tenant_id=uuid.uuid4())

    assert groups[0].first_detected_at == first


@pytest.mark.asyncio
async def test_groups_for_requires_both_claims_in_the_calling_tenant() -> None:
    """A pair can straddle two tenants because detection has no tenant filter, so
    the query joins *both* claim rows and scopes both. Scoping one side would hand
    the counterparty's claim id to whoever owns the other."""
    tenant = uuid.uuid4()
    session, calls = _groups_router([])

    await groups_for(session, tenant_id=tenant)

    sql = calls["sql"][0]
    assert "JOIN memory_claims lo ON lo.claim_id = k.lower_claim_id" in sql
    assert "JOIN memory_claims up ON up.claim_id = k.upper_claim_id" in sql
    assert "COALESCE(lo.owning_tenant_id, lo.author_tenant_id) = :tid" in sql
    assert "COALESCE(up.owning_tenant_id, up.author_tenant_id) = :tid" in sql
    assert calls["params"][0]["tid"] == tenant


@pytest.mark.asyncio
async def test_groups_for_reads_only_unresolved_pairs() -> None:
    """A settled disagreement is history, not a question for a reviewer."""
    session, calls = _groups_router([])

    await groups_for(session, tenant_id=uuid.uuid4())

    assert "k.resolved_at IS NULL" in calls["sql"][0]


@pytest.mark.asyncio
async def test_groups_for_narrows_to_one_axis_by_bound_parameters() -> None:
    """The optional narrowing is bound parameters, not interpolated values."""
    entity = uuid.uuid4()
    session, calls = _groups_router([])

    await groups_for(session, tenant_id=uuid.uuid4(), subject_entity_id=entity, predicate="tier")

    assert "k.subject_entity_id = :eid" in calls["sql"][0]
    assert "k.predicate = :pred" in calls["sql"][0]
    assert calls["params"][0]["eid"] == entity
    assert calls["params"][0]["pred"] == "tier"


@pytest.mark.asyncio
async def test_groups_for_returns_nothing_when_no_disagreement_is_open() -> None:
    session, _ = _groups_router([])

    assert await groups_for(session, tenant_id=uuid.uuid4()) == ()
