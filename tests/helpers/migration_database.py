"""One independent database per migration-reversibility test, cloned not rebuilt.

A test that proves a migration can be reversed has to own the database it
reverses. It cannot downgrade the shared session database: that would drop
tables out from under every other integration module in the run, so a test
proving the downgrade works would break unrelated tests to do it.

Every such test therefore built its own scratch database and ran
``alembic upgrade head`` into it from empty before it could downgrade
anything. The schema that build produces is byte-identical every time, so the
suite paid for the same full head build once per reversibility test. This
module builds it once as a connection-disabled template and hands each test a
clone, which Postgres makes by copying files rather than by replaying
migrations.

Three properties are enforced here rather than left to each caller:

- **A worker database is refused.** The whole point is to downgrade something
  disposable. Pointing this at a database another test is using would be
  indistinguishable, from the caller's side, from correct use — so the refusal
  lives at the boundary and is proven by unit test.
- **Every Alembic subprocess runs with ``TZ=UTC``.** A migration that
  partitions by the current date reads the subprocess timezone, so an
  inherited local zone builds partitions for a different calendar day than the
  template recorded. That mismatch only appears near midnight, on some
  machines.
- **A UTC date rollover invalidates the work rather than being absorbed.** If
  the date turns over mid-node, the clone and the template no longer agree
  about what "today" means, and the run is not measurable.

The executor and the subprocess runner are injected so that the refusal and
the cleanup can be proven without a server: those are the two behaviours whose
failure is silent, and a unit test is the only place they can be exercised
against an adversarial input.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sql_text

from tests.helpers.pg_provider import admin_executor
from tests.helpers.pg_template import (
    SchemaEnvironment,
    ServerVersions,
    alembic_heads,
    assert_no_rollover,
    compute_fingerprint,
    migration_environment,
    revision_chain,
    run_migrations,
    template_name,
    utc_date,
)

#: Databases handed to workers and scenarios carry these ``kind`` segments in
#: the names the run broker mints. A reversibility test must never be pointed at
#: one, because it is about to downgrade whatever it is given.
_PROTECTED_KINDS = ("worker", "scenario", "run")

#: The scratch databases this module creates. Distinct from every broker kind
#: above so a leak is attributable to migration reversibility rather than to
#: "the run".
_KIND = "migr"

#: Alembic is invoked as a module through the running interpreter rather than as
#: a console script: a linked worktree has no virtualenv of its own, while
#: ``alembic.ini`` and the migration sources must come from the tree under test.
_ALEMBIC = (sys.executable, "-m", "alembic")


class MigrationDatabaseError(RuntimeError):
    """The migration-database lifecycle refused an operation."""


class WorkerDatabaseRefused(MigrationDatabaseError):
    """A database another consumer owns was offered for downgrade."""


class AlembicFailed(MigrationDatabaseError):
    """An Alembic subprocess exited non-zero."""


def sync_url(url: str) -> str:
    """The psycopg2 form of a URL, whatever async driver it names."""
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def plain_url(url: str) -> str:
    """The driverless form, which is what ``asyncpg.connect`` wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")


def async_url(url: str) -> str:
    """The ``+asyncpg`` form Alembic needs.

    The project ships no synchronous driver, so a bare ``postgresql://`` URL
    resolves to psycopg2 and the async engine Alembic's environment builds
    rejects it outright. Every URL handed to a migration subprocess goes
    through here, because the failure is a driver error at engine construction
    rather than anything that names a URL.
    """
    plain = plain_url(url)
    return plain.replace("postgresql://", "postgresql+asyncpg://", 1)


def database_of(url: str) -> str:
    """The database name a URL points at, ignoring any query string."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def url_for(base_url: str, database: str) -> str:
    """*base_url* redirected at *database*, preserving scheme and credentials."""
    head, _, tail = base_url.rpartition("/")
    query = tail.split("?", 1)
    suffix = f"?{query[1]}" if len(query) == 2 else ""
    return f"{head}/{database}{suffix}"


def reject_protected_database(name: str) -> None:
    """Raise unless *name* is safe to downgrade and drop.

    Checked by kind segment rather than by a list of known names: the broker
    mints names per run, so an allowlist would go stale the first time a new
    consumer appeared, and the failure mode of a stale allowlist here is
    dropping a database a live test is reading.
    """
    if not name:
        raise WorkerDatabaseRefused("no database name was supplied")
    for kind in _PROTECTED_KINDS:
        if name.startswith(f"cp_{kind}_"):
            raise WorkerDatabaseRefused(
                f"{name} is a {kind} database owned by another consumer; "
                "reversibility tests own a database of their own or none at all"
            )


@dataclass(frozen=True)
class MigrationDatabase:
    """A database at head that a single test may downgrade freely."""

    name: str
    url: str
    started_date: str
    _run: Callable[..., subprocess.CompletedProcess[str]]

    def alembic(self, *args: str) -> subprocess.CompletedProcess[str]:
        """One Alembic invocation, pinned to UTC, with the date rechecked.

        The recheck brackets the call rather than only preceding it: a
        rollover during a multi-second migration is exactly the case that
        would otherwise be absorbed silently.
        """
        assert_no_rollover(self.started_date, stage=f"alembic {' '.join(args)}")
        completed = self._run(args, self.url)
        assert_no_rollover(self.started_date, stage=f"alembic {' '.join(args)} (after)")
        return completed

    def upgrade_head(self) -> subprocess.CompletedProcess[str]:
        return self.alembic("upgrade", "head")

    def downgrade(self, revision: str) -> subprocess.CompletedProcess[str]:
        return self.alembic("downgrade", revision)

    @property
    def sync_url(self) -> str:
        return sync_url(self.url)


def _default_runner(cwd: Path | None = None) -> Callable[..., subprocess.CompletedProcess[str]]:
    root = cwd if cwd is not None else Path(os.getcwd())

    def run(args: tuple[str, ...], database_url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*_ALEMBIC, *args],
            cwd=str(root),
            env=migration_environment(async_url(database_url)),
            capture_output=True,
            text=True,
            check=False,
        )

    return run


@dataclass
class MigrationDatabases:
    """Creates head clones and guarantees they are dropped.

    Holds its own ledger of what it created. Cleanup walks that ledger rather
    than pattern-matching the server, so it can never drop a database it did
    not make, and collects failures instead of stopping at the first one — a
    cleanup that stops early leaves the rest behind for the next run to
    inherit.
    """

    execute: Callable[[str], object]
    base_url: str
    template: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    created: list[str] = field(default_factory=list)

    def name_for(self, label: str) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in label).strip("_").lower()
        return f"cp_{_KIND}_{self.run_id}_{safe}"[:63]

    def clone(self, label: str) -> MigrationDatabase:
        """A fresh database at head, copied from the template.

        ``CREATE DATABASE ... TEMPLATE`` cannot run inside a transaction
        block, so the executor must be autocommitting; that is the executor's
        contract, not something this call can assert from here.
        """
        reject_protected_database(self.template)
        name = self.name_for(label)
        reject_protected_database(name)
        started = utc_date()
        self.execute(f'CREATE DATABASE "{name}" TEMPLATE "{self.template}"')
        self.created.append(name)
        assert_no_rollover(started, stage=f"clone {name}")
        return MigrationDatabase(
            name=name,
            url=url_for(self.base_url, name),
            started_date=started,
            _run=self.runner if self.runner is not None else _default_runner(),
        )

    def drop(self, name: str) -> None:
        """Terminate then drop, idempotently.

        The terminate comes first because a ``DROP DATABASE`` with a live
        backend attached fails, and the backend most likely to be attached is
        one the node itself left behind.
        """
        reject_protected_database(name)
        self.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )
        self.execute(f'DROP DATABASE IF EXISTS "{name}"')
        if name in self.created:
            self.created.remove(name)

    def cleanup(self) -> list[str]:
        failures: list[str] = []
        for name in list(self.created):
            try:
                self.drop(name)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                if name in self.created:
                    self.created.remove(name)
        return failures

    @contextmanager
    def head_clone(self, label: str) -> Iterator[MigrationDatabase]:
        """One clone, dropped even if the test body raises."""
        database = self.clone(label)
        try:
            yield database
        finally:
            self.drop(database.name)


def assert_alembic_ok(completed: subprocess.CompletedProcess[str], what: str) -> None:
    """Fail with the subprocess's own stderr, which is where the reason is."""
    if completed.returncode != 0:
        raise AlembicFailed(f"{what} failed: {completed.stderr[-2000:]}")


@lru_cache(maxsize=1)
def _expected_heads() -> tuple[str, ...]:
    """Alembic's head revisions, resolved once per process.

    Each resolution is a subprocess, and this is asked once per reversibility
    node; paying for it nine times would give back part of the build this
    module exists to stop paying for.
    """
    return tuple(sorted(alembic_heads()))


def assert_at_head(database: MigrationDatabase) -> None:
    """The clone really arrived at head, before anything downgrades it.

    This is the postcondition the per-node ``alembic upgrade head`` used to
    establish. Cloning a template asserts the same thing about the same
    database and reads the stamped revision to prove it, instead of rebuilding
    the schema to find out. A clone that came up empty, or from a stale
    template, would otherwise reach the downgrade looking healthy.
    """
    engine = create_engine(database.sync_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text("SELECT version_num FROM alembic_version"))
            stamped = tuple(sorted(str(row[0]) for row in rows))
    finally:
        engine.dispose()
    expected = _expected_heads()
    if stamped != expected:
        raise MigrationDatabaseError(f"{database.name} is stamped {stamped}, not head {expected}")


#: The two fixtures below are imported by each reversibility module rather than
#: living in a conftest, so that the modules which use them say so in their own
#: import list. Session scope on the template is the whole point: it is the one
#: full head build the run pays for.


#: The published template, keyed by the server it lives on.
#:
#: This cache is the whole reason the build happens once. A session-scoped
#: fixture *imported* into several modules is a separate fixture definition in
#: each of them, so pytest builds it once per importing module rather than once
#: per session — four modules, four full head builds, which is exactly the cost
#: this module exists to remove. Measured: without this the template was built
#: four times for nine nodes. The cache lives below the fixture so every
#: importer shares one build.
_PUBLISHED: dict[str, str] = {}


def publish_template(pg_container: str) -> str:
    """Migrate one database to head and make it a clonable template.

    Publication order matters and is the same order a clone depends on:
    migrate, confirm the date has not rolled, disconnect everything, then
    refuse new connections. A template with a live backend attached cannot be
    cloned at all, so disabling connections is what makes it usable rather
    than merely tidy.
    """
    cached = _PUBLISHED.get(pg_container)
    if cached is not None:
        return cached

    execute = admin_executor(pg_container)
    started = utc_date()
    fingerprint = compute_fingerprint(
        heads=alembic_heads(),
        revision_chain=revision_chain(),
        environment=SchemaEnvironment.from_environ({}, date=started),
        versions=ServerVersions(postgres="16", pgvector="unknown"),
    )
    name = template_name(fingerprint)

    execute(f'DROP DATABASE IF EXISTS "{name}"')
    execute(f'CREATE DATABASE "{name}"')
    run_migrations(async_url(url_for(pg_container, name)))
    assert_no_rollover(started, stage="template publication")
    execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
    )
    execute(f"UPDATE pg_database SET datallowconn = false WHERE datname = '{name}'")
    _PUBLISHED[pg_container] = name
    return name


def drop_template(pg_container: str) -> None:
    """Remove the template, idempotently.

    Connections are re-allowed first: a database that refuses connections also
    refuses the terminate-then-drop sequence on some servers, and a template
    left behind is inherited by the next run.
    """
    name = _PUBLISHED.pop(pg_container, None)
    if name is None:
        return
    execute = admin_executor(pg_container)
    execute(f"UPDATE pg_database SET datallowconn = true WHERE datname = '{name}'")
    execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
    )
    execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
def migration_template(pg_container: str) -> Iterator[tuple[str, str]]:
    """The shared template, built on first request and dropped at session end."""
    name = publish_template(pg_container)
    try:
        yield pg_container, name
    finally:
        drop_template(pg_container)


@pytest.fixture
def migration_databases(migration_template: tuple[str, str]) -> Iterator[MigrationDatabases]:
    """Head clones for one test, dropped even if it fails midway.

    A leaked clone is raised rather than logged: the next run inherits it, and
    an inherited database is how a suite starts passing for reasons nobody
    chose.
    """
    base_url, template = migration_template
    databases = MigrationDatabases(
        execute=admin_executor(base_url),
        base_url=base_url,
        template=template,
    )
    try:
        yield databases
    finally:
        failures = databases.cleanup()
        if failures:
            raise MigrationDatabaseError(f"migration databases leaked: {'; '.join(failures)}")


__all__ = [
    "AlembicFailed",
    "MigrationDatabase",
    "MigrationDatabaseError",
    "MigrationDatabases",
    "WorkerDatabaseRefused",
    "assert_alembic_ok",
    "assert_at_head",
    "async_url",
    "database_of",
    "drop_template",
    "migration_databases",
    "migration_template",
    "plain_url",
    "publish_template",
    "reject_protected_database",
    "sync_url",
    "url_for",
]
