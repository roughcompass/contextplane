"""Withhold claims by provenance, see what that would reach, and put them back.

E4-T8. The service behind this shipped across E4-T2, E4-T3 and E4-T4 and was
reachable by nothing — no route, no tool, no entry in `wiring/`. A quarantine
mechanism nobody can invoke is an incident response that does not exist.

**Three routes, and the shape of the pair matters.** `preview` and `apply` are
separate calls rather than one call with a `dry_run` flag. A flag would put the
decision to withhold and the decision to look on the same request, so a caller
that got the flag wrong withholds content by accident — and the whole reason
this surface exists is that withholding is consequential. Two paths cannot be
confused by a boolean.

**A preview is a point-in-time answer and this surface says so.** The graph
moves: a preview acted on ten minutes later reached a different set. The
response carries the ids rather than a count so a caller can show the
difference, and `truncated` says when the downstream figure is a floor rather
than the answer.

**No idempotency key on `apply`, deliberately.** A key exists so a retry after a
dropped response finds the first result rather than making a second one. That is
the wrong model here: the predicate matches a moving set, so "the identical
request" does not identify an identical outcome, and a replayed key would return
a quarantine whose recorded membership no longer describes what a re-run would
withhold. Applying twice is safe without one — the second `apply` matches only
claims the first did not already withhold, because `apply` refuses to overwrite
an existing `quarantined_at` — and the ledger records two incidents, which is
what happened. A key would have made the second one invisible.

Admin-only, matching every other route in this family. The service enforces the
same bar itself (`producer` or `admin`), and both halves are deliberate: the
route states who this surface is for, and the service refuses regardless of
which transport called it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.routers._admin_common import _admin_required
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.quarantine import SELECTORS
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: quarantine"])


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


class QuarantinePredicate(BaseModel):
    """Which claims to withhold, in the closed provenance vocabulary."""

    selector: str = Field(description=f"One of {list(SELECTORS)}.")
    value: str


class QuarantineApplyRequest(QuarantinePredicate):
    """A predicate plus the reason it is being applied."""

    reason: str = Field(min_length=1, description="Why. Withheld content with no stated cause is unreviewable.")


class QuarantinePreviewResponse(BaseModel):
    """What a predicate reaches, and what depends on what it reaches.

    Two sets that mean different things, kept apart in the response for the
    reason they are kept apart in the service: `matched` is exact and is what
    would be withheld; `downstream` is advisory and **is withheld by nothing**.
    A caller that merged them would tell an operator that applying this makes
    the second list disappear.
    """

    matched: list[uuid.UUID]
    subjects: list[uuid.UUID]
    downstream: list[uuid.UUID]
    seeds_traversed: int
    seeds_total: int
    truncated: bool


class QuarantineResponse(BaseModel):
    """One applied quarantine, and exactly which claims it withheld.

    The ids rather than only the count, because `revert` restores the recorded
    membership and an operator reviewing the incident needs to see the same set
    the ledger will put back.
    """

    quarantine_id: uuid.UUID
    selector: str
    value: str
    matched_count: int
    matched: list[uuid.UUID]


class QuarantineRevertResponse(BaseModel):
    """How many claims a revert actually restored."""

    quarantine_id: uuid.UUID
    restored_count: int


@router.post("/claim-quarantines:preview", response_model=QuarantinePreviewResponse)
async def preview_quarantine(
    request: Request,
    body: QuarantinePredicate,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> QuarantinePreviewResponse:
    """What applying this predicate would reach, without withholding anything.

    A POST rather than a GET despite writing nothing: the predicate is a body,
    and putting a selector and an operator-chosen value in a query string puts
    them in every access log between here and the caller.
    """
    try:
        found = await _services(request).quarantine.preview(ctx, selector=body.selector, value=body.value)
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return QuarantinePreviewResponse(
        matched=list(found.matched),
        subjects=list(found.subjects),
        downstream=list(found.downstream),
        seeds_traversed=found.seeds_traversed,
        seeds_total=found.seeds_total,
        truncated=found.truncated,
    )


@router.post("/claim-quarantines", response_model=QuarantineResponse, status_code=201)
async def apply_quarantine(
    request: Request,
    body: QuarantineApplyRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> QuarantineResponse:
    """Withhold every claim this predicate matches, and record which ones.

    A predicate matching nothing is a `409`, not an empty success: a quarantine
    that withheld nothing reads later as one that was tried and worked, and an
    incident review would take it as evidence the content was contained.
    """
    try:
        applied = await _services(request).quarantine.apply(
            ctx, selector=body.selector, value=body.value, reason=body.reason
        )
    except (ConflictError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return QuarantineResponse(
        quarantine_id=applied.quarantine_id,
        selector=applied.selector,
        value=applied.value,
        matched_count=applied.matched_count,
        matched=list(applied.matched),
    )


@router.post("/claim-quarantines/{quarantine_id}:revert", response_model=QuarantineRevertResponse)
async def revert_quarantine(
    request: Request,
    quarantine_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> QuarantineRevertResponse:
    """Put back exactly what this quarantine withheld. Returns how many.

    The count can be lower than `matched_count` and that is correct rather than
    a partial failure: a claim still held by a second, unreverted quarantine
    stays withheld, because releasing it would republish what the other incident
    still means to contain.

    Reverting an already-reverted quarantine is a `409`. Answering `200` with
    zero would be indistinguishable from a quarantine that had nothing left to
    restore, and those are different facts about the incident.
    """
    try:
        restored = await _services(request).quarantine.revert(ctx, quarantine_id=quarantine_id)
    except (ConflictError, NotFoundError) as exc:
        raise map_catalog_error(exc) from exc
    return QuarantineRevertResponse(quarantine_id=quarantine_id, restored_count=restored)
