"""What a producer sends, and what makes two submissions the same one.

Everything here is decided from the submission alone. Nothing reads a database,
consults a policy, or knows a service exists -- which is what makes the envelope
checkable on its own and the digest reproducible by anyone holding the same
bytes. Admission, authority, ceilings and storage live in `signals/ingest.py`,
which imports this module and not the other way round.

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
- *Signal identity and content digest.* Both derived by the service. A
  caller-supplied digest would let a replay declare itself unchanged.

The envelope refuses all three by name at the boundary, so the refusal is the
same whichever transport a submission arrived on.

**Size limits are here; the source's declared ceiling is not.** They stop
different failures. A count-based ceiling stops a chatty producer and does
nothing about one submission large enough to matter on its own, and only the
second is decidable from the submission alone.

**Replay is decided by content, not by arrival.** The digest is computed over the
normalized envelope, so an exact redelivery converges on the row already stored
and a reused key carrying different content is refused. It covers everything a
producer controls and nothing the server decides, so a redelivery is not made to
look changed by the passage of time or by a governance edit between two calls.

**References are normalized before the digest, not after.** Two spellings of one
commit have to fold to one form first, or a redelivery that spells a reference
differently reads as changed content and is refused as a conflict. That same
normalized identity is what the service later binds to the stored signal.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from typing import TYPE_CHECKING, Any, Final

from contextplane.context.schemas.reference import normalize_reference
from contextplane.context.schemas.trust import CLASSIFICATIONS, ExternalReferenceV1, InvalidContextItem
from contextplane.exceptions import ValidationError
from contextplane.signals.admission import canonical_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

#: The envelope contract version this surface writes. A row records the version
#: it was written under so a later reader can tell which shape it is looking at
#: without guessing from which columns happen to be populated.
SIGNAL_SCHEMA_VERSION: Final[str] = "external_signal.v1"

#: Versions this surface will accept on the way in. One entry today; the set
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

# Size bounds. A count-based ceiling stops a chatty producer and does nothing
# about one submission large enough to matter on its own, so both exist -- and
# only these are decidable without asking the source's registration anything.
MAX_IDENTIFIER_LENGTH: Final[int] = 512
MAX_IDEMPOTENCY_KEY_LENGTH: Final[int] = 255
MAX_EVIDENCE_HANDLE_LENGTH: Final[int] = 2048
MAX_REFERENCES: Final[int] = 32
MAX_PAYLOAD_BYTES: Final[int] = 64 * 1024


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
            encoded = canonical_json(dict(self.payload))
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
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


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


__all__ = [
    "MAX_EVIDENCE_HANDLE_LENGTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_PAYLOAD_BYTES",
    "MAX_REFERENCES",
    "PRODUCER_TYPES",
    "SERVER_ASSIGNED_FIELDS",
    "SIGNAL_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ExternalSignalEnvelopeV1",
    "content_digest_for",
    "normalize_references",
    "reject_server_assigned",
]
