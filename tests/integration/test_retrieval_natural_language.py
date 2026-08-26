"""A question is not a keyword query, and retrieval has to answer one anyway.

Every surface this product puts in front of a person or an agent asks for a
prompt: Context Lab's box says *prompt*, simulation feeds a prompt to a model,
`resolve` takes a `query`. A prompt is a sentence. It carries words the corpus
does not contain — "who", "owns", "what", "for" — because that is what sentences
are made of.

`plainto_tsquery` conjoins, so a sentence became a demand that one row contain
every one of those words at once. The failure it produced was not a degraded
ranking, it was **zero rows**, and it was invisible from the existing gates: the
recall corpus scored 0.96 while `Who owns salt design system?` returned nothing
from a catalog that contains `salt-design-system`. The two are consistent because
recall@10 asks whether the answer is anywhere in ten, and the arms that carried
those ten were not the one being broken.

So these tests assert the thing that was actually wrong, at the smallest scope
that can express it: **adding an ordinary English word to a query that works must
not make it stop working.** That is a property, not a fixture — it holds for any
corpus, and it is the invariant a keyword-shaped parser cannot satisfy.

They sit in the integration tier because the defect was in SQL. A unit test with
a fake session would have asserted the string the code builds, which is the thing
that was wrong; only Postgres can say whether a tsquery matches a tsvector.

**They assert on the arms, not on `search`, and that is not a shortcut.** The
first version of this module asserted `search()` was non-empty and passed against
the unfixed code — all six cases. `search` fuses three arms, and the semantic arm
under the stub embedder returns the *k* nearest of a set of identical zero-vector
distances, which is to say ten arbitrary rows for any string at all. It filled
every result the broken lexical arm left empty. A test that green-lights the
defect it was written for is worse than no test, because the next person reads
the coverage and stops looking. The defect was in two arms; the assertions are on
those two arms.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.config import Settings
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.retrieval import RetrievalService
from contextplane.service.retrieval._query_primitives import any_term_tsquery
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.storage.pg import create_engine, get_session_factory
from contextplane.types import TemporalFilter, TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.eval_corpus import seed_eval_entities

_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

#: Each pair is (keyword form, the same request phrased as a question). The
#: question adds only ordinary English: no new subject, no new constraint. A
#: retriever that answers the left and not the right is parsing prose as a
#: conjunction, which is the defect these tests exist for.
#:
#: Each question adds at least one word the matching fact does not contain, which
#: is the only version of this test that means anything. `How does the
#: payment-service handle refunds?` looks like a fair question and is not: that
#: fact says "handles" and "refunds", so even a conjunction matches it, and the
#: case passed against the bug. The words below — deployed, rejects, escalate —
#: are ordinary things a person asks and this corpus never says.
_KEYWORD_AND_QUESTION = [
    ("payment-service refunds", "Who owns the payment-service and how is it deployed?"),
    ("ingest-pipeline", "Which capability owns the ingest-pipeline?"),
    ("rate-limiter", "Why does the rate-limiter reject my traffic?"),
    ("jwt-validator", "Who owns the jwt-validator and what does it check?"),
]


async def _service(pg_url: str) -> tuple[RetrievalService, TenantContext, object]:
    settings = Settings(
        database_url=pg_url,
        pgbouncer_url=pg_url,
        scheduler_jobstore_url=pg_url,
        embedding_provider="stub",
    )
    tenant_id, actor_id, _fixture_to_entity = await seed_eval_entities(pg_url)
    engine = create_engine(settings)
    factory = get_session_factory(engine)
    embedder = StubEmbedder()
    await drain_outbox(factory, embedder, settings)
    service = RetrievalService(factory, FakeClock(_NOW), embedder, settings)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    return service, ctx, engine


@pytest.mark.asyncio
@pytest.mark.parametrize(("keywords", "question"), _KEYWORD_AND_QUESTION)
async def test_phrasing_a_request_as_a_question_does_not_empty_the_lexical_arm(
    pg_container: str, keywords: str, question: str
) -> None:
    """The invariant, stated as directly as it can be stated.

    Not "the question returns the right answer" — that is what the relevance
    corpus measures, over fifty questions and with judged answer sets. This asks
    only that the question returns *something*, because the regression turned
    working queries into empty ones and an empty result is the one outcome no
    ranking argument can excuse.

    The keyword form is asserted first and separately. If it ever stops matching,
    the fixture changed and the interesting half of the comparison is meaningless
    — a failure message saying so is worth more than one saying both are empty.
    """
    service, ctx, engine = await _service(pg_container)
    try:
        as_keywords = await service._lexical_arm(ctx, keywords, 10, TemporalFilter(as_of=None), None)
        assert as_keywords, f"fixture problem, not a retrieval one: {keywords!r} matches no fact"

        as_question = await service._lexical_arm(ctx, question, 10, TemporalFilter(as_of=None), None)
        assert as_question, (
            f"{keywords!r} matches {len(as_keywords)} row(s) but {question!r} matches none. "
            "The extra words are ordinary English, so the query is being parsed as a "
            "conjunction of every token rather than a request about a subject."
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_question_finds_the_subject_it_names(pg_container: str) -> None:
    """Recall is necessary but it is not the claim being made here.

    The subject the question names has to actually come back, otherwise "returns
    something" is satisfied by returning anything — which is precisely what the
    semantic arm does under a stub embedder, and precisely how the first version
    of this test passed against the bug.

    Both term-reading arms are asserted because both were broken and each is the
    other's fallback: the lexical arm matches the fact bodies, the graph arm seeds
    on entity names. A deployment can lose either and still look healthy.
    """
    question = "Which capability owns the ingest-pipeline?"
    service, ctx, engine = await _service(pg_container)
    try:
        for arm in (service._lexical_arm, service._graph_arm):
            rows = await arm(ctx, question, 10, TemporalFilter(as_of=None), None)
            names = [ref.name for _eid, ref, _facts in rows]
            assert (
                "ingest-pipeline" in names
            ), f"{arm.__name__} was asked {question!r}, which names ingest-pipeline, and returned {names}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_widening_the_term_match_did_not_widen_it_to_everything(pg_container: str) -> None:
    """The term-matching arms must still refuse a query with no terms in it.

    `what is the` parses to an empty tsquery, which matches no row. That is the
    correct answer and not a gap: there is no term to search for, and an arm that
    answered a contentless query with the catalog would be worse than one that
    answered with nothing, because the caller cannot tell "here is what matched"
    from "here is everything". It is also the specific way this fix could have
    gone wrong — a disjunction is a wider net than a conjunction, and the failure
    mode of widening is matching things you did not ask for.

    Asserted on the two arms that read terms, not on `search`. The semantic arm
    has a different contract on purpose: it returns the *k* nearest vectors and
    there is always a nearest, so it answers any string, including one with no
    searchable terms. Asserting an empty `search()` would be asserting that the
    deployment has no embeddings, which is a fact about a fixture rather than
    about retrieval.
    """
    service, ctx, engine = await _service(pg_container)
    try:
        for arm in (service._lexical_arm, service._graph_arm):
            rows = await arm(ctx, "what is the", 10, TemporalFilter(as_of=None), None)
            assert rows == [], f"{arm.__name__} returned {len(rows)} row(s) for a query with no terms"
    finally:
        await engine.dispose()


def test_the_disjunction_is_derived_from_the_parse_and_not_rebuilt() -> None:
    """One parser, not two.

    The whole safety of rewriting `&` to `|` rests on the disjunction covering
    exactly the terms the conjunction did — same tokeniser, same stemmer, same
    stopword list. A second `to_tsquery` built from the raw string would drift
    from the first, and the arms would disagree about what a query's terms are
    while both looking correct in isolation.
    """
    sql = any_term_tsquery("q")
    assert "plainto_tsquery" in sql, "the disjunction must be derived from the parsed query"
    assert ":q" in sql, "the caller still binds the raw query text"
    assert "'&', '|'" in sql, "the rewrite is operator substitution, not a second parse"


def test_both_lexical_arms_parse_a_prompt_the_same_way() -> None:
    """Entity search and claim retrieval share the parser, and must keep sharing it.

    Both have a lexical arm and both had the same conjunction bug. Fixing one and
    not the other would leave a resolution whose canonical block answers the
    prompt and whose observed-claims block silently does not — the harder failure
    to notice, because the envelope still comes back populated.
    """
    from contextplane.service.memory import claim_serving_sql
    from contextplane.service.retrieval import search

    # `claim_serving_sql` rather than `claim_serving`: the statements moved to
    # their own module when the service crossed the size ceiling. The invariant
    # did not move — it is about the two arms parsing a prompt the same way, not
    # about which file either lives in.
    for module, bind in ((search, "query"), (claim_serving_sql, "q")):
        assert module._ANY_TERM == any_term_tsquery(
            bind
        ), f"{module.__name__} builds its own tsquery instead of sharing `any_term_tsquery`"
        assert "@@ plainto_tsquery" not in module._ANY_TERM
