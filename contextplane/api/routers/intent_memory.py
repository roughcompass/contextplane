"""Task-memory REST surface: participants, and the checkpoint chain they share.

    GET    /v1/intents/{intent_id}/participants            → GrantListResponse
    POST   /v1/intents/{intent_id}/participants            → GrantResponse (201)
    DELETE /v1/intents/{intent_id}/participants/{actor_id} → 204
    POST   /v1/intents/{intent_id}/checkpoints             → CheckpointResponse (201 | 200)
    GET    /v1/intents/{intent_id}/checkpoints/{id}        → CheckpointResponse
    GET    /v1/checkpoints/by-digest/{digest}          → CheckpointResponse

This router adapts and does not decide. Every authorization rule, every write,
and every SQL statement lives in `IntentGrantService` and `IntentCheckpointService`,
because the MCP surface answers the same questions and a rule enforced in two
adapters is a rule that will eventually be enforced differently in one of them.

**A denial never says why.** `AudienceDenied` becomes a bare 403 with a fixed
body. The service's reason strings go to the audit record and the log, never to
the caller: distinguishing "no such task", "not a participant" and "grant
expired" hands back three answers that together enumerate the tenant's tasks.

**Append answers 201 or 200, and the difference is real.** A replayed
idempotency key returns the checkpoint the first call wrote, with 200, so a
client retrying after a dropped response can tell that its retry did not append
a second step.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Response, status
from fastapi.responses import JSONResponse

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.intent_memory import (
    CheckpointAppend,
    CheckpointResponse,
    GrantCreate,
    GrantListResponse,
    GrantResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.types import TenantContext
from contextplane.workspaces.audience import AudienceDenied
from contextplane.workspaces.schemas.intent_memory import PARTICIPANT_ROLES

router = APIRouter(prefix="/v1", tags=["intent"])

# Reading a task you participate in is an ordinary consumer act. Changing who
# participates is checked by the service against the *task* role, not this one --
# an admin of the tenant is not automatically an owner of every task.
_read_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR])
_write_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN])

#: One body for every denial. Fixed on purpose -- see the module docstring.
_DENIED = {"error": {"code": "forbidden", "message": "not authorized for this task"}}


def _denied() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content=_DENIED)


@router.get("/intents/{intent_id}/participants", response_model=GrantListResponse)
async def list_participants(
    intent_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> GrantListResponse | JSONResponse:
    """Everyone on this task, expired grants included."""
    try:
        grants = await container.intent_grants.list_grants(ctx, intent_id=intent_id)
    except AudienceDenied:
        return _denied()
    return GrantListResponse(grants=[GrantResponse.of(grant) for grant in grants])


@router.post("/intents/{intent_id}/participants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED)
async def add_participant(
    intent_id: Annotated[uuid.UUID, Path()],
    body: GrantCreate,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> GrantResponse | JSONResponse:
    """Grant one actor participation. Only a task owner may."""
    if body.role not in PARTICIPANT_ROLES:
        raise map_catalog_error(
            ValidationError(f"unknown participant role {body.role!r}; legal values are {sorted(PARTICIPANT_ROLES)}")
        )
    try:
        grant = await container.intent_grants.grant(
            ctx,
            intent_id=intent_id,
            actor_id=body.actor_id,
            role=body.role,  # type: ignore[arg-type]  # checked against PARTICIPANT_ROLES above
            expires_at=body.expires_at,
        )
    except AudienceDenied:
        return _denied()
    return GrantResponse.of(grant)


@router.delete(
    "/intents/{intent_id}/participants/{actor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_participant(
    intent_id: Annotated[uuid.UUID, Path()],
    actor_id: Annotated[str, Path()],
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> Response | JSONResponse:
    """End one actor's participation now.

    204 whether or not anything changed. Revoking an already-revoked grant is
    not an error, and reporting 404 for it would tell a caller whether a grant
    it may not read exists.
    """
    try:
        await container.intent_grants.revoke(ctx, intent_id=intent_id, actor_id=actor_id)
    except AudienceDenied:
        return _denied()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/intents/{intent_id}/checkpoints", response_model=CheckpointResponse)
async def append_checkpoint(
    intent_id: Annotated[uuid.UUID, Path()],
    body: CheckpointAppend,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CheckpointResponse | JSONResponse:
    """Append one step to the task's chain.

    The idempotency key is required rather than optional. An append with no key
    cannot be retried safely, and the one thing a client does after a dropped
    response is retry -- so a surface that accepts a keyless append is a surface
    that produces duplicate steps under exactly the condition it will meet.
    """
    if not (idempotency_key or "").strip():
        raise map_catalog_error(
            ValidationError(
                "an Idempotency-Key header is required to append a checkpoint; without one a retry "
                "after a dropped response appends a second step instead of finding the first"
            )
        )
    try:
        await container.intent_grants.assert_participant(ctx, intent_id=intent_id)
        result = await container.intent_checkpoints.append_checkpoint(
            ctx,
            intent_id=intent_id,
            payload=body.model_dump(exclude={"evidence"}),
            idempotency_key=idempotency_key or "",
            evidence=tuple(body.evidence),
        )
    except AudienceDenied:
        return _denied()

    # 201 for a checkpoint this call created, 200 for one it found. A client
    # retrying a dropped response can tell which happened.
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return CheckpointResponse.of(result.checkpoint)


@router.get("/intents/{intent_id}/checkpoints/{checkpoint_id}", response_model=CheckpointResponse)
async def get_checkpoint(
    intent_id: Annotated[uuid.UUID, Path()],
    checkpoint_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> CheckpointResponse | JSONResponse:
    """One checkpoint by id.

    The task id in the path is part of the address rather than a second filter:
    the service authorizes by the checkpoint's own task, so a mismatched pair is
    a 404 rather than a read of somebody else's chain.
    """
    try:
        await container.intent_grants.assert_participant(ctx, intent_id=intent_id)
        checkpoint = await container.intent_checkpoints.get_checkpoint(ctx, checkpoint_id=checkpoint_id)
    except AudienceDenied:
        return _denied()
    if checkpoint.intent_id != intent_id:
        raise map_catalog_error(NotFoundError(f"no checkpoint {checkpoint_id} on task {intent_id}"))
    return CheckpointResponse.of(checkpoint)


@router.get("/checkpoints/by-digest/{digest}", response_model=CheckpointResponse)
async def get_checkpoint_by_digest(
    digest: Annotated[str, Path()],
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> CheckpointResponse | JSONResponse:
    """One checkpoint by content digest.

    Addressed outside any task path on purpose: a digest names the content, and
    a caller holding one from a receipt does not necessarily know which task it
    belongs to. Authorization is unchanged -- the service still refuses a
    checkpoint on a task this actor does not participate in.
    """
    try:
        checkpoint = await container.intent_checkpoints.get_checkpoint_by_digest(ctx, digest=digest)
        # Authorization runs on the checkpoint's own task, after the lookup
        # names it and before any of its content is returned.
        await container.intent_grants.assert_participant(ctx, intent_id=checkpoint.intent_id)
    except AudienceDenied:
        return _denied()
    return CheckpointResponse.of(checkpoint)


__all__ = ["router"]
