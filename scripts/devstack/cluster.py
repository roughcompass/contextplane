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

import shutil
import subprocess
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
