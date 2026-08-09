"""Admitting one external observation, once, under the authority its source declared.

One contract for every producer -- a human reporting an outcome, an agent
reporting one, an external system reporting its own run -- because a second
ingestion path is a second place the rules can differ, and the rule that differs
is the one nobody re-reads.

**The three things a caller may not decide, and why each is refused rather than
ignored.**

- *Ingestion time.* Server-assigned, from one clock read per submission. A
  supplied `ingested_at` is refused with a message naming it, not silently
  dropped: a producer that believes it set the audit anchor and did not will
  reconcile two systems against a timestamp that means something else.
- *Authority.* Read from the source's own declared governance policy. A producer
  that could name its own authority would name the strongest one, and every
  conflict decided against that claim for the rest of its life would be decided
  wrongly. The ladder itself is the shared one the claim path already uses --
  there is exactly one ordering over these values in this codebase.
- *Signal identity and content digest.* Both derived here. A caller-supplied
  digest would let a replay declare itself unchanged.

**An unregistered source may not write, and neither may a registered one that
belongs to somebody else.** Both answer identically -- the same status, the same
message -- because a distinguishable refusal turns a source id into a
cross-tenant existence oracle. The ownership check runs *before* the ceiling
check, deliberately: admitting against another tenant's declared window would
let one tenant spend a second tenant's ingest budget and read the answer off the
breaker.

**Bounds are the source's own declared ceiling, plus size limits on what one
submission may carry.** The ceiling and circuit breaker are the ones the source
declared when it registered, so a runaway producer stops mattering without an
operator. The size limits exist for a different failure: a single submission
large enough to matter is not stopped by a count-based ceiling at all.

**Replay is decided by content, not by arrival.** The digest is computed over the
normalized envelope, so exact redelivery converges on the row already stored and
a reused key carrying different content is refused. Both keys the ledger enforces
are honoured: the same external occurrence resubmitted under a fresh submission
key finds the stored row rather than creating a second one, because "the same
thing happened" and "the same request was sent" are different questions and the
producer controls only the second.

**References are normalized before the digest, not after.** Two spellings of one
commit have to fold to one form first, or a redelivery that spells a reference
differently reads as changed content and is refused as a conflict. The ledger
carries no reference columns of its own, so a reference reaches durable storage
only through this identity: it binds the submission, and it comes back to the
caller with its collision key so a producer can correlate. Answering "which
references did this signal carry" from storage alone needs a column or a junction
that does not exist yet.

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
made about the ledger row itself. A content refusal is the one exception, and it
carries a *digest* rather than content: the refusal keeps nothing of what was
turned away, so without it an operator has no handle for asking whether a row
bearing the same digest is already in the ledger from before the floor existed.

**Prohibited content is refused before anything is decided, the replay lookup
included.** The observation and the normalized references are scanned separately,
because a producer can get the observation right and still paste a credential
into a reference URI beside it, and the observation scan covers the
evidence-handle URI as well as the payload -- a credential in a query string is a
credential in storage. Running the floor ahead of the replay read costs one scan
on the retry path and closes the case that would otherwise stay open: a detector
added after a row was stored would let an exact redelivery of prohibited content
return the stored row, which is an admitted path to content the floor now
prohibits, reachable by resending it.

**Nothing here concludes anything.** The row records what a producer said, under
the authority the source declared, at three times. No success, no failure, no
causal link, and no learning eligibility is derived from a payload -- those are
decisions with their own evidence requirements, made by other code, later, and a
conclusion reached during ingestion would arrive with no evidence chain at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from contextplane.audit import actions
from contextplane.audit.emit import emit
from contextplane.context.admission import (
    FIELD_EXTERNAL_SIGNAL_PAYLOAD,
    FIELD_EXTERNAL_SIGNAL_REFERENCES,
)
from contextplane.context.schemas.reference import normalize_reference
from contextplane.context.schemas.trust import CLASSIFICATIONS, ExternalReferenceV1, InvalidContextItem
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError, ValidationError
from contextplane.security.pii_guard import AdmissionRefused, admit_or_refuse
from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK
from contextplane.signals.models import ExternalSignal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.service.memory.source_governance import SourceGovernanceService
    from contextplane.types import Clock, TenantContext

_log = logging.getLogger(__name__)

#: The envelope contract version this module writes. A row records the version it
#: was written under so a later reader can tell which shape it is looking at
#: without guessing from which columns happen to be populated.
SIGNAL_SCHEMA_VERSION: Final[str] = "external_signal.v1"

#: Versions this module will accept on the way in. One entry today; the set
#: exists so adding the second is an edit to a vocabulary rather than to a
#: comparison buried in a validator.
SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[str]] = frozenset({SIGNAL_SCHEMA_VERSION})

#: Who produced the observation, matching the ledger's own closed set. `external`
#: is a system speaking for itself; `human` and `agent` are this system's own
#: participants reporting through a registered source.
PRODUCER_TYPES: Final[frozenset[str]] = frozenset({"human", "agent", "external"})

#: Field names a caller may not send. Each is server-derived, and each would
#: change how the row is later read if a producer could set it.
SERVER_ASSIGNED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "ingested_at",
        "ingestion_time",
        "authority",
        "signal_id",
        "content_digest",
    }
)

#: What an audit line points at. A stored observation is addressed by its own id;
#: a refusal has no signal to point at, so it is filed against the source, which
#: is what an operator investigating a run of refusals is looking at anyway.
TARGET_SIGNAL: Final[str] = "external_signal"
TARGET_SIGNAL_SOURCE: Final[str] = "external_signal_source"

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
REASON_PROHIBITED_CONTENT: Final[str] = "prohibited_content"

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

# Size bounds. A count-based ceiling stops a chatty producer and does nothing
# about one submission large enough to matter on its own, so both exist.
MAX_IDENTIFIER_LENGTH: Final[int] = 512
MAX_IDEMPOTENCY_KEY_LENGTH: Final[int] = 255
MAX_EVIDENCE_HANDLE_LENGTH: Final[int] = 2048
MAX_REFERENCES: Final[int] = 32
MAX_PAYLOAD_BYTES: Final[int] = 64 * 1024


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
class ExternalSignalEnvelopeV1:
    """One observation, as a producer describes it.

    Frozen and validated on construction. `__post_init__` refuses rather than
    repairs: a silently-corrected envelope is worse than a rejected one, because
    the correction is invisible at the point somebody relies on the row it wrote.

    Three fields the ledger stores are absent here on purpose -- ingestion time,
    authority, and the content digest are all derived by the service, and an
    envelope that could carry them would be an envelope a caller could use to
    decide them. See the module docstring.
    """

    #: The registered source this observation arrives through. Not a name: the
    #: registration is what carries the declared authority and the ceiling, and a
    #: free-text system name would name no registration at all.
    source_id: uuid.UUID
    #: The external system's own name for itself, folded to lowercase before the
    #: write so two spellings of one source cannot occupy two rows.
    source_system: str
    #: The other system's identifier for this occurrence. Trimmed, not folded:
    #: its case belongs to the system that issued it.
    source_event_id: str
    #: Who produced it, and what kind of thing that is.
    producer_id: str
    producer_type: str
    #: The submission's own key. Distinct from `source_event_id`: one occurrence
    #: may be resubmitted under a new key, and one producer may submit two
    #: occurrences under two keys within one window.
    idempotency_key: str
    classification: str
    schema_version: str
    #: When the source says it happened, and when the producer learned of it.
    #: Both required and never substituted for one another -- collapsing them
    #: destroys the only evidence of lag between a system and its reporter.
    event_time: datetime.datetime
    observed_time: datetime.datetime
    #: The work this observation is about, normalized. Empty is legal: a
    #: diagnostic observation about no particular piece of work is a real thing
    #: to report, and a required reference would be filled in with a placeholder.
    references: tuple[ExternalReferenceV1, ...] = ()
    #: Scope below the tenant. Absent where absence is the meaning: not every
    #: producer knows a team or a project, and a placeholder would be grouped as
    #: though it did.
    team_key: str | None = None
    project_key: str | None = None
    #: When this observation stops being usable as current. Absent means no
    #: expiry was declared, which is not the same as never expires.
    expires_at: datetime.datetime | None = None
    #: The allowlisted projection, or a handle to authorized evidence held
    #: elsewhere. Exactly one, enforced here and again by the ledger's own CHECK.
    payload: Mapping[str, Any] | None = None
    evidence_handle: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValidationError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"this surface writes {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if self.producer_type not in PRODUCER_TYPES:
            raise ValidationError(f"producer_type must be one of {sorted(PRODUCER_TYPES)}")
        if self.classification not in CLASSIFICATIONS:
            # Refused rather than defaulted: a classification nobody declared is
            # one no retention policy covers, and the row would outlive the
            # question of which policy should have applied to it.
            raise ValidationError(f"unknown classification {self.classification!r}")

        for name, value, bound in (
            ("source_system", self.source_system, MAX_IDENTIFIER_LENGTH),
            ("source_event_id", self.source_event_id, MAX_IDENTIFIER_LENGTH),
            ("producer_id", self.producer_id, MAX_IDENTIFIER_LENGTH),
            ("idempotency_key", self.idempotency_key, MAX_IDEMPOTENCY_KEY_LENGTH),
        ):
            if not value.strip():
                raise ValidationError(f"a signal needs a {name}; it is part of how two submissions are told apart")
            if len(value) > bound:
                raise ValidationError(f"{name} is {len(value)} characters, over the {bound}-character bound")

        for name, optional in (("team_key", self.team_key), ("project_key", self.project_key)):
            if optional is not None and not optional.strip():
                # An empty string is the failure that passes an `is not None`
                # check: it would be stored and grouped as a real scope.
                raise ValidationError(f"{name} must be absent or non-empty, never an empty string")

        for name, moment in (
            ("event_time", self.event_time),
            ("observed_time", self.observed_time),
            ("expires_at", self.expires_at),
        ):
            if moment is not None and moment.tzinfo is None:
                raise ValidationError(f"{name} must be timezone-aware; a naive timestamp is unreadable across zones")

        if (self.payload is None) == (self.evidence_handle is None):
            raise ValidationError(
                "a signal carries exactly one of payload or evidence_handle: two copies of one observation drift, "
                "and a signal carrying neither asserts only that something happened somewhere"
            )
        if self.evidence_handle is not None:
            if not self.evidence_handle.strip():
                raise ValidationError("evidence_handle must be non-empty when it is the observation")
            if len(self.evidence_handle) > MAX_EVIDENCE_HANDLE_LENGTH:
                raise ValidationError(
                    f"evidence_handle is {len(self.evidence_handle)} characters, "
                    f"over the {MAX_EVIDENCE_HANDLE_LENGTH}-character bound"
                )
        if self.payload is not None:
            if not self.payload:
                # An empty object is the shape the ledger's own exclusivity rule
                # exists to keep out: it satisfies "a payload is present" while
                # carrying no observation at all, leaving a row that asserts only
                # that something happened somewhere.
                raise ValidationError("payload must carry the observation; an empty object reports nothing")
            encoded = _canonical_json(dict(self.payload))
            if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise ValidationError(f"payload exceeds the {MAX_PAYLOAD_BYTES}-byte bound")

        if len(self.references) > MAX_REFERENCES:
            raise ValidationError(f"a signal carries at most {MAX_REFERENCES} references, got {len(self.references)}")

    def normalized(self) -> ExternalSignalEnvelopeV1:
        """The envelope as it will be stored: folded, trimmed, deduplicated.

        Applied before the digest is taken, so two spellings of one submission
        produce one digest instead of reading as changed content.
        """
        seen: dict[str, ExternalReferenceV1] = {}
        for reference in self.references:
            # Dict-keyed by collision key rather than filtered with a set: the
            # references are frozen dataclasses whose equality is field-wise, so
            # two spellings of one reference are unequal objects that must still
            # collapse to one entry.
            seen.setdefault(reference.collision_key(), reference)
        return dataclasses.replace(
            self,
            source_system=self.source_system.strip().lower(),
            source_event_id=self.source_event_id.strip(),
            producer_id=self.producer_id.strip(),
            idempotency_key=self.idempotency_key.strip(),
            references=tuple(seen[key] for key in sorted(seen)),
        )


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


def _canonical_json(value: object) -> str:
    """One JSON spelling per value, for digest inputs.

    Sorted keys and no incidental whitespace, the same shape every other digest
    input in this codebase uses. JSON's own delimiters are what keep two
    different field splits from hashing alike, so no separate length prefix is
    needed here.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _reference_digest_parts(reference: ExternalReferenceV1) -> dict[str, Any]:
    """A reference as the digest sees it: identity, plus what a reader compares.

    `collision_key` is included alongside the parts rather than instead of them.
    The key answers "is this the same reference"; the parts are what changed when
    a producer resends the same reference at a new revision, and a digest that
    ignored them would call that a replay.
    """
    return {
        "collision_key": reference.collision_key(),
        "source_system": reference.source_system,
        "source_namespace": reference.source_namespace,
        "kind": reference.kind,
        "external_id": reference.external_id,
        "revision": reference.revision,
        "classification": reference.classification,
        "external_authority": reference.external_authority,
    }


def content_digest_for(envelope: ExternalSignalEnvelopeV1) -> str:
    """The digest that decides whether a resubmission is a replay.

    Covers everything a producer controls and nothing the server decides:
    ingestion time, the derived authority and the signal id are all absent, so a
    redelivery is not made to look changed by the passage of time or by a
    governance edit between the two calls.
    """
    normalized = envelope.normalized()
    material = {
        "schema_version": normalized.schema_version,
        "source_id": str(normalized.source_id),
        "source_system": normalized.source_system,
        "source_event_id": normalized.source_event_id,
        "producer_id": normalized.producer_id,
        "producer_type": normalized.producer_type,
        "team_key": normalized.team_key,
        "project_key": normalized.project_key,
        "classification": normalized.classification,
        "event_time": normalized.event_time.isoformat(),
        "observed_time": normalized.observed_time.isoformat(),
        "expires_at": None if normalized.expires_at is None else normalized.expires_at.isoformat(),
        "references": [_reference_digest_parts(reference) for reference in normalized.references],
        "payload": None if normalized.payload is None else dict(normalized.payload),
        "evidence_handle": normalized.evidence_handle,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def normalize_references(raw: Sequence[Mapping[str, Any]]) -> tuple[ExternalReferenceV1, ...]:
    """Build normalized references from a producer's mappings, or refuse.

    Wraps the shared normalizer so both transports raise the same
    `ValidationError` for a malformed reference instead of each translating the
    context layer's own exception its own way.
    """
    references: list[ExternalReferenceV1] = []
    for index, item in enumerate(raw):
        try:
            references.append(normalize_reference(dict(item)))
        except InvalidContextItem as exc:
            raise ValidationError(f"references[{index}]: {exc}") from exc
    return tuple(references)


def reject_server_assigned(supplied: Mapping[str, Any]) -> None:
    """Refuse a submission that tried to set something the server decides.

    Named refusal rather than a silent drop, for every field at once: a producer
    that believes it stamped the ingestion time, or declared its own authority,
    has to be told it did not. Both transports call this so the refusal is the
    same on either -- one of them accepting quietly is exactly the divergence the
    parity suite exists to catch.
    """
    offending = sorted(SERVER_ASSIGNED_FIELDS & set(supplied))
    if offending:
        raise ValidationError(
            f"{', '.join(offending)} is decided by this service and may not be supplied: "
            "ingestion time is the audit anchor, authority comes from the source's own declared policy, "
            "and the signal id and content digest are derived from what you sent"
        )


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

        # The floor, and it runs before the replay lookup on purpose. A detector
        # added after a row was stored would otherwise let an exact redelivery of
        # prohibited content return 200 and the stored row -- an admitted path to
        # content the floor now prohibits, reached by resending it. Refusing here
        # costs one scan on the replay path and leaves no such path open.
        await self._admit_content(ctx, normalized, digest=digest)

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
            resolved = await self._existing(ctx, normalized, digest)
            if resolved is None:  # pragma: no cover - the row that refused the insert cannot then be absent
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

    async def _admit_content(
        self,
        ctx: TenantContext,
        normalized: ExternalSignalEnvelopeV1,
        *,
        digest: str,
    ) -> None:
        """Refuse an observation carrying a prohibited class, before anything is decided.

        Two scans, because the two are separately authored: a producer can get
        the observation right and still paste a credential into a reference URI
        beside it. The payload scan covers whichever form the observation took --
        the canonical serialization of the mapping, or the evidence-handle URI,
        which is a real token channel rather than an opaque pointer.

        `admit_or_refuse` writes the per-class refusal rows itself. What is added
        here is the `signal.rejected` audit row carrying the content digest: the
        refusal stores nothing of the content, so the digest is the only handle
        an operator has for asking whether a row bearing it is already in the
        ledger from before this floor existed.
        """
        subject = f"{normalized.source_id}:{normalized.source_event_id}"
        scans: tuple[tuple[str, str], ...] = (
            (FIELD_EXTERNAL_SIGNAL_PAYLOAD, self._observation_text(normalized)),
            (FIELD_EXTERNAL_SIGNAL_REFERENCES, self._reference_text(normalized)),
        )
        for field_type, text_to_scan in scans:
            if not text_to_scan:
                continue
            try:
                await admit_or_refuse(
                    self._session_factory,
                    ctx,
                    text_to_scan,
                    field_type,
                    subject=subject,
                )
            except AdmissionRefused as refused:
                _log.info(
                    "signal_ingest_refused",
                    extra={
                        "tenant_id": str(ctx.tenant_id),
                        "source_id": str(normalized.source_id),
                        "reason": REASON_PROHIBITED_CONTENT,
                        "field_type": field_type,
                        # Which detectors fired, never what they matched and
                        # never where: an offset plus a length is a description
                        # of the secret's position in text an attacker may be
                        # able to reconstruct.
                        "pii_classes": sorted(set(refused.decision.classes)),
                    },
                )
                await self._audit_rejected(
                    ctx,
                    normalized,
                    reason_class=REASON_PROHIBITED_CONTENT,
                    content_digest=digest,
                )
                # Re-raised as the service layer's own refusal rather than the
                # scanner's. Both transports already translate `ValidationError`
                # to the status a caller can act on, and letting a
                # `security.pii_guard` type through would make every adapter
                # learn a second refusal vocabulary to say the same thing. The
                # message names the classes that fired and never what matched:
                # a refusal that quoted the value would put it in every client
                # log, which is the opposite of what refusing it was for.
                raise ValidationError(
                    "content carries a prohibited class and was not stored: "
                    + ", ".join(sorted(set(refused.decision.classes)))
                ) from refused

    @staticmethod
    def _reference_text(normalized: ExternalSignalEnvelopeV1) -> str:
        """Every field of every normalized reference, serialized for scanning.

        Deliberately *not* `_reference_digest_parts`, which is the right input for
        a replay digest and the wrong one here: it omits `authorized_uri`, which
        is precisely the field a credential gets pasted into. Scanning the digest
        material would have left the most likely token channel in a reference
        unread while looking thorough.
        """
        return _canonical_json(
            [
                {
                    "source_system": reference.source_system,
                    "source_namespace": reference.source_namespace,
                    "kind": reference.kind,
                    "external_id": reference.external_id,
                    "classification": reference.classification,
                    "external_authority": reference.external_authority,
                    "revision": reference.revision,
                    "authorized_uri": reference.authorized_uri,
                }
                for reference in normalized.references
            ]
        )

    @staticmethod
    def _observation_text(normalized: ExternalSignalEnvelopeV1) -> str:
        """The observation as one scannable string, whichever form it arrived in.

        Exactly one of the two is set -- the envelope enforces that -- so this
        never concatenates them and never scans an empty payload as though it
        were content.
        """
        if normalized.payload is not None:
            return _canonical_json(dict(normalized.payload))
        return normalized.evidence_handle or ""

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
                # Present only for a content refusal, where it is the sole handle
                # on what was turned away: the refusal keeps none of the content,
                # so without the digest an operator cannot ask whether a matching
                # row predates the floor.
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
        """Write the row, in its own transaction, and nothing else.

        Separate from `ingest` so the race recovery above has one thing to catch:
        an `IntegrityError` raised anywhere else in that method would mean
        something other than a lost insert race.
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
    "MAX_EVIDENCE_HANDLE_LENGTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_PAYLOAD_BYTES",
    "MAX_REFERENCES",
    "OUTCOME_CREATED",
    "OUTCOME_RECOGNISED",
    "PRODUCER_TYPES",
    "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_INGEST_CEILING",
    "REASON_PRODUCER_IDENTITY",
    "REASON_SOURCE_AUTHORITY_INVALID",
    "REASON_SOURCE_UNREGISTERED",
    "REJECTION_REASONS",
    "SERVER_ASSIGNED_FIELDS",
    "TARGET_SIGNAL",
    "TARGET_SIGNAL_SOURCE",
    "SIGNAL_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ExternalSignalEnvelopeV1",
    "IngestedSignal",
    "SignalIngestRefused",
    "SignalIngestService",
    "content_digest_for",
    "normalize_references",
    "reject_server_assigned",
]
