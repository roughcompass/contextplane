"""Wire shapes for signal ingestion.

These mirror the frozen envelope rather than re-deciding anything. The envelope
already refuses an unknown classification, a naive timestamp, a submission
carrying both a payload and an evidence handle; restating those rules here would
create a second place they can drift, and the copy that drifts is always the one
nobody is looking at.

What these types add is the transport's own concern: **what a client may send.**
Three fields the ledger stores are absent from the request on purpose --
ingestion time, authority, and the content digest are all server-derived, and a
request shape that carried them would be a shape a producer could use to decide
them. `extra="forbid"` closes the body so a misspelled field is a named 422
rather than an argument silently dropped, and the reserved-name check above it
turns the specific case of sending a server-assigned field into an error that
says *why* rather than only *which*.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.sensitivity import Tier
from contextplane.signals.ingest import (
    MAX_EVIDENCE_HANDLE_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_REFERENCES,
    SIGNAL_SCHEMA_VERSION,
    IngestedSignal,
    reject_server_assigned,
)

#: The handling classes the envelope accepts, spelled as a `Literal` so an
#: unknown one is a request-validation 422 naming the field rather than a
#: service-raised error three frames deeper.
#: Aliased rather than restated. This file already imports from `trust.py` and
#: could always have derived it; now the definition is one layer lower still.
ClassificationLiteral = Tier

ProducerTypeLiteral = Literal["human", "agent", "external"]


class SignalReference(BaseModel):
    """One piece of external work an observation is about.

    Closed and complete for the same reason the reference contract itself is:
    `source_system`, `source_namespace`, `kind` and `external_id` are the
    collision scope, so a reference missing one of them collides with everything
    else missing it. Classification and the *external* authority are required
    because a reference nobody can weigh is not evidence of anything.
    """

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(min_length=1, description="Owning system, e.g. `github`. Folded to lowercase.")
    source_namespace: str = Field(min_length=1, description="Namespace inside that system, e.g. `acme/app`.")
    kind: str = Field(min_length=1, description="What the reference names, e.g. `commit`, `run`, `deployment`.")
    external_id: str = Field(min_length=1, description="The id within that system. Trimmed, never case-folded.")
    classification: ClassificationLiteral
    external_authority: str = Field(
        min_length=1,
        description="The authority in the external system. Never this service's own.",
    )
    revision: str | None = Field(default=None, description="Immutable revision, when the source records one.")
    authorized_uri: str | None = Field(default=None, description="A URI a reader is permitted to follow.")
    observed_at: datetime.datetime | None = Field(
        default=None,
        description="When the source observed this reference. Absent when it cannot report one.",
    )


class NormalizedSignalReference(SignalReference):
    """A reference as stored identity sees it: the same fields, plus the key.

    `collision_key` is what makes two spellings of one reference compare equal,
    and it is returned rather than left for the caller to recompute -- a producer
    correlating its own references against what this submission was identified by
    should not have to reimplement the digest to do it.
    """

    collision_key: str

    @classmethod
    def of(cls, reference: ExternalReferenceV1) -> NormalizedSignalReference:
        """The wire shape of one normalized reference."""
        return cls(
            source_system=reference.source_system,
            source_namespace=reference.source_namespace,
            kind=reference.kind,
            external_id=reference.external_id,
            classification=reference.classification,
            external_authority=reference.external_authority,
            revision=reference.revision,
            authorized_uri=reference.authorized_uri,
            observed_at=reference.observed_at,
            collision_key=reference.collision_key(),
        )


class SignalIngestRequest(BaseModel):
    """One observation a producer reports.

    `source_id` names a registered source rather than a system name: the
    registration is what carries the declared authority and the ingest ceiling,
    and a free-text name would name no registration at all.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID = Field(description="The registered source this observation arrives through.")
    source_system: str = Field(
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
        description="The external system's own name for itself. Folded to lowercase before the write.",
    )
    source_event_id: str = Field(
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
        description="That system's identifier for this occurrence. Trimmed, never case-folded.",
    )
    producer_id: str = Field(
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
        description="Who produced the observation, in the source's own id space.",
    )
    producer_type: ProducerTypeLiteral
    idempotency_key: str = Field(
        min_length=1,
        max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        description="This submission's own key. Distinct from source_event_id: one occurrence may be resubmitted.",
    )
    classification: ClassificationLiteral
    schema_version: str = Field(
        default=SIGNAL_SCHEMA_VERSION,
        description=f"The envelope contract version. `{SIGNAL_SCHEMA_VERSION}` today.",
    )
    event_time: datetime.datetime = Field(description="When the source says it happened. Timezone-aware.")
    observed_time: datetime.datetime = Field(description="When the producer learned of it. Timezone-aware.")
    references: list[SignalReference] = Field(
        default_factory=list,
        max_length=MAX_REFERENCES,
        description="The external work this observation is about. Empty is legal for a diagnostic observation.",
    )
    team_key: str | None = Field(default=None, min_length=1, description="Team scope, when the producer knows one.")
    project_key: str | None = Field(
        default=None, min_length=1, description="Project scope, when the producer knows one."
    )
    expires_at: datetime.datetime | None = Field(
        default=None,
        description="When this stops being usable as current. Absent is not the same as never expires.",
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description="The allowlisted projection. Exactly one of payload or evidence_handle.",
    )
    evidence_handle: str | None = Field(
        default=None,
        max_length=MAX_EVIDENCE_HANDLE_LENGTH,
        description="A handle to authorized evidence held elsewhere. Exactly one of payload or evidence_handle.",
    )

    @model_validator(mode="before")
    @classmethod
    def _refuse_server_assigned(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic hands the raw input, any JSON shape
        """Refuse a body that tried to set what this service decides.

        Runs before field validation, so the message names the reason rather than
        letting `extra="forbid"` report only that the field is not allowed here.
        A producer that believes it stamped the ingestion time and did not would
        otherwise reconcile two systems against a timestamp that means something
        else.
        """
        if isinstance(data, dict):
            reject_server_assigned(data)
        return data


class SignalIngestResponse(BaseModel):
    """What the caller is told once a submission is admitted or recognised.

    `replayed` is the field a client retrying a dropped response reads: a retry
    can tell it found the first write rather than making a second one. The status
    code says the same thing (201 created, 200 recognised); the field is here so
    a client that only reads the body still knows.
    """

    signal_id: uuid.UUID
    ingested_at: datetime.datetime = Field(description="Server-assigned. One clock read per submission.")
    authority: str = Field(description="Read off the source's declared policy, never off the request.")
    content_digest: str = Field(description="What makes a redelivery decidable without re-sending the body.")
    replayed: bool
    references: list[NormalizedSignalReference]

    @classmethod
    def of(cls, ingested: IngestedSignal) -> SignalIngestResponse:
        """The wire shape of one admitted or recognised submission."""
        return cls(
            signal_id=ingested.signal_id,
            ingested_at=ingested.ingested_at,
            authority=ingested.authority,
            content_digest=ingested.content_digest,
            replayed=ingested.replayed,
            references=[NormalizedSignalReference.of(reference) for reference in ingested.references],
        )


__all__ = [
    "ClassificationLiteral",
    "NormalizedSignalReference",
    "ProducerTypeLiteral",
    "SignalIngestRequest",
    "SignalIngestResponse",
    "SignalReference",
]
