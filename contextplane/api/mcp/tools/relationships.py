"""The generic relationship surface over MCP: the agent-facing twin of the router.

Same routing, same refusals, same three effects. `intent` is a required argument
with no default for the reason the entity tool gives: a default would route an
agent's write somewhere it did not choose, and the agent surface is where that is
least likely to be noticed.

Authority is not an argument. A tool schema is closed by construction, so the
fields a JSON body has to be screened for simply do not exist here.
"""

from __future__ import annotations

import json
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.entities.validation import EntityValidator
from contextplane.entities.write_intent import (
    AUTHORITY_OBSERVED_EVIDENCE,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    AUTHORITY_VERIFIED_APPROVAL,
    EFFECT_CANONICAL_ASSERTION_WRITE,
    INTENT_AUTHORIZED_APPROVAL,
    INTENT_OBSERVATION,
    ProfileWriteAuthority,
    ProfileWriteAuthorityOrigin,
    RefusedProfileWrite,
    route_profile_write,
)
from contextplane.types import Clock

_ORIGIN_FOR_INTENT: dict[str, ProfileWriteAuthorityOrigin] = {
    INTENT_OBSERVATION: AUTHORITY_OBSERVED_EVIDENCE,
    INTENT_AUTHORIZED_APPROVAL: AUTHORITY_VERIFIED_APPROVAL,
}


async def assert_relationship(
    intent: str,
    relationship_type: str,
    source_entity_id: str,
    destination_entity_id: str,
    approval_reference: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Assert a relationship through the generic profile-governed surface.

    `intent` is one of `observation`, `request`, `authorized_approval` and has no
    default. Returns what the write actually did, so a caller can tell a staged
    claim from a canonical assertion.
    """
    tenant = await context._resolve_tenant(session_factory, clock)
    try:
        routed = route_profile_write(
            intent,
            authority=ProfileWriteAuthority(
                actor_id=str(tenant.actor_id),
                origin=_ORIGIN_FOR_INTENT.get(intent, AUTHORITY_REQUESTER_ENTITLEMENT),
                approval_reference=approval_reference,
            ),
            approval_reference=approval_reference,
        )
    except RefusedProfileWrite as refused:
        raise ToolError(str(refused)) from refused

    try:
        source = uuid.UUID(source_entity_id)
        destination = uuid.UUID(destination_entity_id)
    except ValueError as bad:
        raise ToolError(f"endpoint ids are UUIDs: {bad}") from bad

    validation = await EntityValidator(session_factory).validate(
        tenant_id=tenant.tenant_id, entity_type=relationship_type, attributes={}
    )
    return json.dumps(
        {
            "intent": routed.intent,
            "effect": routed.effect,
            "relationship_type": relationship_type,
            "source_entity_id": str(source),
            "destination_entity_id": str(destination),
            "canonical": routed.effect == EFFECT_CANONICAL_ASSERTION_WRITE,
            "validation": {"valid": validation.valid, "mode": validation.mode},
            "recorded_at": clock.now().isoformat(),
        },
        sort_keys=True,
    )


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tool onto `mcp_server`."""
    mcp_server.tool()(context._bind_tool(assert_relationship, session_factory=session_factory, clock=clock))


__all__ = ["assert_relationship", "register"]
