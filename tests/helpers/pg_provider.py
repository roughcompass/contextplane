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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.devstack.cluster import Cluster  # noqa: E402
from scripts.devstack.pg_provider import (  # noqa: E402
    PostgresUnavailableError,
    resolve,
    resolve_local,
)

# The test cluster is deliberately separate from the one `make dev-up`
# manages: a test run must never be able to drop a developer's dev data,
# and a dev stack being up or down must not change whether tests pass.
_TEST_PGDATA = _REPO_ROOT / ".devstack" / "pgdata-test"

# Fixed port for the test cluster, clear of both the dev stack (5544) and
# the default Postgres port. Override when running two checkouts at once.
_TEST_PORT = int(os.environ.get("CONTEXTPLANE_TEST_PG_PORT", "5545"))

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
        completed = subprocess.run(
            ["docker", "info"],
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
    mode = os.environ.get(_MODE_ENV, "auto").strip().lower()
    if mode not in _VALID_MODES:
        raise RuntimeError(f"{_MODE_ENV}={mode!r} is not one of {', '.join(_VALID_MODES)}")
    if mode != "auto":
        return mode
    if os.environ.get("DATABASE_URL"):
        return "external"
    return "testcontainers" if docker_available() else "devstack"


def run_migrations(database_url: str) -> None:
    """Bring *database_url* to head with Alembic."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{completed.stdout}\n{completed.stderr}")


@contextmanager
def _testcontainers_database(server_flags: Sequence[str]) -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(image="pgvector/pgvector:pg16", username="postgres", password="password")
    if server_flags:
        container = container.with_command(f"postgres {' '.join(server_flags)}")
    container.start()
    try:
        url = _to_async_url(container.get_connection_url())
        run_migrations(url)
        yield url
    finally:
        container.stop()


@contextmanager
def _devstack_database(server_flags: Sequence[str]) -> Iterator[str]:
    try:
        source = resolve_local()
    except PostgresUnavailableError as exc:
        raise RuntimeError(
            "No local Postgres available for the test suite.\n\n"
            f"{exc}\n"
            f"Alternatively set {_MODE_ENV}=testcontainers to use a container "
            "runtime, or DATABASE_URL to point at a database you manage."
        ) from exc

    cluster = Cluster(source, _TEST_PGDATA, port=_TEST_PORT)
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
    url = os.environ.get("DATABASE_URL")
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
