"""Unit tests for ``erase_claims_for_actor`` (contextplane.service.memory.claim_erasure_writes) --
the claims-table half of an actor erasure: scrub, repair, delete.

Scope, deliberately narrow: this function does not decide *which* claims are
erased. It receives an already-computed ``selected`` id list from the caller
(``ClaimErasure.erase_actor`` in ``claim_erasure.py``) and only performs the
writes that selection implies. The two-prong SELECT that decides which claims
die versus survive (the preference-namespace prong and the
no-independent-evidence prong) lives entirely in ``claim_erasure.py`` and
already has its own dedicated unit suite (``tests/unit/test_claim_erasure.py``)
that pins its predicate text and orchestration -- monkeypatching
``erase_claims_for_actor`` out at the import boundary so it never actually
runs. That is exactly the gap this file closes: before this suite,
``erase_claims_for_actor``'s own body -- the excerpt scrub, the confirmation
chain repair, the splice-vs-reopen decision, and the final deletes -- had zero
test references anywhere in the tree, unit or integration.

All DB interaction is mocked at ``session.execute`` via an SQL-string-keyed
router, mirroring ``tests/unit/test_promotion.py``'s ``erase_promotion_artifacts``
suite -- the closest sibling in shape (another erasure participant, another
multi-statement session function with no ORM model in between).

Coverage:
- The excerpt scrub: binds the target actor and tenant (the one query in this
  function that does -- everything downstream trusts the pre-scoped
  ``selected`` list and binds no actor/tenant of its own) and the
  never-matching placeholder substituted for an empty selection.
- The empty-selection case: the scrub still runs (survivors need scrubbing
  even when nothing is being deleted this call), every count comes back zero,
  and nothing past the scrub executes -- not a silent success it did not
  achieve.
- The losers lookup's ``FOR UPDATE`` clause, scoped to exactly the rows a
  concurrent supersession write could race.
- Each write family's ("prong's") independent contribution to the returned
  counts: the excerpt scrub, the confirmation-chain clear, the chain-splice
  decision, and the chain-reopen decision can each be nonzero without the
  others being touched, and all four compose correctly with the two final
  bulk deletes in a single call.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.service.memory.claim_erasure_writes import erase_claims_for_actor

# ---------------------------------------------------------------------------
# Shared router
# ---------------------------------------------------------------------------


def _erase_claims_router(
    *,
    scrub_rowcount: int = 0,
    confirm_cleared_rowcount: int = 0,
    splice_target_rows: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
    loser_rows: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
    provenance_rowcount: int = 0,
    claims_rowcount: int = 0,
) -> tuple[AsyncMock, list[tuple[str, dict[str, Any]]]]:
    """A session covering all six statements ``erase_claims_for_actor`` can
    issue, each routed by a distinguishing substring, most-specific-first
    where two share a prefix (the excerpt scrub and the final provenance
    delete both start with ``DELETE FROM memory_claim_provenance``).
    """
    executed: list[tuple[str, dict[str, Any]]] = []
    splice_rows = [SimpleNamespace(selected_id=sid, splice_to=to) for sid, to in (splice_target_rows or [])]
    losers = [SimpleNamespace(claim_id=cid, superseded_by=sb) for cid, sb in (loser_rows or [])]

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        p = dict(params or {})
        executed.append((sql, p))

        if "USING memory_session_events e" in sql:
            result = MagicMock()
            result.rowcount = scrub_rowcount
            return result
        if "SET confirms_claim_id = NULL" in sql:
            result = MagicMock()
            result.rowcount = confirm_cleared_rowcount
            return result
        if "WITH RECURSIVE chain AS" in sql:
            # `for row in await session.execute(...)` iterates the awaited
            # value directly -- no `.all()` -- so the mock returns a plain
            # list, not a Result-shaped object.
            return list(splice_rows)
        if "SELECT claim_id, superseded_by FROM memory_claims" in sql:
            result = MagicMock()
            result.all = MagicMock(return_value=list(losers))
            return result
        if "SET superseded_by = :to" in sql:
            return MagicMock()
        if "SET status = 'staged'" in sql:
            return MagicMock()
        if "DELETE FROM memory_claim_provenance WHERE claim_id = ANY(:selected)" in sql:
            result = MagicMock()
            result.rowcount = provenance_rowcount
            return result
        if "DELETE FROM memory_claims WHERE claim_id = ANY(:selected)" in sql:
            result = MagicMock()
            result.rowcount = claims_rowcount
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, executed


_ZERO_COUNTS = {
    "claims": 0,
    "provenance_rows": 0,
    "provenance_rows_scrubbed": 0,
    "confirmation_refs_cleared": 0,
    "chains_spliced": 0,
    "losers_reopened": 0,
}


# ---------------------------------------------------------------------------
# Parameter binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_excerpt_scrub_binds_the_target_actor_tenant_and_selected_ids() -> None:
    selected = [uuid.uuid4(), uuid.uuid4()]
    actor, tenant = uuid.uuid4(), uuid.uuid4()
    session, executed = _erase_claims_router()

    await erase_claims_for_actor(session, selected=selected, target_actor_id=actor, tenant_id=tenant)

    sql, params = executed[0]
    assert "USING memory_session_events e" in sql
    assert params["actor"] == actor
    assert params["tid"] == tenant
    assert params["selected"] == selected


@pytest.mark.asyncio
async def test_only_the_scrub_binds_actor_or_tenant_everything_else_trusts_the_selection() -> None:
    """Once the caller hands over a selection, every write past the scrub is
    scoped purely by claim id -- no second tenant/actor check re-runs here.
    That is only safe because the selection itself was already tenant- and
    actor-scoped by the caller; this test pins the boundary so a future
    change can't quietly assume this function re-checks it."""
    session, executed = _erase_claims_router()

    await erase_claims_for_actor(session, selected=[uuid.uuid4()], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    assert len(executed) > 1
    for sql, params in executed[1:]:
        assert "actor" not in params, sql
        assert "tid" not in params, sql


# ---------------------------------------------------------------------------
# Empty selection: the scrub still runs, nothing else does, counts are zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_selection_scrubs_and_returns_early_with_zero_counts() -> None:
    """An empty selection is not a no-op: session-event excerpts on surviving
    claims still need scrubbing. But it must not be a silent success this
    function did not actually achieve either -- none of the repair or delete
    statements may fire, and every one of their counts must read zero, not
    just happen to be absent. The router below only knows the scrub
    statement, so any further write is a hard failure rather than a quietly
    wrong zero."""
    executed: list[tuple[str, dict[str, Any]]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        executed.append((sql, dict(params or {})))
        if "USING memory_session_events e" in sql:
            result = MagicMock()
            result.rowcount = 0
            return result
        raise AssertionError(f"unexpected SQL after an empty selection: {sql}")

    session = AsyncMock()
    session.execute = _execute
    actor, tenant = uuid.uuid4(), uuid.uuid4()

    counts = await erase_claims_for_actor(session, selected=[], target_actor_id=actor, tenant_id=tenant)

    assert counts == _ZERO_COUNTS
    assert len(executed) == 1
    _, params = executed[0]
    assert params["selected"] == [uuid.UUID(int=0)], "empty selection must substitute the never-matching placeholder"


@pytest.mark.asyncio
async def test_empty_selection_with_a_nonzero_scrub_rowcount_is_not_mistaken_for_the_zero_case() -> None:
    """Positive control for the test above: a scrub that genuinely finds rows
    to remove must still report that count, proving the all-zero result
    above is the real outcome of an empty selection and not an artifact of a
    router that only ever returns zero."""
    session, _ = _erase_claims_router(scrub_rowcount=9)

    counts = await erase_claims_for_actor(session, selected=[], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    assert counts["provenance_rows_scrubbed"] == 9
    assert counts["claims"] == 0
    assert counts["confirmation_refs_cleared"] == 0


# ---------------------------------------------------------------------------
# Row locking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_losers_lookup_locks_its_candidate_rows_for_update() -> None:
    """Without the lock, a concurrent supersession write in another
    transaction could re-point a loser's `superseded_by` between this SELECT
    and the UPDATE this function issues from its result."""
    session, executed = _erase_claims_router()

    await erase_claims_for_actor(session, selected=[uuid.uuid4()], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    losers_sql = next(sql for sql, _ in executed if "SELECT claim_id, superseded_by FROM memory_claims" in sql)
    assert "superseded_by = ANY(:selected)" in losers_sql
    assert "claim_id <> ALL(:selected)" in losers_sql
    assert "FOR UPDATE" in losers_sql


# ---------------------------------------------------------------------------
# Each write family's independent contribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_excerpt_scrub_prong_contributes_independently() -> None:
    """A claim survives (isn't in `selected`) but still carries provenance
    authored from the target's own sessions -- scrubbed regardless of
    anything happening to the confirmation chain or a supersession chain
    elsewhere. Isolated here with no confirmation refs and no losers, so a
    nonzero scrub count can only be explained by the scrub statement's own
    rowcount."""
    session, _ = _erase_claims_router(scrub_rowcount=5)

    counts = await erase_claims_for_actor(
        session, selected=[uuid.uuid4()], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )

    assert counts["provenance_rows_scrubbed"] == 5
    assert counts["confirmation_refs_cleared"] == 0
    assert counts["chains_spliced"] == 0
    assert counts["losers_reopened"] == 0


@pytest.mark.asyncio
async def test_the_confirmation_clear_prong_contributes_independently() -> None:
    session, executed = _erase_claims_router(confirm_cleared_rowcount=2)

    counts = await erase_claims_for_actor(
        session, selected=[uuid.uuid4()], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )

    assert counts["confirmation_refs_cleared"] == 2
    assert counts["provenance_rows_scrubbed"] == 0
    assert counts["chains_spliced"] == 0
    assert counts["losers_reopened"] == 0
    clear_sql = next(sql for sql, _ in executed if "SET confirms_claim_id = NULL" in sql)
    assert "confirms_claim_id = ANY(:selected)" in clear_sql
    assert "claim_id <> ALL(:selected)" in clear_sql


@pytest.mark.asyncio
async def test_a_loser_whose_chain_reaches_an_unselected_successor_is_spliced_not_reopened() -> None:
    selected_claim = uuid.uuid4()
    successor = uuid.uuid4()
    loser = uuid.uuid4()
    session, executed = _erase_claims_router(
        splice_target_rows=[(selected_claim, successor)],
        loser_rows=[(loser, selected_claim)],
    )

    counts = await erase_claims_for_actor(
        session, selected=[selected_claim], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )

    assert counts["chains_spliced"] == 1
    assert counts["losers_reopened"] == 0
    _, splice_params = next((sql, p) for sql, p in executed if "SET superseded_by = :to" in sql)
    assert splice_params == {"to": successor, "cid": loser}


@pytest.mark.asyncio
async def test_a_loser_whose_entire_chain_is_selected_is_reopened_not_spliced() -> None:
    """No unselected successor exists anywhere in the chain -- the belief
    this loser was displaced by no longer exists, so it becomes the best
    remaining assertion again instead of staying superseded by nothing."""
    selected_claim = uuid.uuid4()
    loser = uuid.uuid4()
    session, executed = _erase_claims_router(
        splice_target_rows=[],  # the chain never leaves the selected set
        loser_rows=[(loser, selected_claim)],
    )

    counts = await erase_claims_for_actor(
        session, selected=[selected_claim], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )

    assert counts["losers_reopened"] == 1
    assert counts["chains_spliced"] == 0
    reopen_sql, reopen_params = next((sql, p) for sql, p in executed if "SET status = 'staged'" in sql)
    assert "superseded_by = NULL" in reopen_sql
    assert "consolidated_at = NULL" in reopen_sql
    assert reopen_params == {"cid": loser}


@pytest.mark.asyncio
async def test_every_prong_contributes_its_own_count_in_a_single_call() -> None:
    """All six counters can be nonzero in the same call, each reflecting only
    its own statement's rowcount or row-list length -- proof the counts dict
    isn't built by double-counting one write or dropping another when
    several fire together. One selected claim's loser splices, the other's
    reopens, in the same pass."""
    claim_a, claim_b = uuid.uuid4(), uuid.uuid4()
    successor = uuid.uuid4()
    loser_spliced, loser_reopened = uuid.uuid4(), uuid.uuid4()
    session, _ = _erase_claims_router(
        scrub_rowcount=7,
        confirm_cleared_rowcount=3,
        splice_target_rows=[(claim_a, successor)],
        loser_rows=[(loser_spliced, claim_a), (loser_reopened, claim_b)],
        provenance_rowcount=4,
        claims_rowcount=2,
    )

    counts = await erase_claims_for_actor(
        session, selected=[claim_a, claim_b], target_actor_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )

    assert counts == {
        "claims": 2,
        "provenance_rows": 4,
        "provenance_rows_scrubbed": 7,
        "confirmation_refs_cleared": 3,
        "chains_spliced": 1,
        "losers_reopened": 1,
    }


@pytest.mark.asyncio
async def test_a_driver_that_reports_no_rowcount_is_counted_as_zero_not_dropped() -> None:
    """Some drivers return ``None`` for ``rowcount`` rather than 0.

    The counts this returns are what an operator reads to confirm an erasure
    did what was asked, so a ``None`` falling through as ``None`` would read
    as a missing number rather than "nothing matched" -- and on a
    right-to-be-forgotten path those mean very different things. Carried over
    from a parallel implementation of this task, which pinned exactly this
    case and nothing else did.
    """
    session, _ = _erase_claims_router()
    original = session.execute

    async def _null_rowcount(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        result = await original(stmt, params)
        if hasattr(result, "rowcount"):
            result.rowcount = None
        return result

    session.execute = _null_rowcount

    counts = await erase_claims_for_actor(
        session,
        selected=[uuid.uuid4()],
        target_actor_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )

    assert all(count == 0 for count in counts.values()), counts
