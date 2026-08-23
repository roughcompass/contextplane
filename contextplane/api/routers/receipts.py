"""Receipt and resume REST surface.

    GET  /v1/receipts/by-reference                  → ReceiptListResponse
    GET  /v1/receipts/{receipt_id}                  → ReceiptResponse
    GET  /v1/receipts/{receipt_id}/exclusions       → ExclusionListResponse
    GET  /v1/receipts/{receipt_id}/references       → ReferenceListResponse
    POST /v1/context/resume                         → ResumeResponse

Adapts, with one deliberate composition: feedback belongs above context in the
package graph, so the resume's context state and bounded feedback page meet at
this API layer. The MCP surface calls the same composer rather than repeating
it. Tenant predicates, arm bounds and the ambiguity rule still live in their
owning services.

**Resume answers with a status, not a shape a caller has to interpret.**
Resumed, empty and ambiguous are three different instructions -- carry on, start
fresh, disambiguate -- and a caller left to infer them from which fields came
back empty will pick the wrong one and start work that already exists.

**Exclusions are published.** A receipt that records what it withheld and never
shows it is a receipt nobody can act on.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.receipts import (
    ExclusionListResponse,
    ExclusionResponse,
    ReceiptListResponse,
    ReceiptResponse,
    ReferenceListResponse,
    ReferenceResponse,
    ResumeCheckpointResponse,
    ResumeCitationResponse,
    ResumeFeedbackResponse,
    ResumeLearningResponse,
    ResumeRequestBody,
    ResumeResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.context.receipts import refuse_if_unservable
from contextplane.context.resume import ResumeRequest, ResumeState
from contextplane.exceptions import NotFoundError
from contextplane.signals.reads import FeedbackReadService, ResumeFeedback
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1", tags=["context: receipts"])

# Reading a receipt is an ordinary consumer act; the tenant predicate in every
# service query is what actually decides what comes back.
_read_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR])

#: Most receipts one lookup returns. Paged rather than unbounded: a reference
#: cited by a long-running pipeline accumulates one receipt per resolution.
_DEFAULT_PAGE = 50
_MAX_PAGE = 200


def _receipt(row: object) -> ReceiptResponse:
    return ReceiptResponse(
        receipt_id=row.receipt_id,  # type: ignore[attr-defined]
        intent_id=row.intent_id,  # type: ignore[attr-defined]
        state=row.state,  # type: ignore[attr-defined]
        cacheable=row.cacheable,  # type: ignore[attr-defined]
        resolved_at=row.resolved_at,  # type: ignore[attr-defined]
        requested_by=row.requested_by,  # type: ignore[attr-defined]
        request_digest=row.request_digest,  # type: ignore[attr-defined]
        hydration_state=row.hydration_state,  # type: ignore[attr-defined]
        item_count=row.item_count,  # type: ignore[attr-defined]
        exclusion_count=row.exclusion_count,  # type: ignore[attr-defined]
    )


async def _servable_or_refuse(container: Services, ctx: TenantContext, receipt_id: uuid.UUID) -> None:
    """Refuse the evidence reads for a receipt that is not finished being written.

    A receipt's exclusions answer "was there more than this", and its references
    answer "what was this about". Both are read as complete answers. A receipt
    still hydrating would return an empty list for either, which is
    indistinguishable from a complete receipt that withheld nothing and cited
    nothing -- and the second is a fact while the first is a race.

    `GET /receipts/{id}` deliberately does *not* refuse: it surfaces
    `hydration_state` and `withheld_at`, which is how a caller polling for a
    resolution it triggered learns to wait, and how an operator learns why these
    reads are refusing. Refusing the summary too would leave no way to observe
    the states those columns exist to publish.

    404 for a missing receipt is unchanged and comes first, so a caller cannot
    learn that an id exists by getting a different refusal for it.

    **The rule itself is no longer written here.** This function answers 404 and
    then calls the shared predicate, because the copy that used to live here was
    the only copy: the four MCP tools over the same service reads had none, and
    `get_receipt_exclusions` told its caller that "an empty list means nothing
    was withheld" -- the exact belief this 409 exists to prevent.

    `/exclusions` would now be refused by the service anyway. `/references`
    would not: it reads through the index rather than through
    `ContextReceiptService`, so the call here is what covers it, and the MCP
    tool over the same index read makes the same call for the same reason.
    """
    row = await container.context_receipts.get(ctx, receipt_id=receipt_id)
    if row is None:
        raise map_catalog_error(NotFoundError(f"no receipt {receipt_id}"))
    refuse_if_unservable(row)


def resume_status(state: ResumeState) -> str:
    """Which of the three answers this is.

    Shared with the MCP surface rather than computed twice: the whole point of
    the field is that both transports agree on which instruction a caller got.
    """
    if state.is_ambiguous():
        return "ambiguous"
    return "empty" if state.is_empty() else "resumed"


def _resume(
    state: ResumeState,
    *,
    feedback: tuple[ResumeFeedback, ...],
    truncated: tuple[str, ...],
) -> ResumeResponse:
    return ResumeResponse(
        status=resume_status(state),
        intent_id=state.intent_id,
        head_checkpoint_id=state.head_checkpoint_id,
        head_sequence=state.head_sequence,
        head_summary=state.head_summary,
        checkpoints=[
            ResumeCheckpointResponse(
                checkpoint_id=checkpoint.checkpoint_id,
                sequence=checkpoint.sequence,
                goal=checkpoint.goal,
                open_questions=list(checkpoint.open_questions),
                next_action=checkpoint.next_action,
                recorded_at=checkpoint.recorded_at,
            )
            for checkpoint in state.checkpoints
        ],
        receipts=[_receipt(receipt) for receipt in state.receipts],
        references=[
            ReferenceResponse(
                source_system=reference.source_system,
                source_namespace=reference.source_namespace,
                kind=reference.kind,
                external_id=reference.external_id,
                classification=reference.classification,
            )
            for reference in state.references
        ],
        open_questions=list(state.open_questions),
        next_action=state.next_action,
        feedback=[
            ResumeFeedbackResponse(
                feedback_id=item.feedback_id,
                kind=item.kind,
                receipt_id=item.receipt_id,
                receipt_item_id=item.receipt_item_id,
                rating=item.rating,
                learning_eligible=item.learning_eligible,
                created_at=item.created_at,
                consumed=item.consumed,
            )
            for item in feedback
        ],
        learning=[
            ResumeLearningResponse(
                claim_id=claim.claim_id,
                subject_entity_id=claim.subject_entity_id,
                predicate=claim.predicate,
                value=claim.value,
                claim_category=claim.claim_category,
                confidence=claim.confidence,
                authority=claim.authority,
                valid_from=claim.valid_from,
                valid_to=claim.valid_to,
                as_of=claim.as_of,
                human_confirmed=claim.human_confirmed,
                citations=[
                    ResumeCitationResponse(kind=citation.kind, ref=citation.ref, excerpt=citation.excerpt)
                    for citation in claim.citations
                ],
                label=claim.label,
                trust=claim.trust,
                trust_note=claim.trust_note,
            )
            for claim in state.learning
        ],
        truncated=list(truncated),
        ambiguous_intent_ids=list(state.ambiguous_intent_ids),
    )


async def compose_resume_response(
    *,
    container: Services,
    ctx: TenantContext,
    request: ResumeRequest,
) -> ResumeResponse:
    """Run and project one resume for every transport.

    Feedback belongs to ``signals`` while checkpoint/receipt selection belongs
    to ``context``; importing signals downward would violate the package
    boundary. The API layer performs the one permitted composition, and both
    REST and MCP call it so their added arms cannot drift.
    """
    state = await container.context_resume.resume(ctx, request)

    feedback: tuple[ResumeFeedback, ...] = ()
    truncated = list(state.truncated)
    if state.receipts:
        page = await FeedbackReadService(container.session_factory).resume_page(
            ctx,
            receipt_id=state.receipts[0].receipt_id,
            bound=request.feedback_bound,
        )
        feedback = page.items
        if page.truncated:
            truncated.append("feedback")

    return _resume(state, feedback=feedback, truncated=tuple(truncated))


@router.get("/receipts/by-reference", response_model=ReceiptListResponse)
async def receipts_by_reference(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    source_system: Annotated[str, Query()],
    source_namespace: Annotated[str, Query()],
    kind: Annotated[str, Query()],
    external_id: Annotated[str, Query()],
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE)] = _DEFAULT_PAGE,
) -> ReceiptListResponse:
    """Every resolution citing one piece of external work, newest first.

    The read somebody makes starting from a commit or a build, which is how a
    receipt is reached in practice -- nobody holds a receipt id.
    """
    found = await container.context_reference_index.receipts_for_reference(
        ctx,
        source_system=source_system,
        source_namespace=source_namespace,
        kind=kind,
        external_id=external_id,
        limit=limit,
    )
    return ReceiptListResponse(receipts=[_receipt(row) for row in found])


@router.get("/receipts/{receipt_id}", response_model=ReceiptResponse)
async def get_receipt(
    receipt_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> ReceiptResponse:
    """One receipt by id.

    Missing and forbidden are the same 404 by construction: the service's tenant
    predicate is in the SELECT, so a foreign receipt returns nothing rather than
    being found and refused -- and a distinguishable refusal would confirm the
    id exists.
    """
    row = await container.context_receipts.get(ctx, receipt_id=receipt_id)
    if row is None:
        raise map_catalog_error(NotFoundError(f"no receipt {receipt_id}"))
    return _receipt(row)


@router.get("/receipts/{receipt_id}/exclusions", response_model=ExclusionListResponse)
async def get_receipt_exclusions(
    receipt_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    block: Annotated[str | None, Query()] = None,
) -> ExclusionListResponse:
    """What one resolution withheld, optionally for one block.

    The read that answers "was there more than this". Published because a
    receipt that records its withholding and never shows it leaves a reader
    unable to tell a thin answer from a filtered one.
    """
    await _servable_or_refuse(container, ctx, receipt_id)
    found = await container.context_receipts.exclusions_for(ctx, receipt_id=receipt_id, block=block)
    return ExclusionListResponse(
        exclusions=[ExclusionResponse(block=row.block, item_key=row.item_key, reason=row.reason) for row in found]
    )


@router.get("/receipts/{receipt_id}/references", response_model=ReferenceListResponse)
async def get_receipt_references(
    receipt_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> ReferenceListResponse:
    """What one resolution claimed to be about. The read an auditor makes."""
    await _servable_or_refuse(container, ctx, receipt_id)
    found = await container.context_reference_index.references_for_receipt(ctx, receipt_id=receipt_id)
    return ReferenceListResponse(
        references=[
            ReferenceResponse(
                source_system=row.source_system,
                source_namespace=row.source_namespace,
                kind=row.kind,
                external_id=row.external_id,
                classification=row.classification,
            )
            for row in found
        ]
    )


@router.post("/context/resume", response_model=ResumeResponse, status_code=status.HTTP_200_OK)
async def resume_context(
    body: ResumeRequestBody,
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> ResumeResponse:
    """Pick up the named work, within bounds.

    A POST because the reference list is the request body and can carry several
    tuples; nothing here mutates. 200 for all three outcomes -- ambiguous and
    empty are answers, not errors, and a 4xx would make a caller retry a request
    that was correctly formed.
    """
    bounds = {
        name: value
        for name, value in (
            ("checkpoint_bound", body.checkpoint_bound),
            ("receipt_bound", body.receipt_bound),
            ("reference_bound", body.reference_bound),
            ("feedback_bound", body.feedback_bound),
            ("learning_bound", body.learning_bound),
        )
        if value is not None
    }
    try:
        request = ResumeRequest(references=tuple(body.references), **bounds)
    except ValueError as exc:
        raise map_catalog_error(exc) from exc

    try:
        return await compose_resume_response(container=container, ctx=ctx, request=request)
    except PermissionError as exc:
        raise map_catalog_error(exc) from exc


__all__ = ["compose_resume_response", "resume_status", "router"]
