"""Direct tests for the claims-table actor-erasure writer.

The participant-level suite proves which claims are selected with real
Postgres rows. These tests start at the next boundary: a selected claim list
has already been locked by the participant, and ``erase_claims_for_actor``
must scrub the target's excerpts from survivors, repair references to doomed
claims, and delete provenance before claims without leaving the caller's
transaction.

The empty-selection case still performs the survivor-excerpt scrub. A target
can own no deletable claim while their session-event provenance appears on a
claim kept alive by independent evidence, so returning before that first
delete would leave their verbatim text behind.

The complementary integration suite reaches this writer through
``ClaimErasure.erase_actor`` and proves all three real-database selection
outcomes: sole target-owned session evidence dies, independent evidence
survives, and a dangling event reference does not protect a claim. This file
covers the writer's mechanics: scrub-only early return, full deletion and
counts, locked chain splice versus reopen, and nullable driver row counts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from registry.service.memory.claims import erase_claims_for_actor


class _Result:
    """Small synchronous result surface returned by ``AsyncSession.execute``."""

    def __init__(self, *, rowcount: int | None = 0, rows: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)


def _session(
    *,
    scrubbed: int | None = 0,
    confirmations_cleared: int | None = 0,
    provenance_deleted: int | None = 0,
    claims_deleted: int | None = 0,
    splice_targets: list[Any] | None = None,
    losers: list[Any] | None = None,
) -> tuple[AsyncMock, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _execute(statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(statement).split())
        calls.append((sql, dict(params or {})))

        if sql.startswith("DELETE FROM memory_claim_provenance p USING memory_session_events e"):
            return _Result(rowcount=scrubbed)
        if "SET confirms_claim_id = NULL" in sql:
            return _Result(rowcount=confirmations_cleared)
        if sql.startswith("WITH RECURSIVE chain AS"):
            return _Result(rows=splice_targets)
        if sql.startswith("SELECT claim_id, superseded_by FROM memory_claims"):
            return _Result(rows=losers)
        if sql.startswith("UPDATE memory_claims SET superseded_by = :to"):
            return _Result()
        if "SET status = 'staged', superseded_by = NULL" in sql:
            return _Result()
        if sql == "DELETE FROM memory_claim_provenance WHERE claim_id = ANY(:selected)":
            return _Result(rowcount=provenance_deleted)
        if sql == "DELETE FROM memory_claims WHERE claim_id = ANY(:selected)":
            return _Result(rowcount=claims_deleted)
        raise AssertionError(f"unexpected SQL in erasure test: {sql}")

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session, calls


@pytest.mark.asyncio
async def test_empty_selection_still_scrubs_target_excerpts_without_deleting_claims() -> None:
    target_actor_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    session, calls = _session(scrubbed=2)

    counts = await erase_claims_for_actor(
        session,
        selected=[],
        target_actor_id=target_actor_id,
        tenant_id=tenant_id,
    )

    assert len(calls) == 1
    sql, params = calls[0]
    assert "e.actor_id = :actor AND e.tenant_id = :tid" in sql
    assert params == {
        "actor": target_actor_id,
        "tid": tenant_id,
        "selected": [uuid.UUID(int=0)],
    }
    assert counts == {
        "claims": 0,
        "provenance_rows": 0,
        "provenance_rows_scrubbed": 2,
        "confirmation_refs_cleared": 0,
        "chains_spliced": 0,
        "losers_reopened": 0,
    }


@pytest.mark.asyncio
async def test_selected_claims_bind_scope_and_report_every_table_count() -> None:
    selected = [uuid.uuid4(), uuid.uuid4()]
    target_actor_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    session, calls = _session(
        scrubbed=3,
        confirmations_cleared=4,
        provenance_deleted=5,
        claims_deleted=2,
    )

    counts = await erase_claims_for_actor(
        session,
        selected=selected,
        target_actor_id=target_actor_id,
        tenant_id=tenant_id,
    )

    assert calls[0][1] == {
        "actor": target_actor_id,
        "tid": tenant_id,
        "selected": selected,
    }
    assert all(call_params["selected"] == selected for _, call_params in calls[1:] if "selected" in call_params)
    locked_index = next(i for i, (sql, _) in enumerate(calls) if sql.startswith("SELECT claim_id, superseded_by"))
    assert "FOR UPDATE" in calls[locked_index][0]
    provenance_index = next(
        i
        for i, (sql, _) in enumerate(calls)
        if sql == "DELETE FROM memory_claim_provenance WHERE claim_id = ANY(:selected)"
    )
    claims_index = next(
        i for i, (sql, _) in enumerate(calls) if sql == "DELETE FROM memory_claims WHERE claim_id = ANY(:selected)"
    )
    assert provenance_index < claims_index
    assert counts == {
        "claims": 2,
        "provenance_rows": 5,
        "provenance_rows_scrubbed": 3,
        "confirmation_refs_cleared": 4,
        "chains_spliced": 0,
        "losers_reopened": 0,
    }


@pytest.mark.asyncio
async def test_cross_author_losers_are_spliced_or_reopened_under_the_same_lock() -> None:
    selected_with_successor, selected_without_successor = uuid.uuid4(), uuid.uuid4()
    successor = uuid.uuid4()
    spliced_loser, reopened_loser = uuid.uuid4(), uuid.uuid4()
    session, calls = _session(
        splice_targets=[SimpleNamespace(selected_id=selected_with_successor, splice_to=successor)],
        losers=[
            SimpleNamespace(claim_id=spliced_loser, superseded_by=selected_with_successor),
            SimpleNamespace(claim_id=reopened_loser, superseded_by=selected_without_successor),
        ],
    )

    counts = await erase_claims_for_actor(
        session,
        selected=[selected_with_successor, selected_without_successor],
        target_actor_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )

    splice_call = next(
        (sql, params) for sql, params in calls if sql.startswith("UPDATE memory_claims SET superseded_by")
    )
    assert splice_call[1] == {"to": successor, "cid": spliced_loser}
    reopen_call = next((sql, params) for sql, params in calls if "SET status = 'staged'" in sql)
    assert reopen_call[1] == {"cid": reopened_loser}
    assert "superseded_reason = NULL" in reopen_call[0]
    assert "t_invalidated_at = NULL" in reopen_call[0]
    assert "consolidated_at = NULL" in reopen_call[0]
    assert counts["chains_spliced"] == 1
    assert counts["losers_reopened"] == 1


@pytest.mark.asyncio
async def test_driver_rowcount_none_is_reported_as_zero() -> None:
    session, _ = _session(
        scrubbed=None,
        confirmations_cleared=None,
        provenance_deleted=None,
        claims_deleted=None,
    )

    counts = await erase_claims_for_actor(
        session,
        selected=[uuid.uuid4()],
        target_actor_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )

    assert all(count == 0 for count in counts.values())
