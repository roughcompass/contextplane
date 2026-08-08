"""Shared service / settings / persona factories for tests.

Three families of duplicated local helper lived across the unit and
integration suites; each is a thin factory that several test files had
retyped identically:

- `Settings(...)` construction with a dummy database URL (unit tests that
  never touch a real database but must satisfy `Settings`' required
  fields).
- `RetrievalService` wiring from a raw Postgres URL (the perf suite).
- Materializing a `TenantPersona` through the entitlement auth harness by
  hitting `/v1/whoami` once (the REST integration suite).

Local copies that pin a different default (a different `backfill_batch_size`,
a harness that also seeds vocabulary) still call these where the core
construction matches, keeping only the genuinely different step local.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from contextplane.config import Settings
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.retrieval import RetrievalService
from contextplane.storage.pg import get_session_factory
from tests.helpers.auth_harness import EntitlementAuthHarness, TenantPersona, bearer_headers, patch_validator_for_actor
from tests.helpers.clock import FakeClock

# ---------------------------------------------------------------------------
# Settings factories
# ---------------------------------------------------------------------------

# Placeholder DSN for unit tests that construct `Settings` only to satisfy
# its required database fields -- nothing here ever opens a connection.
_DUMMY_DSN = "postgresql+asyncpg://x:x@localhost/test"


def dummy_db_settings() -> Settings:
    """`Settings` with a placeholder DSN and otherwise-default fields.

    For unit tests whose service under test never issues a query -- the
    session factory is mocked -- but still needs a `Settings` instance to
    construct the service with.
    """
    return Settings(
        database_url=_DUMMY_DSN,
        pgbouncer_url=_DUMMY_DSN,
        scheduler_jobstore_url=_DUMMY_DSN,
    )


def overridable_settings(**overrides: object) -> Settings:
    """`Settings` for app-level unit tests (metrics auth, middleware order).

    Starts from a fixed base -- memory jobstore, stub embeddings, JSON
    logging -- and applies `overrides` on top, so a test only has to name
    the one field it cares about (e.g. `metrics_bearer_token=...`).
    """
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "pgbouncer_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "scheduler_jobstore_url": "postgresql+asyncpg://user:pass@localhost:9999/db",
        "scheduler_use_memory_jobstore": True,
        "embedding_provider": "stub",
        "otlp_endpoint": None,
        "log_format": "json",
        "log_level": logging.INFO,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def mutable_settings(**overrides: object) -> Settings:
    """`Settings` for the backfill/reindex CLI tests, with per-field overrides.

    `Settings` is a frozen dataclass; these two callers need to flip one
    field per test case (batch size, dry-run, etc.) without reconstructing
    the whole object, so this applies overrides via `object.__setattr__`
    rather than the constructor. `dummy_db_settings`/`overridable_settings`
    don't need this because their callers only ever set fields once, at
    construction time.
    """
    settings = Settings(
        database_url=_DUMMY_DSN,
        pgbouncer_url=_DUMMY_DSN,
        scheduler_jobstore_url=_DUMMY_DSN,
        backfill_batch_size=2,
    )
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    return settings


# ---------------------------------------------------------------------------
# Service factories
# ---------------------------------------------------------------------------

# Fixed clock anchor for the perf suite's RetrievalService instances. An
# arbitrary past instant; perf assertions are about latency, not recency.
_PERF_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def make_retrieval_service(pg_url: str) -> RetrievalService:
    """A `RetrievalService` wired directly to `pg_url`, for perf tests.

    Perf tests build their own engine (rather than using the `db_session`
    fixture) because they hold it open across a module-scoped seed fixture
    and the per-test measurement.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    session_factory = get_session_factory(engine)
    return RetrievalService(
        session_factory=session_factory,
        clock=FakeClock(_PERF_NOW),
        embedder=StubEmbedder(),
        settings=Settings(
            database_url=pg_url,
            pgbouncer_url=pg_url,
            scheduler_jobstore_url=pg_url,
        ),
    )


# ---------------------------------------------------------------------------
# Persona factories
# ---------------------------------------------------------------------------


async def make_persona_new_client(
    h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]
) -> TenantPersona:
    """Add a persona and materialize its tenant by hitting `/v1/whoami`.

    Opens a fresh `AsyncClient` against the harness app for that one call.
    `pg_url` is accepted (unused here) because several call sites also seed
    vocabulary or other rows after materializing and want one signature for
    both cases.
    """
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = ASGITransport(app=h.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return persona


async def make_persona_shared_client(
    harness: EntitlementAuthHarness, client: AsyncClient, slug: str, roles: list[str]
) -> tuple[TenantPersona, uuid.UUID]:
    """Add a persona and materialize it through a client the caller already has.

    Returns `(persona, tenant_id)` -- unlike `make_persona_new_client`, these
    call sites need the freshly materialized tenant_id back (for cross-tenant
    assertions) and already hold a live `AsyncClient` they want reused rather
    than opening a second one.
    """
    persona = harness.add_persona(slug, roles=roles)
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
        assert resp.status_code == 200, resp.text
    return persona, uuid.UUID(resp.json()["tenant_id"])


__all__ = [
    "dummy_db_settings",
    "overridable_settings",
    "mutable_settings",
    "make_retrieval_service",
    "make_persona_new_client",
    "make_persona_shared_client",
]
