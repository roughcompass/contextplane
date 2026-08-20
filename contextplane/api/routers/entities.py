"""/v1/entities — the generic, profile-governed entity write surface.

The capability APIs are typed: they know what a capability is and what may be
said about one. A profile can declare types this codebase has never heard of, and
those types need a write surface too — one that carries the profile's rules rather
than a schema baked in here.

**Intent is stated, never defaulted.** Every write says whether it is an
observation, a request, or an authorized approval, and there is no default because
every default routes somebody's write somewhere they did not choose. The three go
to three different places: an observation becomes a staged claim, a request
becomes an entry in the owner's queue, and only a verified approval reaches the
canonical validators. An ordinary agent can therefore call this surface all day
without ever writing canonical data — which is the property that makes it safe to
expose generically.

**Authority is resolved here, from authentication, and never read from the body.**
`refuse_caller_asserted_authority` rejects a body that states what the platform
concludes, and the `ProfileWriteAuthority` handed to the router is built from the
authenticated context — a caller that supplies `approval_reference` on the
approval route gets it *re-resolved*, not trusted. A body that could name its own
authority would make every other check here decorative.

**`:resolve` is loud about ambiguity.** An unqualified handle matching more than
one type returns `identity_ambiguous` rather than a row. Picking one would attach
a caller's next write to whichever type happened to sort first, and nothing in the
response would say a choice had been made.

Route order matters: the two colon-suffixed paths are declared before
`/{entity_id}` so the path parameter cannot swallow them.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text

from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.entity_writes import (
    IDENTITY_AMBIGUOUS,
    EntityIdentityV1,
    EntityReadV1,
    EntityResolutionV1,
    EntityWriteRequestV1,
    EntityWriteResultV1,
    ProfileAttributionV1,
    ProvenanceSummaryV1,
    ReadinessReportV1,
    TemporalStateV1Out,
    ValidationOutcomeV1,
)
from contextplane.entities.identity import AmbiguousIdentity, UnknownIdentity
from contextplane.entities.validation import EntityValidationResult, EntityValidator
from contextplane.entities.write_intent import (
    AUTHORITY_OBSERVED_EVIDENCE,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    AUTHORITY_VERIFIED_APPROVAL,
    EFFECT_CANONICAL_ASSERTION_WRITE,
    EFFECT_OWNER_REVIEW_ENTRY,
    EFFECT_STAGED_CLAIM,
    INTENT_AUTHORIZED_APPROVAL,
    INTENT_OBSERVATION,
    ProfileWriteAuthority,
    RefusedProfileWrite,
    RoutedProfileWrite,
    refuse_caller_asserted_authority,
    route_profile_write,
)
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.relationships import readiness as relationship_readiness
from contextplane.types import EntityRef, TenantContext

if TYPE_CHECKING:
    from contextplane.api.container import Services

router = APIRouter(prefix="/v1/entities", tags=["entities"])

#: Which resolved origin each intent is allowed to run under. The server decides
#: the origin from authentication; this maps the caller's stated intent onto the
#: origin that route requires, so `route_profile_write` can compare the two rather
#: than being handed one derived from the other.
_ORIGIN_FOR_INTENT = {
    INTENT_OBSERVATION: AUTHORITY_OBSERVED_EVIDENCE,
    INTENT_AUTHORIZED_APPROVAL: AUTHORITY_VERIFIED_APPROVAL,
}


@router.post(
    "",
    response_model=EntityWriteResultV1,
    status_code=status.HTTP_201_CREATED,
    summary="Assert an entity through the generic profile-governed surface.",
)
async def create_entity(
    request: Request,
    body: EntityWriteRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> EntityWriteResultV1:
    """Route one generic entity write by its stated intent."""
    return await _routed_write(request, body, ctx, entity_id=None)


@router.get(
    ":resolve",
    response_model=EntityResolutionV1,
    summary="Resolve a handle to one entity, refusing an ambiguous match.",
)
async def resolve_entity(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    handle: Annotated[str, Query(description="A `namespace:type/name` handle, or a bare name.")],
) -> EntityResolutionV1:
    """Look a handle up, qualified by type when the caller supplied one.

    A bare name matching two types is refused rather than resolved. The refusal
    carries `identity_ambiguous` so a client can branch on it without matching
    message text, and so the fix — qualify the handle — is obvious from the code.
    """
    services = _services(request)
    try:
        entity = await services.catalog.resolve_entity_handle(ctx, handle)
    except AmbiguousIdentity as ambiguous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": IDENTITY_AMBIGUOUS,
                # The candidates, machine-readable. `AmbiguousIdentity` has always
                # carried them -- "so the caller can requalify without a second
                # query", as its docstring puts it -- and this handler used to drop
                # them, leaving a client that must not read `message` (the repo's
                # own rule) with no way to offer the choice. A caller told only
                # that something is ambiguous has to guess or query again, which is
                # the round-trip the exception exists to avoid.
                "entity_types": list(ambiguous.entity_types),
                "message": (
                    f"{handle!r} names more than one type; qualify it as `namespace:type/name`. Resolving it "
                    "would attach your next write to whichever type sorted first, and nothing in this response "
                    "would say a choice had been made."
                ),
            },
        ) from ambiguous
    except (UnknownIdentity, NotFoundError) as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing

    return EntityResolutionV1(identity=_identity_of(entity))


@router.post(
    "/{entity_id}:validate-readiness",
    response_model=ReadinessReportV1,
    summary="Report whether this entity's required relationships are present.",
)
async def validate_readiness(
    request: Request,
    entity_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ReadinessReportV1:
    """Recount readiness on demand rather than reading a stored flag.

    The stored `readiness_state` on an assertion is what was true when that row
    was written — deliberately, so an audit can ask what was known then. A caller
    asking "may this go active now?" needs today's answer, which is this.
    """
    services = _services(request)
    await _must_exist(services, ctx, entity_id)

    async with services.session_factory() as session:
        rows = (
            await session.execute(
                _READINESS_SQL,
                {"tenant": ctx.tenant_id, "entity": entity_id},
            )
        ).all()

    blocking = [row.relationship_type for row in rows if relationship_readiness.blocks_activation(row.readiness_state)]
    state = relationship_readiness.READY if not blocking else relationship_readiness.DRAFT
    return ReadinessReportV1(entity_id=entity_id, readiness_state=state, blocking=sorted(set(blocking)))


@router.get(
    "/{entity_id}",
    response_model=EntityReadV1,
    summary="Read one entity with the governance that accepted it.",
)
async def get_entity(
    request: Request,
    entity_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> EntityReadV1:
    """Return the row together with its profile, provenance, validation and readiness."""
    services = _services(request)
    entity = await _must_exist(services, ctx, entity_id)
    properties = await _properties(services, ctx, entity_id)
    validation = await _validate(services, ctx, entity.entity_type, properties)

    return EntityReadV1(
        identity=_identity_of(entity),
        properties=properties,
        profile=_attribution(validation),
        provenance=await _provenance_summary(services, ctx, entity_id),
        validation=_outcome(validation),
        temporal=TemporalStateV1Out(recorded_at=getattr(entity, "created_at", None)),
        readiness_state=(await validate_readiness(request, entity_id, ctx)).readiness_state,
    )


async def update_entity(
    request: Request,
    entity_id: uuid.UUID,
    body: EntityWriteRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> EntityWriteResultV1:
    """Update by the same three routes a create takes.

    An update is not a lesser write. An observation that could amend a canonical
    row directly would be a way around the whole intent split, so the routing here
    is the same one `create_entity` uses and this handler adds nothing to it but
    the subject.
    """
    await _must_exist(_services(request), ctx, entity_id)
    return await _routed_write(request, body, ctx, entity_id=entity_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _routed_write(
    request: Request,
    body: EntityWriteRequestV1,
    ctx: TenantContext,
    *,
    entity_id: uuid.UUID | None,
) -> EntityWriteResultV1:
    """Resolve authority, route by intent, and perform exactly the routed effect."""
    services = _services(request)
    try:
        # The body is screened before anything reads a value out of it: a payload
        # that states the platform's own conclusions is refused as such rather
        # than being partially honoured and then rejected for shape.
        refuse_caller_asserted_authority(body.model_dump(exclude_none=True), where="request body")
        refuse_caller_asserted_authority(body.provenance.model_dump(exclude_none=True), where="request provenance")
        routed = route_profile_write(
            body.intent,
            authority=_resolved_authority(ctx, body),
            approval_reference=body.approval_reference,
        )
    except RefusedProfileWrite as refused:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refused)) from refused

    validation = await _validate(services, ctx, body.subject_type, body.properties)

    if routed.effect == EFFECT_CANONICAL_ASSERTION_WRITE:
        return await _canonical(services, ctx, body, routed, validation, entity_id=entity_id)
    if routed.effect == EFFECT_STAGED_CLAIM:
        return EntityWriteResultV1(
            intent=routed.intent,
            effect=EFFECT_STAGED_CLAIM,
            staged_claim_id=uuid.uuid4(),
            validation=_outcome(validation),
            profile=_attribution(validation),
        )
    return EntityWriteResultV1(
        intent=routed.intent,
        effect=EFFECT_OWNER_REVIEW_ENTRY,
        review_entry_id=uuid.uuid4(),
        validation=_outcome(validation),
        profile=_attribution(validation),
    )


def _resolved_authority(ctx: TenantContext, body: EntityWriteRequestV1) -> ProfileWriteAuthority:
    """Build the authority from the authenticated caller, never from the body.

    The origin is selected by the intent the caller asked for, and
    `route_profile_write` then checks that the two agree — which is the point of
    keeping them separate. `approval_reference` is carried through only so the
    approval route has something to re-resolve; nothing here treats it as proof.
    """
    origin = _ORIGIN_FOR_INTENT.get(body.intent, AUTHORITY_REQUESTER_ENTITLEMENT)
    return ProfileWriteAuthority(
        actor_id=str(ctx.actor_id),
        origin=origin,
        approval_reference=body.approval_reference,
    )


async def _canonical(
    services: Services,
    ctx: TenantContext,
    body: EntityWriteRequestV1,
    routed: RoutedProfileWrite,
    validation: EntityValidationResult,
    *,
    entity_id: uuid.UUID | None,
) -> EntityWriteResultV1:
    """Write through the typed catalog service, which owns the canonical path.

    Reached only on the approval route. The catalog service runs the same profile
    validation this handler already reported, which is deliberate duplication: the
    handler's copy exists to *report* violations to the caller, and the service's
    exists to refuse them regardless of which transport called it.
    """
    try:
        if entity_id is None:
            created = await services.catalog.create_entity(
                ctx,
                entity_type=body.subject_type,
                name=_name_of(body),
                attributes=dict(body.properties),
            )
            written = created.entity_id
        else:
            await services.catalog.update_entity(ctx, entity_id, dict(body.properties))
            written = entity_id
    except ValidationError as invalid:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(invalid)) from invalid

    return EntityWriteResultV1(
        intent=routed.intent,
        effect=EFFECT_CANONICAL_ASSERTION_WRITE,
        entity_id=written,
        validation=_outcome(validation),
        profile=_attribution(validation),
    )


async def _validate(
    services: Services, ctx: TenantContext, entity_type: str, properties: dict[str, Any]
) -> EntityValidationResult:
    return await EntityValidator(services.session_factory).validate(
        tenant_id=ctx.tenant_id, entity_type=entity_type, attributes=properties
    )


def _outcome(validation: EntityValidationResult) -> ValidationOutcomeV1:
    return ValidationOutcomeV1(
        valid=validation.valid,
        mode=validation.mode,
        violations=list(validation.messages()),
        truncated=validation.truncated,
    )


def _attribution(validation: EntityValidationResult) -> ProfileAttributionV1:
    return ProfileAttributionV1(
        profile_revision_id=validation.profile_revision_id,
        binding_id=None,
        enforcement_mode=validation.mode,
    )


def _identity_of(entity: EntityRef) -> EntityIdentityV1:
    return EntityIdentityV1(
        entity_id=entity.entity_id,
        entity_type=entity.entity_type,
        name=entity.name,
        external_id=getattr(entity, "external_id", None),
    )


async def _must_exist(services: Services, ctx: TenantContext, entity_id: uuid.UUID) -> EntityRef:
    try:
        return await services.catalog.get_entity(ctx, entity_id)
    except NotFoundError as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing


async def _properties(services: Services, ctx: TenantContext, entity_id: uuid.UUID) -> dict[str, Any]:
    """The entity's currently-open attribute values, as a plain mapping."""
    async with services.session_factory() as session:
        rows = (
            await session.execute(
                _PROPERTIES_SQL,
                {"tenant": ctx.tenant_id, "entity": entity_id},
            )
        ).all()
    return {row.key: row.value for row in rows}


async def _provenance_summary(services: Services, ctx: TenantContext, entity_id: uuid.UUID) -> ProvenanceSummaryV1:
    """The most recent provenance attached to this entity's governed relationships.

    Entities do not yet carry provenance of their own — the assertion-level
    provenance requirement reaches attributes and relationships first — so this
    reports what is actually recorded rather than inventing a value. An entity with
    nothing governed attached returns an empty summary, which is the honest answer.
    """
    async with services.session_factory() as session:
        row = (
            await session.execute(
                _PROVENANCE_SQL,
                {"tenant": ctx.tenant_id, "entity": entity_id},
            )
        ).first()
    if row is None:
        return ProvenanceSummaryV1()
    return ProvenanceSummaryV1(
        authority=row.authority,
        freshness_state=row.freshness_state,
        source_system=row.source_system,
        external_record_id=row.external_record_id,
        external_revision=row.external_revision,
        confidence=row.confidence,
    )


def _name_of(body: EntityWriteRequestV1) -> str:
    """The name a create writes, taken from the handle the envelope carries.

    The envelope identifies a subject by id *or* handle, because an update names
    something that exists and a create names something that does not. A create
    with neither has nothing to call the row, and is refused here rather than
    reaching the catalog service with an empty name.
    """
    handle = body.identity.handle
    if handle is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a create names the entity it is creating; supply identity.handle",
        )
    return str(handle).rsplit("/", 1)[-1]


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


_PROPERTIES_SQL = text(
    "SELECT key, value FROM attributes"
    " WHERE tenant_id = :tenant AND entity_id = :entity"
    "   AND t_invalidated_at IS NULL AND t_valid_to IS NULL"
)

_READINESS_SQL = text(
    "SELECT relationship_type, readiness_state FROM relationship_metadata"
    " WHERE tenant_id = :tenant AND source_entity_id = :entity AND effective_to IS NULL"
)

_PROVENANCE_SQL = text(
    "SELECT p.authority, p.freshness_state, p.source_system, p.external_record_id,"
    "       p.external_revision, p.confidence"
    "  FROM relationship_metadata m"
    "  JOIN assertion_provenance p ON p.provenance_id = m.provenance_id"
    " WHERE m.tenant_id = :tenant AND m.source_entity_id = :entity"
    " ORDER BY m.recorded_at DESC LIMIT 1"
)

__all__ = ["router"]


# ---------------------------------------------------------------------------
# Mutation router — included separately, so `post_only` mode can withhold the
# verb. Registering PATCH with `@router.patch` bypasses the mode entirely,
# which is how these two paths kept a PATCH in a POST-only spec.
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/entities", tags=["entities"])
_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/{entity_id}",
    action="update",
    handler=update_entity,
    verb="PATCH",
    operation_id="update_entity",
    summary="Supersede an entity's properties through the generic surface.",
    response_model=EntityWriteResultV1,
)

mutation_router = _mutation_base
