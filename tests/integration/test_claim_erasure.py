"""Erasing an actor's derived claims: the selections, repairs, and residue.

These tests were written before the participant was registered, and the ones
that matter most assert what *survives*: a claim with any independent evidence
must outlive its author's erasure, because deleting it would destroy another
source's contribution — over-erasure is data loss, under-erasure is the
compliance lie this participant exists to end. The rule under test: a
provenance row disqualifies a claim from erasure iff it is non-session
evidence or a *live* session event of a *different* actor; dangling refs never
disqualify (an erased actor's event is nobody's independent evidence), which
is also what makes the outcome identical no matter which earlier erasure
attempt happened to fail partway.

The chain-repair cases exist because two claim→claim references occur across
authors by design (curator-authored confirmation successors; cross-author
consolidation losers) and neither column cascades.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.claim_erasure import ClaimErasure
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Corpus:
    """SQL seed helpers over one tenant. Each method returns the created id."""

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
            {"aid": aid, "tid": self.tenant_id, "sub": f"er-{aid.hex[:12]}", "now": _NOW},
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

    async def event(self, actor_id: uuid.UUID, body: str = "said a thing") -> uuid.UUID:
        eid = uuid.uuid4()
        await self._exec(
            "INSERT INTO memory_session_events "
            "  (event_id, tenant_id, actor_id, session_id, seq, kind, body, created_at, "
            "   expires_at, size_bytes) "
            "VALUES (:eid, :tid, :aid, :sid, :seq, 'user_message', :body, :now, :now, :size)",
            {
                "eid": eid,
                "tid": self.tenant_id,
                "aid": actor_id,
                "sid": f"s-{actor_id.hex[:8]}",
                "seq": int(eid.int % 1_000_000),
                "body": body,
                "now": _NOW,
                "size": len(body.encode()),
            },
        )
        return eid

    async def claim(
        self,
        author: uuid.UUID,
        *,
        subject: uuid.UUID | None = None,
        namespace: str = "observation/test",
        status: str = "staged",
        superseded_by: uuid.UUID | None = None,
        confirms: uuid.UUID | None = None,
        confirmed_by: uuid.UUID | None = None,
        consolidated: bool = False,
        promotion_state: str | None = None,
    ) -> uuid.UUID:
        cid = uuid.uuid4()
        unlinked = subject is None
        await self._exec(
            "INSERT INTO memory_claims ("
            "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id,"
            "  subject_entity_id, subject_reference, predicate, value_type,"
            "  claim_category, value_jsonb, asserted_valid_from, status,"
            "  visibility, source_authority, size_bytes, namespace,"
            "  superseded_by, superseded_reason, t_invalidated_at, consolidated_at,"
            "  confirms_claim_id, confirmed_by, confirmed_at, promotion_state,"
            "  confidence, confidence_scored_at, confidence_inputs, scorer_version,"
            "  calibration_version, decay_half_life_days, strategy_id"
            ") VALUES ("
            "  :cid, :owner, :tid, :author, :subject, 'subject-ref', 'observed_behavior',"
            "  'prose', 'capability_observation', CAST(:val AS JSONB), :now, :status,"
            "  'private', 'observer_extraction', 9, :ns,"
            "  :sup, :sup_reason, :t_inv, :cons,"
            "  :confirms, :confirmed_by, :confirmed_at, :promo,"
            "  :conf, :conf_at, CAST(:conf_in AS JSONB), :scorer,"
            "  :calib, :half_life, :strategy"
            ")",
            {
                "cid": cid,
                "owner": None if unlinked else self.tenant_id,
                "tid": self.tenant_id,
                "author": author,
                "subject": subject,
                "val": json.dumps("observed"),
                "now": _NOW,
                "status": "unlinked" if unlinked else status,
                "ns": namespace,
                "sup": superseded_by,
                "sup_reason": "lost_conflict" if superseded_by else None,
                "t_inv": _NOW if superseded_by else None,
                "cons": _NOW if consolidated else None,
                "confirms": confirms,
                "confirmed_by": confirmed_by if confirms else None,
                "confirmed_at": _NOW if confirms else None,
                "promo": promotion_state,
                # Scored exactly when linked: the constraint pairs confidence
                # with subject resolution, and the four inputs pair together.
                "conf": None if unlinked else 0.700,
                "conf_at": None if unlinked else _NOW,
                "conf_in": None if unlinked else json.dumps({"seed": True}),
                "scorer": None if unlinked else "seed-1",
                "calib": None if unlinked else "uncalibrated",
                "half_life": None if unlinked else 90,
                # namespace and strategy_id pair by constraint.
                "strategy": "seed_strategy",
            },
        )
        return cid

    async def provenance(self, claim_id: uuid.UUID, kind: str, ref: str, excerpt: str | None = None) -> None:
        await self._exec(
            "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref, evidence_excerpt) "
            "VALUES (:cid, :kind, :ref, :ex)",
            {"cid": claim_id, "kind": kind, "ref": ref, "ex": excerpt},
        )

    async def embedding(self, claim_id: uuid.UUID, chunk: str) -> None:
        await self._exec(
            "INSERT INTO embeddings (tenant_id, target_type, target_id, chunk_index, text_chunk, vector, model_id) "
            "VALUES (:tid, 'claim', :cid, 0, :chunk, CAST(:vec AS vector), 'seed-model')",
            {"tid": self.tenant_id, "cid": claim_id, "chunk": chunk, "vec": str([0.1] * 384)},
        )

    async def promotion(
        self, claim_id: uuid.UUID, actor: uuid.UUID, *, superseded_attr: uuid.UUID | None = None
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """A proposal + journal + canonical attribute row. Returns (promotion_id, attr_id)."""
        entity = await self.entity()
        attr_id, proposal_id, promotion_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await self._exec(
            "INSERT INTO attributes (attr_id, tenant_id, entity_id, key, value, t_valid_from) "
            "VALUES (:aid, :tid, :eid, 'observed_behavior', :val, :now)",
            {"aid": attr_id, "tid": self.tenant_id, "eid": entity, "val": json.dumps("v"), "now": _NOW},
        )
        await self._exec(
            "INSERT INTO memory_promotion_proposal "
            "  (proposal_id, claim_id, owner_tenant_id, author_tenant_id, subject_entity_id, "
            "   predicate, target_kind, target_key, mapping_version, proposed_value, "
            "   valid_from, state, decided_by, decided_at) "
            "VALUES (:pid, :cid, :tid, :tid, :eid, 'observed_behavior', 'attribute', "
            "        'observed_behavior', 1, CAST(:val AS JSONB), :now, 'accepted', :actor, :now)",
            {
                "pid": proposal_id,
                "cid": claim_id,
                "tid": self.tenant_id,
                "eid": entity,
                "val": json.dumps("v"),
                "now": _NOW,
                "actor": actor,
            },
        )
        await self._exec(
            "INSERT INTO memory_promotion_journal "
            "  (promotion_id, proposal_id, claim_id, tenant_id, target_kind, created_row_id, "
            "   superseded_row_id, promoted_at, promoted_by) "
            "VALUES (:prid, :pid, :cid, :tid, 'attribute', :created, :sup, :now, :actor)",
            {
                "prid": promotion_id,
                "pid": proposal_id,
                "cid": claim_id,
                "tid": self.tenant_id,
                "created": attr_id,
                "sup": superseded_attr,
                "now": _NOW,
                "actor": actor,
            },
        )
        return promotion_id, attr_id

    async def fetch(self, sql: str, params: dict[str, Any]) -> list[Any]:
        async with self.factory() as session:
            return list(await session.execute(text(sql), params))

    async def claim_exists(self, claim_id: uuid.UUID) -> bool:
        rows = await self.fetch("SELECT 1 FROM memory_claims WHERE claim_id = :cid", {"cid": claim_id})
        return bool(rows)


@pytest_asyncio.fixture
async def corpus(factory: async_sessionmaker[AsyncSession]) -> _Corpus:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"erase-{tid.hex[:8]}", "now": _NOW},
        )
    return _Corpus(factory, tid)


def _ctx(corpus: _Corpus, requester: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=corpus.tenant_id, actor_id=requester, roles=["admin"], oidc_subject="eraser")


async def _erase(corpus: _Corpus, target: uuid.UUID) -> dict[str, int]:
    requester = await corpus.actor()
    participant = ClaimErasure(corpus.factory)
    return await participant.erase_actor(_ctx(corpus, requester), target)


async def test_sole_session_evidence_claim_is_erased_with_its_vectors(corpus: _Corpus) -> None:
    target = await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target, body="the auth service rate limit is 40rps")
    claim = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim, "session_event", str(event), "rate limit is 40rps")
    await corpus.embedding(claim, "the auth service rate limit is 40rps")

    counts = await _erase(corpus, target)

    assert not await corpus.claim_exists(claim)
    assert counts["claims"] == 1
    assert counts["embeddings"] == 1
    vectors = await corpus.fetch(
        "SELECT 1 FROM embeddings WHERE target_type = 'claim' AND target_id = :cid", {"cid": claim}
    )
    assert vectors == []


async def test_independent_evidence_saves_the_claim_but_not_the_excerpt(corpus: _Corpus) -> None:
    """A document-backed claim survives; the target's verbatim words on it do not."""
    target = await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    claim = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim, "session_event", str(event), "their exact words")
    await corpus.provenance(claim, "document_revision", "doc-abc", "the doc said so")

    counts = await _erase(corpus, target)

    assert await corpus.claim_exists(claim)
    assert counts["claims"] == 0
    assert counts["provenance_rows_scrubbed"] == 1
    remaining = await corpus.fetch(
        "SELECT evidence_kind FROM memory_claim_provenance WHERE claim_id = :cid", {"cid": claim}
    )
    assert [r.evidence_kind for r in remaining] == ["document_revision"]


async def test_another_actors_live_event_is_independent_evidence(corpus: _Corpus) -> None:
    target, other = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    own_event = await corpus.event(target)
    other_event = await corpus.event(other)
    claim = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim, "session_event", str(own_event))
    await corpus.provenance(claim, "session_event", str(other_event))

    await _erase(corpus, target)

    assert await corpus.claim_exists(claim)


async def test_mixed_live_and_dangling_own_evidence_is_erased(corpus: _Corpus) -> None:
    """Dangling refs are nobody's independent evidence — the mixed case dies too."""
    target = await corpus.actor()
    subject = await corpus.entity()
    live_event = await corpus.event(target)
    claim = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim, "session_event", str(live_event))
    await corpus.provenance(claim, "session_event", str(uuid.uuid4()))  # pre-participant residue

    await _erase(corpus, target)

    assert not await corpus.claim_exists(claim)


async def test_zero_provenance_claim_is_erased(corpus: _Corpus) -> None:
    target = await corpus.actor()
    subject = await corpus.entity()
    claim = await corpus.claim(target, subject=subject)

    await _erase(corpus, target)

    assert not await corpus.claim_exists(claim)


async def test_curator_confirmed_claim_survives_with_its_pointer_cleared(corpus: _Corpus) -> None:
    """Confirmation writes curator evidence — independent — but the successor's
    reference to the erased original cannot survive the original."""
    target, curator = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    original = await corpus.claim(target, subject=subject)
    await corpus.provenance(original, "session_event", str(event))
    successor = await corpus.claim(curator, subject=subject, confirms=original, confirmed_by=curator)
    await corpus.provenance(successor, "curator", str(curator))

    counts = await _erase(corpus, target)

    assert not await corpus.claim_exists(original)
    assert await corpus.claim_exists(successor)
    assert counts["confirmation_refs_cleared"] == 1
    row = (
        await corpus.fetch(
            "SELECT confirms_claim_id, confirmed_by, confirmed_at FROM memory_claims WHERE claim_id = :cid",
            {"cid": successor},
        )
    )[0]
    assert (row.confirms_claim_id, row.confirmed_by, row.confirmed_at) == (None, None, None)


async def test_preference_claims_die_regardless_of_author_and_evidence(corpus: _Corpus) -> None:
    """Prong (a) precedence: a confirmed preference is still the person's preference."""
    target, curator = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    ns = f"preference/tenant/{corpus.tenant_id}/actor/{target}"
    pref = await corpus.claim(target, subject=subject, namespace=ns)
    await corpus.provenance(pref, "document_revision", "doc-1")  # independent — still dies
    confirmed_pref = await corpus.claim(curator, subject=subject, namespace=ns)
    await corpus.provenance(confirmed_pref, "curator", str(curator))

    counts = await _erase(corpus, target)

    assert not await corpus.claim_exists(pref)
    assert not await corpus.claim_exists(confirmed_pref)
    assert counts["claims"] == 2


async def test_cross_author_loser_is_spliced_past_a_fully_selected_chain(corpus: _Corpus) -> None:
    target, other = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    # Target's chain: a1 superseded by a2; a2 superseded by the survivor (unselected).
    survivor = await corpus.claim(other, subject=subject)
    await corpus.provenance(survivor, "document_revision", "doc-s")
    a2 = await corpus.claim(target, subject=subject, status="superseded", superseded_by=survivor)
    await corpus.provenance(a2, "session_event", str(event))
    a1 = await corpus.claim(target, subject=subject, status="superseded", superseded_by=a2)
    await corpus.provenance(a1, "session_event", str(event))
    # Cross-author loser pointing at the doomed a1.
    loser = await corpus.claim(other, subject=subject, status="superseded", superseded_by=a1)
    await corpus.provenance(loser, "document_revision", "doc-l")

    counts = await _erase(corpus, target)

    assert counts["chains_spliced"] == 1
    row = (await corpus.fetch("SELECT superseded_by, status FROM memory_claims WHERE claim_id = :cid", {"cid": loser}))[
        0
    ]
    assert row.superseded_by == survivor
    assert row.status == "superseded"


async def test_loser_is_reopened_when_the_whole_chain_is_erased(corpus: _Corpus) -> None:
    target, other = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    winner = await corpus.claim(target, subject=subject)
    await corpus.provenance(winner, "session_event", str(event))
    loser = await corpus.claim(other, subject=subject, status="superseded", superseded_by=winner, consolidated=True)
    await corpus.provenance(loser, "document_revision", "doc-l")

    counts = await _erase(corpus, target)

    assert counts["losers_reopened"] == 1
    row = (
        await corpus.fetch(
            "SELECT status, superseded_by, superseded_reason, t_invalidated_at, consolidated_at "
            "FROM memory_claims WHERE claim_id = :cid",
            {"cid": loser},
        )
    )[0]
    assert row.status == "staged"
    assert row.superseded_by is None and row.superseded_reason is None
    # Both markers cleared: an invalidated or still-consolidated claim would be
    # skipped by the next sweep instead of re-decided.
    assert row.t_invalidated_at is None and row.consolidated_at is None


async def test_promoted_claims_canonical_row_is_deleted_and_predecessor_reopened(
    corpus: _Corpus,
) -> None:
    target = await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    claim = await corpus.claim(target, subject=subject, promotion_state="promoted")
    await corpus.provenance(claim, "session_event", str(event))

    # The promotion closed a predecessor attribute; erasure must reopen it.
    predecessor = uuid.uuid4()
    async with corpus.factory() as session, session.begin():
        entity = await corpus.entity()
        await session.execute(
            text(
                "INSERT INTO attributes (attr_id, tenant_id, entity_id, key, value, "
                "  t_valid_from, t_valid_to, t_invalidated_at) "
                "VALUES (:aid, :tid, :eid, 'k', :val, :now, :now, :now)"
            ),
            {"aid": predecessor, "tid": corpus.tenant_id, "eid": entity, "val": json.dumps("old"), "now": _NOW},
        )
    _, attr_id = await corpus.promotion(claim, target, superseded_attr=predecessor)

    counts = await _erase(corpus, target)

    assert not await corpus.claim_exists(claim)
    assert counts["canonical_rows_deleted"] == 1
    assert counts["canonical_rows_reopened"] == 1
    assert counts["journal_rows_deleted"] == 1
    assert counts["proposals_deleted"] == 1
    gone = await corpus.fetch("SELECT 1 FROM attributes WHERE attr_id = :aid", {"aid": attr_id})
    assert gone == []
    reopened = (
        await corpus.fetch("SELECT t_invalidated_at FROM attributes WHERE attr_id = :aid", {"aid": predecessor})
    )[0]
    assert reopened.t_invalidated_at is None


async def test_stacked_promotion_keeps_the_later_head(corpus: _Corpus) -> None:
    """A later promotion built on the erased row stays live; only the erased
    person's row vanishes from the middle of the chain."""
    target, other = await corpus.actor(), await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    claim = await corpus.claim(target, subject=subject, promotion_state="promoted")
    await corpus.provenance(claim, "session_event", str(event))
    _, target_attr = await corpus.promotion(claim, target)

    # Someone else's later promotion superseded the target's canonical row.
    other_claim = await corpus.claim(other, subject=subject, promotion_state="promoted")
    await corpus.provenance(other_claim, "document_revision", "doc-x")
    _, later_attr = await corpus.promotion(other_claim, other, superseded_attr=target_attr)

    counts = await _erase(corpus, target)

    assert counts["canonical_rows_deleted"] == 1
    assert counts["canonical_rows_reopened"] == 0
    still_there = await corpus.fetch("SELECT 1 FROM attributes WHERE attr_id = :aid", {"aid": later_attr})
    assert still_there
    assert await corpus.claim_exists(other_claim)


async def test_second_run_reports_zeros(corpus: _Corpus) -> None:
    target = await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    claim = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim, "session_event", str(event))

    first = await _erase(corpus, target)
    second = await _erase(corpus, target)

    assert first["claims"] == 1
    assert all(v == 0 for v in second.values()), second


async def test_erasure_is_tenant_scoped(corpus: _Corpus, factory: async_sessionmaker[AsyncSession]) -> None:
    """The same actor's claims in another tenant survive a tenant-A request."""
    target = await corpus.actor()
    subject = await corpus.entity()
    event = await corpus.event(target)
    claim_a = await corpus.claim(target, subject=subject)
    await corpus.provenance(claim_a, "session_event", str(event))

    other_tenant = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": other_tenant, "slug": f"erase-b-{other_tenant.hex[:8]}", "now": _NOW},
        )
    corpus_b = _Corpus(factory, other_tenant)
    # Same human, second tenant: actors are tenant-rows, so seed a row whose
    # claims we can prove untouched by the tenant-A request.
    target_b = await corpus_b.actor()
    subject_b = await corpus_b.entity()
    claim_b = await corpus_b.claim(target_b, subject=subject_b)

    await _erase(corpus, target)

    assert not await corpus.claim_exists(claim_a)
    assert await corpus_b.claim_exists(claim_b)
