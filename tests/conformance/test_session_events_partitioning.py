"""`memory_session_events` is partitioned, and the sequence key survived it.

Partitioning this table is easy to get wrong in one specific way, and the wrong
version passes every functional test. Postgres requires the partition key to
appear in every unique key, so range-partitioning by `created_at` -- the shape
`audit_log` uses, and the obvious reading of E2's "one partitioned insert" --
would force `uq_mse_session_seq` to become
`(tenant_id, actor_id, session_id, seq, created_at)`.

That is a strictly weaker constraint. A session whose events straddle a
partition boundary could hold two events with the same `seq`, and `seq` is what
replay orders by, so the symptom is duplicate turns in a replayed conversation
rather than an error. `session_events.py` also allocates the next `seq` by
inserting and retrying on unique violation, and that loop stops converging on
one winner against a weakened key.

So this file asserts the *shape* rather than the behaviour: that the table is
partitioned at all, that it is hashed on `tenant_id`, and above all that
`uq_mse_session_seq` still contains exactly its original four columns. Read from
the live schema through the catalog rather than from migration source, because a
migration that was written and never applied passes a source comparison and says
nothing about what the database enforces.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

#: The key sequence allocation depends on, exactly as it was before
#: partitioning. A fifth column here is the regression this file exists for.
_SEQUENCE_KEY = "UNIQUE (tenant_id, actor_id, session_id, seq)"


async def _scalar(session, sql: str, **params: object) -> object:
    return (await session.execute(text(sql), params)).scalar_one_or_none()


@pytest.mark.asyncio
async def test_the_table_is_partitioned(db_session) -> None:
    """`relkind = 'p'`. It was `'r'` -- a plain table -- before 0066."""
    # Cast in SQL: `relkind` is Postgres's `"char"` type, which asyncpg hands
    # back as a one-byte `bytes`, and `b"p" == "p"` is False -- a comparison
    # that fails for a reason having nothing to do with the schema.
    kind = await _scalar(db_session, "SELECT relkind::text FROM pg_class WHERE relname = 'memory_session_events'")

    assert kind == "p", f"memory_session_events is not partitioned (relkind {kind!r})"


@pytest.mark.asyncio
async def test_it_is_hashed_on_tenant_not_ranged_on_time(db_session) -> None:
    """Hash on `tenant_id`, matching `embeddings`.

    Hashing on tenant is what lets the sequence key stay intact -- `tenant_id`
    is already its leading column, so the partition key adds nothing. It also
    prunes both hot reads, since `ix_mse_replay` and `ix_mse_listing` are both
    tenant-leading, which a time range would not do for either.
    """
    strategy = await _scalar(
        db_session,
        "SELECT pg_get_partkeydef(c.oid) FROM pg_class c WHERE c.relname = 'memory_session_events'",
    )

    assert strategy == "HASH (tenant_id)", f"unexpected partitioning: {strategy!r}"


@pytest.mark.asyncio
async def test_the_sequence_key_did_not_absorb_the_partition_key(db_session) -> None:
    """The one that matters.

    If this fails with a fifth column, replay ordering is no longer unique
    within a session and the sequence-allocation retry loop no longer converges.
    Both failures are silent.
    """
    definition = await _scalar(
        db_session,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'uq_mse_session_seq'",
    )

    assert definition == _SEQUENCE_KEY, (
        f"uq_mse_session_seq changed shape: {definition!r}. A partition key absorbed into this "
        "constraint lets one session hold two events with the same seq."
    )


@pytest.mark.asyncio
async def test_every_partition_carries_the_replay_index(db_session) -> None:
    """An index declared on the parent should exist on each child.

    Asserted because a partition added later by hand -- the way a hash modulus
    change would add them -- can miss the parent's indexes, and the symptom is a
    sequential scan on the hot replay path for whichever tenants hash there.
    """
    rows = (
        await db_session.execute(
            text(
                "SELECT c.relname, "
                "       count(*) FILTER (WHERE i.indexrelid IS NOT NULL) AS indexes "
                "FROM pg_class c "
                "JOIN pg_inherits inh ON inh.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = inh.inhparent "
                "LEFT JOIN pg_index i ON i.indrelid = c.oid "
                "WHERE p.relname = 'memory_session_events' "
                "GROUP BY c.relname ORDER BY c.relname"
            )
        )
    ).all()

    assert rows, "memory_session_events has no partitions"
    # Four declared indexes plus the primary key and the unique constraint's
    # own index: every child should carry the same count as its siblings, and
    # the check is that none is short rather than what the number happens to be.
    counts = {row[1] for row in rows}
    assert len(counts) == 1, f"partitions carry differing index counts: {dict(rows)}"


#: Every CHECK the table carried before it was partitioned. Named rather than
#: counted, so a failure says which one went missing.
_CHECKS = frozenset(
    {
        "ck_mse_kind",
        "ck_mse_session_len",
        "ck_mse_body_bytes",
        "ck_mse_tool_name",
        "ck_mse_invalidation",
        "ck_mse_reason_len",
        "ck_mse_size",
        "ck_mse_tokenizer",
    }
)


@pytest.mark.asyncio
async def test_partitioning_did_not_drop_a_check(db_session) -> None:
    """A rebuild is a retyping, and a retyping loses things.

    `0066` could not convert the table in place -- Postgres has no
    `ALTER TABLE ... PARTITION BY` -- so it drops and recreates. The first draft
    of that DDL was written by hand from the column list and silently lost all
    eight CHECKs, plus `seq`'s width (`BIGINT`, not `INTEGER`) and
    `expires_at`'s NOT NULL. Two behavioural tests caught the tokenizer pair;
    nothing would have caught the other six until a bad row reached production.

    So the constraint set is asserted by name. This is cheap and it is the
    difference between "the rebuild was verified" and "the rebuild happened to
    keep the parts something else tested".
    """
    rows = (
        await db_session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'memory_session_events'::regclass AND contype = 'c'"
            )
        )
    ).all()
    present = {row[0] for row in rows}

    assert _CHECKS <= present, f"partitioning dropped CHECK(s): {sorted(_CHECKS - present)}"
