"""Organization-scope claim predicates, under `/v1/operator/*`.

Deliberately not under `/v1/admin`. Every route there is tenant-scoped and
authorized by a role *within* a tenant; these routes have no tenant at all and
are authorized by deployment identity. Putting them alongside tenant admin
would invite exactly the mistake the requirement forbids — a tenant admin
reaching a deployment-wide write because the two looked like the same kind of
thing.

Authorization is an exact `(issuer, subject)` pair from the deployment operator
allowlist. Not a role: every role in this system is tenant-scoped, so no role
can serve as the deployment trust root. The allowlist is configuration outside
the database and ungrantable by any tenant.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.errors import build_error
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers.arc_admin import operator_allowlist_fingerprint
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.catalog.global_vocabulary import GlobalPredicate, GlobalVocabularyService
from registry.types import TenantContext
from registry.wiring.container import Services

router = APIRouter(tags=["operator: ontology"], prefix="/v1/operator")


def _require_operator(request: Request, ctx: TenantContext) -> None:
    """Gate on deployment identity, reusing the ARC operator allowlist.

    One allowlist for every deployment-wide write. A second list would drift
    from the first, and an operator removed from one but not the other would
    still hold authority nobody thinks they have.
    """
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        arc_ctx = ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc

    settings = request.app.state.settings
    allowlist: tuple[tuple[str, str], ...] = tuple(getattr(settings, "arc_global_operator_allowlist", ()))
    if arc_ctx.operator_identity not in allowlist:
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="this operation requires deployment operator identity",
        )


def _service(request: Request) -> GlobalVocabularyService:
    services: Services = request.app.state.services
    service = services.global_vocabulary
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="organization vocabulary administration is not configured",
        )
    return service


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePredicateRequest(_Strict):
    value: str = Field(min_length=1, max_length=200)
    value_type: str = Field(min_length=1, max_length=64)
    claim_category: str = Field(min_length=1, max_length=64)
    definition: str = Field(min_length=1, max_length=1000)


class PredicateResponse(BaseModel):
    value: str
    value_type: str
    claim_category: str
    definition: str
    scope: str
    deprecated_at: datetime.datetime | None


class LocalPredicateResponse(BaseModel):
    tenant_id: uuid.UUID
    value: str


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ValidationError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="validation_error", message=str(exc))
    return exc


def _response(predicate: GlobalPredicate) -> PredicateResponse:
    return PredicateResponse(
        value=predicate.value,
        value_type=predicate.value_type,
        claim_category=predicate.claim_category,
        definition=predicate.definition,
        scope=predicate.scope,
        deprecated_at=predicate.deprecated_at,
    )


@router.get("/claim-predicates", response_model=list[PredicateResponse])
async def list_global_predicates(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> list[PredicateResponse]:
    """Every organization-scope predicate, including deprecated ones.

    Deprecated predicates are listed because claims still reference them and an
    operator reconciling the ontology needs to see what a name used to mean.
    """
    _require_operator(request, ctx)
    return [_response(p) for p in await _service(request).list_predicates()]


@router.post("/claim-predicates", response_model=PredicateResponse, status_code=status.HTTP_201_CREATED)
async def create_global_predicate(
    request: Request,
    body: CreatePredicateRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> PredicateResponse:
    """Define a predicate for the whole deployment.

    Refused with 409 if any tenant already uses the name locally. Promoting it
    would silently retype every claim written against their meaning of the
    term, so the local definition has to be reconciled first.
    """
    _require_operator(request, ctx)
    try:
        predicate = await _service(request).create_predicate(
            value=body.value,
            value_type=body.value_type,
            claim_category=body.claim_category,
            definition=body.definition,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _response(predicate)


@router.post("/claim-predicates/{value}/deprecate", response_model=PredicateResponse)
async def deprecate_global_predicate(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    value: Annotated[str, Path(min_length=1, max_length=200)],
) -> PredicateResponse:
    """Retire a predicate without removing it.

    The row stays because claims reference it. Nothing new may be written
    against a deprecated predicate, and no tenant may reuse the name.
    """
    _require_operator(request, ctx)
    try:
        predicate = await _service(request).deprecate_predicate(value=value)
    except Exception as exc:
        raise _translate(exc) from exc
    return _response(predicate)


@router.get("/claim-predicates/local-inventory", response_model=list[LocalPredicateResponse])
async def inventory_local_predicates(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> list[LocalPredicateResponse]:
    """Tenant-local predicates across the deployment, for ontology governance.

    Names and owning tenants only, and only on this operator path. It exists so
    divergence is observable — which terms tenants invented independently, and
    therefore what should become shared. No tenant-facing route exposes another
    tenant's local vocabulary.
    """
    _require_operator(request, ctx)
    return [
        LocalPredicateResponse(tenant_id=tid, value=value)
        for tid, value in await _service(request).local_predicate_inventory()
    ]


__all__ = ["operator_allowlist_fingerprint", "router"]
