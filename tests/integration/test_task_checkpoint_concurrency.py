"""Concurrent appends to one task chain, against a real Postgres.

The unit tests prove what the service decides. What they cannot prove is the
part that only exists when two connections are involved at once: whether the
task lock actually serializes appends, whether the append-only trigger refuses
a rewrite issued outside the service, and whether the sequence index catches a
writer that bypassed the lock entirely.

Each of the three database-enforced rules is exercised the way it would be
violated in production -- by raw SQL on a second connection, not through the
service that already refuses it in Python. A rule that only Python enforces is
one the next writer skips without noticing.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, NotFoundError
from contextplane.types import SystemClock, TenantContext
from contextplane.workspaces.checkpoints import TaskCheckpointService

_OTHER_TENANT_SLUG_PREFIX = "cp-other"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_principal(factory: async_sessionmaker[AsyncSession], prefix: str = "cp-task") -> TenantContext:
    """A tenant and a real actor in it.

    The actor is not decoration: the audit row an append writes carries
    `actor_id`, and `audit_log` has a foreign key onto `actors`. A context built
    around an actor that does not exist would fail the append itself -- which is
    exactly the behaviour under test, since the audit row shares the append's
    transaction.
    """
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": tenant_id, "s": f"{prefix}-{tenant_id.hex[:10]}", "n": "checkpoint concurrency"},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'checkpoint-test-actor', :sub, now())"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}"},
        )
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


@pytest_asyncio.fixture
async def principal(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    return await _seed_principal(factory)


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> TaskCheckpointService:
    return TaskCheckpointService(session_factory=factory, clock=SystemClock(), retention_policy="standard")


async def _chain(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, task_id: uuid.UUID) -> list:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT checkpoint_id, sequence, predecessor_id, digest FROM task_checkpoints "
                "WHERE tenant_id = :t AND task_id = :task ORDER BY sequence"
            ),
            {"t": tenant_id, "task": task_id},
        )
        return result.mappings().all()


# ---------------------------------------------------------------------------
# Concurrent appends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_appends_produce_one_ordered_chain(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    """Six writers finishing at once produce six links, not six sequence 1s."""
    task_id = uuid.uuid4()

    results = await asyncio.gather(
        *(
            service.append_checkpoint(
                principal, task_id=task_id, payload={"goal": f"step {n}"}, idempotency_key=f"key-{n}"
            )
            for n in range(6)
        )
    )

    assert all(result.created for result in results)
    rows = await _chain(factory, principal.tenant_id, task_id)
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4, 5, 6]
    # Every link names the one before it, so a backwards walk reaches the start
    # without a hole.
    assert rows[0]["predecessor_id"] is None
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later["predecessor_id"] == earlier["checkpoint_id"]

    async with factory() as session:
        head = (
            (
                await session.execute(
                    text(
                        "SELECT head_checkpoint_id, head_sequence FROM task_heads WHERE tenant_id = :t AND task_id = :k"
                    ),
                    {"t": principal.tenant_id, "k": task_id},
                )
            )
            .mappings()
            .one()
        )
    assert head["head_sequence"] == 6
    assert head["head_checkpoint_id"] == rows[-1]["checkpoint_id"]


@pytest.mark.asyncio
async def test_concurrent_retries_of_one_key_append_exactly_once(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    """A client that retried five times in flight recorded one step, not five."""
    task_id = uuid.uuid4()
    payload = {"goal": "ship it", "decisions": ["take the lock first"]}

    results = await asyncio.gather(
        *(
            service.append_checkpoint(principal, task_id=task_id, payload=payload, idempotency_key="one-key")
            for _ in range(5)
        )
    )

    assert sum(1 for result in results if result.created) == 1
    assert len({result.checkpoint.checkpoint_id for result in results}) == 1
    rows = await _chain(factory, principal.tenant_id, task_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_reusing_a_key_with_changed_content_conflicts(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    task_id = uuid.uuid4()

    await service.append_checkpoint(principal, task_id=task_id, payload={"goal": "ship it"}, idempotency_key="k")

    with pytest.raises(ConflictError):
        await service.append_checkpoint(
            principal, task_id=task_id, payload={"goal": "ship something else"}, idempotency_key="k"
        )

    rows = await _chain(factory, principal.tenant_id, task_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_appends_to_one_task_id_in_two_tenants_are_separate_chains(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    """Colliding task ids across tenants are unrelated writers, not one queue."""
    other = await _seed_principal(factory, _OTHER_TENANT_SLUG_PREFIX)
    task_id = uuid.uuid4()

    mine, theirs = await asyncio.gather(
        service.append_checkpoint(principal, task_id=task_id, payload={"goal": "mine"}, idempotency_key="k"),
        service.append_checkpoint(other, task_id=task_id, payload={"goal": "theirs"}, idempotency_key="k"),
    )

    assert mine.checkpoint.sequence == 1
    assert theirs.checkpoint.sequence == 1
    assert mine.checkpoint.checkpoint_id != theirs.checkpoint.checkpoint_id
    with pytest.raises(NotFoundError):
        await service.get_checkpoint(other, checkpoint_id=mine.checkpoint.checkpoint_id)
    with pytest.raises(NotFoundError):
        await service.get_checkpoint_by_digest(other, digest=mine.checkpoint.digest)


# ---------------------------------------------------------------------------
# Retrieval stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_checkpoint_reads_back_unchanged_after_later_appends_and_a_summary_edit(
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    task_id = uuid.uuid4()
    evidence = [
        {
            "source_system": "GitHub",
            "source_namespace": "roughcompass/contextplane",
            "kind": "Issue",
            "external_id": "42",
            "classification": "internal",
            "external_authority": "repo-admin",
            "observed_at": datetime.datetime(2026, 5, 1, 9, 0, tzinfo=datetime.UTC),
        }
    ]

    first = await service.append_checkpoint(
        principal,
        task_id=task_id,
        payload={"goal": "step one", "open_questions": ["is the lock enough"], "next_action": "keep going"},
        idempotency_key="k1",
        evidence=evidence,
    )
    await service.append_checkpoint(principal, task_id=task_id, payload={"goal": "step two"}, idempotency_key="k2")
    await service.set_head_summary(principal, task_id=task_id, summary="a completely different story")

    by_id = await service.get_checkpoint(principal, checkpoint_id=first.checkpoint.checkpoint_id)
    by_digest = await service.get_checkpoint_by_digest(principal, digest=first.checkpoint.digest)

    assert by_id == first.checkpoint
    assert by_digest == first.checkpoint
    # Case folded once, at write time, and stable across the round trip.
    assert by_id.evidence[0].source_system == "github"
    assert by_id.evidence[0].kind == "issue"

    head = await service.get_head(principal, task_id=task_id)
    assert head["summary"] == "a completely different story"
    assert head["head_sequence"] == 2


@pytest.mark.asyncio
async def test_the_audit_row_and_the_checkpoint_commit_together(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    task_id = uuid.uuid4()

    written = await service.append_checkpoint(
        principal, task_id=task_id, payload={"goal": "ship it"}, idempotency_key="k1"
    )

    async with factory() as session:
        audit = (
            (
                await session.execute(
                    text(
                        "SELECT action, target_type, actor_id, after_jsonb FROM audit_log "
                        "WHERE tenant_id = :t AND target_id = :target"
                    ),
                    {"t": principal.tenant_id, "target": written.checkpoint.checkpoint_id},
                )
            )
            .mappings()
            .one()
        )
    assert audit["target_type"] == "task_checkpoint"
    assert audit["actor_id"] == principal.actor_id
    assert audit["after_jsonb"]["digest"] == written.checkpoint.digest
    # The audit row is attributed but carries none of the task's content.
    assert "goal" not in audit["after_jsonb"]


# ---------------------------------------------------------------------------
# What the database refuses, regardless of which writer asks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_checkpoint_cannot_be_rewritten(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    written = await service.append_checkpoint(
        principal, task_id=uuid.uuid4(), payload={"goal": "ship it"}, idempotency_key="k1"
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE task_checkpoints SET goal = 'rewritten' WHERE checkpoint_id = :c"),
                {"c": written.checkpoint.checkpoint_id},
            )


@pytest.mark.asyncio
async def test_a_stored_checkpoint_cannot_be_deleted(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    written = await service.append_checkpoint(
        principal, task_id=uuid.uuid4(), payload={"goal": "ship it"}, idempotency_key="k1"
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM task_checkpoints WHERE checkpoint_id = :c"),
                {"c": written.checkpoint.checkpoint_id},
            )


@pytest.mark.asyncio
async def test_two_writers_cannot_both_claim_one_sequence(
    factory: async_sessionmaker[AsyncSession],
    service: TaskCheckpointService,
    principal: TenantContext,
) -> None:
    """The index is the backstop for a writer that bypassed the task lock."""
    task_id = uuid.uuid4()
    written = await service.append_checkpoint(
        principal, task_id=task_id, payload={"goal": "step one"}, idempotency_key="k1"
    )

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_checkpoints (checkpoint_id, tenant_id, task_id, sequence, predecessor_id, "
                    " goal, author, recorded_at, retention_policy, digest) "
                    "VALUES (:c, :t, :task, 1, NULL, 'a second step one', 'someone-else', now(), 'standard', 'x')"
                ),
                {"c": uuid.uuid4(), "t": principal.tenant_id, "task": task_id},
            )

    rows = await _chain(factory, principal.tenant_id, task_id)
    assert [row["checkpoint_id"] for row in rows] == [written.checkpoint.checkpoint_id]


@pytest.mark.asyncio
async def test_only_the_first_checkpoint_may_name_no_predecessor(
    factory: async_sessionmaker[AsyncSession],
    principal: TenantContext,
) -> None:
    """A hole in the chain is a constraint violation, not a silently short history."""
    task_id = uuid.uuid4()

    with pytest.raises(IntegrityError, match="ck_checkpoint_predecessor"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_checkpoints (checkpoint_id, tenant_id, task_id, sequence, predecessor_id, "
                    " goal, author, recorded_at, retention_policy, digest) "
                    "VALUES (:c, :t, :task, 2, NULL, 'orphan', 'someone-else', now(), 'standard', 'x')"
                ),
                {"c": uuid.uuid4(), "t": principal.tenant_id, "task": task_id},
            )
