"""Shared TenantContext / ArcRequestContext builders.

Nearly every service-layer test needs an identity to call the service with,
and that identity is almost always "a TenantContext with these ids and this
role list". That constructor call had been retyped under a local `_ctx` name
in dozens of test files. Where the defaulting rule was byte-for-byte the same
across files -- same role, same id-generation strategy, same optional
oidc_subject -- that copy is a re-typed duplicate and lives here once instead.

Local `_ctx` helpers that pin a different default role, thread a
module-scoped tenant/actor constant through their own parameter defaults, or
build something other than a plain TenantContext (e.g. an
ArcRequestContext with a file-specific host/issuer) encode a real per-file
choice. Those stay defined locally -- some as thin wrappers that still
delegate the actual `TenantContext(...)` construction to `tenant_context`
below, so the divergence is only ever the default value, never the
construction logic.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from registry.arc.types import ArcRequestContext
from registry.types import TenantContext
from tests.helpers.arc_fixtures import ArcSeed

# The issuer claim every ARC integration test seeds its challenges/receipts
# against. Not a secret, not per-tenant -- just the fixed value the harness
# and the seeded rows agree on.
_ARC_ISSUER_CLAIMS = {"iss": "https://idp.example.test"}


def tenant_context(
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    roles: Sequence[str] | None = None,
    oidc_subject: str = "",
) -> TenantContext:
    """Canonical TenantContext builder. Missing ids get a fresh uuid4.

    This is the one place that turns "tenant + actor + roles" into the shape
    services receive from auth middleware in tests. The named helpers below
    are fixed default choices layered on top of it; callers with their own
    defaulting rule (a module-scoped constant tenant, a different default
    role) call this directly rather than re-writing the TenantContext(...)
    call.
    """
    return TenantContext(
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        actor_id=actor_id if actor_id is not None else uuid.uuid4(),
        roles=list(roles) if roles is not None else ["producer"],
        oidc_subject=oidc_subject,
    )


def claim_producer_ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    """Producer-role context for the memory/claim integration suite.

    `oidc_subject="s"` is an arbitrary non-empty placeholder subject; no test
    in this group asserts on its value, only that one is present.
    """
    return tenant_context(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"], oidc_subject="s")


def claim_admin_ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    """Admin-role counterpart of `claim_producer_ctx`, for erasure/config tests."""
    return tenant_context(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"], oidc_subject="s")


def random_producer_ctx() -> TenantContext:
    """Producer-role context with a fresh random tenant and actor."""
    return tenant_context(roles=["producer"])


def random_admin_ctx() -> TenantContext:
    """Admin-role context with a fresh random tenant and actor."""
    return tenant_context(roles=["admin"])


def arc_ctx_with_mcp(seed: ArcSeed, *, roles: Sequence[str] | None = None, mcp: str | None = None) -> ArcRequestContext:
    """ArcRequestContext for an already-seeded ArcSeed, with an optional MCP session id.

    `host_id="host-1"` and the issuer claim match the fixed values the ARC
    detail-audience / JIT-retrieval integration tests seed their challenges
    against. `roles` falls back to `["consumer"]` on falsy input (including
    an explicit empty list) to match the two call sites this replaces.
    """
    effective_roles = list(roles) if roles else ["consumer"]
    tenant = tenant_context(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=effective_roles, oidc_subject="s")
    return ArcRequestContext.from_validated_claims(tenant, _ARC_ISSUER_CLAIMS, host_id="host-1", mcp_session_id=mcp)


__all__ = [
    "tenant_context",
    "claim_producer_ctx",
    "claim_admin_ctx",
    "random_producer_ctx",
    "random_admin_ctx",
    "arc_ctx_with_mcp",
]
