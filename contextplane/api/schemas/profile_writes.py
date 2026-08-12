"""The one body every generic profile mutation arrives in.

Both generic write surfaces -- entities and relationships -- take the same
envelope, so the shape is defined once here rather than twice in the places
that receive it. Two surfaces that each defined their own would drift on the
field that matters least right up until the day it was the field that mattered.

The envelope is closed. An unrecognised field is refused rather than dropped,
because a dropped field is indistinguishable from one the server understood and
acted on: the caller sees a `2xx` either way and has no way to learn that half
its request was ignored.

The envelope carries what the caller *observed* and never what the platform
*concluded*. That line is drawn by name, in the intent module's reserved set,
so a request that tries to state its own trust class, validation outcome or
approver is refused with the reason rather than as an unknown field. The one
authority-adjacent thing a caller may send is the reference to an approval it
believes it holds -- and the service re-resolves that reference rather than
trusting it.

`intent` has no default here, which is the shape's most load-bearing property:
pydantic refuses the body outright when it is missing, so no surface downstream
ever has to decide what an intent-less write meant.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from contextplane.entities.write_intent import (
    INTENT_AUTHORIZED_APPROVAL,
    PROFILE_WRITE_INTENTS,
    ProfileWriteIntent,
    RefusedProfileWrite,
    refuse_caller_asserted_authority,
)

# `<namespace>:<entity_type>/<name>`. Names may repeat across types, which is
# exactly why the type is inside the handle rather than inferred from the name.
QualifiedHandle = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*/[^\s/][^\s]*$")]

NonBlank = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class _ClosedModel(BaseModel):
    """Every component of the write envelope forbids unknown fields.

    Stated once here rather than as a `model_config` line on each class below,
    which is one line each class could individually forget.
    """

    model_config = ConfigDict(extra="forbid")


class _NoAssertedAuthorityModel(_ClosedModel):
    """A component whose field names are additionally checked against the
    reserved set, so the refusal names the reason instead of the field.

    `extra="forbid"` alone would already reject these, but as "unknown field" --
    a message that reads like an oversight and invites somebody to add the
    field. The platform-derived fields are refused on their own terms.
    """

    @model_validator(mode="before")
    @classmethod
    def _refuse_asserted_authority(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic hands the raw input, any JSON shape
        if isinstance(data, dict):
            refuse_caller_asserted_authority(data, where=cls.__name__)
        return data


class ProfileWriteIdentityV1(_NoAssertedAuthorityModel):
    """Which thing is being written about: a stable id, a qualified handle, or both.

    Both may be sent together, which is how a caller holding an id it read from
    an earlier response can still say what it believes that id refers to.
    Whether the two agree is not decidable here -- it takes a lookup -- so it is
    the resolver's to answer; this shape only guarantees it was asked something.
    Neither being present is refused: a write about nothing in particular is a
    write whose subject the server would otherwise have to pick.
    """

    subject_id: uuid.UUID | None = None
    handle: QualifiedHandle | None = None

    @model_validator(mode="after")
    def _needs_a_subject(self) -> Self:
        if self.subject_id is None and self.handle is None:
            raise ValueError(
                "a write names its subject by stable id, by qualified handle, or by both; naming neither "
                "leaves the server to choose what the assertion is about"
            )
        return self


class TargetRevisionV1(_ClosedModel):
    """The profile revision the caller wrote against.

    Sent by the caller rather than assumed from whatever is currently bound,
    because a body composed against one revision and validated against another
    passes or fails for reasons the caller cannot see.
    """

    profile_revision: NonBlank
    binding_revision: NonBlank | None = None


class TemporalStateV1(_ClosedModel):
    """When the assertion is claimed to hold, in the world rather than in the store."""

    valid_from: dt.datetime
    valid_to: dt.datetime | None = None

    @model_validator(mode="after")
    def _interval_moves_forward(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError(
                "a validity interval ends after it starts; an inverted or empty one asserts a fact that was "
                "never true and would still be stored and read back as one"
            )
        return self


class AssertionProvenanceInputV1(_NoAssertedAuthorityModel):
    """Where the assertion came from, as the caller can attest it.

    The caller's half of the provenance record: the source it read, the times
    it observed, and how it derived the value if it derived one. The platform's
    half -- trust class, validating revision, ingest time, approver -- is added
    on the server, and is refused here by name. Provenance changing must never
    silently change trust, and it cannot if the trust half never arrives in a
    request at all.

    Missing provenance blocks the write, which is why every field that
    identifies the source is required rather than defaulted to an empty string:
    a blank source system passes an "is it set" check and tells a later reader
    nothing about where the value came from.
    """

    source_system: NonBlank
    source_namespace: NonBlank
    external_record_id: NonBlank
    external_record_revision: NonBlank | None = None
    # When the thing happened, versus when this caller saw it. Both are the
    # caller's to state; when the platform took delivery is not.
    event_time: dt.datetime | None = None
    observed_time: dt.datetime
    derivation_method: NonBlank | None = None
    derivation_profile: NonBlank | None = None
    # Only meaningful for a value nobody read directly. On a value read straight
    # from a source, a confidence score is a number with nothing behind it that
    # later ranking would nonetheless weight.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _confidence_only_when_derived(self) -> Self:
        if self.confidence is not None and self.derivation_method is None:
            raise ValueError(
                "confidence accompanies a derivation method; a score on a directly-read value quantifies "
                "nothing and would still be read downstream as though it did"
            )
        return self


class ProfileWriteRequestV1(_NoAssertedAuthorityModel):
    """The generic write envelope, identical for entities and relationships.

    `intent` is first and required. `approval_reference` is present on exactly
    the approval intent: sending one on any other intent is a caller describing
    a review that did not happen, and omitting it on the approval intent leaves
    the service nothing to re-resolve.
    """

    intent: ProfileWriteIntent
    subject_kind: Literal["entity", "relationship"]
    subject_type: NonBlank
    identity: ProfileWriteIdentityV1
    target_revision: TargetRevisionV1
    temporal: TemporalStateV1
    idempotency_key: NonBlank
    provenance: AssertionProvenanceInputV1
    # The typed values the profile defines. Deliberately *not* screened against
    # the reserved authority names: a profile is free to define a property
    # called `authority`, and inside this bag it means whatever that profile
    # says it means. It is a value being asserted about the subject, never a
    # statement about what the platform concluded.
    properties: dict[str, Any] = Field(default_factory=dict)
    approval_reference: NonBlank | None = None

    @model_validator(mode="after")
    def _approval_reference_matches_intent(self) -> Self:
        if self.intent == INTENT_AUTHORIZED_APPROVAL:
            if self.approval_reference is None:
                raise ValueError(
                    "an approval write names the approval it passed; the service re-resolves that reference "
                    "and has nothing to resolve without it"
                )
        elif self.approval_reference is not None:
            raise ValueError(
                f"only the {INTENT_AUTHORIZED_APPROVAL!r} intent carries an approval reference; on the "
                f"{self.intent!r} route it asserts a review that did not happen"
            )
        return self


__all__ = [
    "PROFILE_WRITE_INTENTS",
    "AssertionProvenanceInputV1",
    "ProfileWriteIdentityV1",
    "ProfileWriteRequestV1",
    "QualifiedHandle",
    "RefusedProfileWrite",
    "TargetRevisionV1",
    "TemporalStateV1",
]
