"""The composition root: build what is shared, call each area, assemble the container.

This module used to enumerate every service in the application, so every new
service in any domain edited it — 1,231 lines of it. It no longer does. Each
area exposes one registration entry point (`build_<area>_services`) beside the
code it constructs; this module builds only what every area shares, calls the
builders in dependency order threading cross-area collaborators in as explicit
parameters, and expands what they return into the typed `Services` container
field by field. Adding a service to an existing area is an edit to that area's
`wiring.py` and to `contextplane.api.container.Services`, and to nothing here
— the property `scripts/check_file_sizes.py` holds this file's 250-line
ceiling to protect.

Three stages, because the FastAPI `app` does not exist until partway through
startup (the scheduler and its jobs are built first, since `lifespan` closes
over them): `build_core_services` runs before `app` exists;
`attach_core_services`, `build_post_app_services` and `wire_auth_context` run once it does;
`build_services_container` runs inside `lifespan` and reads no `app.state`, so
a field renamed on one side is a construction error here rather than drift.
What the stages hand each other, and the `app.state` keys each still attaches,
live in `contextplane.wiring.stages`.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from contextplane.api.auth.oidc import _OidcCache
from contextplane.api.container import Services
from contextplane.arc import (
    ArcServices as ArcServices,
)
from contextplane.arc import (
    assert_drafter_decision_permits_serving,
    assert_no_legacy_activation_evidence,
    build_arc_services,
)
from contextplane.arc import (
    load_drafter_model_decision as load_drafter_model_decision,
)
from contextplane.auth import wiring as auth_wiring
from contextplane.config import Settings
from contextplane.extraction.strategies import STRATEGIES
from contextplane.service.catalog import wiring as catalog_wiring
from contextplane.service.catalog.core import CatalogService
from contextplane.service.governance import wiring as governance_wiring
from contextplane.service.governance.erasure import ErasureRegistry
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.memory import wiring as memory_wiring
from contextplane.service.notifications import wiring as notification_wiring
from contextplane.service.retrieval import wiring as retrieval_wiring
from contextplane.service.workspace import WorkspaceService
from contextplane.signals.ingest import SignalIngestService
from contextplane.types import Clock, SystemClock
from contextplane.usage.writer import UsageWriter
from contextplane.wiring import stages
from contextplane.workspaces import wiring as layered_context_wiring

# Re-bound under the names `contextplane.main` and the startup/conformance
# tests reach for. Each definition lives beside the wiring it guards or
# describes; these module attributes are also what a test patches out.
CoreServices = stages.CoreServices
PostAppServices = stages.PostAppServices
AuthContext = stages.AuthContext
attach_core_services = stages.attach_core_services
_assert_embedding_dim_matches = retrieval_wiring.assert_embedding_dim_matches
_assert_no_legacy_activation_evidence = assert_no_legacy_activation_evidence
_assert_drafter_decision_permits_serving = assert_drafter_decision_permits_serving


def _area_fields(area: object) -> dict[str, Any]:
    """One area's container, keyed by field name, ready to expand into `Services`.

    Expanded rather than enumerated: enumerating is what made this file grow
    once per service in the whole application. Area containers name their
    fields exactly as `Services` does, so a name that drifts is a `TypeError`
    naming the field at startup, not a service silently missing from the graph.
    """
    return {f.name: getattr(area, f.name) for f in fields(area)}  # type: ignore[arg-type]


def build_core_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> stages.CoreServices:
    """Build the areas that need no `app`, in dependency order.

    Governance first: its visibility service is the cross-tenant chokepoint
    every later area filters through. Notifications next, because adoption
    auto-subscribes through the one subscription service. Retrieval before
    catalog, because the breaking-change advisor searches with it.

    The clock built here is the process's clock and reaches every service on
    the graph, including the ones built after `app` exists — the workspace
    singleton takes it off `app.state` rather than constructing its own, so
    an injected clock moves the whole graph together instead of leaving one
    audit trail stamping from somewhere else.
    """
    clock = SystemClock()
    governance = governance_wiring.build_governance_services(session_factory, clock)
    visibility = governance.visibility
    notifications = notification_wiring.build_notification_services(session_factory, clock, visibility=visibility)
    retrieval = retrieval_wiring.build_retrieval_services(settings, session_factory, clock, visibility=visibility)
    catalog = catalog_wiring.build_catalog_services(
        settings,
        session_factory,
        clock,
        visibility=visibility,
        retrieval=retrieval.retrieval,
        subscriptions=notifications.subscriptions,
    )
    return stages.CoreServices(
        clock=clock,
        governance_area=governance,
        notification_area=notifications,
        retrieval_area=retrieval,
        catalog_area=catalog,
        embedder=retrieval.embedder,
        visibility=visibility,
        catalog=catalog.catalog,
    )


def build_post_app_services(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    settings: Settings,
    *,
    visibility: VisibilityService,
    catalog: CatalogService,
) -> stages.PostAppServices:
    """Build the post-`app` areas — ARC and memory — and attach ARC's live keys.

    Extraction strategies are selected here, not inside the memory area: a
    deployment with no extraction provider must queue nothing at all, or the
    queue grows, is drained into nothing, and costs a write per event for no
    result. The registry also sits above the service layer in the module
    boundary contract, so the root is the one place that may name it.
    `pii_scanner` is an optional deployment hook only the root can see.
    """
    memory_area = memory_wiring.build_memory_services(
        session_factory,
        clock,
        catalog=catalog,
        extraction_strategies=tuple(STRATEGIES.values()) if settings.extraction_provider != "noop" else (),
        pii_scanner=getattr(app.state, "pii_scanner", None),
    )
    arc = build_arc_services(session_factory, clock, settings, visibility=visibility)
    stages.attach_arc_state(app, arc)
    return stages.PostAppServices(arc=arc, memory_area=memory_area, memory=memory_area.memory)


def wire_auth_context(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> stages.AuthContext:
    """Build the loop-dependent auth trio: oidc_cache, entitlement_client, claim_resolver.

    Called from `lifespan`, not `create_app`, because JIT tenant/actor
    resolution needs a running event loop for the entitlement-service HTTP
    client — constructing `httpx.AsyncClient()` itself doesn't, but every
    request that uses it does. `oidc_cache` is built here rather than in
    `contextplane.auth.wiring` for the layering reason recorded on
    `contextplane.wiring.stages.AuthContext`.
    """
    oidc_cache = _OidcCache()
    entitlements = auth_wiring.build_entitlement_services(settings, session_factory)
    stages.attach_auth_state(app, oidc_cache, entitlements.claim_resolver)
    return stages.AuthContext(oidc_cache=oidc_cache, **_area_fields(entitlements))


def build_services_container(
    *,
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: AsyncIOScheduler,
    core: stages.CoreServices,
    arc: stages.PostAppServices,
    auth: stages.AuthContext,
    usage_writer: UsageWriter,
    workspace_service: WorkspaceService,
    erasure: ErasureRegistry | None,
) -> Services:
    """Assemble the typed `Services` container from what every area returned.

    Takes no `app` and reads no `app.state`: every argument is what an area
    builder, `contextplane.wiring.routes.register` (the workspace singleton and
    the erasure registry, both built after the router table is mounted), or
    `contextplane.main.create_app` handed back. Layered context is built here
    rather than earlier because its composer needs three other areas at once.
    """
    layered_context = layered_context_wiring.build_layered_context_services(
        session_factory,
        core.clock,
        retrieval=core.retrieval_area.retrieval,
        claim_serving=arc.memory_area.claim_serving,
        arc_receipt_reader=arc.arc.arc_receipt_reader,
    )
    return Services(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        scheduler=scheduler,
        clock=core.clock,
        usage_writer=usage_writer,
        workspace_service=workspace_service,
        erasure=erasure,
        # Built here, not in an area builder: signals has no `wiring.py` of its
        # own, and inventing one to hold a single stateless service would add a
        # module for the sake of symmetry. Its governance collaborator comes from
        # the memory area, so this stays after that area is built.
        signal_ingest=SignalIngestService(
            session_factory,
            clock=core.clock,
            governance=arc.memory_area.source_governance,
        ),
        **_area_fields(core.governance_area),
        **_area_fields(core.notification_area),
        **_area_fields(core.retrieval_area),
        **_area_fields(core.catalog_area),
        **_area_fields(arc.memory_area),
        **_area_fields(arc.arc),
        **_area_fields(auth),
        **_area_fields(layered_context),
    )
