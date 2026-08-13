"""Provide a migrated Postgres database to the test suite.

The integration and conformance suites need a real Postgres with
pgvector. Historically that meant testcontainers, unconditionally, which
made a container runtime a hard requirement for `make test`,
`make test-integration`, `make test-conformance`, and therefore for
`make all` — the gate a PR has to pass. A developer without a container
runtime could not run the gate at all.

This module makes the source of that database a choice, selected by
``CONTEXTPLANE_TEST_PG``:

``auto`` (default)
    ``DATABASE_URL`` if set, else testcontainers if a container runtime
    answers, else a locally managed cluster. Existing setups and CI keep
    the behaviour they have today; machines without a runtime get a
    working suite instead of an error.
``external``
    Use ``DATABASE_URL``. The schema must already be at head.
``testcontainers``
    Always testcontainers, even if something else is available.
``devstack``
    Always a locally managed cluster, even if a container runtime is
    available. Also the fastest option once the cluster exists.

Whichever source is chosen, the suite gets a **freshly created,
freshly migrated database**, dropped when the session ends. That is not
incidental. A container was thrown away after every session, so state
could not leak between runs; a long-lived local cluster has no such
property, and the per-test ``db_session`` fixture commits rather than
rolling back. Creating a database per session restores the isolation the
container was quietly providing.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - test-harness invocation of dev-only Postgres tooling (fixed argv, resolved bindir), no caller input
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import SupportsInt, cast

# This module sits directly under `scripts/`, so the repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from devstack.cluster import Cluster  # noqa: E402
from devstack.pg_provider import (  # noqa: E402
    PostgresUnavailableError,
    resolve,
    resolve_local,
)
from pg_run_broker import ProviderCapabilities, RunBroker  # noqa: E402

# The test cluster is deliberately separate from the one `make dev-up`
# manages: a test run must never be able to drop a developer's dev data,
# and a dev stack being up or down must not change whether tests pass.
_TEST_PGDATA = _REPO_ROOT / ".devstack" / "pgdata-test"

# Fixed port for the test cluster, clear of both the dev stack (5544) and
# the default Postgres port. Override when running two checkouts at once.
_TEST_PORT = int(os.environ.get("CONTEXTPLANE_TEST_PG_PORT", "5545"))  # config: intentional

# Integration tests create short-lived AsyncEngines without disposing
# them, so the connection count drifts upward over a long run. Applied to
# every locally managed test cluster so the suite does not cascade into
# "sorry, too many clients already" partway through.
# Measured, not asserted: two consecutive full integration runs peaked at 22
# client backends (sampled live against the test container, 2026-08-04). The
# old 500 was a workaround for engines the suite leaked before disposal was
# fixed; 50 is better than twice the observed peak, and a suite that needs
# more than that has a leak worth failing on rather than absorbing.
_SERVER_FLAGS = ("-c", "max_connections=50", "-c", "shared_buffers=128MB")

_MODE_ENV = "CONTEXTPLANE_TEST_PG"
_VALID_MODES = ("auto", "external", "testcontainers", "devstack")

_docker_available_cache: bool | None = None


def _to_async_url(url: str) -> str:
    """Translate a psycopg2-style URL into an asyncpg one."""
    return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


def docker_available() -> bool:
    """True if a container runtime answers. Probed once per process."""
    global _docker_available_cache
    if _docker_available_cache is not None:
        return _docker_available_cache

    if shutil.which("docker") is None:
        _docker_available_cache = False
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no caller input; a probe for whether a container runtime answers
            ["docker", "info"],  # noqa: S607 - "docker" is resolved from PATH, and the shutil.which() guard above is what decides whether this runs at all
            capture_output=True,
            timeout=15,
            check=False,
        )
        _docker_available_cache = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        # Includes TimeoutExpired: a runtime installed but not running
        # can hang, and waiting on it is worse than falling through.
        _docker_available_cache = False
    return _docker_available_cache


def selected_mode() -> str:
    """Resolve CONTEXTPLANE_TEST_PG to a concrete mode."""
    mode = os.environ.get(_MODE_ENV, "auto").strip().lower()  # config: intentional
    if mode not in _VALID_MODES:
        raise RuntimeError(f"{_MODE_ENV}={mode!r} is not one of {', '.join(_VALID_MODES)}")
    if mode != "auto":
        return mode
    if os.environ.get("DATABASE_URL"):  # config: intentional
        return "external"
    return "testcontainers" if docker_available() else "devstack"


def run_migrations(database_url: str) -> None:
    """Bring *database_url* to head with Alembic."""
    completed = subprocess.run(  # noqa: S603 - fixed argv (this interpreter + literal alembic args); the database URL travels in the environment, never in argv
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},  # config: intentional
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{completed.stdout}\n{completed.stderr}")


@contextmanager
def _testcontainers_database(server_flags: Sequence[str]) -> Iterator[str]:
    from testcontainers.postgres import (  # type: ignore[import-untyped]  # noqa: PLC0415 - deferred so the module imports on a machine with no testcontainers installed; only this branch needs it
        PostgresContainer,
    )

    container = PostgresContainer(image="pgvector/pgvector:pg16", username="postgres", password="password")  # noqa: S106 - the credential for a throwaway container this function creates and destroys; it is never read from configuration or reachable off this host
    if server_flags:
        container = container.with_command(f"postgres {' '.join(server_flags)}")
    container.start()
    try:
        url = _to_async_url(container.get_connection_url())
        run_migrations(url)
        yield url
    finally:
        container.stop()


def _test_cluster() -> Cluster:
    """The one locally managed cluster the suite uses.

    Single definition of *which server is the test server*. Both the
    per-session database path and the admin path below go through here, so
    the pgdata directory and the port cannot drift apart between them --
    two functions computing the same cluster from the same constants is
    how they start disagreeing about which one is "the" test cluster.
    """
    try:
        source = resolve_local()
    except PostgresUnavailableError as exc:
        raise RuntimeError(
            "No local Postgres available for the test suite.\n\n"
            f"{exc}\n"
            f"Alternatively set {_MODE_ENV}=testcontainers to use a container "
            "runtime, or DATABASE_URL to point at a database you manage."
        ) from exc
    return Cluster(source, _TEST_PGDATA, port=_TEST_PORT)


@contextmanager
def _devstack_database(server_flags: Sequence[str]) -> Iterator[str]:
    cluster = _test_cluster()
    started_here = not cluster.is_running()
    cluster.start(server_flags=server_flags)

    # Per-session database, named for the process so two checkouts or two
    # concurrent runs never share one.
    database = f"registry_test_{os.getpid()}"
    cluster.drop_database(database)
    cluster.create_database(database)
    try:
        url = cluster.url(database)
        run_migrations(url)
        yield url
    finally:
        # The stop has to be reachable even when the drop fails, and the drop
        # fails in exactly the case that matters: a run that died on connection
        # exhaustion cannot get a connection to issue `DROP DATABASE` either.
        # Without this the error escaped the finally, the postmaster outlived
        # the session, and the *next* session found a cluster already running —
        # so it skipped start(), and since server settings only apply at
        # postmaster start, it silently inherited the dead run's configuration.
        # That is a failure that moves: the symptom lands in the following run,
        # in whatever asked for a connection first.
        try:
            cluster.drop_database(database)
        finally:
            # Leave a cluster that was already up alone — it may belong to
            # another test session running in parallel.
            if started_here:
                cluster.stop()


@contextmanager
def _external_database() -> Iterator[str]:
    url = os.environ.get("DATABASE_URL")  # config: intentional
    if not url:
        raise RuntimeError(
            f"{_MODE_ENV}=external requires DATABASE_URL to be set to a " "Postgres already migrated to head."
        )
    yield _to_async_url(url)


@contextmanager
def test_database(*, server_flags: Sequence[str] = _SERVER_FLAGS) -> Iterator[str]:
    """Yield a migrated database URL for the duration of a test session."""
    mode = selected_mode()
    if mode == "external":
        with _external_database() as url:
            yield url
    elif mode == "testcontainers":
        with _testcontainers_database(server_flags) as url:
            yield url
    else:
        with _devstack_database(server_flags) as url:
            yield url


@contextmanager
def admin_database(*, server_flags: Sequence[str] = _SERVER_FLAGS) -> Iterator[str]:
    """Yield an admin URL against the selected provider's maintenance database.

    `admin_executor` and `build_broker` both *take* an admin URL and this
    module is the only thing that can compute one, so it has to hand one out:
    every statement the broker issues (`CREATE DATABASE`, `DROP DATABASE`)
    must run from outside the database it targets, and a session database is
    the wrong place to issue them from.

    Bare `postgresql://` rather than the `+asyncpg` spelling, because the
    broker's executor connects with asyncpg directly rather than through
    SQLAlchemy.

    Scoped as a context manager because acquiring the server is not free in
    two of the three modes: a container has to be started and stopped, and a
    locally managed cluster that this call started has to be left as it was
    found. A cluster that was already up belongs to somebody else and is not
    stopped here.
    """
    mode = selected_mode()
    if mode == "external":
        url = os.environ.get("DATABASE_URL")  # config: intentional
        if not url:
            raise RuntimeError(f"{_MODE_ENV}=external requires DATABASE_URL to be set.")
        yield _to_admin_url(url)
        return
    if mode == "testcontainers":
        with _testcontainers_database(server_flags) as url:
            yield _to_admin_url(url)
        return
    cluster = _test_cluster()
    started_here = not cluster.is_running()
    cluster.start(server_flags=server_flags)
    try:
        yield _to_admin_url(cluster.url("postgres"))
    finally:
        if started_here:
            cluster.stop()


def _to_admin_url(url: str) -> str:
    """The `postgres` maintenance database on the server *url* points at."""
    bare = url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    base, _, _ = bare.rpartition("/")
    return f"{base}/postgres"


def describe() -> str:
    """Short description of where the test database comes from."""
    mode = selected_mode()
    if mode == "external":
        return "external database from DATABASE_URL"
    if mode == "testcontainers":
        return "testcontainers (pgvector/pgvector:pg16)"
    try:
        return f"local cluster — {resolve(allow_external=False).label}"
    except PostgresUnavailableError:
        return "local cluster (unavailable)"


# -- capability probing ---------------------------------------------------
#
# A parent that runs workers in parallel needs to know what the provider can
# actually do, not what its name suggests. `devstack` in particular is a mode
# a caller can *ask for* on a host with no PostgreSQL binaries at all, and the
# honest answer there is "unavailable, for this reason" rather than a failure
# partway through provisioning.


def devstack_available() -> tuple[bool, str]:
    """Whether a locally managed cluster can be started here, and why not.

    Returns a reason rather than raising: the caller is usually deciding
    whether to skip, and a skip needs something to print.
    """
    try:
        source = resolve_local()
    except PostgresUnavailableError as exc:
        return False, str(exc).strip().splitlines()[0]
    missing = [name for name in ("initdb", "pg_ctl", "postgres", "psql") if not (source.bindir / name).exists()]
    if missing:
        return False, f"{source.bindir} lacks {', '.join(missing)}"
    return True, f"local cluster — {source.label}"


def probe_capabilities(mode: str | None = None) -> ProviderCapabilities:
    """Probe create/clone/terminate/drop for the selected provider.

    Structural rather than executed: a probe that actually created and
    dropped a database on every startup would add the very cost this phase
    exists to remove. What varies between providers is whether a server can
    be obtained at all — every server that *can* be obtained is a full
    PostgreSQL 16 and therefore supports all four operations.
    """
    resolved = mode or selected_mode()
    if resolved == "devstack":
        available, detail = devstack_available()
        return ProviderCapabilities(
            provider="devstack",
            create=available,
            clone=available,
            terminate=available,
            drop=available,
            detail=detail,
        )
    if resolved == "testcontainers":
        available = docker_available()
        return ProviderCapabilities(
            provider="testcontainers",
            create=available,
            clone=available,
            terminate=available,
            drop=available,
            detail="container runtime answers" if available else "no container runtime",
        )
    url = os.environ.get("DATABASE_URL")  # config: intentional
    return ProviderCapabilities(
        provider="external",
        create=bool(url),
        clone=bool(url),
        terminate=bool(url),
        drop=bool(url),
        detail="DATABASE_URL set" if url else "DATABASE_URL unset",
    )


def admin_executor(admin_url: str) -> Callable[[str], list[tuple[object, ...]]]:
    """A synchronous SQL executor over an asyncpg connection.

    Admin statements (`CREATE DATABASE`, `DROP DATABASE`) cannot run inside
    a transaction, and the project ships no synchronous driver, so each
    statement gets its own short-lived asyncpg connection through
    `asyncio.run`. Slower per statement than a pooled connection and
    deliberately so: provisioning happens a handful of times per run, while
    a pool held open across `CREATE DATABASE ... TEMPLATE` is exactly what
    makes a clone fail with "source database is being accessed by other
    users".
    """
    import asyncio  # noqa: PLC0415 - deferred with asyncpg below, so this module stays importable in a synchronous context that never provisions

    import asyncpg  # noqa: PLC0415 - deferred so the module imports without the async driver present; only provisioning needs it

    dsn = admin_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

    def execute(sql: str) -> list[tuple[object, ...]]:
        async def run() -> list[tuple[object, ...]]:
            connection = await asyncpg.connect(dsn)
            try:
                records = await connection.fetch(sql)
            finally:
                await connection.close()
            return [tuple(record.values()) for record in records]

        return asyncio.run(run())

    return execute


def build_broker(admin_url: str, *, provider: str, run_id: str | None = None) -> RunBroker:
    """A broker that owns databases on the server *admin_url* points at.

    The broker is handed an executor and two inventory callables; it never
    learns which provider produced the server beyond the label, which is
    what keeps one broker implementation covering all three.
    """
    execute = admin_executor(admin_url)

    def list_databases() -> list[str]:
        return [str(row[0]) for row in execute("SELECT datname FROM pg_database ORDER BY datname")]

    def count_sessions() -> int:
        rows = execute("SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()")
        return int(cast("SupportsInt", rows[0][0])) if rows else 0

    return RunBroker(
        provider=provider,
        execute=execute,
        list_databases=list_databases,
        count_sessions=count_sessions,
        run_id=run_id,
    )
