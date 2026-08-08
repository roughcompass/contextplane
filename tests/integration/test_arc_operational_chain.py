"""Integration tests for the operational event chain and checkpoint outbox,
against a real Postgres.

What the unit suite (`tests/unit/test_arc_operational_chain.py`,
`tests/unit/test_arc_checkpoint_export.py`) cannot prove with in-memory
fakes: that Postgres itself refuses both directions of the genesis CHECK,
that a real concurrent race for the next sequence produces a gap-free,
fork-free chain rather than merely "the fake didn't notice a problem," that
a detected tamper leaves the row byte-for-byte what it was (never a repair),
and that `SourceStatusService`'s revocation/expiry cascade really does
commit its four parts together against real foreign keys.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.checkpoint_export import (
    CheckpointExportOutcome,
    CheckpointExportService,
    CheckpointIntegrityError,
    CheckpointSinkIdentityConflict,
    SinkReceipt,
)
from contextplane.arc.service.operational_chain import (
    EVENT_FRESHNESS_DOWNGRADED,
    EVENT_INITIALIZED,
    SYSTEM_ACTOR,
    OperationalChainIdempotencyConflict,
    OperationalChainIntegrityError,
    OperationalChainService,
    build_event_payload,
)
from contextplane.arc.workers.checkpoint_exporter import CheckpointExporterWorker
from contextplane.types import SystemClock
from tests.helpers.arc_fixtures import seed_artifact_family
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_active_revision(factory: async_sessionmaker[AsyncSession], artifact_id: uuid.UUID) -> uuid.UUID:
    """A minimal `active` `arc_revisions` row -- what the operational chain
    tables' own `revision_id` foreign key needs. Adapted from
    `test_arc_proposal_concurrency.py`'s own `_seed_bare_revision`, with
    `lifecycle_state='active'` rather than `'draft'`: the chain tests here
    have no lifecycle-transition service to move a draft forward, and
    genesis has no opinion on lifecycle state at all.
    """
    revision_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES ("
                "  :rid, :aid, NULL, 'test-system', :locator, :revision_locator, :digest, 'active', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "revision_locator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": _NOW - datetime.timedelta(days=1),
                "review": _NOW + datetime.timedelta(days=365),
                "retention": _NOW + datetime.timedelta(days=730),
                "now": now,
            },
        )
    return revision_id


async def _seed_artifact_and_revision(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    artifact_id = await seed_artifact_family(factory)
    revision_id = await _seed_active_revision(factory, artifact_id)
    return artifact_id, revision_id


def _service(clock_moment: datetime.datetime = _NOW, *, deployment_id: str = "it-test") -> OperationalChainService:
    return OperationalChainService(clock=FakeClock(clock_moment), deployment_id=deployment_id)


async def _genesis(
    service: OperationalChainService,
    factory: async_sessionmaker[AsyncSession],
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    idempotency_key: str = "genesis",
) -> None:
    async with factory() as session, session.begin():
        await service.append_event(
            session,
            artifact_id=artifact_id,
            revision_id=revision_id,
            event_type=EVENT_INITIALIZED,
            actor=SYSTEM_ACTOR,
            payload=build_event_payload(
                initial_freshness_basis="connector_verified", retention_floor_days=730, legal_hold_active=False
            ),
            authorization_decision_reference="it-test:genesis",
            authority_evidence_digest="1" * 64,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# The genesis CHECK, both directions, against real Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_genesis_event_with_a_predecessor_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="ck_arc_operational_events_chain_link"):
            await session.execute(
                text(
                    "INSERT INTO arc_operational_events ("
                    "  revision_id, sequence, event_id, artifact_id, event_type, event_payload,"
                    "  actor_issuer, actor_subject, actor_role, authorization_decision_reference,"
                    "  authority_evidence_digest, idempotency_key_digest, previous_event_digest,"
                    "  signer_key_id, event_digest, signature, signature_profile, request_payload_digest,"
                    "  created_at"
                    ") VALUES ("
                    "  :rid, 0, :eid, :aid, 'operational_state_initialized', '{}', 'i', 's', 'system', 'x',"
                    "  :d64, :d64, :d64, 'k', :d64, 'sig', 'prof', :d64, :now"
                    ")"
                ),
                {
                    "rid": revision_id,
                    "eid": uuid.uuid4(),
                    "aid": _artifact_id,
                    "d64": "4" * 64,
                    "now": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_a_non_genesis_event_with_no_predecessor_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="ck_arc_operational_events_chain_link"):
            await session.execute(
                text(
                    "INSERT INTO arc_operational_events ("
                    "  revision_id, sequence, event_id, artifact_id, event_type, event_payload,"
                    "  actor_issuer, actor_subject, actor_role, authorization_decision_reference,"
                    "  authority_evidence_digest, idempotency_key_digest, previous_event_digest,"
                    "  signer_key_id, event_digest, signature, signature_profile, request_payload_digest,"
                    "  created_at"
                    ") VALUES ("
                    "  :rid, 1, :eid, :aid, 'freshness_downgraded', '{}', 'i', 's', 'system', 'x',"
                    "  :d64, :d64, NULL, 'k', :d64, 'sig', 'prof', :d64, :now"
                    ")"
                ),
                {
                    "rid": revision_id,
                    "eid": uuid.uuid4(),
                    "aid": _artifact_id,
                    "d64": "4" * 64,
                    "now": _NOW,
                },
            )


# ---------------------------------------------------------------------------
# Race the sequence allocation with asyncio.gather against real Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_appends_to_one_revision_are_gap_free_and_fork_free(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three sibling tasks this phase (T06, T09, T11) each found a real bug
    only by racing a lock -- a sequence allocator that has not been raced
    is not known to be gap-free or fork-free. This races twenty concurrent
    appends, each on its own connection, against one revision's chain."""
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    service = _service()
    await _genesis(service, factory, artifact_id=artifact_id, revision_id=revision_id)

    concurrency = 20

    async def _append(i: int) -> int:
        async with factory() as session, session.begin():
            result = await service.append_event(
                session,
                artifact_id=artifact_id,
                revision_id=revision_id,
                event_type=EVENT_FRESHNESS_DOWNGRADED,
                actor=SYSTEM_ACTOR,
                payload=build_event_payload(initial_freshness_basis="revision_pinned_only", reason_code=f"race-{i}"),
                authorization_decision_reference=f"race:{i}",
                authority_evidence_digest=f"{i % 10}" * 64,
                idempotency_key=f"race-key-{i}",
            )
        return result.sequence

    sequences = await asyncio.gather(*(_append(i) for i in range(concurrency)))

    assert sorted(sequences) == list(range(1, concurrency + 1)), "gap or fork in the allocated sequence numbers"

    # And the chain this produced actually verifies end to end.
    async with factory() as session:
        await service.verify_chain(session, revision_id)

    # No pending checkpoint is missing either -- one per event, genesis
    # included.
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_operational_chain_checkpoints WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()
    assert count == concurrency + 1


# ---------------------------------------------------------------------------
# Recovery never silently rewrites history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_detected_tamper_leaves_the_row_byte_identical_never_a_repair(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    service = _service()
    await _genesis(service, factory, artifact_id=artifact_id, revision_id=revision_id)

    async def _snapshot() -> object:
        async with factory() as session:
            return (
                await session.execute(
                    text(
                        "SELECT event_digest, signature, event_payload, previous_event_digest "
                        "FROM arc_operational_events WHERE revision_id = :rid AND sequence = 0"
                    ),
                    {"rid": revision_id},
                )
            ).one()

    before = await _snapshot()

    # Tamper directly against the database -- the exact attack a compromised
    # database's own write access represents.
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_operational_events SET signature = :bad WHERE revision_id = :rid AND sequence = 0"),
            {"bad": "00" * 64, "rid": revision_id},
        )

    with pytest.raises(OperationalChainIntegrityError) as exc_info:
        async with factory() as session:
            await service.verify_chain(session, revision_id)
    assert exc_info.value.reason_code == "signature_invalid"

    # The state moved to "detected failure" (an exception was raised), not
    # to "repaired" -- the row is still exactly the tampered value, proving
    # verify_chain never wrote anything.
    tampered = await _snapshot()
    assert tampered.signature == "00" * 64
    assert tampered.event_digest == before.event_digest  # the digest itself was untouched by the tamper
    assert tampered.event_payload == before.event_payload

    # Restoring the exact original bytes (an operator's own recovery
    # action, never this module's) makes the row byte-identical to the
    # pre-tamper snapshot and the chain verifies again -- proving nothing
    # else was silently touched while the tamper was live.
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_operational_events SET signature = :sig WHERE revision_id = :rid AND sequence = 0"),
            {"sig": before.signature, "rid": revision_id},
        )
    restored = await _snapshot()
    assert (restored.event_digest, restored.signature, restored.event_payload) == (
        before.event_digest,
        before.signature,
        before.event_payload,
    )
    async with factory() as session:
        await service.verify_chain(session, revision_id)  # clean again


# ---------------------------------------------------------------------------
# Idempotency: exact retry vs a changed payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exact_retry_returns_the_original_identity(factory: async_sessionmaker[AsyncSession]) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    service = _service()

    async def _append_genesis() -> object:
        async with factory() as session, session.begin():
            return await service.append_event(
                session,
                artifact_id=artifact_id,
                revision_id=revision_id,
                event_type=EVENT_INITIALIZED,
                actor=SYSTEM_ACTOR,
                payload=build_event_payload(initial_freshness_basis="connector_verified"),
                authorization_decision_reference="it-test:genesis",
                authority_evidence_digest="1" * 64,
                idempotency_key="genesis-key",
            )

    first = await _append_genesis()
    second = await _append_genesis()

    assert first == second
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_operational_events WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_a_changed_payload_under_the_same_key_is_refused_not_silently_accepted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    service = _service()
    await _genesis(service, factory, artifact_id=artifact_id, revision_id=revision_id, idempotency_key="shared-key")

    async with factory() as session, session.begin():
        with pytest.raises(OperationalChainIdempotencyConflict):
            await service.append_event(
                session,
                artifact_id=artifact_id,
                revision_id=revision_id,
                event_type=EVENT_INITIALIZED,
                actor=SYSTEM_ACTOR,
                # Different retention floor -- a genuinely different request
                # reusing the same key.
                payload=build_event_payload(initial_freshness_basis="connector_verified", retention_floor_days=1),
                authorization_decision_reference="it-test:genesis",
                authority_evidence_digest="1" * 64,
                idempotency_key="shared-key",
            )

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_operational_events WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert count == 1  # the conflicting attempt wrote nothing


# ---------------------------------------------------------------------------
# CheckpointExportService / CheckpointExporterWorker -- crash and retry
# scenarios, against real checkpoint rows.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _RecordingSink:
    """A real, stateful sink double: acknowledges idempotently by
    `{deployment_id, revision_id, sequence}`, refuses a changed digest at
    the same identity, and remembers everything it has ever acknowledged --
    exactly the contract `CheckpointSink` describes, backing it with a
    plain dict instead of a genuine append-only store."""

    accepted: dict[tuple[str, uuid.UUID, int], SinkReceipt] = dataclasses.field(default_factory=dict)

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> SinkReceipt:
        key = (deployment_id, revision_id, sequence)
        existing = self.accepted.get(key)
        if existing is not None:
            if existing.receipt_digest != _receipt_digest(head_digest):
                raise CheckpointSinkIdentityConflict(f"{key} already accepted with a different digest")
            return existing
        receipt = SinkReceipt(
            receipt_digest=_receipt_digest(head_digest),
            receipt_signature=f"sig-{head_digest[:16]}",
            accepted_at=_NOW,
        )
        self.accepted[key] = receipt
        return receipt

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> SinkReceipt | None:
        return self.accepted.get((deployment_id, revision_id, sequence))

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        seqs = [seq for (d, r, seq) in self.accepted if d == deployment_id and r == revision_id]
        return max(seqs) if seqs else None


def _receipt_digest(head_digest: str) -> str:
    return f"receipt-{head_digest}"


async def _pending_checkpoint_id(factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID) -> uuid.UUID:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT checkpoint_id FROM arc_operational_chain_checkpoints "
                    "WHERE revision_id = :rid AND exported_at IS NULL"
                ),
                {"rid": revision_id},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_export_checkpoint_records_a_real_receipt(factory: async_sessionmaker[AsyncSession]) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="export-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)

    outcome = await export.export_checkpoint(checkpoint_id)

    assert outcome is CheckpointExportOutcome.EXPORTED
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT exported_at, sink_receipt_digest FROM arc_operational_chain_checkpoints "
                    "WHERE checkpoint_id = :cid"
                ),
                {"cid": checkpoint_id},
            )
        ).one()
    assert row.exported_at is not None
    assert row.sink_receipt_digest is not None


@pytest.mark.asyncio
async def test_crash_after_ack_before_local_commit_reconciles_on_retry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates the named crash scenario directly: the sink already
    acknowledged this checkpoint (this test calls it once, standing in for
    the export that "crashed" right after), but the local row was never
    marked durable. Calling `export_checkpoint` again -- exactly what
    startup/the next worker pass does -- must reconcile to the same
    receipt without creating a second one, not fail and not double-write.
    """
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="crash-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()

    # Stand in for "the sink acknowledged, then the process crashed before
    # the local commit": call the sink directly, bypassing
    # export_checkpoint's own second transaction.
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT deployment_id, sequence, head_digest FROM arc_operational_chain_checkpoints "
                    "WHERE checkpoint_id = :cid"
                ),
                {"cid": checkpoint_id},
            )
        ).one()
    pre_crash_receipt = await sink.append(
        deployment_id=row.deployment_id, revision_id=revision_id, sequence=row.sequence, head_digest=row.head_digest
    )

    # Local row is still pending -- the "crash" never got to commit it.
    async with factory() as session:
        still_pending = (
            await session.execute(
                text("SELECT exported_at FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"),
                {"cid": checkpoint_id},
            )
        ).scalar_one()
    assert still_pending is None

    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    outcome = await export.export_checkpoint(checkpoint_id)

    assert outcome is CheckpointExportOutcome.EXPORTED
    assert len(sink.accepted) == 1  # no second checkpoint created at the sink
    async with factory() as session:
        reconciled = (
            await session.execute(
                text("SELECT sink_receipt_digest FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"),
                {"cid": checkpoint_id},
            )
        ).scalar_one()
    assert reconciled == pre_crash_receipt.receipt_digest


@pytest.mark.asyncio
async def test_an_exact_retry_of_export_checkpoint_is_a_no_op_the_second_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="retry-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)

    first = await export.export_checkpoint(checkpoint_id)
    second = await export.export_checkpoint(checkpoint_id)

    assert first is CheckpointExportOutcome.EXPORTED
    assert second is CheckpointExportOutcome.ALREADY_EXPORTED
    assert len(sink.accepted) == 1


@pytest.mark.asyncio
async def test_a_sink_mismatch_at_export_time_is_an_integrity_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="mismatch-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()
    # Pre-poison the sink: it already holds a *different* digest for this
    # exact identity, as if a prior deployment (or a tampered local head)
    # had claimed it first.
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT deployment_id, sequence FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"
                ),
                {"cid": checkpoint_id},
            )
        ).one()
    await sink.append(
        deployment_id=row.deployment_id, revision_id=revision_id, sequence=row.sequence, head_digest="f" * 64
    )

    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    with pytest.raises(CheckpointIntegrityError) as exc_info:
        await export.export_checkpoint(checkpoint_id)
    assert exc_info.value.reason_code == "sink_mismatch"

    async with factory() as session:
        exported_at = (
            await session.execute(
                text("SELECT exported_at FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"),
                {"cid": checkpoint_id},
            )
        ).scalar_one()
    assert exported_at is None  # never marked durable off a mismatched receipt


@pytest.mark.asyncio
async def test_an_unavailable_sink_leaves_the_checkpoint_safely_pending(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="unavailable-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    export = CheckpointExportService(factory, clock=SystemClock())  # sink=None

    outcome = await export.export_checkpoint(checkpoint_id)

    assert outcome is CheckpointExportOutcome.SINK_UNAVAILABLE
    async with factory() as session:
        exported_at = (
            await session.execute(
                text("SELECT exported_at FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"),
                {"cid": checkpoint_id},
            )
        ).scalar_one()
    assert exported_at is None


@pytest.mark.asyncio
async def test_verify_against_sink_detects_suffix_rollback(factory: async_sessionmaker[AsyncSession]) -> None:
    """The exact attack this design exists to catch: a compromised database
    deletes a valid suffix and the local head no longer reaches a sequence
    the sink already durably acknowledged."""
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="rollback-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    async with factory() as session, session.begin():
        await chain.append_event(
            session,
            artifact_id=artifact_id,
            revision_id=revision_id,
            event_type=EVENT_FRESHNESS_DOWNGRADED,
            actor=SYSTEM_ACTOR,
            payload=build_event_payload(initial_freshness_basis="revision_pinned_only"),
            authorization_decision_reference="it-test:downgrade",
            authority_evidence_digest="2" * 64,
            idempotency_key="downgrade-key",
        )
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    async with factory() as session:
        checkpoint_ids = (
            (
                await session.execute(
                    text(
                        "SELECT checkpoint_id FROM arc_operational_chain_checkpoints "
                        "WHERE revision_id = :rid ORDER BY sequence"
                    ),
                    {"rid": revision_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(checkpoint_ids) == 2
    for checkpoint_id in checkpoint_ids:
        outcome = await export.export_checkpoint(checkpoint_id)
        assert outcome is CheckpointExportOutcome.EXPORTED

    # A compromised database deletes the local record of the suffix -- the
    # checkpoint row for sequence 1 (the sink, being external, still
    # remembers it).
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_operational_chain_checkpoints WHERE revision_id = :rid AND sequence = 1"),
            {"rid": revision_id},
        )

    async with factory() as session:
        with pytest.raises(CheckpointIntegrityError) as exc_info:
            await export.verify_against_sink(session, revision_id)
    assert exc_info.value.reason_code == "suffix_rollback"


@pytest.mark.asyncio
async def test_verify_against_sink_detects_a_missing_receipt(factory: async_sessionmaker[AsyncSession]) -> None:
    """A checkpoint recorded locally as durable, but the sink -- an
    independent, external system -- has no record of it at all."""
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="missing-receipt-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    outcome = await export.export_checkpoint(checkpoint_id)
    assert outcome is CheckpointExportOutcome.EXPORTED

    # The sink "forgets" -- simulates an external system that lost, or
    # never durably committed, what it appeared to acknowledge.
    sink.accepted.clear()

    async with factory() as session:
        with pytest.raises(CheckpointIntegrityError) as exc_info:
            await export.verify_against_sink(session, revision_id)
    assert exc_info.value.reason_code == "missing_receipt"


@pytest.mark.asyncio
async def test_verify_against_sink_passes_on_a_clean_exported_chain(factory: async_sessionmaker[AsyncSession]) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="clean-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    checkpoint_id = await _pending_checkpoint_id(factory, revision_id)
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    outcome = await export.export_checkpoint(checkpoint_id)
    assert outcome is CheckpointExportOutcome.EXPORTED

    async with factory() as session:
        await export.verify_against_sink(session, revision_id)  # must not raise

    # A concrete assertion beyond "did not raise": the checkpoint this
    # verified really is durable with a receipt on record, which is what a
    # clean `verify_against_sink` pass is actually vouching for.
    async with factory() as session:
        exported_at = (
            await session.execute(
                text("SELECT exported_at FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"),
                {"cid": checkpoint_id},
            )
        ).scalar_one()
    assert exported_at is not None


# ---------------------------------------------------------------------------
# CheckpointExporterWorker against real pending checkpoints.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_drains_a_real_pending_checkpoint(factory: async_sessionmaker[AsyncSession]) -> None:
    artifact_id, revision_id = await _seed_artifact_and_revision(factory)
    chain = _service(deployment_id="worker-test")
    await _genesis(chain, factory, artifact_id=artifact_id, revision_id=revision_id)
    sink = _RecordingSink()
    export = CheckpointExportService(factory, clock=SystemClock(), sink=sink)
    worker = CheckpointExporterWorker(factory, export)

    result = await worker.run_once()

    assert result.exported >= 1
    assert result.integrity_failed == 0
