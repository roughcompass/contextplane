"""What the composition root's stages hand each other, and the `app.state` seam.

`contextplane.wiring.services` builds the app in three stages, because the
FastAPI `app` object does not exist until partway through startup. This module
holds the two things those stages share and neither of them owns alone: the
handoff types (`CoreServices`, `PostAppServices`, `AuthContext`), and the
`app.state` attachments each stage still has to make.

Why the attachments are here rather than in the area that built the service:
`app.state.<key> = ...` is a closed set. `scripts/check_state_access.py`
permits it only inside `contextplane/wiring/` and only for keys named in its
`_WIRING_ASSIGNABLE_KEYS`, with each key's live reader recorded beside it
there — a service attached anywhere else, or under a key nobody registered, is
a gate failure rather than a quiet second service locator. Area builders
therefore return their services and this module attaches the handful that
still have a reader outside the typed container.

Every one of those readers is a router, middleware, or test harness that has
not moved to `contextplane.api.container.Services` — the container is a frozen
snapshot taken once at startup, so a reader that must see a post-startup swap
(a rotated claim resolver, a test's replacement usage writer) genuinely cannot
go through it. The rest are simply not migrated yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.auth.oidc import _OidcCache
from contextplane.arc.wiring import ArcServices
from contextplane.auth.entitlements.resolver import EntitlementResolver
from contextplane.auth.wiring import EntitlementServices
from contextplane.config import Settings
from contextplane.service.catalog.core import CatalogService
from contextplane.service.catalog.wiring import CatalogServices
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.governance.wiring import GovernanceServices
from contextplane.service.memory.session_events import MemoryService
from contextplane.service.memory.wiring import MemoryServices
from contextplane.service.notifications.wiring import NotificationServices
from contextplane.service.retrieval.wiring import RetrievalServices
from contextplane.types import Clock, Embedder
from contextplane.usage.wiring import build_usage_services
from contextplane.usage.writer import UsageWriter


@dataclass(frozen=True)
class CoreServices:
    """The areas built before `app` exists, plus the one clock they share.

    The last three fields are second *names* for objects the areas above
    already hold, not second instances: `contextplane.main.create_app` reads
    them off this object to build the scheduler and to thread collaborators
    into the post-`app` stage, and this object is never expanded into the
    typed container, so naming them twice cannot pass anything twice.
    """

    clock: Clock
    governance_area: GovernanceServices
    notification_area: NotificationServices
    retrieval_area: RetrievalServices
    catalog_area: CatalogServices
    embedder: Embedder
    visibility: VisibilityService
    catalog: CatalogService


@dataclass(frozen=True)
class PostAppServices:
    """The areas built once `app` exists: ARC and memory.

    One object because `create_app` builds both in one step. `memory` is
    surfaced by name for the same reason `CoreServices` surfaces three:
    `contextplane.wiring.routes.register` takes the session memory service
    itself, to register the erasure participant over it.
    """

    arc: ArcServices
    memory_area: MemoryServices
    memory: MemoryService


@dataclass(frozen=True)
class AuthContext(EntitlementServices):
    """The loop-dependent auth trio `wire_auth_context` builds inside `lifespan`.

    The entitlement half comes from `contextplane.auth.wiring`; `oidc_cache` is
    added here because its type is declared in `contextplane.api.auth.oidc`,
    which the module boundary contract places far above the `auth` package —
    only the composition root may see both.
    """

    oidc_cache: _OidcCache


def attach_core_services(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: AsyncIOScheduler,
    core: CoreServices,
) -> UsageWriter:
    """Attach the pre-`app` keys a live reader still needs, and build the usage writer.

    `core` already carries everything the pre-`app` stage built and is threaded
    straight into the container, so most of it never touches `app.state` at
    all. This attaches only the keys something still reads live, and constructs
    the one thing nothing else does: the process's single `UsageWriter`. Two
    writers would each hold their own buffer and each report their own queue
    depth, so the gauge would describe neither.
    """
    app.state.settings = settings
    app.state.session_factory = session_factory
    usage_writer = build_usage_services(session_factory).usage_writer
    app.state.usage_writer = usage_writer
    app.state.scheduler = scheduler
    app.state.clock = core.clock
    app.state.visibility = core.governance_area.visibility
    app.state.retrieval = core.retrieval_area.retrieval
    app.state.subscriptions = core.notification_area.subscriptions
    app.state.notifications = core.notification_area.notifications
    app.state.catalog = core.catalog_area.catalog
    app.state.lifecycle = core.catalog_area.lifecycle
    app.state.external_ids = core.catalog_area.external_ids
    app.state.adoption = core.catalog_area.adoption
    app.state.projections = core.catalog_area.projections
    app.state.breaking_change = core.catalog_area.breaking_change
    app.state.integrations = core.catalog_area.integrations
    app.state.interface_storage = core.catalog_area.interface_storage
    app.state.includes = core.catalog_area.includes
    return usage_writer


def attach_arc_state(app: FastAPI, arc: ArcServices) -> None:
    """Attach the six ARC keys with a reader outside the typed container.

    `contextplane.api.routers.arc` reads the first five live; an ARC MCP
    integration test asserts `arc_preflight` on an app built through
    `create_app` whose lifespan never ran, which works because the stage that
    calls this runs synchronously in `create_app`'s own body.
    """
    app.state.arc_signing = arc.arc_signing
    app.state.arc_clock = arc.arc_clock
    app.state.arc_challenges = arc.arc_challenges
    app.state.arc_jit = arc.arc_jit
    app.state.arc_receipt_reader = arc.arc_receipt_reader
    app.state.arc_preflight = arc.arc_preflight


def attach_auth_state(app: FastAPI, oidc_cache: _OidcCache, claim_resolver: EntitlementResolver | None) -> None:
    """Attach the two auth keys read live rather than through the container.

    `contextplane.api.middleware.tenant` and
    `contextplane.api.mcp.context._resolve_tenant` read both deliberately off
    `app.state`: several test harnesses (and, in principle, an operator
    rotating a credential) replace `app.state.claim_resolver` on an
    already-running app, and the container is a frozen snapshot that would
    keep serving whatever existed the instant it was assembled.
    `entitlement_client` is deliberately absent — its only other reader is the
    lifespan that closes it, from the `AuthContext` it already holds.
    """
    app.state.oidc_cache = oidc_cache
    app.state.claim_resolver = claim_resolver
