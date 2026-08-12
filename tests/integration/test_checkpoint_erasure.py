"""Erasing task memory against a real Postgres: the body goes, the chain stays walkable.

The unit tests prove which statements this code issues. Only a live database can
say whether those statements are the ones the immutability trigger admits -- the
trigger is where "minimize a checkpoint" and "rewrite history" are told apart, and
a fake would agree with whichever one the code happened to produce.

Three properties are proved end to end here, because each spans more than one
subsystem and each fails silently if it is only asserted in one of them:

- **A minimized checkpoint is still a checkpoint.** Identity, sequence,
  predecessor linkage, digest and recorded instant survive, so the successors that
  point at it still resolve and the chain a verifier walks has no hole in it.
- **The head summary goes with it.** The summary is a second copy of the
  checkpoint's own words, and it is removed through the registration the write
  path made -- registrar, outbox and handler in one line, which is the only
  arrangement that proves the registration is findable rather than merely written.
- **Erasing twice is one erasure.** The retry after a partial failure is the
  normal recovery path, so a second run has to report nothing left to do rather
  than mint a second proof under a later instant.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.schemas.trust import InvalidContextItem
from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import SystemClock, TenantContext
from contextplane.workers.derivative_propagation import DerivativePropagationWorker
from contextplane.workspaces import derivative_handlers
from contextplane.workspaces.checkpoints import IntentCheckpointService

_KEY_ID = "k1"
_KEY_HEX = "00112233445566778899aabbccddeeff"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def salts() -> tombstones.KeyedTenantSalt:
    """Configured key material, because an erasure with none refuses before it writes.

    The refusal is proved in the unit tier; what needs a database is the keyed
    path, and that one needs a key.
    """
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


@pytest_asyncio.fixture
async def principal(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    """A tenant, a real actor in it, and the retention policy a tombstone references.

    The policy row is seeded here rather than assumed: `source_tombstones` carries
    a foreign key onto `retention_policies`, and nothing in the tree projects the
    in-code dispositions into that table yet. Seeding it is what lets this suite
    exercise the tombstone write at all, and the same row is what a deployment will
    need before any erasure can be recorded.
    """
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    disposition = policies.disposition(policies.RECORD_TASK_CHECKPOINT)
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": tenant_id, "s": f"cp-erase-{tenant_id.hex[:10]}", "n": "checkpoint erasure"},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'checkpoint-erasure-actor', :sub, now())"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO retention_policies "
                "(policy_version, record_class, legal_basis, retention_days, erasure_mode, "
                " minimization_action, tombstone_behaviour, verifier_disclosure) "
                "VALUES (:v, :cls, :basis, :days, :mode, :action, :tombstone, :disclosure) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "v": policies.POLICY_VERSION,
                "cls": disposition.record_class,
                "basis": disposition.legal_basis,
                "days": disposition.retention_days,
                "mode": disposition.erasure_mode,
                "action": disposition.minimization_action,
                "tombstone": disposition.tombstone_behaviour,
                "disclosure": disposition.verifier_disclosure,
            },
        )
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> IntentCheckpointService:
    return IntentCheckpointService(session_factory=factory, clock=SystemClock(), retention_policy="standard")


async def _task_for(factory: async_sessionmaker[AsyncSession], ctx: TenantContext) -> uuid.UUID:
    """A task this actor participates in, granted the way a first owner has to be."""
    intent_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO intent_participant_grants "
                "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                "VALUES (:t, :task, :actor, 'owner', 'bootstrap', now() - interval '1 hour', NULL, 'explicit/v1')"
            ),
            {"t": ctx.tenant_id, "task": intent_id, "actor": str(ctx.actor_id)},
        )
    return intent_id


async def _append(
    service: IntentCheckpointService,
    ctx: TenantContext,
    intent_id: uuid.UUID,
    *,
    goal: str,
    key: str,
) -> uuid.UUID:
    result = await service.append_checkpoint(
        ctx,
        intent_id=intent_id,
        payload={"goal": goal, "decisions": ["ship it"], "next_action": f"continue {goal}"},
        idempotency_key=key,
    )
    return result.checkpoint.checkpoint_id


async def _rows(factory: async_sessionmaker[AsyncSession], sql: str, params: dict[str, object]) -> list[dict]:
    async with factory() as session:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]


async def _chain(factory: async_sessionmaker[AsyncSession], ctx: TenantContext, intent_id: uuid.UUID) -> list[dict]:
    return await _rows(
        factory,
        "SELECT checkpoint_id, sequence, predecessor_id, goal, decisions, assumptions, evidence, "
        "completed_checks, open_questions, next_action, author, recorded_at, digest "
        "FROM intent_checkpoints WHERE tenant_id = :t AND intent_id = :task ORDER BY sequence",
        {"t": ctx.tenant_id, "task": intent_id},
    )


def _participant(
    factory: async_sessionmaker[AsyncSession], salts: tombstones.KeyedTenantSalt
) -> derivative_handlers.CheckpointErasure:
    return derivative_handlers.CheckpointErasure(factory, salts)


@pytest.mark.asyncio
async def test_erasing_an_actor_clears_the_body_and_leaves_the_chain_walkable(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """The whole point of minimizing rather than deleting. Every successor names its
    predecessor, so a deleted checkpoint is a hole in the history resume walks --
    while a minimized one keeps the position and loses only the words."""
    intent_id = await _task_for(factory, principal)
    for step in range(1, 4):
        await _append(service, principal, intent_id, goal=f"step {step}", key=f"k{step}")
    before = await _chain(factory, principal, intent_id)

    counts = await _participant(factory, salts).erase_actor(principal, principal.actor_id)

    assert counts == {"checkpoints": 3, "tombstones": 3}
    after = await _chain(factory, principal, intent_id)
    assert [row["sequence"] for row in after] == [1, 2, 3]
    for original, minimized in zip(before, after, strict=True):
        # The immutable list, item by item: this is what a post-erasure verifier
        # reads, and what the trigger refuses any UPDATE for moving.
        for column in ("checkpoint_id", "sequence", "predecessor_id", "author", "recorded_at", "digest"):
            assert minimized[column] == original[column], f"{column} moved during minimization"
        assert minimized["goal"] == derivative_handlers.ERASED_CHECKPOINT_GOAL
        assert minimized["decisions"] == [] and minimized["evidence"] == []
        assert minimized["assumptions"] == [] and minimized["completed_checks"] == []
        assert minimized["open_questions"] == [] and minimized["next_action"] is None
        assert "step" not in str(minimized), "a body field still holds the erased words"

    # The linkage still resolves: every predecessor names a row that is still here.
    ids = {row["checkpoint_id"] for row in after}
    assert all(row["predecessor_id"] in ids for row in after if row["predecessor_id"] is not None)


@pytest.mark.asyncio
async def test_the_head_summary_is_removed_through_the_registration_the_write_path_made(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """Registrar, outbox and handler in one line.

    Asserting the registration exists would prove only that a row was written. What
    matters is that an erasure can *find* it: the enqueue joins source links to
    registrations, and a summary registered against the wrong source, or with no
    source, is a copy of the erased words that nothing reaches.
    """
    intent_id = await _task_for(factory, principal)
    head_id = await _append(service, principal, intent_id, goal="draft the migration", key="k1")
    await service.set_head_summary(principal, intent_id=intent_id, summary="waiting on review of the migration")

    registrations = await _rows(
        factory,
        "SELECT derivative_id, derivative_kind, storage_locator, expires_at FROM derivative_registrations "
        "WHERE tenant_id = :t",
        {"t": principal.tenant_id},
    )
    assert len(registrations) == 1, "the two head writes registered two derivatives for one artefact"
    assert registrations[0]["derivative_kind"] == derivatives.KIND_SUMMARY
    assert registrations[0]["storage_locator"] == derivative_handlers.summary_locator(intent_id)

    await _participant(factory, salts).erase_actor(principal, principal.actor_id)
    tombstone_id = (
        await _rows(
            factory,
            "SELECT tombstone_id FROM source_tombstones WHERE tenant_id = :t AND subject_id = :s",
            {"t": principal.tenant_id, "s": head_id},
        )
    )[0]["tombstone_id"]

    # The question the erasure asks: which registered derivatives were built from
    # this checkpoint. Written out rather than taken on trust, because a summary
    # registered with no source link, or against the wrong one, produces a row
    # that exists and joins to nothing -- indistinguishable from unregistered.
    reachable = await _rows(
        factory,
        "SELECT r.derivative_id FROM derivative_registrations r "
        "JOIN derivative_source_links l ON l.derivative_id = r.derivative_id "
        "WHERE r.tenant_id = :t AND l.source_record_class = :cls AND l.source_id = :sid",
        {"t": principal.tenant_id, "cls": policies.RECORD_TASK_CHECKPOINT, "sid": head_id},
    )
    assert len(reachable) == 1, "the summary was registered but an erasure cannot reach it from its source"

    # The work item is planted rather than enqueued through the retention module's
    # own writer. That writer binds its parameters bare inside a SELECT list, so
    # Postgres types them as text and the uuid/timestamptz columns refuse them --
    # a defect in code this task does not own and is being repaired alongside it.
    # Planting the row keeps this test measuring what it is about: whether the
    # registration a head write made is one the drain can apply a handler to.
    now = datetime.datetime.now(datetime.UTC)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO derivative_work_outbox "
                "(tenant_id, derivative_id, operation, trigger, tombstone_id, available_at) "
                "VALUES (:t, :did, :op, :trigger, :tombstone, :now)"
            ),
            {
                "t": principal.tenant_id,
                "did": reachable[0]["derivative_id"],
                "op": derivatives.OPERATION_DELETE,
                "trigger": derivatives.TRIGGER_ERASURE,
                "tombstone": tombstone_id,
                "now": now,
            },
        )
        await session.commit()

    registry = derivatives.HandlerRegistry()
    registry.register(derivative_handlers.SummaryDerivativeHandler())
    report = await DerivativePropagationWorker(factory, registry).run_once(now=now)

    assert (report.applied, report.artefacts, report.failed) == (1, 1, 0)
    head = (
        await _rows(
            factory,
            "SELECT summary, head_checkpoint_id, head_sequence FROM intent_heads "
            "WHERE tenant_id = :t AND intent_id = :task",
            {"t": principal.tenant_id, "task": intent_id},
        )
    )[0]
    assert head["summary"] == derivative_handlers.ERASED_SUMMARY
    assert "review" not in head["summary"] and "migration" not in head["summary"]
    # The projection survives its own redaction: the head still points at the chain.
    assert head["head_checkpoint_id"] == head_id and head["head_sequence"] == 1


@pytest.mark.asyncio
async def test_erasing_the_same_actor_twice_is_one_erasure(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """A retry after a partial failure is the recovery path, not an edge case. The
    second run must find nothing to do rather than mint a second proof under a
    later instant -- the tombstone's whole value is that it names one moment."""
    intent_id = await _task_for(factory, principal)
    await _append(service, principal, intent_id, goal="only step", key="k1")

    participant = _participant(factory, salts)
    first = await participant.erase_actor(principal, principal.actor_id)
    before = await _rows(
        factory,
        "SELECT tombstone_id, effective_at, proof_hmac FROM source_tombstones WHERE tenant_id = :t",
        {"t": principal.tenant_id},
    )
    second = await participant.erase_actor(principal, principal.actor_id)
    after = await _rows(
        factory,
        "SELECT tombstone_id, effective_at, proof_hmac FROM source_tombstones WHERE tenant_id = :t",
        {"t": principal.tenant_id},
    )

    assert first == {"checkpoints": 1, "tombstones": 1}
    assert second == {"checkpoints": 0, "tombstones": 0}
    assert before == after, "the retry rewrote the tombstone the first attempt minted"


@pytest.mark.asyncio
async def test_a_tombstone_proves_the_erasure_without_holding_any_of_it(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """One tombstone per erased checkpoint, and the proof commits to the digest of
    what was erased rather than carrying it. A reader without the tenant's salt
    learns that the record existed and was erased, and nothing else."""
    intent_id = await _task_for(factory, principal)
    checkpoint_id = await _append(service, principal, intent_id, goal="a memorable secret goal", key="k1")
    digest = (await _chain(factory, principal, intent_id))[0]["digest"]

    await _participant(factory, salts).erase_actor(principal, principal.actor_id)

    (row,) = await _rows(
        factory,
        "SELECT record_class, subject_id, policy_version, reason, request_authority, proof_hmac, effective_at "
        "FROM source_tombstones WHERE tenant_id = :t",
        {"t": principal.tenant_id},
    )
    assert row["record_class"] == policies.RECORD_TASK_CHECKPOINT
    assert uuid.UUID(str(row["subject_id"])) == checkpoint_id
    assert row["policy_version"] == policies.POLICY_VERSION
    assert row["request_authority"] == str(principal.actor_id)
    assert row["proof_hmac"] == tombstones.mint_proof(
        salts.salt_for(principal.tenant_id),
        record_class=policies.RECORD_TASK_CHECKPOINT,
        subject_id=checkpoint_id,
        content_digest=digest,
        effective_at=row["effective_at"],
    )
    assert digest not in row["proof_hmac"] and "secret" not in str(row)


@pytest.mark.asyncio
async def test_a_minimized_checkpoint_no_longer_reads_back_as_content(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """The consequence of preserving the digest, stated as a test rather than left
    to be discovered.

    The digest is the record's commitment to what it held, and it is kept so a
    verifier can check structure and so the tombstone's proof commits to something.
    Rehydration recomputes the digest and refuses a row whose content no longer
    matches -- which is exactly what a minimized checkpoint is. So the erased body
    cannot be served as a checkpoint, by this reader or any other, while the row's
    structural facts stay queryable.
    """
    intent_id = await _task_for(factory, principal)
    checkpoint_id = await _append(service, principal, intent_id, goal="something worth reading", key="k1")
    assert (await service.get_checkpoint(principal, checkpoint_id=checkpoint_id)).goal == "something worth reading"

    await _participant(factory, salts).erase_actor(principal, principal.actor_id)

    with pytest.raises(InvalidContextItem, match="digest does not match"):
        await service.get_checkpoint(principal, checkpoint_id=checkpoint_id)
    (row,) = await _chain(factory, principal, intent_id)
    assert row["checkpoint_id"] == checkpoint_id and row["sequence"] == 1


@pytest.mark.asyncio
async def test_an_erasure_reaches_only_the_actor_and_tenant_it_was_asked_about(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
    salts: tombstones.KeyedTenantSalt,
) -> None:
    """Erasing one person must not erase another's work. The author column and the
    tenant predicate are the two things standing between "this actor's checkpoints"
    and "every checkpoint in the database"."""
    intent_id = await _task_for(factory, principal)
    await _append(service, principal, intent_id, goal="mine", key="k1")

    other = TenantContext(tenant_id=principal.tenant_id, actor_id=uuid.uuid4(), roles=["producer"])
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'other', :sub, now())"
            ),
            {"a": other.actor_id, "t": other.tenant_id, "sub": f"sub-{other.actor_id.hex[:10]}"},
        )
    other_task = await _task_for(factory, other)
    await _append(service, other, other_task, goal="theirs", key="k1")

    await _participant(factory, salts).erase_actor(principal, principal.actor_id)

    (survivor,) = await _chain(factory, other, other_task)
    assert survivor["goal"] == "theirs", "erasing one actor reached another actor's checkpoint"


@pytest.mark.asyncio
async def test_an_ordinary_rewrite_is_still_refused_after_the_erasure_exception(
    factory: async_sessionmaker[AsyncSession],
    service: IntentCheckpointService,
    principal: TenantContext,
) -> None:
    """The exception is a write shape, not a caller. Anything that is not exactly
    the minimization -- here, a new goal with the body left in place -- is refused
    by the same trigger that admits the erasure."""
    intent_id = await _task_for(factory, principal)
    checkpoint_id = await _append(service, principal, intent_id, goal="the original", key="k1")

    with pytest.raises(DBAPIError, match="append-only"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE intent_checkpoints SET goal = 'rewritten' WHERE checkpoint_id = :c"),
                {"c": checkpoint_id},
            )
