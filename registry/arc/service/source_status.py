"""Local source-approval-status storage and the one fail-closed read every
later ARC checkpoint (submission, approval, activation, selection, every
protected-action authorization) calls before trusting a previously admitted
source.

ARC never re-derives trust from an admitted source's own signed claim after
admission — a claim's `expires_at` and the approving authority's continued
good standing are facts about the world that can change after the row was
written, and nothing about the row itself would ever notice. `arc_source_
approval_status` (created alongside admission) is the local record of what
this deployment currently believes, refreshed by `source_status_refresh.py`
on an interval capped well inside five minutes; this module owns reading it
and, eventually, writing a terminal transition into it.

**Why revocation and expiry write nothing here yet.** Recording either is
not a one-column update: it must, in the same transaction, flip this row,
revoke or expire every active revision standing on it, and write the audit
event and operational-chain record that makes the transition provable —
four things committing together or not at all. The collaborator that
performs that four-part write does not exist in this deployment yet. Rather
than write the one column now and leave the other three to a later pass
(which is exactly the partial-write hazard the atomic design exists to
rule out), `record_revocation` and `record_expiry` refuse before opening a
session at all. Once the real collaborator exists, these two methods are
where it gets wired in; nothing about the calls a caller makes today needs
to change.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.queries import source_admission as queries
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

# `arc_source_approval_status.status`'s closed vocabulary. "overdue" is
# deliberately not one of these literals -- it is never written, only
# derived at read time by comparing `next_check_at` to the caller's clock
# (see `check_status`). A status can go stale without anyone having written
# a row; overdue is what catches that.
STATUS_CURRENT = "current"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_UNKNOWN = "unknown"

#: The freshness window every fresh or refreshed row gets: `next_check_at`
#: is never more than this far past `checked_at`. Matches the ceiling the
#: admission path already seeds and the refresh worker maintains.
FRESHNESS_WINDOW = datetime.timedelta(seconds=300)


class SourceStatusUnavailable(RegistryError):
    """Unknown, overdue, expired, or revoked status (`arc_source_status_unavailable`, 409).

    One type regardless of which of the four conditions tripped -- which one
    matters for the message a caller reads, never for how it is handled: in
    every case the answer is "do not trust this source right now."
    """


class SourceOperationalIntegrityPending(RegistryError):
    """The write that would record this transition is not available yet
    (`arc_operational_integrity_pending`, 409).

    Raised before any row is touched -- see the module docstring for why a
    partial write here would be worse than refusing outright.
    """


@dataclasses.dataclass(frozen=True)
class SourceStatusView:
    """What `check_status` hands back on success -- deliberately narrow.

    A caller that needs more about the source (its digest, its verifier,
    its admitted bytes) already has its own read of `arc_source_approval_
    evidence`; this is only ever the answer to "is it still safe to trust
    this source right now."
    """

    source_evidence_id: uuid.UUID
    status: str
    checked_at: datetime.datetime
    next_check_at: datetime.datetime


class SourceStatusService:
    """Reads and (once wired) writes `arc_source_approval_status`.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock -- every freshness and expiry comparison goes
        through this rather than a bare `datetime.now()`, so a caller can
        drive the boundary deterministically in a test.
    operational_chain_appender:
        The same-transaction audit/event/outbox writer `record_revocation`
        and `record_expiry` need. `None` on every deployment today, because
        that collaborator does not exist yet; both methods refuse rather
        than guess at a partial write. Typed loosely on purpose -- this
        constructor argument exists as a seam for a collaborator whose
        shape is not this module's to invent.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        operational_chain_appender: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._operational_chain_appender = operational_chain_appender

    async def check_status(self, source_evidence_id: uuid.UUID) -> SourceStatusView:
        """Fail closed unless the row is fresh and `current`, right now.

        Freshness is checked before the stored status literal, and against
        the caller's own clock rather than trusting that the refresh worker
        already ran: a source's admission-time claim caps `next_check_at` at
        its own `expires_at` (see `source_admission.py`'s admission
        transaction), so once that deadline arrives this check starts
        failing closed on its own, on every caller, independently of
        whether the refresh worker's own attempt to record the expiry has
        landed yet. Expiry is enforced on the deadline this way, not only
        when something happens to call this method afterward, and not only
        once the worker gets to it.
        """
        async with self._session_factory() as session:
            status = await queries.load_status(session, source_evidence_id)
        if status is None:
            raise NotFoundError(f"source status for {source_evidence_id} not found")

        now = self._clock.now()
        if now >= status.next_check_at:
            raise SourceStatusUnavailable(
                f"source {source_evidence_id} status was last checked at {status.checked_at.isoformat()} "
                f"and is overdue for refresh as of {now.isoformat()}"
            )
        if status.status != STATUS_CURRENT:
            raise SourceStatusUnavailable(f"source {source_evidence_id} status is {status.status!r}, not current")

        return SourceStatusView(
            source_evidence_id=status.source_evidence_id,
            status=status.status,
            checked_at=status.checked_at,
            next_check_at=status.next_check_at,
        )

    async def record_revocation(self, source_evidence_id: uuid.UUID, *, reason_code: str) -> None:
        """Record that a source's approval has been revoked upstream.

        Refuses with `SourceOperationalIntegrityPending` on every
        deployment today -- see the module docstring. `reason_code` is
        accepted now so a caller's call site does not need to change once
        this refuses less often; it is not yet written anywhere.
        """
        self._refuse_until_appender_exists(source_evidence_id, target_status=STATUS_REVOKED)

    async def record_expiry(self, source_evidence_id: uuid.UUID) -> None:
        """Record that a source's approval has passed its own `expires_at`.

        Refuses with `SourceOperationalIntegrityPending` on every
        deployment today -- see the module docstring. `check_status`
        already fails closed on the same deadline independently of this
        method ever succeeding; this is the write that would make the
        deadline durable in `arc_source_approval_status` itself, and it is
        not available yet.
        """
        self._refuse_until_appender_exists(source_evidence_id, target_status=STATUS_EXPIRED)

    def _refuse_until_appender_exists(self, source_evidence_id: uuid.UUID, *, target_status: str) -> None:
        """The one guard both write methods share -- raises before either
        opens a session, so "refused" and "touched nothing" are the same
        fact rather than two things a caller has to trust line up."""
        if self._operational_chain_appender is None:
            raise SourceOperationalIntegrityPending(
                f"recording source {source_evidence_id} as {target_status!r} requires the same-transaction "
                "audit/event/outbox writer this deployment has not wired yet"
            )


__all__ = [
    "FRESHNESS_WINDOW",
    "STATUS_CURRENT",
    "STATUS_EXPIRED",
    "STATUS_REVOKED",
    "STATUS_UNKNOWN",
    "SourceOperationalIntegrityPending",
    "SourceStatusService",
    "SourceStatusUnavailable",
    "SourceStatusView",
]
