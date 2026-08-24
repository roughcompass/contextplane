"""Pydantic request/response models for the memory-curation REST surface.

These are the wire contracts `api/routers/memory_curation.py` serves --
extracted out of the router so the route handlers and their adaptation
logic are not competing for space with thirty view-model definitions in the
same file. Every model is closed (`extra="forbid"`) for the same reason
`api/schemas/catalog.py` gives: a caller that misspells a field and has it
silently dropped believes an argument took effect when it did not.

None of the models below carry their own docstring, unlike this package's
other schema modules. They did not carry one in the router they moved from
either: Pydantic promotes a class docstring into the generated OpenAPI
component's `description`, and adding one now would grow the frozen
`openapi.json` snapshot on a change that is supposed to be move-only. See
this file's `D101` entry in `pyproject.toml` for the enforcement side of the
same reason.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Closed view models, request and response alike.

    A request field the caller misspelled and had silently dropped would
    look like it took effect when it did not; a response model left open
    could grow an undocumented field with nobody noticing the contract
    changed.
    """

    model_config = ConfigDict(extra="forbid")


# --- curation queue ---------------------------------------------------------


class QueueItemResponse(_Strict):
    claim_id: uuid.UUID
    reason: str
    subject_reference: str
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    confidence: float | None
    created_at: datetime.datetime
    human_backed: bool
    proposal_id: uuid.UUID | None
    available_actions: list[str]
    # Why this row sits where it does. The service has computed all three since
    # the ordering landed and dropped them here, which left the queue's own
    # explanation reachable only by reading its SQL. A rank a reviewer cannot
    # interrogate is a rank they learn to ignore.
    escalated: bool
    dependant_count: int
    sampling_priority: int


class QueueListResponse(_Strict):
    items: list[QueueItemResponse]
    next_cursor: str | None


class QueueCountsResponse(_Strict):
    counts: dict[str, int]


# --- link / discard ----------------------------------------------------------


class LinkClaimRequest(_Strict):
    subject_reference: str = Field(min_length=1)


class LinkedClaimResponse(_Strict):
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    status: str
    visibility: str
    owning_tenant_id: uuid.UUID | None
    source_authority: str
    is_contested: bool


class DiscardClaimRequest(_Strict):
    reason: str = Field(min_length=1)


class DiscardResponse(_Strict):
    status: str


# --- promotion proposals ------------------------------------------------------


class ProposalResponse(_Strict):
    proposal_id: uuid.UUID
    claim_id: uuid.UUID
    owner_tenant_id: uuid.UUID
    author_tenant_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    target_kind: str
    target_key: str
    current_value: Any
    proposed_value: Any
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    high_impact: bool
    high_impact_reasons: list[str]
    state: str
    created_at: datetime.datetime | None


class ProposalListResponse(_Strict):
    items: list[ProposalResponse]
    next_cursor: str | None


class ReviewProposalRequest(_Strict):
    state: Literal["accepted", "rejected"]
    # `None` and "not sent" are different: an accept with no amendment must
    # promote the claim's own proposed value, never a caller-shaped null.
    # `model_fields_set` on the parsed body is how the handler tells them
    # apart -- see `review_promotion_proposal` in the router.
    amended_value: Any = None
    reason: str | None = Field(default=None, min_length=1)


class ProposalDecisionResponse(_Strict):
    # Nested rather than flattened: `proposal` is always "the row's current
    # state", and `promotion_id` is always "the promotion this call itself
    # just created, or None" -- collapsing the two onto one flat model would
    # make a `null` on a later GET of the same shape ambiguous between "never
    # promoted" and "not asked about here".
    proposal: ProposalResponse
    promotion_id: uuid.UUID | None


# --- promotion reversal --------------------------------------------------------


class ReversePromotionRequest(_Strict):
    reason: str = Field(min_length=1)


class ReversePromotionResponse(_Strict):
    status: str


# --- confirmation --------------------------------------------------------------


class ConfirmationResponse(_Strict):
    claim_id: uuid.UUID
    confirms_claim_id: uuid.UUID
    source_authority: str
    confidence: float
    bucket: str
    hold_until: datetime.datetime


# --- adjudication ----------------------------------------------------------------


class AdjudicateClaimRequest(_Strict):
    verdict: Literal["correct", "incorrect", "undecidable"]
    observed_confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = Field(default=None, min_length=1)


class AdjudicateClaimResponse(_Strict):
    status: str


# --- claim history -------------------------------------------------------------


class BelievedClaimResponse(_Strict):
    claim_id: uuid.UUID
    predicate: str
    value: Any
    source_authority: str
    confidence: float | None
    bucket: str | None
    status: str
    superseded_by: uuid.UUID | None
    superseded_reason: str | None
    created_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None
    is_contested: bool
    was_current: bool


class ClaimHistoryResponse(_Strict):
    items: list[BelievedClaimResponse]


class BelievedClaimsResponse(_Strict):
    items: list[BelievedClaimResponse]


# --- capability requests -------------------------------------------------------


class CapabilityRequestResponse(_Strict):
    request_id: uuid.UUID
    owner_tenant_id: uuid.UUID
    requester_tenant_id: uuid.UUID
    subject_entity_id: uuid.UUID
    request_category: str
    title: str
    body: str
    status: str
    decision_reason: str | None
    resulting_promotion_id: uuid.UUID | None
    created_at: datetime.datetime


class CapabilityRequestListResponse(_Strict):
    items: list[CapabilityRequestResponse]
    next_cursor: str | None


class RequestTransitionResponse(_Strict):
    from_status: str
    to_status: str
    reason: str | None
    occurred_at: datetime.datetime


class RequestHistoryResponse(_Strict):
    items: list[RequestTransitionResponse]


class RaiseCapabilityRequestRequest(_Strict):
    subject_entity_id: uuid.UUID
    request_category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


class TransitionRequestRequest(_Strict):
    to_status: Literal["acknowledged", "accepted", "declined", "duplicate", "resolved"]
    reason: str | None = Field(default=None, min_length=1)


class LinkRequestToPromotionRequest(_Strict):
    promotion_id: uuid.UUID


class LinkRequestToPromotionResponse(_Strict):
    status: str


# --- direct claim assertion ----------------------------------------------------


class EvidenceItemRequest(_Strict):
    # The seven values here are the evidence_kind CHECK constraint on the
    # claim-provenance table (0001_baseline_schema.py), closed as a Literal
    # so a caller sending a kind that constraint would reject gets a 422 from
    # request validation instead of a raw database integrity error
    # surfacing as a 500.
    kind: Literal[
        "session_event",
        "document_revision",
        "commit",
        "work_item",
        "connector_run",
        "curator",
        "incident",
    ]
    ref: str = Field(min_length=1)
    excerpt: str | None = Field(default=None, min_length=1)


class AssertClaimRequest(_Strict):
    subject_reference: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: Any
    evidence: list[EvidenceItemRequest] = Field(min_length=1)
    asserted_valid_from: datetime.datetime | None = None
    asserted_valid_to: datetime.datetime | None = None
    visibility: Literal["public", "tenant-shared", "private"] | None = None
    namespace: str | None = None


class AssertClaimResponse(_Strict):
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    status: str
    visibility: str
    owning_tenant_id: uuid.UUID | None
    source_authority: str
    is_contested: bool


# --- disposition consequences -------------------------------------------------


class DispositionPolicyResponse(_Strict):
    disposition: str
    approval_authority: str
    evidence_threshold: str
    scope: str
    supersession: str
    rollback: str
    target_kind: str | None


class DispositionPolicyListResponse(_Strict):
    items: list[DispositionPolicyResponse]
