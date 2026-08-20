"""Precision, measured from what the receipt says was served.

`recall@10` asks whether the right answer was anywhere in the top ten. It cannot
notice a retriever that returns the right answer alongside nine wrong ones, and
that retriever is the one that wastes an agent's context window. Precision is the
other half, and it needs something recall does not: the **complete** set of
relevant entities per question, so that anything else returned is a false
positive. `eval/fixtures/search_questions.json` holds an any-one-of set, which is
the right shape for recall and the wrong shape for precision.
`eval/fixtures/retrieval_relevance.json` holds the complete sets.

**The join is against the receipt, not against the return value.** Every question
goes through `ContextResolver.resolve`, and the served entity ids are read back
out of `context_receipt_items` — the same rows an auditor would read. That is the
point of measuring it this way: a receipt that under-records what was served is
worse than no receipt, and this is the only gate in the tree that would notice.
It is why the module asserts the receipt and the envelope agree item for item
before scoring anything; if they disagree, the precision figure is the second
question and the receipt is the first.

**Report first, threshold later**, as with the extraction ground truth. The one
assertion about quality is the existing recall floor, recomputed here from the
receipt so that the two gates cannot drift into measuring different things.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.types import ArcRequestContext
from contextplane.config import Settings
from contextplane.context.arms import BLOCK_CANONICAL
from contextplane.context.resolve import ContextResolver
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.retrieval import RetrievalService
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.storage.pg import create_engine, get_session_factory
from contextplane.types import TenantContext
from contextplane.workspaces.wiring import build_layered_context_services
from tests.helpers.clock import FakeClock
from tests.helpers.eval_corpus import (
    SEARCH_QUESTION_COUNT,
    load_search_questions,
    seed_eval_entities,
)

_RELEVANCE_FILE = pathlib.Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "retrieval_relevance.json"

#: The existing recall gate's bar, recomputed here from the receipt. Not a new
#: threshold — the same one, asserted against a different source of truth, so a
#: receipt that stops recording what was served fails a quality gate rather than
#: only an audit one.
_RECALL_FLOOR = 0.70

_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)


class _NoCapabilities:
    """The visibility collaborator ARC authorization takes, answering nothing.

    None of these fifty questions names an ARC receipt, so the ARC arm is empty
    on every one of them and this collaborator is never consulted. It raises
    rather than returning an empty list so that a future change quietly routing
    this path through capability visibility fails loudly instead of measuring
    something else.
    """

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        raise AssertionError("resolving these eval questions must not consult capability visibility")


def _load_relevance() -> dict[str, dict[str, Any]]:
    document = json.loads(_RELEVANCE_FILE.read_text())
    judgments: list[dict[str, Any]] = document["judgments"]
    assert (
        len(judgments) == SEARCH_QUESTION_COUNT
    ), f"expected {SEARCH_QUESTION_COUNT} relevance judgments, found {len(judgments)}"
    return {row["question_id"]: row for row in judgments}


# --- the fixture contract -------------------------------------------------------


def test_every_question_has_a_relevance_judgment() -> None:
    """A question with no judgment would be silently dropped from precision while
    still counting toward recall, and the two figures would describe different
    question sets under one heading."""
    judged = _load_relevance()
    for question in load_search_questions():
        assert question["id"] in judged


def test_every_judged_entity_exists_in_the_corpus() -> None:
    """An id naming nothing can never be returned, so it would depress recall
    forever and be invisible in the report."""
    from tests.helpers.eval_corpus import EVAL_ENTITIES

    known = {entity["id"] for entity in EVAL_ENTITIES}
    for row in _load_relevance().values():
        assert set(row["relevant_entity_ids"]) <= known
        assert set(row["borderline_entity_ids"]) <= known


def test_the_relevance_set_contains_the_recall_set() -> None:
    """Recall says finding any one of these counts as a hit, so each of them is
    relevant by construction. A relevance set that dropped one would score the
    two gates against contradictory ground truth."""
    judged = _load_relevance()
    for question in load_search_questions():
        row = judged[question["id"]]
        scored = set(row["relevant_entity_ids"]) | set(row["borderline_entity_ids"])
        missing = set(question["expected_entity_ids"]) - scored
        assert not missing, f"{question['id']}: recall entities absent from the relevance judgment: {sorted(missing)}"


def test_relevant_and_borderline_do_not_overlap() -> None:
    """Borderline means excluded from scoring. An id in both would be counted and
    excluded at once, and which won would depend on the order of two set
    operations."""
    for row in _load_relevance().values():
        assert not set(row["relevant_entity_ids"]) & set(row["borderline_entity_ids"])


def test_no_question_is_judged_relevant_to_everything() -> None:
    """A permissive relevance set cannot distinguish a good retriever from a bad
    one, because almost everything counts as a hit. Half the corpus is the loosest
    a single question should ever be."""
    from tests.helpers.eval_corpus import EVAL_ENTITY_COUNT

    for question_id, row in _load_relevance().items():
        assert len(row["relevant_entity_ids"]) <= EVAL_ENTITY_COUNT // 2, f"{question_id} is judged too permissively"


def test_every_question_has_at_least_one_relevant_entity() -> None:
    """Precision over an empty relevant set is zero for any answer, which would
    make the question unanswerable rather than hard."""
    for question_id, row in _load_relevance().items():
        assert row["relevant_entity_ids"], f"{question_id} has no relevant entity"


# --- precision -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PrecisionAtK:
    """Counts, not ratios, so questions combine by adding.

    `borderline` is tracked separately rather than folded into either side. An
    entity two judges could not agree on tells you nothing about the retriever,
    and scoring it either way puts the disagreement into the number.
    """

    hits: int
    misses: int
    borderline: int

    @property
    def precision(self) -> float | None:
        scored = self.hits + self.misses
        return self.hits / scored if scored else None

    def __add__(self, other: PrecisionAtK) -> PrecisionAtK:
        return PrecisionAtK(
            hits=self.hits + other.hits,
            misses=self.misses + other.misses,
            borderline=self.borderline + other.borderline,
        )


def precision_at_k(
    *, served: list[uuid.UUID], relevant: set[uuid.UUID], borderline: set[uuid.UUID], k: int
) -> PrecisionAtK:
    """How much of the top *k* a caller would have been right to read.

    Fixed *k* over a twenty-entity corpus has a ceiling that is a fact about the
    corpus rather than about the retriever: a question with two relevant entities
    cannot exceed 0.2 at k=10 however perfectly it is answered. `precision_at_r`
    below is the figure that is not capped that way; both are reported, because a
    reader who sees only the capped one will conclude the retriever is bad.
    """
    top = served[:k]
    return PrecisionAtK(
        hits=sum(1 for e in top if e in relevant),
        misses=sum(1 for e in top if e not in relevant and e not in borderline),
        borderline=sum(1 for e in top if e in borderline),
    )


def precision_at_r(*, served: list[uuid.UUID], relevant: set[uuid.UUID], borderline: set[uuid.UUID]) -> PrecisionAtK:
    """Precision over as many results as there are relevant entities.

    R-precision: for a question with three right answers, how many of the top
    three are right. It has no corpus-size ceiling — a perfect answer scores 1.0
    whether the question has one relevant entity or five — which is what makes it
    the comparable figure across a corpus this small.
    """
    return precision_at_k(served=served, relevant=relevant, borderline=borderline, k=len(relevant))


def test_a_perfect_answer_scores_one() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    score = precision_at_k(served=[a, b], relevant={a, b}, borderline=set(), k=10)
    assert score.precision == 1.0


def test_an_irrelevant_result_costs_precision() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    score = precision_at_k(served=[a, b], relevant={a}, borderline=set(), k=10)
    assert score.precision == 0.5


def test_a_borderline_result_costs_nothing_either_way() -> None:
    """The property that keeps two judges' disagreement out of the number."""
    a, b = uuid.uuid4(), uuid.uuid4()
    score = precision_at_k(served=[a, b], relevant={a}, borderline={b}, k=10)
    assert score.precision == 1.0
    assert score.borderline == 1


def test_results_past_k_are_not_counted() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    score = precision_at_k(served=[a, b, c], relevant={a, b}, borderline=set(), k=2)
    assert score.precision == 1.0


def test_an_empty_answer_has_no_precision_rather_than_perfect_precision() -> None:
    """A retriever that returns nothing has returned nothing wrong. Scoring that
    as 1.0 puts the best score in the hands of a broken index."""
    assert precision_at_k(served=[], relevant={uuid.uuid4()}, borderline=set(), k=10).precision is None


def test_scores_combine_by_adding() -> None:
    assert PrecisionAtK(3, 1, 0) + PrecisionAtK(1, 2, 1) == PrecisionAtK(4, 3, 1)


def test_r_precision_is_not_capped_by_how_many_results_were_returned() -> None:
    """The property fixed-k lacks on a twenty-entity corpus: a perfect answer
    scores 1.0 whether the question has one right answer or three."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    padding = [uuid.uuid4() for _ in range(7)]

    one = precision_at_r(served=[a, *padding], relevant={a}, borderline=set())
    three = precision_at_r(served=[a, b, c, *padding], relevant={a, b, c}, borderline=set())
    assert one.precision == 1.0
    assert three.precision == 1.0

    # And the same answers scored at a fixed ten are both dragged down by the
    # padding, by different amounts, for no reason a reader could act on.
    assert precision_at_k(served=[a, *padding], relevant={a}, borderline=set(), k=10).precision == 0.125
    assert precision_at_k(served=[a, b, c, *padding], relevant={a, b, c}, borderline=set(), k=10).precision == 0.3


def test_r_precision_punishes_a_right_answer_buried_under_wrong_ones() -> None:
    """What it is for: the relevant entity is returned, so recall is satisfied,
    and it is below three irrelevant ones, so the agent reads them first."""
    a = uuid.uuid4()
    buried = precision_at_r(served=[uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), a], relevant={a}, borderline=set())
    assert buried.precision == 0.0


# --- the measurement --------------------------------------------------------------


async def _resolver(pg_url: str) -> tuple[ContextResolver, async_sessionmaker[Any], Any]:
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        embedding_provider="stub",
    )
    engine = create_engine(settings)
    factory = get_session_factory(engine)
    clock = FakeClock(_NOW)
    embedder = StubEmbedder()
    await drain_outbox(factory, embedder, settings)

    services = build_layered_context_services(
        factory,
        clock,
        retrieval=RetrievalService(factory, clock, embedder, settings),
        claim_serving=ClaimServingService(factory, clock=clock),
        arc_receipt_reader=ReceiptReader(factory, authorization=ArcAuthorizationService(visibility=_NoCapabilities())),
    )
    return services.context_resolver, factory, engine


async def _served_from_receipt(factory: async_sessionmaker[Any], receipt_id: uuid.UUID) -> set[uuid.UUID]:
    """The catalog entities a receipt records as served. A **set**, not a list.

    `context_receipt_items` has no position column, no rank and no score — only
    `receipt_item_id`, `block`, `source`, `item_key` and the trust fields. So a
    receipt records *which* items were served and not *in what order*, and the
    only honest thing to return here is an unordered set. Anything ordered would
    be ordering by the digest, which is a hash of the entity id and means nothing.

    That is a finding about the product rather than an inconvenience for the
    test, and it is written up in `eval/EVAL.md`.
    """
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT item_key FROM context_receipt_items WHERE receipt_id = :rid AND block = :block"),
                {"rid": receipt_id, "block": BLOCK_CANONICAL},
            )
        ).all()
    return {uuid.UUID(row.item_key) for row in rows}


def _ranked_from_envelope(envelope: Any) -> list[uuid.UUID]:
    """The canonical block in fused-rank order, highest first.

    Reconstructed from `payload["score"]` rather than taken as given, because the
    block's own item order is the digest order `ordered_items` imposes — chosen so
    that two resolutions over unchanged data produce the same order, which it does,
    and which is not rank order. A consumer reading the block top-down is reading
    a hash.
    """
    canonical = next(b for b in envelope.blocks if b.name == BLOCK_CANONICAL)
    scored = [
        (float(item.payload["score"]), str(item.payload["entity_id"]), item)  # type: ignore[arg-type]
        for item in canonical.items
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [uuid.UUID(entity_id) for _, entity_id, _ in scored]


@pytest.mark.asyncio
async def test_precision_and_recall_from_receipts(pg_container: str) -> None:
    """Resolve all fifty questions; report set precision from the receipt and
    rank precision from the envelope, and say why those are two different reads.

    Asserts three things and reports the rest:

    - the receipt records exactly the set the envelope served, for every question;
    - every question was measured, so the figure describes fifty and not a subset;
    - recall@10 recomputed from the receipt still clears the existing floor.

    No precision threshold. What to demand of it is a decision to make after
    seeing what it does, and a threshold chosen in the same commit as the first
    measurement is a threshold chosen to pass.
    """
    judged = _load_relevance()
    questions = load_search_questions()

    tenant_id, actor_id, fixture_to_entity = await seed_eval_entities(pg_container)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    resolver, factory, engine = await _resolver(pg_container)

    try:
        as_a_set = PrecisionAtK(0, 0, 0)
        at_1 = PrecisionAtK(0, 0, 0)
        at_r = PrecisionAtK(0, 0, 0)
        recall_hits = 0
        measured = 0
        worst: list[tuple[float, str]] = []

        for question in questions:
            row = judged[question["id"]]
            relevant = {fixture_to_entity[i] for i in row["relevant_entity_ids"]}
            borderline = {fixture_to_entity[i] for i in row["borderline_entity_ids"]}
            recall_set = {fixture_to_entity[i] for i in question["expected_entity_ids"]}

            resolved = await resolver.resolve(ctx, query=question["question"], moment=_NOW, limit=10)
            served = await _served_from_receipt(factory, resolved.receipt_id)
            ranked = _ranked_from_envelope(resolved.envelope)

            assert served == set(ranked), (
                f"{question['id']}: the receipt and the envelope disagree about what was served — "
                "the receipt is the audit record, so this is a defect before it is a metric"
            )

            measured += 1
            # From the receipt: a set question, answerable without an order.
            as_a_set = as_a_set + precision_at_k(
                served=sorted(served), relevant=relevant, borderline=borderline, k=len(served)
            )
            # From the envelope's payload scores: the rank questions, which the
            # receipt cannot answer because it stores no rank.
            question_at_r = precision_at_r(served=ranked, relevant=relevant, borderline=borderline)
            at_r = at_r + question_at_r
            at_1 = at_1 + precision_at_k(served=ranked, relevant=relevant, borderline=borderline, k=1)
            if recall_set & served:
                recall_hits += 1
            if question_at_r.precision is not None:
                worst.append((question_at_r.precision, question["id"]))

        assert measured == SEARCH_QUESTION_COUNT, "every question must be measured, or the figure describes a subset"

        recall_at_10 = recall_hits / len(questions)
        print(f"\nretrieval quality over {measured} questions (StubEmbedder, lexical-dominant)")
        print("  from the receipt (set, no order available):")
        print(
            f"    precision  = {as_a_set.precision:.3f}   "
            f"({as_a_set.hits} relevant / {as_a_set.hits + as_a_set.misses} served)"
        )
        print(f"    recall@10  = {recall_at_10:.3f}   ({recall_hits}/{len(questions)})")
        print("  from the envelope payload scores (rank, not receipt-derivable):")
        print(f"    R-precision  = {at_r.precision:.3f}   ({at_r.hits} relevant / {at_r.hits + at_r.misses} scored)")
        print(f"    precision@1  = {at_1.precision:.3f}   ({at_1.hits} relevant / {at_1.hits + at_1.misses} scored)")
        print(f"  borderline results excluded from scoring: {as_a_set.borderline}")
        print("  weakest by R-precision: " + ", ".join(f"{qid}={p:.2f}" for p, qid in sorted(worst)[:5]))
        print(
            "\nThe two reads answer different questions and only the first is auditable.\n"
            "`context_receipt_items` has no rank, score or position column, so a receipt records which\n"
            "items were served and not the order they were served in. The envelope's own item order is\n"
            "the digest order `ordered_items` imposes for reproducibility, which is a hash of the entity\n"
            "id -- so a consumer reading the block top-down is not reading a ranking, and the assembler's\n"
            "item cap truncates by that hash too. Record all four figures in eval/EVAL.md."
        )

        assert recall_at_10 >= _RECALL_FLOOR, (
            f"recall@10 computed from receipts = {recall_at_10:.3f} < {_RECALL_FLOOR}; "
            "either retrieval regressed or the receipt is no longer recording what was served"
        )
    finally:
        await engine.dispose()
        probe = create_async_engine(pg_container)
        await probe.dispose()
