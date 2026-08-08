"""The role vocabulary every layer gates on.

Four valid roles: consumer, producer, admin, auditor.

The names live here, beside the rest of authentication, rather than in the
HTTP layer that happens to enforce them first. Services, workers, and the ARC
authorization rules all decide on roles, and none of them serve a request --
a service reaching up into the API package to learn what "admin" is called
inverts the dependency arrow for a set of four strings.

``require_roles``, the FastAPI dependency that turns a missing role into an
HTTP 403, stays in the API layer: it is request machinery, not vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable

from contextplane.types import TenantContext

#: Named constants for the four roles. Import these in preference to bare string
#: literals so that renaming or extending the set has a single change point.
ROLE_CONSUMER: str = "consumer"
ROLE_PRODUCER: str = "producer"
ROLE_ADMIN: str = "admin"
ROLE_AUDITOR: str = "auditor"

#: All roles recognised by the system.
VALID_ROLES: frozenset[str] = frozenset({ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR})


def has_any_role(ctx: TenantContext, roles: Iterable[str]) -> bool:
    """Return True if *ctx.roles* intersects *roles*."""
    return any(r in ctx.roles for r in roles)


__all__ = [
    "ROLE_ADMIN",
    "ROLE_AUDITOR",
    "ROLE_CONSUMER",
    "ROLE_PRODUCER",
    "VALID_ROLES",
    "has_any_role",
]
