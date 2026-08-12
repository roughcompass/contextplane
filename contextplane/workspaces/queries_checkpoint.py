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
`(tenant_id, intent_id, sequence)` already makes a duplicate sequence impossible,
but on its own it turns a concurrent append into a constraint violation the
caller has to interpret and retry. Taking the lock first makes concurrent
appends queue and produce one ordered chain, with the unique index still there
as the backstop for any writer that skipped the lock.

One statement here is issued by another module rather than written out below:
registering the head summary as a derivative. It lives with the head writes
regardless, because the property that matters is transactional -- a summary that
is written and not registered is a copy of a checkpoint's words that no erasure
can find, and the window in which that is true has to be zero rather than short.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Result, text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.retention import derivatives, policies
from contextplane.workspaces import derivative_handlers
from contextplane.workspaces.audience import (
    CAPABILITY_EXTEND,
    CAPABILITY_READ,
    RECOGNIZED_RESOLVERS,
    ROLES_THAT_EXTEND,
    ROLES_THAT_READ,
)

# The columns a checkpoint row is rehydrated from. Listed once so a read by id
# and a read by digest cannot drift into returning different shapes -- retrieval
# has to be stable across both, and two column lists is how that stops being
# true without anyone noticing.
_CHECKPOINT_COLUMNS = (
    "checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, decisions, assumptions, "
    "evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, digest"
)


#: The audience test, as SQL, in one place.
#:
#: `queries_audience.py` expresses the same rule as a SQLAlchemy predicate, and
#: the two cannot literally share an expression because these statements are raw
#: `text()`. What they do share is the vocabulary: the role sets and the
#: recognised resolvers are imported from the audience module rather than
#: restated, so the two definitions can drift in wording and not in meaning.
#:
#: Written as a correlated EXISTS against the row being read or written, so the
#: check happens in the same statement rather than as a separate round trip a
#: caller could skip. A checkpoint the actor may not see is not found, which is
#: also the answer for a checkpoint that does not exist -- the two must be
#: indistinguishable or the difference enumerates the tenant's tasks.
_AUDIENCE_EXISTS = """EXISTS (
    SELECT 1 FROM intent_participant_grants g
     WHERE g.tenant_id = :tenant_id
       AND g.intent_id = {task_column}
       AND g.actor_id = :actor_id
       AND g.granted_at <= :moment
       AND (g.expires_at IS NULL OR g.expires_at > :moment)
       AND g.resolver_version = ANY(:resolvers)
       AND g.role = ANY(:roles)
)"""


#: Which roles carry which capability, as the audience module's own sets.
#:
#: The role *names* are not restated here -- these are the exported frozensets,
#: so a role added to or removed from a capability there changes what these
#: statements accept without this file being touched. Only the two-entry
#: association is local, because the audience module's own capability table is
#: private to it. A public accessor there would remove even that.
_CAPABILITY_ROLES: dict[str, frozenset[str]] = {
    CAPABILITY_READ: ROLES_THAT_READ,
    CAPABILITY_EXTEND: ROLES_THAT_EXTEND,
}


def audience_params(*, actor_id: str, moment: datetime.datetime, capability: str) -> dict[str, Any]:
    """The bound parameters the audience clause needs.

    `capability` selects the role set rather than the call site naming roles.
    A statement that listed roles itself would be a second copy of the
    capability table, and the copy is what keeps honouring a role the table has
    stopped granting.
    """
    return {
        "actor_id": actor_id,
        "moment": moment,
        "resolvers": sorted(RECOGNIZED_RESOLVERS),
        "roles": sorted(_CAPABILITY_ROLES[capability]),
    }


async def lock_task(session: AsyncSession, *, tenant_id: uuid.UUID, intent_id: uuid.UUID) -> None:
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
        {"key": f"{tenant_id}:{intent_id}"},
    )


async def select_head(session: AsyncSession, *, tenant_id: uuid.UUID, intent_id: uuid.UUID) -> dict[str, Any] | None:
    """The current head projection for a task, or None if it has no checkpoints yet."""
    result = await session.execute(
        text(
            "SELECT tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at "
            "FROM intent_heads WHERE tenant_id = :tenant_id AND intent_id = :intent_id"
        ),
        {"tenant_id": tenant_id, "intent_id": intent_id},
    )
    return _one(result)


async def select_checkpoint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
    capability: str = CAPABILITY_READ,
) -> dict[str, Any] | None:
    """One checkpoint by its stable id, scoped to the tenant and the audience.

    Returns `None` for a checkpoint the actor may not see, which is the same
    answer as for one that does not exist. Distinguishing them would turn this
    read into a way to enumerate the tenant's task ids.
    """
    # The f-string interpolates a module constant column list and a module
    # constant clause, never caller input; every predicate is a bound parameter.
    statement = (
        f"SELECT {_CHECKPOINT_COLUMNS} FROM intent_checkpoints "  # noqa: S608
        "WHERE tenant_id = :tenant_id AND checkpoint_id = :cid "
        f"AND {_AUDIENCE_EXISTS.format(task_column='intent_checkpoints.intent_id')}"
    )
    result = await session.execute(
        text(statement),
        {
            "tenant_id": tenant_id,
            "cid": checkpoint_id,
            **audience_params(actor_id=actor_id, moment=moment, capability=capability),
        },
    )
    return _one(result)


async def select_checkpoint_by_digest(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    digest: str,
    actor_id: str,
    moment: datetime.datetime,
    capability: str = CAPABILITY_READ,
) -> dict[str, Any] | None:
    """One checkpoint by its content digest, scoped to the tenant that owns it.

    The digest covers the checkpoint id, so two checkpoints cannot share one
    digest unless they are the same checkpoint. `LIMIT 1` is a statement of that
    rather than a tolerance for duplicates.
    """
    # Same shape as select_checkpoint: constant column list, bound predicates.
    statement = (
        f"SELECT {_CHECKPOINT_COLUMNS} FROM intent_checkpoints "  # noqa: S608
        "WHERE tenant_id = :tenant_id AND digest = :digest "
        f"AND {_AUDIENCE_EXISTS.format(task_column='intent_checkpoints.intent_id')} LIMIT 1"
    )
    result = await session.execute(
        text(statement),
        {
            "tenant_id": tenant_id,
            "digest": digest,
            **audience_params(actor_id=actor_id, moment=moment, capability=capability),
        },
    )
    return _one(result)


async def insert_checkpoint(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    intent_id: uuid.UUID,
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
    actor_id: str,
    moment: datetime.datetime,
) -> bool:
    """Append one checkpoint row, if this actor may extend the task.

    Returns whether the row landed. `False` means the audience test failed --
    the caller is not a participant with a role that extends, at `moment`.

    Written as `INSERT ... SELECT ... WHERE EXISTS` rather than a check followed
    by an insert, so the authorization and the write are one statement. A
    separate pre-check is a statement a future caller can forget, and it also
    leaves a window in which a grant is revoked between the check and the
    insert.

    No ON CONFLICT clause, deliberately.

    A conflict here means two writers derived the same identity or the same
    sequence for one task, and the correct response is to fail the transaction,
    not to quietly keep whichever row landed first. The service resolves
    legitimate replays before it ever reaches this statement.
    """
    result = await session.execute(
        text(
            "INSERT INTO intent_checkpoints "
            "(checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, decisions, assumptions, "
            " evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, digest) "
            "SELECT :cid, :tenant_id, :intent_id, :sequence, :pred, :goal, CAST(:decisions AS JSONB), "
            " CAST(:assumptions AS JSONB), CAST(:evidence AS JSONB), CAST(:completed_checks AS JSONB), "
            " CAST(:open_questions AS JSONB), :next_action, :author, :recorded_at, :retention_policy, :digest "
            f"WHERE {_AUDIENCE_EXISTS.format(task_column=':intent_id')}"
        ),
        {
            "cid": checkpoint_id,
            "tenant_id": tenant_id,
            "intent_id": intent_id,
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
            **audience_params(actor_id=actor_id, moment=moment, capability=CAPABILITY_EXTEND),
        },
    )
    return bool(cast("CursorResult[Any]", result).rowcount)


async def upsert_head(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
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
            "INSERT INTO intent_heads (tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at) "
            "VALUES (:tenant_id, :intent_id, :cid, :sequence, :summary, :updated_at) "
            "ON CONFLICT (tenant_id, intent_id) DO UPDATE SET "
            " head_checkpoint_id = EXCLUDED.head_checkpoint_id, "
            " head_sequence = EXCLUDED.head_sequence, "
            " summary = EXCLUDED.summary, "
            " updated_at = EXCLUDED.updated_at "
            "WHERE intent_heads.head_sequence < EXCLUDED.head_sequence"
        ),
        {
            "tenant_id": tenant_id,
            "intent_id": intent_id,
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
    intent_id: uuid.UUID,
    summary: str,
    updated_at: datetime.datetime,
) -> uuid.UUID | None:
    """Overwrite the mutable summary without touching the chain it points at.

    Returns the head checkpoint the summary now describes, or `None` when there
    was no head to update -- so the caller can tell "no such task" from "summary
    replaced". The head checkpoint id and sequence are left alone: the summary is
    prose about the task, and letting it move the head would make a note the
    thing that decides what resume reads.

    The head checkpoint comes back from the update itself rather than from a read
    before or after it. It is the source the new summary has to be registered
    against, and any second statement asking which checkpoint that is would be
    answering about a different instant than the one that wrote the prose.
    """
    result = await session.execute(
        text(
            "UPDATE intent_heads SET summary = :summary, updated_at = :updated_at "
            "WHERE tenant_id = :tenant_id AND intent_id = :intent_id "
            "RETURNING head_checkpoint_id"
        ),
        {"summary": summary, "updated_at": updated_at, "tenant_id": tenant_id, "intent_id": intent_id},
    )
    row = result.first()
    return None if row is None else uuid.UUID(str(row[0]))


async def register_summary_derivative(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
    head_checkpoint_id: uuid.UUID,
) -> uuid.UUID:
    """Record the head summary as a derivative of the checkpoint it was built from.

    Called on the caller's session, inside the caller's transaction, for the same
    reason the audit write is: a summary that commits without its registration is
    a copy of a checkpoint's words that an erasure of that checkpoint cannot find,
    and it stays invisible precisely because nothing about the row says it was
    derived from anything.

    Registering the same head twice is one row -- the locator is the identity --
    and it adds a source link for each checkpoint the summary has described. That
    is the conservative direction on purpose: an extra link makes the summary
    reachable from an erasure of a checkpoint whose words may no longer be in it,
    and a missing one makes it unreachable from one whose words are.
    """
    return await derivatives.register_derivative(
        session,
        tenant_id=tenant_id,
        kind=derivatives.KIND_SUMMARY,
        storage_locator=derivative_handlers.summary_locator(intent_id),
        audience_partition=derivative_handlers.summary_audience(intent_id),
        classification=derivative_handlers.SUMMARY_CLASSIFICATION,
        handler_version=derivative_handlers.SUMMARY_HANDLER_VERSION,
        sources=[
            derivatives.SourceRef(
                record_class=policies.RECORD_TASK_CHECKPOINT,
                source_id=head_checkpoint_id,
                # A checkpoint's retention is bounded by tenant or workspace
                # deletion rather than by a duration, so it carries no expiry of
                # its own to inherit.
                expires_at=None,
            )
        ],
        fallback_expiry=derivative_handlers.EVENT_BOUNDED_HORIZON,
    )


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
    "register_summary_derivative",
    "select_checkpoint",
    "select_checkpoint_by_digest",
    "select_head",
    "update_head_summary",
    "upsert_head",
]
