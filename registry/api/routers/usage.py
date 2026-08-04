"""Producer surface: how the capabilities you publish are actually being used.

A **separate endpoint** from the operator surface in `admin_usage.py`, not a widened
role list on it. The two answer different questions for different people, and the
difference is the scoping rather than the numbers:

| Surface | Scope | Answers |
|---|---|---|
| `/v1/admin/usage/...` | the caller's own traffic | what my organisation calls |
| `/v1/usage/owned-capabilities` | capabilities the caller owns | what everyone calls of mine |

Merging them would have meant one endpoint whose meaning changed with the caller's
role, which is the kind of surface that gets read wrong in a review and then
misinterpreted in a dashboard.

**This reads across tenants, deliberately, and stops at totals.** A publisher's
capability is called by other tenants, whose rollup rows are keyed by their own
tenant id — so scoping this to the caller's tenant would answer how much a publisher
calls their own capability, which is almost never the question. It therefore sums
every tenant's rows for the owned capability and returns no breakdown by caller.
Totals tell an owner their work is used; a per-consumer split would tell them how
heavily each named customer leans on it, and that is a different disclosure decision
than the one this endpoint implements.

**On the gate.** `admin` or `producer`, and the `producer` half is the point. A
resolved principal carries exactly one collapsed role, so a gate admitting only
`admin` would exclude every actual publisher — the reader this exists for. There is
a test that says so with a producer-only principal.

**Not adoption metrics.** Adopter counts and version distribution come off different
primitives and are a different surface. A capability can be widely adopted and never
called; this answers only the second half.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.routers._admin_common import _admin_or_producer_required
from registry.types import TenantContext
from registry.usage import reads

router = APIRouter(prefix="/v1/usage")

#: Default window. Matches the operator surface so the two cannot disagree about
#: what "recently" means.
_DEFAULT_WINDOW_DAYS = 30


class OwnedCapabilityUsageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: uuid.UUID
    name: str
    calls: int = Field(description="Calls from every tenant, including your own.")
    ok_calls: int
    error_calls: int = Field(
        description=(
            "Calls that failed. Yours to act on rather than the caller's: a rising "
            "count here is a capability behaving badly, which the total alone hides."
        )
    )
    actor_days: int = Field(
        description=(
            "Sum of each calling tenant's daily distinct actors. Not a headcount, and "
            "further from one than elsewhere in this API, since it sums across tenants "
            "as well as days."
        )
    )
    payload_bytes: int | None = Field(
        description=(
            "Bytes returned, summed. Null when nothing measured it — MCP calls and "
            "streaming responses record no size, so null is not zero."
        )
    )


class OwnedCapabilityUsageListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime.date
    end: datetime.date
    capabilities: list[OwnedCapabilityUsageOut] = Field(
        description=(
            "Owned capabilities with recorded usage, most-called first. A capability "
            "nobody has called is absent rather than present with zeros — this reads "
            "usage, not the catalog."
        )
    )


@router.get(
    "/owned-capabilities",
    response_model=OwnedCapabilityUsageListOut,
    tags=["retrieval"],
    summary="How the capabilities your tenant owns are being called",
)
async def get_owned_capability_usage(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_or_producer_required)],
    start: Annotated[
        datetime.date | None, Query(alias="from", description="First day of the window, inclusive.")
    ] = None,
    end: Annotated[
        datetime.date | None, Query(alias="to", description="Last day of the window, inclusive.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=reads.MAX_RANKING_LIMIT)] = reads.DEFAULT_RANKING_LIMIT,
) -> OwnedCapabilityUsageListOut:
    resolved_end = end if end is not None else datetime.datetime.now(tz=datetime.UTC).date()
    resolved_start = start if start is not None else resolved_end - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS - 1)

    try:
        rows = await reads.read_owned_capability_usage(
            request.app.state.session_factory,
            owner_tenant_id=ctx.tenant_id,
            start=resolved_start,
            end=resolved_end,
            limit=limit,
        )
    except (reads.InvalidRangeError, reads.RangeTooWideError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return OwnedCapabilityUsageListOut(
        start=resolved_start,
        end=resolved_end,
        capabilities=[
            # Field by field rather than a splat, for the same reason as the operator
            # surface: `actor_days` carries a caveat and dropping it silently is worse
            # than failing to build.
            OwnedCapabilityUsageOut(
                capability_id=row.capability_id,
                name=row.name,
                calls=row.calls,
                ok_calls=row.ok_calls,
                error_calls=row.error_calls,
                actor_days=row.actor_days,
                payload_bytes=row.payload_bytes,
            )
            for row in rows
        ],
    )
