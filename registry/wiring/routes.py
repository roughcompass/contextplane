"""Every router mounted on the app, plus the MCP surface it feeds.

`register(app, memory=...)` is called once, from `create_app`, after every
core and ARC service has already been attached to `app.state`
(`registry.wiring.services` runs first). `memory` is the one field this
module needs that isn't on `app.state` — see `RouteServices`'s own
docstring. Router registration order is load-bearing for two reasons:
FastAPI resolves overlapping routes (e.g. the capabilities exact-match
PATCH/DELETE vs. the consumer read router's list endpoint) in registration
order, and the OpenAPI operation ordering in the generated spec follows it
too — reordering these calls is a visible diff in `openapi.json` even when
no route's behavior changed.

The workspace singleton and the erasure fan-out registry are built here,
immediately before the MCP surface that is their only consumer that isn't
already a router — moving them to `registry.wiring.services` instead would
split "why does WorkspaceService get built once here" from "where it's
used" across two files for no benefit. `register` returns both on a
`RouteServices` for `registry.main.create_app` to thread into
`build_services_container` directly.

The router imports for the mode-aware routers below stay inside `register`
rather than moving to the top of this module. Every router module that
calls `get_mode_settings()` at its own import time, or builds its routes
through the shared `_entity_crud` CRUD factory (which does the same thing
one layer down — see `concepts`/`operations`), bakes the current
`REGISTRY_HTTP_METHODS_MODE` into the `HttpMethodRouter` it builds at that
moment. Switching modes means `importlib.reload`-ing those modules, and a
`from module import router` bound once at this module's own import time
would keep pointing at the pre-reload object forever after. A fresh `from
... import ...` inside `register` re-reads whatever the reloaded module
holds right now, which is what `tests/integration/test_http_methods_mode.py`
depends on — that test discovers the affected set itself, by scanning
`registry/api/routers/*.py` for the same two markers, rather than trusting
a hand-maintained list (its own docstring names a router that once escaped
manual tracking this way). At last count that set was: `capabilities`,
`concepts`, `operations`, `artifacts`, `admin_lifecycle`, `admin_pii`,
`admin_sync`, `admin_vocab`, `admin_extraction`, `adoptions`,
`subscriptions`, `external_ids`, `graph`, `workspaces`, `memory`,
`memory_curation`, `admin_memory_curation`, `interface`,
`admin_progression`, `admin_workspaces` — re-run the test's own discovery
scan rather than trusting this list if the router table below changes.
Every function-local import below that carries a `PLC0415` suppression is
one of these; every router import that doesn't need reloading has already
been hoisted to the top of the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI

from registry.api.mcp.server import create_mcp_app, create_registry_mcp_server
from registry.api.routers import (
    admin_audit,
    admin_operational_health,
    admin_usage,
    whoami,
)
from registry.api.routers import admin_global_vocab as global_vocab_router
from registry.api.routers import arc as arc_router
from registry.api.routers import arc_admin as arc_admin_router
from registry.api.routers import arc_admin_enrollment as arc_admin_enrollment_router
from registry.api.routers import arc_approval as arc_approval_router
from registry.api.routers import arc_authoring as arc_authoring_router
from registry.api.routers import arc_drafting as arc_drafting_router
from registry.api.routers import retrieval as retrieval_router
from registry.api.routers import usage as usage_router
from registry.api.routers.breaking_change import router as breaking_change_router
from registry.api.routers.integrations import router as integrations_router
from registry.api.routers.notifications import router as notifications_router
from registry.ingest.webhook import router as webhook_router
from registry.service.governance.erasure import (
    EmbeddingErasure,
    ErasureRegistry,
    SessionMemoryErasure,
    WorkspaceErasure,
)
from registry.service.memory.claim_erasure import ClaimErasure
from registry.service.retrieval.embedding_index import EmbeddingIndex
from registry.usage.erasure import UsageErasure

if TYPE_CHECKING:
    from registry.service.memory.session_events import MemoryService
    from registry.service.workspace import WorkspaceService


@dataclass
class RouteServices:
    """What `register` builds beyond the router table: the workspace
    singleton and the erasure fan-out registry, both consumed by
    `registry.wiring.services.build_services_container`.
    """

    workspace_service: WorkspaceService
    erasure: ErasureRegistry


def register(app: FastAPI, *, memory: MemoryService) -> RouteServices:
    """Mount every domain router, the workspace/erasure singletons, and MCP.

    `memory` is threaded in as a parameter rather than read off
    `app.state.memory` -- `MemoryService` has no reader outside
    `registry.wiring` (see `registry.wiring.services.ArcServices`), so it
    flows here as a plain return value instead of a bare `app.state`
    attribute the way `session_factory`, `retrieval`, `catalog`, `clock`,
    `notifications`, and `includes` still do below.
    """
    # These router modules read REGISTRY_HTTP_METHODS_MODE at their own import
    # time (directly, or through the shared _entity_crud CRUD factory) and
    # bake it into the HttpMethodRouter they build -- a module-level `from
    # ... import ...` would bind once, at this module's own first import, and
    # never see a later `importlib.reload`. See this module's own docstring.
    from registry.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        admin_extraction,
        admin_lifecycle,
        admin_memory_curation,
        admin_pii,
        admin_sync,
        admin_vocab,
        artifacts,
        capabilities,
        concepts,
        operations,
    )
    from registry.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        memory as memory_router,
    )
    from registry.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        memory_curation as memory_curation_router,
    )

    app.include_router(global_vocab_router.router)
    # Curation queue, claim link/discard, promotion-proposal read/review,
    # promotion reversal, and claim history/believed -- registered *before*
    # memory_router below, and that ordering is load-bearing the same way
    # memory_router's own `/claims/search` is declared before its
    # `/claims/{claim_id}`: FastAPI matches in declaration order, and
    # memory_router's `GET /claims/{claim_id}` is a bare single-segment GET
    # that would otherwise bind literal siblings like `/claims/believed` to
    # a UUID path parameter and fail validation, never falling through to
    # this router's own route for it. Every plain-POST/PATCH action in this
    # router (`:link`, `:discard`, `:confirm`, `:adjudicate`, the proposal
    # PATCH) is unaffected by this ordering either way -- a GET-only route
    # never wins a method mismatch, so those only ever had one router that
    # could serve them.
    app.include_router(memory_curation_router.router)
    # Promotion-proposal review (PATCH .../{proposal_id}) is this router's
    # first genuine verb mutation -- a real POST-tunnel alias exists for it,
    # unlike the plain-POST actions above -- so it goes through
    # HttpMethodRouter on its own mode-aware mutation_router, mirroring
    # memory_router's own router / mutation_router split.
    app.include_router(memory_curation_router.mutation_router)
    app.include_router(memory_router.router)
    # DELETE /v1/memory/sessions/{session_id}/events/{event_id} — registered via
    # HttpMethodRouter so REGISTRY_HTTP_METHODS_MODE is honoured.
    app.include_router(memory_router.mutation_router)
    app.include_router(whoami.router)
    app.include_router(arc_router.router)
    app.include_router(arc_admin_router.router)
    app.include_router(arc_admin_enrollment_router.router)
    app.include_router(arc_authoring_router.router)
    app.include_router(arc_approval_router.router)
    app.include_router(arc_drafting_router.router)
    app.include_router(admin_operational_health.router)
    app.include_router(admin_usage.router)
    app.include_router(usage_router.router)
    app.include_router(capabilities.router)
    app.include_router(concepts.router)
    app.include_router(operations.router)
    app.include_router(artifacts.router)
    app.include_router(admin_sync.router)
    app.include_router(admin_vocab.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_pii.router)
    app.include_router(admin_extraction.router)
    app.include_router(admin_memory_curation.router)

    # Mutation routers — PATCH/DELETE registered via HttpMethodRouter so
    # REGISTRY_HTTP_METHODS_MODE controls the exposed surface.
    app.include_router(capabilities.mutation_router)
    app.include_router(concepts.mutation_router)
    app.include_router(operations.mutation_router)
    app.include_router(artifacts.mutation_router)
    app.include_router(admin_sync.mutation_router)
    app.include_router(admin_vocab.mutation_router)
    app.include_router(admin_extraction.mutation_router)
    app.include_router(admin_memory_curation.mutation_router)

    # Lifecycle endpoint registered via HttpMethodRouter so mode env var is honoured.
    app.include_router(admin_lifecycle.mutation_router)

    # PII admin endpoints — already use HttpMethodRouter.
    app.include_router(admin_pii.pii_pattern_router)
    app.include_router(admin_pii.pii_field_policy_router)

    # Webhook receiver (public, HMAC-verified).
    app.include_router(webhook_router)

    # Graph routers — admin stubs + reverse traversal + projections.
    from registry.api.routers.graph import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        capability_graph_router,
        graph_admin_mutation_router,
        projection_router,
    )
    from registry.api.routers.graph import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as graph_admin_router,
    )

    app.include_router(graph_admin_router)
    app.include_router(capability_graph_router)
    # Edge-property-schema PATCH via HttpMethodRouter.
    app.include_router(graph_admin_mutation_router)
    # /v1/graph/provider and /v1/graph/consumer projection endpoints.
    app.include_router(projection_router)

    # External-ID registry routers.
    from registry.api.routers.external_ids import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        entity_external_ids_router,
        external_systems_admin_router,
    )

    app.include_router(external_systems_admin_router)
    app.include_router(entity_external_ids_router)

    # Adoption routers.
    from registry.api.routers.adoptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as adoptions_mutation_router,
    )
    from registry.api.routers.adoptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as adoptions_router,
    )

    app.include_router(adoptions_router)
    app.include_router(adoptions_mutation_router)

    # Subscription routers.
    from registry.api.routers.subscriptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as subscriptions_mutation_router,
    )
    from registry.api.routers.subscriptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as subscriptions_router,
    )

    app.include_router(subscriptions_router)
    app.include_router(subscriptions_mutation_router)

    # Notification inbox router.
    app.include_router(notifications_router)

    # Breaking-change advisor router.
    app.include_router(breaking_change_router)

    # Integration-pair lookup router.
    app.include_router(integrations_router)

    # Interface storage router.
    from registry.api.routers.interface import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as interface_mutation_router,
    )
    from registry.api.routers.interface import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as interface_router,
    )

    app.include_router(interface_router)
    # PUT /v1/capabilities/{id}/interface — registered via HttpMethodRouter so
    # REGISTRY_HTTP_METHODS_MODE is honoured.
    app.include_router(interface_mutation_router)

    # Workspace CRUD + entry CRUD + share + search routers, plus the
    # singleton builder -- all four names come off the same reloaded module,
    # so they stay grouped in one function-local import rather than
    # splitting the builder out to the top: separating them would make it
    # easy for a future edit to hoist the builder without noticing it shares
    # a reload boundary with the routers right below it.
    from registry.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        _build_workspace_service,
    )
    from registry.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        entry_mutation_router as workspace_entry_mutation_router,
    )
    from registry.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as workspace_mutation_router,
    )
    from registry.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as workspace_router,
    )

    app.include_router(workspace_router)
    app.include_router(workspace_mutation_router)
    app.include_router(workspace_entry_mutation_router)

    # Progression definition admin endpoints (POST/GET/PUT/DELETE).
    from registry.api.routers.admin_progression import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as admin_progression_router,
    )

    app.include_router(admin_progression_router)

    # RTBF admin endpoint — DELETE /v1/admin/actors/{actor_id}/personal-data.
    from registry.api.routers.admin_workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as admin_workspaces_router,
    )

    app.include_router(admin_workspaces_router)

    # Consumer read router: /v1/search, /v1/capabilities (list), and
    # /v1/capabilities/{entity_id}/dependencies.
    # Mounted after the capabilities router so FastAPI resolves the exact-match
    # PATCH/DELETE routes first (they share the same prefix).
    app.include_router(retrieval_router.router)

    workspace_svc = _build_workspace_service(app)
    # Surviving bare readers: registry.api.routers.workspaces, admin_workspaces
    # read this live rather than through the container.
    app.state.workspace_service = workspace_svc

    # Erasure fans a right-to-be-forgotten request across every subsystem
    # holding personal data. Registered in one place so coverage is a visible
    # list rather than something each subsystem hopes another remembered --
    # a subsystem that is missing here is missing silently, and the person is
    # told their data is gone when some of it is not.

    erasure = ErasureRegistry()
    erasure.register(WorkspaceErasure(workspace_svc))
    # Claims must run BEFORE session memory: deciding whether a claim has
    # independent evidence means checking whether its session refs resolve to a
    # different actor's events, and that check needs the events still present.
    # The registry stops at the first failure, so events cannot be deleted
    # before claim selection has succeeded.
    erasure.register(ClaimErasure(app.state.session_factory))
    erasure.register(SessionMemoryErasure(memory))
    # Vectors carry the source text verbatim, so an erasure that stopped at the source
    # tables would leave the erased person's own words searchable.
    erasure.register(EmbeddingErasure(EmbeddingIndex(app.state.session_factory)))
    # Raw usage rows name the actor who made each call. The writer goes in too, so
    # an event still buffered when the request arrives cannot flush afterwards and
    # put the actor back into a table they were just erased from.
    erasure.register(UsageErasure(app.state.session_factory, writer=app.state.usage_writer))
    # Surviving bare readers: tests/integration/test_memory_erasure.py and
    # tests/conformance/test_erasure_coverage.py read this live off a
    # partially-started app.
    app.state.erasure = erasure

    # Mount MCP server under /mcp — same process, same port, no sidecar. The
    # MCP surface is not mode-aware and is deliberately excluded from the
    # reload set the mode test builds (reloading it would replace objects the
    # app has already captured for no behavioral reason), so its imports live
    # at the top of this module rather than function-local.
    registry_mcp_server = create_registry_mcp_server(
        retrieval=app.state.retrieval,
        catalog=app.state.catalog,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        notifications=app.state.notifications,
        includes=app.state.includes,
        workspace_service=workspace_svc,
    )
    mcp_router = create_mcp_app(server=registry_mcp_server, parent_app=app)
    app.mount("/mcp", mcp_router)

    return RouteServices(workspace_service=workspace_svc, erasure=erasure)
