"""Shared integration fixtures: Postgres + pgvector, FakeClock, and respx
cassette transport for connector HTTP mocking.

One database per pytest session, migrated to head before any test runs.
Where that database comes from is chosen by ``REGISTRY_TEST_PG`` — a
container, a locally managed cluster, or one you point at with
``DATABASE_URL``. See ``tests/helpers/pg_provider.py``; no container
runtime is required.

Note that `db_session` does *not* roll back: it commits like the
application does, so triggers and constraints behave the way they will
in production. Isolation between sessions comes from the database being
created fresh and dropped afterwards, not from transaction rollback.

Cassette infrastructure
-----------------------
``respx_cassette(connector_name)`` is a session-scoped factory fixture.  Call
it inside a test or fixture to get a ``respx.MockRouter`` pre-loaded with all
``cassette_*.json`` files found under
``tests/fixtures/connectors/<connector_name>/``.

Cassette file schema (each file is one HTTP exchange)::

    {
        "request": {
            "method": "GET",
            "url": "https://...",
            "headers": {"Authorization": "Bearer test-token"}
        },
        "response": {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": <string or JSON-serialisable object>
        }
    }

Set ``REFRESH_CASSETTES=1`` to enable pass-through mode (no mocking); real
network calls are made and results can be captured to update the cassette files.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from collections.abc import AsyncGenerator, Callable, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from registry.config import Settings
from tests.helpers.clock import FakeClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "connectors"


def _load_cassette(path: Path) -> dict:
    """Load and parse a single cassette JSON file."""
    with path.open() as fh:
        return json.load(fh)


def _response_from_cassette(entry: dict) -> Response:
    """Build an httpx.Response from the ``response`` block of a cassette entry."""
    resp = entry["response"]
    body = resp["body"]
    if not isinstance(body, str):
        body = json.dumps(body)
    return Response(
        status_code=resp.get("status", 200),
        headers=resp.get("headers", {}),
        text=body,
    )


# ---------------------------------------------------------------------------
# Cassette fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def respx_cassette() -> Callable[[str], respx.MockRouter]:
    """Return a factory that loads cassette files for a named connector.

    Usage::

        def test_something(respx_cassette):
            router = respx_cassette("openapi")
            with router:
                # all httpx calls are intercepted
                ...

    When ``REFRESH_CASSETTES=1`` is set the returned router is in pass-through
    mode — every call is forwarded to the real network.  This is intentionally
    not activated in CI.
    """
    refresh = os.environ.get("REFRESH_CASSETTES", "").strip() == "1"

    def _factory(connector_name: str) -> respx.MockRouter:
        router = respx.MockRouter(assert_all_called=False)

        if refresh:
            # Pass-through: forward to real network.  Callers can capture
            # responses and overwrite the cassette files.
            router.pass_through(lambda request: True)  # type: ignore[arg-type]
            return router

        cassette_dir = _FIXTURES_ROOT / connector_name
        if not cassette_dir.is_dir():
            raise FileNotFoundError(f"No cassette directory for connector '{connector_name}': {cassette_dir}")

        cassette_files = sorted(cassette_dir.glob("cassette_*.json"))
        if not cassette_files:
            raise FileNotFoundError(f"No cassette_*.json files found in {cassette_dir}")

        for cassette_path in cassette_files:
            entry = _load_cassette(cassette_path)
            req = entry["request"]
            method = req["method"].upper()
            url = req["url"]
            response = _response_from_cassette(entry)
            router.route(method=method, url=url).mock(return_value=response)

        return router

    return _factory


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _set_http_methods_mode_for_integration() -> Iterator[None]:
    """Default REGISTRY_HTTP_METHODS_MODE to "both" for the integration suite.

    Several integration tests POST to alias paths like
    ``/v1/admin/external-systems/{slug}:delete`` and expect 204. Those
    aliases are only registered when the mode is ``both`` or ``post_only``.
    The default mode is ``rest``, which leaves them unregistered.

    Scope this to the integration session so unit tests (which explicitly
    verify the rest-default behavior) are unaffected. Routers register
    routes at module-import time, so we set the env var *before* the first
    integration test imports a router. Tests that need a specific mode
    (``test_http_methods_mode.py``) override the env var and reload the
    affected modules.
    """
    prev = os.environ.get("REGISTRY_HTTP_METHODS_MODE")
    os.environ["REGISTRY_HTTP_METHODS_MODE"] = "both"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("REGISTRY_HTTP_METHODS_MODE", None)
        else:
            os.environ["REGISTRY_HTTP_METHODS_MODE"] = prev


@pytest.fixture(scope="session")
def app_settings(pg_container: str) -> Settings:
    return Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        # MemoryJobStore avoids the APScheduler-pickles-engine-closure error
        # ("Can't get local object 'create_engine.<locals>.connect'") that
        # SQLAlchemyJobStore hits when a TestClient's lifespan starts the
        # scheduler. Production deploys use SQLAlchemyJobStore.
        scheduler_use_memory_jobstore=True,
        # Settings is a plain dataclass and ignores the environment, so leaving
        # this unset picks up the production default: an in-process model read
        # from a path that only exists inside the container image. create_app()
        # would raise. Tests that want real vectors set it themselves.
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
