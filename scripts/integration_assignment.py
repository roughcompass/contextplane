"""The parent's half of the broker contract: which database each worker gets.

The worker's half lives in the reporter and in the integration conftest --
it consumes an assigned URL and refuses to provision anything itself. This
module is the other side: it takes the exclusive lease, closes admission,
migrates one template, clones a database per worker, and hands each child
exactly the two variables it is allowed to see.

Kept apart from the runner because the boundary is real rather than
arithmetic. The runner qualifies an invocation, collects, dispatches and
reconciles; none of that knows what a database is. This module is the only
place that talks to a server, so the runner stays testable without one.

**One server, one database per worker.** Per-worker *clusters* would make
the measurement meaningless: N postmasters on one host compete for the same
page cache with none of them sized for it, so the numbers would describe
the machinery the measurement introduced rather than test parallelism. The
shared postmaster's buffers, WAL and checkpointer are part of what is being
measured at 4 and 8 workers, and correctly so, because a CI runner meets the
same contention. A scale curve that flattens must be reported with that
topology stated beside it rather than as "parallelism does not help".
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# This module sits directly under `scripts/`, so the repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pg_run_broker import BrokerManifest  # noqa: E402

if TYPE_CHECKING:
    from pg_run_broker import RunBroker

# `pg_provider` and `pg_template` reach a real server and a real Alembic run,
# so they are imported inside the functions that provision rather than at
# module scope. Everything above that line -- the two variable names, the
# assignment records, the digest -- has to be importable where no database
# exists at all: the runner annotates against them, and the sealed-runner
# fixture controls build a synthetic tree carrying no provider modules. A
# module-level import here makes that tree fail with `ModuleNotFoundError`
# from inside a child, which reads as a broken runner rather than a missing
# optional dependency.

#: The assigned database. Spelled the way the runner's child allowlist spells
#: it, which is the contract that decides what actually reaches a worker.
ASSIGNED_URL_VARIABLE = "CONTEXTPLANE_TEST_DATABASE_URL"

#: The manifest the assignment came from, so a worker can refuse a database
#: handed to it by anything other than the broker holding this run's lease.
#:
#: The broker's own `worker_environment()` emits this under a different name
#: (`CONTEXTPLANE_BROKER_MANIFEST_DIGEST`), which the runner's allowlist does
#: not carry. Passing that mapping through verbatim would not raise -- the
#: digest would be filtered out silently and the worker would then fail closed
#: reporting a missing digest, naming the broker for a fault that is not
#: there. Both variables are therefore built from the manifest's *values*
#: under the names the child environment actually admits.
MANIFEST_DIGEST_VARIABLE = "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST"


class AssignmentError(RuntimeError):
    """Raised when a run cannot be given the databases it asked for."""


@dataclass(frozen=True)
class Assignment:
    """What one worker is told, and nothing more."""

    worker_id: str
    database_url: str
    database_name: str

    def environment(self, digest: str) -> dict[str, str]:
        """The two variables this worker is allowed to see."""
        return {ASSIGNED_URL_VARIABLE: self.database_url, MANIFEST_DIGEST_VARIABLE: digest}


@dataclass(frozen=True)
class Assignments:
    """Every worker's database for one sequence, plus the manifest digest.

    The digest covers the *redacted* assignment map, so it can travel to a
    child without carrying a credential, and a child can recompute it to
    prove the assignment it received is the one the manifest recorded.
    """

    manifest_digest: str
    by_worker: dict[str, Assignment]

    def environment(self, worker_id: str) -> dict[str, str]:
        assignment = self.by_worker.get(worker_id)
        if assignment is None:
            raise AssignmentError(f"no database was assigned to worker {worker_id!r}")
        return assignment.environment(self.manifest_digest)

    def as_evidence(self) -> dict[str, object]:
        """Redacted: worker IDs and database names, never a URL."""
        return {
            "manifest_digest": self.manifest_digest,
            "workers": sorted(self.by_worker),
            "databases": sorted(assignment.database_name for assignment in self.by_worker.values()),
        }


def swap_database(admin_url: str, database: str) -> str:
    """Point an admin URL at a different database on the same server."""
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{database}"


def migration_url(admin_url: str, database: str) -> str:
    """The `+asyncpg` spelling Alembic needs.

    The project ships no synchronous driver, so a bare `postgresql://` URL
    resolves to psycopg2 and SQLAlchemy's async engine rejects it. The
    broker's executor wants the bare form, so the two spellings are
    converted at this boundary rather than one being carried everywhere.
    """
    return swap_database(admin_url, database).replace("postgresql://", "postgresql+asyncpg://")


def publish_template(admin_url: str, *, postgres_version: str = "unknown") -> str:
    """Migrate one template every worker clones, and close it to connections.

    Migrating is the expensive step, so it happens once per sequence rather
    than once per worker. Publication follows the order the broker requires:
    migrate, check the date has not rolled, terminate connections, then
    disable new ones.

    The date check is not ceremony. The fingerprint carries a UTC date, so a
    template built either side of midnight is a different template under the
    same name, and a measured run that straddles the rollover is comparing
    two schemas while reporting one.
    """
    from pg_provider import admin_executor  # noqa: PLC0415 - deferred: reaches a real server
    from pg_template import (  # noqa: PLC0415 - deferred: runs Alembic
        SchemaEnvironment,
        ServerVersions,
        compute_fingerprint,
        revision_chain,
        run_migrations,
        template_name,
        utc_date,
    )

    execute = admin_executor(admin_url)
    started = utc_date()
    fingerprint = compute_fingerprint(
        heads=["sequence-scope"],
        revision_chain=revision_chain(),
        environment=SchemaEnvironment.from_environ({}, date=started),
        versions=ServerVersions(postgres=postgres_version, pgvector="unknown"),
    )
    name = template_name(fingerprint)

    execute(f'DROP DATABASE IF EXISTS "{name}"')
    execute(f'CREATE DATABASE "{name}"')
    run_migrations(migration_url(admin_url, name))

    if utc_date() != started:
        raise AssignmentError("UTC date rolled while the template was being built; this run is not measurable")

    _terminate_connections(execute, name)
    # Identifier is minted here from a schema fingerprint, not from any caller
    # string, so there is nothing for a caller to inject through.
    execute(f"UPDATE pg_database SET datallowconn = false WHERE datname = '{name}'")  # noqa: S608
    return name


def drop_template(admin_url: str, name: str) -> None:
    """Reopen, disconnect and drop a published template. Idempotent."""
    from pg_provider import admin_executor  # noqa: PLC0415 - deferred: reaches a real server

    execute = admin_executor(admin_url)
    execute(f"UPDATE pg_database SET datallowconn = true WHERE datname = '{name}'")  # noqa: S608 - fingerprint-minted identifier, as above
    _terminate_connections(execute, name)
    execute(f'DROP DATABASE IF EXISTS "{name}"')


def _terminate_connections(execute: Callable[[str], object], name: str) -> None:
    """Disconnect every backend on *name* except this one."""
    execute(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' AND pid <> pg_backend_pid()"  # noqa: S608 - fingerprint-minted identifier, as above
    )


def assign_workers(
    broker: RunBroker,
    admin_url: str,
    worker_ids: Sequence[str],
    *,
    template: str,
    control: str | None = None,
) -> Assignments:
    """Clone one database per worker and record who got which.

    The clone goes through the broker rather than around it, so admission
    and the single-use control are enforced on the path that actually
    provisions. A worker never learns the template name, the admin URL, or
    any other worker's database.
    """
    if not worker_ids:
        raise AssignmentError("a sequence needs at least one worker to assign")
    if len(set(worker_ids)) != len(worker_ids):
        raise AssignmentError(f"worker IDs must be unique; got {list(worker_ids)!r}")

    manifest = BrokerManifest(run_id=broker.run_id)
    by_worker: dict[str, Assignment] = {}
    for worker_id in worker_ids:
        name = broker.clone_database(
            broker.database_name("worker", worker_id),
            template=template,
            control=control,
        )
        url = swap_database(admin_url, name)
        manifest.assign(worker_id, url, name)
        by_worker[worker_id] = Assignment(worker_id=worker_id, database_url=url, database_name=name)

    return Assignments(manifest_digest=manifest.digest(), by_worker=by_worker)


@contextmanager
def assigned_databases(
    admin_url: str,
    worker_ids: Sequence[str],
    *,
    provider: str,
    controller_id: str,
    sequence_id: str,
    control: str | None = None,
    postgres_version: str = "unknown",
) -> Iterator[Assignments]:
    """Hold the lease and the databases for the whole of one sequence.

    The lease is taken before the first child and released only after the
    last one has been reconciled, because a second run provisioning against
    the same server mid-sequence would change what is being measured without
    appearing anywhere in the evidence.

    Everything created here is dropped on the way out, including after a
    failure -- a run that dies holding databases makes the next run's
    inventory check fail for a reason that has nothing to do with it.
    """
    from pg_provider import build_broker  # noqa: PLC0415 - deferred: reaches a real server

    broker = build_broker(admin_url, provider=provider)
    broker.open_sequence(controller_id, sequence_id)
    template: str | None = None
    try:
        template = publish_template(admin_url, postgres_version=postgres_version)
        yield assign_workers(broker, admin_url, worker_ids, template=template, control=control)
    finally:
        broker.cleanup()
        if template is not None:
            drop_template(admin_url, template)
        broker.close_sequence(controller_id)
