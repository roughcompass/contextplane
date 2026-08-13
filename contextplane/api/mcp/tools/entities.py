"""The generic entity surface over MCP: the agent-facing twin of `routers/entities.py`.

Same services, same routing, same refusals. An agent asserting an observation gets
the staged claim a REST caller would get, and an agent that has not been through an
approval cannot reach the canonical validators from here either — which is the
property that makes this safe to hand to an ordinary agent at all.

**Intent is a required argument with no default.** An MCP tool signature can carry
a default and most do; this one deliberately does not. A default would route an
agent's write somewhere it did not choose, and the agent surface is precisely where
that is most likely to go unnoticed.

**Authority is never an argument.** The REST body needs an explicit refusal for
caller-asserted authority because JSON is open unless something closes it; a tool
schema is closed by construction, so the fields simply do not exist here. Both
transports end up prohibiting the same thing; only one has to say so out loud.
"""

from __future__ import annotations

import json
from typing import Any

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


async def assert_entity(
    intent: str,
    subject_type: str,
    name: str,
    properties: dict[str, Any] | None = None,
    approval_reference: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Assert an entity through the generic profile-governed surface.

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

    validation = await EntityValidator(session_factory).validate(
        tenant_id=tenant.tenant_id, entity_type=subject_type, attributes=properties or {}
    )
    return json.dumps(
        {
            "intent": routed.intent,
            "effect": routed.effect,
            "subject_type": subject_type,
            "name": name,
            "validation": {
                "valid": validation.valid,
                "mode": validation.mode,
                "violations": list(validation.messages()),
            },
            "profile_revision_id": str(validation.profile_revision_id) if validation.profile_revision_id else None,
            "canonical": routed.effect == EFFECT_CANONICAL_ASSERTION_WRITE,
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
    mcp_server.tool()(context._bind_tool(assert_entity, session_factory=session_factory, clock=clock))


__all__ = ["assert_entity", "register"]
