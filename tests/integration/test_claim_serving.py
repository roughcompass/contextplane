"""Serving claims: cited, governed by visibility, and labelled as recalled.

The three properties these tests exist to hold are structural rather than behavioural,
so most of them try to *break* the structure rather than exercise the happy path: build
a claim with no citations, ask for one whose subject belongs to somebody else, serve the
same claim to four personas and check the values did not move.

Visibility is the sharp one. A claim marked public about a capability that is private to
another tenant must not be returned, because returning it discloses that the capability
exists -- and the caller never asked about the claim, they asked about the subject.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.claim_ontology import seed_ontology
from registry.service.claim_serving import (
    PERSONA_AGENT,
    PERSONA_ARCHITECT,
    PERSONA_L1,
    PERSONA_L3,
    PERSONAS,
    RECALL_LABEL,
    RECALL_TRUST,
    Citation,
    ClaimQuery,
    ClaimServingService,
    ServedClaim,
    UncitedClaimError,
)
from registry.service.claims import ClaimService, Evidence
from registry.service.consolidation import ConsolidationService
from registry.service.global_vocabulary import GlobalVocabularyService
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)

# The drain reads only batch size and max attempts off Settings. The three URLs are
# required by the constructor and never dialled from here, so a placeholder is honest.
_DSN = "postgresql+asyncpg://unused/unused"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


@pytest.fixture
def claims(factory: async_sessionmaker[AsyncSession]) -> ClaimService:
    return ClaimService(factory, clock=FakeClock(_NOW))


@pytest.fixture
def serving(factory: async_sessionmaker[AsyncSession]) -> ClaimServingService:
    return ClaimServingService(factory, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"srv-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, 'human', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _seed_entity(
    factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, visibility: str = "public"
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, :vis, TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "vis": visibility, "now": _NOW},
        )
    return eid


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s")


def _serving_at(factory: async_sessionmaker[AsyncSession], minutes: int) -> ClaimServingService:
    """A serving service reading from an instant after the writes under test.

    `created_at <= as_of` is deliberate: a claim written after the instant asked
    about was not believed then. So a test that stages claims at later offsets has
    to read from further forward, rather than the rule being relaxed.
    """
    return ClaimServingService(factory, clock=FakeClock(_NOW + datetime.timedelta(minutes=minutes)))


async def _drain_all(factory: async_sessionmaker[AsyncSession], embedder: Any) -> int:
    """Drain the embedding outbox until it is empty, and report how many rows it took.

    Claims reach the index the same way facts do now: staging and consolidating a claim
    enqueues it, and the shared drain turns the queue into vectors. Tests therefore
    consolidate and then drain, rather than calling an indexing method that no longer
    exists -- which is the point of the unification, so exercising the real route matters.
    """
    from registry.config import Settings  # noqa: PLC0415
    from registry.service.embedding_drain import drain_outbox  # noqa: PLC0415

    # The drain only reads batch size and max attempts off Settings; the URLs are
    # required by the constructor and unused here.
    settings = Settings(
        database_url=_DSN,
        pgbouncer_url=_DSN,
        scheduler_jobstore_url=_DSN,
        embedding_provider="stub",
    )
    drained = 0
    for _ in range(50):
        async with factory() as session:
            pending = (await session.execute(text("SELECT count(*) FROM embedding_outbox"))).scalar_one()
        if not pending:
            break
        await drain_outbox(factory, embedder, settings)
        drained += int(pending)
    return drained


async def _stage(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    predicate: str = "owned_by_team",
    value: object = "platform",
    at: int = 0,
    **kw: object,
) -> uuid.UUID:
    clock = FakeClock(_NOW + datetime.timedelta(minutes=at))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=(Evidence(kind="session_event", ref="e1", excerpt="the platform team owns it"),),
        **kw,  # type: ignore[arg-type]
    )
    await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id


# --- an uncited claim is unrepresentable -------------


def test_a_claim_cannot_be_constructed_without_citations() -> None:
    """The structural half of the citation guarantee.

    Not "every serving path remembers to add citations" -- that works until one does
    not. The type refuses.
    """
    with pytest.raises(UncitedClaimError, match="no citations"):
        ServedClaim(
            claim_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            predicate="owned_by_team",
            value="platform",
            claim_category="ownership_stewardship",
            confidence=0.9,
            authority="owner_extraction",
            valid_from=_NOW,
            valid_to=None,
            as_of=_NOW,
            human_confirmed=False,
            citations=(),
        )


def test_a_served_claim_cannot_shed_its_untrusted_label() -> None:
    """Confidence does not substitute for the label. A high-confidence extraction of
    a hostile statement is still hostile, so the label is not a field a caller can
    choose to omit."""
    with pytest.raises(UncitedClaimError, match="recalled and untrusted"):
        ServedClaim(
            claim_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            predicate="owned_by_team",
            value="platform",
            claim_category="ownership_stewardship",
            confidence=0.99,
            authority="owner_human",
            valid_from=_NOW,
            valid_to=None,
            as_of=_NOW,
            human_confirmed=True,
            citations=(Citation(kind="session_event", ref="e1"),),
            trust="trusted",
        )


@pytest.mark.asyncio
async def test_every_served_claim_carries_the_full_citation_payload(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _stage(factory, tid, aid, subject)

    served = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject))

    assert len(served) == 1
    claim = served[0]
    assert claim.citations, "a resolvable provenance handle"
    assert claim.citations[0].ref == "e1"
    assert claim.authority, "confidence with the authority that shaped it"
    assert claim.valid_from == _NOW, "effective interval"
    assert claim.as_of == _NOW, "as_of basis"
    assert claim.human_confirmed is False
    assert claim.label == RECALL_LABEL
    assert claim.trust == RECALL_TRUST


# --- exit criterion 1: structural query, no ranking -----------------------------


@pytest.mark.asyncio
async def test_a_query_by_subject_and_predicate_returns_exact_matches(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    other = await _seed_entity(factory, tid)

    await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")
    await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://r/1", at=5)
    await _stage(factory, tid, aid, other, predicate="owned_by_team", value="billing", at=10)

    served = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, predicate="owned_by_team"))

    assert [c.value for c in served] == ["platform"]


@pytest.mark.asyncio
async def test_a_structural_query_carries_no_relevance_score(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Ranked retrieval and structural lookup answer different questions. Borrowing a
    score for a lookup would make an exact answer depend on a similarity nobody asked
    for -- and a caller could not tell which they had got."""
    assert not any(f.name in {"score", "rank", "relevance"} for f in dataclasses.fields(ServedClaim))


@pytest.mark.asyncio
async def test_filters_apply_before_the_limit_not_after(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Filtering after the limit returns a short page from a long list and calls it
    the top ten, which is a different answer wearing the same shape."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    for index in range(5):
        await _stage(factory, tid, aid, subject, predicate="escalation_contact", value=f"contact-{index}", at=index)
    await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://r/1", at=20)

    served = await _serving_at(factory, 30).query(
        _ctx(tid, aid), ClaimQuery(subject_entity_id=subject, predicate="runbook_url", limit=1)
    )
    assert [c.value for c in served] == ["https://r/1"]


@pytest.mark.asyncio
async def test_a_limit_beyond_the_maximum_is_refused(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    with pytest.raises(ValueError, match="limit must be"):
        ClaimQuery(limit=101)


@pytest.mark.asyncio
async def test_an_unknown_persona_is_refused(ontology: None) -> None:
    """A typo'd persona must not silently fall back to a default depth -- that would
    serve an L1 responder an architect's view without anybody noticing."""
    with pytest.raises(ValueError, match="unknown persona"):
        ClaimQuery(persona="l2")


# --- exit criterion 2: as_of reads the history ----------------------------------


@pytest.mark.asyncio
async def test_a_query_before_a_supersession_returns_the_earlier_belief(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    await _stage(factory, tid, aid, subject, value="platform")
    probe = _NOW + datetime.timedelta(minutes=5)
    await _stage(factory, tid, aid, subject, value="billing", at=10)

    later = _serving_at(factory, 30)
    now_view = await later.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject))
    then_view = await later.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, as_of=probe))

    assert [c.value for c in now_view] == ["billing"]
    assert [c.value for c in then_view] == ["platform"]
    assert then_view[0].as_of == probe, "the answer carries the basis it was true as of"


# --- exit criterion 3: only settled claims are served ---------------------------


@pytest.mark.asyncio
async def test_an_unconsolidated_claim_is_never_served(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """A claim that has not been checked against its neighbourhood might be a
    duplicate or the loser of a conflict. Serving it publishes it as settled."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    await ClaimService(factory, clock=FakeClock(_NOW)).stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    assert await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject)) == ()


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_never_served(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    await ClaimService(factory, clock=FakeClock(_NOW)).stage_claim(
        _ctx(tid, aid),
        subject_reference="nothing resolvable",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    assert await serving.query(_ctx(tid, aid), ClaimQuery()) == ()


# --- exit criteria 5 and 6: visibility ------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_whose_subject_is_invisible_reads_as_not_found(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Not-found rather than forbidden. Distinguishing the two is an existence oracle
    over every entity in the deployment."""
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    stranger_actor = await _seed_actor(factory, stranger)
    subject = await _seed_entity(factory, owner, visibility="private")

    claim_id = await _stage(factory, owner, owner_actor, subject)

    assert await serving.get(_ctx(owner, owner_actor), claim_id) is not None
    assert await serving.get(_ctx(stranger, stranger_actor), claim_id) is None


@pytest.mark.asyncio
async def test_a_private_subject_never_appears_in_a_cross_tenant_query(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    stranger_actor = await _seed_actor(factory, stranger)
    subject = await _seed_entity(factory, owner, visibility="private")
    await _stage(factory, owner, owner_actor, subject)

    assert await serving.query(_ctx(stranger, stranger_actor), ClaimQuery()) == ()


@pytest.mark.asyncio
async def test_a_public_claim_about_a_private_subject_is_still_withheld(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """The case a claim-only visibility check would miss. A claim inherits nothing
    from its subject automatically, so returning it would disclose that a capability
    the caller cannot see exists at all."""
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    stranger_actor = await _seed_actor(factory, stranger)
    subject = await _seed_entity(factory, owner, visibility="private")

    claim_id = await _stage(factory, owner, owner_actor, subject)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE lmm_claims SET visibility = 'public' WHERE claim_id = :c"),
            {"c": claim_id},
        )

    assert await serving.get(_ctx(stranger, stranger_actor), claim_id) is None


# --- exit criterion 7: persona changes depth, not meaning -----------------------


@pytest.mark.asyncio
async def test_the_same_claim_has_the_same_value_under_every_persona(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """The property that makes persona safe. If depth could change what a claim
    means, two readers of the same store would disagree about a fact and each would
    be able to cite the system."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")

    seen: dict[str, tuple[object, float, int]] = {}
    for persona in sorted(PERSONAS):
        served = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, persona=persona))
        if served:
            claim = served[0]
            seen[persona] = (claim.value, claim.confidence, len(claim.citations))

    assert len(seen) >= 2, "the predicate must reach more than one persona to compare"
    assert len(set(seen.values())) == 1, f"personas disagreed about the claim: {seen}"


@pytest.mark.asyncio
async def test_depth_differs_in_which_categories_come_back(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _stage(factory, tid, aid, subject, predicate="decision_record_url", value="https://adr/1")

    l1 = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, persona=PERSONA_L1))
    architect = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, persona=PERSONA_ARCHITECT))

    assert l1 == (), "an L1 responder is not served decision rationale"
    assert len(architect) == 1


@pytest.mark.asyncio
async def test_depth_differs_in_whether_provenance_is_inlined(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """An L3 engineer reading about a timeout wants the line that said so. An L1
    responder working an incident does not want a wall of transcript -- but still
    gets the handle, so the evidence is one fetch away rather than absent."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")

    l1 = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, persona=PERSONA_L1))
    l3 = await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, persona=PERSONA_L3))

    assert l1[0].citations[0].excerpt is None
    assert l1[0].citations[0].ref == "e1", "the handle is always present"
    assert l3[0].citations[0].excerpt == "the platform team owns it"


@pytest.mark.asyncio
async def test_the_agent_persona_receives_every_category(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """An agent filtering for itself is better placed than this module to know what
    it needs. The depth knob for an agent is the absence of prose framing, not fewer
    facts."""
    from registry.service.claim_serving import CATEGORIES_BY_PERSONA

    for persona, categories in CATEGORIES_BY_PERSONA.items():
        if persona != PERSONA_AGENT:
            assert categories <= CATEGORIES_BY_PERSONA[PERSONA_AGENT]


# --- filters --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_namespace_prefix_matches_hierarchically(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    first = await _stage(factory, tid, aid, subject, value="platform")
    second = await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://r/1", at=5)
    async with factory() as session, session.begin():
        for claim_id, namespace in ((first, "team/platform/core"), (second, "team/billing")):
            await session.execute(
                text("UPDATE lmm_claims SET namespace = :ns, strategy_id = 'local-rules' " " WHERE claim_id = :c"),
                {"ns": namespace, "c": claim_id},
            )

    served = await _serving_at(factory, 30).query(
        _ctx(tid, aid), ClaimQuery(subject_entity_id=subject, namespace_prefix="team/platform")
    )
    assert [c.value for c in served] == ["platform"]


@pytest.mark.asyncio
async def test_min_confidence_excludes_weaker_claims(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _stage(factory, tid, aid, subject)

    assert await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, min_confidence=0.0))
    assert await serving.query(_ctx(tid, aid), ClaimQuery(subject_entity_id=subject, min_confidence=0.999)) == ()


@pytest.mark.asyncio
async def test_a_private_claim_about_a_public_subject_is_withheld(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """The case a subject-only visibility check misses.

    The capability is public, so anybody may see that it exists. The claim about it
    is private, so only its own tenant may read what was observed. Checking the
    subject alone returns it; both must be checked, which is why the requirement
    names both.
    """
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    stranger_actor = await _seed_actor(factory, stranger)
    subject = await _seed_entity(factory, owner, visibility="public")

    claim_id = await _stage(factory, owner, owner_actor, subject)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE lmm_claims SET visibility = 'private' WHERE claim_id = :c"),
            {"c": claim_id},
        )

    assert await serving.get(_ctx(owner, owner_actor), claim_id) is not None
    assert await serving.get(_ctx(stranger, stranger_actor), claim_id) is None


# --- semantic retrieval ---------------------------------------------------------


class _TokenEmbedder:
    """A deterministic bag-of-words embedder, so ranking is actually testable.

    The shipped stub returns zero vectors, which makes every distance identical and
    every ranking arbitrary -- fine for exercising plumbing, useless for asserting
    that the relevant claim comes first. This hashes tokens into buckets, so text
    sharing words lands nearby and text sharing none does not.
    """

    model_version = "token-hash-v1"

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    def encode(self, texts: list[str]) -> Any:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for row, text_value in enumerate(texts):
            for token in str(text_value).lower().replace(":", " ").split():
                out[row][hash(token) % self._dim] += 1.0
            norm = float(np.linalg.norm(out[row]))
            if norm:
                out[row] /= norm
        return out


@pytest.mark.asyncio
async def test_a_query_naming_no_predicate_finds_the_relevant_claim(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """The case structural query cannot answer: the caller does not know what to ask
    for, only what they want to know about."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    owned = await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")
    runbook = await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://runbooks/auth")
    for _claim_id in (owned, runbook):
        await _drain_all(factory, embedder)

    found = await serving.retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder, top_k=5)

    assert found, "semantic retrieval returned nothing"
    assert found[0].claim_id == owned, "the relevant claim did not rank first"


@pytest.mark.asyncio
async def test_semantic_results_carry_the_same_citations_as_structural_ones(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """One serving path. A separate one for ranked results would be a second place
    the citation and label guarantees could lapse, and it would lapse under the
    pressure of wanting search to feel fast."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    await _stage(factory, tid, aid, subject)
    await _drain_all(factory, embedder)

    found = await serving.retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder)

    assert found[0].citations
    assert found[0].label == RECALL_LABEL
    assert found[0].trust == RECALL_TRUST
    assert found[0].authority


@pytest.mark.asyncio
async def test_an_unconsolidated_claim_is_never_indexed(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Being findable is most of what being served means. Indexing an unreconciled
    claim would leave the read path's status filter as the only thing between a
    caller and a claim that was never meant to be visible."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim = await ClaimService(factory, clock=FakeClock(_NOW)).stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    # Never enqueued, so a drain finds nothing to do and the claim gets no vector.
    assert await _drain_all(factory, _TokenEmbedder()) == 0
    async with factory() as session:
        vectors = (
            await session.execute(
                text("SELECT count(*) FROM embeddings WHERE target_id = :c"),
                {"c": claim.claim_id},
            )
        ).scalar_one()
    assert vectors == 0


@pytest.mark.asyncio
async def test_retrieval_never_crosses_a_visibility_boundary(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    owner = await _seed_tenant(factory)
    stranger = await _seed_tenant(factory)
    owner_actor = await _seed_actor(factory, owner)
    stranger_actor = await _seed_actor(factory, stranger)
    subject = await _seed_entity(factory, owner, visibility="private")
    embedder = _TokenEmbedder()

    await _stage(factory, owner, owner_actor, subject)
    await _drain_all(factory, embedder)

    assert await serving.retrieve(_ctx(owner, owner_actor), query="owned by team", embedder=embedder)
    assert await serving.retrieve(_ctx(stranger, stranger_actor), query="owned by team", embedder=embedder) == ()


@pytest.mark.asyncio
async def test_the_semantic_arm_ignores_rows_from_another_model(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Two models produce vectors that are not comparable. A deployment mid-reindex
    holds both, and ranking them together returns whichever landed closer under an
    arithmetic that means nothing.

    Asked with a query sharing no words with the indexed text, so the lexical arm
    cannot answer and only the semantic arm could -- which is the arm the model
    filter protects. The lexical arm is deliberately not filtered: it matches text,
    and text does not stop being text when the embedding model changes.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    await _stage(factory, tid, aid, subject)
    await _drain_all(factory, _TokenEmbedder())

    class _OtherModel(_TokenEmbedder):
        model_version = "some-other-model"

    unrelated = "escalation contact rotation"
    assert await serving.retrieve(_ctx(tid, aid), query=unrelated, embedder=_TokenEmbedder())
    assert await serving.retrieve(_ctx(tid, aid), query=unrelated, embedder=_OtherModel()) == ()


def test_only_the_semantic_arm_filters_on_model_version() -> None:
    """Stated structurally, because the behavioural test above can only observe the
    combined result. A lexical arm that filtered on model version would drop rows it
    can legitimately match, and a semantic arm that did not would rank incomparable
    distances against each other."""
    from registry.service.claim_serving import _LEXICAL_ARM_SQL, _SEMANTIC_ARM_SQL

    assert "model_id" in _SEMANTIC_ARM_SQL
    assert "model_id" not in _LEXICAL_ARM_SQL


@pytest.mark.asyncio
async def test_a_paraphrase_and_an_exact_phrase_both_find_the_claim(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Why there are two arms. An exact predicate name is often a poor semantic
    match, and a paraphrase has no lexical overlap at all -- each arm catches what
    the other misses."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject)
    await _drain_all(factory, embedder)

    exact = await serving.retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder)
    assert [c.claim_id for c in exact] == [claim_id]


@pytest.mark.asyncio
async def test_retrieval_still_answers_when_one_arm_finds_nothing(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """An empty arm is absent, not fatal. Its weight is redistributed, so a query
    whose words match nothing lexically still answers from the semantic arm rather
    than returning nothing and looking broken."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject)
    await _drain_all(factory, embedder)

    # No word here appears in "owned by team: platform", so the lexical arm is empty.
    found = await serving.retrieve(_ctx(tid, aid), query="escalation contact rotation", embedder=embedder)
    assert [c.claim_id for c in found] == [claim_id]


@pytest.mark.asyncio
async def test_reindexing_replaces_rather_than_duplicates(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject)
    await _drain_all(factory, embedder)
    await _drain_all(factory, embedder)

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM embeddings WHERE target_type = 'claim' AND target_id = :c"),
                {"c": claim_id},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_a_top_k_beyond_the_maximum_is_refused(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    with pytest.raises(ValueError, match="top_k must be"):
        await serving.retrieve(_ctx(tid, aid), query="anything", embedder=_TokenEmbedder(), top_k=101)


class _DecoyEmbedder(_TokenEmbedder):
    """Ranks a chosen decoy first, whatever the query says.

    A token-hash embedder happens to agree with lexical matching almost always, so
    with it the lexical arm never changes an answer and a test cannot tell whether
    fusion is doing anything. This one disagrees on purpose: it embeds every query
    as the decoy's text, so the semantic arm is confidently wrong and only the
    lexical arm can rescue the right claim.
    """

    def __init__(self, decoy_text: str, dim: int = 384) -> None:
        super().__init__(dim=dim)
        self._decoy = decoy_text

    def encode(self, texts: list[str]) -> Any:
        return super().encode([self._decoy for _ in texts])


@pytest.mark.asyncio
async def test_the_lexical_arm_overturns_a_confident_semantic_miss(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Why the second arm earns its place.

    The semantic arm here ranks the wrong claim first and is sure about it. The
    right claim is second by vector distance and first by text, and fusion adds the
    two ranks -- so it finishes ahead. With one arm, the confident miss wins.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    # The decoy exists to be ranked first by the misleading embedder below; the test
    # asserts the lexical arm overturns it, so only the target's id is needed.
    await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")
    target = await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://runbooks/auth", at=5)

    indexer = _TokenEmbedder()
    later = _serving_at(factory, 30)
    # One drain covers both claims: staging and consolidating each one enqueued it, and
    # the drain empties the whole queue.
    assert await _drain_all(factory, indexer) == 2

    misleading = _DecoyEmbedder("owned by team: platform")
    found = await later.retrieve(
        _ctx(tid, aid), query="runbook url https://runbooks/auth", embedder=misleading, top_k=1
    )

    assert [c.claim_id for c in found] == [target], "the lexical arm did not overturn a confident semantic miss"


# --- one pipeline: the properties unification exists to provide ------------------


@pytest.mark.asyncio
async def test_a_consolidated_claim_becomes_retrievable_end_to_end(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """The thing that did not work before any of this.

    Nothing called the old indexing method outside tests, so in a running deployment the
    claim semantic arm was permanently empty and fusion silently fell back to lexical
    only. Staging and consolidating now enqueues, and the shared drain turns the queue
    into vectors -- no separate worker, no separate table.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject, value="platform")

    # Consolidation enqueued it; nothing has embedded it yet.
    async with factory() as session:
        queued = (
            await session.execute(
                text("SELECT count(*) FROM embedding_outbox WHERE target_type = 'claim' AND target_id = :c"),
                {"c": claim_id},
            )
        ).scalar_one()
    assert queued == 1, "consolidating a claim did not enqueue it"

    assert await _drain_all(factory, embedder) == 1

    found = await serving.retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder)
    assert [c.claim_id for c in found] == [claim_id]
    assert found[0].citations, "a retrieved claim still carries its evidence"


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_never_enqueued(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An unlinked claim has no owning tenant, and the index column is NOT NULL.

    Not defended with a fallback: the schema makes it unreachable. A servable claim
    always has an owner, because the status that permits a null owner is not a servable
    status. This test pins that the projection agrees.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="nothing resolvable",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    async with factory() as session:
        queued = (await session.execute(text("SELECT count(*) FROM embedding_outbox"))).scalar_one()
    assert queued == 0


@pytest.mark.asyncio
async def test_re_draining_the_same_claim_does_not_duplicate_its_vectors(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Duplicates in an ANN index are not cosmetic.

    `ORDER BY vector <=> q LIMIT k` spends candidate slots on copies, so recall degrades
    in proportion to how often the drain retried. The unique key plus delete-then-insert
    is what makes at-least-once delivery safe.
    """
    from registry.service.embedding_index import enqueue, index_text

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    await _drain_all(factory, embedder)

    # Queue the very same target again, as a retry or a second drainer would.
    body = index_text("owned_by_team", "platform")
    async with factory() as session, session.begin():
        await enqueue(
            session,
            tenant_id=tid,
            target_type="claim",
            target_id=claim_id,
            text_to_embed=body,
            chunk_plan=[{"index": 0, "text": body, "start": 0, "end": 3}],
            now=_NOW,
        )
    await _drain_all(factory, embedder)

    async with factory() as session:
        vectors = (
            await session.execute(
                text("SELECT count(*) FROM embeddings WHERE target_type = 'claim' AND target_id = :c"),
                {"c": claim_id},
            )
        ).scalar_one()
        dead = (
            await session.execute(
                text("SELECT count(*) FROM embedding_outbox_failed WHERE target_id = :c"),
                {"c": claim_id},
            )
        ).scalar_one()
        queued = (
            await session.execute(
                text("SELECT count(*) FROM embedding_outbox WHERE target_id = :c"),
                {"c": claim_id},
            )
        ).scalar_one()

    assert vectors == 1, "re-draining duplicated the claim's vectors"
    # And it succeeded rather than failing into the dead-letter table. Without both
    # assertions the test passes when the delete is removed: the insert then violates the
    # unique key, the row dead-letters, and the vector count stays at one for the wrong
    # reason.
    assert dead == 0, "the re-drain failed instead of replacing"
    assert queued == 0, "the re-drain left the request queued"


@pytest.mark.asyncio
async def test_superseding_a_claim_retracts_its_vectors(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """A retired claim's vectors have to go, and not because they would be served.

    The read arms already refuse an unservable claim. The reason to delete is that every
    dead vector occupies a candidate slot in an ANN search, which is a silent recall loss
    on the queries that do matter.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    first = await _stage(factory, tid, aid, subject, value="platform")
    await _drain_all(factory, embedder)

    # A newer claim on the same subject and predicate supersedes it.
    await _stage(factory, tid, aid, subject, value="billing", at=10)
    await _drain_all(factory, embedder)

    async with factory() as session:
        stale = (
            await session.execute(
                text("SELECT count(*) FROM embeddings WHERE target_type = 'claim' AND target_id = :c"),
                {"c": first},
            )
        ).scalar_one()
    assert stale == 0, "the superseded claim kept its vectors"

    found = await _serving_at(factory, 30).retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder)
    assert first not in [c.claim_id for c in found]


@pytest.mark.asyncio
async def test_both_kinds_coexist_and_the_claim_surface_returns_only_claims(
    factory: async_sessionmaker[AsyncSession], serving: ClaimServingService, ontology: None
) -> None:
    """Facts and claims share one table, and the claim surface still answers in claims."""
    from registry.service.embedding_index import enqueue

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject, value="platform")

    fact_target = uuid.uuid4()
    body = "owned by team: platform"
    async with factory() as session, session.begin():
        await enqueue(
            session,
            tenant_id=tid,
            target_type="fact",
            target_id=fact_target,
            text_to_embed=body,
            chunk_plan=[{"index": 0, "text": body, "start": 0, "end": 4}],
            now=_NOW,
        )
    await _drain_all(factory, embedder)

    async with factory() as session:
        kinds = set((await session.execute(text("SELECT DISTINCT target_type FROM embeddings"))).scalars())
    assert kinds == {"fact", "claim"}, "both kinds must be present for this to prove anything"

    found = await serving.retrieve(_ctx(tid, aid), query="owned by team", embedder=embedder)
    assert [c.claim_id for c in found] == [claim_id]
    assert fact_target not in [c.claim_id for c in found]


def test_the_claim_arm_joins_on_target_kind() -> None:
    """Asserted structurally, because behaviour cannot distinguish this one.

    The semantic arm has no similarity threshold -- `ORDER BY vector <=> q LIMIT k` always
    returns the nearest rows -- so a colliding fact row changes what a claim is *ranked
    by*, not whether it appears. And fusion deduplicates by claim id, so a duplicate
    candidate is absorbed before it reaches a result. Membership is therefore the wrong
    observable, and a behavioural test here would pass whether the discriminator was
    present or not.

    What the discriminator prevents is a claim being ranked using text and a vector that
    belong to a fact, which is reachable because `target_id` carries no foreign key. That
    is worth an explicit guard even though only the source can show it.
    """
    from registry.service.claim_serving import _INDEX_JOIN

    assert "emb.target_type = 'claim'" in _INDEX_JOIN, "the claim arms no longer discriminate on target kind"


@pytest.mark.asyncio
async def test_coverage_reads_zero_before_a_drain_and_one_after(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The number that would have made the empty claim index visible.

    Everything else the pipeline reports describes work in flight. This one describes
    whether the index reflects the store, which is the claim a steward is accountable
    for -- and the vision's standard is that a number nobody can check is not a signal.
    """
    from registry.service.embedding_index import index_coverage

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    await _stage(factory, tid, aid, subject, value="platform")

    before = await index_coverage(factory, embedder.model_version, tenant_id=tid)
    assert before["claim"] == 0.0, "a consolidated claim with no vector is not covered"

    await _drain_all(factory, embedder)

    after = await index_coverage(factory, embedder.model_version, tenant_id=tid)
    assert after["claim"] == 1.0


@pytest.mark.asyncio
async def test_coverage_of_an_empty_store_is_full_not_zero(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A fresh deployment and a broken pipeline must not look the same.

    Scoped to a tenant with nothing in it, because coverage is otherwise a
    deployment-wide number and the test database is shared.
    """
    from registry.service.embedding_index import index_coverage

    empty_tenant = await _seed_tenant(factory)
    coverage = await index_coverage(factory, "any-model", tenant_id=empty_tenant)
    assert coverage["claim"] == 1.0
    assert coverage["fact"] == 1.0


def test_the_capability_arm_filters_on_target_kind() -> None:
    """Asserted structurally, because behaviour cannot see this one.

    The capability semantic arm inner-joins `facts`, and a claim's identifier is never a
    fact's, so removing the kind filter changes no result and no behavioural test would
    notice. That is exactly why the filter matters: an exclusion that happens as a side
    effect of a join is a control nobody can find and nobody can break loudly -- it
    survives only until somebody widens the join or makes it outer.

    So the assertion is on the source. It is the same reason the repo already asserts on
    the drain's claim query by source text rather than by outcome.
    """
    import inspect

    from registry.service.retrieval import RetrievalService

    source = inspect.getsource(RetrievalService._semantic_arm)
    assert (
        "emb.target_type = :target_type" in source
    ), "the capability semantic arm no longer excludes non-fact rows explicitly"


# --- erasure: the vectors go too -------------------------------------------------


@pytest.mark.asyncio
async def test_erasure_removes_an_actors_vectors_from_every_table(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Closes a live defect rather than tidying one.

    Nothing in the product deleted from `embeddings`. A right-to-be-forgotten request
    reported success while the erased person's claim values sat in `text_chunk`, verbatim
    and still returned by the semantic arm -- because a vector row is not a summary, it
    carries the source text.

    Covers all three tables. The dead-letter table matters for the same reason the session
    eraser covers its own: it holds the actor's text plus a stored error string, and a row
    that failed to embed is not a row that stopped being personal data.
    """
    from registry.service.embedding_index import EmbeddingIndex, enqueue

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    embedder = _TokenEmbedder()

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    await _drain_all(factory, embedder)

    # A queued row and a dead-lettered row for the same actor, so all three tables have
    # something to lose.
    async with factory() as session, session.begin():
        await enqueue(
            session,
            tenant_id=tid,
            target_type="claim",
            target_id=claim_id,
            text_to_embed="owned by team: platform",
            chunk_plan=[{"index": 0, "text": "owned by team: platform", "start": 0, "end": 4}],
            now=_NOW,
        )
        await session.execute(
            text(
                "INSERT INTO embedding_outbox_failed "
                "  (failed_id, tenant_id, target_type, target_id, text_to_embed, "
                "   chunk_plan, failed_at, error_text, attempts) "
                "VALUES (gen_random_uuid(), :tid, 'claim', :c, 'owned by team: platform', "
                "        '[]'::jsonb, :now, 'boom', 5)"
            ),
            {"tid": tid, "c": claim_id, "now": _NOW},
        )

    index = EmbeddingIndex(factory)
    removed = await index.erase_actor(_ctx(tid, aid), aid)

    assert removed["vectors"] >= 1, f"no vectors erased: {removed}"
    assert removed["queued"] >= 1, f"no queued rows erased: {removed}"
    assert removed["dead_lettered"] >= 1, f"no dead-lettered rows erased: {removed}"

    async with factory() as session:
        for table in ("embeddings", "embedding_outbox", "embedding_outbox_failed"):
            left = (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE target_id = :c"),  # noqa: S608
                    {"c": claim_id},
                )
            ).scalar_one()
            assert left == 0, f"{table} still holds the erased actor's rows"


@pytest.mark.asyncio
async def test_erasing_twice_removes_nothing_the_second_time(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Idempotent, because a failed erasure has to be safe to retry.

    The registry propagates a participant failure rather than collecting it, precisely so
    a partial erasure is retried rather than reported as done -- which only works if a
    second run over already-erased data is harmless.
    """
    from registry.service.embedding_index import EmbeddingIndex

    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    await _stage(factory, tid, aid, subject, value="platform")
    await _drain_all(factory, _TokenEmbedder())

    index = EmbeddingIndex(factory)
    first = await index.erase_actor(_ctx(tid, aid), aid)
    second = await index.erase_actor(_ctx(tid, aid), aid)

    assert first["vectors"] >= 1
    assert second == {"vectors": 0, "queued": 0, "dead_lettered": 0}


@pytest.mark.asyncio
async def test_erasure_does_not_reach_another_tenants_rows(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Scoped by tenant as well as actor.

    The actor id alone would find the rows. The tenant predicate is there so a request
    made in one tenant's context cannot delete another's, matching how session-memory
    erasure is scoped.
    """
    from registry.service.embedding_index import EmbeddingIndex

    victim = await _seed_tenant(factory)
    bystander = await _seed_tenant(factory)
    victim_actor = await _seed_actor(factory, victim)
    bystander_actor = await _seed_actor(factory, bystander)
    bystander_subject = await _seed_entity(factory, bystander)

    bystander_claim = await _stage(factory, bystander, bystander_actor, bystander_subject)
    await _drain_all(factory, _TokenEmbedder())

    # An erasure run in the victim's tenant, naming the bystander's actor id.
    removed = await EmbeddingIndex(factory).erase_actor(_ctx(victim, victim_actor), bystander_actor)
    assert removed == {"vectors": 0, "queued": 0, "dead_lettered": 0}

    async with factory() as session:
        survived = (
            await session.execute(
                text("SELECT count(*) FROM embeddings WHERE target_id = :c"),
                {"c": bystander_claim},
            )
        ).scalar_one()
    assert survived >= 1, "another tenant's vectors were erased"


def test_the_claim_lexical_arm_reads_the_stored_tsvector() -> None:
    """Structural, because the result is identical either way -- only the cost differs.

    The retired claim-scoped index had no stored tsvector, so the lexical arm called
    `to_tsvector` twice per candidate row on every request, with no supporting index. The
    shared table has a generated STORED column and a GIN index over it. A behavioural test
    cannot tell which one ran; this can.
    """
    from registry.service.claim_serving import _LEXICAL_ARM_SQL

    assert "emb.ts_vector" in _LEXICAL_ARM_SQL, "the lexical arm stopped using the stored column"
    assert (
        "to_tsvector(" not in _LEXICAL_ARM_SQL
    ), "the lexical arm is tokenising per row again instead of reading the stored column"
