"""The workspace evaluation harness is versioned, frozen, and non-vacuous.

An evaluation harness fails in a way ordinary code does not: it keeps running,
keeps producing numbers, and the numbers keep looking reasonable. Nothing about
a green suite of scenario assertions would catch a corpus that halved, a judge
that scores everything 1.0, a treatment silently skipped, or a threshold edited
between collection and write-up. So this module tests the properties that make
the result mean something, in the order a defect in any one of them should be
caught:

1. The frozen corpus is on disk at the size the protocol committed to, checked
   before any scenario's content is trusted.
2. The scorer is pinned by content digest, and the rubric it implements is the
   frozen text.
3. The scorer is **non-vacuous**: a perfect envelope, an empty one and a partial
   one score differently, and a leak is recorded rather than averaged.
4. Every configuration runs on every scenario, unconditionally, and an errored
   run stays in the batch as a failure.
5. The result is signed, re-derivable, and carries no decision.

The suite deliberately does not run a measurement. It exercises the instrument
against synthetic envelopes; producing an actual result is an operational act
that the protocol forbids until the freeze addendum lands, and a test that
produced one would be collecting observations as a side effect of `make test`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from contextplane.context.assembler import ArmOutcome, contextual_item
from contextplane.context.evaluation import evidence, harness, judge, protocol, scenarios, treatments
from contextplane.context.quality import derive_quality
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
    ContextBlockV1,
    ContextEnvelopeV1,
    ContextItemV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import TrustMetadataV1

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = scenarios.corpus_path(_REPO_ROOT)

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_TENANT = "11111111-1111-5111-8111-111111111111"
_TASK = "22222222-2222-5222-8222-222222222222"
_OTHER_TASK = "33333333-3333-5333-8333-333333333333"
_KEY = b"conformance-signing-key"

# The counts the protocol froze, restated here rather than imported. A test that
# reads its expectation from the module under test passes by definition -- the
# same reasoning the drafter-decision gate applies to its seven gate ids.
_EXPECTED_SCENARIO_COUNTS = {"task_resume": 20, "cross_task_recall": 20}
_EXPECTED_CONFIGURATIONS = (
    "baseline-no-memory",
    "treatment-a-lexical-reference",
    "treatment-b-semantic-exact-scan",
)


# ---------------------------------------------------------------------------
# Builders. Synthetic envelopes, so every scoring path is reachable without a
# database and without resolving anything real.
# ---------------------------------------------------------------------------


def _item(
    key: str,
    *,
    task_id: str = _TASK,
    tenant_id: str = _TENANT,
    classification: str = "internal",
    text: str = "",
    digest: str | None = None,
) -> ContextItemV1:
    payload: dict[str, object] = {"task_id": task_id, "tenant_id": tenant_id, "goal": text or key}
    if digest is not None:
        payload["digest"] = digest
    return contextual_item(
        block=BLOCK_WORKSPACE,
        source="task_checkpoint",
        item_key=key,
        payload=payload,
        trust=TrustMetadataV1(
            trust="asserted",
            source="task_checkpoint",
            assertion_kind="annotation",
            authority=f"task:{task_id}",
            freshness=_NOW,
            mutability="immutable",
            attribution="agent-alpha",
            classification=classification,
        ),
    )


def _envelope(items: tuple[ContextItemV1, ...]) -> ContextEnvelopeV1:
    workspace = ContextBlockV1(
        name=BLOCK_WORKSPACE,
        state="success" if items else "empty",
        items=items,
    )
    others = tuple(
        ContextBlockV1(name=name, state="empty") for name in (BLOCK_CANONICAL, BLOCK_ARC, BLOCK_OBSERVED_CLAIMS)
    )
    blocks = (others[0], others[1], others[2], workspace)
    return ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))


class _WordEmbedder:
    """A deterministic bag-of-words embedder.

    Not a model, and not pretending to be one. It exists so the scan's ranking
    is a function of the text rather than of a random seed, which is what lets a
    test assert that a semantically related candidate outranks an unrelated one
    without depending on a model artifact being present.
    """

    model_version = "conformance-bag-of-words"
    vocabulary = (
        "budget",
        "cache",
        "drain",
        "erasure",
        "hold",
        "latency",
        "partition",
        "receipt",
        "retry",
        "salt",
        "sandbox",
        "tenant",
    )

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if word in text.lower() else 0.0 for word in self.vocabulary] for text in texts]


@dataclasses.dataclass
class _StubSource:
    """A workspace source under the harness's control.

    Records what it was asked for, so a test can assert that the semantic
    treatment resolved the authorized set *before* it scored anything, and that
    the baseline asked for nothing at all.
    """

    lexical_items: tuple[ContextItemV1, ...] = ()
    reference_items: tuple[ContextItemV1, ...] = ()
    candidates: tuple[treatments.Candidate, ...] = ()
    calls: list[str] = dataclasses.field(default_factory=list)
    raise_on_lexical: Exception | None = None

    async def lexical(self, scenario: scenarios.Scenario) -> ArmOutcome:
        self.calls.append("lexical")
        if self.raise_on_lexical is not None:
            raise self.raise_on_lexical
        return ArmOutcome(items=self.lexical_items)

    async def reference(self, scenario: scenarios.Scenario) -> ArmOutcome:
        self.calls.append("reference")
        return ArmOutcome(items=self.reference_items)

    async def authorized_candidates(self, scenario: scenarios.Scenario) -> tuple[treatments.Candidate, ...]:
        self.calls.append("authorized_candidates")
        return self.candidates


def _no_other_arms(scenario: scenarios.Scenario) -> dict[str, Any]:
    """The three held-fixed arms, each truthfully empty.

    Empty rather than absent: an arm missing from the mapping is a *failed*
    block, which would degrade every envelope in every configuration equally and
    make the harness measure a broken system three times.
    """

    async def empty() -> ArmOutcome:
        return ArmOutcome()

    return {BLOCK_CANONICAL: empty, BLOCK_ARC: empty, BLOCK_OBSERVED_CLAIMS: empty}


def _one_scenario(**overrides: Any) -> scenarios.Scenario:
    base: dict[str, Any] = {
        "scenario_id": "SYNTH-01",
        "kind": "task_resume",
        "description": "a synthetic scenario used to exercise the harness",
        "tenant_id": _TENANT,
        "actor_id": "agent-alpha",
        "term": "drain the retry budget",
        "reference": None,
        "required_item_keys": ("a", "b"),
        "relevant_item_keys": ("a", "b"),
        "facts": judge.AuthorizationFacts(
            permitted_tenant_ids=frozenset({_TENANT}),
            permitted_task_ids=frozenset({_TASK}),
        ),
    }
    base.update(overrides)
    return scenarios.Scenario(**base)


# ---------------------------------------------------------------------------
# 1. The frozen corpus. Cardinality before content.
# ---------------------------------------------------------------------------


def test_the_frozen_corpus_is_committed_at_the_documented_path() -> None:
    assert _CORPUS.is_file(), f"the frozen corpus is not committed at {_CORPUS}"


def test_the_corpus_holds_exactly_the_frozen_scenario_counts() -> None:
    """Checked before any scenario's content is trusted. A corpus that quietly
    shrank raises every mean it feeds without any system having improved, and
    the surviving scenarios all still pass."""
    corpus = scenarios.load_corpus(_CORPUS)
    counted = {kind: len(corpus.by_kind(kind)) for kind in _EXPECTED_SCENARIO_COUNTS}
    assert counted == _EXPECTED_SCENARIO_COUNTS
    assert len(corpus.scenarios) == sum(_EXPECTED_SCENARIO_COUNTS.values())


def test_a_corpus_of_the_wrong_size_is_refused_rather_than_scored(tmp_path: Path) -> None:
    document = json.loads(_CORPUS.read_text(encoding="utf-8"))
    document["scenarios"] = document["scenarios"][:10]
    truncated = tmp_path / "scenarios.json"
    truncated.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(scenarios.CorpusInvalid, match="changed size"):
        scenarios.load_corpus(truncated)


def test_every_scenario_names_its_required_facts_in_advance() -> None:
    """The mechanism the whole pre-registration rests on. A scenario with no
    required facts is passed by a system that returns nothing."""
    corpus = scenarios.load_corpus(_CORPUS)
    for scenario in corpus.scenarios:
        assert scenario.required_item_keys, f"{scenario.scenario_id}: no required facts"
        assert set(scenario.required_item_keys) <= set(scenario.relevant_item_keys)
        assert scenario.description.strip(), f"{scenario.scenario_id}: no description"
        assert scenario.term.strip(), f"{scenario.scenario_id}: no query term"


def test_scenario_ids_are_unique_across_the_corpus() -> None:
    corpus = scenarios.load_corpus(_CORPUS)
    ids = [s.scenario_id for s in corpus.scenarios]
    assert len(set(ids)) == len(ids)


def test_the_corpus_declares_authorization_facts_the_judge_can_check() -> None:
    """The judge must be able to answer "should this have been served?" without
    asking the system under test, which would agree with itself."""
    corpus = scenarios.load_corpus(_CORPUS)
    for scenario in corpus.scenarios:
        facts = scenario.facts
        assert facts.permitted_tenant_ids, f"{scenario.scenario_id}: no permitted tenant"
        assert facts.permitted_task_ids, f"{scenario.scenario_id}: no permitted audience"
        assert facts.max_classification in judge.CLASSIFICATION_ORDER


def test_the_corpus_exercises_the_lifecycle_and_classification_boundaries() -> None:
    """A corpus in which nothing is ever withheld cannot distinguish a system
    that withholds correctly from one that never had to."""
    corpus = scenarios.load_corpus(_CORPUS)
    assert any(s.facts.withdrawn_item_keys for s in corpus.scenarios)
    assert any(s.facts.max_classification == "internal" for s in corpus.scenarios)
    assert any(s.reference is not None for s in corpus.scenarios)


# ---------------------------------------------------------------------------
# 2. The scorer is pinned, and the freeze covers it.
# ---------------------------------------------------------------------------


def test_the_judge_version_is_the_one_the_protocol_pinned() -> None:
    assert protocol.JUDGE_VERSION == "workspace-eval-judge v1.0.0"


def test_the_judge_digest_is_reproducible_from_the_committed_source() -> None:
    """A freeze cannot digest a program that does not exist, and a digest that
    is not reproducible from disk pins nothing."""
    import hashlib

    source = Path(judge.__file__)
    assert protocol.judge_source_digest() == hashlib.sha256(source.read_bytes()).hexdigest()


def test_a_missing_scorer_invalidates_rather_than_defaulting(tmp_path: Path) -> None:
    with pytest.raises(protocol.ProtocolInvalidated, match="does not exist"):
        protocol.judge_source_digest(tmp_path / "judge.py")


def test_the_freeze_covers_both_the_values_and_the_scorer(tmp_path: Path) -> None:
    """Digesting the thresholds alone would let the scorer be rewritten under a
    fixed set of numbers, which changes every result without changing anything
    the freeze can see."""
    frozen = protocol.freeze()
    edited = tmp_path / "judge.py"
    edited.write_text(Path(judge.__file__).read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8")
    moved = protocol.freeze(judge_source=edited)
    assert moved.protocol_digest == frozen.protocol_digest
    assert moved.judge_digest != frozen.judge_digest
    assert moved.freeze_digest() != frozen.freeze_digest()


def test_a_protocol_change_after_collection_invalidates_the_run(tmp_path: Path) -> None:
    collected_under = protocol.freeze()
    edited = tmp_path / "judge.py"
    edited.write_text(Path(judge.__file__).read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8")
    with pytest.raises(protocol.ProtocolInvalidated, match="restarts"):
        protocol.assert_unchanged(collected_under, judge_source=edited)


def test_the_frozen_values_are_the_ones_committed_in_advance() -> None:
    """Restated here rather than read off the module, so an edited threshold
    fails a test instead of redefining what the test expects."""
    values = protocol.frozen_values()
    assert values["treatment_a_margin"] == 0.15
    assert values["treatment_b_margin"] == 0.10
    assert values["marginal_band"] == 0.02
    assert values["safety_tolerance"] == 0
    assert values["latency_median_regression_limit"] == 0.25
    assert values["human_risk_sample_size"] == 10
    assert values["safety_review_is_exhaustive"] is True
    assert values["configurations"] == list(_EXPECTED_CONFIGURATIONS)
    assert values["scenario_counts"] == _EXPECTED_SCENARIO_COUNTS


def test_the_branch_table_states_a_consequence_for_every_case() -> None:
    """Four branches, including the one where nothing helps. A protocol with no
    stated consequence for "neither helps" is one that cannot return it."""
    assert set(protocol.BRANCHES) == {
        "semantic-adds-value-and-passes-safety",
        "lexical-sufficient",
        "semantic-adds-value-but-fails-safety",
        "task-memory-fails-baseline",
    }


def test_the_latency_gate_is_a_repeated_run_median_with_a_documented_tail_rule() -> None:
    assert protocol.LATENCY_REPEATS > 1, "a single reading per scenario measures the machine"
    assert "median" in protocol.LATENCY_TAIL_RULE
    assert "does not gate" in protocol.LATENCY_TAIL_RULE


# ---------------------------------------------------------------------------
# 3. The scorer is non-vacuous.
# ---------------------------------------------------------------------------


def test_the_rubric_the_scorer_implements_is_the_frozen_text() -> None:
    for clause in (
        "required item keys present in envelope",
        "matching by receipt item key or content digest",
        "tenant, audience, classification, or lifecycle state",
        "No partial credit",
    ):
        assert clause in judge.RUBRIC


def test_a_perfect_envelope_scores_one_and_an_empty_one_scores_zero() -> None:
    scenario = _one_scenario()
    perfect = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-a-lexical-reference",
        envelope=_envelope((_item("a"), _item("b"))),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    nothing = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="baseline-no-memory",
        envelope=_envelope(()),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    assert perfect.recall == 1.0
    assert nothing.recall == 0.0
    assert perfect.recall > nothing.recall, "the scorer cannot distinguish a full answer from no answer"


def test_a_partial_envelope_scores_strictly_between() -> None:
    """The property a vacuous scorer fails: one of two required facts is not
    "close enough" to a pass, and it is not a zero either."""
    scenario = _one_scenario()
    partial = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-a-lexical-reference",
        envelope=_envelope((_item("a"),)),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    assert partial.recall == 0.5
    assert partial.required_found == 1
    assert partial.required_total == 2


def test_a_required_fact_matches_by_content_digest_as_well_as_by_item_key() -> None:
    scenario = _one_scenario(required_item_keys=("digest-of-a",), relevant_item_keys=("digest-of-a",))
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-a-lexical-reference",
        envelope=_envelope((_item("some-other-key", digest="digest-of-a"),)),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    assert scored.recall == 1.0


def test_a_near_miss_earns_no_partial_credit() -> None:
    scenario = _one_scenario(required_item_keys=("abcdef",), relevant_item_keys=("abcdef",))
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-a-lexical-reference",
        envelope=_envelope((_item("abcdeg"),)),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    assert scored.recall == 0.0


@pytest.mark.parametrize(
    ("item", "expected_kind"),
    [
        (_item("leak", tenant_id="99999999-9999-5999-8999-999999999999"), judge.VIOLATION_TENANT),
        (_item("leak", task_id=_OTHER_TASK), judge.VIOLATION_AUDIENCE),
        (_item("leak", classification="restricted"), judge.VIOLATION_CLASSIFICATION),
    ],
)
def test_a_served_item_outside_the_declared_facts_is_a_safety_violation(
    item: ContextItemV1, expected_kind: str
) -> None:
    scenario = _one_scenario()
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-b-semantic-exact-scan",
        envelope=_envelope((item,)),
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
    )
    assert [v.kind for v in scored.violations] == [expected_kind]
    assert scored.is_safe is False


def test_serving_a_withdrawn_item_is_a_lifecycle_violation() -> None:
    """The dangerous case: the item was authorized once, which is exactly why a
    surviving copy reads as legitimate."""
    scenario = _one_scenario(
        facts=judge.AuthorizationFacts(
            permitted_tenant_ids=frozenset({_TENANT}),
            permitted_task_ids=frozenset({_TASK}),
            withdrawn_item_keys=frozenset({"erased"}),
        )
    )
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-b-semantic-exact-scan",
        envelope=_envelope((_item("erased"),)),
        required_item_keys=("a",),
        relevant_item_keys=("a",),
        facts=scenario.facts,
    )
    assert [v.kind for v in scored.violations] == [judge.VIOLATION_LIFECYCLE]


def test_an_item_failing_two_boundaries_is_reported_against_both() -> None:
    scenario = _one_scenario()
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-b-semantic-exact-scan",
        envelope=_envelope((_item("leak", task_id=_OTHER_TASK, classification="restricted"),)),
        required_item_keys=("a",),
        relevant_item_keys=("a",),
        facts=scenario.facts,
    )
    assert {v.kind for v in scored.violations} == {judge.VIOLATION_AUDIENCE, judge.VIOLATION_CLASSIFICATION}


def test_precision_counts_served_workspace_items_only() -> None:
    scenario = _one_scenario()
    scored = judge.score(
        scenario_id=scenario.scenario_id,
        configuration="treatment-a-lexical-reference",
        envelope=_envelope((_item("a"), _item("noise"))),
        required_item_keys=("a",),
        relevant_item_keys=("a",),
        facts=scenario.facts,
    )
    assert scored.served_total == 2
    assert scored.precision == 0.5


def test_an_unreadable_classification_is_treated_as_the_most_restrictive() -> None:
    """Guessing downward is what publishes it."""
    assert judge._classification_rank("not-a-label") == len(judge.CLASSIFICATION_ORDER) - 1


# ---------------------------------------------------------------------------
# 4. The batch: unconditional, inclusive of failures, invalidated by the
#    environment rather than by a scenario.
# ---------------------------------------------------------------------------


async def test_every_configuration_runs_on_every_scenario_unconditionally() -> None:
    """Running the semantic treatment only if the lexical one passed would
    foreclose the branch where lexical fails and semantic succeeds -- an answer
    the protocol has to be able to return, and one that is unobservable if the
    second treatment never ran."""
    corpus = scenarios.load_corpus(_CORPUS)
    source = _StubSource()
    batch = await harness.run_batch(
        corpus=corpus,
        source=source,
        other_arms=_no_other_arms,
        embedder=_WordEmbedder(),
        now=_NOW,
        repeats=1,
    )
    assert [r.configuration for r in batch.results] == list(_EXPECTED_CONFIGURATIONS)
    for result in batch.results:
        assert len(result.runs) == len(corpus.scenarios)
    # Baseline scored zero on everything, and the semantic treatment still ran.
    assert batch.by_configuration("baseline-no-memory").mean_recall == 0.0
    assert len(batch.by_configuration("treatment-b-semantic-exact-scan").runs) == len(corpus.scenarios)


async def test_the_baseline_asks_the_workspace_source_for_nothing() -> None:
    """The ablation removes the arm's content, not the arm. A baseline that
    still read the workspace would not be a no-memory baseline."""
    scenario = _one_scenario()
    source = _StubSource(lexical_items=(_item("a"),))
    run = await harness.run_scenario(
        scenario=scenario,
        configuration="baseline-no-memory",
        source=source,
        other_arms=_no_other_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=1,
    )
    assert source.calls == []
    assert run.score.recall == 0.0
    assert run.score.errored is False, "an empty baseline is a truthful answer, not a failure"


async def test_the_semantic_treatment_resolves_the_authorized_set_before_scoring() -> None:
    """Authorization is the candidate set, not a filter over it -- so the
    resolution has to happen before anything is scored, and this is where that
    ordering is pinned."""
    scenario = _one_scenario()
    source = _StubSource(
        candidates=(treatments.Candidate(item_key="a", text="drain the retry budget", item=_item("a")),)
    )
    await harness.run_scenario(
        scenario=scenario,
        configuration="treatment-b-semantic-exact-scan",
        source=source,
        other_arms=_no_other_arms(scenario),
        embedder=_WordEmbedder(),
        now=_NOW,
        repeats=1,
    )
    assert source.calls[0] == "authorized_candidates"


async def test_a_system_error_counts_as_a_failure_and_stays_in_the_batch() -> None:
    """Excluding errored runs after the fact is the most common way a result
    improves without the system improving."""
    scenario = _one_scenario()
    source = _StubSource(raise_on_lexical=RuntimeError("the service refused"))
    run = await harness.run_scenario(
        scenario=scenario,
        configuration="treatment-a-lexical-reference",
        source=source,
        other_arms=_no_other_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=1,
    )
    assert run.score.errored is True
    assert run.score.recall == 0.0


async def test_a_failed_workspace_block_is_an_error_not_a_truthful_empty() -> None:
    """The assembler catches what an arm raises, so a broken arm and an empty
    workspace both arrive as a block with no items. Scoring the first as a
    legitimate zero would report a broken integration as evidence that workspace
    memory does not help."""
    scenario = _one_scenario()

    class _UnusableSource(_StubSource):
        async def lexical(self, scenario: scenarios.Scenario) -> ArmOutcome:
            # An item with no trust metadata: the block contract refuses it, so
            # the arm has failed without ever raising in this frame.
            return ArmOutcome(items=(ContextItemV1(receipt_item_id=_item("a").receipt_item_id, payload={}),))

    run = await harness.run_scenario(
        scenario=scenario,
        configuration="treatment-a-lexical-reference",
        source=_UnusableSource(),
        other_arms=_no_other_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=1,
    )
    assert run.score.errored is True


async def test_an_infrastructure_error_invalidates_the_whole_batch() -> None:
    """A database that fell over mid-run produced observations under two
    different systems. The batch is rerun whole rather than losing a scenario."""
    corpus = scenarios.load_corpus(_CORPUS)
    source = _StubSource(raise_on_lexical=harness.InfrastructureError("the database went away"))
    with pytest.raises(harness.BatchInvalidated, match="rerun whole"):
        await harness.run_batch(
            corpus=corpus,
            source=source,
            other_arms=_no_other_arms,
            embedder=_WordEmbedder(),
            now=_NOW,
            repeats=1,
        )


async def test_a_batch_with_no_configuration_is_refused() -> None:
    corpus = scenarios.load_corpus(_CORPUS)
    with pytest.raises(harness.BatchInvalidated, match="measured nothing"):
        await harness.run_batch(
            corpus=corpus,
            source=_StubSource(),
            other_arms=_no_other_arms,
            embedder=None,
            now=_NOW,
            configurations=(),
        )


async def test_the_semantic_treatment_refuses_to_run_without_a_model() -> None:
    """A semantic treatment with no embedder is the baseline wearing its name,
    and it would report the baseline's numbers as the treatment's."""
    scenario = _one_scenario()
    with pytest.raises(ValueError, match="needs an embedder"):
        treatments.workspace_arm_for(
            "treatment-b-semantic-exact-scan", scenario=scenario, source=_StubSource(), embedder=None
        )


async def test_latency_is_a_repeated_run_median_and_the_tail_only_describes() -> None:
    scenario = _one_scenario()
    run = await harness.run_scenario(
        scenario=scenario,
        configuration="treatment-a-lexical-reference",
        source=_StubSource(lexical_items=(_item("a"), _item("b"))),
        other_arms=_no_other_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=protocol.LATENCY_REPEATS,
    )
    assert len(run.durations_ms) == protocol.LATENCY_REPEATS
    result = harness.ConfigurationResult(configuration="treatment-a-lexical-reference", runs=(run,))
    assert result.median_latency_ms == run.median_ms
    assert result.slowest_scenario_median_ms == run.median_ms


def test_the_human_risk_sample_is_ten_deterministic_scenarios_across_both_kinds() -> None:
    corpus = scenarios.load_corpus(_CORPUS)
    drawn = harness.human_risk_sample(corpus)
    assert len(drawn) == 10
    assert drawn == harness.human_risk_sample(corpus), "the review sample must not move between runs"
    by_id = {s.scenario_id: s.kind for s in corpus.scenarios}
    assert {by_id[i] for i in drawn} == set(_EXPECTED_SCENARIO_COUNTS)


def test_every_safety_failure_is_carried_never_sampled() -> None:
    """Sampling a risk review is a resourcing decision; sampling safety failures
    is deciding not to look at some of them."""
    leaks = tuple(
        harness.ScenarioRun(
            score=judge.score(
                scenario_id=f"SYNTH-{n:02d}",
                configuration="treatment-b-semantic-exact-scan",
                envelope=_envelope((_item("leak", task_id=_OTHER_TASK),)),
                required_item_keys=("a",),
                relevant_item_keys=("a",),
                facts=_one_scenario().facts,
            ),
            durations_ms=(1.0,),
        )
        for n in range(25)
    )
    result = harness.ConfigurationResult(configuration="treatment-b-semantic-exact-scan", runs=leaks)
    assert len(result.safety_failures) == 25
    assert protocol.HUMAN_RISK_SAMPLE_SIZE < 25, "the sample size must be smaller, or this proves nothing"


# ---------------------------------------------------------------------------
# 5. The exact scan cannot reach outside the authorized set.
# ---------------------------------------------------------------------------


def test_the_scan_serves_only_what_the_authorized_set_contained() -> None:
    scanned = treatments.exact_scan(
        query="retry budget",
        candidates=(treatments.Candidate(item_key="mine", text="the retry budget we agreed", item=_item("mine")),),
        embedder=_WordEmbedder(),
    )
    assert [i.receipt_item_id.item_key for i in scanned.items] == ["mine"]


def test_an_empty_authorized_set_yields_nothing_rather_than_widening() -> None:
    """ "Too few authorized matches" is a correct answer. A fallback to a broader
    corpus would answer a question the caller was not entitled to ask."""
    scanned = treatments.exact_scan(query="retry budget", candidates=(), embedder=_WordEmbedder())
    assert scanned.items == ()
    assert scanned.exclusions == ()


def test_an_unrelated_candidate_does_not_reach_the_floor() -> None:
    scanned = treatments.exact_scan(
        query="retry budget",
        candidates=(treatments.Candidate(item_key="unrelated", text="the sandbox partition", item=_item("unrelated")),),
        embedder=_WordEmbedder(),
    )
    assert scanned.items == ()


def test_the_scan_is_deterministic_over_an_unchanged_authorized_set() -> None:
    """Two resolutions over unchanged data must agree, or every latency repeat
    would also be a different measurement of a different answer."""
    candidates = tuple(
        treatments.Candidate(item_key=key, text=text, item=_item(key))
        for key, text in (("one", "retry budget drain"), ("two", "retry budget cache"), ("three", "salt tenant"))
    )
    first = treatments.exact_scan(query="retry budget", candidates=candidates, embedder=_WordEmbedder())
    second = treatments.exact_scan(
        query="retry budget", candidates=tuple(reversed(candidates)), embedder=_WordEmbedder()
    )
    assert [i.receipt_item_id.item_key for i in first.items] == [i.receipt_item_id.item_key for i in second.items]


def test_a_zero_vector_scores_zero_rather_than_raising() -> None:
    assert treatments.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ---------------------------------------------------------------------------
# 6. The evidence: signed, re-derivable, and undecided.
# ---------------------------------------------------------------------------


async def _small_batch() -> harness.BatchResult:
    corpus = scenarios.load_corpus(_CORPUS)
    return await harness.run_batch(
        corpus=corpus,
        source=_StubSource(),
        other_arms=_no_other_arms,
        embedder=_WordEmbedder(),
        now=_NOW,
        repeats=1,
    )


async def test_the_result_is_signed_and_verifies_against_its_own_content() -> None:
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    assert signed.verify(_KEY) is True


async def test_an_edited_result_fails_verification() -> None:
    """A digest an editor can recompute after editing seals nothing, which is
    why the signature takes an operator-supplied key."""
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    tampered = dataclasses.replace(
        signed,
        document={**signed.document, "corpus_digest": "0" * 64},
    )
    assert tampered.verify(_KEY) is False
    assert signed.verify(b"a-different-key") is False


async def test_an_unsigned_result_is_refused_rather_than_caveated() -> None:
    with pytest.raises(evidence.EvidenceUnsigned):
        evidence.build(await _small_batch(), signing_key=b"")


async def test_the_evidence_records_no_decision() -> None:
    """The party that runs the measurement and the party that concludes from it
    are not the same party."""
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    serialized = signed.as_json()
    for branch in protocol.BRANCHES:
        assert branch not in serialized, f"the evidence names branch {branch!r}; concluding is not its job"
    for word in ("decision", "recommendation", "verdict"):
        assert f'"{word}"' not in serialized, f"the evidence carries a {word!r} field"
    assert signed.document["disclaimer"] == evidence.EVIDENCE_CARRIES_NO_DECISION


async def test_the_evidence_carries_what_a_reader_needs_to_re_derive_the_means() -> None:
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    for entry in signed.document["configurations"]:
        per_scenario = entry["primary_metric"]["per_scenario"]
        assert len(per_scenario) == 40
        recomputed = sum(s["recall"] for s in per_scenario) / len(per_scenario)
        assert recomputed == pytest.approx(entry["primary_metric"]["mean"])
    assert signed.document["freeze"]["judge_version"] == protocol.JUDGE_VERSION
    assert signed.document["judge"]["rubric"] == judge.RUBRIC


async def test_the_evidence_labels_the_secondary_metrics_as_secondary() -> None:
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    for entry in signed.document["configurations"]:
        assert entry["primary_metric"]["name"] == "required_fact_recall"
        assert "workspace_item_precision_mean" in entry["secondary_metrics"]
        assert "resolution_latency_ms" in entry["secondary_metrics"]
        assert entry["secondary_metrics"]["resolution_latency_ms"]["tail_rule"] == protocol.LATENCY_TAIL_RULE


async def test_a_protocol_change_between_collection_and_write_up_blocks_the_result(tmp_path: Path) -> None:
    """The dangerous edit is the one made after the observations exist and
    before they are written up; a check at collection time cannot see it."""
    batch = await _small_batch()
    edited = tmp_path / "judge.py"
    edited.write_text(Path(judge.__file__).read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8")
    with pytest.raises(protocol.ProtocolInvalidated):
        evidence.build(batch, signing_key=_KEY, judge_source=edited)


async def test_the_evidence_stamps_the_corpus_it_was_collected_over() -> None:
    corpus = scenarios.load_corpus(_CORPUS)
    signed = evidence.build(await _small_batch(), signing_key=_KEY)
    assert signed.document["corpus_digest"] == corpus.digest
