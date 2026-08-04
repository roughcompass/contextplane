"""ARC preflight + challenge/receipt tools.

Every tool here except ``arc_complete_preflight`` itself calls
``_arc_preflight()`` first. That ordering is the point: REST
re-authenticates on each request, a long-lived MCP connection does not, so
without a preflight gate a credential that changed mid-connection would
keep working until disconnect. Running the gate before any ARC service is
reached also means a caller who never preflighted cannot probe those
services for whether a receipt or an artifact exists.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.mcp import context
from registry.exceptions import ConflictError
from registry.types import Clock


async def _arc_preflight(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> Any:
    """Resolve identity and confirm this connection completed `whoami`.

    Raises `ToolError` carrying one bounded code. Which check refused is
    deliberately not distinguished: the remedy is the same either way,
    and naming it would tell a prober how far they got.
    """
    from registry.arc.service.preflight import (
        PreflightError,
        credential_fingerprint,
        restriction_digest,
    )
    from registry.arc.types import ArcRequestContext

    ctx = await context._resolve_tenant(session_factory, clock)
    registry = context._arc_state("arc_preflight")
    try:
        record = registry.require(
            connection_id=context._request_connection_id.get() or None,
            credential_fingerprint=credential_fingerprint(context._request_token.get()),
            tenant_id=ctx.tenant_id,
            token_restriction_digest=restriction_digest(None),
            now=clock.now(),
        )
    except PreflightError as exc:
        raise ToolError(json.dumps({"code": exc.code, "message": str(exc), "details": {}})) from exc
    return ArcRequestContext(
        tenant=ctx,
        oidc_issuer=record.oidc_issuer,
        host_id=None,
        mcp_session_id=record.connection_id,
    )


# ---------------------------------------------------------------------------
# Tool: arc_complete_preflight
# ---------------------------------------------------------------------------


async def arc_complete_preflight(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Record this connection's identity so ARC tools may be used.

    Call once per connection, before any other arc_* tool. Re-call after
    refreshing a token: a changed credential invalidates the record, and
    every later ARC call is refused until a new preflight is completed.

    Returns:
        JSON object: {preflight, tenant_id, actor_id, roles[]}.
    """
    from registry.arc.service.preflight import (
        credential_fingerprint,
        restriction_digest,
    )

    ctx = await context._resolve_tenant(session_factory, clock)
    registry = context._arc_state("arc_preflight")
    connection_id = context._request_connection_id.get()
    if not connection_id:
        raise ToolError("no server connection identity is associated with this call")

    # Expiry comes from the credential, not from a fixed window here: the
    # preflight must not outlive the authentication behind it.
    expires_at = clock.now() + timedelta(hours=1)
    record = registry.record(
        connection_id=connection_id,
        credential_fingerprint=credential_fingerprint(context._request_token.get()),
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        oidc_issuer=context._validated_issuer(),
        oidc_subject=ctx.oidc_subject,
        roles=tuple(ctx.roles),
        token_restriction_digest=restriction_digest(None),
        authentication_expires_at=expires_at,
        completed_at=clock.now(),
    )
    return json.dumps(
        {
            "preflight": "complete",
            "tenant_id": str(record.tenant_id),
            "actor_id": str(record.actor_id),
            "roles": list(record.roles),
        }
    )


# ---------------------------------------------------------------------------
# Tool: arc_issue_context_challenge
# ---------------------------------------------------------------------------


async def arc_issue_context_challenge(
    session_id: str,
    manifest_claims_digest: str,
    idempotency_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Issue a single-use ARC challenge for this session.

    Requires a completed preflight on this connection.

    Args:
        session_id: The agent session this challenge binds to.
        manifest_claims_digest: SHA-256 hex digest of the canonical manifest claims.
        idempotency_key: Caller-chosen key; an exact retry returns the same challenge.

    Returns:
        JSON object: {arc_nonce, issued_at, expires_at, manifest_claims_digest}.
    """
    import base64

    ctx = await _arc_preflight(session_factory, clock)
    challenges = context._arc_state("arc_challenges")
    try:
        issued = await challenges.issue_challenge(
            ctx,
            session_id=session_id,
            manifest_claims_digest=manifest_claims_digest,
            idempotency_key=idempotency_key,
        )
    except ConflictError as exc:
        raise ToolError(json.dumps({"code": "idempotency_conflict", "message": str(exc), "details": {}})) from exc
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "forbidden", "message": str(exc), "details": {}})) from exc

    return json.dumps(
        {
            "arc_nonce": base64.b64encode(issued.arc_nonce).decode("ascii"),
            "issued_at": issued.issued_at.isoformat(),
            "expires_at": issued.expires_at.isoformat(),
            "manifest_claims_digest": issued.manifest_claims_digest,
        }
    )


# ---------------------------------------------------------------------------
# Tool: arc_get_context_resolution_receipt
# ---------------------------------------------------------------------------


async def arc_get_context_resolution_receipt(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read one ARC resolution receipt.

    Requires a completed preflight on this connection. A receipt in
    another tenant reports as not-found rather than forbidden.

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object: the receipt, with source fields redacted by audience.
    """
    ctx = await _arc_preflight(session_factory, clock)
    reader = context._arc_state("arc_receipt_reader")
    try:
        return json.dumps(await reader.get_receipt(ctx, uuid.UUID(receipt_id)), default=str)
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "validation_error", "message": str(exc), "details": {}})) from exc
    except Exception as exc:
        raise ToolError(json.dumps({"code": "not_found", "message": "receipt not found", "details": {}})) from exc


# ---------------------------------------------------------------------------
# Tool: arc_explain_context_resolution
# ---------------------------------------------------------------------------


async def arc_explain_context_resolution(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Explain why one ARC resolution produced the status it did.

    Requires a completed preflight on this connection. Built from the
    receipt's own record rather than by re-running selection, so it can
    never disagree with what actually happened.

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object: {resolution_status, blocked_reasons[], degraded_reasons[],
        budget, selected[], events[]}.
    """
    ctx = await _arc_preflight(session_factory, clock)
    reader = context._arc_state("arc_receipt_reader")
    try:
        return json.dumps(await reader.explain(ctx, uuid.UUID(receipt_id)), default=str)
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "validation_error", "message": str(exc), "details": {}})) from exc
    except Exception as exc:
        raise ToolError(json.dumps({"code": "not_found", "message": "receipt not found", "details": {}})) from exc


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``, bound to the given
    services."""
    deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    mcp_server.tool()(context._bind_tool(arc_complete_preflight, **deps))
    mcp_server.tool()(context._bind_tool(arc_issue_context_challenge, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_context_resolution_receipt, **deps))
    mcp_server.tool()(context._bind_tool(arc_explain_context_resolution, **deps))


__all__: list[str] = [
    "arc_complete_preflight",
    "arc_issue_context_challenge",
    "arc_get_context_resolution_receipt",
    "arc_explain_context_resolution",
    "register",
]
