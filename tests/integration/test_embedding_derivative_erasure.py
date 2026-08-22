"""Erasing a claim's retrieval artefacts end to end, against a real database.

The property under test is one sentence and it is the reason the registry exists:
**deleting every registered derivative for a source leaves no vector, no chunk text,
no full-text material, and no pending or dead-lettered `embedding_outbox` row derived
from that source.** Everything here is arranged to give that sentence a chance to be
false.

Three arrangements matter and each closes a different hole:

- **A drained target.** The claim is projected, the drain embeds it, and the vector
  row exists with `text_chunk` holding the claim's own words. This is the copy the
  semantic arm serves.
- **A planted pending request.** After the drain, a fresh request is enqueued for the
  same claim — a re-projection that has not been embedded yet. It holds `text_to_embed`
  verbatim, so a propagation that only reached `embeddings` would leave the claim's
  text sitting in a queue, waiting to be turned back into a vector.
- **A planted dead-letter.** A request that failed to embed still holds the text it
  failed to embed, and nothing drains that table.

The propagation work is scheduled through the production enqueue helper rather than
inserted by hand, so the path under test is the whole one: the source link the
registrar wrote is what the enqueue follows to find the artefact at all, and a
registration that named the wrong record class would produce an empty queue here
instead of a passing test.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.config import Settings
from contextplane.embedding.stub import StubEmbedder
from contextplane.embedding.targets import TARGET_CLAIM
from contextplane.retention import derivatives, policies
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.retrieval.derivative_handlers import retrieval_derivative_handlers
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.service.retrieval.embedding_index import (
    artefact_locator,
    enqueue,
    index_text,
    project_claim,
)
from contextplane.types import TenantContext
from contextplane.workers.derivative_propagation import DerivativePropagationWorker
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_CLAIM_TEXT_MARKER = "the words that must not survive"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Corpus:
    """One tenant, one actor, and the claims planted under them."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
        self.factory = factory
        self.tenant_id = tenant_id

    async def _exec(self, sql: str, params: dict[str, Any]) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(text(sql), params)

    async def actor(self) -> uuid.UUID:
        aid = uuid.uuid4()
        await self._exec(
            "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
            "VALUES (:aid, :tid, 'a', :sub, :now)",
            {"aid": aid, "tid": self.tenant_id, "sub": f"dh-{aid.hex[:12]}", "now": _NOW},
        )
        return aid

    async def entity(self) -> uuid.UUID:
        eid = uuid.uuid4()
        await self._exec(
            "INSERT INTO entities (entity_id, tenant_id, entity_type, name) "
            "VALUES (:eid, :tid, 'capability', :name)",
            {"eid": eid, "tid": self.tenant_id, "name": f"cap-{eid.hex[:8]}"},
        )
        return eid

    async def claim(self, author: uuid.UUID, subject: uuid.UUID, *, value: str) -> uuid.UUID:
        """A consolidated, staged claim — the shape `project_claim` considers servable."""
        cid = uuid.uuid4()
        await self._exec(
            "INSERT INTO memory_claims ("
            "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id,"
            "  subject_entity_id, subject_reference, predicate, value_type,"
            "  claim_category, value_jsonb, asserted_valid_from, status,"
            "  visibility, source_authority, size_bytes, namespace, consolidated_at,"
            "  confidence, confidence_scored_at, confidence_inputs, scorer_version,"
            "  calibration_version, decay_half_life_days, strategy_id, created_at"
            ") VALUES ("
            "  :cid, :tid, :tid, :author, :subject, 'subject-ref', 'observed_behavior',"
            "  'prose', 'capability_observation', CAST(:val AS JSONB), :now, 'staged',"
            "  'private', 'observer_extraction', 9, :ns, :now,"
            "  0.700, :now, CAST(:conf_in AS JSONB), 'seed-1',"
            "  'uncalibrated', 90, 'seed_strategy', :now"
            ")",
            {
                "cid": cid,
                "tid": self.tenant_id,
                "author": author,
                "subject": subject,
                "val": json.dumps(value),
                "ns": "observation/test",
                "conf_in": json.dumps({"seed": True}),
                "now": _NOW,
            },
        )
        return cid

    async def count(self, sql: str, params: dict[str, Any]) -> int:
        async with self.factory() as session:
            return int((await session.execute(text(sql), params)).scalar_one())


@pytest_asyncio.fixture
async def corpus(factory: async_sessionmaker[AsyncSession]) -> _Corpus:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"dh-{tid.hex[:12]}", "now": _NOW},
        )
    return _Corpus(factory, tid)


# ---------------------------------------------------------------------------
# Arrangement helpers
# ---------------------------------------------------------------------------


async def _project_and_drain(corpus: _Corpus, claim_id: uuid.UUID, settings: Settings) -> None:
    """Queue the claim, then let the real drain turn it into a vector."""
    async with corpus.factory() as session, session.begin():
        assert await project_claim(session, claim_id=claim_id, now=_NOW) is True
    await drain_outbox(corpus.factory, StubEmbedder(), settings)


async def _authorise_erasure(corpus: _Corpus, claim_id: uuid.UUID, actor_id: uuid.UUID) -> uuid.UUID:
    """Write the policy row and the tombstone an erasure-triggered work item must name."""
    tombstone_id = uuid.uuid4()
    disposition = policies.disposition(policies.RECORD_MEMORY_CLAIM)
    async with corpus.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO retention_policies "
                "  (policy_version, record_class, legal_basis, retention_days, erasure_mode, "
                "   minimization_action, tombstone_behaviour, verifier_disclosure, created_at) "
                "VALUES (:ver, :cls, :basis, :days, :mode, :action, :tomb, :disclosure, :now) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "ver": policies.POLICY_VERSION,
                "cls": policies.RECORD_MEMORY_CLAIM,
                "basis": disposition.legal_basis,
                "days": disposition.retention_days,
                "mode": disposition.erasure_mode,
                "action": disposition.minimization_action,
                "tomb": disposition.tombstone_behaviour,
                "disclosure": disposition.verifier_disclosure,
                "now": _NOW,
            },
        )
        await session.execute(
            text(
                "INSERT INTO source_tombstones "
                "  (tombstone_id, tenant_id, record_class, subject_id, policy_version, "
                "   request_authority, reason, effective_at, proof_hmac, propagation_state) "
                "VALUES (:id, :tid, :cls, :subject, :ver, :authority, 'erasure', :now, :proof, 'pending')"
            ),
            {
                "id": tombstone_id,
                "tid": corpus.tenant_id,
                "cls": policies.RECORD_MEMORY_CLAIM,
                "subject": claim_id,
                "ver": policies.POLICY_VERSION,
                "authority": str(actor_id),
                "now": _NOW,
                "proof": "test-proof",
            },
        )
    return tombstone_id


async def _schedule_every_derivative_of(corpus: _Corpus, claim_id: uuid.UUID, tombstone_id: uuid.UUID) -> int:
    """Enqueue a delete for every derivative built from this claim. Returns how many."""
    async with corpus.factory() as session, session.begin():
        return await derivatives.enqueue_for_sources(
            session,
            tenant_id=corpus.tenant_id,
            record_class=policies.RECORD_MEMORY_CLAIM,
            source_ids=[claim_id],
            operation=derivatives.OPERATION_DELETE,
            trigger=derivatives.TRIGGER_ERASURE,
            now=_NOW,
            tombstone_id=tombstone_id,
        )


def _worker(corpus: _Corpus) -> DerivativePropagationWorker:
    registry = derivatives.HandlerRegistry()
    for handler in retrieval_derivative_handlers():
        registry.register(handler)
    return DerivativePropagationWorker(corpus.factory, registry)


async def _artefact_counts(corpus: _Corpus, claim_id: uuid.UUID) -> dict[str, int]:
    params = {"cid": claim_id}
    return {
        "vectors": await corpus.count(
            "SELECT count(*) FROM embeddings WHERE target_type = 'claim' AND target_id = :cid", params
        ),
        "queued": await corpus.count(
            "SELECT count(*) FROM embedding_outbox WHERE target_type = 'claim' AND target_id = :cid", params
        ),
        "dead_lettered": await corpus.count(
            "SELECT count(*) FROM embedding_outbox_failed WHERE target_type = 'claim' AND target_id = :cid",
            params,
        ),
        "searchable_text": await corpus.count(
            "SELECT count(*) FROM embeddings "
            " WHERE target_type = 'claim' AND target_id = :cid "
            "   AND ts_vector @@ plainto_tsquery('english', 'words survive')",
            params,
        ),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projecting_a_claim_registers_the_queued_request_before_it_is_ever_embedded(
    corpus: _Corpus,
) -> None:
    """The pending row holds the claim's text verbatim, so it is covered from the moment it exists.

    Registering only when the drain writes a vector would leave every request that
    has not drained yet — including every request that never will, because the drain
    is behind or the row keeps failing — outside the coverage list entirely.
    """
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)

    async with corpus.factory() as session, session.begin():
        assert await project_claim(session, claim_id=claim_id, now=_NOW) is True

    registered = await corpus.count(
        "SELECT count(*) FROM derivative_registrations r "
        "  JOIN derivative_source_links l ON l.derivative_id = r.derivative_id "
        " WHERE l.source_id = :cid AND r.storage_locator = :loc AND r.derivative_kind = :kind",
        {"cid": claim_id, "loc": artefact_locator(TARGET_CLAIM, claim_id), "kind": derivatives.KIND_VECTOR},
    )
    assert registered == 1


@pytest.mark.asyncio
async def test_a_registered_artefact_expires_on_the_claims_content_clock(
    corpus: _Corpus, app_settings: Settings
) -> None:
    """Retention is the minimum across sources, and the claim's excerpt clock is the earlier one.

    Asserted against the database rather than the helper because the registration's
    `expires_at` is written by an upsert that takes the least of the stored and the
    incoming value — a rule only a real round trip exercises.
    """
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)
    await _project_and_drain(corpus, claim_id, app_settings)

    async with corpus.factory() as session:
        expires_at = (
            await session.execute(
                text("SELECT expires_at FROM derivative_registrations WHERE storage_locator = :loc"),
                {"loc": artefact_locator(TARGET_CLAIM, claim_id)},
            )
        ).scalar_one()

    assert expires_at == policies.payload_deadline(policies.RECORD_MEMORY_CLAIM, _NOW)


@pytest.mark.asyncio
async def test_discarding_a_claim_takes_its_vectors_out_of_the_index(corpus: _Corpus, app_settings: Settings) -> None:
    """E3-T6: `discard` used to leave them there, and nothing noticed.

    `close_superseded` and `mark_consolidated` both call `project_claim` after
    changing whether a claim is servable. `discard` writes `status='rejected'`
    -- "and it never serves again" -- and did not, so a staged, consolidated,
    already-indexed claim kept its vectors after a curator refused it.

    Not a correctness leak, which is why it survived: every read filters on
    `status`, so a rejected claim cannot be served. It is the recall loss
    retraction exists to prevent -- each dead vector occupies a candidate slot
    in `ORDER BY vector <-> q LIMIT k` -- and it was bounded only by retention
    expiry.

    Asserted against `embeddings` rather than through a search, because a search
    would return the same answer either way. The whole defect is invisible from
    the result set.
    """
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)
    await _project_and_drain(corpus, claim_id, app_settings)

    vectors = "SELECT count(*) FROM embeddings WHERE target_type = :kind AND target_id = :cid"
    assert await corpus.count(vectors, {"kind": TARGET_CLAIM, "cid": claim_id}) == 1, (
        "the claim was not indexed to begin with, so discarding it would remove nothing "
        "and this test would pass without exercising anything"
    )

    ctx = TenantContext(
        tenant_id=corpus.tenant_id,
        actor_id=actor,
        roles=["producer"],
        oidc_subject="curator",
    )
    await ClaimService(corpus.factory, clock=FakeClock(_NOW)).discard(ctx, claim_id=claim_id, reason="spurious")

    assert await corpus.count(vectors, {"kind": TARGET_CLAIM, "cid": claim_id}) == 0
    # The queued request goes too. A row left in the outbox would be re-embedded
    # by the next drain, putting the vector back and making the retraction look
    # intermittent rather than broken.
    assert (
        await corpus.count(
            "SELECT count(*) FROM embedding_outbox WHERE target_type = :kind AND target_id = :cid",
            {"kind": TARGET_CLAIM, "cid": claim_id},
        )
        == 0
    )


@pytest.mark.asyncio
async def test_draining_the_claim_registers_the_vector_it_wrote(corpus: _Corpus, app_settings: Settings) -> None:
    """The registration and the artefact are written together, and the row is really there."""
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)
    await _project_and_drain(corpus, claim_id, app_settings)

    counts = await _artefact_counts(corpus, claim_id)
    assert counts["vectors"] >= 1, "the drain must have written the vector this test then erases"
    assert counts["searchable_text"] >= 1, "and the claim's own words must be reachable through the lexical arm"


# ---------------------------------------------------------------------------
# The binding property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_every_registered_derivative_leaves_no_copy_of_the_claim_anywhere(
    corpus: _Corpus, app_settings: Settings
) -> None:
    """The whole point, arranged so that missing any one facet fails it.

    A drained vector, a pending request planted afterwards, and a dead-lettered
    request all hold the same text at the moment the erasure runs. After the
    propagation drain there must be none of them, and the full-text material —
    generated from `text_chunk` on the vector row — goes with the row that carried
    it.
    """
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)
    await _project_and_drain(corpus, claim_id, app_settings)

    body = index_text("observed_behavior", _CLAIM_TEXT_MARKER)
    async with corpus.factory() as session, session.begin():
        # A re-projection that has not been embedded yet.
        await enqueue(
            session,
            tenant_id=corpus.tenant_id,
            target_type=TARGET_CLAIM,
            target_id=claim_id,
            text_to_embed=body,
            chunk_plan=[{"index": 0, "text": body, "start": 0, "end": len(body.split())}],
            now=_NOW,
        )
        # A request that failed to embed. Nothing drains this table.
        await session.execute(
            text(
                "INSERT INTO embedding_outbox_failed "
                "  (failed_id, tenant_id, target_type, target_id, text_to_embed, chunk_plan, "
                "   failed_at, error_text, attempts) "
                "VALUES (gen_random_uuid(), :tid, 'claim', :cid, :body, CAST(:plan AS jsonb), "
                "        :now, 'boom', 5)"
            ),
            {"tid": corpus.tenant_id, "cid": claim_id, "body": body, "plan": json.dumps([]), "now": _NOW},
        )

    before = await _artefact_counts(corpus, claim_id)
    assert before["vectors"] >= 1
    assert before["queued"] == 1
    assert before["dead_lettered"] == 1

    tombstone_id = await _authorise_erasure(corpus, claim_id, actor)
    scheduled = await _schedule_every_derivative_of(corpus, claim_id, tombstone_id)
    assert scheduled >= 1, "the claim must have at least one registered derivative to erase"

    report = await _worker(corpus).run_once(now=_NOW + datetime.timedelta(minutes=1))
    assert report.claimed == scheduled
    assert report.failed == 0, "a handler that refused would leave the content in place"

    assert await _artefact_counts(corpus, claim_id) == {
        "vectors": 0,
        "queued": 0,
        "dead_lettered": 0,
        "searchable_text": 0,
    }


@pytest.mark.asyncio
async def test_propagating_the_same_erasure_twice_removes_nothing_further_and_still_succeeds(
    corpus: _Corpus, app_settings: Settings
) -> None:
    """Retrying a partially-applied propagation is the recovery path, so a second pass must be free."""
    actor = await corpus.actor()
    claim_id = await corpus.claim(actor, await corpus.entity(), value=_CLAIM_TEXT_MARKER)
    await _project_and_drain(corpus, claim_id, app_settings)

    tombstone_id = await _authorise_erasure(corpus, claim_id, actor)
    await _schedule_every_derivative_of(corpus, claim_id, tombstone_id)
    worker = _worker(corpus)
    await worker.run_once(now=_NOW + datetime.timedelta(minutes=1))

    # A second cause for the same derivative: the outbox is unique per derivative,
    # per operation, per trigger, per tombstone, so re-running the erasure enqueues
    # nothing. A different trigger does, and asks the handler to run again against
    # artefacts already gone.
    async with corpus.factory() as session, session.begin():
        assert (
            await derivatives.enqueue_for_sources(
                session,
                tenant_id=corpus.tenant_id,
                record_class=policies.RECORD_MEMORY_CLAIM,
                source_ids=[claim_id],
                operation=derivatives.OPERATION_DELETE,
                trigger=derivatives.TRIGGER_EXPIRY,
                now=_NOW,
            )
            >= 1
        )

    second = await worker.run_once(now=_NOW + datetime.timedelta(minutes=2))
    assert second.claimed >= 1
    assert second.failed == 0
    assert second.artefacts == 0, "nothing was left to remove, which is a successful answer"


@pytest.mark.asyncio
async def test_an_address_the_handler_cannot_resolve_fails_the_item_rather_than_marking_it_done(
    corpus: _Corpus,
) -> None:
    """A registration pointing somewhere unrecognised must leave a failed item, not a clean queue.

    `pending_overdue` counts a failed item, and the fail-closed read path keys off
    that — so the refusal has to reach the row rather than being swallowed into a
    "done" the operator would read as an erasure that completed.
    """
    async with corpus.factory() as session, session.begin():
        derivative_id = (
            await session.execute(
                text(
                    "INSERT INTO derivative_registrations "
                    "  (tenant_id, derivative_kind, storage_locator, audience_partition, classification, "
                    "   rebuild_handler_version, delete_handler_version, redact_handler_version, "
                    "   policy_version, expires_at, blocking, sync_status) "
                    "VALUES (:tid, :kind, 'somewhere/else', 'tenant', 'internal', 'v', 'v', 'v', "
                    "        :ver, :expires, FALSE, 'pending') "
                    "RETURNING derivative_id"
                ),
                {
                    "tid": corpus.tenant_id,
                    "kind": derivatives.KIND_VECTOR,
                    "ver": policies.POLICY_VERSION,
                    "expires": _NOW,
                },
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO derivative_work_outbox "
                "  (tenant_id, derivative_id, operation, trigger, available_at) "
                "VALUES (:tid, :did, :op, :trigger, :now)"
            ),
            {
                "tid": corpus.tenant_id,
                "did": derivative_id,
                "op": derivatives.OPERATION_DELETE,
                "trigger": derivatives.TRIGGER_EXPIRY,
                "now": _NOW,
            },
        )

    report = await _worker(corpus).run_once(now=_NOW + datetime.timedelta(minutes=1))
    assert report.claimed == 1
    assert report.applied == 0

    async with corpus.factory() as session:
        state, error = (
            await session.execute(
                text("SELECT state, last_error FROM derivative_work_outbox WHERE derivative_id = :did"),
                {"did": derivative_id},
            )
        ).one()
    assert state == "pending", "a first refusal is retried, and the row carries why"
    assert "does not address the embedding index" in error
