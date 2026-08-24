"""Per-actor detail, for an auditor, on the record.

E11-T3. Its own router rather than a path on `learning_reads.py`, and that is
the design rather than a filing choice: that surface is owner-facing and
deliberately has **no per-actor path at all**, pinned structurally over its whole
route table by `tests/conformance/test_feedback_privacy.py`. A per-actor route
added there would have deleted the only thing keeping it actor-free.

So the capability that needs accounting for lives where the accounting is:
behind `ROLE_AUDITOR`, and behind a justification written before the answer.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.routers._admin_common import _auditor_required
from contextplane.exceptions import ValidationError
from contextplane.service.memory.audit_drilldown import DRILLDOWN_METRICS
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin/audit", tags=["admin: audit"])


class ActorDrilldownRequest(BaseModel):
    """One question about one actor, and why it is being asked.

    A POST for a read, deliberately. This request *writes* — the justification is
    recorded before the figure is returned — and a GET that wrote a row would be
    a GET a caller could be made to issue by a link.
    """

    metric: str = Field(description=f"One of {list(DRILLDOWN_METRICS)}.")
    window_start: datetime.datetime
    window_end: datetime.datetime
    justification: str = Field(
        min_length=20,
        max_length=2000,
        description=(
            "Why this read is being made. Free text on purpose: a dropdown produces "
            "the reason nearest the top, and the point is a sentence somebody has to "
            "be willing to have read back to them."
        ),
    )


class ActorDrilldownResponse(BaseModel):
    subject_actor_id: uuid.UUID
    metric: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    value: int
    #: The record of this question. Returned so an auditor can cite their own
    #: read; a surface that recorded something it would not show the caller is
    #: one people learn to distrust.
    read_id: uuid.UUID


class JustifiedReadsResponse(BaseModel):
    items: list[dict[str, Any]]


@router.post("/actors/{subject_actor_id}:drilldown", response_model=ActorDrilldownResponse)
async def read_actor_metric(
    request: Request,
    body: ActorDrilldownRequest,
    ctx: Annotated[TenantContext, Depends(_auditor_required)],
    subject_actor_id: Annotated[uuid.UUID, Path()],
) -> ActorDrilldownResponse:
    """One actor's figure, after the reason for asking is on the record.

    The justification is written **first, in the same transaction**. If it
    cannot be written the caller gets nothing: a read that could not be recorded
    is a read that does not happen.
    """
    services: Services = request.app.state.services
    try:
        detail = await services.audit_drilldown.read_actor_metric(
            ctx,
            subject_actor_id=subject_actor_id,
            metric=body.metric,
            window_start=body.window_start,
            window_end=body.window_end,
            justification=body.justification,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return ActorDrilldownResponse(
        subject_actor_id=detail.subject_actor_id,
        metric=detail.metric,
        window_start=detail.window_start,
        window_end=detail.window_end,
        value=detail.value,
        read_id=detail.read_id,
    )


@router.get("/actors/{subject_actor_id}/reads", response_model=JustifiedReadsResponse)
async def reads_of_subject(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_auditor_required)],
    subject_actor_id: Annotated[uuid.UUID, Path()],
    limit: int = Query(100, ge=1, le=500),
) -> JustifiedReadsResponse:
    """Who has looked at this actor, and what they said.

    The direction that makes the record more than bookkeeping: a log only its
    own author can read disciplines nobody.
    """
    services: Services = request.app.state.services
    found = await services.audit_drilldown.reads_of_subject(ctx, subject_actor_id=subject_actor_id, limit=limit)
    return JustifiedReadsResponse(items=found)


__all__ = ["router"]
