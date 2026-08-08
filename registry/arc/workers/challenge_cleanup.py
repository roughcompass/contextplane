"""ChallengeCleanupWorker -- purges unconsumed `arc_context_challenges` long past expiry.

A challenge's usable window is minutes, not hours -- see `CHALLENGE_TTL` in
`registry.arc.service.challenge`. Once `expires_at` passes, the row is
already useless for its original purpose: `validate_challenge` refuses an
expired challenge outright, and a consumed one is refused for a different
reason entirely. Deleting the instant a challenge expires would be the
obvious move, and the wrong one -- an operator investigating why a host's
challenge went unconsumed (a crashed client, a network partition, a bug in
retry logic) needs the row to still be there afterward, not just until its
five-minute window closed. `RETENTION_AFTER_EXPIRY` is that investigation
window: long enough to debug, short enough that abandoned challenges do not
accumulate forever.

Consumed challenges are never candidates here, at the query level -- not
because a later step checks and refuses, but because `consumed_at IS NULL`
is part of the WHERE clause itself. A consumed challenge is single-use
evidence a receipt already points at (`arc_receipts.challenge_id` is a real
foreign key, and a deferred constraint trigger enforces that every consumed
challenge has exactly one receipt referencing it); deleting one out from
under its receipt is not a race this worker should ever get close enough to
lose. Filtering it out of the candidate set is what makes that true by
construction rather than by hoping the database rejects the attempt.

A purge of exactly the rows that were never consumed is also a purge of the
one place "this challenge was requested and nobody ever came back to use
it" was still visible. Every pass that deletes at least one row writes a
single outbox event recording how many and as of what cutoff, one row per
pass rather than one per challenge -- an hourly pass bounded at
`DEFAULT_LIMIT` deleted rows is still bounded audit volume, and no
individual purged challenge carries anything worth naming beyond its
tenant, which `arc.challenge.issued` already recorded at request time. A
pass that deletes nothing writes nothing: an empty pass is the healthy
case, and an hourly audit row saying so forever would just be noise an
operator has to filter back out.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.audit import actions
from registry.types import Clock, SystemClock

_log = logging.getLogger(__name__)

#: Rows deleted per pass. Bounds how long one call holds row locks against
#: `arc_context_challenges`, the same reasoning as every other batch worker
#: in this package.
DEFAULT_LIMIT = 1000

#: How long an expired, unconsumed challenge survives before it is eligible
#: for deletion. See the module docstring for why this is not zero.
RETENTION_AFTER_EXPIRY = datetime.timedelta(hours=24)


@dataclasses.dataclass(frozen=True)
class CleanupResult:
    """Outcome of one bounded pass -- what a scheduler or metrics layer reports."""

    deleted: int


class ChallengeCleanupWorker:
    """Deletes unconsumed, long-expired challenges, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock. Defaults to the real UTC wall-clock when `None`.
    limit:
        Maximum rows deleted per `run_once()` call.
    retention:
        How long past `expires_at` an unconsumed challenge is kept before
        it becomes eligible for deletion. Defaults to
        `RETENTION_AFTER_EXPIRY`; overridable per deployment.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        retention: datetime.timedelta = RETENTION_AFTER_EXPIRY,
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._limit = limit
        self._retention = retention

    async def run_once(self) -> CleanupResult:
        """Delete up to `limit` eligible challenges in one transaction.

        Eligible means unconsumed and expired more than `retention` ago --
        both conditions are in the claim CTE itself, so a consumed row is
        never a candidate the DELETE below has to reject; it is simply not
        selected. `FOR UPDATE SKIP LOCKED` on the candidate set means a
        concurrent cleanup pass (or any other transaction holding one of
        these rows) is skipped rather than blocked on.

        The audit row -- one per pass, not one per deleted challenge -- is
        written in the same transaction as the DELETE, so a crash between
        the two cannot leave a purge with no trace of it. Deployment-scoped
        rather than filed under any one tenant: the DELETE above matches
        every tenant in a single statement, and there is no per-tenant
        breakdown left to attribute once the rows themselves are gone.
        """
        now = self._clock.now()
        cutoff = now - self._retention
        async with self._session_factory() as session, session.begin():
            deleted_ids = await self._delete_batch(session, cutoff)
            if deleted_ids:
                await self._emit_expired_event(session, deleted=len(deleted_ids), cutoff=cutoff, now=now)

        _log.info(
            "arc_challenge_cleanup: deleted=%d cutoff=%s",
            len(deleted_ids),
            cutoff,
        )
        return CleanupResult(deleted=len(deleted_ids))

    async def _emit_expired_event(
        self, session: AsyncSession, *, deleted: int, cutoff: datetime.datetime, now: datetime.datetime
    ) -> None:
        """Record what one pass purged: how many, and as of what cutoff.

        Counts only, the same reasoning `ApprovalTrustService`'s cascade
        audit row uses for a write that can touch an unbounded number of
        rows: naming every purged challenge individually would make a
        single busy pass a multi-kilobyte payload for no operator benefit
        the count does not already give them.
        """
        await audit_outbox.emit_global(
            session,
            event_type=actions.ARC_CHALLENGE_EXPIRED,
            payload={
                "deleted_count": deleted,
                "cutoff": cutoff.isoformat(),
                "run_at": now.isoformat(),
            },
        )

    async def _delete_batch(self, session: AsyncSession, cutoff: datetime.datetime) -> list[object]:
        result = await session.execute(
            text(
                "WITH candidates AS ("
                "  SELECT challenge_id FROM arc_context_challenges"
                "  WHERE consumed_at IS NULL AND expires_at < :cutoff"
                "  ORDER BY expires_at"
                "  LIMIT :limit"
                "  FOR UPDATE SKIP LOCKED"
                ") "
                "DELETE FROM arc_context_challenges "
                "USING candidates "
                "WHERE arc_context_challenges.challenge_id = candidates.challenge_id "
                "RETURNING arc_context_challenges.challenge_id"
            ),
            {"cutoff": cutoff, "limit": self._limit},
        )
        return list(result.scalars().all())


__all__ = ["DEFAULT_LIMIT", "RETENTION_AFTER_EXPIRY", "ChallengeCleanupWorker", "CleanupResult"]
