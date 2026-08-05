"""Standalone sync-worker entrypoint.

Run via ``python -m registry.sync_worker``.

This module starts the APScheduler-based sync loop — building the async
engine, the catalog service the sync jobs write through, and the scheduler
itself, then registering one cron job per active ``sync_sources`` row and
running until it receives a shutdown signal — without launching the FastAPI
HTTP server. It is the entrypoint used by the Helm sync-worker Deployment
(``deploy/helm/templates/deployment-sync.yaml``).

Construction reuses the same wiring the API process uses
(``registry.storage.pg`` for the engine/session factory,
``registry.wiring.services.build_core_services`` for the ``CatalogService``
sync jobs write through) rather than duplicating it, so the two processes
can never quietly drift onto different construction paths for the same
services.

Usage::

    python -m registry.sync_worker

Environment variables are the same as the API server (DATABASE_URL, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

log = logging.getLogger(__name__)


async def _run() -> None:
    # Import here so startup errors surface with a clear traceback, and so
    # `python -m registry.sync_worker --help`-shaped invocations (none exist
    # today, but nothing stops a future one) don't pay for the whole service
    # graph just to parse arguments.
    from registry.config import get_settings  # noqa: PLC0415 - worker entrypoint
    from registry.ingest.runner import create_scheduler, register_sync_jobs  # noqa: PLC0415 - worker entrypoint
    from registry.service.memory.claim_writer import ClaimService  # noqa: PLC0415 - worker entrypoint
    from registry.service.memory.source_governance import SourceGovernanceService  # noqa: PLC0415 - worker entrypoint
    from registry.service.memory.source_ingest import SourceIngestService  # noqa: PLC0415 - worker entrypoint
    from registry.storage.pg import create_engine, get_session_factory  # noqa: PLC0415 - worker entrypoint
    from registry.wiring.services import build_core_services  # noqa: PLC0415 - worker entrypoint

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    # Reuses the same builder the API process uses for its request-time
    # services; the sync worker only needs `.catalog`, but constructing it
    # via this path means the two processes never build CatalogService two
    # different ways.
    core = build_core_services(settings, session_factory)

    # The connector run loop's one write path into the claim store -- built
    # the same way `registry/wiring/jobs.py::start` builds its own copy for
    # the API process's scheduler, since both are stateless wrappers over
    # `session_factory` and `core.clock`.
    claims = ClaimService(session_factory, clock=core.clock)
    governance = SourceGovernanceService(session_factory, clock=core.clock)
    source_ingest = SourceIngestService(claims=claims, governance=governance, catalog=core.catalog)

    scheduler = create_scheduler(settings)
    scheduler.start()
    # Sources are queried and their cron jobs registered only once the
    # scheduler is already running — matching the order the API process
    # uses in `registry/wiring/jobs.py::start`.
    await register_sync_jobs(
        scheduler=scheduler,
        session_factory=session_factory,
        catalog=core.catalog,
        settings=settings,
        source_ingest=source_ingest,
    )

    # Each sync source carries its own schedule (cron / interval), so there is
    # no single sync-worker-level interval to report. Log the start event alone.
    log.info("sync-worker: scheduler started")

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _handle_signal() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await stop
    finally:
        log.info("sync-worker: shutting down scheduler")
        scheduler.shutdown(wait=True)
        # This process created the engine, so it owes the connection pool
        # back — mirrors the API process's own shutdown in `registry/main.py`.
        await engine.dispose()
        log.info("sync-worker: stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
