"""Immutable migrated-schema templates, fingerprinted by what built them.

A run that creates one database per worker cannot afford to migrate each
one: `alembic upgrade head` over the full revision chain dominates any
per-worker budget. Postgres can copy an existing database cheaply
(`CREATE DATABASE x TEMPLATE y`), so the run migrates **once** into a
template and clones it per worker.

That trade is only safe if a stale template can never be reused. A
template is a frozen copy of whatever schema the migrations produced at
some past moment; reusing one after the migrations changed hands the suite
a schema that no longer matches the code under test, and the failure
surfaces as an unrelated assertion far from the cause. So the template
carries a **fingerprint** of everything that can change its shape, and a
reuse only happens when the fingerprint still matches.

Alembic's head revision is deliberately *not* sufficient. Editing a
migration in place, or editing a helper a migration imports, leaves the
head identical while changing the schema. The fingerprint therefore covers
file bytes, not revision identifiers:

- `alembic.ini`, the migration `env.py`, and every revision file, ordered;
- every repo-local module a revision imports, resolved recursively — a
  migration that imports a shared column vocabulary changes shape when
  that vocabulary changes, with no edit to the migration itself;
- the head set and the ordered revision chain;
- the schema-affecting environment: embedding width, partition count, the
  exact timezone, and the UTC calendar date;
- the PostgreSQL and pgvector versions.

**Why the date is in there.** The baseline migration partitions by date
using `date.today()`, so the same revision chain produces a different
schema either side of UTC midnight. A run that migrates at 23:59:59 and
clones at 00:00:01 would hand out databases whose partitions do not cover
the rows the tests then write. The date is part of the fingerprint, every
migration subprocess is pinned to `TZ=UTC` so it agrees with the
fingerprint, and the date is rechecked immediately after migration and
again around every reuse. A rollover discards the candidate rather than
publishing it, and invalidates a measured run rather than quietly
producing one.

Publication is guarded by a server-scoped advisory lock so two concurrent
runs cannot both migrate the same candidate, and a published template has
new connections disabled — a template Postgres can copy is a template no
test may connect to.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess  # noqa: S404 - test-harness invocation of Alembic in a fixed argv (interpreter + module + fixed flags), no caller input
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Everything the fingerprint reads from disk, relative to the repo root.
_ALEMBIC_INI = Path("alembic.ini")
_MIGRATIONS_DIR = Path("contextplane/storage/migrations")
_VERSIONS_DIR = _MIGRATIONS_DIR / "versions"

# Environment variables that change the *shape* of the migrated schema
# rather than where it is stored. `EMBEDDING_DIM` sets the vector column
# width and `EMBEDDINGS_PARTITION_COUNT` the number of partitions, both
# read at CREATE TABLE time by the baseline migration.
_SCHEMA_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("EMBEDDING_DIM", "384"),
    ("EMBEDDINGS_PARTITION_COUNT", "8"),
)

# The timezone every migration subprocess runs under. Pinned rather than
# inherited so the `date.today()` a partition-creating migration calls
# resolves to the same calendar day the fingerprint recorded.
_MIGRATION_TZ = "UTC"

# A published template must be uncopyable-into by accident and
# unconnectable by tests. `datallowconn = false` is the flag that enforces
# the second half; it is asserted on reuse, not assumed.
_TEMPLATE_LOCK_NAMESPACE = 0x7C9A_1D01


class TemplateError(RuntimeError):
    """A template could not be built, validated, or reused."""


class DateRolloverError(TemplateError):
    """UTC midnight crossed during template creation, validation, or reuse.

    Not a failure of the schema — a failure of the *measurement*. The
    caller must discard the candidate or invalidate the run rather than
    treat the resulting template as usable.
    """


class SchemaDigestMismatch(TemplateError):
    """The migrated catalog did not match the expected canonical digest."""


def utc_date() -> str:
    """Today's UTC calendar date as `YYYY-MM-DD`.

    Read through `datetime.now(timezone.utc)` rather than `date.today()`:
    the latter is local-time and would disagree with the `TZ=UTC` the
    migration subprocesses are pinned to.
    """
    return datetime.now(UTC).date().isoformat()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# -- migration-transitive source resolution -------------------------------


def _module_to_candidate_paths(module: str, root: Path) -> Iterator[Path]:
    """Repo-local file candidates for a dotted module name."""
    parts = module.split(".")
    yield root.joinpath(*parts).with_suffix(".py")
    yield root.joinpath(*parts) / "__init__.py"


def _imported_modules(source: bytes, path: Path, root: Path) -> set[str]:
    """Dotted module names imported by *source*.

    Relative imports are resolved against the importing file's package so
    that a migration's `from .helpers import x` is followed like any other
    local import.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - a repo that cannot parse cannot migrate
        raise TemplateError(f"{path} is not parseable Python: {exc}") from exc

    modules: set[str] = set()
    try:
        package_parts = path.relative_to(root).parent.parts
    except ValueError:
        # A source outside the tree being fingerprinted: absolute imports
        # still resolve, relative ones have no anchor here.
        package_parts = ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from .x import y` inside a/b/c.py resolves against a/b.
                base = list(package_parts[: len(package_parts) - (node.level - 1)])
                if node.module:
                    base.extend(node.module.split("."))
                if base:
                    modules.add(".".join(base))
            elif node.module:
                modules.add(node.module)
    return modules


def migration_transitive_sources(root: Path | None = None) -> list[Path]:
    """Every repo-local source file the migrations reach, recursively.

    Starts from `env.py` plus every revision and follows repo-local
    imports. Third-party and stdlib imports resolve to no repo-local file
    and are therefore skipped: their versions are pinned by the lockfile,
    not by bytes in this tree.

    Returned sorted and deduplicated so the fingerprint is stable across
    filesystem iteration order.
    """
    base = root if root is not None else _REPO_ROOT
    seeds = [base / _MIGRATIONS_DIR / "env.py", *sorted((base / _VERSIONS_DIR).glob("*.py"))]

    seen: set[Path] = set()
    queue: list[Path] = [p for p in seeds if p.is_file()]
    while queue:
        current = queue.pop()
        resolved = current.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for module in _imported_modules(resolved.read_bytes(), resolved, base):
            for candidate in _module_to_candidate_paths(module, base):
                if candidate.is_file():
                    queue.append(candidate)
    return sorted(seen)


# -- fingerprint -----------------------------------------------------------


@dataclass(frozen=True)
class SchemaEnvironment:
    """The schema-affecting environment, normalized.

    Captured explicitly rather than read at hash time so a caller can
    fingerprint a hypothetical environment (the mutation tests rely on
    this) and so the recorded value is visible in evidence.
    """

    embedding_dim: str
    partition_count: str
    timezone_name: str = _MIGRATION_TZ
    utc_date: str = field(default_factory=utc_date)

    @classmethod
    def from_environ(cls, env: dict[str, str] | None = None, *, date: str | None = None) -> SchemaEnvironment:
        source = os.environ if env is None else env
        # Defaults come from _SCHEMA_ENV_VARS rather than being repeated here,
        # so adding a schema-affecting variable to that tuple is enough to put
        # it in the fingerprint.
        values = {name: source.get(name, default) for name, default in _SCHEMA_ENV_VARS}
        return cls(
            embedding_dim=values["EMBEDDING_DIM"],
            partition_count=values["EMBEDDINGS_PARTITION_COUNT"],
            timezone_name=_MIGRATION_TZ,
            utc_date=date if date is not None else utc_date(),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "embedding_dim": self.embedding_dim,
            "partition_count": self.partition_count,
            "timezone": self.timezone_name,
            "utc_date": self.utc_date,
        }


@dataclass(frozen=True)
class ServerVersions:
    """PostgreSQL and pgvector versions the template was built against."""

    postgres: str
    pgvector: str

    def as_dict(self) -> dict[str, str]:
        return {"postgres": self.postgres, "pgvector": self.pgvector}


def _file_entries(paths: Iterable[Path], root: Path) -> list[dict[str, str]]:
    """Ordered `(relative path, content digest)` pairs.

    Digests rather than raw bytes keep the hashed payload small while
    still failing on a one-byte edit anywhere in the set.
    """
    entries = []
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            # Outside the repo: identify by absolute path so an import
            # escaping the tree is visible rather than silently dropped.
            relative = path.as_posix()
        entries.append({"path": relative, "sha256": _sha256_hex(path.read_bytes())})
    return sorted(entries, key=lambda entry: entry["path"])


def fingerprint_inputs(
    *,
    root: Path | None = None,
    heads: Sequence[str],
    revision_chain: Sequence[str],
    environment: SchemaEnvironment,
    versions: ServerVersions,
    extra_sources: Sequence[Path] = (),
) -> dict[str, object]:
    """The complete, ordered payload the fingerprint hashes.

    Exposed separately from `compute_fingerprint` so a mismatch can be
    explained — a bare digest tells you a template is stale but not which
    input moved.
    """
    base = root if root is not None else _REPO_ROOT
    sources = list(migration_transitive_sources(base))
    for extra in extra_sources:
        if extra.is_file() and extra.resolve() not in {s.resolve() for s in sources}:
            sources.append(extra.resolve())

    config = [base / _ALEMBIC_INI]
    return {
        "config": _file_entries((p for p in config if p.is_file()), base),
        "sources": _file_entries(sources, base),
        "heads": sorted(heads),
        "revision_chain": list(revision_chain),
        "environment": environment.as_dict(),
        "versions": versions.as_dict(),
    }


def compute_fingerprint(**kwargs: object) -> str:
    """SHA-256 over the canonical JSON of `fingerprint_inputs`.

    `sort_keys` plus fixed separators make the digest reproducible across
    processes and Python versions; the ordered lists inside the payload
    are already normalized by `fingerprint_inputs`.
    """
    payload = fingerprint_inputs(**kwargs)  # type: ignore[arg-type]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(canonical.encode("utf-8"))


# -- canonical catalog digest ---------------------------------------------

# One query per catalog dimension, each ordered so the output is
# byte-stable. Kept as text rather than an ORM query because the digest
# must describe the database as the server sees it, independent of whether
# the models in this tree still match.
_CATALOG_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "schemas",
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' ORDER BY nspname",
    ),
    (
        "relations",
        "SELECT n.nspname, c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
        "ORDER BY n.nspname, c.relname, c.relkind",
    ),
    (
        "columns",
        "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
        "coalesce(column_default, ''), coalesce(character_maximum_length::text, ''), "
        "coalesce(numeric_precision::text, '') FROM information_schema.columns "
        "WHERE table_schema NOT LIKE 'pg_%' AND table_schema <> 'information_schema' "
        "ORDER BY table_schema, table_name, column_name",
    ),
    (
        "constraints",
        "SELECT n.nspname, conname, contype, pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
        "ORDER BY n.nspname, conname, contype",
    ),
    (
        "indexes",
        "SELECT schemaname, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema' "
        "ORDER BY schemaname, indexname",
    ),
    (
        "types",
        "SELECT n.nspname, t.typname, t.typtype FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
        "ORDER BY n.nspname, t.typname, t.typtype",
    ),
    (
        "functions",
        "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
        "ORDER BY n.nspname, p.proname, 3",
    ),
    (
        "triggers",
        "SELECT event_object_schema, event_object_table, trigger_name, action_statement "
        "FROM information_schema.triggers ORDER BY 1, 2, 3, 4",
    ),
    ("extensions", "SELECT extname, extversion FROM pg_extension ORDER BY extname"),
)


def catalog_queries() -> tuple[tuple[str, str], ...]:
    """The ordered catalog dimensions the digest covers."""
    return _CATALOG_QUERIES


def canonical_schema_digest(rows_by_dimension: dict[str, Sequence[Sequence[object]]]) -> str:
    """SHA-256 over a normalized catalog snapshot.

    Takes already-fetched rows rather than a connection so the
    normalization is unit-testable without a server, and so the same
    function digests a devstack catalog read through `psql` and a
    testcontainers one read through asyncpg.
    """

    def normalize_row(row: Sequence[object]) -> list[str]:
        # None and "" collapse together: two drivers report a null default
        # differently, and the digest must not move because of the driver.
        return ["" if value is None else str(value) for value in row]

    normalized = {
        dimension: [normalize_row(row) for row in rows_by_dimension.get(dimension, ())]
        for dimension, _ in _CATALOG_QUERIES
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(canonical.encode("utf-8"))


# -- migration execution --------------------------------------------------


def migration_environment(database_url: str, *, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment an `alembic upgrade head` subprocess must run with.

    `TZ` is forced rather than passed through. A migration that partitions
    by `date.today()` reads the subprocess's timezone, so an inherited
    `TZ=America/Los_Angeles` would build partitions for a different
    calendar day than the fingerprint recorded — and the mismatch would
    only appear near midnight, on some machines, which is the worst shape
    a schema bug can have.
    """
    base = dict(os.environ if env is None else env)
    base["DATABASE_URL"] = database_url
    base["TZ"] = _MIGRATION_TZ
    return base


def run_migrations(database_url: str, *, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
    """Bring *database_url* to head with every subprocess pinned to UTC."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(cwd if cwd is not None else _REPO_ROOT),
        env=migration_environment(database_url, env=env),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TemplateError(f"alembic upgrade head failed:\n{completed.stdout}\n{completed.stderr}")


def alembic_heads(*, cwd: Path | None = None) -> list[str]:
    """Head revision identifiers, as Alembic reports them."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=str(cwd if cwd is not None else _REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TemplateError(f"alembic heads failed:\n{completed.stdout}\n{completed.stderr}")
    return sorted(line.split()[0] for line in completed.stdout.splitlines() if line.strip())


def revision_chain(*, root: Path | None = None) -> list[str]:
    """Revision identifiers in dependency order, oldest first.

    Read from the revision files' own `revision`/`down_revision` values
    rather than by shelling out, so the chain is available to a unit test
    with no Alembic environment configured.
    """
    base = root if root is not None else _REPO_ROOT
    down_of: dict[str, str | None] = {}
    for path in sorted((base / _VERSIONS_DIR).glob("*.py")):
        tree = ast.parse(path.read_bytes())
        revision: str | None = None
        down: str | None = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "revision" and isinstance(node.value, ast.Constant):
                    revision = str(node.value.value)
                elif target.id == "down_revision":
                    down = str(node.value.value) if isinstance(node.value, ast.Constant) and node.value.value else None
        if revision is not None:
            down_of[revision] = down

    ordered: list[str] = []
    remaining = dict(down_of)
    placed: set[str | None] = {None}
    while remaining:
        progressed = False
        for revision in sorted(remaining):
            if remaining[revision] in placed:
                ordered.append(revision)
                placed.add(revision)
                del remaining[revision]
                progressed = True
        if not progressed:
            # A cycle or a dangling down_revision. Append what is left in a
            # stable order rather than looping: the fingerprint's job is to
            # change when the chain changes, not to validate it.
            ordered.extend(sorted(remaining))
            break
    return ordered


# -- publication and reuse ------------------------------------------------


@dataclass(frozen=True)
class TemplateIdentity:
    """A published template and the fingerprint that justifies reusing it."""

    database: str
    fingerprint: str
    schema_digest: str
    utc_date: str

    def as_evidence(self) -> dict[str, str]:
        """Non-secret fields safe to record in evidence."""
        return {
            "template": self.database,
            "fingerprint": self.fingerprint,
            "schema_digest": self.schema_digest,
            "utc_date": self.utc_date,
        }


def template_name(fingerprint: str, *, prefix: str = "cp_tmpl") -> str:
    """Deterministic template database name for a fingerprint.

    Truncated because Postgres caps identifiers at 63 bytes; the prefix
    keeps templates recognizable in `\\l` output, and 32 hex characters of
    SHA-256 is far past any collision that matters for a test host.
    """
    return f"{prefix}_{fingerprint[:32]}"


def advisory_lock_key(fingerprint: str) -> int:
    """A stable 64-bit advisory-lock key for a fingerprint.

    Server-scoped: two runs building the same template must serialize,
    while runs building *different* templates must not block each other.
    Derived from the fingerprint so the key follows the template rather
    than the process.
    """
    digest = hashlib.sha256(f"{_TEMPLATE_LOCK_NAMESPACE}:{fingerprint}".encode()).digest()
    # Signed 63-bit: pg_advisory_lock takes a bigint, and a value with the
    # sign bit set is a different key than the same bits unsigned.
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def assert_no_rollover(started: str, *, stage: str) -> None:
    """Fail if the UTC calendar date moved since *started*.

    Called immediately after migration, after the schema digest, and
    around reuse. Each call site is a point where continuing would mean
    publishing or trusting a template built under a different calendar day
    than the one its fingerprint claims.
    """
    current = utc_date()
    if current != started:
        raise DateRolloverError(
            f"UTC date rolled from {started} to {current} during {stage}; "
            "the template's partition DDL no longer matches its fingerprint"
        )
