"""Wire shapes for the receipt and resume surfaces.

These mirror what the services already decided. The interesting one is
`ResumeResponse`: it has to carry three outcomes that a caller acts on
differently -- resumed, nothing to resume, and the request named work belonging
to more than one task -- and collapsing any two of them into "empty" is what
sends an agent off to start work that already exists.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field


class ReceiptResponse(BaseModel):
    """One stored resolution."""

    receipt_id: uuid.UUID
    intent_id: uuid.UUID | None
    state: str
    cacheable: bool
    resolved_at: datetime.datetime
    requested_by: str
    request_digest: str | None


class ReceiptListResponse(BaseModel):
    """Receipts citing one piece of external work, newest first."""

    receipts: list[ReceiptResponse]


class ExclusionResponse(BaseModel):
    """One item a resolution found and did not return.

    Surfaced rather than kept internal: the difference between "there was
    nothing" and "there was something you may not see" is the whole reason the
    row is stored, and a reader who never sees it cannot act on it.
    """

    block: str
    item_key: str
    reason: str


class ExclusionListResponse(BaseModel):
    """Everything one resolution withheld, or an empty list if it withheld nothing."""

    exclusions: list[ExclusionResponse]


class ReferenceResponse(BaseModel):
    """One piece of external work a receipt cites."""

    source_system: str
    source_namespace: str
    kind: str
    external_id: str
    classification: str


class ReferenceListResponse(BaseModel):
    """Every piece of external work one resolution cites."""

    references: list[ReferenceResponse]


class ResumeRequestBody(BaseModel):
    """The work a caller is picking up.

    References are `system/namespace/kind/external_id` tuples because that is
    what a pipeline holds: its own run id, the pull request it is working on.
    There is deliberately no transcript flag -- see `context/resume.py`.
    """

    references: list[tuple[str, str, str, str]] = Field(min_length=1)
    checkpoint_bound: int | None = Field(default=None, ge=1)
    receipt_bound: int | None = Field(default=None, ge=1)
    reference_bound: int | None = Field(default=None, ge=1)
    feedback_bound: int | None = Field(default=None, ge=1)
    learning_bound: int | None = Field(default=None, ge=1)


class ResumeCheckpointResponse(BaseModel):
    """One recorded step, as resume returns it. Conclusions, never an exchange."""

    checkpoint_id: uuid.UUID
    sequence: int
    goal: str
    open_questions: list[str]
    next_action: str | None
    recorded_at: datetime.datetime


class ResumeFeedbackResponse(BaseModel):
    """A minimized verdict; reporter identity and free text stay private."""

    feedback_id: uuid.UUID
    kind: str
    receipt_id: uuid.UUID
    receipt_item_id: str | None
    rating: str
    learning_eligible: bool
    created_at: datetime.datetime
    consumed: bool


class ResumeCitationResponse(BaseModel):
    """A resolvable handle to evidence supporting newer learning."""

    kind: str
    ref: str
    excerpt: str | None = None


class ResumeLearningResponse(BaseModel):
    """A reviewed claim with the governed serving path's trust contract."""

    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    value: Any
    claim_category: str
    confidence: float
    authority: str
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    as_of: datetime.datetime
    human_confirmed: bool
    citations: list[ResumeCitationResponse]
    label: str
    trust: str
    trust_note: str


class ResumeResponse(BaseModel):
    """Everything needed to carry on, and which of three answers this is.

    `status` is explicit rather than inferred from empty fields. A caller that
    has to work out whether an empty checkpoint list means "new work", "you may
    not see it" or "you named two tasks" will get that wrong, and the wrong
    branch starts work that already exists.
    """

    status: str = Field(description="One of `resumed`, `empty`, `ambiguous`.")
    intent_id: uuid.UUID | None
    head_checkpoint_id: uuid.UUID | None
    head_sequence: int | None
    head_summary: str | None
    checkpoints: list[ResumeCheckpointResponse]
    receipts: list[ReceiptResponse]
    references: list[ReferenceResponse]
    open_questions: list[str]
    next_action: str | None
    feedback: list[ResumeFeedbackResponse]
    learning: list[ResumeLearningResponse]
    truncated: list[str]
    #: Populated only when `status` is `ambiguous`: the tasks to choose between.
    ambiguous_intent_ids: list[uuid.UUID] = Field(default_factory=list)


__all__ = [
    "ExclusionListResponse",
    "ExclusionResponse",
    "ReceiptListResponse",
    "ReceiptResponse",
    "ReferenceListResponse",
    "ReferenceResponse",
    "ResumeCitationResponse",
    "ResumeCheckpointResponse",
    "ResumeFeedbackResponse",
    "ResumeLearningResponse",
    "ResumeRequestBody",
    "ResumeResponse",
]
