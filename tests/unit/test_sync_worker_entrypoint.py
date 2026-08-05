"""Unit tests for `registry/sync_worker.py` — the standalone sync-worker entrypoint.

This module is not imported by anything else in the codebase (it is a Helm
Deployment's process entrypoint, invoked as `python -m registry.sync_worker`),
so nothing short of a dedicated test would notice it silently rotting the next
time the wiring it composes changes shape. Two things are covered:

- A plain import test, so a broken import can never ship invisibly.
- `_run()` driven end-to-end with no real DB: scheduler starts, sync-job
  registration is attempted (and only after the scheduler is already
  running), then the signal-driven stop future is triggered and shutdown is
  verified (scheduler stopped, engine disposed).

No real DB, no sleeps beyond asyncio primitives (`asyncio.sleep(0)` yields to
the event loop without consuming wall-clock time).
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.config import Settings
from registry.service.catalog.core import CatalogService


def _settings(**overrides: Any) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,  # No real jobstore driver required.
        embedding_provider="stub",  # No model artifact required for build_core_services.
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _empty_source_session_factory() -> MagicMock:
    """A session factory answering `register_sync_jobs`'s one query with zero rows.

    Same shape as the mock used in `tests/unit/test_sync_runner.py` for the
    same function.
    """
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


def test_module_imports_cleanly() -> None:
    """Import must never crash — the entrypoint can otherwise rot invisibly."""
    import registry.sync_worker as sync_worker

    assert callable(sync_worker._run)
    assert callable(sync_worker.main)


@pytest.mark.asyncio
async def test_run_starts_scheduler_registers_jobs_then_shuts_down_cleanly() -> None:
    """`_run()` must start the scheduler before registering jobs, then shut down cleanly on signal."""
    settings = _settings()
    session_factory = _empty_source_session_factory()

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    # Records whether the scheduler was already running at the moment
    # register_sync_jobs was invoked — this is the ordering the fix restores.
    scheduler_running_at_registration: list[bool] = []
    register_calls: list[tuple[Any, Any, Any, Any]] = []

    async def _fake_register_sync_jobs(
        scheduler: Any, session_factory: Any, catalog: Any, settings: Any, *, source_ingest: Any = None
    ) -> None:
        scheduler_running_at_registration.append(scheduler.running)
        register_calls.append((scheduler, session_factory, catalog, settings))

    # Capture the signal handlers `_run()` registers instead of sending a real
    # OS signal to the test process — deterministic, and never at risk of
    # tearing down the test runner if the timing assumption is ever wrong.
    captured_handlers: dict[int, Any] = {}
    loop = asyncio.get_running_loop()
    real_add_signal_handler = loop.add_signal_handler

    def _fake_add_signal_handler(sig: int, callback: Any, *args: Any) -> None:
        captured_handlers[sig] = callback

    with (
        patch("registry.config.get_settings", return_value=settings),
        patch("registry.storage.pg.create_engine", return_value=mock_engine),
        patch("registry.storage.pg.get_session_factory", return_value=session_factory),
        patch("registry.ingest.runner.register_sync_jobs", AsyncMock(side_effect=_fake_register_sync_jobs)),
        patch.object(loop, "add_signal_handler", side_effect=_fake_add_signal_handler),
    ):
        from registry.sync_worker import _run

        task = asyncio.create_task(_run())

        # Let the task run up to `await stop` — everything from settings
        # resolution through signal-handler registration executes with no
        # other suspension point in between.
        for _ in range(100):
            if captured_handlers:
                break
            await asyncio.sleep(0)
        assert captured_handlers, "signal handlers were never registered — _run() did not reach the wait point"

        # Registration must have happened, and only once the scheduler was
        # already running (this ordering is exactly what the crash-fix restores).
        assert register_calls, "register_sync_jobs was never invoked"
        assert scheduler_running_at_registration == [True]
        assert register_calls[0][1] is session_factory
        assert isinstance(register_calls[0][2], CatalogService)

        scheduler = register_calls[0][0]
        assert scheduler.running is True

        # Trigger shutdown the same way SIGTERM/SIGINT would.
        assert signal.SIGTERM in captured_handlers
        captured_handlers[signal.SIGTERM]()

        await asyncio.wait_for(task, timeout=5)

    assert scheduler.running is False, "scheduler must be shut down on stop"
    mock_engine.dispose.assert_awaited_once()

    # Restore is automatic (patch.object context manager), but assert the
    # loop's real method is back in place so no other test in the session
    # inherits the fake.
    assert loop.add_signal_handler == real_add_signal_handler
