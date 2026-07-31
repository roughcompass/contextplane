"""Startup guard on the stored vector width.

`embeddings.vector` has a fixed width in the schema. If the configured
`EMBEDDING_DIM` disagrees with it, every insert the drain attempts fails — but
the drain is a background job whose errors surface as a rising attempt counter,
not a crash. The API keeps accepting writes, the outbox keeps growing, and
nothing that a health check looks at goes red. Catching it at startup turns a
slow invisible failure into a refused boot.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from registry.config import Settings
from registry.main import _assert_embedding_dim_matches


def _settings(dim: int = 384) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/r",
        pgbouncer_url="postgresql+asyncpg://u:p@localhost/r",
        scheduler_jobstore_url="postgresql+asyncpg://u:p@localhost/r",
        embedding_dim=dim,
    )


def _session_factory(atttypmod: int | None):
    """Fake session factory whose only query returns the column's typmod."""

    class _Result:
        def first(self):
            return None if atttypmod is None else (atttypmod,)

    class _Session:
        async def execute(self, *_args, **_kwargs):
            return _Result()

    @asynccontextmanager
    async def _factory_cm():
        yield _Session()

    def factory():
        return _factory_cm()

    return factory


async def test_matching_dimension_passes():
    await _assert_embedding_dim_matches(_session_factory(384), _settings(384))


async def test_mismatched_dimension_refuses_to_start():
    with pytest.raises(RuntimeError) as excinfo:
        await _assert_embedding_dim_matches(_session_factory(384), _settings(1536))
    message = str(excinfo.value)
    assert "384" in message and "1536" in message


async def test_error_offers_both_ways_out():
    with pytest.raises(RuntimeError) as excinfo:
        await _assert_embedding_dim_matches(_session_factory(384), _settings(1536))
    message = str(excinfo.value)
    assert "EMBEDDING_DIM=384" in message, "should offer matching config to the schema"
    assert "EMBEDDING_DIM_ALLOW_REBUILD" in message, "should offer rebuilding the schema"


async def test_unconstrained_column_is_not_a_mismatch():
    """A bare `vector` column reports typmod -1 — no constraint, not a conflict."""
    await _assert_embedding_dim_matches(_session_factory(-1), _settings(1536))


async def test_missing_table_is_tolerated():
    """Migrations may not have run yet; that is not this check's job to report."""
    await _assert_embedding_dim_matches(_session_factory(None), _settings(384))
