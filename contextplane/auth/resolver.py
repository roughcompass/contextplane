"""Base class and shared data types for claim-source resolvers.

All resolver implementations return the same ``ResolvedIdentity`` shape.
``EntitlementResolver`` (``contextplane.auth.entitlements.resolver``) is the
single concrete implementation wiring constructs directly today.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data types shared across all resolver implementations


@dataclass(frozen=True)
class AuditIdentity:
    """Caller identity recorded in every audit log entry.

    `email` is optional: deployments backed by identity providers that do not
    surface email addresses (e.g. entitlement-service-only deployments) record `None` explicitly
    so the gap is visible in the audit log rather than silently filled.

    `preferred_username` always falls back to `sub` when the identity provider
    does not supply a display name — it is therefore always non-None.
    """

    sub: str
    email: str | None
    preferred_username: str


@dataclass(frozen=True)
class TenantGrant:
    """One tenant-scoped role grant derived from a resolver's claims.

    `tenant_id` is the catalog-internal UUID (the row in the `tenants` table).
    `tenant_external_id` is the stable external identifier used by the
    originating identity system (e.g. the LDAP slug for entitlement-resolved deployments).
    `catalog_role` is one of the roles enumerated in `role_mappings`
    (admin | producer | auditor | viewer).
    """

    tenant_id: uuid.UUID
    tenant_external_id: str
    catalog_role: str


@dataclass
class ResolvedIdentity:
    """Output contract for every `ClaimResolverBase.resolve()` call.

    `user_id` is the stable opaque subject from the token (`sub` claim or
    equivalent). It is used as the key for per-subject caches and for
    actor-row lookups — it is NOT a UUID.

    `tenant_grants` is the full set of tenant-scoped roles the caller holds.
    An empty list means the caller is authenticated but holds no grants; the
    middleware translates this to HTTP 403 before any service code is reached.

    `audit_identity` carries the fields written to the audit log for every
    write operation. It is populated from the actors table where available
    and falls back to subject-only values when the actors table has not yet
    been seeded (actor JIT upsert is handled by a separate task).
    """

    user_id: str
    tenant_grants: list[TenantGrant] = field(default_factory=list)
    audit_identity: AuditIdentity | None = None


# ---------------------------------------------------------------------------
# Abstract base class


class ClaimResolverBase(ABC):
    """Abstract base for all claim-source resolver implementations.

    Concrete subclasses implement `resolve` to convert a raw claims dict
    into a `ResolvedIdentity`.

    Subclasses may be stateful (e.g. holding a session factory or a cache)
    but must be safe for concurrent async use — every request shares the same
    resolver instance.
    """

    @abstractmethod
    async def resolve(self, claims: dict[str, Any]) -> ResolvedIdentity:
        """Convert raw token claims into a `ResolvedIdentity`.

        Implementations are responsible for:
        - Extracting the subject from `claims["sub"]` (or equivalent).
        - Fetching and parsing any external data needed to derive tenant grants.
        - Materialising JIT tenants and actors when required.
        - Returning a `ResolvedIdentity` with the caller's full grant set.

        On hard failures (upstream 5xx, missing required claim), raise rather
        than returning empty grants — the middleware translates exceptions to
        the appropriate HTTP error code.
        """


__all__ = [
    "AuditIdentity",
    "ClaimResolverBase",
    "ResolvedIdentity",
    "TenantGrant",
]
