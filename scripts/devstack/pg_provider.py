"""Locate a usable PostgreSQL installation for the local dev stack.

The dev stack needs Postgres 16 with the `vector` and `pgcrypto`
extensions. A container image is the most convenient way to get that, but
it is not the only one, and some environments do not permit a container
runtime at all. This module decouples "which Postgres" from "how the dev
stack uses it" by resolving a *bindir* — a directory holding `initdb`,
`pg_ctl`, `postgres`, and `psql` — from whichever source is available.

Sources are tried in this order:

1. ``DATABASE_URL`` — a database somebody else manages. No cluster
   lifecycle; the dev stack connects and nothing more.
2. ``REGISTRY_PG_BINDIR`` — explicit override for an install in a place
   this module would not think to look.
3. Postgres.app (macOS), preferring the major version the project targets.
4. ``initdb`` on ``PATH`` — a system or distro package.
5. The ``pgserver`` distribution, which ships PostgreSQL and pgvector
   binaries inside a wheel. Install with ``pip install -e ".[devstack]"``.

Every source except the first converges on the same cluster manager in
``cluster.py``, so supporting a new one means adding a candidate function
here and nothing else.

Resolution never guesses about pgvector: a resolved bindir is only
returned after `vector.control` is confirmed present. Migrations run
``CREATE EXTENSION vector`` and build an HNSW index, so a Postgres
without pgvector fails partway through `alembic upgrade head` with an
error that says nothing about the real problem.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The port the dev stack publishes Postgres on. Deliberately not 5432:
# it matches the compose port mapping so that every downstream default
# (scripts/seed.py, scripts/bootstrap_dev_tenant.py, the curl examples in
# the docs) works against either provider without being told which is
# running.
DEFAULT_PORT = 5544
DEFAULT_DATABASE = "registry"
DEFAULT_USER = "postgres"
DEFAULT_PASSWORD = "password"  # noqa: S105 - throwaway credential for a local-only Postgres cluster bound to localhost (see cluster.py's own docstring); not a credential for anything network-reachable

# Executables the cluster manager needs. `psql` is included because admin
# operations (create database, create extension) go through it — the
# project depends on asyncpg only, so there is no synchronous driver
# available for bootstrap work that must happen before the app starts.
REQUIRED_BINARIES = ("initdb", "pg_ctl", "postgres", "psql")

# Postgres major the project targets. Other majors are usable and are
# accepted with a warning; CI and the container image both run this one.
PREFERRED_MAJOR = 16

_POSTGRES_APP_VERSIONS = Path("/Applications/Postgres.app/Contents/Versions")

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?")


class PostgresUnavailableError(RuntimeError):
    """No usable Postgres could be found. Message enumerates every option."""


@dataclass(frozen=True)
class ExternalPostgres:
    """A database managed outside the dev stack.

    The dev stack connects to it and never starts, stops, or initialises
    anything. This is the escape hatch for a shared team instance, a
    CI service container, or any environment where installing Postgres
    locally is not possible.
    """

    url: str

    @property
    def label(self) -> str:
        return f"external database ({_redact(self.url)})"


@dataclass(frozen=True)
class LocalPostgres:
    """A local Postgres installation the dev stack manages a cluster with."""

    bindir: Path
    source: str
    version: str
    extension_dir: Path

    @property
    def label(self) -> str:
        return f"{self.source} — PostgreSQL {self.version} ({self.bindir})"

    @property
    def major(self) -> int:
        match = _VERSION_RE.match(self.version)
        return int(match.group(1)) if match else 0


PostgresSource = ExternalPostgres | LocalPostgres


def _redact(url: str) -> str:
    """Strip the password from a connection URL for display."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def _extension_dir(bindir: Path) -> Path | None:
    """Return the extension directory for *bindir*, or None if pgvector is absent.

    `pg_config --sharedir` is authoritative for a normally-installed
    Postgres, but reports the build-time prefix for a relocated one (the
    `pgserver` wheel, for instance). Both layouts are checked, and the
    answer is whichever directory actually contains `vector.control`.
    """
    candidates: list[Path] = []

    pg_config = bindir / "pg_config"
    if pg_config.is_file():
        try:
            sharedir = subprocess.run(  # noqa: S603 - pg_config is an absolute path already resolved from bindir; "--sharedir" is a fixed flag, no caller input; local dev-stack tooling
                [str(pg_config), "--sharedir"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sharedir = ""
        if sharedir:
            candidates.append(Path(sharedir) / "extension")

    prefix = bindir.parent
    candidates.append(prefix / "share" / "postgresql" / "extension")
    candidates.append(prefix / "share" / "extension")

    for candidate in candidates:
        if (candidate / "vector.control").is_file():
            return candidate
    return None


def _server_version(bindir: Path) -> str | None:
    """Return the version string reported by `postgres --version`."""
    try:
        completed = subprocess.run(  # noqa: S603 - bindir/"postgres" is an absolute path already resolved; "--version" is a fixed flag, no caller input; local dev-stack tooling
            [str(bindir / "postgres"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(completed.stdout)
    return match.group(0) if match else None


def _has_required_binaries(bindir: Path) -> bool:
    return all((bindir / name).is_file() for name in REQUIRED_BINARIES)


@dataclass(frozen=True)
class _Candidate:
    """A bindir that was found, with whatever disqualified it (if anything)."""

    source: str
    bindir: Path
    problem: str | None


def _inspect(source: str, bindir: Path) -> _Candidate | LocalPostgres:
    """Validate a candidate bindir, returning a usable source or the reason it isn't."""
    if not bindir.is_dir():
        return _Candidate(source, bindir, "directory does not exist")
    if not _has_required_binaries(bindir):
        missing = [n for n in REQUIRED_BINARIES if not (bindir / n).is_file()]
        return _Candidate(source, bindir, f"missing {', '.join(missing)}")
    version = _server_version(bindir)
    if version is None:
        return _Candidate(source, bindir, "`postgres --version` did not run")
    extension_dir = _extension_dir(bindir)
    if extension_dir is None:
        return _Candidate(source, bindir, "pgvector not installed (no vector.control)")
    return LocalPostgres(bindir=bindir, source=source, version=version, extension_dir=extension_dir)


def _postgres_app_bindirs() -> list[tuple[str, Path]]:
    """Postgres.app version bindirs, project-preferred major first, then newest."""
    if not _POSTGRES_APP_VERSIONS.is_dir():
        return []
    majors: list[int] = []
    for child in _POSTGRES_APP_VERSIONS.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue  # skip the `latest` symlink; it duplicates a real version
        if child.name.isdigit():
            majors.append(int(child.name))
    majors.sort(key=lambda m: (m != PREFERRED_MAJOR, -m))
    return [(f"Postgres.app {m}", _POSTGRES_APP_VERSIONS / str(m) / "bin") for m in majors]


def _path_bindir() -> list[tuple[str, Path]]:
    found = shutil.which("initdb")
    return [("initdb on PATH", Path(found).parent)] if found else []


def _pgserver_bindir() -> list[tuple[str, Path]]:
    try:
        import pgserver
    except ImportError:
        return []
    bin_path = getattr(pgserver, "POSTGRES_BIN_PATH", None)
    if bin_path is None:
        module_file = getattr(pgserver, "__file__", None)
        if module_file is None:
            return []
        bin_path = Path(module_file).parent / "pginstall" / "bin"
    return [("pgserver (pip)", Path(bin_path))]


def _override_bindir() -> list[tuple[str, Path]]:
    override = os.environ.get("REGISTRY_PG_BINDIR")
    return [("REGISTRY_PG_BINDIR", Path(override))] if override else []


def candidates() -> list[tuple[str, Path]]:
    """Every bindir this module knows how to look for, in preference order."""
    return [
        *_override_bindir(),
        *_postgres_app_bindirs(),
        *_path_bindir(),
        *_pgserver_bindir(),
    ]


_HELP = """\
No usable PostgreSQL 16 with pgvector was found.

The dev stack can get Postgres from any one of these. Pick whichever your
environment permits:

  1. Postgres.app (macOS)
     Install it, then re-run. Recent versions bundle pgvector.

  2. A Postgres already on PATH
     Any install providing initdb/pg_ctl/postgres/psql plus pgvector.
     On Debian/Ubuntu with the PGDG repository:
       apt-get install postgresql-16 postgresql-16-pgvector

  3. The pgserver package, which ships Postgres + pgvector in a wheel
       pip install -e ".[devstack]"
     Wheels exist for macOS and Linux x86_64 on Python 3.9-3.12. There is
     no wheel for Python 3.13 or Linux arm64 — use another option there.

  4. An install in a non-standard location
       export REGISTRY_PG_BINDIR=/path/to/postgres/bin

  5. A database somebody else runs (shared team instance, CI service)
       export DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/registry
     The dev stack will connect without managing a cluster.

Also usable: `docker compose up -d`, if a container runtime is available.
"""


def resolve(*, allow_external: bool = True) -> PostgresSource:
    """Return the first usable Postgres source.

    Raises PostgresUnavailableError with per-candidate diagnostics when
    nothing is usable, so the developer learns which options were tried
    and what was wrong with each rather than just that it failed.
    """
    external = os.environ.get("DATABASE_URL")
    if allow_external and external:
        return ExternalPostgres(url=external)

    rejected: list[_Candidate] = []
    for source, bindir in candidates():
        result = _inspect(source, bindir)
        if isinstance(result, LocalPostgres):
            return result
        rejected.append(result)

    lines = [_HELP]
    if rejected:
        lines.append("Candidates that were tried:\n")
        lines.extend(f"  - {c.source}: {c.problem}\n    {c.bindir}\n" for c in rejected)
    raise PostgresUnavailableError("\n".join(lines))


def resolve_local() -> LocalPostgres:
    """Resolve a Postgres the dev stack can manage a cluster with.

    Same as `resolve(allow_external=False)`, but typed to the only thing
    that call can return, so callers that go on to manage a cluster do
    not each have to re-narrow the union.
    """
    source = resolve(allow_external=False)
    if not isinstance(source, LocalPostgres):  # pragma: no cover - defensive
        raise PostgresUnavailableError("resolve(allow_external=False) returned an external source")
    return source


def describe() -> str:
    """One-line summary of the resolved source, for `make dev-status`."""
    try:
        return resolve().label
    except PostgresUnavailableError:
        return "unavailable (run `make dev-up` for the full diagnostic)"
