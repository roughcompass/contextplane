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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.exceptions import ConflictError
from contextplane.workspaces.audience import RECOGNIZED_RESOLVERS
from contextplane.workspaces.models import IntentCheckpoint, IntentHead, IntentParticipantGrant
from contextplane.workspaces.schemas.intent_memory import IntentParticipantGrantV1, ParticipantRole

#: The temporal exclusion migration 0070 installs. Named here so the translation
#: below matches on the driver's structured `constraint_name` rather than on
#: message text, which Postgres is free to rephrase.
_NO_OVERLAP = "ex_intent_participant_grants_no_overlap"


def _active_grant_predicate(*, tenant_id: uuid.UUID, actor_id: str, moment: datetime.datetime) -> ColumnElement[bool]:
    """The one definition of "this actor participates right now".

    Every read below composes this rather than restating it. A second copy is
    how one read path keeps honouring a revoked grant after the others stop:
    the copies do not disagree loudly, they disagree in one code path nobody is
    looking at.
    """
    return and_(
        IntentParticipantGrant.tenant_id == tenant_id,
        IntentParticipantGrant.actor_id == actor_id,
        IntentParticipantGrant.granted_at <= moment,
        or_(IntentParticipantGrant.expires_at.is_(None), IntentParticipantGrant.expires_at > moment),
        # A grant from a resolver this build does not recognize is not evidence.
        # Enforced here as well as in the resolver so a query that never loads
        # grant objects cannot skip the check.
        IntentParticipantGrant.resolver_version.in_(sorted(RECOGNIZED_RESOLVERS)),
    )


def _authorized_task_ids(*, tenant_id: uuid.UUID, actor_id: str, moment: datetime.datetime) -> Select[tuple[uuid.UUID]]:
    """Sub-select of the task IDs this actor may see at *moment*."""
    return select(IntentParticipantGrant.intent_id).where(
        _active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment)
    )


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


async def fetch_task_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
) -> list[IntentParticipantGrantV1]:
    """Every grant on one task, active or not, as contract objects.

    Expired grants are included: the resolver decides what "active" means, and
    an audit of a past read needs the grants that applied then. Callers
    authorizing a request pass the result to the resolver rather than trusting
    the row count.
    """
    rows = (
        await session.execute(
            select(IntentParticipantGrant)
            .where(IntentParticipantGrant.tenant_id == tenant_id, IntentParticipantGrant.intent_id == intent_id)
            .order_by(IntentParticipantGrant.granted_at)
        )
    ).scalars()
    return [
        IntentParticipantGrantV1(
            intent_id=row.intent_id,
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
    grant: IntentParticipantGrantV1,
) -> None:
    """Store one grant. The contract object has already refused a self-grant.

    A grant whose window overlaps one this actor already holds on this task is
    refused by `ex_intent_participant_grants_no_overlap` and reported as a
    conflict. Translating it here rather than letting the `IntegrityError` reach
    a router is the whole point of E7-T5: both adapters catch `AudienceDenied`
    and nothing else, so an untranslated constraint violation is a 500 for what
    is really "that actor is already a participant".

    Note what this does *not* refuse: a grant after a revoke. Revoke closes the
    window at `moment` and the reads treat the range as half-open, so the new
    grant's window starts where the old one ended and the two do not overlap.
    That sequence was a 500 before this constraint existed and is the ordinary
    case now.
    """
    session.add(
        IntentParticipantGrant(
            tenant_id=tenant_id,
            intent_id=grant.intent_id,
            actor_id=grant.actor_id,
            role=grant.role,
            granted_by=grant.granted_by,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
            resolver_version=grant.resolver_version,
        )
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        # Named, not matched on message text. `constraint_name` comes from the
        # driver's structured error, so this does not break when Postgres
        # rephrases its diagnostics.
        if getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None) != _NO_OVERLAP:
            raise
        raise ConflictError(
            f"actor {grant.actor_id} already participates in task {grant.intent_id} over an overlapping window; "
            "revoke the existing grant before issuing a new one"
        ) from exc


async def revoke_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    intent_id: uuid.UUID,
    actor_id: str,
    moment: datetime.datetime,
) -> bool:
    """End an actor's participation at *moment*, keeping the row.

    Returns whether anything changed. A row already ended earlier is left
    alone: re-revoking must not extend the window that was already closed.

    **Scoped to the grant whose window contains `moment`.** This used to select
    every row for `(tenant, intent, actor)` and take `scalar_one_or_none()`,
    which was safe only while a unique constraint guaranteed there was one.
    Since 0070 replaced that with a temporal exclusion, an actor granted,
    revoked and granted again has several rows and the old read would raise
    `MultipleResultsFound`. The exclusion is what makes this narrowed read
    single-valued: at most one window can contain any instant.

    The predicate is written out rather than reusing `_active_grant_predicate`
    because that one also requires a recognized `resolver_version`, and a grant
    issued by a resolver this build no longer recognizes must still be
    revocable. Refusing to revoke something because it is already unreadable
    would leave a row nobody can serve and nobody can close.
    """
    row = (
        await session.execute(
            select(IntentParticipantGrant).where(
                IntentParticipantGrant.tenant_id == tenant_id,
                IntentParticipantGrant.intent_id == intent_id,
                IntentParticipantGrant.actor_id == actor_id,
                IntentParticipantGrant.granted_at <= moment,
                or_(
                    IntentParticipantGrant.expires_at.is_(None),
                    IntentParticipantGrant.expires_at > moment,
                ),
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
    intent_id: uuid.UUID,
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
            select(IntentParticipantGrant.role).where(
                _active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment),
                IntentParticipantGrant.intent_id == intent_id,
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
            select(IntentParticipantGrant.intent_id)
            .where(_active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment))
            .order_by(IntentParticipantGrant.intent_id)
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
            .select_from(IntentParticipantGrant)
            .where(_active_grant_predicate(tenant_id=tenant_id, actor_id=actor_id, moment=moment))
        )
    ).scalar_one()
    return int(total)


async def lookup_authorized_head(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    intent_id: uuid.UUID,
    moment: datetime.datetime,
) -> IntentHead | None:
    """One task's head projection, if this actor participates.

    `None` covers both "no such task" and "not a participant" on purpose. The
    caller cannot distinguish them, so neither can anyone probing through it.
    """
    return (
        await session.execute(
            select(IntentHead).where(
                IntentHead.tenant_id == tenant_id,
                IntentHead.intent_id == intent_id,
                IntentHead.intent_id.in_(_authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)),
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
) -> list[IntentCheckpoint]:
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
            select(IntentCheckpoint)
            .where(
                IntentCheckpoint.tenant_id == tenant_id,
                IntentCheckpoint.intent_id.in_(
                    _authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)
                ),
                IntentCheckpoint.goal.ilike(f"%{needle}%"),
            )
            .order_by(IntentCheckpoint.recorded_at.desc())
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
