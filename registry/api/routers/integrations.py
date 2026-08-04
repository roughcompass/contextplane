"""GET /v1/integrations?connects=...&and=...

Surfaces :class:`IntegrationLookupService` over HTTP. Visibility is
filtered at the service layer so only integrations the caller's tenant
can see are returned.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request

from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers._common import ViewParam, entity_ref_to_item
from registry.api.schemas import IntegrationListResponse
from registry.service.integration_lookup import IntegrationLookupService
from registry.types import TenantContext

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])


def _svc(request: Request) -> IntegrationLookupService:
    return request.app.state.integrations  # type: ignore[no-any-return]


@router.get(
    "",
    response_model=IntegrationListResponse,
    response_model_exclude_unset=True,
    summary="Find integrations that connect two capabilities",
)
async def find_integrations(
    request: Request,
    connects: uuid.UUID = Query(..., description="capability_a_id"),
    and_: uuid.UUID = Query(..., alias="and", description="capability_b_id"),
    view: ViewParam = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> IntegrationListResponse:
    """List integrations whose member edges connect ``connects`` and ``and``.

    Visibility-filtered: an integration is included only if it is
    visible to the calling tenant.

    Pagination: ``next_cursor`` is always ``None`` — integrations connecting
    two specific capabilities are bounded (typically 1–3 rows), so keyset
    pagination is not wired. The envelope exists for client shape consistency.
    """
    refs = await _svc(request).find_integrations_connecting(ctx=ctx, cap_a_id=connects, cap_b_id=and_)
    items = [entity_ref_to_item(r, audit=view == "audit") for r in refs]
    return IntegrationListResponse(items=items, next_cursor=None)


__all__ = ["router"]
