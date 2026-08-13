"""Manage a local PostgreSQL cluster for the dev stack.

Owns the lifecycle of a throwaway cluster under `.devstack/pgdata`:
`initdb` on first use, `pg_ctl start/stop`, database creation, and the
`vector` + `pgcrypto` extensions the migrations require.

This is deliberately a thin wrapper over the Postgres command-line tools
rather than a library binding. The tools come from whichever bindir
`pg_provider` resolved, so the same code drives Postgres.app, a distro
package, and the binaries bundled in the `pgserver` wheel. Admin
operations go through `psql` because the project depends on asyncpg only
— there is no synchronous driver available for bootstrap work that has to
happen before the app is running.

The cluster listens on localhost only. The compose stack publishes
Postgres on all interfaces with a well-known password; there is no reason
for the native path to inherit that.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess  # noqa: S404 - local dev-stack tooling; every call site below is a fixed argv with no caller input, each already reasoned at its own noqa
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .pg_provider import (
    DEFAULT_DATABASE,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USER,
    LocalPostgres,
)

# Unix domain socket paths are capped near 104 bytes on macOS. A repo
# checked out somewhere deep would blow that budget, so the socket
# directory falls back to the system temp dir when the in-repo path is
# too long. Nothing connects over the socket — the app and psql both use
# TCP — but Postgres still needs somewhere to put it.
_MAX_SOCKET_DIR_LEN = 80

# Locales tried in order when initialising the cluster. The container
# image runs with en_US.UTF-8, and text collation affects ORDER BY
# results, so matching it keeps sort-order-sensitive tests behaving the
# same on both providers.
_PREFERRED_LOCALES = ("en_US.UTF-8", "en_US.utf8", "C.UTF-8", "C.utf8")


class ClusterError(RuntimeError):
    """A Postgres command failed. Message includes the server log tail."""


class ClusterLeaseError(RuntimeError):
    """Another live process holds the lease on this cluster."""


def free_port() -> int:
    """A port the OS says is free right now.

    Bind-to-zero rather than a fixed offset from a base port. A measured
    run cannot tolerate "port already in use" halfway through warm-up, and
    it equally cannot tolerate silently attaching to *somebody else's*
    Postgres that happens to be on the port it guessed. There is an
    inherent race between releasing this port and Postgres binding it; the
    lease below is what makes the window safe, by ensuring only one run is
    ever trying to claim a given data directory.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ClusterLease:
    """An exclusive, stale-tolerant lease over one cluster data directory.

    Two concurrent runs must not share a data directory: the second
    `pg_ctl start` would find a running postmaster, skip its own start, and
    silently inherit the first run's server settings — a failure that
    surfaces in whichever run next asks for a connection, not in the one
    that caused it.

    A lease is a lockfile created `O_EXCL` holding the owner's PID. It is
    stale-tolerant because a run killed with SIGKILL cannot clean up after
    itself, and a dead owner's lockfile must not wedge the host forever;
    liveness is checked by signalling the recorded PID rather than by age,
    so a slow run is never mistaken for a dead one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._acquired = False

    def _owner_pid(self) -> int | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but is owned by another user, so it is alive as far as
            # this lease is concerned.
            return True
        except OSError:
            return False
        return True

    def acquire(self) -> ClusterLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            owner = self._owner_pid()
            if owner is not None and self._pid_alive(owner):
                raise ClusterLeaseError(
                    f"{self.path} is leased by live process {owner}; " "another test run owns this cluster"
                ) from exc
            # Stale: the recorded owner is gone. Remove and retry once. A
            # second EEXIST means somebody won the race in between, which is
            # a genuine conflict rather than a stale file.
            self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        self._acquired = True
        return self

    def release(self) -> None:
        """Drop the lease if this process holds it. Idempotent."""
        if not self._acquired:
            return
        if self._owner_pid() == os.getpid():
            self.path.unlink(missing_ok=True)
        self._acquired = False

    def __enter__(self) -> ClusterLease:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


class Cluster:
    """A dev-stack-managed PostgreSQL cluster."""

    def __init__(
        self,
        source: LocalPostgres,
        pgdata: Path,
        *,
        port: int = DEFAULT_PORT,
        log_path: Path | None = None,
    ) -> None:
        self.source = source
        # Absolute throughout: Postgres resolves relative paths given to
        # `-o unix_socket_directories` against its own working directory,
        # not the caller's, which fails in a way the log makes look like a
        # permissions problem.
        self.pgdata = pgdata.resolve()
        self.port = port
        self.log_path = log_path.resolve() if log_path is not None else self.pgdata.parent / "logs" / "postgres.log"

    @classmethod
    def for_run(
        cls,
        source: LocalPostgres,
        run_id: str,
        *,
        base: Path,
        port: int | None = None,
    ) -> tuple[Cluster, ClusterLease]:
        """A cluster nothing else on this host is using, plus its lease.

        Every path and the port are derived from *run_id* so two concurrent
        runs — two checkouts, two candidates of a scale sequence — cannot
        collide on a data directory, a socket, or a port. The lease is
        returned rather than acquired-and-forgotten so the caller's cleanup
        releases it on the same path it took it.

        The caller owns the lease: acquire it before `start()` and release
        it after `destroy()`, or a killed run leaves a lockfile whose owner
        is gone (harmless — the next acquirer detects the dead PID — but
        noisy).
        """
        run_root = (base / f"run-{run_id}").resolve()
        return (
            cls(
                source,
                run_root / "pgdata",
                port=port if port is not None else free_port(),
                log_path=run_root / "logs" / "postgres.log",
            ),
            ClusterLease(run_root / "cluster.lease"),
        )

    # -- paths ------------------------------------------------------------

    def _bin(self, name: str) -> str:
        return str(self.source.bindir / name)

    @property
    def _socket_dir(self) -> Path:
        candidate = self.pgdata.parent / "run"
        if len(str(candidate)) > _MAX_SOCKET_DIR_LEN:
            return Path(tempfile.gettempdir()) / f"registry-devstack-{self.port}"
        return candidate

    def url(self, database: str = DEFAULT_DATABASE) -> str:
        """SQLAlchemy async URL for *database* on this cluster."""
        return f"postgresql+asyncpg://{DEFAULT_USER}:{DEFAULT_PASSWORD}" f"@localhost:{self.port}/{database}"

    # -- lifecycle --------------------------------------------------------

    @property
    def initialized(self) -> bool:
        return (self.pgdata / "PG_VERSION").is_file()

    def is_running(self) -> bool:
        """True when pg_ctl reports a live server for this data directory."""
        if not self.initialized:
            return False
        completed = subprocess.run(  # noqa: S603 - _bin() resolves an absolute path from the resolved Postgres bindir; -D/status are fixed flags, no caller input; local dev-stack tooling
            [self._bin("pg_ctl"), "-D", str(self.pgdata), "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        # pg_ctl status: 0 = running, 3 = stopped, 4 = no/invalid data dir.
        return completed.returncode == 0

    def initialize(self) -> None:
        """Run initdb if the data directory is not already a cluster."""
        if self.initialized:
            return
        self.pgdata.parent.mkdir(parents=True, exist_ok=True)
        # initdb insists on an empty (or absent) target and 0700 perms.
        if self.pgdata.exists():
            shutil.rmtree(self.pgdata)

        args = [
            self._bin("initdb"),
            "-D",
            str(self.pgdata),
            "-U",
            DEFAULT_USER,
            "--encoding=UTF8",
            # Local-only cluster on a developer machine: trust auth avoids
            # a password file without widening exposure, since the server
            # binds to localhost. The password is still set below so the
            # connection URL is identical to the compose one.
            "--auth-local=trust",
            "--auth-host=trust",
        ]
        locale = self._pick_locale()
        if locale is not None:
            args.append(f"--locale={locale}")

        completed = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 - args is initdb's absolute path plus fixed flags and an optionally-appended locale name from the host's own `locale -a` output, no caller input; local dev-stack tooling
        if completed.returncode != 0:
            raise ClusterError(f"initdb failed ({completed.returncode}):\n" f"{completed.stdout}\n{completed.stderr}")

    def _pick_locale(self) -> str | None:
        """First preferred locale the host actually has, or None for the default."""
        try:
            available = subprocess.run(  # noqa: S603 - fixed argv, no caller input; local dev-stack tooling
                ["locale", "-a"],  # noqa: S607 - "locale" is a standard POSIX utility resolved from PATH; best-effort probe already wrapped in a try/except that falls back to None on any failure
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return None
        normalized = {line.strip().lower(): line.strip() for line in available}
        for wanted in _PREFERRED_LOCALES:
            match = normalized.get(wanted.lower())
            if match is not None:
                return match
        return None

    def start(self, *, server_flags: Sequence[str] = ()) -> None:
        """Start the cluster, initialising it first if needed. Idempotent."""
        self.initialize()
        if self.is_running():
            return

        socket_dir = self._socket_dir
        socket_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        options = [
            "-p",
            str(self.port),
            "-c",
            "listen_addresses=localhost",
            "-c",
            f"unix_socket_directories={socket_dir}",
            # Pinned, not inherited. Without this the cluster takes the host's zone,
            # and `timestamptz + interval '<n> days'` is a calendar duration that
            # preserves local wall-clock across a DST transition -- so it spans 4319
            # or 4321 hours where a flat 4320 was meant. Any date-boundary assertion
            # then depends on where the developer is sitting: the same suite passes in
            # UTC and fails in a zone that observes DST, on the same server and the
            # same major. The container provider runs UTC, so pinning here is also
            # what makes the two providers differ in containerization alone, which is
            # the only way a comparison between them attributes anything correctly.
            "-c",
            "TimeZone=UTC",
            *server_flags,
        ]
        completed = subprocess.run(  # noqa: S603 - _bin() resolves an absolute path from the resolved Postgres bindir; the rest are fixed flags or repo-local paths, no caller input; local dev-stack tooling
            [
                self._bin("pg_ctl"),
                "-D",
                str(self.pgdata),
                "-l",
                str(self.log_path),
                "-o",
                " ".join(options),
                "-w",
                "-t",
                "60",
                "start",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ClusterError(
                f"pg_ctl start failed ({completed.returncode}):\n"
                f"{completed.stdout}\n{completed.stderr}\n"
                f"--- {self.log_path} ---\n{self._log_tail()}"
            )
        # Match the compose credentials so the connection URL is the same
        # string on both providers, even though auth is trust locally.
        self.psql(
            f"ALTER ROLE {DEFAULT_USER} WITH PASSWORD '{DEFAULT_PASSWORD}'",
            database="postgres",
        )

    def stop(self) -> None:
        """Stop the cluster if it is running. Idempotent."""
        if not self.is_running():
            return
        completed = subprocess.run(  # noqa: S603 - _bin() resolves an absolute path from the resolved Postgres bindir; the rest are fixed flags, no caller input; local dev-stack tooling
            [self._bin("pg_ctl"), "-D", str(self.pgdata), "-m", "fast", "-w", "stop"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ClusterError(
                f"pg_ctl stop failed ({completed.returncode}):\n" f"{completed.stdout}\n{completed.stderr}"
            )

    def destroy(self) -> None:
        """Stop the cluster and delete its data directory."""
        self.stop()
        if self.pgdata.exists():
            shutil.rmtree(self.pgdata)

    def _log_tail(self, lines: int = 40) -> str:
        if not self.log_path.is_file():
            return "(no log file)"
        return "\n".join(self.log_path.read_text(encoding="utf-8").splitlines()[-lines:])

    # -- databases --------------------------------------------------------

    def psql(self, sql: str, *, database: str = DEFAULT_DATABASE) -> str:
        """Run *sql* via psql and return stdout. Raises ClusterError on failure."""
        completed = subprocess.run(  # noqa: S603 - _bin() resolves an absolute path; sql/database come only from this module's own hardcoded call sites, never external input; passed as one argv element (no shell), so no shell-metacharacter risk either way
            [
                self._bin("psql"),
                "-h",
                "localhost",
                "-p",
                str(self.port),
                "-U",
                DEFAULT_USER,
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-tAc",
                sql,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ClusterError(
                f"psql failed ({completed.returncode}) on {database}: {sql}\n" f"{completed.stdout}\n{completed.stderr}"
            )
        return completed.stdout.strip()

    def database_exists(self, name: str) -> bool:
        out = self.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'", database="postgres")  # noqa: S608 - name is always DEFAULT_DATABASE or a test-harness-generated `registry_test_<pid>` string, never external input; local-only devstack tooling
        return out == "1"

    def create_database(self, name: str) -> None:
        """Create *name* if absent, with the extensions the migrations need."""
        if not self.database_exists(name):
            self.psql(f'CREATE DATABASE "{name}"', database="postgres")
        self.ensure_extensions(name)

    def drop_database(self, name: str) -> None:
        """Drop *name* if present, disconnecting any stragglers first."""
        if not self.database_exists(name):
            return
        self.psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608 - name is always DEFAULT_DATABASE or a test-harness-generated `registry_test_<pid>` string, never external input; local-only devstack tooling
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()",
            database="postgres",
        )
        self.psql(f'DROP DATABASE IF EXISTS "{name}"', database="postgres")

    def ensure_extensions(self, database: str = DEFAULT_DATABASE) -> None:
        """Install the extensions the migrations assume are available.

        Migrations create these themselves, but doing it here means a
        missing pgvector surfaces as a clear error at stack startup
        rather than midway through `alembic upgrade head`.

        pgvector is required. pgcrypto is installed when the server has
        it and skipped otherwise: the schema's only use of it is
        gen_random_uuid(), which Postgres has provided in core since 13,
        and contrib-free builds are common enough to be worth supporting.
        """
        self.psql("CREATE EXTENSION IF NOT EXISTS vector", database=database)
        self.psql(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
                    CREATE EXTENSION IF NOT EXISTS pgcrypto;
                END IF;
            END
            $$
            """,
            database=database,
        )
