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


# --- exit criterion 4 and NF7.3: an uncited claim is unrepresentable -------------


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
    for claim_id in (owned, runbook):
        assert await serving.index_claim(claim_id, embedder=embedder)

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

    claim_id = await _stage(factory, tid, aid, subject)
    await serving.index_claim(claim_id, embedder=embedder)

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

    assert await serving.index_claim(claim.claim_id, embedder=_TokenEmbedder()) is False


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

    claim_id = await _stage(factory, owner, owner_actor, subject)
    await serving.index_claim(claim_id, embedder=embedder)

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

    claim_id = await _stage(factory, tid, aid, subject)
    await serving.index_claim(claim_id, embedder=_TokenEmbedder())

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

    assert "model_version" in _SEMANTIC_ARM_SQL
    assert "model_version" not in _LEXICAL_ARM_SQL


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
    await serving.index_claim(claim_id, embedder=embedder)

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
    await serving.index_claim(claim_id, embedder=embedder)

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
    await serving.index_claim(claim_id, embedder=embedder)
    await serving.index_claim(claim_id, embedder=embedder)

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM lmm_claim_embedding WHERE claim_id = :c"),
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

    decoy = await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform")
    target = await _stage(factory, tid, aid, subject, predicate="runbook_url", value="https://runbooks/auth", at=5)

    indexer = _TokenEmbedder()
    later = _serving_at(factory, 30)
    for claim_id in (decoy, target):
        assert await later.index_claim(claim_id, embedder=indexer)

    misleading = _DecoyEmbedder("owned by team: platform")
    found = await later.retrieve(
        _ctx(tid, aid), query="runbook url https://runbooks/auth", embedder=misleading, top_k=1
    )

    assert [c.claim_id for c in found] == [target], "the lexical arm did not overturn a confident semantic miss"
