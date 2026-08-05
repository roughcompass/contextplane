"""Unit tests for registry.service.memory.contest.

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
- pinned exactly as it behaves today: an unknown resolution raises a bare
  `ValueError`, not the `ValidationError` this codebase's other rejection
  paths use. **This function has zero production callers as of this
  suite** -- nothing in the service layer calls `resolve()`; only
  `resolve_contests_for` (a different function in this module) is wired
  in, from `consolidation.py`. It is real, reachable, and tested here
  because a caller may reasonably need one-contest-at-a-time resolution
  later, but it is *not* being wired up or fixed as part of this change --
  see the task notes for why.
- settling a contest that no longer has an open row is a no-op: no
  counterparty recompute fires
- settling a real, open contest recomputes both counterparty flags in one
  statement, scoped to exactly the two claims in the pair
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.catalog.global_vocabulary import CARDINALITY_MULTI, CARDINALITY_SINGLE
from registry.service.memory.contest import (
    RESOLUTION_SUPERSEDED,
    detect_for_claim,
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
async def test_resolve_pins_the_current_bare_value_error_for_an_unknown_resolution() -> None:
    """As it behaves today: `ValueError`, not this codebase's
    `ValidationError`. Not the desired shape -- carried forward, not fixed,
    by this task -- but pinned so a future change to it is a deliberate
    decision rather than an accident nobody noticed."""
    session, calls = _resolve_router(affected=None)

    with pytest.raises(ValueError, match="unknown resolution"):
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
