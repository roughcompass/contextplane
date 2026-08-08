"""ReviewExpiryWorker -- retires `arc_revisions` whose review date has passed.

`review_expires_at` is a governance freshness bound, not a content TTL: it
is the point past which nobody has re-attested that this revision is still
the right thing to enforce. `ArtifactService.activate` already refuses to
put a revision into force once that date has passed (re-checked at
activation rather than trusted from registration, because a revision can
sit in draft for months). This worker is the other half -- a revision that
was active and has simply outlived its review date the same way a
certificate outlives its expiry, with nobody left to renew or revoke it in
time. Nothing about its content changed; the guarantee that someone still
vouches for it did.

Expiry tombstones mandatory obligations the same way `revoke` and
`invalidate` do, and for the same reason: an obligation is a family-level
record that a mandatory directive is currently satisfied, keyed by the
directive's stable identity rather than by the revision itself. If expiry
only flipped `arc_revisions.lifecycle_state` and left the obligation
pointing at a now-expired revision, a bundle selected afterward would look
identical to one whose obligation was never mandatory in the first place --
exactly the silent unblocking every other tombstoning transition exists to
prevent. So the obligation's `current_revision_id` is cleared and its state
becomes `missing_review_expired`, leaving it standing rather than deleting
it, until an approved successor satisfies it again.

One outbox row per expired revision, not one per batch: an auditor reading
the log should be able to see exactly which revision expired and when, at
the same granularity every other lifecycle transition already gets.
Attribution follows the revision's own scope -- a tenant-scoped revision's
expiry is that tenant's event, but a global revision (`tenant_id IS NULL`)
has no tenant to attribute to, so its expiry is filed under the reserved
deployment tenant the same way every other deployment-wide ARC event is.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.artifact import LIFECYCLE_ACTIVE, LIFECYCLE_EXPIRED, OBLIGATION_MISSING_REVIEW_EXPIRED
from contextplane.audit import actions
from contextplane.types import Clock, SystemClock

_log = logging.getLogger(__name__)

#: Revisions transitioned per pass. Bounds how long one call holds row
#: locks against `arc_revisions`, the same reasoning as every other batch
#: worker in this package.
DEFAULT_LIMIT = 500


@dataclasses.dataclass(frozen=True)
class ReviewExpiryResult:
    """Outcome of one bounded pass -- what a scheduler or metrics layer reports."""

    expired_revisions: int
    tombstoned_obligations: int


class ReviewExpiryWorker:
    """Expires overdue-for-review revisions, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock. Defaults to the real UTC wall-clock when `None`.
    limit:
        Maximum revisions transitioned per `run_once()` call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._limit = limit

    async def run_once(self) -> ReviewExpiryResult:
        """Expire up to `limit` overdue active revisions in one transaction.

        Transitioning, tombstoning, and emitting all happen in the same
        transaction so a crash mid-pass leaves every revision exactly as it
        was: either fully transitioned with its obligations tombstoned and
        its audit rows queued, or still `active` and untouched. The
        predicate (`lifecycle_state = 'active' AND review_expires_at <=
        now`) is what makes a second call idempotent -- a revision this
        pass already moved to `expired` simply stops matching it.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            expired = await self._expire_batch(session, now)
            if not expired:
                return ReviewExpiryResult(expired_revisions=0, tombstoned_obligations=0)

            revision_ids: list[uuid.UUID] = [row["revision_id"] for row in expired]
            tombstoned = await self._tombstone_obligations(session, revision_ids, now)

            for row in expired:
                await self._emit_expired_event(session, row, now)

        _log.info(
            "arc_review_expiry: expired=%d tombstoned_obligations=%d now=%s",
            len(expired),
            tombstoned,
            now,
        )
        return ReviewExpiryResult(expired_revisions=len(expired), tombstoned_obligations=tombstoned)

    async def _expire_batch(self, session: AsyncSession, now: datetime.datetime) -> list[dict[str, Any]]:
        result = await session.execute(
            text(
                "WITH candidates AS ("
                "  SELECT revision_id FROM arc_revisions"
                "  WHERE lifecycle_state = :active AND review_expires_at <= :now"
                "  ORDER BY review_expires_at"
                "  LIMIT :limit"
                "  FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE arc_revisions r "
                "SET lifecycle_state = :expired "
                "FROM candidates c "
                "WHERE r.revision_id = c.revision_id "
                "RETURNING r.revision_id, r.artifact_id, r.tenant_id, r.review_expires_at"
            ),
            {"active": LIFECYCLE_ACTIVE, "expired": LIFECYCLE_EXPIRED, "now": now, "limit": self._limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _tombstone_obligations(
        self, session: AsyncSession, revision_ids: list[uuid.UUID], now: datetime.datetime
    ) -> int:
        """Advance every obligation pointing at one of these revisions.

        Mirrors `ArtifactService._tombstone_obligations` exactly -- same
        columns, same shape -- because the invariant it maintains (an
        obligation the schema requires to name a revision when
        `satisfied`, and must not once it no longer is) is identical here.
        Bulk rather than one UPDATE per revision: every row in this batch
        moves to the same state, so there is no reason to pay for N round
        trips over one.
        """
        result = await session.execute(
            text(
                "UPDATE arc_mandatory_obligations "
                "SET obligation_state = :state, current_revision_id = NULL, updated_at = :now "
                "WHERE current_revision_id = ANY(:rids)"
            ),
            {"state": OBLIGATION_MISSING_REVIEW_EXPIRED, "now": now, "rids": revision_ids},
        )
        return result.rowcount or 0  # type: ignore[attr-defined]

    async def _emit_expired_event(self, session: AsyncSession, row: dict[str, Any], now: datetime.datetime) -> None:
        payload = {
            "artifact_id": str(row["artifact_id"]),
            "revision_id": str(row["revision_id"]),
            "review_expires_at": row["review_expires_at"].isoformat(),
            "expired_at": now.isoformat(),
        }
        tenant_id = row["tenant_id"]
        if tenant_id is None:
            await audit_outbox.emit_global(session, event_type=actions.ARC_REVIEW_EXPIRED, payload=payload)
        else:
            await audit_outbox.emit(
                session, tenant_id=tenant_id, event_type=actions.ARC_REVIEW_EXPIRED, payload=payload
            )


__all__ = ["DEFAULT_LIMIT", "ReviewExpiryResult", "ReviewExpiryWorker"]
