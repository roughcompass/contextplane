"""Sweeping session events at the period their own rows already advertise.

E6-T2. `memory_session_events` carries `expires_at` on every row, written from
the tenant's `memory_retention_days`, and `ix_mse_expiry` exists specifically to
sweep on it. **Nothing swept.** The highest-volume record class in the system
advertised a retention period, carried the column and the index to enforce it,
and accumulated forever — a deployment reading its own configuration would have
believed otherwise.

## Why this reads the row instead of the policy

Every other expiry in this service computes a deadline from a class-level
`retention_days` and an anchor column. This one does not, and the difference is
the point: a session event's period is the tenant's choice *within* the class,
already resolved and stored as `expires_at` at write time. Recomputing it from
the policy would ignore the tenant's number, and recomputing it from
`memory_retention_days` would use *today's* setting to expire a row written
under a different one — silently re-dating history every time an operator
changes the number.

The class ceiling is still enforced, just earlier: `tenants.memory_retention_days`
is CHECK-constrained to 1–180, and 180 is the `retention_days` on the
`session_event` disposition. So the write path cannot produce an `expires_at`
beyond what the policy allows, and the sweep can trust the column.

## Holds still apply

A held record is not deleted, and `partition_by_hold` is what makes that true
here as everywhere else. That is most of the argument for bringing this class
into the framework rather than giving it a sweeper of its own: before this, no
legal hold could protect a session event, because nothing consulted holds on the
way to deleting one.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Final, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.retention import holds, policies
from contextplane.types import TenantContext

_log = logging.getLogger(__name__)

#: One batch. A sweep that tried to clear the whole backlog in one statement
#: would hold locks on the largest table in the system for as long as that takes;
#: the next tick continues, because the predicate is "still expired".
DEFAULT_BATCH: Final[int] = 1000

#: No `expires_at IS NOT NULL` guard, because the column is `NOT NULL`: every
#: session event carries a period, so no row can escape this sweep by having
#: none. A predicate that can never be false invites the next reader to believe
#: it can, and to wonder what a null would mean.
_DUE_SQL = """
SELECT event_id
  FROM memory_session_events
 WHERE tenant_id = :tenant
   AND expires_at <= :now
 ORDER BY expires_at
 LIMIT :limit
"""

_DELETE_SQL = """
DELETE FROM memory_session_events
 WHERE tenant_id = :tenant AND event_id = ANY(CAST(:ids AS UUID[]))
"""


class SessionEventExpiry:
    """Deletes session events past the period their rows carry."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        hold_store: holds.HoldStore,
        *,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._session_factory = session_factory
        self._holds = hold_store
        self._batch = batch

    async def delete_expired_events(self, ctx: TenantContext, *, now: datetime.datetime) -> int:
        """One batch of expired events, holds respected. Returns how many went.

        Deleted outright rather than minimized: unlike a receipt or a signal,
        a session event has no envelope worth keeping once the body is gone.
        What survives it is the claims extracted from it, which outlive the
        session by design and carry a digest of the session they came from.
        """
        async with self._session_factory() as session:
            due = [
                row.event_id
                for row in (
                    await session.execute(
                        text(_DUE_SQL),
                        {"tenant": ctx.tenant_id, "now": now, "limit": self._batch},
                    )
                ).all()
            ]
            deletable, held = await holds.partition_by_hold(
                self._holds, ctx.tenant_id, policies.RECORD_SESSION_EVENT, due, now=now
            )
            if held:
                _log.info(
                    "session_events.expiry_held: record_class=%s held=%d",
                    policies.RECORD_SESSION_EVENT,
                    len(held),
                )
            if not deletable:
                return 0

            result = await session.execute(text(_DELETE_SQL), {"tenant": ctx.tenant_id, "ids": list(deletable)})
            await session.commit()
            # A cast in one place rather than a suppression: `execute` is typed
            # as returning a generic `Result` with no `rowcount`, and this caller
            # has just run a DELETE. `signals/erasure.py` makes the same move for
            # the same reason.
            return int(cast("Any", result).rowcount or 0)
