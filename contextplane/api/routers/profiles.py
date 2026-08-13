"""Profile administration: publishing governed documents and binding tenants.

REST only, and deliberately not MCP. These routes decide which definitions a
tenant's writes are validated against — an agent able to rebind its own tenant
could publish a permissive profile and then satisfy it, which is the failure
this surface exists to prevent. The read side is the one route here an ordinary
caller uses, and it reports what is already in force rather than changing it.

**The tenant comes from the authenticated context and from nowhere else.** No
request model on this page carries a tenant field. That is not an oversight to
be tidied up later: a body that could name a tenant, or omit one and inherit a
default, is exactly the bypass that makes every downstream validator
meaningless, because a caller could bind itself to a profile that permits what
it wanted to write.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.errors import build_error
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.profile.bindings import (
    Binding,
    BindingError,
    BindingNotFound,
    ConcurrentActivation,
    InvalidTransition,
    RollbackNotReady,
)
from contextplane.profile.schemas.entity import CORE_ENTITY_DEFINITIONS
from contextplane.profile.schemas.interface import (
    CORE_INTERFACE_DEFINITIONS,
    CORE_INTERFACE_VERSIONS,
    InterfaceFamilyDefinition,
)
from contextplane.profile.schemas.relationship import CORE_RELATIONSHIP_DEFINITIONS
from contextplane.profile.service import (
    DuplicatePublicationError,
    ProfileConflictError,
    ProfilePublicationError,
    PublishedDocumentIsImmutable,
)
from contextplane.types import TenantContext

if TYPE_CHECKING:
    from contextplane.api.container import Services

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])

#: The shipped core families, published as-is. The request carries no document:
#: a caller-supplied profile body would let the published authority differ from
#: the one this deployment's compiler can actually validate against, which is
#: the same bypass shape as a caller-supplied tenant.
_CORE_INTERFACES: tuple[InterfaceFamilyDefinition, ...] = (*CORE_INTERFACE_DEFINITIONS, *CORE_INTERFACE_VERSIONS)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class PublishRevisionRequest(BaseModel):
    """Publish a new core revision of a profile."""

    model_config = ConfigDict(extra="forbid")

    profile_family: str = Field(min_length=1, max_length=200)
    profile_name: str = Field(min_length=1, max_length=200)
    semantic_version: str = Field(min_length=1, max_length=64)
    compatibility: str = Field(description="backward_compatible, breaking, or deprecating")
    predecessor_revision_id: uuid.UUID | None = None
    migration_plan_ref: str | None = Field(default=None, max_length=500)


class ProfileRevisionResponse(BaseModel):
    """A published revision's identity and its position in the chain.

    Named for its area rather than `RevisionResponse` because the ARC
    observation surface already publishes a `RevisionResponse` component. Two
    models sharing a class name make FastAPI emit both as module-qualified
    names, which silently renames the *other* area's published component --
    a contract change to an endpoint this task never touched.
    """

    model_config = ConfigDict(extra="forbid")

    profile_revision_id: uuid.UUID
    profile_family: str
    profile_name: str
    semantic_version: str
    document_digest: str
    compatibility: str
    predecessor_revision_id: uuid.UUID | None
    published_at: datetime.datetime


class PublishExtensionRequest(BaseModel):
    """Publish this tenant's extension of a core revision.

    No tenant field: the extension belongs to whoever is authenticated. A
    tenant that could publish into another's namespace could change what that
    tenant's writes are validated against.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1, max_length=200)
    target_core_revision_id: uuid.UUID


class ExtensionResponse(BaseModel):
    """A published extension's identity and what it extends."""

    model_config = ConfigDict(extra="forbid")

    extension_revision_id: uuid.UUID
    namespace: str
    target_core_revision_id: uuid.UUID
    document_digest: str
    extension_points: list[str]
    published_at: datetime.datetime


class PlanBindingRequest(BaseModel):
    """Draft a binding. Governs nothing until it is validated and activated."""

    model_config = ConfigDict(extra="forbid")

    profile_revision_id: uuid.UUID
    extension_revision_ids: list[uuid.UUID] = Field(default_factory=list)
    effective_from: datetime.datetime
    reason: str = Field(min_length=1, max_length=1000)
    audit_reference: str | None = Field(default=None, max_length=500)
    migration_run_id: uuid.UUID | None = None


class BindingTransitionRequest(BaseModel):
    """Move a binding along its state machine."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    audit_reference: str | None = Field(default=None, max_length=500)


class BindingResponse(BaseModel):
    """A binding as the administration surface reports it."""

    model_config = ConfigDict(extra="forbid")

    binding_id: uuid.UUID
    profile_revision_id: uuid.UUID
    extension_set_digest: str
    state: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    migration_run_id: uuid.UUID | None
    rollback_target_binding_id: uuid.UUID | None
    rollback_ready: bool
    actor: str
    reason: str
    audit_reference: str | None
    recorded_at: datetime.datetime


class ConformanceResponse(BaseModel):
    """What is governing this tenant right now."""

    model_config = ConfigDict(extra="forbid")

    bound: bool
    binding: BindingResponse | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/revisions", response_model=ProfileRevisionResponse, status_code=status.HTTP_201_CREATED)
async def publish_revision(
    request: Request,
    body: PublishRevisionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ProfileRevisionResponse:
    """Publish a core revision, or refuse with every conflict at once.

    The definitions compiled here are the shipped core families. The request
    does not carry a document: a caller-supplied profile body would let the
    published authority differ from the one this deployment's compiler can
    actually validate against.
    """
    services = _services(request)
    try:
        published = await services.profiles.publish_revision(
            profile_family=body.profile_family,
            profile_name=body.profile_name,
            semantic_version=body.semantic_version,
            entities=CORE_ENTITY_DEFINITIONS,
            relationships=CORE_RELATIONSHIP_DEFINITIONS,
            interfaces=_CORE_INTERFACES,
            compatibility=body.compatibility,
            published_by=str(ctx.actor_id),
            predecessor_revision_id=body.predecessor_revision_id,
            migration_plan_ref=body.migration_plan_ref,
        )
    except ProfilePublicationError as error:
        raise _translate_publication(error) from error

    return ProfileRevisionResponse(
        profile_revision_id=published.profile_revision_id,
        profile_family=published.profile_family,
        profile_name=published.profile_name,
        semantic_version=published.semantic_version,
        document_digest=published.document_digest,
        compatibility=published.compatibility,
        predecessor_revision_id=published.predecessor_revision_id,
        published_at=published.published_at,
    )


@router.post("/extensions", response_model=ExtensionResponse, status_code=status.HTTP_201_CREATED)
async def publish_extension(
    request: Request,
    body: PublishExtensionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ExtensionResponse:
    """Publish this tenant's extension of a core revision."""
    services = _services(request)
    try:
        published = await services.profiles.publish_extension(
            tenant_id=ctx.tenant_id,
            namespace=body.namespace,
            target_core_revision_id=body.target_core_revision_id,
            entities=CORE_ENTITY_DEFINITIONS,
            relationships=CORE_RELATIONSHIP_DEFINITIONS,
            interfaces=_CORE_INTERFACES,
            published_by=str(ctx.actor_id),
        )
    except ProfilePublicationError as error:
        raise _translate_publication(error) from error

    return ExtensionResponse(
        extension_revision_id=published.extension_revision_id,
        namespace=published.namespace,
        target_core_revision_id=published.target_core_revision_id,
        document_digest=published.document_digest,
        extension_points=list(published.extension_points),
        published_at=published.published_at,
    )


@router.post("/bindings", response_model=BindingResponse, status_code=status.HTTP_201_CREATED)
async def plan_binding(
    request: Request,
    body: PlanBindingRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> BindingResponse:
    """Draft a binding for the authenticated tenant."""
    services = _services(request)
    try:
        binding = await services.profile_bindings.plan_binding(
            tenant_id=ctx.tenant_id,
            profile_revision_id=body.profile_revision_id,
            extension_revision_ids=body.extension_revision_ids,
            effective_from=body.effective_from,
            actor=str(ctx.actor_id),
            reason=body.reason,
            audit_reference=body.audit_reference,
            migration_run_id=body.migration_run_id,
        )
    except BindingError as error:
        raise _translate_binding(error) from error
    return _binding_response(binding)


@router.post("/bindings/{binding_id}/validate", response_model=BindingResponse)
async def validate_binding(
    request: Request,
    body: BindingTransitionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> BindingResponse:
    """Move a planned binding into validation. Still governs nothing."""
    services = _services(request)
    try:
        binding = await services.profile_bindings.start_validation(
            tenant_id=ctx.tenant_id, binding_id=binding_id, actor=str(ctx.actor_id), reason=body.reason
        )
    except BindingError as error:
        raise _translate_binding(error) from error
    return _binding_response(binding)


@router.post("/bindings/{binding_id}/activate", response_model=BindingResponse)
async def activate_binding(
    request: Request,
    body: BindingTransitionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> BindingResponse:
    """Put a validated binding into force, closing the incumbent."""
    services = _services(request)
    try:
        binding = await services.profile_bindings.activate(
            tenant_id=ctx.tenant_id,
            binding_id=binding_id,
            actor=str(ctx.actor_id),
            reason=body.reason,
            audit_reference=body.audit_reference,
        )
    except BindingError as error:
        raise _translate_binding(error) from error
    return _binding_response(binding)


@router.post("/bindings/{binding_id}/rollback", response_model=BindingResponse)
async def begin_rollback(
    request: Request,
    body: BindingTransitionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> BindingResponse:
    """Start rolling an active binding back onto its recorded target."""
    services = _services(request)
    try:
        binding = await services.profile_bindings.begin_rollback(
            tenant_id=ctx.tenant_id, binding_id=binding_id, actor=str(ctx.actor_id), reason=body.reason
        )
    except BindingError as error:
        raise _translate_binding(error) from error
    return _binding_response(binding)


@router.post("/bindings/{binding_id}/rollback/complete", response_model=BindingResponse)
async def complete_rollback(
    request: Request,
    body: BindingTransitionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> BindingResponse:
    """Finish a rollback, restoring the target binding to active."""
    services = _services(request)
    try:
        binding = await services.profile_bindings.complete_rollback(
            tenant_id=ctx.tenant_id, binding_id=binding_id, actor=str(ctx.actor_id), reason=body.reason
        )
    except BindingError as error:
        raise _translate_binding(error) from error
    return _binding_response(binding)


@router.get("/conformance", response_model=ConformanceResponse)
async def read_conformance(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ConformanceResponse:
    """Report the binding governing the authenticated tenant, if any.

    `bound: false` with a null binding is a real answer rather than a 404: a
    tenant with no active binding is governed by core defaults, which is a
    state a caller needs to be able to observe without treating it as an error.
    """
    services = _services(request)
    binding = await services.profile_bindings.active_binding(tenant_id=ctx.tenant_id)
    return ConformanceResponse(bound=binding is not None, binding=_binding_response(binding) if binding else None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def _binding_response(binding: Binding) -> BindingResponse:
    return BindingResponse(
        binding_id=binding.binding_id,
        profile_revision_id=binding.profile_revision_id,
        extension_set_digest=binding.extension_set_digest,
        state=binding.state,
        effective_from=binding.effective_from,
        effective_to=binding.effective_to,
        migration_run_id=binding.migration_run_id,
        rollback_target_binding_id=binding.rollback_target_binding_id,
        rollback_ready=binding.rollback_ready,
        actor=binding.actor,
        reason=binding.reason,
        audit_reference=binding.audit_reference,
        recorded_at=binding.recorded_at,
    )


def _translate_publication(error: ProfilePublicationError) -> Exception:
    """Map a publication refusal onto the status that describes it.

    A compile failure is 422 and carries its conflicts, because the document is
    well-formed and wrong rather than malformed. A duplicate is 409. An attempt
    to mutate something published is 409 too, not 405: the method is allowed
    generally, it is this row that cannot move.
    """
    if isinstance(error, ProfileConflictError):
        return build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="profile_conflicts",
            message=str(error),
        )
    if isinstance(error, DuplicatePublicationError | PublishedDocumentIsImmutable):
        return build_error(status.HTTP_409_CONFLICT, code="profile_conflict", message=str(error))
    return build_error(status.HTTP_400_BAD_REQUEST, code="profile_publication_refused", message=str(error))


def _translate_binding(error: BindingError) -> Exception:
    if isinstance(error, BindingNotFound):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="no such binding")
    if isinstance(error, InvalidTransition | RollbackNotReady):
        return build_error(status.HTTP_409_CONFLICT, code="binding_conflict", message=str(error))
    if isinstance(error, ConcurrentActivation):
        return build_error(status.HTTP_409_CONFLICT, code="binding_raced", message=str(error))
    return build_error(status.HTTP_400_BAD_REQUEST, code="binding_refused", message=str(error))


__all__ = ["router"]
