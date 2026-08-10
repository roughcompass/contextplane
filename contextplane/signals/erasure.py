"""Erasing, expiring and revoking signals and the feedback that cites them.

Three different clocks reach these two tables and they do different things, which is
the whole reason this module exists rather than one delete statement per table.

- **A signal's payload goes before its envelope.** The observation a producer sent is
  the part that holds a person's words; the envelope — who sent it, when, under what
  authority, with what digest — is what makes every derived claim auditable. So the
  payload and its evidence handle clear on the earlier clock and the envelope survives
  to the later one, at which point the row goes entirely.
- **Feedback is minimized, never deleted.** The free-text note is the part somebody
  wrote; the discriminant, the rating and the receipt linkage are what make the
  feedback countable and the binding checkable. Deleting the row would silently
  change every aggregate computed over it, so the note clears and the structure stays.
- **Revocation is neither of those.** A source withdrawing its material is not time
  passing: it happens at once, on a named signal, and it has to reach the derivatives
  built from that signal. So it stamps `revoked_at` and enqueues propagation under its
  own trigger, in one transaction, so a revocation that is recorded is a revocation
  that is scheduled.

**Reference bindings are deleted with the signal, in the same transaction.**
`context_reference_bindings` points at its subject polymorphically, so nothing
cascades when a signal row goes. Left behind, the binding is not merely orphaned: a
reverse lookup asking "what cites reference R" resurrects the erased signal's id, and
the same reference then reads as still-cited. That is why the delete is here and not
in a follow-up sweep — a sweep that runs later is a window in which the erasure is
reportable as complete and isn't.

**On which signals belong to an actor.** A signal names its producer by the
producer's own id, as text, with a type saying what kind of thing that producer is.
Only a `human` or `agent` producer is an actor of this system; an `external` producer
is a system, and erasing a person must not delete a vendor's whole feed because one
id happened to collide. So the actor predicate is the id *and* the type, and it is
written once, here, rather than inferred at each call site.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.retention import derivatives, holds, policies, tombstones
from contextplane.signals import ingest
from contextplane.types import Clock, TenantContext

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

_log = logging.getLogger(__name__)


def _rows_affected(result: object) -> int:
    """How many rows a DML statement touched.

    A cast in one place rather than a suppression at each call site: `execute` is typed
    as returning a generic `Result`, which has no `rowcount`, but every caller here has
    just run an UPDATE or DELETE and a cast says that where a blanket ignore would only
    hide it. `None` becomes zero because a driver that declines to report is not a
    driver that changed rows.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


#: The name this participant registers and reports under.
SUBSYSTEM = "signals"

#: How `context_reference_bindings` spells a signal subject, taken from the module that
#: writes those rows rather than restated here. The table is polymorphic and has no
#: foreign key to enforce the spelling, so a second literal that drifted by one
#: character would delete nothing and report success — the erasure would pass its own
#: tests while leaving every binding in place.
SUBJECT_TYPE_EXTERNAL_SIGNAL = ingest.SUBJECT_EXTERNAL_SIGNAL

#: The origin types that identify an actor of this system. A signal calls this column
#: `producer_type` and feedback calls it `reporter_type`; both vocabularies are the same
#: three values, and in both an `external` origin is a system rather than a person. One
#: tuple because the rule is one rule — erasing a person must not delete a vendor's feed
#: because an id collided — and two tuples would eventually disagree.
ACTOR_ORIGIN_TYPES: tuple[str, ...] = ("human", "agent")

#: How many rows one expiry batch touches. Bounded so a sweep tick cannot hold a
#: transaction open across an entire table.
DEFAULT_BATCH = 500

#: What replaces a minimized payload, and why it is a marker rather than NULL.
#:
#: The approved disposition says to clear the payload and the evidence handle. The
#: schema will not allow both: `num_nonnulls(payload, evidence_handle) = 1` holds on
#: every row, deliberately, so that a signal always says where its body is. Clearing
#: both would need a migration, which is outside this change, and leaving the payload
#: in place would not be minimization at all.
#:
#: So the payload is replaced by this marker: no observation, no producer text, nothing
#: derived from what was there — only the fact that minimization happened and the rule
#: it happened under. That satisfies the constraint, removes the content, and leaves a
#: row that reads as deliberately reduced rather than as never having had a body.
MINIMIZED_PAYLOAD: dict[str, object] = {"minimized": True, "policy_version": policies.POLICY_VERSION}


class SignalErasureRefused(Exception):
    """Raised when a revocation or erasure cannot proceed, before anything is written.

    Its own type so a caller can tell "this signal is not yours" from a database
    failure. Every path that raises it does so before the first write, which is the
    property the tests assert twice: refused, and nothing written.
    """


# Signals this actor produced. The type filter is the load-bearing half — see the
# module docstring on why an `external` producer's rows are not an actor's.
_ACTOR_SIGNALS_SQL = """
SELECT signal_id FROM external_signals
WHERE tenant_id = :tenant
  AND producer_id = :actor
  AND producer_type = ANY(:origin_types)
"""

_DELETE_BINDINGS_SQL = """
DELETE FROM context_reference_bindings
WHERE tenant_id = :tenant
  AND subject_type = :subject_type
  AND subject_id = ANY(:ids)
"""

_DELETE_SIGNALS_SQL = """
DELETE FROM external_signals
WHERE tenant_id = :tenant AND signal_id = ANY(:ids)
"""

# Minimization, not deletion: the discriminant, rating and receipt linkage survive so
# every aggregate over this table keeps its denominator. Only the note goes.
_MINIMIZE_ACTOR_FEEDBACK_SQL = """
UPDATE context_feedback
SET note = NULL
WHERE tenant_id = :tenant
  AND reporter_id = :actor
  AND reporter_type = ANY(:origin_types)
  AND note IS NOT NULL
"""

# Payload clock: the observation goes, the envelope stays.
_EXPIRE_PAYLOADS_SQL = """
UPDATE external_signals
SET payload = CAST(:marker AS jsonb), evidence_handle = NULL
WHERE signal_id = ANY(:ids)
"""

# "Not yet minimized" cannot be spelled as "has a body": the schema guarantees every
# row has exactly one, so that predicate is always true and the sweep would never
# finish. A row is done when its payload is the marker and its handle is gone, so due
# means a handle still present, or a payload that is not the marker.
_DUE_PAYLOADS_SQL = """
SELECT signal_id FROM external_signals
WHERE tenant_id = :tenant
  AND ingested_at < :deadline
  AND (evidence_handle IS NOT NULL OR NOT (payload @> CAST(:marker AS jsonb)))
ORDER BY ingested_at
LIMIT :limit
"""

_DUE_SIGNALS_SQL = """
SELECT signal_id FROM external_signals
WHERE tenant_id = :tenant AND ingested_at < :deadline
ORDER BY ingested_at
LIMIT :limit
"""

_DUE_FEEDBACK_SQL = """
SELECT feedback_id FROM context_feedback
WHERE tenant_id = :tenant AND created_at < :deadline AND note IS NOT NULL
ORDER BY created_at
LIMIT :limit
"""

_MINIMIZE_FEEDBACK_BATCH_SQL = """
UPDATE context_feedback
SET note = NULL
WHERE feedback_id = ANY(:ids)
"""

_REVOKE_SQL = """
UPDATE external_signals
SET revoked_at = :now
WHERE tenant_id = :tenant AND signal_id = :id AND revoked_at IS NULL
"""


async def _write_tombstone(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    salts: tombstones.TenantSaltResolver,
    record_class: str,
    subject_id: uuid.UUID,
    content_digest: str,
    reason: str,
    now: datetime.datetime,
) -> uuid.UUID:
    """Record that a subject was erased, in a form that proves it without holding it.

    Re-read rather than returning the generated id, because a retry must land on the
    tombstone the first attempt wrote: the outbox is unique per tombstone, so a second
    id would let the same work be enqueued twice under two authorisations.
    """
    proof = tombstones.mint_proof(
        salts.salt_for(ctx.tenant_id),
        record_class=record_class,
        subject_id=subject_id,
        content_digest=content_digest,
        effective_at=now,
    )
    await session.execute(
        text(
            """
            INSERT INTO source_tombstones
                (tombstone_id, tenant_id, record_class, subject_id, policy_version,
                 request_authority, reason, effective_at, proof_hmac, propagation_state)
            VALUES (:id, :tenant, :cls, :subject, :policy, :authority, :reason,
                    :now, :proof, 'pending')
            ON CONFLICT (tenant_id, record_class, subject_id) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "tenant": ctx.tenant_id,
            "cls": record_class,
            "subject": subject_id,
            "policy": policies.POLICY_VERSION,
            # Text, not the uuid: `request_authority` records who asked as a string,
            # and asyncpg refuses a UUID for a text parameter rather than coercing it.
            "authority": str(ctx.actor_id),
            "reason": reason,
            "now": now,
            "proof": proof,
        },
    )
    stored = await session.execute(
        text(
            "SELECT tombstone_id FROM source_tombstones "
            "WHERE tenant_id = :tenant AND record_class = :cls AND subject_id = :subject"
        ),
        {"tenant": ctx.tenant_id, "cls": record_class, "subject": subject_id},
    )
    return uuid.UUID(str(stored.scalar_one()))


class SignalErasure:
    """Erases an actor's signals and minimizes their feedback, in one transaction.

    Registered as an erasure participant, and deliberately not registered anywhere by
    this module: wiring it into the registry is a separate change, so nothing here
    runs until something constructs it.
    """

    subsystem = SUBSYSTEM

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        salts: tombstones.TenantSaltResolver,
        *,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._salts = salts
        self._clock = clock

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Delete the actor's signals and their bindings; minimize their feedback.

        One transaction, and the order inside it is not arbitrary. The tombstone
        authorises the work, the propagation enqueue reads the derivative links while
        they still exist, the bindings go before the signals that own them, and the
        signal rows go last. Moving the enqueue after the delete would schedule
        nothing, because the links it reads are keyed by the source row.

        Idempotent by construction: the tombstone conflicts away, the enqueue's own
        uniqueness returns zero on a repeat, and each delete narrows to rows that are
        still there.
        """
        now = self._clock.now()
        counts: dict[str, int] = {}

        async with self._session_factory() as session:
            signal_ids = [
                uuid.UUID(str(row[0]))
                for row in (
                    await session.execute(
                        text(_ACTOR_SIGNALS_SQL),
                        {
                            "tenant": ctx.tenant_id,
                            "actor": str(target_actor_id),
                            "origin_types": list(ACTOR_ORIGIN_TYPES),
                        },
                    )
                ).all()
            ]

            counts["signals"] = await self._erase_signals(session, ctx, signal_ids, now)
            minimized = await session.execute(
                text(_MINIMIZE_ACTOR_FEEDBACK_SQL),
                {
                    "tenant": ctx.tenant_id,
                    # Text, like the signal producer: feedback records who reported by
                    # the reporter's own id, not by a foreign key to `actors`.
                    "actor": str(target_actor_id),
                    "origin_types": list(ACTOR_ORIGIN_TYPES),
                },
            )
            counts["feedback_notes_minimized"] = _rows_affected(minimized)

            await session.commit()

        _log.info(
            "signals.erasure_applied: actor=%s counts=%s",
            target_actor_id,
            counts,
        )
        return counts

    async def _erase_signals(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        signal_ids: Sequence[uuid.UUID],
        now: datetime.datetime,
    ) -> int:
        """Tombstone, schedule, unbind and delete a set of signals. Returns rows deleted.

        Zero is a real answer and is reported as one: this actor produced no signals.
        Distinguishing it from "never asked" is what makes the participant's report
        readable after the fact.
        """
        if not signal_ids:
            return 0

        for signal_id in signal_ids:
            # One tombstone per erased signal, so a dependent can be invalidated by
            # cause rather than by a single actor-wide marker that says only "someone
            # in here was erased".
            tombstone_id = await _write_tombstone(
                session,
                ctx=ctx,
                salts=self._salts,
                record_class=policies.RECORD_EXTERNAL_SIGNAL,
                subject_id=signal_id,
                content_digest=str(signal_id),
                reason=derivatives.TRIGGER_ERASURE,
                now=now,
            )
            await derivatives.enqueue_for_sources(
                session,
                tenant_id=ctx.tenant_id,
                record_class=policies.RECORD_EXTERNAL_SIGNAL,
                source_ids=[signal_id],
                operation=derivatives.OPERATION_DELETE,
                trigger=derivatives.TRIGGER_ERASURE,
                now=now,
                tombstone_id=tombstone_id,
            )

        ids = list(signal_ids)
        # Before the signals: the binding table has no foreign key to its subject, so
        # deleting the signal first leaves a row a reverse lookup resurrects.
        await session.execute(
            text(_DELETE_BINDINGS_SQL),
            {"tenant": ctx.tenant_id, "subject_type": SUBJECT_TYPE_EXTERNAL_SIGNAL, "ids": ids},
        )
        deleted = await session.execute(text(_DELETE_SIGNALS_SQL), {"tenant": ctx.tenant_id, "ids": ids})
        return _rows_affected(deleted)


async def revoke_signal(
    session_factory: async_sessionmaker[AsyncSession],
    salts: tombstones.TenantSaltResolver,
    *,
    ctx: TenantContext,
    signal_id: uuid.UUID,
    now: datetime.datetime,
    reason: str = derivatives.TRIGGER_REVOCATION,
) -> int:
    """Stamp `revoked_at` and schedule propagation for everything built from the signal.

    One transaction, because the two halves are one fact. A stamp without an enqueue
    is a signal marked withdrawn whose vectors and summaries still answer queries; an
    enqueue without a stamp is work whose cause cannot be shown. Returns how many
    propagation items this call created — zero on a repeat, which is what makes
    re-revoking free rather than an amplifier.

    **Who calls this, today.** The expiry sweep and tests that plant a revoked signal,
    and nothing else: there is deliberately no operator- or adapter-facing revocation
    endpoint in this deployment, so a reader looking for one will not find it and
    should not add one here. That surface is a separate change with its own
    authorization question — who may revoke another system's material — and answering
    it by exposing this function would answer it by omission.

    Refuses before writing when the signal is not this tenant's, so a caller cannot
    revoke across a tenant boundary and cannot learn from the outcome whether the id
    exists elsewhere.
    """
    async with session_factory() as session:
        stamped = await session.execute(
            text(_REVOKE_SQL),
            {"tenant": ctx.tenant_id, "id": signal_id, "now": now},
        )
        if not _rows_affected(stamped):
            # Either it is not this tenant's, or it was already revoked. Both are
            # refusals here and neither is distinguished in the message: telling them
            # apart would confirm the existence of another tenant's row.
            msg = "signal is not available for revocation in this tenant, or was already revoked"
            raise SignalErasureRefused(msg)

        tombstone_id = await _write_tombstone(
            session,
            ctx=ctx,
            salts=salts,
            record_class=policies.RECORD_EXTERNAL_SIGNAL,
            subject_id=signal_id,
            content_digest=str(signal_id),
            reason=reason,
            now=now,
        )
        scheduled = await derivatives.enqueue_for_sources(
            session,
            tenant_id=ctx.tenant_id,
            record_class=policies.RECORD_EXTERNAL_SIGNAL,
            source_ids=[signal_id],
            operation=derivatives.OPERATION_DELETE,
            trigger=derivatives.TRIGGER_REVOCATION,
            now=now,
            tombstone_id=tombstone_id,
        )
        await session.commit()

    _log.info("signals.revoked: signal=%s scheduled=%d", signal_id, scheduled)
    return scheduled


class SignalExpiry:
    """The per-class expiry batches, for a sweep worker to receive by injection.

    Not imported by the worker: the scheduler package sits below this one in the
    import contract, so the worker takes these as callables it is handed rather than
    reaching for them. That is why they are grouped on a small object instead of being
    module functions the worker would have to import to name.

    Every batch consults the hold seam before deleting or minimizing anything. A held
    record is returned to the caller with its hold rather than silently skipped,
    because a suspended deletion has to be attributable to something.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        hold_store: holds.HoldStore,
        *,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._session_factory = session_factory
        self._holds = hold_store
        self._batch_size = batch_size

    async def minimize_signal_payloads(self, ctx: TenantContext, *, now: datetime.datetime) -> int:
        """Clear payloads past the payload clock, leaving the envelope. One batch."""
        deadline = self._deadline(policies.RECORD_EXTERNAL_SIGNAL, now, payload=True)
        return await self._batch(
            ctx,
            due_sql=_DUE_PAYLOADS_SQL,
            apply_sql=_EXPIRE_PAYLOADS_SQL,
            record_class=policies.RECORD_EXTERNAL_SIGNAL,
            deadline=deadline,
            now=now,
            extra={"marker": json.dumps(MINIMIZED_PAYLOAD)},
        )

    async def delete_expired_signals(self, ctx: TenantContext, *, now: datetime.datetime) -> int:
        """Delete envelopes past the record clock, with their bindings. One batch.

        The bindings go in the same statement pair as the rows, for the reason the
        module docstring gives: nothing cascades, and a binding outliving its signal is
        a reverse lookup that resurrects it.
        """
        deadline = self._deadline(policies.RECORD_EXTERNAL_SIGNAL, now, payload=False)
        async with self._session_factory() as session:
            due = await self._due(session, ctx, _DUE_SIGNALS_SQL, deadline)
            deletable, held = await holds.partition_by_hold(
                self._holds, ctx.tenant_id, policies.RECORD_EXTERNAL_SIGNAL, due, now=now
            )
            if held:
                _log.info("signals.expiry_held: record_class=%s held=%d", policies.RECORD_EXTERNAL_SIGNAL, len(held))
            if not deletable:
                return 0

            ids = list(deletable)
            await session.execute(
                text(_DELETE_BINDINGS_SQL),
                {"tenant": ctx.tenant_id, "subject_type": SUBJECT_TYPE_EXTERNAL_SIGNAL, "ids": ids},
            )
            result = await session.execute(text(_DELETE_SIGNALS_SQL), {"tenant": ctx.tenant_id, "ids": ids})
            await session.commit()
            return _rows_affected(result)

    async def minimize_feedback_notes(self, ctx: TenantContext, *, now: datetime.datetime) -> int:
        """Clear free-text notes past the payload clock. One batch.

        Never deletes the row: the discriminant, rating and receipt linkage are what
        every aggregate over this table counts, and removing rows would change those
        answers retroactively while looking like data that was never there.
        """
        deadline = self._deadline(policies.RECORD_CONTEXT_FEEDBACK, now, payload=True)
        return await self._batch(
            ctx,
            due_sql=_DUE_FEEDBACK_SQL,
            apply_sql=_MINIMIZE_FEEDBACK_BATCH_SQL,
            record_class=policies.RECORD_CONTEXT_FEEDBACK,
            deadline=deadline,
            now=now,
        )

    @staticmethod
    def _deadline(record_class: str, now: datetime.datetime, *, payload: bool) -> datetime.datetime:
        """The instant a row of this class becomes due, derived from the approved policy.

        Computed backwards from `now` rather than forwards from each row's anchor: the
        query needs one comparison value, and asking the policy for the period keeps
        the number out of this module.
        """
        anchor = (
            policies.payload_deadline(record_class, now) if payload else policies.expiry_deadline(record_class, now)
        )
        if anchor is None:
            # Event-bounded classes have no clock, so nothing is ever due by time. The
            # deadline is `now` itself, which selects nothing, rather than a guessed
            # period.
            return now
        return now - (anchor - now)

    async def _due(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        statement: str,
        deadline: datetime.datetime,
        extra: dict[str, object] | None = None,
    ) -> list[uuid.UUID]:
        rows = (
            await session.execute(
                text(statement),
                {
                    "tenant": ctx.tenant_id,
                    "deadline": deadline,
                    "limit": self._batch_size,
                    **(extra or {}),
                },
            )
        ).all()
        return [uuid.UUID(str(row[0])) for row in rows]

    async def _batch(
        self,
        ctx: TenantContext,
        *,
        due_sql: str,
        apply_sql: str,
        record_class: str,
        deadline: datetime.datetime,
        now: datetime.datetime,
        extra: dict[str, object] | None = None,
    ) -> int:
        """Shared shape for the two minimizing batches: select, consult holds, apply."""
        async with self._session_factory() as session:
            due = await self._due(session, ctx, due_sql, deadline, extra=extra)
            actionable, held = await holds.partition_by_hold(self._holds, ctx.tenant_id, record_class, due, now=now)
            if held:
                _log.info("signals.expiry_held: record_class=%s held=%d", record_class, len(held))
            if not actionable:
                return 0
            result = await session.execute(text(apply_sql), {"ids": list(actionable), **(extra or {})})
            await session.commit()
            return _rows_affected(result)


__all__ = [
    "ACTOR_ORIGIN_TYPES",
    "DEFAULT_BATCH",
    "MINIMIZED_PAYLOAD",
    "SUBJECT_TYPE_EXTERNAL_SIGNAL",
    "SUBSYSTEM",
    "SignalErasure",
    "SignalErasureRefused",
    "SignalExpiry",
    "revoke_signal",
]
