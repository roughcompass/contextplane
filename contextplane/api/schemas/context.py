"""Wire shapes for the context-resolve surface.

These mirror the frozen envelope contract rather than re-deciding any part of
it. `ContextBlockV1` already refuses a success block with no items, a degraded
block with no reason, and a non-canonical item without trust; restating those
rules here would create a second place they can drift, and the copy that drifts
is always the one nobody reads.

**The response is a projection, not a translation.** Every field below exists in
the contract object it came from, with the same name and the same meaning. Where
JSON cannot carry the contract's type -- a `ReceiptItemIdV1` is a triple that
knows how to digest itself -- the wire form carries *both* the digest a receipt
line quotes and the parts it was derived from, because a caller that can see
only the digest cannot check it.

**Block order is the contract's order, not the serializer's.** `BLOCK_NAMES`
fixes it, and the response is built by walking that tuple rather than by
iterating whatever the envelope happens to hold, so a reordering upstream is a
test failure here instead of a silently different response.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from contextplane.context.lifecycle import LIFECYCLE_REFERENCE_KINDS, normalize_reference_kind
from contextplane.context.schemas.envelope import (
    BLOCK_NAMES,
    ContextEnvelopeV1,
    ContextItemV1,
)
from contextplane.context.schemas.trust import (
    CLASSIFICATIONS,
    ExternalReferenceV1,
    QualityStateV1,
    TrustMetadataV1,
)

# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------

#: Bounds on what one request may ask for. The cap is here rather than in the
#: arms because it is a transport concern: an arm asked for ten thousand items
#: is a slow arm, and a caller who wants more should page rather than widen.
MAX_ARM_LIMIT = 200
DEFAULT_ARM_LIMIT = 25


class ExternalReferenceRequest(BaseModel):
    """A pointer to something the registry does not own.

    Mirrors `ExternalReferenceV1`. `classification` is required rather than
    defaulted: a reference whose sensitivity the caller did not state is one the
    server would have to guess about, and guessing low is the failure that
    matters.
    """

    source_system: str = Field(min_length=1)
    source_namespace: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    classification: str = Field(description=f"One of {sorted(CLASSIFICATIONS)}.")
    external_authority: str = Field(min_length=1, description="The authority in the external system, not ours.")
    revision: str | None = None
    authorized_uri: str | None = None
    observed_at: datetime.datetime | None = None

    def to_contract(self) -> ExternalReferenceV1:
        """The frozen contract object, which does its own validating."""
        return ExternalReferenceV1(
            source_system=self.source_system,
            source_namespace=self.source_namespace,
            kind=self.kind,
            external_id=self.external_id,
            classification=self.classification,  # type: ignore[arg-type]
            external_authority=self.external_authority,
            revision=self.revision,
            authorized_uri=self.authorized_uri,
            observed_at=self.observed_at,
        )


class ContextResolveRequest(BaseModel):
    """One context resolution.

    `arc_receipt_id` is optional and its absence is not an error. The ARC arm is
    receipt-anchored -- it serves what an attested resolution already selected --
    so a request naming no receipt gets a complete envelope whose ARC block is
    `empty`. `arc_block_note` on the response says which kind of empty it is,
    because "you named no receipt" and "that receipt selected nothing" are the
    same value with different meanings and only the first is the caller's to fix.
    """

    query: str = Field(min_length=1, description="What the caller is asking for.")
    arc_receipt_id: uuid.UUID | None = Field(
        default=None,
        description="An attested ARC resolution to serve. Omit it and the ARC block comes back empty, not failed.",
    )
    subject_entity_id: uuid.UUID | None = None
    task_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Tasks whose workspace material may be recalled, subject to the caller's participation.",
    )
    workspace_term: str | None = Field(default=None, description="Lexical term for the workspace arm.")
    workspace_reference: ExternalReferenceRequest | None = Field(
        default=None,
        description="Recall workspace material citing this external reference.",
    )
    lifecycle_references: list[ExternalReferenceRequest] = Field(
        default_factory=list,
        description=(
            "Where in a delivery lifecycle this request is being made, as references the registry "
            f"does not own. Each `kind` must be one of {list(LIFECYCLE_REFERENCE_KINDS)}. Context "
            "recorded as applying somewhere else is withheld and reported, never silently dropped. "
            "Stage is your own system's name for it: nothing here is stored, ordered, or advanced."
        ),
    )
    limit: int = Field(default=DEFAULT_ARM_LIMIT, ge=1, le=MAX_ARM_LIMIT)
    max_age_s: float | None = Field(
        default=None,
        gt=0,
        description="Treat arm results older than this as stale. Omit to accept any age.",
    )

    @field_validator("lifecycle_references")
    @classmethod
    def _kinds_are_in_the_closed_vocabulary(
        cls, references: list[ExternalReferenceRequest]
    ) -> list[ExternalReferenceRequest]:
        """Refuse an unknown lifecycle kind here, so the caller sees a 422.

        The check is `normalize_reference_kind`, not a copy of the set: the same
        function the profile and the control-plane translation enforce with. A
        second copy would be a second vocabulary, and two spellings that store
        cleanly and then fail to join is exactly what closing it prevents.

        Only this field. `workspace_reference` legitimately carries kinds from
        other subsystems -- ARC sources, checkpoints -- and narrowing it to the
        lifecycle vocabulary would reject references that were never in it.
        """
        for reference in references:
            normalize_reference_kind(reference.kind)
        return references


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class ReceiptItemIdResponse(BaseModel):
    """A receipt line's stable name, with the parts it was derived from.

    Both, deliberately. The digest is what a receipt quotes; the parts are what
    make it checkable. A response carrying only the digest asks the caller to
    trust an opaque string, which is the opposite of what a receipt is for.
    """

    value: str = Field(description="The digest a receipt line carries verbatim.")
    block: str
    source: str
    item_key: str


class TrustResponse(BaseModel):
    """All eight trust labels. Complete or absent, never partial.

    Present for every non-canonical item and absent for every canonical one --
    the contract enforces that, and this shape carries whichever it was given.
    """

    trust: str
    source: str
    assertion_kind: str
    authority: str
    freshness: datetime.datetime | None
    mutability: str
    attribution: str | None
    classification: str

    @classmethod
    def of(cls, trust: TrustMetadataV1) -> TrustResponse:
        """Project one trust record onto the wire, label for label."""
        return cls(
            trust=trust.trust,
            source=trust.source,
            assertion_kind=trust.assertion_kind,
            authority=trust.authority,
            freshness=trust.freshness,
            mutability=trust.mutability,
            attribution=trust.attribution,
            classification=trust.classification,
        )


class ContextItemResponse(BaseModel):
    """One piece of context and everything needed to weigh it."""

    receipt_item_id: ReceiptItemIdResponse
    payload: dict[str, Any]
    trust: TrustResponse | None = None

    @classmethod
    def of(cls, item: ContextItemV1) -> ContextItemResponse:
        """Project one item, carrying its id's digest and the parts behind it."""
        return cls(
            receipt_item_id=ReceiptItemIdResponse(
                value=item.receipt_item_id.value(),
                block=item.receipt_item_id.block,
                source=item.receipt_item_id.source,
                item_key=item.receipt_item_id.item_key,
            ),
            payload=dict(item.payload),
            trust=TrustResponse.of(item.trust) if item.trust is not None else None,
        )


class ContextBlockResponse(BaseModel):
    """One arm of the answer.

    `state` is carried, never inferred. A reader who computes it from whether
    `items` is empty gets the one case that matters wrong: a failed arm has no
    items either, and treating it as empty reads a broken arm as a quiet one.
    """

    name: str
    state: str = Field(description="One of success, empty, degraded, failed.")
    items: list[ContextItemResponse] = Field(default_factory=list)
    reason: str | None = Field(
        default=None,
        description="Why the arm did not fully succeed. Always present when degraded or failed.",
    )


class QualityResponse(BaseModel):
    """What the caller should do about the response as a whole."""

    degraded_blocks: list[str]
    reasons: list[str]
    cacheable: bool

    @classmethod
    def of(cls, quality: QualityStateV1) -> QualityResponse:
        """Project the quality record, degraded blocks paired with their reasons."""
        return cls(
            degraded_blocks=list(quality.degraded_blocks),
            reasons=list(quality.reasons),
            cacheable=quality.cacheable,
        )


class ContextEnvelopeResponse(BaseModel):
    """Exactly four blocks, quality, state, and the receipt that records them.

    `receipt_id` is not decorative and is not optional. It names the stored
    record of this resolution, which is what makes the answer auditable later --
    and it is the id `GET /v1/receipts/{receipt_id}` takes.
    """

    state: str = Field(description="One of complete, degraded, blocked.")
    blocks: list[ContextBlockResponse] = Field(description=f"Exactly {list(BLOCK_NAMES)}, in that order.")
    quality: QualityResponse
    receipt_id: uuid.UUID
    arc_block_note: str | None = Field(
        default=None,
        description=(
            "Set when the ARC block is empty because the request named no receipt, "
            "which is a caller-fixable emptiness rather than an absence of results."
        ),
    )

    @classmethod
    def of(
        cls,
        envelope: ContextEnvelopeV1,
        *,
        receipt_id: uuid.UUID,
        arc_block_note: str | None = None,
    ) -> ContextEnvelopeResponse:
        """Project one envelope onto the wire.

        Blocks are walked in `BLOCK_NAMES` order via `envelope.block()` rather
        than by iterating `envelope.blocks`, so the response order is the
        contract's and a reordering upstream fails a test here instead of
        quietly changing what callers receive.
        """
        return cls(
            state=envelope.state,
            blocks=[
                ContextBlockResponse(
                    name=name,
                    state=envelope.block(name).state,
                    items=[ContextItemResponse.of(item) for item in envelope.block(name).items],
                    reason=envelope.block(name).reason,
                )
                for name in BLOCK_NAMES
            ],
            quality=QualityResponse.of(envelope.quality),
            receipt_id=receipt_id,
            arc_block_note=arc_block_note,
        )


__all__ = [
    "DEFAULT_ARM_LIMIT",
    "MAX_ARM_LIMIT",
    "ContextBlockResponse",
    "ContextEnvelopeResponse",
    "ContextItemResponse",
    "ContextResolveRequest",
    "ExternalReferenceRequest",
    "QualityResponse",
    "ReceiptItemIdResponse",
    "TrustResponse",
]
