"""Admitting one external observation, once, under the authority its source declared.

One contract for every producer -- a human reporting an outcome, an agent
reporting one, an external system reporting its own run -- because a second
ingestion path is a second place the rules can differ, and the rule that differs
is the one nobody re-reads.

**What a producer sends, and what identifies it, live in `signals/envelope.py`.**
That module is everything decidable from the submission alone -- the shape, the
size bounds, the normalization, the content digest, and the refusal of the fields
this service assigns for itself. It knows nothing about sources, ceilings or
storage, which is what keeps the digest reproducible by anyone holding the same
bytes. This module supplies the three things it deliberately cannot: the
ingestion time from one clock read, the authority read off the source's declared
policy, and the signal id.

**An unregistered source may not write, and neither may a registered one that
belongs to somebody else.** Both answer identically -- the same status, the same
message -- because a distinguishable refusal turns a source id into a
cross-tenant existence oracle. The ownership check runs *before* the ceiling
check, deliberately: admitting against another tenant's declared window would
let one tenant spend a second tenant's ingest budget and read the answer off the
breaker.

**The bound enforced here is the source's own declared ceiling.** The ceiling and
circuit breaker are the ones the source declared when it registered, so a runaway
producer stops mattering without an operator. The per-submission size limits are
a different failure and are decided by the envelope, because a single submission
large enough to matter is not stopped by a count-based ceiling at all.

**Replay is decided by content, not by arrival.** The envelope's digest is what
decides it; what this module adds is that both keys the ledger enforces are
honoured, so the same external occurrence resubmitted under a fresh submission
key finds the stored row rather than creating a second one -- "the same thing
happened" and "the same request was sent" are different questions and the
producer controls only the second.

**A signal's references reach storage, not only identity.** The normalized
references the envelope produced are bound to the stored signal through the
junction every subject shares, so "which references did signal X carry" is
answered from rows rather than by re-reading whatever shape that producer's
payload happened to use. The ledger still carries no reference columns of its own
-- the binding is beside the identity, not instead of it, and the collision key
still comes back to the caller so a producer can correlate.

**Every admission decision is audited, and none of them carries the observation.**
A submission stored and a submission recognised as already stored are both
`signal.ingested`, told apart by an `outcome` field, because an auditor counting
how often a source retries needs the two in one series. A refusal is
`signal.rejected` against the *source*, carrying which of a closed set of reason
classes fired -- a refusal leaves no signal row for the first action to point at,
and "what did this source record" and "what is it being turned away for" are
different questions. Neither line contains the payload, the evidence handle, or
the producer id: the audit log is the one table guaranteed to be retained and
read, so anything put here escapes every retention and classification decision
made about the ledger row itself. A content refusal carries a *digest* rather
than content, and is refused before anything here is decided -- the replay lookup
included. That floor lives in `signals/admission.py`, which says why it sits
where it does.

**Nothing here concludes anything.** The row records what a producer said, under
the authority the source declared, at three times. No success, no failure, no
causal link, and no learning eligibility is derived from a payload -- those are
decisions with their own evidence requirements, made by other code, later, and a
conclusion reached during ingestion would arrive with no evidence chain at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from contextplane.audit import actions
from contextplane.audit.emit import emit
from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError, ValidationError
from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK
from contextplane.signals.admission import REASON_PROHIBITED_CONTENT, admit_observation

# Only `ExternalSignalEnvelopeV1` and `content_digest_for` are used here; the
# rest are re-exports. Every name below is imported from
# `contextplane.signals.ingest` by a module this change does not own -- the two
# transports, their request schemas, and the admission floor's type-checking
# import -- so dropping one would be an edit to those callers disguised as a
# move. `__all__` declares the re-export rather than leaving it incidental.
from contextplane.signals.envelope import (
    MAX_EVIDENCE_HANDLE_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_REFERENCES,
    SIGNAL_SCHEMA_VERSION,
    ExternalSignalEnvelopeV1,
    content_digest_for,
    normalize_references,
    reject_server_assigned,
)
from contextplane.signals.models import ExternalSignal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.service.memory.source_governance import SourceGovernanceService
    from contextplane.types import Clock, TenantContext

_log = logging.getLogger(__name__)

#: What an audit line points at. A stored observation is addressed by its own id;
#: a refusal has no signal to point at, so it is filed against the source, which
#: is what an operator investigating a run of refusals is looking at anyway.
TARGET_SIGNAL: Final[str] = "external_signal"
TARGET_SIGNAL_SOURCE: Final[str] = "external_signal_source"

#: What a signal's references are bound under in the junction every subject
#: shares. The set is closed by the schema's own CHECK, so this is the value the
#: database admits rather than a label chosen here.
SUBJECT_EXTERNAL_SIGNAL: Final[str] = "external_signal"

#: Whether the audited submission was stored by this call or recognised as
#: already stored. Two values under one action, because an auditor counting how
#: often a source retries needs both in one series.
OUTCOME_CREATED: Final[str] = "created"
OUTCOME_RECOGNISED: Final[str] = "recognised"

# Why a submission was turned away, as a closed vocabulary. Closed deliberately:
# these read as labels, and a set that grew a member every time a validation
# message was reworded would be useless for counting. Each names a decision this
# service made about admission -- a malformed envelope never reaches here, and is
# ordinary request validation rather than an admission decision to audit.
REASON_PRODUCER_IDENTITY: Final[str] = "producer_identity"
REASON_SOURCE_UNREGISTERED: Final[str] = "source_unregistered"
REASON_SOURCE_AUTHORITY_INVALID: Final[str] = "source_authority_invalid"
REASON_INGEST_CEILING: Final[str] = "ingest_ceiling"
REASON_IDEMPOTENCY_CONFLICT: Final[str] = "idempotency_conflict"

REJECTION_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_PRODUCER_IDENTITY,
        REASON_SOURCE_UNREGISTERED,
        REASON_SOURCE_AUTHORITY_INVALID,
        REASON_INGEST_CEILING,
        REASON_IDEMPOTENCY_CONFLICT,
        REASON_PROHIBITED_CONTENT,
    }
)


class SignalIngestRefused(RegistryError):
    """The source may not write right now.

    Its own type, and deliberately not a `ValidationError`: nothing about the
    submission is wrong. The source is over its declared ceiling or its circuit
    is open, so the same bytes will be accepted later -- which is a different
    instruction to the caller than "fix this and resend", and the two must not
    map to one status code.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class IngestedSignal:
    """What the caller is told, after a submission is admitted or recognised.

    `replayed` is the field a client retrying a dropped response reads: it is how
    a retry can tell that it found the first write rather than making a second.
    """

    signal_id: uuid.UUID
    #: Server-assigned, one clock read per submission.
    ingested_at: datetime.datetime
    #: Read off the source's declared policy, never off the request.
    authority: str
    content_digest: str
    replayed: bool
    #: Echoed back with collision keys so a producer can correlate its own
    #: references with what this submission was identified by.
    references: tuple[ExternalReferenceV1, ...] = ()


#: The producer types that name one of this system's own participants rather than
#: an external system speaking for itself.
_INTERNAL_PRODUCER_TYPES: Final[frozenset[str]] = frozenset({"human", "agent"})


def _assert_producer_is_the_caller(ctx: TenantContext, normalized: ExternalSignalEnvelopeV1) -> None:
    """A participant may only report as itself.

    `producer_id` is an id in the *source's* space, so an external system's own
    identifier for its runner is exactly right and this service has no way to
    check it. A `human` or `agent` producer is different: that id names a
    participant of this deployment, and letting a caller put somebody else's
    there would file an observation under a name the ledger then treats as
    attribution -- with the reporter's own identity nowhere in the row.

    Refused rather than overwritten with the caller's own id. A silently
    rewritten attribution is a submission that succeeded while reporting
    something other than what the producer sent.
    """
    if normalized.producer_type not in _INTERNAL_PRODUCER_TYPES:
        return
    if normalized.producer_id != str(ctx.actor_id):
        raise ValidationError(
            f"a {normalized.producer_type} signal must report producer_id {ctx.actor_id}: "
            "a participant of this deployment may only report as itself, and an external system's "
            "own producer id belongs on an `external` signal"
        )


async def _bind_references(
    session: AsyncSession,
    ctx: TenantContext,
    references: Sequence[ExternalReferenceV1],
    *,
    signal_id: uuid.UUID,
    now: datetime.datetime,
) -> None:
    """Store what the signal cited, and bind each citation to it.

    Runs in the caller's transaction, so a signal row and the record of what it
    carried are committed together or not at all -- a signal stored without its
    bindings would read as one that cited nothing.

    **First write wins on the reference row.** A collision key already stored
    keeps the values it arrived with: `observed_at`, `classification`,
    `external_authority` and `revision` are not refreshed. One reference row is
    shared by every subject that cites it, so a writer that edited it would
    change what *other* subjects are recorded as having cited, after the fact.
    The consequence is named and accepted: a later submission carrying a higher
    classification for the same reference does not raise the stored one. Whether
    a shared row is ever refreshed, and on whose authority, is a policy decision
    with its own evidence requirements -- ingestion is not where it is made.

    The reference's own fields are spread into the insert rather than listed:
    they are the table's columns, and a second list here would be one that could
    fall out of step with the schema silently.
    """
    for reference in references:
        key = reference.collision_key()
        stored = (
            await session.execute(
                pg_insert(ContextExternalReference)
                .values(
                    reference_id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    collision_key=key,
                    created_at=now,
                    **dataclasses.asdict(reference),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "collision_key"])
                .returning(ContextExternalReference.reference_id)
            )
        ).scalar_one_or_none()
        if stored is None:
            # The insert declined a conflict, so the row is already somebody's
            # and the unique index it collided with makes this lookup exact. A
            # concurrent inserter is waited out by that index before the
            # conflict is reported, so its row is visible by the time this runs.
            stored = (
                await session.execute(
                    select(ContextExternalReference.reference_id).where(
                        ContextExternalReference.tenant_id == ctx.tenant_id,
                        ContextExternalReference.collision_key == key,
                    )
                )
            ).scalar_one()
        session.add(
            ContextReferenceBinding(
                binding_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                reference_id=stored,
                subject_type=SUBJECT_EXTERNAL_SIGNAL,
                subject_id=signal_id,
                bound_at=now,
            )
        )


class SignalIngestService:
    """Admit one external observation into the signal ledger, or say why not.

    Stateless between calls, holding only its collaborators: both transports
    construct one per request from the same session factory and clock, so
    neither carries ingestion policy of its own.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        governance: SourceGovernanceService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._governance = governance

    async def ingest(self, ctx: TenantContext, envelope: ExternalSignalEnvelopeV1) -> IngestedSignal:
        """Store one observation, or return the one already stored for it.

        Ordered deliberately: identity and authorization first, the ceiling
        second, storage last. A submission from an unregistered or unowned source
        must not spend a ceiling, and a submission that will be refused must not
        reach the table at all.
        """
        normalized = envelope.normalized()
        try:
            _assert_producer_is_the_caller(ctx, normalized)
        except ValidationError:
            await self._audit_rejected(ctx, normalized, reason_class=REASON_PRODUCER_IDENTITY)
            raise
        authority = await self._authority_for(ctx, normalized)
        digest = content_digest_for(normalized)

        # Before the replay lookup, deliberately; `signals/admission.py` says why.
        await admit_observation(
            self._session_factory, ctx, normalized, digest=digest, audit_rejection=self._audit_rejected
        )

        # Read before the ceiling is spent. A redelivery is not new work, and
        # charging a retry against the window would let a client with a flaky
        # connection trip its own source's breaker by succeeding slowly.
        existing = await self._existing(ctx, normalized, digest)
        if existing is not None:
            await self._audit_ingested(ctx, normalized, existing, outcome=OUTCOME_RECOGNISED)
            return existing

        admission = await self._governance.admit(normalized.source_id)
        if not admission.permitted:
            _log.info(
                "signal_ingest_refused",
                extra={
                    "tenant_id": str(ctx.tenant_id),
                    "source_id": str(normalized.source_id),
                    "reason": admission.reason,
                },
            )
            await self._audit_rejected(ctx, normalized, reason_class=REASON_INGEST_CEILING)
            raise SignalIngestRefused(admission.reason or "the source may not write right now")

        # One clock read for the whole submission: the instant the row is
        # stamped with is the instant it was admitted at.
        now = self._clock.now()
        signal_id = uuid.uuid4()
        try:
            await self._store(ctx, normalized, signal_id=signal_id, digest=digest, authority=authority, now=now)
        except IntegrityError:
            # Two submissions of one observation raced past the read above, and
            # the ledger's unique keys refused the loser. The loser is not a
            # failure: re-reading either finds the row the winner wrote (an exact
            # replay, which is what a concurrent double-submit is) or raises the
            # same conflict a serial changed replay would. Without this, an
            # idempotent surface answers 500 for the one case it exists to make
            # safe.
            #
            # `_store` also writes references and bindings, so this catch is
            # wider than the ledger. It is still only reachable through the
            # ledger's keys: the reference upsert declines its own conflict
            # rather than raising, and the binding's unique key is scoped by a
            # `signal_id` generated on this call, which nothing else can hold.
            # Any other integrity failure re-raises below rather than being
            # reported as a replay -- a wrong answer here would tell a caller
            # its observation was stored when it was not.
            resolved = await self._existing(ctx, normalized, digest)
            if resolved is None:
                raise
            await self._audit_ingested(ctx, normalized, resolved, outcome=OUTCOME_RECOGNISED)
            return resolved
        _log.info(
            "signal_ingested",
            extra={
                "tenant_id": str(ctx.tenant_id),
                "source_id": str(normalized.source_id),
                "signal_id": str(signal_id),
                "producer_type": normalized.producer_type,
                "classification": normalized.classification,
                "authority": authority,
                "reference_count": len(normalized.references),
            },
        )
        ingested = IngestedSignal(
            signal_id=signal_id,
            ingested_at=now,
            authority=authority,
            content_digest=digest,
            replayed=False,
            references=normalized.references,
        )
        await self._audit_ingested(ctx, normalized, ingested, outcome=OUTCOME_CREATED)
        return ingested

    async def _audit_ingested(
        self,
        ctx: TenantContext,
        normalized: ExternalSignalEnvelopeV1,
        ingested: IngestedSignal,
        *,
        outcome: str,
    ) -> None:
        """Record that an observation entered the ledger, or was recognised in it.

        Both outcomes under one action with an `outcome` field rather than two
        actions: an auditor counting how often a source retries needs the two in
        one series, and a log that recorded only first arrivals cannot answer it.

        The payload and the evidence handle never appear. The audit log is the
        one table guaranteed to be retained and read, so putting the observation
        in it would defeat every retention and classification decision made about
        the ledger row itself -- the digest is what an auditor needs to tie this
        line to that row anyway. `producer_id` is left out for the same reason at
        one remove: `actor_id` on the row already names an internal producer, and
        an external one's id is a free-text string from another system that may
        well be a person's name.
        """
        await emit(
            self._session_factory,
            ctx,
            self._clock,
            action=actions.SIGNAL_INGESTED,
            target_type=TARGET_SIGNAL,
            target_id=ingested.signal_id,
            after={
                "outcome": outcome,
                "source_id": str(normalized.source_id),
                "source_system": normalized.source_system,
                "source_event_id": normalized.source_event_id,
                "producer_type": normalized.producer_type,
                "classification": normalized.classification,
                "authority": ingested.authority,
                "schema_version": normalized.schema_version,
                "content_digest": ingested.content_digest,
                "reference_count": len(normalized.references),
            },
        )

    async def _audit_rejected(
        self,
        ctx: TenantContext,
        normalized: ExternalSignalEnvelopeV1,
        *,
        reason_class: str,
        content_digest: str | None = None,
    ) -> None:
        """Record that an observation was turned away, and which rule turned it.

        Targeted at the source rather than the signal, because a refusal leaves
        no signal to point at -- and the source is what an operator investigating
        a run of refusals is actually looking at.

        `reason_class` is a closed set, deliberately: it reads as a label, and a
        vocabulary that grew a member every time a validation message was
        reworded would be useless for counting. The message itself is not
        recorded, because a message can quote the offending value and this is the
        one place a record is guaranteed to be kept.
        """
        await emit(
            self._session_factory,
            ctx,
            self._clock,
            action=actions.SIGNAL_REJECTED,
            target_type=TARGET_SIGNAL_SOURCE,
            target_id=normalized.source_id,
            after={
                "reason_class": reason_class,
                "source_system": normalized.source_system,
                "source_event_id": normalized.source_event_id,
                "producer_type": normalized.producer_type,
                # Content refusals only: the sole handle on what was turned away.
                **({} if content_digest is None else {"content_digest": content_digest}),
            },
            error_code=reason_class,
        )

    async def _store(
        self,
        ctx: TenantContext,
        normalized: ExternalSignalEnvelopeV1,
        *,
        signal_id: uuid.UUID,
        digest: str,
        authority: str,
        now: datetime.datetime,
    ) -> None:
        """Write the row and what it cited, in one transaction, and nothing else.

        Separate from `ingest` so the race recovery above has one thing to catch.
        Three tables are written here, and only the ledger's own unique keys can
        raise: the reference upsert declines a collision rather than raising it,
        and the binding's unique key is scoped by a `signal_id` this call just
        generated. That is what makes "an `IntegrityError` out of here is a lost
        insert race" still true after the bindings were added -- and the caller
        re-raises anything it cannot resolve, rather than assuming it.
        """
        async with self._session_factory() as session, session.begin():
            session.add(
                ExternalSignal(
                    signal_id=signal_id,
                    tenant_id=ctx.tenant_id,
                    team_key=normalized.team_key,
                    project_key=normalized.project_key,
                    source_system=normalized.source_system,
                    producer_id=normalized.producer_id,
                    producer_type=normalized.producer_type,
                    source_event_id=normalized.source_event_id,
                    idempotency_key=normalized.idempotency_key,
                    content_digest=digest,
                    authority=authority,
                    classification=normalized.classification,
                    event_time=normalized.event_time,
                    observed_time=normalized.observed_time,
                    ingested_at=now,
                    expires_at=normalized.expires_at,
                    schema_version=normalized.schema_version,
                    payload=None if normalized.payload is None else dict(normalized.payload),
                    evidence_handle=normalized.evidence_handle,
                    # Never derived at ingestion. A source marks its own earlier
                    # occurrence superseded through its own later submission;
                    # inferring it here would decide what may be learned from
                    # with no evidence that anything was superseded.
                    superseded_for_learning=False,
                )
            )
            # Same transaction, deliberately: a signal whose bindings were
            # written separately could be committed as one that cited nothing.
            await _bind_references(session, ctx, normalized.references, signal_id=signal_id, now=now)

    async def _authority_for(self, ctx: TenantContext, normalized: ExternalSignalEnvelopeV1) -> str:
        """What the source declared its observations are worth.

        A source this tenant does not own is reported exactly as one that does
        not exist. The check is here, above `admit`, because `admit` resolves a
        source by id alone -- correct for its own job, and a cross-tenant oracle
        if it were the only gate on this path.

        Both refusals audit before they raise, and both audit under the caller's
        own tenant: the row records that *this* tenant named that source and was
        turned away, which says nothing about whether the source exists elsewhere.
        """
        source_id = normalized.source_id
        policy = await self._governance.policy_for(source_id)
        if policy is None or policy.tenant_id != ctx.tenant_id:
            await self._audit_rejected(ctx, normalized, reason_class=REASON_SOURCE_UNREGISTERED)
            raise NotFoundError("no such source")
        if policy.authority_tier not in SOURCE_AUTHORITY_RANK:
            # Only reachable for a row written around the declaration path, which
            # validates the tier. Refused rather than stored: a tier outside the
            # ladder ranks against nothing, so every later conflict involving
            # this signal would be decided by an ordering that does not exist.
            await self._audit_rejected(ctx, normalized, reason_class=REASON_SOURCE_AUTHORITY_INVALID)
            raise ValidationError(
                f"source {source_id} carries authority tier {policy.authority_tier!r}, "
                f"which is not on the authority ladder {sorted(SOURCE_AUTHORITY_RANK)}"
            )
        return policy.authority_tier

    async def _existing(
        self,
        ctx: TenantContext,
        normalized: ExternalSignalEnvelopeV1,
        digest: str,
    ) -> IngestedSignal | None:
        """The row this submission is a redelivery of, or None if it is new.

        Both of the ledger's unique keys are consulted in one read, because
        either one matching means the caller is talking about something already
        stored. Same digest converges; a different digest is refused, because
        neither overwriting the stored observation nor storing a second one under
        the same key would be true.
        """
        stmt = select(ExternalSignal).where(
            ExternalSignal.tenant_id == ctx.tenant_id,
            ExternalSignal.producer_id == normalized.producer_id,
            (ExternalSignal.source_event_id == normalized.source_event_id)
            | (ExternalSignal.idempotency_key == normalized.idempotency_key),
        )
        async with self._session_factory() as session:
            rows = tuple((await session.execute(stmt)).scalars().all())

        if not rows:
            return None
        for row in rows:
            if row.content_digest == digest:
                return IngestedSignal(
                    signal_id=row.signal_id,
                    ingested_at=row.ingested_at,
                    authority=row.authority,
                    content_digest=row.content_digest,
                    replayed=True,
                    references=normalized.references,
                )
        await self._audit_rejected(ctx, normalized, reason_class=REASON_IDEMPOTENCY_CONFLICT)
        raise ConflictError(
            f"signal {rows[0].signal_id} is already stored for this source event or submission key "
            "with different content; a replay that changed what it reports is not a replay"
        )


__all__ = [
    # This module's own surface.
    "OUTCOME_CREATED",
    "OUTCOME_RECOGNISED",
    "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_INGEST_CEILING",
    "REASON_PRODUCER_IDENTITY",
    "REASON_SOURCE_AUTHORITY_INVALID",
    "REASON_SOURCE_UNREGISTERED",
    "REJECTION_REASONS",
    "SUBJECT_EXTERNAL_SIGNAL",
    "TARGET_SIGNAL",
    "TARGET_SIGNAL_SOURCE",
    "IngestedSignal",
    "SignalIngestRefused",
    "SignalIngestService",
    # Re-exported from `signals/envelope.py`, where they now live. Kept importable
    # from here because callers outside this change import them from here; new
    # code should read them from the envelope module directly.
    "MAX_EVIDENCE_HANDLE_LENGTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_REFERENCES",
    "SIGNAL_SCHEMA_VERSION",
    "ExternalSignalEnvelopeV1",
    "content_digest_for",
    "normalize_references",
    "reject_server_assigned",
]
