"""An embedder that cannot rank must be absent from a ranked read, not quiet in it.

`StubEmbedder` returns zero vectors and documents what that means:

    Search still returns rows -- every distance is identical, so the ranking is
    arbitrary and the lexical arm decides the order.

The first half is true and the second was not. Arbitrary is not neutral. The
semantic arm carries the largest of entity search's three weights and the larger
of claim retrieval's two, so on any deployment running the stub — every
`make dev-up`, every smoke stack, every test in this tree — the biggest single
contribution to the fused score was assigned to rows chosen by nothing.

Measured on `eval/fixtures/search_questions.json`, changing nothing else:

    | metric       | arm present | arm absent |
    |--------------|-------------|------------|
    | recall@10    | 1.000       | 1.000      |
    | R-precision  | 0.597-0.610 | 0.895      |
    | precision@1  | 0.660-0.740 | 0.980      |

The ranges are the point as much as the numbers. Each run of that suite seeds a
fresh corpus with fresh identities, and the arm's rows moved with them — so the
same fifty questions scored differently from one run to the next. With the arm
absent, two runs agree exactly. (Within a single database the arm is stable, so
no test here can show that; `test_retrieval_relevance.py` is where it surfaces,
and its numbers above are the record.)

`fuse_hybrid_arms` already had the concept — an arm left out of both the arms map
and the weights map is *absent*, and `redistribute_weights` hands its share to the
arms that can still answer. Leaving it out of the arms map alone would be worse
than the bug: it would hold its weight while contributing nothing, lowering every
score by omission, which is the case that function's docstring warns about.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from contextplane.config import Settings
from contextplane.embedding.stub import STUB_MODEL_VERSION, StubEmbedder
from contextplane.service.retrieval import RetrievalService
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.storage.pg import create_engine, get_session_factory
from contextplane.types import TemporalFilter, TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.eval_corpus import load_search_questions, seed_eval_entities

_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def seeded(pg_container: str) -> AsyncIterator[tuple[RetrievalService, TenantContext]]:
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    tenant_id, actor_id, _map = await seed_eval_entities(pg_container)
    engine = create_engine(settings)
    factory = get_session_factory(engine)
    embedder = StubEmbedder()
    await drain_outbox(factory, embedder, settings)
    try:
        yield (
            RetrievalService(factory, FakeClock(_NOW), embedder, settings),
            TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stub_embedders_arm_contributes_nothing_to_the_fused_score(
    seeded: tuple[RetrievalService, TenantContext],
) -> None:
    """The invariant, read off the result rather than off the implementation.

    Every `SearchResult` carries `retrieval_arms`, the per-arm breakdown fusion
    accumulated. If the semantic arm ran, its name appears there. Asserting on
    that rather than on a call count means the test still holds if the arm is
    skipped somewhere else, and fails if it is reinstated anywhere.
    """
    service, ctx = seeded
    assert service._embedder.model_version == STUB_MODEL_VERSION, "fixture no longer uses the stub"

    results = await service.search(ctx, "How does the payment-service handle refunds?", 10, TemporalFilter(as_of=None))

    assert results, "the term-reading arms must still answer"
    contributing = {arm for result in results for arm in result.retrieval_arms}
    assert "semantic" not in contributing, (
        f"an embedder that returns zero vectors contributed to the ranking: {sorted(contributing)}. "
        "Every distance ties, so the rows it supplied were chosen by nothing and carried the "
        "largest of the three weights."
    )
    assert contributing <= {"lexical", "graph"}


@pytest.mark.asyncio
async def test_the_surviving_arms_share_the_whole_score(
    seeded: tuple[RetrievalService, TenantContext],
) -> None:
    """Absent, not silent — and the difference is visible in the numbers.

    Dropping the arm from the arms map alone would leave its weight assigned to a
    contributor that answers nothing, so every fused score would fall by that
    share and the ranking would read as though every result got worse. Weights
    that sum to one after redistribution are what stop that: the best result's
    top-ranked contribution is the surviving arms' full weight, not a fraction of
    it.
    """
    service, ctx = seeded
    results = await service.search(ctx, "Which capability owns the ingest-pipeline?", 10, TemporalFilter(as_of=None))

    assert results
    assert sum(results[0].retrieval_arms.values()) > 0.0
    assert results[0].fused_rank_score == pytest.approx(sum(results[0].retrieval_arms.values()))
    # Rank decay is 1/(rank+1), so an arm's contribution to its own top row is its
    # full weight. Summing the arms' first-place contributions across the whole
    # result set therefore recovers the redistributed total, which must be 1.0 —
    # anything less means a weight was dropped rather than reassigned.
    best_per_arm: dict[str, float] = {}
    for result in results:
        for arm, contribution in result.retrieval_arms.items():
            best_per_arm[arm] = max(best_per_arm.get(arm, 0.0), contribution)
    assert sum(best_per_arm.values()) == pytest.approx(
        1.0
    ), f"the absent arm's weight was not redistributed: {best_per_arm}"


@pytest.mark.asyncio
async def test_every_corpus_question_is_still_answered(
    seeded: tuple[RetrievalService, TenantContext],
) -> None:
    """Removing an arm must not remove recall, which is the obvious way this could go wrong.

    The stub arm was contributing noise, but it was contributing *rows*, and an
    arm that returns rows can carry a question the other two miss. It did not —
    recall@10 is 1.000 either way on the measured corpus — and this is the cheap
    always-on version of that check: every question comes back with something.
    """
    service, ctx = seeded
    empty = [
        question["question"]
        for question in load_search_questions()
        if not await service.search(ctx, question["question"], 10, TemporalFilter(as_of=None))
    ]
    assert empty == [], f"{len(empty)} question(s) returned nothing once the stub arm was dropped: {empty[:3]}"


def test_the_stub_still_marks_its_vectors_as_fake() -> None:
    """The discriminator this decision rests on.

    `STUB_MODEL_VERSION` is what tells a ranked read that its embedder has no
    signal. `stub.py` already forbids changing it — *"Do not change this to a real
    model id — that is precisely how fake vectors become indistinguishable from
    real ones"* — and now a second thing depends on it, so the constraint is
    asserted where the dependency is rather than only stated where it is defined.
    """
    assert StubEmbedder(dim=8).model_version == STUB_MODEL_VERSION
    assert STUB_MODEL_VERSION == "stub-zero"
    assert (
        StubEmbedder(dim=8).encode(["anything"]) == 0
    ).all(), "the stub returns zero vectors; if it ever returns real ones this whole exclusion is wrong"
