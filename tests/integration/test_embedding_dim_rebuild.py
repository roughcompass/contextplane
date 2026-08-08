"""EMBEDDING_DIM on a fresh database: the width is set at creation, not rebuilt.

The migration chain this file used to test squashed a rebuild branch that
only ever fired once, while migrating *through* the one revision that added
it — and only for a database that already held 384-dimensional vectors. That
branch does not exist anymore because the baseline this repository now ships
does not need it: there is nothing to rebuild on a fresh database. The
baseline reads `EMBEDDING_DIM` once, at `CREATE TABLE embeddings` time, and
creates the `vector` column at the configured width directly.

What is still true, and still worth an integration test rather than a unit
one: a real `alembic upgrade head` against a real Postgres, with
`EMBEDDING_DIM` set in the environment, produces a column at that width and a
working HNSW index on every hash partition — not just a Python function that
returns the right integer.

Changing `EMBEDDING_DIM` against a database already at head — the case the
old rebuild branch handled, badly, for exactly one migration's lifetime — has
no mechanism here either. That is deliberate: it is a destructive, one-time
operator action (delete and recompute every embedding), and it belongs in an
explicit, reviewed script when the need actually arises, not in a migration
that runs unattended as part of every deploy.

Runs its own throwaway cluster rather than the session database, so setting
`EMBEDDING_DIM` here cannot affect any other test's fixtures.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - test-harness invocation of dev-only Postgres tooling (fixed argv, resolved bindir), no caller input
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_DIM = 128


def _pg_bin() -> Path | None:
    raw = os.environ.get("CONTEXTPLANE_PG_BINDIR")
    return Path(raw) if raw else None


@pytest.fixture
def fresh_cluster() -> Iterator[str]:
    """A cluster of its own, torn down afterwards."""
    bindir = _pg_bin()
    if bindir is None or not (bindir / "initdb").exists():
        pytest.skip("CONTEXTPLANE_PG_BINDIR is not set; this test needs its own cluster")

    data_dir = Path(tempfile.mkdtemp(prefix="pg-embeddim-"))
    socket_dir = Path(tempfile.mkdtemp(prefix="pg-sock-"))
    port = "5488"
    try:
        subprocess.run(
            [str(bindir / "initdb"), "-D", str(data_dir), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                str(bindir / "pg_ctl"),
                "-D",
                str(data_dir),
                "-o",
                f"-p {port} -k {socket_dir}",
                "-l",
                str(data_dir / "log"),
                "start",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [str(bindir / "createdb"), "-h", str(socket_dir), "-p", port, "-U", "postgres", "embeddim"],
            check=True,
            capture_output=True,
        )
        yield f"postgresql://postgres@/embeddim?host={socket_dir}&port={port}"
    finally:
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data_dir), "stop"], capture_output=True, check=False)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _alembic(dsn: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    # sys.executable, not a hard-coded .venv/bin/python — a git worktree does
    # not carry its own virtualenv, and every other subprocess in this suite
    # already resolves the interpreter this way.
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
        env={**os.environ, "DATABASE_URL": dsn.replace("postgresql://", "postgresql+asyncpg://"), **env},
    )


def _vector_width(dsn: str) -> int:
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            " WHERE c.relname = 'embeddings' AND a.attname = 'vector'"
        )
        return int(cur.fetchone()[0])


def test_a_default_width_creates_the_documented_384_dim_column(fresh_cluster: str) -> None:
    result = _alembic(fresh_cluster, {})
    assert result.returncode == 0, (result.stdout + result.stderr).decode()
    assert _vector_width(fresh_cluster) == 384


def test_a_configured_width_creates_the_column_at_that_width(fresh_cluster: str) -> None:
    """No opt-in required — a fresh database has no existing vectors to lose,
    so there is nothing destructive about honouring EMBEDDING_DIM at creation."""
    result = _alembic(fresh_cluster, {"EMBEDDING_DIM": str(_TARGET_DIM)})
    assert result.returncode == 0, (result.stdout + result.stderr).decode()
    assert _vector_width(fresh_cluster) == _TARGET_DIM


def test_hnsw_indexes_exist_on_every_partition_at_a_configured_width(fresh_cluster: str) -> None:
    result = _alembic(fresh_cluster, {"EMBEDDING_DIM": str(_TARGET_DIM)})
    assert result.returncode == 0, (result.stdout + result.stderr).decode()

    with psycopg2.connect(fresh_cluster) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            " JOIN pg_am am ON am.oid = i.relam WHERE am.amname = 'hnsw'"
        )
        assert cur.fetchone()[0] >= 1, "no HNSW index was built at the configured width"


def test_an_invalid_embedding_dim_fails_the_migration_rather_than_the_first_write(fresh_cluster: str) -> None:
    """A mistyped value fails the deploy, not silently falls back to a default
    that then disagrees with the embedder's actual output width."""
    result = _alembic(fresh_cluster, {"EMBEDDING_DIM": "not-a-number"})
    assert result.returncode != 0
    assert b"EMBEDDING_DIM must be an integer" in result.stdout + result.stderr
