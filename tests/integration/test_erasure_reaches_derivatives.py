"""The whole chain, against a real database: cause, queue, handler, artefact.

Every other test in this area proves one link. The participant finds what the
actor authored; the enqueuer writes an outbox row; a handler, given a
registration, removes the thing it addresses. All three passed while the chain
itself did not exist — nothing constructed the drain, so an erasure wrote its
tombstone, queued its work, and the erased person's words stayed in the summary,
the receipt and the vector indefinitely.

So these tests assert the property that actually matters and that no single link
can state: **after an erasure and a drain, the words are gone from the artefacts,
and nothing is left overdue.** Reading the artefact tables back is the only way to
say that, because every intermediate step reports success whether or not the last
one happened.

Three causes, because they enter the queue by three different routes and only the
handler stage is shared:

- **erasure** — a person asked; the participant walks their records.
- **revocation** — a source withdrew material; one signal, no person.
- **expiry** — nobody asked, the clock ran out, and the sweep found it.

The artefacts here are a task head summary and a receipt's item keys: two real
tables, holding two different shapes of the person's own words, reached by two
handlers that resolve their locators differently. That is enough to prove the
chain end to end without seeding every kind — the registration pin next door is
what proves no kind is missing a handler.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context import derivative_handlers as receipt_handlers
from contextplane.context.derivatives import ContextDerivativeErasure
from contextplane.retention import derivatives, holds, policies, tombstones
from contextplane.signals.erasure import revoke_signal
from contextplane.types import TenantContext
from contextplane.wiring.derivatives import build_propagation_worker
from contextplane.workers import retention_expiry
from contextplane.workers.derivative_propagation import pending_overdue
from contextplane.workspaces import derivative_handlers as summary_handlers

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_LATER = _NOW + datetime.timedelta(days=365)
_LONG_AGO = _NOW - datetime.timedelta(days=365)

#: The erased person's own words, planted in every artefact so a single grep of
#: the tables afterwards is a real assertion rather than a proxy for one.
_THEIR_WORDS = "quarterly revenue slipped because I missed the migration"

_KEY_ID = "test-key"
_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


class _FixedClock:
    def now(self) -> datetime.datetime:
        return _NOW


async def _seed(session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> dict[str, Any]:
    """A checkpoint with a head summary, a receipt with items, a signal, and a claim.

    Raw inserts rather than the owning services, for the reason the sibling
    end-to-end file gives: what is under test is how erasure and propagation read
    and write these tables, and routing through five services would make a column
    regression surface somewhere else entirely.
    """
    task_id = uuid.uuid4()
    checkpoint_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    signal_id = uuid.uuid4()
    claim_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO task_checkpoints (checkpoint_id, tenant_id, task_id, sequence, goal, "
            "                              author, recorded_at, retention_policy, digest) "
            "VALUES (:id, :t, :task, 1, :goal, :author, :now, 'standard', :digest)"
        ),
        {
            "id": checkpoint_id,
            "t": tenant_id,
            "task": task_id,
            "goal": _THEIR_WORDS,
            "author": str(actor_id),
            "now": _NOW,
            "digest": f"sha256:{uuid.uuid4().hex}",
        },
    )
    # The summary is the derivative: prose built from the chain, holding the words
    # the checkpoint carried.
    await session.execute(
        text(
            "INSERT INTO task_heads (tenant_id, task_id, head_checkpoint_id, head_sequence, summary, updated_at) "
            "VALUES (:t, :task, :cp, 1, :summary, :now)"
        ),
        {"t": tenant_id, "task": task_id, "cp": checkpoint_id, "summary": _THEIR_WORDS, "now": _NOW},
    )

    await session.execute(
        text(
            "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
            "VALUES (:id, :t, 'complete', FALSE, :now, :requester)"
        ),
        {"id": receipt_id, "t": tenant_id, "now": _NOW, "requester": str(actor_id)},
    )
    # `item_key` names what was cited back to the person — their own words, in a
    # table the receipt's own record knows as provenance rather than as content.
    await session.execute(
        text(
            "INSERT INTO context_receipt_items (receipt_id, receipt_item_id, block, source, item_key) "
            "VALUES (:r, :iid, 'canonical', 'memory', :key)"
        ),
        {"r": receipt_id, "iid": f"item-{uuid.uuid4().hex[:12]}", "key": _THEIR_WORDS},
    )

    await session.execute(
        text(
            "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type, "
            "                              source_event_id, idempotency_key, content_digest, authority, "
            "                              classification, ingested_at, schema_version, payload) "
            "VALUES (:id, :t, 'github', :producer, 'human', :event, :idem, :digest, 'observer_extraction', "
            "        'internal', :now, 'external_signal.v1', '{\"conclusion\": \"success\"}'::jsonb)"
        ),
        {
            "id": signal_id,
            "t": tenant_id,
            "producer": str(actor_id),
            "event": f"github:run:{uuid.uuid4().hex[:12]}",
            "idem": f"delivery-{uuid.uuid4().hex[:12]}",
            "digest": f"sha256:{uuid.uuid4().hex}",
            "now": _NOW,
        },
    )
    await session.execute(
        text(
            "INSERT INTO memory_claims (claim_id, author_tenant_id, author_actor_id, subject_reference, "
            "                           predicate, value_type, claim_category, value_jsonb, asserted_valid_from, "
            "                           status, visibility, source_authority, size_bytes, is_contested, created_at) "
            "VALUES (:id, :t, :author, 'svc:checkout', 'owns', 'string', 'ownership', "
            "        '\"team-a\"'::jsonb, :now, 'unlinked', 'private', 'unattributed', 12, FALSE, :now)"
        ),
        {"id": claim_id, "t": tenant_id, "author": actor_id, "now": _NOW},
    )

    return {
        "task_id": task_id,
        "checkpoint_id": checkpoint_id,
        "receipt_id": receipt_id,
        "signal_id": signal_id,
        "claim_id": claim_id,
    }


async def _register_artefacts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    seeded: dict[str, Any],
    expires_at: datetime.datetime = _LATER,
) -> dict[str, uuid.UUID]:
    """Register the summary and the receipt link against the records that built them."""
    summary = await derivatives.register_derivative(
        session,
        tenant_id=tenant_id,
        kind=derivatives.KIND_SUMMARY,
        storage_locator=summary_handlers.summary_locator(seeded["task_id"]),
        audience_partition=summary_handlers.summary_audience(seeded["task_id"]),
        classification="internal",
        handler_version=summary_handlers.SUMMARY_HANDLER_VERSION,
        sources=[
            derivatives.SourceRef(
                record_class=policies.RECORD_TASK_CHECKPOINT,
                source_id=seeded["checkpoint_id"],
                expires_at=expires_at,
            )
        ],
    )
    receipt_link = await derivatives.register_derivative(
        session,
        tenant_id=tenant_id,
        kind=derivatives.KIND_RECEIPT_LINK,
        storage_locator=receipt_handlers.locator_for(seeded["receipt_id"]),
        audience_partition="private",
        classification="internal",
        handler_version=receipt_handlers.HANDLER_VERSION,
        sources=[
            derivatives.SourceRef(
                record_class=policies.RECORD_CONTEXT_RECEIPT,
                source_id=seeded["receipt_id"],
                expires_at=expires_at,
            )
        ],
    )
    return {"summary": summary, "receipt_link": receipt_link}


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """A tenant, an actor, their records, and the artefacts built from them."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'derivative reach')"),
                {"t": tenant_id, "s": f"err-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
            seeded = await _seed(session, tenant_id=tenant_id, actor_id=actor_id)
            registered = await _register_artefacts(session, tenant_id=tenant_id, seeded=seeded)

        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"]),
            "seeded": seeded,
            "registered": registered,
        }
    finally:
        await engine.dispose()


async def _drain(world: dict[str, Any]) -> Any:
    """Run the drain the deployment ships, over the registry the deployment builds."""
    return await build_propagation_worker(world["factory"], _salts()).run_once(now=_NOW)


async def _drain_until_empty(world: dict[str, Any]) -> Any:
    """Drain to a standstill, and report the first tick — the one with the work in it.

    The queue and the overdue count are both process-wide rather than per-tenant,
    which is correct for a deployment (one drain serves every tenant) and means a
    sibling test's rows sit in the same queue. Draining to empty is what the
    scheduled job does on any real deployment anyway; a single tick would leave
    this asserting against whatever else happened to be queued.
    """
    first = await _drain(world)
    for _ in range(10):
        if not (await _drain(world)).had_work:
            break
    return first


async def _scalar(world: dict[str, Any], sql: str, params: dict[str, Any]) -> Any:
    async with world["factory"]() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _summary_of(world: dict[str, Any]) -> str:
    return str(
        await _scalar(
            world,
            "SELECT summary FROM task_heads WHERE tenant_id = :t AND task_id = :task",
            {"t": world["tenant_id"], "task": world["seeded"]["task_id"]},
        )
    )


async def _item_keys_of(world: dict[str, Any]) -> list[str]:
    async with world["factory"]() as session:
        rows = (
            await session.execute(
                text("SELECT item_key FROM context_receipt_items WHERE receipt_id = :r"),
                {"r": world["seeded"]["receipt_id"]},
            )
        ).all()
    return [str(row[0]) for row in rows]


async def _overdue(world: dict[str, Any]) -> int:
    """Overdue work for this tenant, an hour past the drain's own clock.

    Tenant-scoped because the queue is process-wide and this suite shares one
    database: a sibling test's rows are not this erasure's business, and the read
    paths that fail closed ask the same narrowed question for the same reason —
    refusing a tenant's read over another tenant's backlog loses availability for
    no privacy gained.
    """
    async with world["factory"]() as session:
        return await pending_overdue(
            session,
            now=_NOW + datetime.timedelta(hours=1),
            tenant_id=world["tenant_id"],
        )


# --- erasure -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_erasure_removes_the_persons_words_from_every_derivative(world: dict[str, Any]) -> None:
    """The property the whole subsystem exists for, asserted on the artefact tables.

    Not "work was enqueued" and not "a handler returned a count" — the words
    themselves, read back out of the two tables that held them.
    """
    await ContextDerivativeErasure(world["factory"], _salts(), _FixedClock()).erase_actor(
        world["ctx"], world["actor_id"]
    )

    report = await _drain(world)

    assert report.applied >= 2, f"the drain applied {report.applied} items; failures: {report.failed}"
    assert _THEIR_WORDS not in await _summary_of(world)
    assert all(_THEIR_WORDS not in key for key in await _item_keys_of(world))


@pytest.mark.asyncio
async def test_nothing_is_left_overdue_once_the_drain_has_run(world: dict[str, Any]) -> None:
    """The number the read paths fail closed on.

    A non-zero overdue count after a completed drain means an item failed its way
    to `failed`, which is a compliance incident rather than a slow queue — and the
    reads that must not serve content behind an unapplied erasure stay closed until
    somebody fixes it.
    """
    await ContextDerivativeErasure(world["factory"], _salts(), _FixedClock()).erase_actor(
        world["ctx"], world["actor_id"]
    )
    await _drain_until_empty(world)

    assert await _overdue(world) == 0


@pytest.mark.asyncio
async def test_the_erased_item_key_is_recognisable_rather_than_blank(world: dict[str, Any]) -> None:
    """A minimized key has to still say that something was there.

    Blanking it would make an erased citation indistinguishable from a receipt line
    that never had a key, which is the difference between "removed" and "lost".
    """
    await ContextDerivativeErasure(world["factory"], _salts(), _FixedClock()).erase_actor(
        world["ctx"], world["actor_id"]
    )
    await _drain(world)

    keys = await _item_keys_of(world)
    assert keys
    assert all(key.startswith(tombstones.ERASED_KEY_PREFIX) for key in keys)


@pytest.mark.asyncio
async def test_a_second_drain_finds_nothing_and_changes_nothing(world: dict[str, Any]) -> None:
    """Re-running the drain is the normal recovery path after a partial failure."""
    await ContextDerivativeErasure(world["factory"], _salts(), _FixedClock()).erase_actor(
        world["ctx"], world["actor_id"]
    )
    await _drain(world)
    after_first = await _item_keys_of(world)

    second = await _drain(world)

    assert second.claimed == 0
    assert await _item_keys_of(world) == after_first


# --- the ordering the registry depends on ---------------------------------------


@pytest.mark.asyncio
async def test_the_enqueuer_observes_claims_before_the_participant_that_deletes_them(
    world: dict[str, Any],
) -> None:
    """Registration order, proven by what gets enqueued rather than by reading the list.

    The claims participant deletes `memory_claims`; the derivative participant reads
    that same table to find what the actor authored. Registered after it, the
    enqueuer finds an empty table, schedules nothing, and the erasure reports
    success while every artefact derived from those claims keeps the person's
    words. This runs both in the order the application registers them and asserts
    the claim's derivative was queued.
    """
    from contextplane.service.governance.erasure import ErasureRegistry
    from contextplane.service.memory import derivative_handlers as claim_handlers
    from contextplane.service.memory.claim_erasure import ClaimErasure

    claim_derivative = None
    async with world["factory"]() as session, session.begin():
        claim_derivative = await derivatives.register_derivative(
            session,
            tenant_id=world["tenant_id"],
            kind=derivatives.KIND_CLAIM_DERIVATIVE,
            # The family's own spelling. A locator this handler cannot parse would
            # leave the item retrying forever, which is a defect in the test rather
            # than a property of the ordering it is about.
            storage_locator=claim_handlers.locator_for(uuid.uuid4()),
            audience_partition="private",
            classification="internal",
            handler_version="v1",
            sources=[
                derivatives.SourceRef(
                    record_class=policies.RECORD_MEMORY_CLAIM,
                    source_id=world["seeded"]["claim_id"],
                    expires_at=_LATER,
                )
            ],
        )

    registry = ErasureRegistry()
    # The wiring order: the enqueuer first, then the participants that delete.
    registry.register(ContextDerivativeErasure(world["factory"], _salts(), _FixedClock()))
    registry.register(ClaimErasure(world["factory"]))
    await registry.erase_actor(world["ctx"], world["actor_id"])

    async with world["factory"]() as session:
        queued = (
            await session.execute(
                text("SELECT derivative_id FROM derivative_work_outbox WHERE tenant_id = :t"),
                {"t": world["tenant_id"]},
            )
        ).all()

    assert claim_derivative in {row[0] for row in queued}, (
        "no propagation was scheduled for the claim's derivative, which means the enqueuing "
        "participant read `memory_claims` after something had already emptied it"
    )


# --- revocation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revoked_signal_reaches_the_artefacts_built_from_it(world: dict[str, Any]) -> None:
    """A source withdrawing its material is not a person asking, and reaches the same artefacts.

    No actor is named here at all: the cause is the signal. If revocation queued
    work that the drain could not apply, material a source explicitly withdrew
    would stay in every artefact derived from it.
    """
    async with world["factory"]() as session, session.begin():
        await derivatives.register_derivative(
            session,
            tenant_id=world["tenant_id"],
            kind=derivatives.KIND_SUMMARY,
            storage_locator=summary_handlers.summary_locator(world["seeded"]["task_id"]),
            audience_partition=summary_handlers.summary_audience(world["seeded"]["task_id"]),
            classification="internal",
            handler_version=summary_handlers.SUMMARY_HANDLER_VERSION,
            sources=[
                derivatives.SourceRef(
                    record_class=policies.RECORD_EXTERNAL_SIGNAL,
                    source_id=world["seeded"]["signal_id"],
                    expires_at=_LATER,
                )
            ],
        )

    scheduled = await revoke_signal(
        world["factory"],
        _salts(),
        ctx=world["ctx"],
        signal_id=world["seeded"]["signal_id"],
        reason="the source withdrew it",
        now=_NOW,
    )
    assert scheduled >= 1

    report = await _drain_until_empty(world)

    assert report.applied >= 1
    assert _THEIR_WORDS not in await _summary_of(world)
    assert await _overdue(world) == 0


@pytest.mark.asyncio
async def test_revocation_is_recorded_as_its_own_cause(world: dict[str, Any]) -> None:
    """Three causes, three triggers, and the record has to keep them apart.

    An operator asking why an artefact was removed gets a different answer for a
    withdrawal than for a person's request, and a shared trigger would erase that
    distinction from the only place it is written down.
    """
    async with world["factory"]() as session, session.begin():
        await derivatives.register_derivative(
            session,
            tenant_id=world["tenant_id"],
            kind=derivatives.KIND_SUMMARY,
            storage_locator=summary_handlers.summary_locator(world["seeded"]["task_id"]),
            audience_partition=summary_handlers.summary_audience(world["seeded"]["task_id"]),
            classification="internal",
            handler_version=summary_handlers.SUMMARY_HANDLER_VERSION,
            sources=[
                derivatives.SourceRef(
                    record_class=policies.RECORD_EXTERNAL_SIGNAL,
                    source_id=world["seeded"]["signal_id"],
                    expires_at=_LATER,
                )
            ],
        )

    await revoke_signal(
        world["factory"],
        _salts(),
        ctx=world["ctx"],
        signal_id=world["seeded"]["signal_id"],
        reason="the source withdrew it",
        now=_NOW,
    )

    async with world["factory"]() as session:
        triggers = (
            await session.execute(
                text("SELECT DISTINCT trigger FROM derivative_work_outbox WHERE tenant_id = :t"),
                {"t": world["tenant_id"]},
            )
        ).all()

    assert {row[0] for row in triggers} == {derivatives.TRIGGER_REVOCATION}


# --- expiry ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_finds_an_over_age_derivative_and_the_drain_removes_it(
    pg_container: str,
) -> None:
    """Nobody asked. The clock ran out, and the artefact still holds the words.

    This is the case that is invisible until it is a breach: no request, no
    incident, just a retention period that passed while the artefact stayed
    readable. Built with its own world so the expiry is in the past at
    registration rather than mutated afterwards — the registrar takes the minimum
    of stored and incoming expiry, so an update could not have moved it earlier.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'expiry reach')"),
                {"t": tenant_id, "s": f"exp-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
            seeded = await _seed(session, tenant_id=tenant_id, actor_id=actor_id)
            await _register_artefacts(session, tenant_id=tenant_id, seeded=seeded, expires_at=_LONG_AGO)

        world = {"factory": factory, "tenant_id": tenant_id, "seeded": seeded}

        sweep = retention_expiry.RetentionExpiryWorker(
            factory,
            holds.NoHoldStorage(),
            clock=_FixedClock(),
        )
        report = await sweep.run_once()
        assert report.enqueued >= 2, "the sweep found no over-age derivative to queue"

        drained = await _drain_until_empty(world)

        assert drained.applied >= 2
        assert _THEIR_WORDS not in await _summary_of(world)
        assert all(_THEIR_WORDS not in key for key in await _item_keys_of(world))
        assert await _overdue(world) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_sweep_queues_expiry_work_once_however_often_it_runs(pg_container: str) -> None:
    """One cause, one item — the property that makes a frequent schedule free.

    Idempotence lives in the schema rather than the sweep, because the sweep is the
    part that gets re-run; this proves the two agree against a real unique index
    rather than against a fake that was told to.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'expiry twice')"),
                {"t": tenant_id, "s": f"ext-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
            seeded = await _seed(session, tenant_id=tenant_id, actor_id=actor_id)
            await _register_artefacts(session, tenant_id=tenant_id, seeded=seeded, expires_at=_LONG_AGO)

        sweep = retention_expiry.RetentionExpiryWorker(factory, holds.NoHoldStorage(), clock=_FixedClock())
        first = await sweep.run_once()
        second = await sweep.run_once()

        assert first.enqueued >= 2
        assert second.enqueued == 0
    finally:
        await engine.dispose()
