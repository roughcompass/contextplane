"""Nominate a reporting obligation, classify it, and read the backlog.

The object this exposes was decided and then built by nobody for a release. It
is exposed here in the same change that creates it, because a governed record
nothing can reach is the defect this plan has now found five times: a mechanism
that is correct, tested, and consulted by nothing.

**Nominating and classifying are separate routes, not one route with a
materiality field.** A create that accepted a classification would get a guess,
because the person who noticed the thing is rarely the person entitled to
classify it. A guessed materiality is worse than an honest `unclassified`: it
stops anybody looking again.

**There is no route that classifies automatically, and there will not be one
until a threshold set is ratified.** That set is external and is not this team's
to write. The absence is deliberate and is the honest state — a placeholder
threshold behind a compliance-shaped endpoint is worse than an absent one,
because the absent one is visible.

**REST only, and that is the convention rather than an omission.** No
`/v1/admin` surface in this codebase has an MCP twin -- not quarantine, not the
PII policy editor, not the lifecycle admin. The MCP tools are the agent-facing
surface and these operations are operator-facing, so the parity rule that
applies to the memory and context surfaces does not reach here. What does reach
here is the half of that rule that matters: the refusals live in
`ReportingObligationService`, so a second transport added later inherits them
rather than needing them re-stated.

**The backlog is a read, and its healthy value is not zero.** `unclassified` is
the state most obligations are in most of the time. A surface that presented the
count as an error would train its reader to ignore it, so the response carries
the age of the longest wait beside the count -- five nominated this morning and
five nominated in March are the same number and a different situation.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.routers._admin_common import _admin_required
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.governance.obligations import (
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    ReportingObligation,
)
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: obligations"])


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


class NominateObligationRequest(BaseModel):
    """What may need reporting, in the reporter's own words."""

    summary: str = Field(
        min_length=10,
        max_length=4000,
        description="An obligation nobody can identify later is not a record.",
    )


class ClassifyObligationRequest(BaseModel):
    """A decision somebody made, with the reason they made it.

    `unclassified` is absent from this type on purpose: it is where a row starts
    and is not a conclusion a decision can reach. Allowing it would let an actor
    clear a classification while leaving their name recorded as having made one.
    """

    materiality: Literal["material", "not_material"]
    note: str = Field(
        min_length=20,
        max_length=2000,
        description="Why. A one-word rationale is the same as none.",
    )


class ObligationResponse(BaseModel):
    """One obligation. The classification fields are all set or all null."""

    obligation_id: uuid.UUID
    summary: str
    materiality: str
    nominated_at: datetime.datetime
    nominated_by: uuid.UUID
    classified_at: datetime.datetime | None
    classified_by: uuid.UUID | None
    classification_note: str | None


class BacklogResponse(BaseModel):
    """The unclassified backlog, and how long the longest has waited.

    Both, because the count alone is not actionable.
    """

    unclassified_count: int
    oldest_age_seconds: float


def _response(obligation: ReportingObligation) -> ObligationResponse:
    return ObligationResponse(
        obligation_id=obligation.obligation_id,
        summary=obligation.summary,
        materiality=obligation.materiality,
        nominated_at=obligation.nominated_at,
        nominated_by=obligation.nominated_by,
        classified_at=obligation.classified_at,
        classified_by=obligation.classified_by,
        classification_note=obligation.classification_note,
    )


@router.post("/reporting-obligations", response_model=ObligationResponse, status_code=201)
async def nominate_obligation(
    request: Request,
    body: NominateObligationRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> ObligationResponse:
    """Record that something may need reporting, without deciding whether it does."""
    try:
        obligation = await _services(request).reporting_obligations.nominate(ctx, summary=body.summary)
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation)


@router.post("/reporting-obligations/{obligation_id}/classify", response_model=ObligationResponse)
async def classify_obligation(
    request: Request,
    body: ClassifyObligationRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    obligation_id: Annotated[uuid.UUID, Path()],
) -> ObligationResponse:
    """Decide one obligation's materiality, on the record.

    Refuses a second classification rather than overwriting: the first answer is
    the one somebody acted on, and an overwrite would leave the trail describing
    only the most recent opinion.
    """
    try:
        obligation = await _services(request).reporting_obligations.classify(
            ctx,
            obligation_id=obligation_id,
            materiality=body.materiality,
            note=body.note,
        )
    except (ConflictError, NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation)


@router.get("/reporting-obligations/{obligation_id}", response_model=ObligationResponse)
async def get_obligation(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    obligation_id: Annotated[uuid.UUID, Path()],
) -> ObligationResponse:
    """One obligation, scoped to the caller's tenant."""
    try:
        obligation = await _services(request).reporting_obligations.get(ctx, obligation_id=obligation_id)
    except NotFoundError as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation)


@router.get("/reporting-obligations:backlog", response_model=BacklogResponse)
async def obligation_backlog(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> BacklogResponse:
    """How many obligations are waiting, and how long the longest has waited.

    A read rather than a scheduled report, and both numbers rather than one: a
    scheduled job that silently stops turns a backlog into "whenever somebody
    looks", and a count with no age cannot tell a morning's work from a
    six-month lapse.
    """
    backlog = await _services(request).reporting_obligations.unclassified_backlog(ctx)
    return BacklogResponse(
        unclassified_count=backlog.count,
        oldest_age_seconds=backlog.oldest_age_seconds,
    )


#: Named so a reader of this module can see the closed set without opening the
#: service. Kept in step with `ClassifyObligationRequest` by
#: `tests/unit/test_reporting_obligations_routes.py`, because a `Literal` cannot
#: be enumerated at runtime and the two would otherwise drift silently.
CLASSIFIABLE_ON_THE_WIRE: frozenset[str] = frozenset({MATERIALITY_MATERIAL, MATERIALITY_NOT_MATERIAL})

__all__ = ["CLASSIFIABLE_ON_THE_WIRE", "router"]
