"""Admin surface for the memory-curation operator knobs: promotion policy,
the autopromote allowlist, source governance, and calibration.

**Calibration's routes wire in here, but not its logic.** `GET
/v1/admin/memory-calibration` and its `:refit` action live in this file like
every other operator knob, so an admin has one surface to read rather than
one per background worker. What does *not* live here is the
`load_observations -> fit -> publish` sequence itself -- both this file's
`:refit` route and the calibration-refit worker call the same
`contextplane.workers.calibration_refit.refit_one` function, so an operator's
on-demand refit and the scheduled sweep can never quietly compute a fit two
different ways.

`may_provision_entities` (whether an unresolved connector subject may get an
entity provisioned for it before its claim is linked) is exposed on both the
`POST` declare body and the `PATCH .../{id}` body, alongside the tier/ceiling/
window fields those two routes already carry -- off by default on `declare`
(an operator opts a source in deliberately) and merge-preserved on `PATCH`
the same way every other field there is, so a `PATCH` naming only
`ingest_ceiling` cannot silently flip a source's provisioning posture.

**Gate: `_admin_required`, matching `admin_operational_health.py`'s
convention.** Every route here reads or changes a tenant-wide posture
(what promotes without review, what a source may write), so the bar is
the tenant-administrator role on every route, not a mix of roles per
action.

**Route registration.** `PUT /memory-promotion-policy` and
`PATCH /memory-sources/{id}` both have a real alternate form under
`CONTEXTPLANE_HTTP_METHODS_MODE=post_only` (a proxy that strips PUT/PATCH still
needs a way in), so both go through `HttpMethodRouter.add_mutation_route`
on this file's own mode-aware `mutation_router`. The PUT's alias action is
`"replace"`, not `"update"`: it replaces the tenant's whole policy
(floor, threshold, and review list together) in one write, the same
distinction `interface.py`'s `PUT .../interface` draws against every
`PATCH`-shaped `"update"` alias elsewhere in this API. The PATCH's alias
is `"update"`, matching every other partial-update PATCH in this codebase.

`:allow`, `:revoke`, and `:reset-breaker` stay plain `@router.post` routes,
not run through `HttpMethodRouter` -- the same reasoning `memory_curation.py`
documents for `:link`/`:discard`: none of the three has a genuine alternate
HTTP verb to switch between across deployment modes, so there is nothing
for the switch to do. Feeding an already-suffixed path through
`add_mutation_route` would double the suffix under `post_only`/`both`
instead.

Services come off the typed container (`request.app.state.services`), never
a bare `app.state.<name>` read, matching every router in this codebase that
has migrated to it. The promotion-policy reader/writer are bare module
functions taking a session directly rather than a service method (a
deliberate choice recorded where they are defined), so this file opens one
off `services.session_factory` -- also a typed field on the same container,
not a second, untyped attribute read.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.errors import map_catalog_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.routers._admin_common import _admin_required
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.memory.calibration import MappingStatus
from contextplane.service.memory.promotion_eligibility import PromotionPolicy, load_policy, set_policy
from contextplane.service.memory.source_governance import SourcePolicy
from contextplane.types import TenantContext
from contextplane.wiring.container import Services
from contextplane.workers.calibration_refit import refit_one

router = APIRouter(prefix="/v1/admin", tags=["admin: memory curation"])

_mode, _sep = get_mode_settings()
mutation_router = APIRouter(prefix="/v1/admin", tags=["admin: memory curation"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


class _Strict(BaseModel):
    """Closed request/response models -- a misspelled field is refused, not
    silently dropped; a response never grows an undocumented field nobody
    noticed changed the contract."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Promotion policy
# ---------------------------------------------------------------------------


class PromotionPolicyResponse(_Strict):
    """The tenant's promotion-review posture, read by GET and echoed by PUT after a replace."""

    confidence_floor: float
    blast_radius_threshold: int
    always_review: list[str]


def _to_policy_response(policy: PromotionPolicy) -> PromotionPolicyResponse:
    return PromotionPolicyResponse(
        confidence_floor=policy.confidence_floor,
        blast_radius_threshold=policy.blast_radius_threshold,
        always_review=sorted(policy.always_review),
    )


@router.get("/memory-promotion-policy", response_model=PromotionPolicyResponse)
async def get_promotion_policy(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> PromotionPolicyResponse:
    """The tenant's current promotion-review posture.

    An unconfigured tenant is not an error: it reads back `PromotionPolicy`'s
    own cautious defaults (no confidence floor, a low blast-radius threshold,
    nothing on the always-review list) rather than a 404, because those
    defaults are exactly what governs promotion until an admin changes them.
    """
    services = _services(request)
    async with services.session_factory() as session:
        policy = await load_policy(session, ctx.tenant_id)
    return _to_policy_response(policy)


class PromotionPolicyRequest(_Strict):
    """Body for PUT /memory-promotion-policy -- a full replace, not a partial update; all three fields are required."""

    confidence_floor: float = Field(ge=0.0, le=1.0)
    blast_radius_threshold: int = Field(ge=0)
    always_review: list[str] = Field(default_factory=list)


async def put_promotion_policy(
    request: Request,
    body: PromotionPolicyRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> PromotionPolicyResponse:
    """Replace the tenant's promotion-review posture in one write.

    The bounds on `confidence_floor`/`blast_radius_threshold` are checked
    twice -- once by the view model's own `Field` constraints, once inside
    `set_policy` itself -- deliberately: the view model lets a malformed
    request 422 before a session even opens, but `set_policy` is the one
    gate that matters if this write is ever reached some other way.
    """
    services = _services(request)
    async with services.session_factory() as session, session.begin():
        try:
            policy = await set_policy(
                session,
                ctx,
                confidence_floor=body.confidence_floor,
                blast_radius_threshold=body.blast_radius_threshold,
                always_review=frozenset(body.always_review),
                now=services.clock.now(),
            )
        except (ValidationError, PermissionError) as exc:
            raise map_catalog_error(exc) from exc
    return _to_policy_response(policy)


_mut_mr.add_mutation_route(
    path="/memory-promotion-policy",
    action="replace",
    handler=put_promotion_policy,
    verb="PUT",
    response_model=PromotionPolicyResponse,
)


# ---------------------------------------------------------------------------
# Autopromote allowlist
# ---------------------------------------------------------------------------


class AllowlistResponse(_Strict):
    """The tenant's current autopromote allowlist, returned by the GET and by both mutation routes."""

    predicates: list[str]


async def _allowlist_response(services: Services, tenant_id: uuid.UUID) -> AllowlistResponse:
    predicates = await services.promotion_guardrails.allowlist_for(tenant_id)
    return AllowlistResponse(predicates=sorted(predicates))


@router.get("/memory-autopromote-allowlist", response_model=AllowlistResponse)
async def get_autopromote_allowlist(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> AllowlistResponse:
    """Every predicate this tenant has opted into automatic promotion.

    Empty by default -- an unconfigured tenant auto-promotes nothing, the
    same closed posture `GuardrailService.may_auto_promote` itself starts
    from.
    """
    return await _allowlist_response(_services(request), ctx.tenant_id)


class AllowlistPredicateRequest(_Strict):
    """Body shared by `:allow` and `:revoke` -- one predicate name per call."""

    predicate: str = Field(min_length=1)


@router.post("/memory-autopromote-allowlist:allow", response_model=AllowlistResponse)
async def allow_autopromote_predicate(
    request: Request,
    body: AllowlistPredicateRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> AllowlistResponse:
    """Opt one predicate into automatic promotion.

    Returns the allowlist's new state rather than a bare status -- an
    operator widening what may skip review should be able to confirm the
    resulting set without a second round trip. Adding an already-allowlisted
    predicate is a no-op (the service's own `ON CONFLICT DO NOTHING`), so
    this route has no not-found or conflict case to translate.
    """
    services = _services(request)
    await services.promotion_guardrails.allow(ctx.tenant_id, body.predicate, actor_id=ctx.actor_id)
    return await _allowlist_response(services, ctx.tenant_id)


@router.post("/memory-autopromote-allowlist:revoke", response_model=AllowlistResponse)
async def revoke_autopromote_predicate(
    request: Request,
    body: AllowlistPredicateRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> AllowlistResponse:
    """Opt one predicate back out of automatic promotion.

    Idempotent the same way `allow` is: revoking a predicate that was never
    allowlisted leaves the (empty) intersection unchanged, so there is
    nothing here to 404 on either.
    """
    services = _services(request)
    await services.promotion_guardrails.revoke(ctx.tenant_id, body.predicate, actor_id=ctx.actor_id)
    return await _allowlist_response(services, ctx.tenant_id)


# ---------------------------------------------------------------------------
# Source governance
# ---------------------------------------------------------------------------


class SourcePolicyResponse(_Strict):
    """A declared source's governance state, including live breaker/breach counters."""

    source_id: uuid.UUID
    tenant_id: uuid.UUID
    authority_tier: str
    ingest_ceiling: int
    window_seconds: int
    breaker_open_until: datetime.datetime | None
    breach_count: int
    may_provision_entities: bool


def _to_source_policy_response(policy: SourcePolicy) -> SourcePolicyResponse:
    return SourcePolicyResponse(
        source_id=policy.source_id,
        tenant_id=policy.tenant_id,
        authority_tier=policy.authority_tier,
        ingest_ceiling=policy.ingest_ceiling,
        window_seconds=policy.window_seconds,
        breaker_open_until=policy.breaker_open_until,
        breach_count=policy.breach_count,
        may_provision_entities=policy.may_provision_entities,
    )


@router.get("/memory-sources", response_model=list[SourcePolicyResponse])
async def list_memory_sources(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> list[SourcePolicyResponse]:
    """Every source this tenant has declared an authority tier and a ceiling
    for. A source that exists in `sync_sources` but has never been declared
    here does not appear -- it also may not write a single claim yet."""
    policies = await _services(request).source_governance.policies_for_tenant(ctx.tenant_id)
    return [_to_source_policy_response(p) for p in policies]


class SourceDeclareRequest(_Strict):
    """Body for POST /memory-sources -- declares or re-declares a source's full policy in one write."""

    source_id: uuid.UUID
    authority_tier: str = Field(min_length=1)
    ingest_ceiling: int = Field(default=1000, gt=0)
    window_seconds: int = Field(default=3600, gt=0)
    may_provision_entities: bool = False


@router.post(
    "/memory-sources",
    response_model=SourcePolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def declare_memory_source(
    request: Request,
    body: SourceDeclareRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> SourcePolicyResponse:
    """Register what a source's claims are worth and how many it may write
    per window. Required before the source may write anything --
    `SourceGovernanceService.admit` refuses an undeclared source outright.
    Re-declaring an already-governed source upserts its tier and ceiling;
    the `PATCH` route below reaches the same write path for a partial
    change.
    """
    try:
        policy = await _services(request).source_governance.declare(
            ctx,
            source_id=body.source_id,
            authority_tier=body.authority_tier,
            ingest_ceiling=body.ingest_ceiling,
            window_seconds=body.window_seconds,
            may_provision_entities=body.may_provision_entities,
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_source_policy_response(policy)


class SourcePolicyPatch(_Strict):
    """Body for PATCH /memory-sources/{id} -- every field optional; an omitted field keeps its current value."""

    authority_tier: str | None = Field(default=None, min_length=1)
    ingest_ceiling: int | None = Field(default=None, gt=0)
    window_seconds: int | None = Field(default=None, gt=0)
    may_provision_entities: bool | None = Field(default=None)


async def patch_memory_source(
    source_id: uuid.UUID,
    body: SourcePolicyPatch,
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> SourcePolicyResponse:
    """Partial-update a declared source's tier, ceiling, and/or provisioning flag.

    Loads the current policy first so an omitted field keeps its existing
    value instead of reverting to `declare`'s own defaults -- a `PATCH` that
    names only `ingest_ceiling` must not silently reset the authority tier
    (or flip `may_provision_entities` off) to whatever `declare` would
    default it to. The tenant check happens here, before `declare` is ever
    called: `policy_for` carries no tenant filter of its own, and `declare`'s
    own ownership check answers a wrong tenant with 403, which -- unlike this
    route's 404 -- would confirm that some tenant governs this `source_id`,
    just not the caller's.
    """
    services = _services(request)
    current = await services.source_governance.policy_for(source_id)
    if current is None or current.tenant_id != ctx.tenant_id:
        raise map_catalog_error(NotFoundError("no such source"))

    try:
        policy = await services.source_governance.declare(
            ctx,
            source_id=source_id,
            authority_tier=body.authority_tier if body.authority_tier is not None else current.authority_tier,
            ingest_ceiling=body.ingest_ceiling if body.ingest_ceiling is not None else current.ingest_ceiling,
            window_seconds=body.window_seconds if body.window_seconds is not None else current.window_seconds,
            may_provision_entities=(
                body.may_provision_entities
                if body.may_provision_entities is not None
                else current.may_provision_entities
            ),
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_source_policy_response(policy)


_mut_mr.add_mutation_route(
    path="/memory-sources/{source_id}",
    action="update",
    handler=patch_memory_source,
    verb="PATCH",
    response_model=SourcePolicyResponse,
)


@router.post("/memory-sources/{source_id}:reset-breaker", response_model=SourcePolicyResponse)
async def reset_memory_source_breaker(
    source_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> SourcePolicyResponse:
    """Close a tripped breaker early, returning the reloaded policy so the
    caller can see the breaker is clear without a second GET.

    `reset_breaker` folds "no such source" and "not your source" into one
    `PermissionError` (403) by design -- its own docstring records that
    decision so a future type-narrowing does not silently re-split the two
    into a cross-tenant existence oracle. This route does not re-split them
    either; `NotFoundError` is caught alongside it only for symmetry with
    every other route in this file, not because `reset_breaker` raises it
    today.
    """
    services = _services(request)
    try:
        await services.source_governance.reset_breaker(ctx, source_id)
    except (NotFoundError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc

    policy = await services.source_governance.policy_for(source_id)
    if policy is None:  # pragma: no cover - reset_breaker just proved this row exists
        raise HTTPException(status_code=500, detail="source policy vanished after reset-breaker")
    return _to_source_policy_response(policy)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class CalibrationMappingResponse(_Strict):
    """One (provider, model, strategy) triple's most recent fit -- deployment-wide, not tenant-scoped."""

    provider_id: str
    model_id: str
    strategy_id: str
    version: str
    status: str
    n_adjudicated: int
    measured_error: float
    fitted_at: datetime.datetime


def _to_calibration_mapping_response(mapping: MappingStatus) -> CalibrationMappingResponse:
    return CalibrationMappingResponse(
        provider_id=mapping.provider_id,
        model_id=mapping.model_id,
        strategy_id=mapping.strategy_id,
        version=mapping.version,
        status=mapping.status,
        n_adjudicated=mapping.n_adjudicated,
        measured_error=mapping.measured_error,
        fitted_at=mapping.fitted_at,
    )


@router.get("/memory-calibration", response_model=list[CalibrationMappingResponse])
async def list_memory_calibration(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> list[CalibrationMappingResponse]:
    """Every (provider, model, strategy) triple ever fitted, reduced to its
    most recent attempt.

    Deployment-wide, not scoped to the calling tenant: `memory_calibration_mapping`
    carries no tenant column, because the thing being measured -- how much a
    provider's self-reported confidence predicts correctness -- is shared, and
    no tenant can recalibrate somebody else's. An admin from any tenant sees
    the same rows.
    """
    mappings = await _services(request).calibration.active_mappings()
    return [_to_calibration_mapping_response(m) for m in mappings]


class CalibrationRefitRequest(_Strict):
    """Body for POST /memory-calibration:refit -- names the exact triple to refit now."""

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)


class CalibrationRefitResponse(_Strict):
    """Outcome of an on-demand refit; `activated: false` means the triple stayed below the evaluation floor."""

    provider_id: str
    model_id: str
    strategy_id: str
    version: str
    activated: bool
    n_adjudicated: int


@router.post("/memory-calibration:refit", response_model=CalibrationRefitResponse)
async def refit_memory_calibration(
    request: Request,
    body: CalibrationRefitRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> CalibrationRefitResponse:
    """Run the fit -> publish sequence for one named triple right now, rather
    than waiting for the periodic worker's next tick.

    Calls `refit_one` -- the exact function the calibration-refit worker calls
    per triple -- so this is not a second implementation of what "refit a
    triple" means; it is the same one, run on demand. A triple with fewer
    judged outcomes than the evaluation floor requires comes back with the
    `uncalibrated` version and `activated: false`, the same refusal the worker
    itself would report; nothing about calling this early bypasses that gate.
    """
    services = _services(request)
    outcome = await refit_one(
        services.calibration,
        provider_id=body.provider_id,
        model_id=body.model_id,
        strategy_id=body.strategy_id,
        clock=services.clock,
        fitted_by=ctx.actor_id,
    )
    return CalibrationRefitResponse(
        provider_id=outcome.provider_id,
        model_id=outcome.model_id,
        strategy_id=outcome.strategy_id,
        version=outcome.version,
        activated=outcome.activated,
        n_adjudicated=outcome.n_adjudicated,
    )
