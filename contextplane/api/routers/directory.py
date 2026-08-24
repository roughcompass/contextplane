"""The three listings that did not exist, so a field naming one can offer it.

    GET /v1/intents            → IntentListResponse
    GET /v1/tenants            → TenantListResponse
    GET /v1/receipts           → ReceiptListResponse

E23-T1. Twelve dashboard fields asked for an intent, a tenant or a receipt by
UUID and none of the three could be listed, so every one of them was a text box
asking a reader for a value they had no way to obtain.

**Three, not the five the plan held.** Checkpoints and gates turned out to be
reachable already — a checkpoint by id and by digest, a gate as a value inside
the progression definition an existing read returns — so those fields need no
service change and are E23-T3's.

This router adapts and does not decide. What a caller may see is settled in the
services: an intent listing is scoped to the caller's participation grants, a
tenant listing to their credential's memberships, and a receipt listing to what
the detail read would serve them. Each of those is an authorization argument, and
an argument made in a router is one the MCP surface does not have.
"""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.directory import (
    IntentListResponse,
    IntentSummaryResponse,
    ReceiptListResponse,
    ReceiptSummaryResponse,
    TenantListResponse,
    TenantSummaryResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import CatalogError
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1", tags=["directory"])

# All three are reads, and each narrows itself further underneath: an intent to
# the caller's grants, a tenant to their credential, a receipt to what the detail
# read would serve. Tenant role here is the outer gate and never the only one.
_read_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR])


@router.get("/intents", response_model=IntentListResponse)
async def list_intents(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    page_size: int = 50,
) -> IntentListResponse:
    """Tasks this caller participates in, most recently touched first.

    Not "every task in this tenant", because there is no such list to return: a
    task exists as an id referenced from grants and checkpoints and nowhere else.
    Participation is already the rule for reading a task's material, so a
    directory scoped to it cannot offer a task whose checkpoints the caller could
    not then open.
    """
    try:
        found = await container.intent_directory.list_intents(ctx, page_size=page_size)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return IntentListResponse(items=[IntentSummaryResponse.of(entry) for entry in found])


@router.get("/tenants", response_model=TenantListResponse)
async def list_tenants(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> TenantListResponse:
    """The tenants this credential reaches, the current one first.

    The only cross-tenant read in the product, and it does not query for the
    answer: the set comes from the caller's own resolved entitlements, and the
    table is consulted only to attach display names to tenants the credential had
    already named.
    """
    found = await container.tenant_directory.reachable(ctx)
    return TenantListResponse(items=[TenantSummaryResponse.of(entry) for entry in found])


@router.get("/receipts", response_model=ReceiptListResponse)
async def list_receipts(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    limit: int = 50,
    before: datetime.datetime | None = None,
) -> ReceiptListResponse:
    """Recent resolutions this caller may open, newest first.

    **A withheld or unhydrated receipt is absent, not empty.** The detail reads
    refuse both, and a list that showed them as rows would disclose that a
    resolution happened, when, and against what query — from a surface built
    because another surface refuses to say. Rendering them as empty rows would
    disclose the same thing more quietly.

    `before` is a `resolved_at` the caller took from its last row. Keyset rather
    than offset, so a receipt written between two pages cannot shift the window
    and hide one.
    """
    try:
        found = await container.context_receipts.recent(ctx, limit=limit, before=before)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return ReceiptListResponse(
        items=[ReceiptSummaryResponse.of(receipt) for receipt in found],
        next_before=found[-1].resolved_at if len(found) == limit else None,
    )
