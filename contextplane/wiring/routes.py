"""Every router mounted on the app, plus the MCP surface it feeds.

`register(app, memory=...)` is called once, from `create_app`, after every
core and ARC service has already been attached to `app.state`
(`contextplane.wiring.services` runs first). `memory` is the one field this
module needs that isn't on `app.state` — see `RouteServices`'s own
docstring. Router registration order is load-bearing for two reasons:
FastAPI resolves overlapping routes (e.g. the capabilities exact-match
PATCH/DELETE vs. the consumer read router's list endpoint) in registration
order, and the OpenAPI operation ordering in the generated spec follows it
too — reordering these calls is a visible diff in `openapi.json` even when
no route's behavior changed.

The workspace singleton and the erasure fan-out registry are built here,
immediately before the MCP surface that is their only consumer that isn't
already a router — moving them to `contextplane.wiring.services` instead would
split "why does WorkspaceService get built once here" from "where it's
used" across two files for no benefit. `register` returns both on a
`RouteServices` for `contextplane.main.create_app` to thread into
`build_services_container` directly.

The router imports for the mode-aware routers below stay inside `register`
rather than moving to the top of this module. Every router module that
calls `get_mode_settings()` at its own import time, or builds its routes
through the shared `_entity_crud` CRUD factory (which does the same thing
one layer down — see `concepts`/`operations`), bakes the current
`CONTEXTPLANE_HTTP_METHODS_MODE` into the `HttpMethodRouter` it builds at that
moment. Switching modes means `importlib.reload`-ing those modules, and a
`from module import router` bound once at this module's own import time
would keep pointing at the pre-reload object forever after. A fresh `from
... import ...` inside `register` re-reads whatever the reloaded module
holds right now, which is what `tests/integration/test_http_methods_mode.py`
depends on — that test discovers the affected set itself, by scanning
`contextplane/api/routers/*.py` for the same two markers, rather than trusting
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

from contextplane.api.mcp.server import create_contextplane_mcp_server, create_mcp_app
from contextplane.api.routers import (
    admin_audit,
    admin_operational_health,
    admin_usage,
    whoami,
)
from contextplane.api.routers import admin_global_vocab as global_vocab_router
from contextplane.api.routers import arc as arc_router
from contextplane.api.routers import arc_activation as arc_activation_router
from contextplane.api.routers import arc_admin as arc_admin_router
from contextplane.api.routers import arc_admin_enrollment as arc_admin_enrollment_router
from contextplane.api.routers import arc_approval as arc_approval_router
from contextplane.api.routers import arc_authoring as arc_authoring_router
from contextplane.api.routers import arc_drafting as arc_drafting_router
from contextplane.api.routers import arc_observation as arc_observation_router
from contextplane.api.routers import context as context_router
from contextplane.api.routers import context_feedback as context_feedback_router
from contextplane.api.routers import intent_memory as task_memory_router
from contextplane.api.routers import learning_reads as learning_reads_router
from contextplane.api.routers import (
    profiles as profiles_router,
)
from contextplane.api.routers import receipts as receipts_router
from contextplane.api.routers import retrieval as retrieval_router
from contextplane.api.routers import signals as signals_router
from contextplane.api.routers import usage as usage_router
from contextplane.api.routers.breaking_change import router as breaking_change_router
from contextplane.api.routers.integrations import router as integrations_router
from contextplane.api.routers.notifications import router as notifications_router
from contextplane.context.derivative_handlers import ReceiptErasure
from contextplane.context.derivatives import ContextDerivativeErasure
from contextplane.ingest.webhook import router as webhook_router
from contextplane.retention.tombstones import KeyedTenantSalt
from contextplane.service.governance.erasure import (
    EmbeddingErasure,
    ErasureRegistry,
    SessionMemoryErasure,
    WorkspaceErasure,
)
from contextplane.service.memory.claim_erasure import ClaimErasure
from contextplane.service.retrieval.embedding_index import EmbeddingIndex
from contextplane.signals.erasure import SignalErasure
from contextplane.usage.erasure import UsageErasure
from contextplane.workspaces.derivative_handlers import CheckpointErasure

if TYPE_CHECKING:
    from contextplane.service.memory.session_events import MemoryService
    from contextplane.service.workspace import WorkspaceService


@dataclass
class RouteServices:
    """What `register` builds beyond the router table: the workspace
    singleton and the erasure fan-out registry, both consumed by
    `contextplane.wiring.services.build_services_container`.
    """

    workspace_service: WorkspaceService
    erasure: ErasureRegistry


def register(app: FastAPI, *, memory: MemoryService) -> RouteServices:
    """Mount every domain router, the workspace/erasure singletons, and MCP.

    `memory` is threaded in as a parameter rather than read off
    `app.state.memory` -- `MemoryService` has no reader outside
    `contextplane.wiring` (it is surfaced by name on
    `contextplane.wiring.stages.PostAppServices` for this call and no other),
    so it flows here as a plain return value instead of a bare `app.state`
    attribute the way `session_factory`, `retrieval`, `catalog`, `clock`,
    `notifications`, and `includes` still do below.
    """
    # These router modules read CONTEXTPLANE_HTTP_METHODS_MODE at their own import
    # time (directly, or through the shared _entity_crud CRUD factory) and
    # bake it into the HttpMethodRouter they build -- a module-level `from
    # ... import ...` would bind once, at this module's own first import, and
    # never see a later `importlib.reload`. See this module's own docstring.
    from contextplane.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        admin_extraction,
        admin_lifecycle,
        admin_memory_curation,
        admin_pii,
        admin_sync,
        admin_vocab,
        artifacts,
        capabilities,
        concepts,
        entities,
        operations,
        relationships,
    )
    from contextplane.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        memory as memory_router,
    )
    from contextplane.api.routers import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
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
    # Task memory: participants and the checkpoint chain they share. Reaches
    # both services through the container like every other router here.
    app.include_router(task_memory_router.router)
    # Receipt lookup and bounded resume.
    app.include_router(receipts_router.router)
    app.include_router(context_router.router)
    app.include_router(memory_router.router)
    # DELETE /v1/memory/sessions/{session_id}/events/{event_id} — registered via
    # HttpMethodRouter so CONTEXTPLANE_HTTP_METHODS_MODE is honoured.
    app.include_router(memory_router.mutation_router)
    app.include_router(profiles_router.router)
    app.include_router(whoami.router)
    app.include_router(arc_router.router)
    app.include_router(arc_admin_router.router)
    app.include_router(arc_admin_enrollment_router.router)
    app.include_router(arc_authoring_router.router)
    app.include_router(arc_approval_router.router)
    app.include_router(arc_observation_router.router)
    app.include_router(arc_activation_router.router)
    app.include_router(arc_drafting_router.router)
    app.include_router(admin_operational_health.router)
    app.include_router(admin_usage.router)
    app.include_router(usage_router.router)
    app.include_router(capabilities.router)
    app.include_router(concepts.router)
    app.include_router(entities.router)
    app.include_router(relationships.router)
    app.include_router(operations.router)
    app.include_router(artifacts.router)
    app.include_router(admin_sync.router)
    app.include_router(admin_vocab.router)
    app.include_router(admin_audit.router)
    app.include_router(admin_pii.router)
    app.include_router(admin_extraction.router)
    app.include_router(admin_memory_curation.router)

    # Mutation routers — PATCH/DELETE registered via HttpMethodRouter so
    # CONTEXTPLANE_HTTP_METHODS_MODE controls the exposed surface.
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
    from contextplane.api.routers.graph import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        capability_graph_router,
        graph_admin_mutation_router,
        projection_router,
    )
    from contextplane.api.routers.graph import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as graph_admin_router,
    )

    app.include_router(graph_admin_router)
    app.include_router(capability_graph_router)
    # Edge-property-schema PATCH via HttpMethodRouter.
    app.include_router(graph_admin_mutation_router)
    # /v1/graph/provider and /v1/graph/consumer projection endpoints.
    app.include_router(projection_router)

    # External-ID registry routers.
    from contextplane.api.routers.external_ids import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        entity_external_ids_router,
        external_systems_admin_router,
    )

    app.include_router(external_systems_admin_router)
    app.include_router(entity_external_ids_router)

    # Adoption routers.
    from contextplane.api.routers.adoptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as adoptions_mutation_router,
    )
    from contextplane.api.routers.adoptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as adoptions_router,
    )

    app.include_router(adoptions_router)
    app.include_router(adoptions_mutation_router)

    # Subscription routers.
    from contextplane.api.routers.subscriptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as subscriptions_mutation_router,
    )
    from contextplane.api.routers.subscriptions import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
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
    from contextplane.api.routers.interface import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as interface_mutation_router,
    )
    from contextplane.api.routers.interface import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as interface_router,
    )

    app.include_router(interface_router)
    # PUT /v1/capabilities/{id}/interface — registered via HttpMethodRouter so
    # CONTEXTPLANE_HTTP_METHODS_MODE is honoured.
    app.include_router(interface_mutation_router)

    # Workspace CRUD + entry CRUD + share + search routers, plus the
    # singleton builder -- all four names come off the same reloaded module,
    # so they stay grouped in one function-local import rather than
    # splitting the builder out to the top: separating them would make it
    # easy for a future edit to hoist the builder without noticing it shares
    # a reload boundary with the routers right below it.
    from contextplane.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        _build_workspace_service,
    )
    from contextplane.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        entry_mutation_router as workspace_entry_mutation_router,
    )
    from contextplane.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        mutation_router as workspace_mutation_router,
    )
    from contextplane.api.routers.workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as workspace_router,
    )

    app.include_router(workspace_router)
    app.include_router(workspace_mutation_router)
    app.include_router(workspace_entry_mutation_router)

    # Progression definition admin endpoints (POST/GET/PUT/DELETE).
    from contextplane.api.routers.admin_progression import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as admin_progression_router,
    )

    app.include_router(admin_progression_router)

    # RTBF admin endpoint — DELETE /v1/admin/actors/{actor_id}/personal-data.
    from contextplane.api.routers.admin_workspaces import (  # noqa: PLC0415 - mode-reload contract: see module docstring, tests/integration/test_http_methods_mode.py
        router as admin_workspaces_router,
    )

    app.include_router(admin_workspaces_router)

    # Consumer read router: /v1/search, /v1/capabilities (list), and
    # /v1/capabilities/{entity_id}/dependencies.
    # Mounted after the capabilities router so FastAPI resolves the exact-match
    # PATCH/DELETE routes first (they share the same prefix).
    app.include_router(retrieval_router.router)

    # Signal ingestion: POST /v1/signals. Appended at the tail rather than
    # grouped with the other context-domain routers above, because it overlaps no
    # existing path and every registration above it is load-bearing where it
    # stands -- moving one to make room for this would be a visible spec diff for
    # a route whose resolution nothing competes for.
    app.include_router(signals_router.router)

    # Feedback about a served answer: POST /v1/context/feedback. Appended beside
    # signal ingestion for the same reason it was -- it overlaps no existing
    # path, so nothing above has to move to make room. The two are neighbours by
    # subject as well: one records what a source observed, the other what a
    # reporter thought of what we served.
    app.include_router(context_feedback_router.router)

    # Aggregate quality reads: GET /v1/learning/*. Mounted after the surface that
    # collects the reports these aggregate, and separate from the operator health
    # router on purpose -- that one answers "is anything broken now" for a console,
    # this answers "is what we serve any good" for an owner over a window. Every
    # figure here is floored where it is constructed, so mounting order carries no
    # privacy weight; it is placed last simply because it overlaps no existing path.
    app.include_router(learning_reads_router.router)

    workspace_svc = _build_workspace_service(app)
    # Surviving bare readers: contextplane.api.routers.workspaces, admin_workspaces
    # read this live rather than through the container.
    app.state.workspace_service = workspace_svc

    # Erasure fans a right-to-be-forgotten request across every subsystem
    # holding personal data. Registered in one place so coverage is a visible
    # list rather than something each subsystem hopes another remembered --
    # a subsystem that is missing here is missing silently, and the person is
    # told their data is gone when some of it is not.

    erasure = ErasureRegistry()

    # The salt resolver reads whatever key material the deployment configured, and
    # refuses rather than improvising when there is none. A deployment that
    # configures no active key still boots and still shows these subsystems in the
    # coverage list; the refusal surfaces when an erasure actually runs, so an
    # erasure that cannot mint a keyed tombstone fails loudly instead of reporting a
    # removal it did not record. One resolver for every participant that mints a
    # tombstone: two would be two answers to "which key is active".
    salts = KeyedTenantSalt(
        app.state.settings.retention_key_material(),
        active_key_id=app.state.settings.retention_active_key_id,
    )

    # Derivatives go FIRST, and the order is the point. Every participant below
    # deletes or minimizes rows it owns; this one reads those same rows to find what
    # the erased actor authored, so that it can schedule removal of the vectors,
    # chunks, summaries, caches, exports and receipt links built from them. Running
    # after a participant that has already deleted its source rows, it finds nothing
    # and silently schedules no propagation -- leaving the erased person's words in
    # every artefact derived from their records while the erasure reports success.
    # That is not hypothetical: the claims participant below deletes the claim rows
    # this one reads, and only that participant's own synchronous embedding cleanup
    # kept the gap from being visible.
    erasure.register(ContextDerivativeErasure(app.state.session_factory, salts))
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
    # The three source families, all after the enqueuer above for the reason it
    # gives. Signals are deleted, feedback free text minimized; receipts and
    # checkpoints are minimized rather than deleted, because both are evidence other
    # rows point at and a delete would fail on exactly the records somebody reported
    # on.
    erasure.register(SignalErasure(app.state.session_factory, salts, clock=app.state.clock))
    erasure.register(ReceiptErasure(app.state.session_factory, salts, clock=app.state.clock))
    erasure.register(CheckpointErasure(app.state.session_factory, salts))
    # Surviving bare readers: tests/integration/test_memory_erasure.py and
    # tests/conformance/test_erasure_coverage.py read this live off a
    # partially-started app.
    app.state.erasure = erasure

    # Mount MCP server under /mcp — same process, same port, no sidecar. The
    # MCP surface is not mode-aware and is deliberately excluded from the
    # reload set the mode test builds (reloading it would replace objects the
    # app has already captured for no behavioral reason), so its imports live
    # at the top of this module rather than function-local.
    contextplane_mcp_server = create_contextplane_mcp_server(
        retrieval=app.state.retrieval,
        catalog=app.state.catalog,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        notifications=app.state.notifications,
        includes=app.state.includes,
        workspace_service=workspace_svc,
    )
    mcp_router = create_mcp_app(server=contextplane_mcp_server, parent_app=app)
    app.mount("/mcp", mcp_router)

    return RouteServices(workspace_service=workspace_svc, erasure=erasure)
