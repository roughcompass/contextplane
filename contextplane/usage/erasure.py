"""The usage subsystem's participation in erasing an actor.

Raw usage rows name the actor who made the call, so they are personal data and a
right-to-be-forgotten request has to reach them. The aggregates do not, and this
is the one participant whose most important property is what it leaves alone.

**Rollups are deliberately untouched.** They carry distinct-actor counts and no
actor identifier, so they are not this person's data — and rewriting them would
let an erasure request silently change a total someone has already reported.
A number quoted for a closed month must stay the number quoted for that month.
That is also what makes the retention boundary affordable: raw rows can be
deleted, on request or on schedule, without losing the answers.

**No `SKIP LOCKED`, unlike the retention sweep.** Skipping a locked row is
correct for expiry, which will come round again in an hour. Here it would report
a completed erasure while leaving rows behind, which is the exact failure the
erasure registry exists to prevent. This waits for the lock instead.

**Batched, because this is the highest-volume table in the system.** One row per
API call means a busy service account can accumulate millions inside the
retention window, and erasing them in a single transaction would hold locks on a
partitioned table for as long as it took. Batches commit independently and the
loop runs until nothing is left — no ceiling, because a truncated erasure that
reported success would be the same silent failure as skipping a locked row.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.types import TenantContext
from contextplane.usage.writer import UsageWriter

__all__ = ["UsageErasure"]

_log = logging.getLogger(__name__)

#: Rows per committed batch. Matches the retention sweep — the rows are narrow and
#: the delete does no index maintenance worth speaking of.
_BATCH_SIZE = 5000


class UsageErasure:
    """Deletes one actor's raw usage rows, within one tenant, and nothing else."""

    subsystem = "usage"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        writer: UsageWriter | None = None,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._writer = writer
        self._batch_size = batch_size

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Remove the actor's usage rows and report what went, per source.

        Scoped to the requesting tenant. An actor id is unique across the system, so
        an unscoped delete would work — and would also let one tenant's
        administrator erase rows recording calls made in another tenant, which is a
        cross-tenant write dressed up as a compliance action.

        The queued events go first. They are the ones that would otherwise flush
        after the delete and put the actor back.
        """
        discarded = 0
        if self._writer is not None:
            discarded = self._writer.discard_actor(ctx.tenant_id, target_actor_id)

        deleted = 0
        while True:
            batch = await self._delete_batch(ctx.tenant_id, target_actor_id)
            if batch == 0:
                break
            deleted += batch

        _log.info(
            "erasure.usage: tenant=%s actor=%s deleted=%d discarded_from_buffer=%d",
            ctx.tenant_id,
            target_actor_id,
            deleted,
            discarded,
        )
        # Per source rather than one total: a receipt saying "12" cannot be checked
        # against anything, and the buffer is a second place the rows lived.
        return {"usage_events": deleted, "usage_events_buffered": discarded}

    async def _delete_batch(self, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> int:
        """One bounded, independently committed delete.

        Scoped by primary key from a subquery because Postgres does not accept a
        `LIMIT` on `DELETE` directly. `FOR UPDATE` without `SKIP LOCKED`: waiting is
        slower than skipping and is the only option that cannot end early with rows
        still there.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "DELETE FROM usage_events "
                    "WHERE (event_id, occurred_at) IN ("
                    "  SELECT event_id, occurred_at FROM usage_events "
                    "   WHERE tenant_id = :tenant AND actor_id = :actor "
                    "   LIMIT :limit FOR UPDATE"
                    ")"
                ),
                {"tenant": tenant_id, "actor": actor_id, "limit": self._batch_size},
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]
