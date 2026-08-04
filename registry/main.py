"""FastAPI app factory — the composition root for the registry service.

`create_app(settings)` is the one entry point everything else depends on:
uvicorn's ASGI factory (``registry.main:create_app``), test fixtures, and
`scripts/export_openapi.py` all import it from this exact path. Building the
app means constructing the service graph, the background scheduler and its
jobs, the router table, and the OpenAPI/error/tracing contract — each of
those is a `registry.wiring` module's job, not this file's. This function
used to do all of it directly, at 807 lines with 74 imports buried inside
its body, so a change to any one part collided with every other change in
the same function. Splitting it here means each part can be read, tested,
and changed against just the module that owns it.

See `registry.wiring.jobs` for what the scheduler runs (embedding drain,
per-source sync jobs, the consolidation sweep, the audit-partition
archival check, and the workspace/session-memory/usage expiry sweeps) and
`docs/runbook-ops.md` for the audit-partition operator procedure.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from registry.config import Settings, get_settings
from registry.logging_config import configure_logging
from registry.storage.pg import create_engine, get_session_factory
from registry.wiring import http_app, jobs, openapi, routes, services, tracing


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI app. Idempotent — safe to call repeatedly in tests."""
    settings = settings or get_settings()
    configure_logging(settings)
    tracer_provider = tracing._init_otel(settings)

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    core = services.build_core_services(settings, session_factory)
    scheduler, webhook_worker = jobs.build_scheduler(settings, session_factory, core.clock, core.embedder)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        services.wire_auth_context(app, settings, session_factory)
        # Every other field was set as an individual app.state attribute
        # above (attach_core_services / _wire_arc) or inside
        # `routes.register` (workspace_service, erasure); this is the first
        # point where the full container can be built.
        app.state.services = services.build_services_container(app)

        await services._assert_embedding_dim_matches(session_factory, settings)

        await jobs.start(scheduler, session_factory, core.catalog, settings)
        # The usage writer's drain task. Started here rather than at construction
        # because it needs a running event loop, and stopped with a final flush so a
        # rolling deploy does not discard events it already accepted.
        await app.state.usage_writer.start()
        try:
            yield
        finally:
            await app.state.usage_writer.stop()
            scheduler.shutdown(wait=False)
            # Release the webhook worker's HTTP client on shutdown.
            await webhook_worker.close()
            # Close the entitlement-service HTTP client.
            if app.state.entitlement_client is not None:
                await app.state.entitlement_client.aclose()
            # Return the connection pool. This app created the engine, so this
            # app owes it back; until now nothing did, and the pool survived
            # until the process ended.
            #
            # Harmless in a server that outlives its pool and fatal to a test
            # suite that does not: each app built during a run left its pool
            # behind, the count climbed across the session, and the run died on
            # "too many clients" somewhere near the end — in whichever test
            # happened to ask for a connection next, which is never the test at
            # fault. The suite's own fixtures were blamed for it, and its
            # ceiling raised twice to stay ahead of the drift.
            #
            # After every other subsystem above, because they hold sessions
            # from this pool and disposing it first would fail their teardown.
            await engine.dispose()
            # Flush queued spans last, so anything the teardown above emits is
            # included. Spans accumulate in a worker thread and are lost if the
            # process ends without draining them, which makes the traces least
            # complete for exactly the shutdowns worth investigating. The flush
            # blocks, hence the thread; it is bounded by the exporter's
            # per-attempt timeout, so a collector that has gone away delays
            # teardown rather than holding it.
            if tracer_provider is not None:
                await asyncio.to_thread(tracer_provider.shutdown)

    app = FastAPI(
        title=settings.service_name,
        lifespan=lifespan,
        description=openapi._OPENAPI_DESCRIPTION,
        openapi_tags=openapi._OPENAPI_TAGS,
    )

    services.attach_core_services(app, settings, engine, session_factory, scheduler, core)
    services._wire_arc(app, session_factory, core.clock, settings, visibility=core.visibility)
    routes.register(app)

    http_app.register_middleware(app, settings, session_factory)
    openapi._install_openapi_security(app, settings)
    http_app._install_error_envelope(app)
    http_app.instrument(app)
    http_app.register_probes(app, settings, session_factory)

    return app


__all__ = ["create_app"]
