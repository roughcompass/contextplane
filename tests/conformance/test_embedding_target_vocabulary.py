"""The embedding store's discriminator is a closed set, and the schema agrees with the code.

Two rules that only work together. The readers filter on `target_type` to decide which
rows are theirs -- capability search wants facts, the claim surface wants claims. With an
open vocabulary "filter to facts" is a guess about what else might be in the table; with a
closed one it is an invariant, and the filter becomes a control a test can break.

The column was unconstrained `TEXT` for the pipeline's whole life, which is how it stayed
stuck at a single value while a second index was built alongside it.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from registry.embedding.targets import EMBEDDING_TARGETS

# Every table carrying the discriminator. All three, because a vocabulary enforced on one
# and not the others would let a row enter the queue that the store would then refuse --
# a failure that surfaces at drain time, far from its cause.
_TABLES = ("embeddings", "embedding_outbox", "embedding_outbox_failed")

_CONSTRAINTS = {
    "embeddings": "ck_embed_target_type",
    "embedding_outbox": "ck_outbox_target_type",
    "embedding_outbox_failed": "ck_outbox_failed_target_type",
}


def _members(definition: str) -> set[str]:
    """Pull the enumerated values out of a rendered CHECK definition.

    Postgres normalises `IN (...)` to `= ANY (ARRAY[...])`, so the parse targets the
    normalised form rather than what the migration wrote.
    """
    return set(re.findall(r"'([^']+)'::text", definition))


@pytest.mark.asyncio
async def test_every_discriminator_column_is_constrained(pg_container: str) -> None:
    """A missing constraint on any of the three, not just the store."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            for table in _TABLES:
                found = (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c' "
                            "   AND conname = :name"
                        ),
                        {"t": table, "name": _CONSTRAINTS[table]},
                    )
                ).first()
                assert found is not None, f"{table} has no closed vocabulary on target_type"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_schema_vocabulary_matches_the_code_vocabulary(pg_container: str) -> None:
    """Read back out of the live schema rather than trusting the migration's source.

    The constraint is rendered from the code constant, so this asserts the rendering
    actually took -- and it is the check that fails if somebody adds a target kind in
    Python and forgets that existing databases were built from the old set.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            for table in _TABLES:
                definition = (
                    await conn.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            " WHERE conrelid = CAST(:t AS regclass) AND conname = :name"
                        ),
                        {"t": table, "name": _CONSTRAINTS[table]},
                    )
                ).scalar_one()
                assert _members(str(definition)) == set(
                    EMBEDDING_TARGETS
                ), f"{table}'s constraint and EMBEDDING_TARGETS disagree: {definition}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unknown_target_kind_is_refused(pg_container: str) -> None:
    """The constraint does something, rather than enumerating a set nothing checks."""
    import uuid

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.begin() as conn:
            tenant = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, now(), TRUE)"
                ),
                {"t": tenant, "s": f"vocab-{tenant.hex[:8]}"},
            )
        with pytest.raises(Exception, match="ck_outbox_target_type"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO embedding_outbox "
                        "  (outbox_id, tenant_id, target_type, target_id, text_to_embed, "
                        "   chunk_plan, enqueued_at, attempts) "
                        "VALUES (gen_random_uuid(), :t, 'interface', gen_random_uuid(), "
                        "        'x', '[]'::jsonb, now(), 0)"
                    ),
                    {"t": tenant},
                )
    finally:
        await engine.dispose()


def test_the_rendered_set_is_stable() -> None:
    """`sql_set` sorts, so the generated DDL does not change between runs.

    A frozenset renders in hash order otherwise, which would make the same migration
    produce a different constraint definition on different interpreters.
    """
    from registry.embedding.targets import sql_set

    assert sql_set(EMBEDDING_TARGETS) == "'claim', 'fact'"


# --- one rule, two languages ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_claim_text_rule_is_the_same_in_python_and_in_sql(pg_container: str) -> None:
    """`index_text` and the migration's re-enqueue must render a claim identically.

    The migration that widens the vector column truncates the store and re-enqueues
    everything, so it has to build the claim text itself -- in SQL, because it cannot call
    into the application. That makes one rule exist in two languages, and two copies of a
    rule drift.

    If they disagree, a width change silently re-embeds every claim under text that does
    not match what the live path would produce, so a claim's vector stops corresponding to
    its own rendering and ranking quietly degrades. Nothing else would notice.
    """
    from registry.service.retrieval.embedding_index import index_text

    cases: list[tuple[str, object]] = [
        ("owned_by_team", "platform"),
        ("runbook_url", "https://runbooks/auth"),
        ("recovery_time_objective_seconds", 900),
        ("is_publicly_callable", True),
        ("target_availability", "0.999"),
    ]

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            for predicate, value in cases:
                import json as _json

                rendered = (
                    await conn.execute(
                        text(
                            "SELECT replace(CAST(:pred AS TEXT), '_', ' ') || ': ' || "
                            "       CASE WHEN jsonb_typeof(CAST(:val AS JSONB)) = 'string' "
                            "            THEN CAST(:val AS JSONB) #>> '{}' "
                            "            ELSE CAST(:val AS JSONB)::text END"
                        ),
                        {"pred": predicate, "val": _json.dumps(value)},
                    )
                ).scalar_one()
                assert str(rendered) == index_text(predicate, value), (
                    f"SQL and Python disagree for {predicate}={value!r}: "
                    f"{rendered!r} vs {index_text(predicate, value)!r}"
                )
    finally:
        await engine.dispose()
