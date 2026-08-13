"""Request and response shapes for the generic relationship write surface.

The request envelope is `profile_writes.ProfileWriteRequestV1`, shared with the
entity surface: one intent vocabulary, one authority refusal, two subjects. This
module adds the narrowing to `relationship`, the endpoints a relationship write
needs and the entity one does not, and the read shape.

**A relationship names two endpoints, and both are required at write time.** The
entity envelope's `identity` addresses one subject; an edge is not a subject in
that sense, so the endpoints travel beside it rather than inside it. Making them
required here rather than defaulting either to the identity means a caller cannot
accidentally assert a self-loop by omission — the shape that makes a closure walk
non-terminating.

**The response reports the effect, as the entity surface does, and for the same
reason.** An observation that came back looking like a canonical write would tell
a caller its edge is in the graph while it waits for somebody to agree.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextplane.api.schemas.entity_writes import (
    ProfileAttributionV1,
    ProvenanceSummaryV1,
    TemporalStateV1Out,
    ValidationOutcomeV1,
)
from contextplane.api.schemas.profile_writes import ProfileWriteRequestV1
from contextplane.entities.write_intent import (
    EFFECT_CANONICAL_ASSERTION_WRITE,
    EFFECT_OWNER_REVIEW_ENTRY,
    EFFECT_STAGED_CLAIM,
    ProfileWriteIntent,
)

_EFFECTS = frozenset({EFFECT_STAGED_CLAIM, EFFECT_OWNER_REVIEW_ENTRY, EFFECT_CANONICAL_ASSERTION_WRITE})

#: The ceiling on one page of a relationship query. A traversal with no bound is
#: a request whose cost the caller cannot predict and the server cannot refuse.
MAX_PAGE_SIZE = 200


class RelationshipEndpointsV1(BaseModel):
    """The two entities an edge joins, in the stored direction."""

    model_config = ConfigDict(extra="forbid")

    source_entity_id: uuid.UUID
    destination_entity_id: uuid.UUID


class RelationshipWriteRequestV1(ProfileWriteRequestV1):
    """A generic write whose subject is a relationship."""

    subject_kind: Literal["relationship"] = "relationship"
    endpoints: RelationshipEndpointsV1


class RelationshipWriteResultV1(BaseModel):
    """What a generic relationship write did, named for the effect it had."""

    model_config = ConfigDict(extra="forbid")

    intent: ProfileWriteIntent
    effect: str
    relationship_id: uuid.UUID | None = None
    staged_claim_id: uuid.UUID | None = None
    review_entry_id: uuid.UUID | None = None
    readiness_state: str | None = None
    validation: ValidationOutcomeV1
    profile: ProfileAttributionV1

    @model_validator(mode="after")
    def _identifier_matches_effect(self) -> Self:
        """Exactly one identifier, and the one the effect implies."""
        if self.effect not in _EFFECTS:
            raise ValueError(f"unknown effect {self.effect!r}; legal: {', '.join(sorted(_EFFECTS))}")
        expected: dict[str, uuid.UUID | None] = {
            EFFECT_CANONICAL_ASSERTION_WRITE: self.relationship_id,
            EFFECT_STAGED_CLAIM: self.staged_claim_id,
            EFFECT_OWNER_REVIEW_ENTRY: self.review_entry_id,
        }
        if expected[self.effect] is None:
            raise ValueError(f"a {self.effect!r} result carries the identifier of what it created")
        if any(value is not None for effect, value in expected.items() if effect != self.effect):
            raise ValueError(
                f"a {self.effect!r} result carries only its own identifier; another would tell a caller "
                "something was written that was not"
            )
        return self


class RelationshipReadV1(BaseModel):
    """One governed relationship with the governance a reader needs.

    `is_inverse` travels with the row because an inverse is the same stored fact
    read from the other end. A caller that treated one as a second edge would be
    double-counting, and nothing else in the body would say which it held.
    """

    model_config = ConfigDict(extra="forbid")

    relationship_id: uuid.UUID
    relationship_type: str
    endpoints: RelationshipEndpointsV1
    properties: dict[str, Any] = Field(default_factory=dict)
    profile: ProfileAttributionV1
    provenance: ProvenanceSummaryV1
    validation: ValidationOutcomeV1
    temporal: TemporalStateV1Out
    readiness_state: str
    is_inverse: bool = False


class RelationshipQueryV1(BaseModel):
    """A bounded traversal from one entity.

    `direction` chooses the stored direction or the derived inverse view; there is
    no "both", because a page mixing the two would have no stable order and a
    caller could not tell which half it had.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: uuid.UUID
    direction: Literal["outgoing", "incoming"] = "outgoing"
    relationship_type: str | None = None
    at: datetime.datetime | None = None
    limit: int = Field(default=50, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


class RelationshipPageV1(BaseModel):
    """One page of a traversal, with what a caller needs to ask for the next."""

    model_config = ConfigDict(extra="forbid")

    items: list[RelationshipReadV1]
    limit: int
    offset: int
    #: True when the underlying result had at least one more row than this page.
    #: A total count would be a second query over a window that may move between
    #: the two, and a caller paging forward does not need one.
    has_more: bool


__all__ = [
    "MAX_PAGE_SIZE",
    "RelationshipEndpointsV1",
    "RelationshipPageV1",
    "RelationshipQueryV1",
    "RelationshipReadV1",
    "RelationshipWriteRequestV1",
    "RelationshipWriteResultV1",
]
