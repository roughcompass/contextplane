"""Which tenants this credential reaches, so a field naming one can offer it.

E23-T1. Three dashboard fields ask for a `Target tenant ID` or a set of
`Shared tenant UUIDs`, and there was no way to ask which tenants exist.

## This is the only cross-tenant read in the product, and it is not a query

Every other read here is scoped by `tenant_id` in a predicate. This one cannot
be: the question is *which* tenants, so a `WHERE tenant_id = :tenant_id` would
answer "the one you already named".

So it does not read the tenants table for the answer. **It reads the caller's own
entitlements**, which the middleware already resolved from their credential into
`ctx.tenant_memberships`, and consults the table only to attach display names to
tenants the credential had already named. A caller sees exactly the tenants their
credential grants and cannot see one it does not, and that is true by
construction rather than by a predicate somebody has to keep correct.

The failure this shape is chosen against is specific. A version that selected
from `tenants` and filtered afterwards would be one refactor from returning the
deployment's whole tenant list, and the failure would be a disclosure rather than
an error — nothing would break, and every operator would learn the names of every
customer.

## A membership with no row is returned, not dropped

A credential can name a tenant this deployment has not materialised yet, because
tenant rows are created just in time on first sight. Dropping it would tell a
caller they have no access to a tenant they do have access to; showing it with
its slug and no display name is the honest answer, and `is_provisioned` says
which it is.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import TenantContext


@dataclasses.dataclass(frozen=True)
class ReachableTenant:
    """One tenant this credential reaches."""

    tenant_id: uuid.UUID
    tenant_slug: str
    #: The tenant's own name, or `None` when this deployment has not
    #: materialised the row yet. Absent is not the same as unnamed.
    display_name: str | None
    roles: tuple[str, ...]
    #: Whether a row exists here for it. A credential may name a tenant this
    #: deployment has never seen; that is a state, not an error.
    is_provisioned: bool
    #: Whether this is the tenant the current request is acting as.
    is_current: bool


_NAMES = text("SELECT tenant_id, display_name FROM tenants WHERE tenant_id = ANY(:ids)")


class TenantDirectoryService:
    """The tenants one credential reaches, with their names."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reachable(self, ctx: TenantContext) -> tuple[ReachableTenant, ...]:
        """Every tenant this caller's credential grants, current one first.

        Ordered so a picker's first row is the tenant the reader is already in,
        then alphabetically. A list ordered by whatever the entitlement service
        happened to return would reshuffle between requests, and a picker whose
        rows move is one people mis-click.
        """
        memberships = list(ctx.tenant_memberships)
        if not memberships:
            # An entitlement deployment always populates this; a single-tenant
            # or test deployment does not. Reporting the one tenant the request
            # is acting as is the truthful answer for both, and is what makes a
            # picker over this render one row rather than none.
            return (
                ReachableTenant(
                    display_name=await self._name_of(ctx.tenant_id),
                    is_current=True,
                    is_provisioned=True,
                    roles=tuple(sorted(ctx.roles)),
                    tenant_id=ctx.tenant_id,
                    tenant_slug="",
                ),
            )

        async with self._session_factory() as session:
            rows = (await session.execute(_NAMES, {"ids": [m.tenant_id for m in memberships]})).mappings()
        names = {row["tenant_id"]: row["display_name"] for row in rows}

        reachable = [
            ReachableTenant(
                display_name=names.get(membership.tenant_id),
                is_current=membership.tenant_id == ctx.tenant_id,
                is_provisioned=membership.tenant_id in names,
                roles=tuple(sorted(membership.roles)),
                tenant_id=membership.tenant_id,
                tenant_slug=membership.tenant_slug,
            )
            for membership in memberships
        ]
        return tuple(sorted(reachable, key=lambda entry: (not entry.is_current, entry.tenant_slug.lower())))

    async def _name_of(self, tenant_id: uuid.UUID) -> str | None:
        async with self._session_factory() as session:
            row = (await session.execute(_NAMES, {"ids": [tenant_id]})).mappings().first()
        return None if row is None else str(row["display_name"])


__all__ = ["ReachableTenant", "TenantDirectoryService"]
