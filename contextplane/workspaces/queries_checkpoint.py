"""Every SQL statement the checkpoint chain issues, and nothing else.

Split from the service on purpose. The rules about *when* a checkpoint may be
appended are policy and belong next to the code that decides them; the shape of
the statements that carry it out is the part that has to stay parameterized,
tenant-scoped and reviewable in one place. A reader auditing "can this subsystem
read another tenant's checkpoint" reads this file and nothing else.

Two invariants are visible in every statement here:

**`tenant_id` is a predicate, never an assumption.** Each read binds it
explicitly rather than trusting that the caller already scoped the id it passed
in. A `checkpoint_id` is a UUID a client can hold; without the tenant predicate,
holding one from another tenant would be enough to read it.

**Appends serialize on an advisory lock keyed by the task.** The unique index on
`(tenant_id, task_id, sequence)` already makes a duplicate sequence impossible,
but on its own it turns a concurrent append into a constraint violation the
caller has to interpret and retry. Taking the lock first makes concurrent
appends queue and produce one ordered chain, with the unique index still there
as the backstop for any writer that skipped the lock.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Result, text
from sqlalchemy.ext.asyncio import AsyncSession

# The columns a checkpoint row is rehydrated from. Listed once so a read by id
# and a read by digest cannot drift into returning different shapes -- retrieval
# has to be stable across both, and two column lists is how that stops being
# true without anyone noticing.
_CHECKPOINT_COLUMNS = (
    "checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, decisions, assumptions, "
    "evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, digest"
)


async def lock_task(session: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID) -> None:
    """Serialize appends to one task for the rest of this transaction.

    Transaction-scoped (`_xact_`) rather than session-scoped so the lock is
    released by commit or rollback, including the rollback nobody wrote code
    for. A session-scoped lock survives a failed append and would strand the
    task until the connection is returned to the pool.

    Keyed by tenant *and* task: two tenants appending to task ids that happen to
    collide are unrelated writers, and making them wait on each other would be a
    cross-tenant coupling with no reason behind it.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{tenant_id}:{task_id}"},
    )


async def select_head(session: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID) -> dict[str, Any] | None:
    """The current head projection for a task, or None if it has no checkpoints yet."""
    result = await session.execute(
        text(
            "SELECT tenant_id, task_id, head_checkpoint_id, head_sequence, summary, updated_at "
            "FROM task_heads WHERE tenant_id = :tenant_id AND task_id = :task_id"
        ),
        {"tenant_id": tenant_id, "task_id": task_id},
    )
    return _one(result)


async def select_checkpoint(
    session: AsyncSession, *, tenant_id: uuid.UUID, checkpoint_id: uuid.UUID
) -> dict[str, Any] | None:
    """One checkpoint by its stable id, scoped to the tenant that owns it."""
    # The f-string interpolates a module constant column list, never caller
    # input; every predicate below is still a bound parameter.
    statement = (
        f"SELECT {_CHECKPOINT_COLUMNS} FROM task_checkpoints "  # noqa: S608
        "WHERE tenant_id = :tenant_id AND checkpoint_id = :cid"
    )
    result = await session.execute(text(statement), {"tenant_id": tenant_id, "cid": checkpoint_id})
    return _one(result)


async def select_checkpoint_by_digest(
    session: AsyncSession, *, tenant_id: uuid.UUID, digest: str
) -> dict[str, Any] | None:
    """One checkpoint by its content digest, scoped to the tenant that owns it.

    The digest covers the checkpoint id, so two checkpoints cannot share one
    digest unless they are the same checkpoint. `LIMIT 1` is a statement of that
    rather than a tolerance for duplicates.
    """
    # Same shape as select_checkpoint: constant column list, bound predicates.
    statement = (
        f"SELECT {_CHECKPOINT_COLUMNS} FROM task_checkpoints "  # noqa: S608
        "WHERE tenant_id = :tenant_id AND digest = :digest LIMIT 1"
    )
    result = await session.execute(text(statement), {"tenant_id": tenant_id, "digest": digest})
    return _one(result)


async def insert_checkpoint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    task_id: uuid.UUID,
    sequence: int,
    predecessor_id: uuid.UUID | None,
    goal: str,
    decisions: Sequence[str],
    assumptions: Sequence[str],
    evidence: Sequence[Mapping[str, Any]],
    completed_checks: Sequence[str],
    open_questions: Sequence[str],
    next_action: str | None,
    author: str,
    recorded_at: datetime.datetime,
    retention_policy: str,
    digest: str,
) -> None:
    """Append one checkpoint row. No ON CONFLICT clause, deliberately.

    A conflict here means two writers derived the same identity or the same
    sequence for one task, and the correct response is to fail the transaction,
    not to quietly keep whichever row landed first. The service resolves
    legitimate replays before it ever reaches this statement.
    """
    await session.execute(
        text(
            "INSERT INTO task_checkpoints "
            "(checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, decisions, assumptions, "
            " evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, digest) "
            "VALUES (:cid, :tenant_id, :task_id, :sequence, :pred, :goal, CAST(:decisions AS JSONB), "
            " CAST(:assumptions AS JSONB), CAST(:evidence AS JSONB), CAST(:completed_checks AS JSONB), "
            " CAST(:open_questions AS JSONB), :next_action, :author, :recorded_at, :retention_policy, :digest)"
        ),
        {
            "cid": checkpoint_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "sequence": sequence,
            "pred": predecessor_id,
            "goal": goal,
            "decisions": _json(decisions),
            "assumptions": _json(assumptions),
            "evidence": _json(evidence),
            "completed_checks": _json(completed_checks),
            "open_questions": _json(open_questions),
            "next_action": next_action,
            "author": author,
            "recorded_at": recorded_at,
            "retention_policy": retention_policy,
            "digest": digest,
        },
    )


async def upsert_head(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    head_checkpoint_id: uuid.UUID,
    head_sequence: int,
    summary: str,
    updated_at: datetime.datetime,
) -> None:
    """Move the head projection to a newly appended checkpoint.

    The `head_sequence < EXCLUDED.head_sequence` guard makes the update
    monotonic. Under the task lock no out-of-order writer should exist, but the
    head is a projection: a projection that can be moved backwards is one where
    a late-arriving retry silently rewinds what resume reads as current.
    """
    await session.execute(
        text(
            "INSERT INTO task_heads (tenant_id, task_id, head_checkpoint_id, head_sequence, summary, updated_at) "
            "VALUES (:tenant_id, :task_id, :cid, :sequence, :summary, :updated_at) "
            "ON CONFLICT (tenant_id, task_id) DO UPDATE SET "
            " head_checkpoint_id = EXCLUDED.head_checkpoint_id, "
            " head_sequence = EXCLUDED.head_sequence, "
            " summary = EXCLUDED.summary, "
            " updated_at = EXCLUDED.updated_at "
            "WHERE task_heads.head_sequence < EXCLUDED.head_sequence"
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "cid": head_checkpoint_id,
            "sequence": head_sequence,
            "summary": summary,
            "updated_at": updated_at,
        },
    )


async def update_head_summary(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    summary: str,
    updated_at: datetime.datetime,
) -> bool:
    """Overwrite the mutable summary without touching the chain it points at.

    Returns whether a row was updated, so the caller can tell "no such task"
    from "summary replaced". The head checkpoint id and sequence are left alone:
    the summary is prose about the task, and letting it move the head would make
    a note the thing that decides what resume reads.
    """
    result = await session.execute(
        text(
            "UPDATE task_heads SET summary = :summary, updated_at = :updated_at "
            "WHERE tenant_id = :tenant_id AND task_id = :task_id"
        ),
        {"summary": summary, "updated_at": updated_at, "tenant_id": tenant_id, "task_id": task_id},
    )
    # `execute` is declared as returning the read-oriented `Result`; a DML
    # statement always yields a `CursorResult`, which is the only one that
    # carries a row count.
    return bool(cast("CursorResult[Any]", result).rowcount)


async def insert_audit(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    after: Mapping[str, Any],
    ts: datetime.datetime,
) -> None:
    """Write the audit row on the caller's session, inside the caller's transaction.

    Not on a session of its own. An audit row written separately either records
    an append that later rolled back, or is lost when the append that it
    describes commits and the audit write fails. Sharing the transaction makes
    the two share a fate, which is the only version of this that an auditor can
    rely on.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log (audit_id, tenant_id, actor_id, action, target_type, target_id, after_jsonb, ts) "
            "VALUES (:audit_id, :tenant_id, :actor_id, :action, :target_type, :target_id, CAST(:after AS JSONB), :ts)"
        ),
        {
            "audit_id": audit_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "after": _json(after),
            "ts": ts,
        },
    )


def _one(result: Result[Any]) -> dict[str, Any] | None:
    """The first row as a plain dict, or None.

    Copied out of the driver's row mapping on the way past so nothing above this
    module handles a SQLAlchemy row type. A caller that receives a live row also
    inherits its lifetime and its key semantics, neither of which is part of what
    these functions promise.
    """
    row = result.mappings().first()
    return None if row is None else {str(key): value for key, value in row.items()}


def _json(value: object) -> str:
    """Deterministic JSON for a JSONB bind.

    Sorted keys so the same content serializes identically every time: an
    evidence array whose key order varies would make two byte-different rows out
    of one checkpoint, and the digest that names it would stop being checkable
    against what is stored.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "insert_audit",
    "insert_checkpoint",
    "lock_task",
    "select_checkpoint",
    "select_checkpoint_by_digest",
    "select_head",
    "update_head_summary",
    "upsert_head",
]
