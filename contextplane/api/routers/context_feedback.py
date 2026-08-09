"""Feedback over HTTP: one route, three shapes, one contract.

    POST /v1/context/feedback → ContextFeedbackResponse (201 created | 200 recognised)

This router adapts and does not decide. Which shapes are legal, what a rating may
be, whether a receipt is the caller's, whether an item is on it, and whether a
resubmission is a replay or a conflict all live in `signals/feedback.py`, because
the MCP surface answers the same questions and a rule enforced in two adapters is
one that will eventually be enforced differently in one of them.

**The request models live here rather than in `api/schemas/`.** They describe this
one route's body and nothing else reads them; the service's own dataclass is the
shape the domain speaks. Keeping them beside the route means the translation from
wire JSON to domain object is visible in one file instead of two.

**201 or 200, and the difference is real.** Feedback this call stored answers 201;
a submission it recognised as already stored answers 200 with the same body. A
client retrying a dropped response can tell that its retry found the first write
rather than filing a second complaint.

**Idempotency is a body field, not a header**, for the same reason the signal
surface makes that choice: the key is part of what the reporter submits and part
of what the ledger enforces a unique index on. A header would give one submission
two identities that could disagree, and a proxy that drops it would turn a retry
into a duplicate report.

**Refusals say what the caller should do.** A receipt that is not the caller's and
one that does not exist both answer 404 with the same message — distinguishing
them would turn a receipt id into a cross-tenant existence oracle. An item that is
not on an authorized receipt also answers 404, but with its own message, because
by then the caller has been authorized for the receipt and there is nothing left to
leak. A shape or vocabulary violation answers 422: the caller sent something no
retry will fix. A key reused for different content answers 409 under its own code,
so a client can stop retrying rather than reading it as a generic conflict.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import build_error, map_catalog_error
from contextplane.auth.roles import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.signals.feedback import (
    FeedbackService,
    FeedbackSubmissionV1,
    RecordedFeedback,
)
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1", tags=["context-feedback"])

# Reporting feedback is a write, so it needs a write role. `consumer` is first
# among them because a consumer telling us an answer was wrong is the ordinary
# case this surface exists for; requiring `producer` would leave the people who
# actually receive the answers unable to say anything about them.
_feedback_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN])


class ContextFeedbackRequest(BaseModel):
    """One report about a served answer.

    `kind` decides which of `receipt_id` and `receipt_item_id` are required and
    which are forbidden; the service enforces that union and returns 422 naming
    the violation. They are optional here because two of the three shapes omit
    them, not because any combination is acceptable.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="One of item_specific, receipt_level, diagnostic_observation.")
    rating: str = Field(description="A verdict from the bounded vocabulary.")
    reporter_id: str = Field(min_length=1, description="Who is reporting. Must be the caller unless external.")
    reporter_type: str = Field(description="One of human, agent, external.")
    idempotency_key: str = Field(min_length=1, description="This submission's key. Replays carry it verbatim.")

    receipt_id: uuid.UUID | None = Field(default=None, description="The resolution this is about.")
    receipt_item_id: str | None = Field(default=None, description="The exact item on that receipt.")
    note: str | None = Field(default=None, description="Free text. Minimized before the structured fields expire.")
    learning_eligible: bool = Field(
        default=True,
        description=(
            "Whether this may be used as learning or evaluation evidence. May be lowered by the reporter; "
            "always false for a diagnostic observation, which cites nothing that could be checked."
        ),
    )


class ContextFeedbackResponse(BaseModel):
    """What was stored, and whether this call is what stored it."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: uuid.UUID
    kind: str
    rating: str
    learning_eligible: bool
    receipt_id: uuid.UUID | None
    receipt_item_id: str | None
    content_digest: str
    created_at: str
    replayed: bool = Field(
        description="True when this call recognised an existing submission rather than storing a new one."
    )

    @classmethod
    def of(cls, recorded: RecordedFeedback) -> ContextFeedbackResponse:
        """Build the wire body from what the service recorded."""
        return cls(
            feedback_id=recorded.feedback_id,
            kind=recorded.kind,
            rating=recorded.rating,
            learning_eligible=recorded.learning_eligible,
            receipt_id=recorded.receipt_id,
            receipt_item_id=recorded.receipt_item_id,
            content_digest=recorded.content_digest,
            created_at=recorded.created_at.isoformat(),
            replayed=recorded.replayed,
        )


def _feedback_service(container: Services) -> FeedbackService:
    """Build the service from what the container already publishes."""
    return FeedbackService(container.session_factory, clock=container.clock)


@router.post("/context/feedback", response_model=ContextFeedbackResponse)
async def record_context_feedback(
    body: ContextFeedbackRequest,
    ctx: Annotated[TenantContext, Depends(_feedback_required)],
    container: Annotated[Services, Depends(services)],
    response: Response,
) -> ContextFeedbackResponse:
    """Report feedback about a served answer, bound to exactly what it is about.

    Item-specific feedback cites a receipt and an exact item on it; receipt-level
    cites a receipt and no item; a diagnostic observation cites neither and is
    never learning-eligible. The binding is resolved against the receipt's own
    rows before anything is written, so an item belonging to a different receipt
    is refused rather than stored.

    Nothing is inferred: no rating is derived from an external outcome, and a
    submission that is refused leaves no row behind.
    """
    submission = FeedbackSubmissionV1(
        kind=body.kind,
        rating=body.rating,
        reporter_id=body.reporter_id,
        reporter_type=body.reporter_type,
        idempotency_key=body.idempotency_key,
        receipt_id=body.receipt_id,
        receipt_item_id=body.receipt_item_id,
        note=body.note,
        learning_eligible=body.learning_eligible,
    )
    try:
        recorded = await _feedback_service(container).record(ctx, submission)
    except ConflictError as exc:
        # Named ahead of the generic translator: `map_catalog_error` would give
        # this the right status under the generic `conflict` code, which a client
        # cannot tell from any other 409 on any other surface. A reused key
        # carrying different content is the one conflict this route produces, and
        # naming it is what lets a client stop retrying.
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message=str(exc),
        ) from exc
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc

    response.status_code = status.HTTP_200_OK if recorded.replayed else status.HTTP_201_CREATED
    return ContextFeedbackResponse.of(recorded)


__all__ = ["ContextFeedbackRequest", "ContextFeedbackResponse", "router"]
