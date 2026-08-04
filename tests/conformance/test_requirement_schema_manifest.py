"""The gate over the requirement→schema manifest: promised columns exist, live.

Runs against a migrated database rather than the migration files, because the
promise is about the state an operator actually has — a column created in one
migration and dropped in a later one reads as present in the file that created
it and is absent where it counts.

The negative case exercises the same comparison function the gate uses, with a
fabricated manifest entry, so "the gate fires on a missing column" is proven
rather than assumed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conformance.requirement_schema_manifest import (
    MANIFEST,
    RequirementColumns,
    missing_columns,
)


@pytest_asyncio.fixture
async def engine(pg_container: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield eng
    finally:
        await eng.dispose()


async def _live_columns(engine: AsyncEngine) -> set[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        )
        return {(r.table_name, r.column_name) for r in rows}


async def test_every_promised_column_exists_in_the_live_schema(engine: AsyncEngine) -> None:
    problems = missing_columns(await _live_columns(engine))
    assert not problems, "\n".join(problems)


async def test_the_gate_fires_on_a_column_the_schema_lacks(engine: AsyncEngine) -> None:
    """Negative fixture: a fabricated commitment to a column that cannot exist."""
    fabricated = (
        RequirementColumns(
            requirement_id="TEST-NEGATIVE",
            table="memory_session_events",
            columns=frozenset({"column_that_must_not_exist"}),
        ),
    )
    problems = missing_columns(await _live_columns(engine), fabricated)
    assert problems == [
        "TEST-NEGATIVE: memory_session_events.column_that_must_not_exist is promised "
        "by the requirement but absent from the live schema"
    ]


async def test_the_gate_fires_on_a_table_the_schema_lacks(engine: AsyncEngine) -> None:
    """A dropped or renamed table must read as every one of its columns missing."""
    fabricated = (
        RequirementColumns(
            requirement_id="TEST-NEGATIVE",
            table="table_that_must_not_exist",
            columns=frozenset({"size_bytes"}),
        ),
    )
    problems = missing_columns(await _live_columns(engine), fabricated)
    assert len(problems) == 1 and "table_that_must_not_exist.size_bytes" in problems[0]


def test_the_manifest_is_not_empty() -> None:
    """An empty manifest passes vacuously; this pins that it never becomes one."""
    assert len(MANIFEST) >= 2
