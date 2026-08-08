"""Role-gated access helper.

``require_roles(required)`` returns a FastAPI dependency that asserts the
resolved ``TenantContext`` carries at least one of the roles in *required*.
On failure it raises ``HTTPException(403)``; the raised exception message
references the missing roles to ease debugging.

The role names themselves live in ``contextplane.auth.roles`` -- every layer
decides on roles, so the vocabulary cannot belong to the one that speaks HTTP.
This module holds only the request-time enforcement.

Usage in a router::

    _admin_dep = require_roles(["admin"])

    @router.get("/...")
    async def handler(ctx: TenantContext = Depends(_admin_dep)):
        ...

Module-level closures are used so the ``Depends(require_roles([...]))`` call
is not inside a function body (avoids ruff B008 "do not perform function calls
in default arguments").
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.types import TenantContext


def require_roles(required: list[str]) -> Callable[..., TenantContext]:
    """Return a FastAPI dependency that enforces role-based access.

    The dependency resolves ``TenantContext`` via ``get_tenant_context`` and
    raises ``HTTP 403`` if ``ctx.roles`` does not intersect *required*.
    An empty *required* list always passes (no-op guard).

    :param required: At least one of these roles must be present.
    """

    async def _dep(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if required and not any(r in ctx.roles for r in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires one of: {required}; got: {ctx.roles}",
            )
        return ctx

    return _dep  # type: ignore[return-value]


__all__ = ["require_roles"]
