"""Receipt and resume REST surface.

    GET  /v1/receipts/by-reference                  → ReceiptListResponse
    GET  /v1/receipts/{receipt_id}                  → ReceiptResponse
    GET  /v1/receipts/{receipt_id}/exclusions       → ExclusionListResponse
    GET  /v1/receipts/{receipt_id}/references       → ReferenceListResponse
    POST /v1/context/resume                         → ResumeResponse

Adapts only. Every tenant predicate, every bound and the ambiguity rule live in
the services, because the MCP surface answers the same questions and a rule
enforced in two adapters is a rule that will be enforced differently in one.

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

from contextplane.api.auth.context import (
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_CONSUMER,
    ROLE_PRODUCER,
    require_roles,
)
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.receipts import (
    ExclusionListResponse,
    ExclusionResponse,
    ReceiptListResponse,
    ReceiptResponse,
    ReferenceListResponse,
    ReferenceResponse,
    ResumeCheckpointResponse,
    ResumeRequestBody,
    ResumeResponse,
)
from contextplane.context.resume import ResumeRequest, ResumeState
from contextplane.exceptions import NotFoundError
from contextplane.types import TenantContext
from contextplane.wiring.container import Services, services

router = APIRouter(prefix="/v1", tags=["context receipts"])

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
        task_id=row.task_id,  # type: ignore[attr-defined]
        state=row.state,  # type: ignore[attr-defined]
        cacheable=row.cacheable,  # type: ignore[attr-defined]
        resolved_at=row.resolved_at,  # type: ignore[attr-defined]
        requested_by=row.requested_by,  # type: ignore[attr-defined]
        request_digest=row.request_digest,  # type: ignore[attr-defined]
    )


def resume_status(state: ResumeState) -> str:
    """Which of the three answers this is.

    Shared with the MCP surface rather than computed twice: the whole point of
    the field is that both transports agree on which instruction a caller got.
    """
    if state.is_ambiguous():
        return "ambiguous"
    return "empty" if state.is_empty() else "resumed"


def _resume(state: ResumeState) -> ResumeResponse:
    return ResumeResponse(
        status=resume_status(state),
        task_id=state.task_id,
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
        open_questions=list(state.open_questions),
        next_action=state.next_action,
        truncated=list(state.truncated),
        ambiguous_task_ids=list(state.ambiguous_task_ids),
    )


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
        )
        if value is not None
    }
    try:
        request = ResumeRequest(references=tuple(body.references), **bounds)
    except ValueError as exc:
        raise map_catalog_error(exc) from exc

    return _resume(await container.context_resume.resume(ctx, request))


__all__ = ["resume_status", "router"]
