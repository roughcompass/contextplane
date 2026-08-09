"""Auth-area service construction: the entitlement client and the claim resolver.

One registration entry point per area, called by the composition root.

This area covers the *entitlement* half of the auth trio and deliberately not
the third member. `_OidcCache` is declared in `contextplane.api.auth.oidc`,
and the module boundary contract places `api` nine layers above `auth`;
constructing it here would be an upward import from the bottom of the tree
into the HTTP surface. It is therefore built by the composition root, which
is allowed to see both — measured, not assumed, and recorded in
`contextplane.wiring.services.wire_auth_context`.

Both fields below are `None` on a deployment that has not configured
`ENTITLEMENT_SERVICE_URL`. That is a real deployment shape, not a degraded
one: the tenant middleware fails closed on the first authenticated request
("claim resolver not configured"), which is the loud signal a half-configured
deployment should get rather than a silently permissive one.

Construction happens inside the app's lifespan rather than at app-factory
time. Building `httpx.AsyncClient()` needs no running event loop, but every
request that uses it does, and the resolver exists only to serve those.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.auth.entitlements.client import fetch_entitlements
from contextplane.auth.entitlements.resolver import EntitlementResolver
from contextplane.config import Settings


@dataclass(frozen=True)
class EntitlementServices:
    """What this area contributes to the typed container, by container field name."""

    entitlement_client: httpx.AsyncClient | None
    claim_resolver: EntitlementResolver | None


def build_entitlement_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> EntitlementServices:
    """Construct the entitlement client and the claim resolver bound to it."""
    if not settings.entitlement_service_url:
        return EntitlementServices(entitlement_client=None, claim_resolver=None)

    client = httpx.AsyncClient()
    return EntitlementServices(
        entitlement_client=client,
        claim_resolver=EntitlementResolver(
            settings=settings,
            session_factory=session_factory,
            fetcher=functools.partial(fetch_entitlements, client),
        ),
    )
