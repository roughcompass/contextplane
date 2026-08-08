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
and writing a terminal transition into it.

**Why revocation and expiry write nothing here without an injected
appender.** Recording either is not a one-column update: it must, in the
same transaction, flip this row, revoke or expire every active revision
standing on it, and write the audit event and operational-chain record that
makes the transition provable — four things committing together or not at
all. `record_revocation`/`record_expiry` refuse before opening a session at
all when no `OperationalChainService` is injected, exactly as they did
before that collaborator existed — "refused" and "touched nothing" stay the
same fact rather than two things a caller has to trust line up. Once one is
injected (see `_record_terminal_transition` below), the four-part write
runs for real, in one transaction, on the caller-visible methods below.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.operational_chain import (
    EVENT_FRESHNESS_DOWNGRADED,
    SYSTEM_ACTOR,
    OperationalChainService,
    build_event_payload,
)
from contextplane.arc.service.queries import source_admission as queries
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, RegistryError
from contextplane.types import Clock

# `arc_revisions.lifecycle_state` targets the cascade moves a dependent
# revision to. Duplicated from `artifact_integrity.py`'s own
# `LIFECYCLE_REVOKED`/`LIFECYCLE_EXPIRED` literals rather than imported:
# that module is the base of the artifact-service import graph and this
# service does not otherwise depend on it, so importing two string
# constants across that boundary would cost more coupling than the
# duplication it avoids -- the same reasoning `submission.py`'s own
# `_materialisation_scope` gives for not importing `proposal.py`'s
# equivalent helper.
_LIFECYCLE_REVOKED = "revoked"
_LIFECYCLE_EXPIRED = "expired"

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
        The same-transaction operational-event/checkpoint writer
        `record_revocation` and `record_expiry` need to make their four-part
        write atomic. `None` on a deployment that has not wired one; both
        methods refuse rather than guess at a partial write. See both
        methods and `_record_terminal_transition` for the write itself.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        operational_chain_appender: OperationalChainService | None = None,
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

        Refuses with `SourceOperationalIntegrityPending` before opening a
        session if no operational-chain appender is injected -- see the
        module docstring. Otherwise runs the full four-part transaction:
        flip this source's status to `revoked`, cascade every dependent
        `active` revision to `revoked`, write one audit event naming them,
        and append one `freshness_downgraded` operational event per
        cascaded revision -- all committing together or not at all.
        """
        await self._record_terminal_transition(
            source_evidence_id, target_status=STATUS_REVOKED, reason_code=reason_code
        )

    async def record_expiry(self, source_evidence_id: uuid.UUID) -> None:
        """Record that a source's approval has passed its own `expires_at`.

        Same shape as `record_revocation`, targeting `expired` instead of
        `revoked`. `check_status` already fails closed on the same deadline
        independently of this method ever running; this is the write that
        makes the deadline durable in `arc_source_approval_status` and
        cascades it to dependent revisions.
        """
        await self._record_terminal_transition(source_evidence_id, target_status=STATUS_EXPIRED, reason_code=None)

    async def _record_terminal_transition(
        self, source_evidence_id: uuid.UUID, *, target_status: str, reason_code: str | None
    ) -> None:
        """The one four-part write both public methods share.

        Raises before opening a session when no appender is injected --
        "refused" and "touched nothing" stay the same fact. Once an
        appender exists, everything below runs in one transaction: the
        status flip is itself a compare-and-swap
        (`queries.mark_status_terminal`) guarded on the row not already
        being `revoked`/`expired`, so a call that loses that race (or
        re-fires against an already-terminal row) does nothing further --
        no double cascade, no duplicate audit row, no second operational
        event.
        """
        if self._operational_chain_appender is None:
            raise SourceOperationalIntegrityPending(
                f"recording source {source_evidence_id} as {target_status!r} requires the same-transaction "
                "operational-chain writer this deployment has not wired yet"
            )
        appender = self._operational_chain_appender

        now = self._clock.now()
        lifecycle_state = _LIFECYCLE_REVOKED if target_status == STATUS_REVOKED else _LIFECYCLE_EXPIRED
        authority_digest = _authority_evidence_digest(
            source_evidence_id=source_evidence_id, target_status=target_status, reason_code=reason_code, now=now
        )

        async with self._session_factory() as session, session.begin():
            applied = await queries.mark_status_terminal(
                session,
                source_evidence_id=source_evidence_id,
                status=target_status,
                checked_at=now,
                next_check_at=now + FRESHNESS_WINDOW,
            )
            if not applied:
                # Already `revoked`/`expired` (a genuine retry, or this
                # call losing a race to another one targeting the same
                # row) -- the cascade already ran, or never gets to, on
                # whichever call actually flipped the row.
                return

            evidence = await queries.load_evidence(session, source_evidence_id)
            if evidence is None:
                msg = f"source status for {source_evidence_id} exists but its evidence row does not"
                raise NotFoundError(msg)

            dependents = await queries.find_active_revisions_by_source(session, source_evidence_id)
            for dependent in dependents:
                await queries.revoke_or_expire_revision(
                    session, revision_id=dependent.revision_id, lifecycle_state=lifecycle_state, now=now
                )
                await appender.append_event(
                    session,
                    artifact_id=dependent.artifact_id,
                    revision_id=dependent.revision_id,
                    event_type=EVENT_FRESHNESS_DOWNGRADED,
                    actor=SYSTEM_ACTOR,
                    payload=build_event_payload(
                        initial_freshness_basis="revision_pinned_only",
                        reason_code=reason_code or f"source_{target_status}",
                        authority_evidence_digest=authority_digest,
                    ),
                    authorization_decision_reference=f"source_status:{target_status}:{source_evidence_id}",
                    authority_evidence_digest=authority_digest,
                    idempotency_key=f"source-status-{target_status}-{source_evidence_id}-{dependent.revision_id}",
                )

            audit_event_type = (
                actions.ARC_SOURCE_STATUS_REVOKED
                if target_status == STATUS_REVOKED
                else actions.ARC_SOURCE_STATUS_EXPIRED
            )
            audit_payload = {
                "source_evidence_id": str(source_evidence_id),
                "reason_code": reason_code,
                "cascaded_revision_ids": [str(d.revision_id) for d in dependents],
            }
            # Filed under the source's own tenant, matching `audit_outbox`'s
            # own documented rule: a tenant-scoped source's revocation is
            # that tenant's business, and filing it under the deployment-
            # global sentinel instead would both mislead that tenant's
            # auditor and hide the event from the one auditor who should
            # see it. Only a genuinely global source (`tenant_id IS NULL`)
            # uses `emit_global`.
            if evidence.tenant_id is not None:
                await audit_outbox.emit(
                    session, tenant_id=evidence.tenant_id, event_type=audit_event_type, payload=audit_payload
                )
            else:
                await audit_outbox.emit_global(session, event_type=audit_event_type, payload=audit_payload)


def _authority_evidence_digest(
    *, source_evidence_id: uuid.UUID, target_status: str, reason_code: str | None, now: datetime.datetime
) -> str:
    """The digest a cascaded `freshness_downgraded` event's top-level
    `authority_evidence_digest` names -- what actually justified the
    downgrade.

    There is no separate evidence row this transition points at (unlike
    genesis, whose authority evidence is the artifact-semantics digest
    already durable on the proposal): the source-status determination
    itself, at the moment of the transition, *is* the evidence. This digest
    is exactly that determination, reproducible by anyone who knows the
    same four facts -- not a random value, and not read back from anywhere,
    so a caller inspecting the operational event and the audit row this
    method's caller wrote alongside it can independently recompute it.
    """
    encoded = json.dumps(
        {
            "source_evidence_id": str(source_evidence_id),
            "target_status": target_status,
            "reason_code": reason_code,
            "at": now.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
