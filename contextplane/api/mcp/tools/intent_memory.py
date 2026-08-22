"""Task-memory MCP tools: the same surface the REST router publishes.

Same services, same authorization, same refusals. Both transports adapt one
pair of services rather than each implementing the surface, because the moment
they diverge the divergence is silent -- an agent calling over MCP would get an
answer a REST caller could not, and nothing would report that as a fault.

**A denial says nothing about why.** Every refusal comes back as the same
message. Distinguishing "no such task", "not a participant" and "grant expired"
would hand a caller three answers that together enumerate the tenant's tasks,
and an MCP client is exactly the caller that can afford to ask a thousand times.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import ConflictError, NotFoundError
from contextplane.types import Clock
from contextplane.workspaces.audience import AudienceDenied
from contextplane.workspaces.schemas.intent_memory import (
    PARTICIPANT_ROLES,
    IntentCheckpointV1,
    IntentParticipantGrantV1,
    ParticipantRole,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

    from contextplane.workspaces.checkpoints import IntentCheckpointService
    from contextplane.workspaces.grants import IntentGrantService

#: One message for every refusal, matching the REST surface's fixed 403 body.
_DENIED = "not authorized for this task"


def _service(name: str, *, label: str) -> object:
    """One task-memory service off the app's typed container, at call time.

    Not a constructor argument to `create_contextplane_mcp_server`: that factory
    runs while the router table is being mounted, before the container exists,
    so anything bound there would be a second instance of a service whose
    retention policy is fixed at construction. Reading it here means REST and
    MCP genuinely share one.
    """
    app = context._request_app.get()
    service = getattr(context._services(app), name, None)
    if service is None:
        raise ToolError(f"{label} is not configured on this deployment")
    return service


def _grants() -> IntentGrantService:
    return cast("IntentGrantService", _service("intent_grants", label="task participation"))


def _checkpoints() -> IntentCheckpointService:
    return cast("IntentCheckpointService", _service("intent_checkpoints", label="task checkpoints"))


def _grant_json(grant: IntentParticipantGrantV1) -> dict[str, Any]:
    return {
        "intent_id": str(grant.intent_id),
        "actor_id": grant.actor_id,
        "role": grant.role,
        "granted_by": grant.granted_by,
        "granted_at": grant.granted_at.isoformat(),
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "resolver_version": grant.resolver_version,
    }


def _checkpoint_json(checkpoint: IntentCheckpointV1) -> dict[str, Any]:
    return {
        "checkpoint_id": str(checkpoint.checkpoint_id),
        "intent_id": str(checkpoint.intent_id),
        "sequence": checkpoint.sequence,
        "predecessor_id": str(checkpoint.predecessor_id) if checkpoint.predecessor_id else None,
        "goal": checkpoint.goal,
        "decisions": list(checkpoint.decisions),
        "assumptions": list(checkpoint.assumptions),
        "completed_checks": list(checkpoint.completed_checks),
        "open_questions": list(checkpoint.open_questions),
        "next_action": checkpoint.next_action,
        "author": checkpoint.author,
        "recorded_at": checkpoint.recorded_at.isoformat(),
        "retention_policy": checkpoint.retention_policy,
        "digest": checkpoint.digest,
    }


async def list_intent_participants(
    intent_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Everyone participating in one task, expired grants included.

    Args:
        intent_id: UUID of the task.

    Returns:
        JSON object with a `grants` array, each carrying role, who granted it,
        when, and when it stops applying.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        found = await _grants().list_grants(ctx, intent_id=uuid.UUID(intent_id))
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    return json.dumps({"grants": [_grant_json(grant) for grant in found]})


async def grant_intent_participation(
    intent_id: str,
    actor_id: str,
    role: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Add one actor to a task. Only a task owner may.

    Args:
        intent_id: UUID of the task.
        actor_id: The actor being granted participation.
        role: One of `reader`, `contributor`, `owner`, `auditor`.

    Returns:
        JSON object for the stored grant.
    """
    if role not in PARTICIPANT_ROLES:
        return json.dumps({"error": f"unknown participant role {role!r}; legal values are {sorted(PARTICIPANT_ROLES)}"})
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        # Narrowed rather than ignored: the membership check above is what makes
        # this safe, and a cast says so where a blanket ignore would hide the
        # next argument that stops matching.
        grant = await _grants().grant(
            ctx, intent_id=uuid.UUID(intent_id), actor_id=actor_id, role=cast("ParticipantRole", role)
        )
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    except ConflictError as exc:
        # Kept in step with the REST adapter deliberately: this module's whole
        # premise is transport parity, and an error one surface reports and the
        # other crashes on is the least parity there is.
        return json.dumps({"error": str(exc)})
    return json.dumps(_grant_json(grant))


async def revoke_intent_participation(
    intent_id: str,
    actor_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """End one actor's participation now. Only a task owner may.

    Idempotent: revoking an already-revoked grant reports `changed: false`
    rather than failing, because a retry after a dropped response must not read
    as a different outcome.

    Args:
        intent_id: UUID of the task.
        actor_id: The actor being removed.

    Returns:
        JSON object with `changed`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        changed = await _grants().revoke(ctx, intent_id=uuid.UUID(intent_id), actor_id=actor_id)
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    return json.dumps({"changed": changed})


async def append_intent_checkpoint(
    intent_id: str,
    goal: str,
    idempotency_key: str,
    decisions: list[str] | None = None,
    assumptions: list[str] | None = None,
    completed_checks: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_action: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Append one step to a task's checkpoint chain.

    `idempotency_key` is required, not optional. An append with no key cannot be
    retried safely, and retrying is the one thing a client does after a dropped
    response -- so a keyless append produces a duplicate step under exactly the
    condition it will meet.

    Args:
        intent_id: UUID of the task.
        goal: What this step was trying to achieve.
        idempotency_key: Caller-chosen key; a repeat returns the first result.
        decisions: Decisions taken at this step.
        assumptions: Assumptions this step rests on.
        completed_checks: Checks that passed.
        open_questions: What is still unresolved.
        next_action: What should happen next, if known.

    Returns:
        JSON object for the checkpoint, with `created` saying whether this call
        wrote it or found one an earlier call under the same key had written.
    """
    if not idempotency_key.strip():
        return json.dumps({"error": "an idempotency_key is required to append a checkpoint"})
    ctx = await context._resolve_tenant(session_factory, clock)
    payload = {
        "goal": goal,
        "decisions": decisions or [],
        "assumptions": assumptions or [],
        "completed_checks": completed_checks or [],
        "open_questions": open_questions or [],
        "next_action": next_action,
    }
    try:
        await _grants().assert_participant(ctx, intent_id=uuid.UUID(intent_id))
        result = await _checkpoints().append_checkpoint(
            ctx, intent_id=uuid.UUID(intent_id), payload=payload, idempotency_key=idempotency_key
        )
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    return json.dumps({**_checkpoint_json(result.checkpoint), "created": result.created})


async def get_intent_checkpoint(
    checkpoint_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """One checkpoint by id.

    Args:
        checkpoint_id: UUID of the checkpoint.

    Returns:
        JSON object for the checkpoint.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        checkpoint = await _checkpoints().get_checkpoint(ctx, checkpoint_id=uuid.UUID(checkpoint_id))
        await _grants().assert_participant(ctx, intent_id=checkpoint.intent_id)
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    except NotFoundError:
        return json.dumps({"error": "no such checkpoint"})
    return json.dumps(_checkpoint_json(checkpoint))


async def get_intent_checkpoint_by_digest(
    digest: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """One checkpoint by content digest.

    A digest names content, so a caller holding one from a receipt need not know
    which task it belongs to. Authorization is unchanged.

    Args:
        digest: The checkpoint's content digest.

    Returns:
        JSON object for the checkpoint.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        checkpoint = await _checkpoints().get_checkpoint_by_digest(ctx, digest=digest)
        await _grants().assert_participant(ctx, intent_id=checkpoint.intent_id)
    except AudienceDenied:
        return json.dumps({"error": _DENIED})
    except NotFoundError:
        return json.dumps({"error": "no such checkpoint"})
    return json.dumps(_checkpoint_json(checkpoint))


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``.

    Only the two things tenant resolution needs are bound. The services
    themselves come off the container at call time -- see `_service`.
    """
    grant_deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    checkpoint_deps: dict[str, Any] = grant_deps

    mcp_server.tool()(context._bind_tool(list_intent_participants, **grant_deps))
    mcp_server.tool()(context._bind_tool(grant_intent_participation, **grant_deps))
    mcp_server.tool()(context._bind_tool(revoke_intent_participation, **grant_deps))
    mcp_server.tool()(context._bind_tool(append_intent_checkpoint, **checkpoint_deps))
    mcp_server.tool()(context._bind_tool(get_intent_checkpoint, **checkpoint_deps))
    mcp_server.tool()(context._bind_tool(get_intent_checkpoint_by_digest, **checkpoint_deps))


__all__ = [
    "append_intent_checkpoint",
    "get_intent_checkpoint",
    "get_intent_checkpoint_by_digest",
    "grant_intent_participation",
    "list_intent_participants",
    "register",
    "revoke_intent_participation",
]
