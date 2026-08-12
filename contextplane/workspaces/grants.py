"""Who may participate in a task, and who may decide that.

The queries beneath this module already put the audience predicate in the SQL.
What was missing was an owner for the *writes*: without one, the only way to
grant or revoke was for a caller to open its own session, which puts transaction
boundaries and the authorization decision in whichever surface happened to need
them first. Two surfaces would then have two answers about who may grant.

**Granting is itself a governed act.** An actor who may read a task is not
thereby an actor who may widen its audience. Only an owner may grant or revoke,
and that check runs here rather than in a router, because REST and MCP both need
it and a rule enforced twice is a rule that will eventually be enforced
differently.

**Revocation is temporal, never a delete.** The row stays and stops applying. A
deleted grant erases the fact that the actor ever had access, which is exactly
the fact an audit of a past read needs.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from contextplane.workspaces import queries_audience as queries
from contextplane.workspaces.audience import AudienceDenied
from contextplane.workspaces.schemas.intent_memory import (
    ROLE_OWNER,
    IntentParticipantGrantV1,
    ParticipantRole,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext


class IntentGrantService:
    """Grant reads and writes for one deployment, and the rule about who may make them."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- authorization ---------------------------------------------------

    async def _require_owner(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        intent_id: uuid.UUID,
        actor_id: str,
        moment: datetime.datetime,
    ) -> None:
        """Refuse anyone but an owner, without saying whether the task exists.

        The refusal is deliberately the same for "you are not an owner" and "you
        are not a participant at all". Distinguishing them would let anyone
        enumerate which task ids exist by watching which refusal came back, and
        a task id is not public.
        """
        role = await queries.fetch_actor_role(
            session, tenant_id=tenant_id, intent_id=intent_id, actor_id=actor_id, moment=moment
        )
        if role != ROLE_OWNER:
            raise AudienceDenied(
                "only an owner may change a task's participants; reading a task does not confer the "
                "right to widen its audience"
            )

    # -- reads -----------------------------------------------------------

    async def list_grants(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
    ) -> tuple[IntentParticipantGrantV1, ...]:
        """Every grant on one task, active or not, for a participant of it.

        Expired grants are included: an audit of a past read needs the grants
        that applied then, and hiding them would make a revoked participant look
        like one who was never there.

        Any active participant may read the list. Knowing who else is on a task
        you are already on is not a widening of anyone's access, and hiding it
        makes coordination guesswork.
        """
        moment = self._clock.now()
        async with self._session_factory() as session:
            role = await queries.fetch_actor_role(
                session, tenant_id=ctx.tenant_id, intent_id=intent_id, actor_id=str(ctx.actor_id), moment=moment
            )
            if role is None:
                raise AudienceDenied("no active participant grant for this actor on this task")
            return tuple(await queries.fetch_task_grants(session, tenant_id=ctx.tenant_id, intent_id=intent_id))

    async def role_for(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
        actor_id: str | None = None,
    ) -> ParticipantRole | None:
        """One actor's active role, or `None`. Defaults to the calling actor."""
        moment = self._clock.now()
        async with self._session_factory() as session:
            return await queries.fetch_actor_role(
                session,
                tenant_id=ctx.tenant_id,
                intent_id=intent_id,
                actor_id=actor_id or str(ctx.actor_id),
                moment=moment,
            )

    async def assert_participant(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
    ) -> ParticipantRole:
        """Refuse a caller who is not an active participant, and say what they are.

        Published because `IntentCheckpointService` does not enforce the audience
        itself -- it neither authorizes an append nor filters a read -- so
        without this a surface over it would let any actor in the tenant write
        into, and read, any task's chain. The durable fix is the audience
        predicate inside those queries, the way `queries_audience` already does
        it for grants; until that lands this is the one place both transports
        can share the check rather than each remembering it.
        """
        role = await self.role_for(ctx, intent_id=intent_id)
        if role is None:
            raise AudienceDenied("no active participant grant for this actor on this task")
        return role

    # -- writes ----------------------------------------------------------

    async def grant(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
        actor_id: str,
        role: ParticipantRole,
        expires_at: datetime.datetime | None = None,
        resolver_version: str = "explicit/v1",
    ) -> IntentParticipantGrantV1:
        """Add one participant to a task.

        The grant object is built here, from the server's clock and the calling
        actor's identity, so a caller cannot backdate its own grant or attribute
        one to somebody else. `granted_by` is the caller, always.

        The contract object refuses a self-grant, which is the check that stops
        an owner quietly re-granting themselves a wider role than the one they
        hold.
        """
        moment = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await self._require_owner(
                session, tenant_id=ctx.tenant_id, intent_id=intent_id, actor_id=str(ctx.actor_id), moment=moment
            )
            record = IntentParticipantGrantV1(
                intent_id=intent_id,
                actor_id=actor_id,
                role=role,
                granted_by=str(ctx.actor_id),
                granted_at=moment,
                expires_at=expires_at,
                resolver_version=resolver_version,
            )
            await queries.insert_grant(session, tenant_id=ctx.tenant_id, grant=record)
            return record

    async def revoke(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
        actor_id: str,
    ) -> bool:
        """End one participant's access now. Returns whether anything changed.

        Idempotent by construction: a grant already ended earlier is left alone,
        so revoking twice does not extend the window that was already closed and
        the second call is not an error.
        """
        moment = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await self._require_owner(
                session, tenant_id=ctx.tenant_id, intent_id=intent_id, actor_id=str(ctx.actor_id), moment=moment
            )
            return await queries.revoke_grant(
                session, tenant_id=ctx.tenant_id, intent_id=intent_id, actor_id=actor_id, moment=moment
            )


__all__ = ["IntentGrantService"]
