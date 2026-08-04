"""The vector-width rebuild path, executed for the first time.

This migration's rebuild branch only runs behind an explicit `EMBEDDING_DIM_ALLOW_REBUILD`
opt-in, which is why it carried three defects for its whole life without anyone noticing:
it named columns that did not exist, it built indexes on partition names that were never
created, and its conflict clause was inert. All three are fixed -- and a fix to code that
has still never executed is only differently unverified, so this drives the real thing
against a real database.

Runs its own throwaway cluster rather than the session database, because the migration
truncates `embeddings` and widens a column: doing that to the shared fixture would break
every test that ran afterwards.

**One limitation, found by writing this and worth stating plainly.** This is an ordinary
migration, so it runs once -- while migrating *through* revision 0022 -- and never again.
So it can only set the width during an initial migration. An operator who changes
`EMBEDDING_DIM` on a database already at head gets a no-op `upgrade head` and then a
startup guard that refuses to boot on the mismatch, with nothing able to resolve it. That
is a deeper defect than the three fixed here and it needs a repeatable operation -- a
script, not a migration. These tests therefore drive the path from an empty database,
which is the only path that exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_DIM = 128


def _pg_bin() -> Path | None:
    raw = os.environ.get("REGISTRY_PG_BINDIR")
    return Path(raw) if raw else None


@pytest.fixture
def rebuild_cluster() -> Iterator[str]:
    """A cluster of its own, torn down afterwards."""
    bindir = _pg_bin()
    if bindir is None or not (bindir / "initdb").exists():
        pytest.skip("REGISTRY_PG_BINDIR is not set; this test needs its own cluster")

    data_dir = Path(tempfile.mkdtemp(prefix="pg-dimrebuild-"))
    socket_dir = Path(tempfile.mkdtemp(prefix="pg-sock-"))
    port = "5487"
    try:
        subprocess.run(  # noqa: S603
            [str(bindir / "initdb"), "-D", str(data_dir), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
            check=True,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603
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
        subprocess.run(  # noqa: S603
            [str(bindir / "createdb"), "-h", str(socket_dir), "-p", port, "-U", "postgres", "rebuild"],
            check=True,
            capture_output=True,
        )
        yield f"postgresql://postgres@/rebuild?host={socket_dir}&port={port}"
    finally:
        subprocess.run(  # noqa: S603
            [str(bindir / "pg_ctl"), "-D", str(data_dir), "stop"], capture_output=True, check=False
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _alembic(dsn: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    # The interpreter running the tests, not one assumed to sit at a fixed path
    # inside the checkout. A hard-coded `.venv/bin/python` is absent in a git
    # worktree, where the virtualenv lives in the primary checkout only, and the
    # failure is a bare FileNotFoundError that says nothing about why. Every
    # other subprocess in the suite already uses sys.executable.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
        env={**os.environ, "DATABASE_URL": dsn.replace("postgresql://", "postgresql+asyncpg://"), **env},
    )


def test_the_rebuild_refuses_without_the_explicit_opt_in(rebuild_cluster: str) -> None:
    """An unattended deploy with a mistyped width must fail, not erase the index.

    Driven from an empty database with a non-default width, because that is the only way
    this branch can fire: it is an ordinary one-shot migration, so it runs while migrating
    *through* it and never again. See the module docstring.
    """
    refused = _alembic(rebuild_cluster, {"EMBEDDING_DIM": str(_TARGET_DIM)})
    assert refused.returncode != 0
    assert b"EMBEDDING_DIM_ALLOW_REBUILD" in refused.stdout + refused.stderr


def test_the_rebuild_widens_the_column_and_rebuilds_the_indexes(rebuild_cluster: str) -> None:
    """The whole branch, executed for the first time.

    Asserts what it can from a fresh chain: the column ends at the new width, the HNSW
    indexes exist on the partitions that were actually created -- the old code built them
    on names that never existed -- and the migration reaches head rather than failing on
    the claim re-enqueue, which is guarded because `lmm_claims` does not exist this early.
    """
    result = _alembic(
        rebuild_cluster,
        {"EMBEDDING_DIM": str(_TARGET_DIM), "EMBEDDING_DIM_ALLOW_REBUILD": "true"},
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode()

    with psycopg2.connect(rebuild_cluster) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            " WHERE c.relname = 'embeddings' AND a.attname = 'vector'"
        )
        assert cur.fetchone()[0] == _TARGET_DIM

        cur.execute(
            "SELECT count(*) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            " JOIN pg_am am ON am.oid = i.relam WHERE am.amname = 'hnsw'"
        )
        assert cur.fetchone()[0] >= 1, "no HNSW index survived the rebuild"


def test_a_default_width_leaves_the_column_alone(rebuild_cluster: str) -> None:
    """The early return. Every ordinary migration run takes this path, so it is the one
    that must not require an opt-in or touch the index."""
    assert _alembic(rebuild_cluster, {}).returncode == 0

    with psycopg2.connect(rebuild_cluster) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            " WHERE c.relname = 'embeddings' AND a.attname = 'vector'"
        )
        assert cur.fetchone()[0] == 384


def test_the_claim_re_enqueue_is_guarded_on_the_table_existing(rebuild_cluster: str) -> None:
    """The defect this test found.

    The claim refill names `lmm_claims`, and this migration runs five revisions before the
    one that creates it. Unguarded, any non-default width on a fresh database failed the
    whole chain with "relation does not exist" -- so the fix to the three original defects
    had introduced a fourth.
    """
    assert (
        _alembic(
            rebuild_cluster,
            {"EMBEDDING_DIM": str(_TARGET_DIM), "EMBEDDING_DIM_ALLOW_REBUILD": "true"},
        ).returncode
        == 0
    ), "a non-default width must not break the chain before lmm_claims exists"

    with psycopg2.connect(rebuild_cluster) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('lmm_claims') IS NOT NULL")
        assert cur.fetchone()[0], "the chain should still have reached the claim tables"
