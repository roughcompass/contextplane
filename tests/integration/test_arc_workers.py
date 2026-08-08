"""ARC background workers: audit-outbox drain, challenge cleanup, review expiry.

The shared Postgres container backing these tests is session-scoped and
never rolled back (see `tests/conftest.py`), so other ARC integration test
files leave rows behind in the same tables these workers sweep. Two
patterns keep assertions honest under that sharing:

- Scope by the specific id a test created (`outbox_id`, `challenge_id`,
  `revision_id`) rather than trusting a global row count. A leftover row
  from another file cannot make an id-scoped assertion pass or fail
  incorrectly.
- Where an exact aggregate count is the point of the test (batch-size
  bounding), give the rows this test creates a timestamp far enough in the
  past that nothing else in the suite could plausibly compete with them
  for the same claim order, rather than trying to flush or lock out
  everything else in the table first.

The audit-drain batch-size test is the one exception: `created_at` on
`arc_audit_outbox` is a database-assigned default with no test hook to
override it, so there is no timestamp to pin. That test instead drains the
table to empty before seeding its own rows -- draining is idempotent and
never destructive (it only moves already-committed facts into
`audit_log`, which is where they belong regardless of which test put them
there), so doing it first is safe rather than a workaround.
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

from registry.arc.models import DEPLOYMENT_TENANT_ID
from registry.arc.service import audit_outbox
from registry.arc.service.artifact import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_EXPIRED,
    OBLIGATION_MISSING_REVIEW_EXPIRED,
    OBLIGATION_SATISFIED,
)
from registry.arc.workers.audit_drain import AUDIT_LOG_TARGET_TYPE, AuditDrainWorker, AuditSinkError, DrainResult
from registry.arc.workers.challenge_cleanup import RETENTION_AFTER_EXPIRY, ChallengeCleanupWorker
from registry.arc.workers.review_expiry import ReviewExpiryWorker
from registry.audit import actions
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc, seed_challenge
from tests.helpers.clock import FakeClock

# A pair far enough in the past that no other test's fixture data (all
# clustered around ARC_NOW, 2026-01-01) could ever sort ahead of it in an
# `ORDER BY ... ASC LIMIT n` claim query. Two constants, not one, because
# `arc_context_challenges` CHECKs `expires_at > issued_at`; a batch test
# that only rewrites `expires_at` while the row's real `issued_at` (set at
# seed time, near ARC_NOW) stays put would need `expires_at` in the future
# relative to it, which defeats the point of predating everything else.
_ANCIENT_ISSUED = datetime.datetime(2, 1, 1, tzinfo=datetime.UTC)
_ANCIENT_EXPIRES = datetime.datetime(2, 1, 2, tzinfo=datetime.UTC)

# `arc_revisions.review_expires_at` carries no such CHECK, so one constant
# is enough there.
_ANCIENT_REVIEW = datetime.datetime(1, 1, 1, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-workers")


# ---------------------------------------------------------------------------
# AuditDrainWorker
# ---------------------------------------------------------------------------


class _AlwaysFailsSink:
    """A sink that never lands a row -- the only honest way to test retry.

    No legitimate outbox row makes the real Postgres sink fail (payload
    size is already bounded at emit time, every other column is
    worker-controlled), so exercising the failure path means substituting
    a sink that fails on command rather than fabricating a constraint
    violation the schema never produces.
    """

    def __init__(self, code: str = "boom_test_code") -> None:
        self._code = code

    async def write(self, session: AsyncSession, **kwargs: Any) -> None:
        raise AuditSinkError(self._code)


async def _drain_everything(factory: async_sessionmaker[AsyncSession]) -> None:
    """Drain every undrained outbox row so a batch-count test starts from zero.

    Safe regardless of which other test left rows behind: draining is
    exactly what is supposed to eventually happen to them.
    """
    worker = AuditDrainWorker(factory, FakeClock(ARC_NOW), limit=10_000)
    while (await worker.run_once()).claimed:
        pass


async def _outbox_row(factory: async_sessionmaker[AsyncSession], outbox_id: uuid.UUID) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT drained_at, attempts, last_error_code, last_attempt_at "
                    "FROM arc_audit_outbox WHERE outbox_id = :id"
                ),
                {"id": outbox_id},
            )
        ).one()


async def _audit_log_rows(factory: async_sessionmaker[AsyncSession], audit_id: uuid.UUID) -> list[Any]:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT tenant_id, action, target_type, target_id, after_jsonb "
                    "FROM audit_log WHERE audit_id = :id"
                ),
                {"id": audit_id},
            )
        ).all()


@pytest.mark.asyncio
async def test_a_pending_outbox_row_is_drained_into_audit_log(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type=actions.ARC_CHALLENGE_ISSUED, payload={"marker": marker}
        )

    # Drained in passes rather than one, because the worker claims a bounded
    # batch. This suite shares one database, so by the time this test runs the
    # outbox may already hold more pending rows than a single pass can claim,
    # and the row emitted above would sit behind them. Asserting on one pass
    # made this test a function of how many rows every earlier test happened to
    # leave behind -- green when run alone, red in the full suite. The property
    # is that a pending row is drained, not that the first pass reaches it.
    worker = AuditDrainWorker(factory, FakeClock(ARC_NOW))
    outbox_row = None
    for _ in range(20):
        result = await worker.run_once()
        assert result.failed == 0
        outbox_row = await _outbox_row(factory, outbox_id)
        if outbox_row.drained_at is not None:
            break
        if result.drained == 0:
            break  # nothing left to claim, so another pass cannot help
    assert outbox_row is not None
    assert outbox_row.drained_at is not None
    assert outbox_row.last_error_code is None

    audit_rows = await _audit_log_rows(factory, outbox_id)
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row.tenant_id == seed.tenant_id
    assert row.action == actions.ARC_CHALLENGE_ISSUED
    assert row.target_type == AUDIT_LOG_TARGET_TYPE
    assert row.target_id == outbox_id
    assert row.after_jsonb == {"marker": marker}


@pytest.mark.asyncio
async def test_redraining_an_already_landed_row_does_not_duplicate_the_audit_row(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Simulates a crash after the `audit_log` insert lands but before
    `drained_at` is recorded: resetting it to NULL and draining again must
    not create a second `audit_log` row for the same event."""
    await _drain_everything(factory)
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type=actions.ARC_CONTEXT_RESOLVED, payload={"marker": marker}
        )

    worker = AuditDrainWorker(factory, FakeClock(ARC_NOW))
    first = await worker.run_once()
    assert first.drained == 1

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_audit_outbox SET drained_at = NULL WHERE outbox_id = :id"), {"id": outbox_id}
        )

    second = await worker.run_once()
    assert second.drained == 1
    assert second.failed == 0

    assert len(await _audit_log_rows(factory, outbox_id)) == 1


@pytest.mark.asyncio
async def test_a_failing_sink_leaves_the_row_retryable_with_a_bounded_code(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _drain_everything(factory)
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CONTEXT_BLOCKED,
            payload={"marker": uuid.uuid4().hex},
        )

    result = await AuditDrainWorker(factory, FakeClock(ARC_NOW), sink=_AlwaysFailsSink()).run_once()

    assert result.drained == 0
    assert result.failed == 1
    outbox_row = await _outbox_row(factory, outbox_id)
    assert outbox_row.drained_at is None
    assert outbox_row.attempts == 1
    assert outbox_row.last_error_code == "boom_test_code"
    assert outbox_row.last_attempt_at is not None
    assert await _audit_log_rows(factory, outbox_id) == []


@pytest.mark.asyncio
async def test_a_row_past_the_attempt_ceiling_drops_out_of_the_drain_query(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _drain_everything(factory)
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CONTEXT_DEGRADED,
            payload={"marker": uuid.uuid4().hex},
        )

    worker = AuditDrainWorker(factory, FakeClock(ARC_NOW), max_attempts=2, sink=_AlwaysFailsSink())
    await worker.run_once()
    await worker.run_once()

    outbox_row = await _outbox_row(factory, outbox_id)
    assert outbox_row.attempts == 2
    assert outbox_row.drained_at is None

    third = await worker.run_once()
    assert third.claimed == 0

    # Untouched by the pass that no longer claims it -- still exactly at
    # the ceiling, not incremented further.
    outbox_row_after = await _outbox_row(factory, outbox_id)
    assert outbox_row_after.attempts == 2


@pytest.mark.asyncio
async def test_a_locked_row_is_skipped_rather_than_claimed_twice(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Two drain passes must never work the same row. Holding the row's
    lock open in one session and running a pass from another proves the
    claim really is exclusive, not just eventually consistent."""
    await _drain_everything(factory)
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type=actions.ARC_JIT_GRANTED, payload={"marker": uuid.uuid4().hex}
        )

    async with factory() as holder, holder.begin():
        await holder.execute(
            text("SELECT outbox_id FROM arc_audit_outbox WHERE outbox_id = :id FOR UPDATE"), {"id": outbox_id}
        )
        result = await AuditDrainWorker(factory, FakeClock(ARC_NOW)).run_once()
        assert result.claimed == 0

    # The holder's transaction has ended; the row is claimable again.
    result_after = await AuditDrainWorker(factory, FakeClock(ARC_NOW)).run_once()
    assert result_after.drained == 1


@pytest.mark.asyncio
async def test_one_pass_claims_at_most_the_configured_limit(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _drain_everything(factory)
    async with factory() as session, session.begin():
        for _ in range(3):
            await audit_outbox.emit(
                session,
                tenant_id=seed.tenant_id,
                event_type=actions.ARC_CONTEXT_RESOLVED,
                payload={"marker": uuid.uuid4().hex},
            )

    worker = AuditDrainWorker(factory, FakeClock(ARC_NOW), limit=2)
    assert (await worker.run_once()).claimed == 2
    assert (await worker.run_once()).claimed == 1
    assert (await worker.run_once()).claimed == 0


@pytest.mark.asyncio
async def test_running_with_nothing_pending_is_a_no_op(factory: async_sessionmaker[AsyncSession]) -> None:
    await _drain_everything(factory)
    assert await AuditDrainWorker(factory, FakeClock(ARC_NOW)).run_once() == DrainResult(claimed=0, drained=0, failed=0)


# ---------------------------------------------------------------------------
# ChallengeCleanupWorker
# ---------------------------------------------------------------------------


async def _challenge_exists(factory: async_sessionmaker[AsyncSession], challenge_id: uuid.UUID) -> bool:
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT 1 FROM arc_context_challenges WHERE challenge_id = :cid"), {"cid": challenge_id}
            )
        ).one_or_none()
    return row is not None


async def _backdate_challenge(
    factory: async_sessionmaker[AsyncSession],
    challenge_id: uuid.UUID,
    *,
    issued_at: datetime.datetime,
    expires_at: datetime.datetime,
) -> None:
    """Rewrite both timestamps together so `expires_at > issued_at` still holds."""
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_context_challenges SET issued_at = :issued, expires_at = :expires "
                "WHERE challenge_id = :cid"
            ),
            {"issued": issued_at, "expires": expires_at, "cid": challenge_id},
        )


async def _insert_stub_receipt(
    session: AsyncSession, *, challenge_id: uuid.UUID, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """A minimal, schema-valid `arc_receipts` row.

    The deferred challenge-consumption trigger requires exactly one receipt
    referencing a challenge before a transaction that consumes it can
    commit, so proving "consumed" without one is not possible -- mirrors
    the identical stub used in `test_arc_challenge_consume.py`.
    """
    await session.execute(
        text(
            "INSERT INTO arc_receipts ("
            "  receipt_id, challenge_id, tenant_id, actor_id, host_id, session_id,"
            "  manifest_fingerprint, attestation_id, resolution_status,"
            "  selection_engine_version, registry_build_revision, canonical_profile_versions,"
            "  selection_config_digest, evaluated_at, freshness_basis, budget_limit_bytes,"
            "  response_replay_ciphertext, response_replay_nonce, response_replay_key_id"
            ") VALUES ("
            "  :receipt_id, :challenge_id, :tenant_id, :actor_id, 'host-1', 'sess-1',"
            "  :fingerprint, :attestation_id, 'ready',"
            "  'test-engine', 'test-build', '{}',"
            "  :config_digest, :evaluated_at, 'revision_pinned_only', 12288,"
            "  :ciphertext, :nonce, 'test-key'"
            ")"
        ),
        {
            "receipt_id": uuid.uuid4(),
            "challenge_id": challenge_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "fingerprint": "f" * 64,
            "attestation_id": f"att-{challenge_id}",
            "config_digest": "c" * 64,
            "evaluated_at": ARC_NOW,
            "ciphertext": b"stub-ciphertext",
            "nonce": b"stub-nonce-12",
        },
    )


@pytest.mark.asyncio
async def test_an_unconsumed_challenge_expired_past_the_retention_window_is_deleted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    clock = FakeClock(ARC_NOW + RETENTION_AFTER_EXPIRY + datetime.timedelta(hours=1))

    result = await ChallengeCleanupWorker(factory, clock).run_once()

    assert result.deleted >= 1
    assert not await _challenge_exists(factory, challenge_id)


@pytest.mark.asyncio
async def test_an_unconsumed_challenge_within_the_retention_window_is_not_yet_deleted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    # Past its five-minute TTL, so validate_challenge would already refuse
    # it -- but nowhere near the retention window this worker enforces.
    clock = FakeClock(ARC_NOW + datetime.timedelta(minutes=30))

    await ChallengeCleanupWorker(factory, clock).run_once()

    assert await _challenge_exists(factory, challenge_id)


@pytest.mark.asyncio
async def test_a_consumed_challenge_is_never_deleted_even_once_expired(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    async with factory() as session, session.begin():
        await _insert_stub_receipt(session, challenge_id=challenge_id, tenant_id=seed.tenant_id, actor_id=seed.actor_id)
        await session.execute(
            text("UPDATE arc_context_challenges SET consumed_at = :now WHERE challenge_id = :cid"),
            {"now": ARC_NOW, "cid": challenge_id},
        )
    clock = FakeClock(ARC_NOW + RETENTION_AFTER_EXPIRY + datetime.timedelta(hours=1))

    await ChallengeCleanupWorker(factory, clock).run_once()

    assert await _challenge_exists(factory, challenge_id)


@pytest.mark.asyncio
async def test_an_unexpired_challenge_is_untouched(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    await ChallengeCleanupWorker(factory, FakeClock(ARC_NOW)).run_once()

    assert await _challenge_exists(factory, challenge_id)


@pytest.mark.asyncio
async def test_one_pass_deletes_at_most_the_configured_limit(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    challenge_ids = [await seed_challenge(factory, tenant_id=seed.tenant_id) for _ in range(3)]
    for cid in challenge_ids:
        await _backdate_challenge(factory, cid, issued_at=_ANCIENT_ISSUED, expires_at=_ANCIENT_EXPIRES)

    worker = ChallengeCleanupWorker(factory, FakeClock(ARC_NOW), limit=2)
    assert (await worker.run_once()).deleted == 2
    assert (await worker.run_once()).deleted == 1
    assert (await worker.run_once()).deleted == 0

    for cid in challenge_ids:
        assert not await _challenge_exists(factory, cid)


async def _outbox_ids_for_challenge_expired(factory: async_sessionmaker[AsyncSession]) -> set[uuid.UUID]:
    """Every undrained-or-drained outbox row's id for the purge-summary event.

    Snapshotting the id set before and after one `run_once()` call is how
    these tests isolate "what did this call write" from whatever other
    tests in this session-scoped suite already purged and audited -- the
    payload here carries a count and a cutoff, not a challenge id, so there
    is no per-row key to filter on the way the issuance tests filter by
    `challenge_id`.
    """
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT outbox_id FROM arc_audit_outbox WHERE event_type = :etype"),
                {"etype": actions.ARC_CHALLENGE_EXPIRED},
            )
        ).all()
    return {row.outbox_id for row in rows}


async def _challenge_expired_outbox_row(factory: async_sessionmaker[AsyncSession], outbox_id: uuid.UUID) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT tenant_id, event_payload FROM arc_audit_outbox WHERE outbox_id = :oid"),
                {"oid": outbox_id},
            )
        ).one()


@pytest.mark.asyncio
async def test_a_purge_that_deletes_rows_emits_one_summary_audit_row(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    challenge_ids = [await seed_challenge(factory, tenant_id=seed.tenant_id) for _ in range(3)]
    for cid in challenge_ids:
        await _backdate_challenge(factory, cid, issued_at=_ANCIENT_ISSUED, expires_at=_ANCIENT_EXPIRES)

    before = await _outbox_ids_for_challenge_expired(factory)
    clock = FakeClock(ARC_NOW + RETENTION_AFTER_EXPIRY + datetime.timedelta(hours=1))
    result = await ChallengeCleanupWorker(factory, clock).run_once()
    after = await _outbox_ids_for_challenge_expired(factory)

    new_rows = after - before
    assert len(new_rows) == 1, f"expected exactly one new purge-summary row, found {len(new_rows)}"

    row = await _challenge_expired_outbox_row(factory, new_rows.pop())
    assert row.tenant_id == DEPLOYMENT_TENANT_ID
    assert row.event_payload["deleted_count"] == result.deleted
    assert row.event_payload["deleted_count"] >= 3
    assert row.event_payload["cutoff"] == (clock.now() - RETENTION_AFTER_EXPIRY).isoformat()


@pytest.mark.asyncio
async def test_a_purge_that_deletes_nothing_emits_no_audit_row(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    # An unexpired challenge is never a candidate, so this pass has nothing
    # to delete and nothing to audit.
    await seed_challenge(factory, tenant_id=seed.tenant_id)

    before = await _outbox_ids_for_challenge_expired(factory)
    result = await ChallengeCleanupWorker(factory, FakeClock(ARC_NOW)).run_once()
    after = await _outbox_ids_for_challenge_expired(factory)

    assert result.deleted == 0
    assert after == before


# ---------------------------------------------------------------------------
# ReviewExpiryWorker
# ---------------------------------------------------------------------------


async def _set_review_expires_at(
    factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID, review_expires_at: datetime.datetime
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET review_expires_at = :exp WHERE revision_id = :rid"),
            {"exp": review_expires_at, "rid": revision_id},
        )


async def _revision_row(factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT lifecycle_state, tenant_id FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).one()


async def _insert_obligation(
    factory: async_sessionmaker[AsyncSession],
    *,
    artifact_id: uuid.UUID,
    directive_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> uuid.UUID:
    obligation_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_mandatory_obligations ("
                "  obligation_id, artifact_id, directive_id, current_revision_id, applicability_snapshot,"
                "  applicability_digest, obligation_state, effective_from, updated_at"
                ") VALUES (:oid, :aid, :did, :rid, CAST(:snap AS JSONB), :digest, :state, :efrom, :now)"
            ),
            {
                "oid": obligation_id,
                "aid": artifact_id,
                "did": directive_id,
                "rid": revision_id,
                "snap": "{}",
                "digest": "d" * 64,
                "state": OBLIGATION_SATISFIED,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "now": ARC_NOW,
            },
        )
    return obligation_id


async def _obligation_row(factory: async_sessionmaker[AsyncSession], obligation_id: uuid.UUID) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT obligation_state, current_revision_id FROM arc_mandatory_obligations "
                    "WHERE obligation_id = :oid"
                ),
                {"oid": obligation_id},
            )
        ).one()


async def _seed_global_revision(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A minimal global (`tenant_id IS NULL`) artifact + active revision.

    `seed_arc` only ever creates tenant-scoped artifacts; the
    deployment-attribution test needs one with no tenant to prove the
    global fallback actually runs.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, NULL, :slug, 'policy', :title, :now, :issuer, :subject)"
            ),
            {
                "aid": artifact_id,
                "slug": f"global-{artifact_id.hex[:8]}",
                "title": f"Test artifact {artifact_id.hex[:8]}",
                "now": ARC_NOW,
                "issuer": "https://idp.example.test",
                "subject": "seed-actor",
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES ("
                "  :rid, :aid, NULL, 'test-system', :locator, :rlocator, :digest, 'active', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "rlocator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
                "now": ARC_NOW,
            },
        )
    return revision_id


async def _outbox_events_for_revision(factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID) -> list[Any]:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT tenant_id, event_payload FROM arc_audit_outbox "
                    "WHERE event_type = :etype AND event_payload ->> 'revision_id' = :rid"
                ),
                {"etype": actions.ARC_REVIEW_EXPIRED, "rid": str(revision_id)},
            )
        ).all()


@pytest.mark.asyncio
async def test_an_active_revision_past_its_review_date_is_expired(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _set_review_expires_at(factory, seed.revision_id, ARC_NOW - datetime.timedelta(days=1))

    await ReviewExpiryWorker(factory, FakeClock(ARC_NOW)).run_once()

    row = await _revision_row(factory, seed.revision_id)
    assert row.lifecycle_state == LIFECYCLE_EXPIRED


@pytest.mark.asyncio
async def test_a_non_expired_active_revision_is_untouched(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    # seed_arc's revision already carries a review date far in the future.
    await ReviewExpiryWorker(factory, FakeClock(ARC_NOW)).run_once()

    row = await _revision_row(factory, seed.revision_id)
    assert row.lifecycle_state == LIFECYCLE_ACTIVE


@pytest.mark.asyncio
async def test_a_mandatory_obligation_on_an_expired_revision_is_tombstoned(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    obligation_id = await _insert_obligation(
        factory, artifact_id=seed.artifact_id, directive_id=seed.directive_id, revision_id=seed.revision_id
    )
    await _set_review_expires_at(factory, seed.revision_id, ARC_NOW - datetime.timedelta(days=1))

    await ReviewExpiryWorker(factory, FakeClock(ARC_NOW)).run_once()

    obligation = await _obligation_row(factory, obligation_id)
    assert obligation.obligation_state == OBLIGATION_MISSING_REVIEW_EXPIRED
    assert obligation.current_revision_id is None


@pytest.mark.asyncio
async def test_expiring_a_revision_emits_one_audit_outbox_row(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _set_review_expires_at(factory, seed.revision_id, ARC_NOW - datetime.timedelta(days=1))

    await ReviewExpiryWorker(factory, FakeClock(ARC_NOW)).run_once()

    rows = await _outbox_events_for_revision(factory, seed.revision_id)
    assert len(rows) == 1
    assert rows[0].tenant_id == seed.tenant_id


@pytest.mark.asyncio
async def test_a_global_revisions_expiry_attributes_to_the_deployment_tenant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    revision_id = await _seed_global_revision(factory)
    await _set_review_expires_at(factory, revision_id, ARC_NOW - datetime.timedelta(days=1))

    await ReviewExpiryWorker(factory, FakeClock(ARC_NOW)).run_once()

    rows = await _outbox_events_for_revision(factory, revision_id)
    assert len(rows) == 1
    assert rows[0].tenant_id == DEPLOYMENT_TENANT_ID


@pytest.mark.asyncio
async def test_running_twice_does_not_reexpire_or_reemit(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    await _set_review_expires_at(factory, seed.revision_id, ARC_NOW - datetime.timedelta(days=1))
    worker = ReviewExpiryWorker(factory, FakeClock(ARC_NOW))

    await worker.run_once()
    await worker.run_once()

    row = await _revision_row(factory, seed.revision_id)
    assert row.lifecycle_state == LIFECYCLE_EXPIRED
    assert len(await _outbox_events_for_revision(factory, seed.revision_id)) == 1


@pytest.mark.asyncio
async def test_one_pass_expires_at_most_the_configured_limit(factory: async_sessionmaker[AsyncSession]) -> None:
    seeds = [await seed_arc(factory, slug_prefix=f"arc-review-limit-{i}") for i in range(3)]
    for one_seed in seeds:
        await _set_review_expires_at(factory, one_seed.revision_id, _ANCIENT_REVIEW)

    worker = ReviewExpiryWorker(factory, FakeClock(ARC_NOW), limit=2)
    assert (await worker.run_once()).expired_revisions == 2
    assert (await worker.run_once()).expired_revisions == 1
    assert (await worker.run_once()).expired_revisions == 0

    for one_seed in seeds:
        row = await _revision_row(factory, one_seed.revision_id)
        assert row.lifecycle_state == LIFECYCLE_EXPIRED
