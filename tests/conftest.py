"""Shared database fixtures: Postgres + pgvector and FakeClock.

One database per pytest session, migrated to head before any test runs.
Where that database comes from is chosen by ``CONTEXTPLANE_TEST_PG`` — a
container, a locally managed cluster, or one you point at with
``DATABASE_URL``. See ``tests/helpers/pg_provider.py``; no container
runtime is required.

Note that `db_session` does *not* roll back: it commits like the
application does, so triggers and constraints behave the way they will
in production. Isolation between sessions comes from the database being
created fresh and dropped afterwards, not from transaction rollback.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from collections.abc import AsyncGenerator, Iterator

# Several integration + conformance tests POST to alias paths like
# ``/v1/capabilities/{id}:delete`` and expect 204. Those aliases are
# only registered when ``CONTEXTPLANE_HTTP_METHODS_MODE`` is ``both`` or
# ``post_only``; the default is ``rest``, which leaves them
# unregistered. Routers register routes at module-import time, so we
# set the env var here — at the very top of the shared conftest, before
# any router gets imported — so the registration is correct regardless
# of which test bucket pytest collects first. Tests that need a specific
# mode (``test_http_methods_mode.py``) still override + reload locally.
os.environ.setdefault("CONTEXTPLANE_HTTP_METHODS_MODE", "both")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from contextplane.config import Settings
from tests.helpers.clock import FakeClock
from tests.helpers.pg_provider import test_database


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    """Save and restore root-logger handlers around each test.

    configure_logging() calls root_logger.handlers.clear() on entry, which
    would wipe caplog's LogCaptureHandler if any test triggered the
    reconfiguration. This fixture protects caplog-based assertions from
    cross-test handler bleed without coupling tests to the logging module.
    """
    original = logging.root.handlers[:]
    yield
    logging.root.handlers[:] = original


@pytest.fixture(scope="session")
def pg_container() -> Iterator[str]:
    """A migrated Postgres 16 + pgvector database for the whole test session.

    Returns the connection URL. The name predates there being more than
    one way to get that database; the source is now whatever
    ``CONTEXTPLANE_TEST_PG`` selects.
    """
    with test_database() as url:
        yield url


@pytest.fixture(scope="session")
def app_settings(pg_container: str) -> Settings:
    # embedding_provider must be pinned. Settings is a plain dataclass, so
    # constructing it here ignores the environment and picks up the production
    # default — an in-process model loaded from a path that exists only inside
    # the container image. Without this, create_app() raises on any dev machine
    # or CI runner. Tests that want real vectors set it themselves.
    return Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )


@pytest_asyncio.fixture
async def db_session(pg_container: str) -> AsyncGenerator[AsyncSession, None]:
    """Per-test AsyncSession against the shared container."""
    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},  # required for asyncpg + pgbouncer compatibility
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def mock_entitlement_service():
    """Yield a respx MockRouter scoped to the entitlement service base URL.

    Tests opt in by depending on this fixture and registering route
    expectations on the yielded router (e.g. `mock_entitlement_service.get(
    url__regex=r"/api/v1/ldap-entitlements.*").respond(200, json=...)`).

    The base URL matches the canonical test value used in jwt_factory.py
    and the new entitlement code paths. Override the URL in a more specific
    fixture if a test needs a different one.
    """
    import respx  # local import: respx is a dev-only dep

    base_url = "https://entitlement.test.local"
    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        yield router
