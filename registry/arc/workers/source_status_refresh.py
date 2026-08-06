"""SourceStatusRefreshWorker -- keeps `arc_source_approval_status` from going
stale, and is the one process that notices an upstream revocation or an
approaching expiry deadline without anyone having to ask.

Two independent things can make an admitted source's approval stop being
trustworthy after it was admitted: the approving authority revokes it, or the
claim's own `expires_at` deadline arrives. Nothing about either event touches
the row on its own -- an admitted claim is a fact about the past, and
`arc_source_approval_status` is this deployment's current belief, which only
a periodic check keeps current. This worker is that periodic check: each
pass looks at every row whose freshness window has lapsed, asks whatever
configured connector or verifier provider admitted it whether the approval
still stands, and separately compares the claim's own deadline against the
clock. A still-good row just gets its freshness window pushed out; a revoked
or deadline-passed row is handed to `SourceStatusService`, which is where
the actual state transition (and the cascade to whatever depends on it)
lives.

Real connector/provider HTTP integration is out of scope this phase -- see
`RemoteSourceStatusProvider`'s own docstring -- so every deployment today
gets `_AlwaysCurrentProvider`, and the only transition this worker can
currently drive end to end is the deadline one, which needs no external
call at all.

**Why the due-set read carries no row lock.** Every other bounded worker in
this package claims its batch with `FOR UPDATE SKIP LOCKED` and holds that
lock for the whole pass, because its own mutation has to happen inside that
same lock to avoid double-processing. This worker's mutation
(`update_status_refresh`) is instead its own conditional compare-and-swap,
so a lock held across the batch would buy nothing -- and it would cost
something real: `record_revocation`/`record_expiry` are meant to eventually
open their own transaction against the very row this pass would still be
holding open, which is exactly the shape of a self-deadlock. Reading the due
set as a plain, unlocked `SELECT` and mutating (or delegating) one row at a
time is what keeps that door closed regardless of what those two methods
grow into.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.queries import source_admission as queries
from registry.arc.service.source_status import (
    FRESHNESS_WINDOW,
    SourceOperationalIntegrityPending,
    SourceStatusService,
)
from registry.types import Clock, SystemClock

_log = logging.getLogger(__name__)

#: Rows checked per pass. Bounds how much work one `run_once()` call does
#: regardless of how large the due backlog is -- the scheduler's interval
#: decides how often the rest gets picked up, the same reasoning as every
#: other bounded worker in this package.
DEFAULT_LIMIT = 500


@dataclasses.dataclass(frozen=True)
class RemoteStatusCheck:
    """What a `RemoteSourceStatusProvider` reports for one source.

    Deliberately just a revocation flag: expiry is this deployment's own
    claimed deadline, never something an upstream system needs to tell it,
    and "unknown" (the fourth status literal) has no writer yet either --
    see `source_status.py`'s vocabulary for why it stays fail-closed on the
    read side regardless of how a row would come to carry it.
    """

    revoked: bool
    reason_code: str | None = None


class RemoteSourceStatusProvider(Protocol):
    """Where a due row's upstream approval status is actually checked.

    The real implementation would call the admitting connector's or
    verifier's own revocation-status endpoint; wiring one is out of scope
    this phase (see the module docstring), so every deployment today
    constructs this worker with `_AlwaysCurrentProvider`. Tests substitute a
    provider that reports a revocation on command -- there is no other
    honest way to exercise that branch without a real upstream to revoke
    against.
    """

    async def check(
        self,
        *,
        source_evidence_id: uuid.UUID,
        verifier_id: str,
        connector_id: str | None,
        policy_id: str | None,
    ) -> RemoteStatusCheck: ...


class _AlwaysCurrentProvider:
    """The only provider any deployment gets today. Never reports a
    revocation, because there is no real connector/provider integration
    behind it yet to ask."""

    async def check(
        self,
        *,
        source_evidence_id: uuid.UUID,
        verifier_id: str,
        connector_id: str | None,
        policy_id: str | None,
    ) -> RemoteStatusCheck:
        return RemoteStatusCheck(revoked=False)


@dataclasses.dataclass(frozen=True)
class SourceStatusRefreshResult:
    """Outcome of one bounded pass -- what a scheduler or metrics layer reports.

    `refreshed` counts rows whose freshness window was actually pushed out.
    `integrity_pending` counts rows this pass correctly identified as
    revoked or past their expiry deadline but could not yet record --
    `source_status.py`'s own module docstring explains why that refusal is
    the safe outcome today rather than a bug. `failed` counts rows a
    provider call or an unexpected error knocked out of this pass without
    knocking out any other row in it.
    """

    due: int
    refreshed: int
    integrity_pending: int
    failed: int


class SourceStatusRefreshWorker:
    """Refreshes `arc_source_approval_status`, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    service:
        The `SourceStatusService` this worker hands revocation/expiry
        transitions to. Sharing one instance with the rest of the
        deployment (rather than constructing a second one here) means both
        see the same operational-chain appender wiring the moment it
        exists.
    clock:
        Injectable clock. Defaults to the real UTC wall-clock when `None`.
    limit:
        Maximum rows checked per `run_once()` call.
    remote_provider:
        Where a due row's upstream status is checked. Defaults to
        `_AlwaysCurrentProvider`; tests substitute one that reports
        revocation on command.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        service: SourceStatusService,
        clock: Clock | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        remote_provider: RemoteSourceStatusProvider | None = None,
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._service = service
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._limit = limit
        self._remote_provider: RemoteSourceStatusProvider = remote_provider or _AlwaysCurrentProvider()

    async def run_once(self) -> SourceStatusRefreshResult:
        """Check up to `limit` due rows, each independently.

        Every row is its own `try`/`except`: a provider call that raises,
        or any other unexpected failure, is logged and counted rather than
        allowed to abort the rows still waiting behind it -- one poisoned
        row costs this pass one batch slot, never the whole pass. Re-
        running immediately over the same due set changes nothing further,
        because `update_status_refresh`'s own compare-and-swap guard (and,
        for a row this pass could not yet record, the fact that nothing was
        written at all) leaves a just-processed row no longer matching the
        due predicate or exactly as it was.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            due_ids = await queries.select_due_for_refresh(session, now=now, limit=self._limit)

        refreshed = 0
        integrity_pending = 0
        failed = 0
        for source_evidence_id in due_ids:
            try:
                if await self._refresh_one(source_evidence_id, now):
                    refreshed += 1
            except SourceOperationalIntegrityPending:
                integrity_pending += 1
            except Exception as exc:  # noqa: BLE001 - one row's failure must not abort the batch
                _log.warning("arc_source_status_refresh: failed for %s: %s", source_evidence_id, exc)
                failed += 1

        result = SourceStatusRefreshResult(
            due=len(due_ids), refreshed=refreshed, integrity_pending=integrity_pending, failed=failed
        )
        if result.due:
            _log.info(
                "arc_source_status_refresh: due=%d refreshed=%d integrity_pending=%d failed=%d",
                result.due,
                result.refreshed,
                result.integrity_pending,
                result.failed,
            )
        return result

    async def _refresh_one(self, source_evidence_id: uuid.UUID, now: datetime.datetime) -> bool:
        """Decide and act on one due row. Returns whether it was refreshed.

        Order matters: an upstream revocation is checked first because it
        can arrive before a claim's own deadline does, and either outcome
        hands off to `SourceStatusService` rather than writing `status`
        here directly -- this module never writes that column itself.
        """
        async with self._session_factory() as session:
            evidence = await queries.load_evidence(session, source_evidence_id)
        if evidence is None:
            # The insert order admission uses guarantees every status row has
            # an evidence sibling; a miss here means this row is not
            # explainable by that invariant, not that it is merely stale.
            # Skip rather than crash the batch over it -- an operator can
            # still find it by re-running with logging turned up.
            return False

        remote = await self._remote_provider.check(
            source_evidence_id=source_evidence_id,
            verifier_id=evidence.verifier_id,
            connector_id=evidence.connector_id,
            policy_id=evidence.policy_id,
        )
        if remote.revoked:
            await self._service.record_revocation(
                source_evidence_id, reason_code=remote.reason_code or "upstream_revoked"
            )
            return False  # pragma: no cover - unreachable while record_revocation always refuses

        if now >= evidence.expires_at:
            await self._service.record_expiry(source_evidence_id)
            return False  # pragma: no cover - unreachable while record_expiry always refuses

        next_check_at = min(now + FRESHNESS_WINDOW, evidence.expires_at)
        async with self._session_factory() as session, session.begin():
            applied = await queries.update_status_refresh(
                session, source_evidence_id=source_evidence_id, checked_at=now, next_check_at=next_check_at
            )
        return applied


__all__ = [
    "DEFAULT_LIMIT",
    "RemoteSourceStatusProvider",
    "RemoteStatusCheck",
    "SourceStatusRefreshResult",
    "SourceStatusRefreshWorker",
]
