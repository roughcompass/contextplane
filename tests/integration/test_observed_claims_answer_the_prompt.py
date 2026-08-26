"""The observed-claims block, against a corpus large enough to be wrong about.

The unit tests for ADR 0027 assert *which read the arm chooses*, which is the
behaviour and is worth asserting cheaply. They cannot show that choosing
correctly produces a better answer, because a fake returns whatever it is told
to. Only a real corpus with claims about more than one subject can.

That distinction is not academic here. The development stack holds four claims in
total, so every read — recency or ranked — returns all of them, and the reported
symptom (*"Who owns salt design system?"* answering with claims about
`memory-loop-demo`) is invisible on it. A corpus too small to be wrong about is
also too small to verify against.

So this seeds two subjects with clearly distinct vocabulary and asks about one of
them. Under the recency read the answer was whichever claims were written last;
under the ranked read it is the ones the question is about.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.config import Settings
from contextplane.context.arms import BLOCK_OBSERVED_CLAIMS, ContextArms
from contextplane.context.assembler import assemble
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import Evidence
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.service.retrieval.embedding_drain import drain_outbox
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
#: Read from an hour after the last write. `created_at <= as_of` is deliberate
#: in claim serving: a claim written after the instant asked about was not
#: believed then, so a test that stages claims has to read from further forward.
_MOMENT = _NOW + datetime.timedelta(hours=1)
_DSN = "postgresql+asyncpg://unused/unused"

#: Two subjects whose claims share no vocabulary, so a wrong answer is
#: unmistakable rather than merely lower-ranked. The distractors are written
#: *last* on purpose: under the recency read they are what the block returned,
#: whatever was asked, which is the failure this file exists to catch.
_SUBJECT_CLAIMS = [
    ("checkout", "exposes_operation", "checkout payments refund authorization scope"),
    ("checkout", "escalation_contact", "checkout payments refund duty engineer"),
    ("telemetry", "exposes_operation", "telemetry metrics ingestion sampling window"),
    ("telemetry", "escalation_contact", "telemetry metrics ingestion duty engineer"),
]


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _settings() -> Settings:
    return Settings(
        database_url=_DSN,
        pgbouncer_url=_DSN,
        scheduler_jobstore_url=_DSN,
        embedding_provider="stub",
    )


class _NoRetrieval:
    """The canonical arm is not under test, and must not answer for the claims arm."""

    async def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


class _NoReceipts:
    async def get_receipt(self, *_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("the ARC arm is not exercised here")


class _NoRecall:
    def lexical_arm(self, **_kwargs: Any) -> Any:
        async def arm() -> Any:
            from contextplane.context.assembler import ArmOutcome

            return ArmOutcome()

        return arm

    reference_arm = lexical_arm
    intent_arm = lexical_arm
    participant_arm = lexical_arm


async def _seed(factory: async_sessionmaker[AsyncSession], pg_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Two subjects, four claims, distractors written last."""
    from sqlalchemy import text

    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tenant_id, "s": f"claims-{tenant_id.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, actor_kind, created_at) "
                "VALUES (:a, :t, 'seeder', :sub, 'human', :n)"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"s-{actor_id.hex[:8]}", "n": _NOW},
        )
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))

    # `seed_entity` names its own entity; the claims carry the vocabulary that
    # distinguishes the two subjects, which is what the ranked read reads.
    subjects = {name: await _seed_entity(factory, tenant_id) for name in ("checkout", "telemetry")}

    for offset, (subject, predicate, value) in enumerate(_SUBJECT_CLAIMS):
        clock = FakeClock(_NOW + datetime.timedelta(minutes=offset))
        claim = await ClaimService(factory, clock=clock).stage_claim(
            _ctx(tenant_id, actor_id),
            subject_reference=str(subjects[subject]),
            predicate=predicate,
            value=value,
            evidence=(Evidence(kind="session_event", ref="e1", excerpt=value),),
        )
        await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)

    await drain_outbox(factory, StubEmbedder(), _settings())
    return tenant_id, actor_id


def _arms(factory: async_sessionmaker[AsyncSession], *, embedder: Any) -> ContextArms:
    clock = FakeClock(_MOMENT)
    return ContextArms(
        session_factory=factory,
        retrieval=_NoRetrieval(),  # type: ignore[arg-type]
        claims=ClaimServingService(factory, clock=clock),
        arc_receipts=_NoReceipts(),  # type: ignore[arg-type]
        recall=_NoRecall(),  # type: ignore[arg-type]
        instructions=None,  # type: ignore[arg-type]
        embedder=embedder,
    )


async def _claim_values(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    query: str | None,
    embedder: Any,
) -> list[str]:
    from contextplane.types import TenantContext

    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    arm = _arms(factory, embedder=embedder).observed_claims_arm(ctx, query=query, moment=_MOMENT, limit=2)
    envelope = (await assemble({BLOCK_OBSERVED_CLAIMS: arm}, now=_MOMENT)).envelope
    return [str(item.payload.get("value")) for item in envelope.block(BLOCK_OBSERVED_CLAIMS).items]


@pytest.mark.asyncio
async def test_two_questions_over_one_corpus_get_two_different_answers(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """ "Scoped by the query" is a claim about *two* reads, and needs two to test.

    A block that returned checkout claims for everything would satisfy any single
    question about checkout. Asking both questions over the identical corpus, at
    the identical instant, with only the query differing, is what distinguishes
    scoping from luck — and under the recency read the two answers were
    necessarily identical, because nothing in the read depended on the question.

    `limit=2` against four claims is the other half. With a bound at or above the
    corpus every read returns everything and there is nothing to choose between;
    the bound has to bite.
    """
    tenant_id, actor_id = await _seed(factory, pg_container)

    about_checkout = await _claim_values(
        factory,
        tenant_id,
        actor_id,
        query="which authorization scope does checkout need for refunds",
        embedder=StubEmbedder(),
    )
    about_telemetry = await _claim_values(
        factory,
        tenant_id,
        actor_id,
        query="what is the telemetry ingestion sampling window",
        embedder=StubEmbedder(),
    )

    assert about_checkout and about_telemetry
    assert set(about_checkout) != set(about_telemetry), (
        f"both questions returned {about_checkout} — the block is not reading the query at all, "
        "which is the recency read this change replaced"
    )
    assert any("checkout" in value for value in about_checkout), f"asked about checkout and got {about_checkout}"
    assert any("telemetry" in value for value in about_telemetry), f"asked about telemetry and got {about_telemetry}"


@pytest.mark.asyncio
async def test_the_query_scoped_read_returns_something_the_recency_read_did_not(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The contrast, pinned, so a revert fails on behaviour rather than on a signature.

    Removing the `query` parameter breaks the tests above with a `TypeError`,
    which says the API changed and not that the answer got worse. The realistic
    regression is subtler — somebody simplifies the branch and every call takes
    the structural read again, with the parameter still accepted and ignored.
    This is what catches that: both reads, same corpus, same instant, same bound,
    differing only in whether a question was asked.
    """
    tenant_id, actor_id = await _seed(factory, pg_container)

    asked = await _claim_values(
        factory,
        tenant_id,
        actor_id,
        query="which authorization scope does checkout need for refunds",
        embedder=StubEmbedder(),
    )
    unasked = await _claim_values(factory, tenant_id, actor_id, query=None, embedder=StubEmbedder())

    assert asked and unasked
    assert set(asked) != set(unasked), (
        f"asking a question changed nothing: both reads returned {asked}. The block is taking "
        "the recency path regardless of the query, which is the behaviour ADR 0027 replaced."
    )


@pytest.mark.asyncio
async def test_the_lexical_arm_alone_puts_only_the_asked_subject_in_reach(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Precision, asserted where a stub embedder cannot spoil it.

    The test above deliberately asserts *difference* rather than purity, because
    the ranked read fuses a semantic arm with a lexical one and this suite runs
    on `StubEmbedder` — zero vectors, so every distance ties and the semantic arm
    contributes an arbitrary handful. Measured: asking about telemetry returned
    the right telemetry claim alongside a checkout distractor the semantic arm
    supplied. That is a property of the fixture's embedder, not of the change,
    and a test that asserted purity through the fused read would be asserting
    that this deployment has no vectors.

    So precision is asserted one level down, on the arm that actually reads the
    query's terms. A real deployment's semantic arm adds recall on top of this;
    it cannot subtract the precision shown here.
    """
    tenant_id, actor_id = await _seed(factory, pg_container)
    from contextplane.types import TenantContext

    serving = ClaimServingService(factory, clock=FakeClock(_MOMENT))
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])

    async with factory() as session:
        # Private, deliberately: there is no public read of one arm, and the
        # point of this test is to isolate the arm the fused read hides.
        rows = await serving._fused_candidates(
            session,
            tenant_id=tenant_id,
            query="which authorization scope does checkout need for refunds",
            vector=[0.0] * 384,
            model_version="never-matches-a-stored-vector",
            categories=["interface_contract", "ownership_stewardship"],
            category=None,
            namespace_prefix=None,
            now=_MOMENT,
            top_k=2,
        )

    values = [str(row["value"]) for row in rows]
    assert values, "the lexical arm found nothing for a question the corpus answers"
    assert all(
        "checkout" in value for value in values
    ), f"the term-reading arm was asked about checkout and returned {values}"
    assert ctx.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_a_deployment_that_cannot_rank_still_answers_and_says_it_could_not_rank(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The fallback, against the real read rather than a fake.

    No embedder means no ranking, so the block serves recency — the previous
    behaviour, which is the right fallback and the wrong answer to the question
    asked. It must still be an answer, and it must still say so.
    """
    tenant_id, actor_id = await _seed(factory, pg_container)
    from contextplane.types import TenantContext

    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    arm = _arms(factory, embedder=None).observed_claims_arm(
        ctx,
        query="which authorization scope does checkout need for refunds",
        moment=_MOMENT,
        limit=2,
    )
    envelope = (await assemble({BLOCK_OBSERVED_CLAIMS: arm}, now=_MOMENT)).envelope
    block = envelope.block(BLOCK_OBSERVED_CLAIMS)

    assert block.items, "a deployment without an embedder must still serve claims"
    assert block.reason is not None and "embedder" in block.reason
