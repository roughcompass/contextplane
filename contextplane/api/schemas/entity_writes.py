"""Request and response shapes for the generic entity write surface.

The request envelope itself is not redefined here — it is
`contextplane.api.schemas.profile_writes.ProfileWriteRequestV1`, shared with the
relationship surface so that one intent vocabulary and one authority refusal serve
both. This module adds only what is specific to entities: the narrowing that a
body arriving on `/v1/entities` describes an entity, and the response.

**The response says what happened to the write, not just that it succeeded.** The
three intents produce three different effects — a staged claim, an owner-review
entry, a canonical assertion — and a caller that sent an observation and got back
`201 Created` with an entity id would reasonably conclude its observation was now
canonical fact. So `effect` is on every response, and the identifier it carries is
named for what was actually created.

**Reads carry the governance, not only the values.** A generic reader has no other
way to find out which profile revision validated a row, which authority asserted
it, or whether it is ready — and those are exactly the questions that make a
generic surface safe to build on. Leaving them to a second call would mean every
careful caller makes two, and every careless one makes none.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextplane.api.schemas.profile_writes import ProfileWriteRequestV1
from contextplane.entities.write_intent import (
    EFFECT_CANONICAL_ASSERTION_WRITE,
    EFFECT_OWNER_REVIEW_ENTRY,
    EFFECT_STAGED_CLAIM,
    ProfileWriteIntent,
)

#: The response code a caller gets when an unqualified handle matches more than
#: one type. Loud and specific on purpose: the alternative is picking one, which
#: silently attaches a write to whichever type happened to sort first.
IDENTITY_AMBIGUOUS = "identity_ambiguous"

#: The three effects a generic write can have, as the response reports them.
_EFFECTS = frozenset({EFFECT_STAGED_CLAIM, EFFECT_OWNER_REVIEW_ENTRY, EFFECT_CANONICAL_ASSERTION_WRITE})


class EntityWriteRequestV1(ProfileWriteRequestV1):
    """A generic write whose subject is an entity.

    Narrows `subject_kind` rather than dropping it. The field stays in the body so
    one envelope serialises for both surfaces and a client library does not need a
    different shape per path — but a body arriving here saying `relationship` is a
    caller who has the wrong URL, and being told so beats having the field quietly
    ignored.
    """

    subject_kind: Literal["entity"] = "entity"


class EntityIdentityV1(BaseModel):
    """How this entity is named, qualified by its type.

    The namespace and type accompany the name because a bare name is not an
    identity in a profile-governed graph: two types may legitimately carry the
    same name, and a reader holding only the name cannot tell which it has.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None = None


class ProfileAttributionV1(BaseModel):
    """Which governance accepted this row."""

    model_config = ConfigDict(extra="forbid")

    profile_revision_id: uuid.UUID | None
    binding_id: uuid.UUID | None
    #: `unbound` when the tenant has adopted no profile — a real state, and one a
    #: caller needs to be able to see rather than infer from nulls.
    enforcement_mode: str


class ProvenanceSummaryV1(BaseModel):
    """Who asserted this and how far it may be trusted.

    A summary rather than the whole provenance row: the fields here are the ones a
    reader needs to decide whether to act on the value. `confidence` is present
    only for a derived assertion, mirroring the rule the provenance record itself
    enforces.
    """

    model_config = ConfigDict(extra="forbid")

    authority: str | None = None
    freshness_state: str | None = None
    source_system: str | None = None
    external_record_id: str | None = None
    external_revision: str | None = None
    confidence: float | None = None


class ValidationOutcomeV1(BaseModel):
    """What the profile said about this write.

    `violations` can be non-empty on a successful write: that is what an advisory
    binding means. A caller reading only the status code would miss it, so the
    list travels with the row.
    """

    model_config = ConfigDict(extra="forbid")

    valid: bool
    mode: str
    violations: list[str] = Field(default_factory=list)
    truncated: bool = False


class TemporalStateV1Out(BaseModel):
    """When this row is in force."""

    model_config = ConfigDict(extra="forbid")

    effective_from: datetime.datetime | None = None
    effective_to: datetime.datetime | None = None
    recorded_at: datetime.datetime | None = None


class EntityWriteResultV1(BaseModel):
    """What a generic write did, named for the effect it actually had.

    `entity_id` is populated only for a canonical write. An observation produces a
    staged claim and a request produces a review entry; returning an entity id for
    either would tell the caller its value is now in the graph when it is waiting
    for somebody to agree.
    """

    model_config = ConfigDict(extra="forbid")

    intent: ProfileWriteIntent
    #: One of the three effects. Typed as `str` rather than a `Literal` of the
    #: three constants because those are module-level names, not literal
    #: expressions -- the validator below is what actually constrains it, and it
    #: constrains more than membership.
    effect: str
    #: The id of whatever was created, under the name of what it is.
    entity_id: uuid.UUID | None = None
    staged_claim_id: uuid.UUID | None = None
    review_entry_id: uuid.UUID | None = None
    validation: ValidationOutcomeV1
    profile: ProfileAttributionV1

    @model_validator(mode="after")
    def _identifier_matches_effect(self) -> Self:
        """Exactly one identifier, and it must be the one the effect implies.

        Checked on the response rather than trusted from the handler: a route that
        set the wrong field would produce a body that reads as a canonical write
        having happened, and no caller could tell.
        """
        if self.effect not in _EFFECTS:
            raise ValueError(f"unknown effect {self.effect!r}; legal: {', '.join(sorted(_EFFECTS))}")
        expected: dict[str, uuid.UUID | None] = {
            EFFECT_CANONICAL_ASSERTION_WRITE: self.entity_id,
            EFFECT_STAGED_CLAIM: self.staged_claim_id,
            EFFECT_OWNER_REVIEW_ENTRY: self.review_entry_id,
        }
        if expected[self.effect] is None:
            raise ValueError(f"a {self.effect!r} result carries the identifier of what it created")
        others = [value for effect, value in expected.items() if effect != self.effect]
        if any(value is not None for value in others):
            raise ValueError(
                f"a {self.effect!r} result carries only its own identifier; another would tell a caller "
                "something was written that was not"
            )
        return self


class EntityReadV1(BaseModel):
    """One entity with the governance a generic reader needs to act on it."""

    model_config = ConfigDict(extra="forbid")

    identity: EntityIdentityV1
    properties: dict[str, Any] = Field(default_factory=dict)
    profile: ProfileAttributionV1
    provenance: ProvenanceSummaryV1
    validation: ValidationOutcomeV1
    temporal: TemporalStateV1Out
    readiness_state: str


class EntityResolutionV1(BaseModel):
    """The result of a handle lookup."""

    model_config = ConfigDict(extra="forbid")

    identity: EntityIdentityV1


class ReadinessReportV1(BaseModel):
    """Whether this entity's required relationships are present.

    `blocking` names what is missing rather than only counting it: a caller told
    "not ready" with no list has to guess, and guessing is how a caller ends up
    asserting relationships the profile never asked for.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: uuid.UUID
    readiness_state: str
    blocking: list[str] = Field(default_factory=list)


EntityIdPath = Annotated[uuid.UUID, Field(description="The entity's identifier.")]


__all__ = [
    "IDENTITY_AMBIGUOUS",
    "EntityIdPath",
    "EntityIdentityV1",
    "EntityReadV1",
    "EntityResolutionV1",
    "EntityWriteRequestV1",
    "EntityWriteResultV1",
    "ProfileAttributionV1",
    "ProvenanceSummaryV1",
    "ReadinessReportV1",
    "TemporalStateV1Out",
    "ValidationOutcomeV1",
]
