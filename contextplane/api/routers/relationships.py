"""/v1/relationships — the generic, profile-governed relationship write surface.

The entity twin of this router explains the intent split; the same three routes
apply here, for the same reason. What is different is what the approval route
reaches: a relationship's canonical write is transactional and takes an aggregate
lock, because "at most three of these per source" is a fact about rows the new one
is not, and an unlocked count-then-write lets two callers both satisfy a maximum
neither of them still satisfies once both have committed.

So the approval route here does not assemble its own insert. It calls the one
write service that owns that transaction, and a refusal comes back as the code
that refused it — endpoint type, duplicate, maximum, cross-organization — rather
than as a generic 422 a caller has to parse prose out of.

**Queries are bounded and one-directional.** A page is capped, and `direction`
picks the stored direction or the derived inverse view. There is no "both": a page
mixing them would have no stable order, and a caller could not tell which half it
was holding.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text

from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.entity_writes import (
    ProfileAttributionV1,
    ProvenanceSummaryV1,
    TemporalStateV1Out,
    ValidationOutcomeV1,
)
from contextplane.api.schemas.relationship_writes import (
    RelationshipEndpointsV1,
    RelationshipPageV1,
    RelationshipQueryV1,
    RelationshipReadV1,
    RelationshipWriteRequestV1,
    RelationshipWriteResultV1,
)
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
from contextplane.exceptions import NotFoundError
from contextplane.relationships import queries as relationship_queries
from contextplane.relationships.service import (
    Endpoint,
    RelationshipWriteRefused,
    RelationshipWriteService,
)
from contextplane.relationships.validation import RelationshipValidationResult, RelationshipValidator
from contextplane.types import TenantContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from contextplane.api.container import Services

router = APIRouter(prefix="/v1/relationships", tags=["relationships"])

_ORIGIN_FOR_INTENT = {
    INTENT_OBSERVATION: AUTHORITY_OBSERVED_EVIDENCE,
    INTENT_AUTHORIZED_APPROVAL: AUTHORITY_VERIFIED_APPROVAL,
}


class _CatalogEndpointResolver:
    """Fills the write service's endpoint port from the catalog read path.

    The relationships package may not import the visibility chokepoint — it sits
    below `contextplane.service` in the import contract — so the port is declared
    there and satisfied here, at the transport, which is already above both. Every
    entity read still goes through the chokepoint; this is the seam that lets it.
    """

    def __init__(self, services: Services, ctx: TenantContext) -> None:
        self._services = services
        self._ctx = ctx

    async def resolve(self, session: AsyncSession, *, tenant_id: uuid.UUID, entity_id: uuid.UUID) -> Endpoint | None:
        try:
            entity = await self._services.catalog.get_entity(self._ctx, entity_id)
        except (NotFoundError, PermissionError):
            return None
        return Endpoint(entity_id=entity.entity_id, entity_type=entity.entity_type, tenant_id=entity.tenant_id)


@router.post(
    "",
    response_model=RelationshipWriteResultV1,
    status_code=status.HTTP_201_CREATED,
    summary="Assert a relationship through the generic profile-governed surface.",
)
async def create_relationship(
    request: Request,
    body: RelationshipWriteRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> RelationshipWriteResultV1:
    """Route one generic relationship write by its stated intent."""
    return await _routed_write(request, body, ctx)


@router.post(
    ":query",
    response_model=RelationshipPageV1,
    summary="Traverse one entity's relationships, bounded and one-directional.",
)
async def query_relationships(
    request: Request,
    body: RelationshipQueryV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> RelationshipPageV1:
    """Read one page of relationships in force, forward or as inverse views."""
    services = _services(request)
    at = body.at if body.at is not None else services.clock.now()

    async with services.session_factory() as session:
        if body.direction == "outgoing":
            rows = await relationship_queries.outgoing(
                session,
                tenant_id=ctx.tenant_id,
                source_entity_id=body.entity_id,
                at=at,
                relationship_type=body.relationship_type,
            )
        else:
            rows = await relationship_queries.incoming(
                session,
                tenant_id=ctx.tenant_id,
                destination_entity_id=body.entity_id,
                at=at,
                relationship_type=body.relationship_type,
            )

    # One row beyond the page is read to answer `has_more`. A count would be a
    # second query over a window that may move between the two.
    window = rows[body.offset : body.offset + body.limit + 1]
    page = window[: body.limit]
    return RelationshipPageV1(
        items=[_read_of(row) for row in page],
        limit=body.limit,
        offset=body.offset,
        has_more=len(window) > body.limit,
    )


@router.get(
    "/{relationship_id}",
    response_model=RelationshipReadV1,
    summary="Read one governed relationship with the governance that accepted it.",
)
async def get_relationship(
    request: Request,
    relationship_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> RelationshipReadV1:
    """Return the stored row together with its profile, provenance and readiness."""
    services = _services(request)
    async with services.session_factory() as session:
        row = (
            (await session.execute(_READ_ONE_SQL, {"tenant": ctx.tenant_id, "rid": relationship_id})).mappings().first()
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relationship not found")
    return _read_of_row(dict(row))


async def update_relationship(
    request: Request,
    relationship_id: uuid.UUID,
    body: RelationshipWriteRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> RelationshipWriteResultV1:
    """Supersede the named assertion, by the same three routes a create takes.

    An observation that could amend a canonical edge directly would be a way
    around the intent split, so this adds nothing to the routing but the subject.

    Only the canonical route supersedes. The staged routes mint a claim id and
    a review-entry id and persist neither — they are placeholders for a staging
    surface that does not exist yet — so an observation or a request against
    this path records nothing that names the edge it was about. That is the
    behaviour a create already has, unchanged here rather than quietly widened.
    """
    services = _services(request)
    async with services.session_factory() as session:
        exists = (await session.execute(_EXISTS_SQL, {"tenant": ctx.tenant_id, "rid": relationship_id})).first()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relationship not found")
    return await _routed_write(request, body, ctx, supersedes=relationship_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _routed_write(
    request: Request,
    body: RelationshipWriteRequestV1,
    ctx: TenantContext,
    *,
    supersedes: uuid.UUID | None = None,
) -> RelationshipWriteResultV1:
    services = _services(request)
    try:
        refuse_caller_asserted_authority(body.model_dump(exclude_none=True), where="request body")
        refuse_caller_asserted_authority(body.provenance.model_dump(exclude_none=True), where="request provenance")
        routed = route_profile_write(
            body.intent,
            authority=ProfileWriteAuthority(
                actor_id=str(ctx.actor_id),
                origin=_ORIGIN_FOR_INTENT.get(body.intent, AUTHORITY_REQUESTER_ENTITLEMENT),
                approval_reference=body.approval_reference,
            ),
            approval_reference=body.approval_reference,
        )
    except RefusedProfileWrite as refused:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refused)) from refused

    validation = await RelationshipValidator(services.session_factory).validate(
        tenant_id=ctx.tenant_id, relationship_type=body.subject_type, properties=body.properties
    )

    if routed.effect == EFFECT_CANONICAL_ASSERTION_WRITE:
        return await _canonical(services, ctx, body, routed, validation, supersedes=supersedes)
    if routed.effect == EFFECT_STAGED_CLAIM:
        return RelationshipWriteResultV1(
            intent=routed.intent,
            effect=EFFECT_STAGED_CLAIM,
            staged_claim_id=uuid.uuid4(),
            validation=_outcome(validation),
            profile=_attribution(validation),
        )
    return RelationshipWriteResultV1(
        intent=routed.intent,
        effect=EFFECT_OWNER_REVIEW_ENTRY,
        review_entry_id=uuid.uuid4(),
        validation=_outcome(validation),
        profile=_attribution(validation),
    )


async def _canonical(
    services: Services,
    ctx: TenantContext,
    body: RelationshipWriteRequestV1,
    routed: RoutedProfileWrite,
    validation: RelationshipValidationResult,
    *,
    supersedes: uuid.UUID | None = None,
) -> RelationshipWriteResultV1:
    """Write through the transactional relationship service, which owns the lock.

    The refusal codes come back as they were raised. A caller told
    `maximum_cardinality_exceeded` can act on it; a caller told `422` with prose
    has to parse the prose, and prose changes.
    """
    service = RelationshipWriteService(endpoints=_CatalogEndpointResolver(services, ctx))
    try:
        async with services.session_factory() as session, session.begin():
            if supersedes is None:
                asserted = await service.assert_relationship(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=ctx.actor_id,
                    relationship_type=body.subject_type,
                    source_entity_id=body.endpoints.source_entity_id,
                    destination_entity_id=body.endpoints.destination_entity_id,
                    properties=dict(body.properties),
                    now=services.clock.now(),
                )
            else:
                asserted = await service.supersede_relationship(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=ctx.actor_id,
                    relationship_id=supersedes,
                    relationship_type=body.subject_type,
                    source_entity_id=body.endpoints.source_entity_id,
                    destination_entity_id=body.endpoints.destination_entity_id,
                    properties=dict(body.properties),
                    now=services.clock.now(),
                )
    except RelationshipWriteRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": refused.code, "message": refused.detail},
        ) from refused

    return RelationshipWriteResultV1(
        intent=routed.intent,
        effect=EFFECT_CANONICAL_ASSERTION_WRITE,
        relationship_id=asserted.relationship_id,
        readiness_state=asserted.readiness_state,
        validation=_outcome(validation),
        profile=_attribution(validation),
    )


def _outcome(validation: RelationshipValidationResult) -> ValidationOutcomeV1:
    return ValidationOutcomeV1(
        valid=validation.valid,
        mode=validation.mode,
        violations=list(validation.messages()),
        truncated=validation.truncated,
    )


def _attribution(validation: RelationshipValidationResult) -> ProfileAttributionV1:
    return ProfileAttributionV1(
        profile_revision_id=validation.profile_revision_id,
        binding_id=None,
        enforcement_mode=validation.mode,
    )


def _read_of(row: relationship_queries.GovernedRelationship) -> RelationshipReadV1:
    return RelationshipReadV1(
        relationship_id=row.relationship_id,
        relationship_type=row.relationship_type,
        endpoints=RelationshipEndpointsV1(
            source_entity_id=row.source_entity_id, destination_entity_id=row.destination_entity_id
        ),
        properties=dict(row.properties),
        profile=ProfileAttributionV1(
            profile_revision_id=None, binding_id=row.profile_binding_id, enforcement_mode="mandatory"
        ),
        provenance=ProvenanceSummaryV1(),
        validation=ValidationOutcomeV1(valid=True, mode="mandatory"),
        temporal=TemporalStateV1Out(
            effective_from=row.effective_from, effective_to=row.effective_to, recorded_at=row.recorded_at
        ),
        readiness_state=row.readiness_state,
        is_inverse=row.is_inverse,
    )


def _read_of_row(row: Mapping[str, Any]) -> RelationshipReadV1:
    return RelationshipReadV1(
        relationship_id=row["relationship_id"],
        relationship_type=row["relationship_type"],
        endpoints=RelationshipEndpointsV1(
            source_entity_id=row["source_entity_id"], destination_entity_id=row["destination_entity_id"]
        ),
        properties=dict(row["properties"]),
        profile=ProfileAttributionV1(
            profile_revision_id=row["validating_profile_revision_id"],
            binding_id=row["profile_binding_id"],
            enforcement_mode="mandatory",
        ),
        provenance=ProvenanceSummaryV1(
            authority=row["authority"],
            freshness_state=row["freshness_state"],
            source_system=row["source_system"],
            external_record_id=row["external_record_id"],
            external_revision=row["external_revision"],
            confidence=row["confidence"],
        ),
        validation=ValidationOutcomeV1(valid=True, mode="mandatory"),
        temporal=TemporalStateV1Out(
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            recorded_at=row["recorded_at"],
        ),
        readiness_state=row["readiness_state"],
    )


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


_EXISTS_SQL = text("SELECT 1 FROM relationship_metadata WHERE tenant_id = :tenant AND relationship_id = :rid")

_READ_ONE_SQL = text(
    "SELECT m.relationship_id, m.relationship_type, m.source_entity_id, m.destination_entity_id,"
    "       m.properties, m.effective_from, m.effective_to, m.recorded_at, m.readiness_state,"
    "       m.profile_binding_id, p.authority, p.freshness_state, p.source_system,"
    "       p.external_record_id, p.external_revision, p.confidence,"
    "       p.validating_profile_revision_id"
    "  FROM relationship_metadata m"
    "  JOIN assertion_provenance p ON p.provenance_id = m.provenance_id"
    " WHERE m.tenant_id = :tenant AND m.relationship_id = :rid"
)

__all__ = ["router"]


# ---------------------------------------------------------------------------
# Mutation router — included separately, so `post_only` mode can withhold the
# verb. Registering PATCH with `@router.patch` bypasses the mode entirely,
# which is how these two paths kept a PATCH in a POST-only spec.
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/relationships", tags=["relationships"])
_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/{relationship_id}",
    action="update",
    handler=update_relationship,
    verb="PATCH",
    operation_id="update_relationship",
    summary="Supersede a relationship through the generic surface.",
    response_model=RelationshipWriteResultV1,
)

mutation_router = _mutation_base
