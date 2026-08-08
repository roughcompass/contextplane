"""Reading and writing task participation, and scoping every task read by it.

Plain module-level functions over an already-open ``AsyncSession``; callers own
transaction scope, following the same convention as the other query modules in
this repository.

**The audience predicate is applied in SQL, not after the fetch.** Every read
here joins against an active grant for the asking actor. A version that selected
rows and filtered them in Python would be correct about what it returns and wrong
about everything else it reveals: a count computed before filtering, a keyset
page that comes back short, a lexical search whose latency tracks how many
matches the actor cannot see. Discovery is a property of the query, so the
predicate belongs in the query.

**"Active" is evaluated against a caller-supplied moment.** Not `now()` in SQL:
a request that authorizes at one instant and reads at another can straddle an
expiry, and two answers from one request is worse than either answer. The caller
takes the clock reading once and passes it down.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.workspaces.audience import RECOGNIZED_RESOLVERS
from contextplane.workspaces.models import TaskCheckpoint, TaskHead, TaskParticipantGrant
from contextplane.workspaces.schemas.task_memory import ParticipantRole, TaskParticipantGrantV1


def _active_grant_predicate(*, tenant_id: uuid.UUID, actor_id: str, moment: datetime.datetime) -> ColumnElement[bool]:
    """The one definition of "this actor participates right now".

    Every read below composes this rather than restating it. A second copy is
    how one read path keeps honouring a revoked grant after the others stop:
    the copies do not disagree loudly, they disagree in one code path nobody is
    looking at.
    """
    return and_(
        TaskParticipantGrant.tenant_id == tenant_id,
        TaskParticipantGrant.actor_id == actor_id,
        TaskParticipantGrant.granted_at <= moment,
        or_(TaskParticipantGrant.expires_at.is_(None), TaskParticipantGrant.expires_at > moment),
        # A grant from a resolver this build does not recognize is not evidence.
        # Enforced here as well as in the resolver so a query that never loads
        # grant objects cannot skip the check.
        TaskParticipantGrant.resolver_version.in_(sorted(RECOGNIZED_RESOLVERS)),
    )


def _authorized_task_ids(*, tenant_id: uuid.UUID, actor_id: str, moment: datetime.datetime) -> Select[tuple[uuid.UUID]]:
    """Sub-select of the task IDs this actor may see at *moment*."""
    return select(TaskParticipantGrant.task_id).where(
        _active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment)
    )


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


async def fetch_task_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
) -> list[TaskParticipantGrantV1]:
    """Every grant on one task, active or not, as contract objects.

    Expired grants are included: the resolver decides what "active" means, and
    an audit of a past read needs the grants that applied then. Callers
    authorizing a request pass the result to the resolver rather than trusting
    the row count.
    """
    rows = (
        await session.execute(
            select(TaskParticipantGrant)
            .where(TaskParticipantGrant.tenant_id == tenant_id, TaskParticipantGrant.task_id == task_id)
            .order_by(TaskParticipantGrant.granted_at)
        )
    ).scalars()
    return [
        TaskParticipantGrantV1(
            task_id=row.task_id,
            actor_id=row.actor_id,
            role=row.role,  # type: ignore[arg-type]
            granted_by=row.granted_by,
            granted_at=row.granted_at,
            expires_at=row.expires_at,
            resolver_version=row.resolver_version,
        )
        for row in rows
    ]


async def insert_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    grant: TaskParticipantGrantV1,
) -> None:
    """Store one grant. The contract object has already refused a self-grant."""
    session.add(
        TaskParticipantGrant(
            tenant_id=tenant_id,
            task_id=grant.task_id,
            actor_id=grant.actor_id,
            role=grant.role,
            granted_by=grant.granted_by,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
            resolver_version=grant.resolver_version,
        )
    )
    await session.flush()


async def revoke_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
) -> bool:
    """End an actor's participation at *moment*, keeping the row.

    Returns whether anything changed. A row already ended earlier is left
    alone: re-revoking must not extend the window that was already closed.
    """
    row = (
        await session.execute(
            select(TaskParticipantGrant).where(
                TaskParticipantGrant.tenant_id == tenant_id,
                TaskParticipantGrant.task_id == task_id,
                TaskParticipantGrant.actor_id == actor_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.expires_at is not None and row.expires_at <= moment:
        return False
    if moment <= row.granted_at:
        # The database refuses this window outright; refusing here names why
        # rather than surfacing a constraint violation from a flush later on.
        return False
    row.expires_at = moment
    await session.flush()
    return True


async def fetch_actor_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
) -> ParticipantRole | None:
    """The actor's active role on one task, or `None`.

    The narrow question, answered in one round trip, for call sites that need
    authorization and nothing else. It applies the same predicate as every
    other read here, so it cannot drift from them.
    """
    role = (
        await session.execute(
            select(TaskParticipantGrant.role).where(
                _active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment),
                TaskParticipantGrant.task_id == task_id,
            )
        )
    ).scalar_one_or_none()
    return role  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Audience-scoped task reads
# ---------------------------------------------------------------------------


async def list_authorized_task_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
    limit: int = 100,
) -> list[uuid.UUID]:
    """The tasks this actor may see. Nothing else appears, at any page size."""
    rows = (
        await session.execute(
            select(TaskParticipantGrant.task_id)
            .where(_active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment))
            .order_by(TaskParticipantGrant.task_id)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def count_authorized_tasks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
) -> int:
    """How many tasks this actor may see.

    Counted over the authorized set, never over the tenant's tasks. A total
    that includes tasks the caller cannot open is a disclosure with no row in
    it: watch the number move and you have learned that a task was created.
    """
    total = (
        await session.execute(
            select(func.count())
            .select_from(TaskParticipantGrant)
            .where(_active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment))
        )
    ).scalar_one()
    return int(total)


async def lookup_authorized_head(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    task_id: uuid.UUID,
    moment: datetime.datetime,
) -> TaskHead | None:
    """One task's head projection, if this actor participates.

    `None` covers both "no such task" and "not a participant" on purpose. The
    caller cannot distinguish them, so neither can anyone probing through it.
    """
    return (
        await session.execute(
            select(TaskHead).where(
                TaskHead.tenant_id == tenant_id,
                TaskHead.task_id == task_id,
                TaskHead.task_id.in_(_authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)),
            )
        )
    ).scalar_one_or_none()


async def search_authorized_checkpoints(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    term: str,
    moment: datetime.datetime,
    limit: int = 50,
) -> list[TaskCheckpoint]:
    """Lexical search over checkpoints, restricted to authorized tasks.

    The audience predicate is part of the search, not a filter over its result.
    Ranking or truncating first and authorizing second leaks through the shape
    of the page even when every row returned is permitted.

    An empty or whitespace-only term matches nothing rather than everything: a
    blank search that returned the caller's whole corpus is a different feature,
    and one an empty input should not silently invoke.
    """
    needle = term.strip()
    if not needle:
        return []
    rows = (
        await session.execute(
            select(TaskCheckpoint)
            .where(
                TaskCheckpoint.tenant_id == tenant_id,
                TaskCheckpoint.task_id.in_(_authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)),
                TaskCheckpoint.goal.ilike(f"%{needle}%"),
            )
            .order_by(TaskCheckpoint.recorded_at.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)


__all__ = [
    "count_authorized_tasks",
    "fetch_actor_role",
    "fetch_task_grants",
    "insert_grant",
    "list_authorized_task_ids",
    "lookup_authorized_head",
    "revoke_grant",
    "search_authorized_checkpoints",
]
