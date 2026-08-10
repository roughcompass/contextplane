"""Unit coverage for the workspace evaluation instrument.

The conformance suite beside this one is a drift gate: it checks that the corpus
on disk is the size the protocol froze, that the scorer's digest is reproducible,
and that the committed thresholds are the ones committed. This file asks the
other question -- whether each piece behaves correctly -- against small
hand-built inputs, so every branch is reachable without a database, without the
40-scenario fixture, and without an embedding model.

The split matters because the two fail for different reasons. A drift gate goes
red when somebody edits a frozen value; these go red when somebody breaks the
arithmetic. Fixing one by adjusting the other is exactly the move both exist to
prevent.
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

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_TENANT = "11111111-1111-5111-8111-111111111111"
_TASK = "22222222-2222-5222-8222-222222222222"
_ELSEWHERE = "33333333-3333-5333-8333-333333333333"
_KEY = b"unit-signing-key"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _item(
    key: str,
    *,
    task_id: str = _TASK,
    tenant_id: str = _TENANT,
    classification: str = "internal",
    goal: str = "",
    digest: str | None = None,
) -> ContextItemV1:
    payload: dict[str, object] = {"task_id": task_id, "tenant_id": tenant_id, "goal": goal or key}
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


def _envelope(*items: ContextItemV1) -> ContextEnvelopeV1:
    workspace = ContextBlockV1(name=BLOCK_WORKSPACE, state="success" if items else "empty", items=tuple(items))
    others = tuple(
        ContextBlockV1(name=name, state="empty") for name in (BLOCK_CANONICAL, BLOCK_ARC, BLOCK_OBSERVED_CLAIMS)
    )
    blocks = (*others, workspace)
    return ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))


def _facts(**overrides: Any) -> judge.AuthorizationFacts:
    base: dict[str, Any] = {
        "permitted_tenant_ids": frozenset({_TENANT}),
        "permitted_task_ids": frozenset({_TASK}),
    }
    base.update(overrides)
    return judge.AuthorizationFacts(**base)


def _scenario(scenario_id: str = "U-01", **overrides: Any) -> scenarios.Scenario:
    base: dict[str, Any] = {
        "scenario_id": scenario_id,
        "kind": "task_resume",
        "description": "a hand-built scenario",
        "tenant_id": _TENANT,
        "actor_id": "agent-alpha",
        "term": "retry budget",
        "reference": None,
        "required_item_keys": ("a",),
        "relevant_item_keys": ("a",),
        "facts": _facts(),
    }
    base.update(overrides)
    return scenarios.Scenario(**base)


def _corpus(*entries: scenarios.Scenario) -> scenarios.Corpus:
    return scenarios.Corpus(version=1, scenarios=tuple(entries), digest="unit-corpus-digest")


class _Embedder:
    """Bag-of-words over a tiny vocabulary. Deterministic, and not a model."""

    model_version = "unit-bag-of-words"
    vocabulary = ("budget", "cache", "drain", "retry", "salt", "tenant")

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if word in t.lower() else 0.0 for word in self.vocabulary] for t in texts]


class _MiscountingEmbedder:
    model_version = "unit-miscounting"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0]]


@dataclasses.dataclass
class _Source:
    lexical_items: tuple[ContextItemV1, ...] = ()
    reference_items: tuple[ContextItemV1, ...] = ()
    candidates: tuple[treatments.Candidate, ...] = ()
    lexical_exclusions: tuple[Any, ...] = ()
    calls: list[str] = dataclasses.field(default_factory=list)
    raises: Exception | None = None

    async def lexical(self, scenario: scenarios.Scenario) -> ArmOutcome:
        self.calls.append("lexical")
        if self.raises is not None:
            raise self.raises
        return ArmOutcome(items=self.lexical_items, exclusions=self.lexical_exclusions)

    async def reference(self, scenario: scenarios.Scenario) -> ArmOutcome:
        self.calls.append("reference")
        return ArmOutcome(items=self.reference_items)

    async def authorized_candidates(self, scenario: scenarios.Scenario) -> tuple[treatments.Candidate, ...]:
        self.calls.append("authorized_candidates")
        return self.candidates


def _arms(scenario: scenarios.Scenario) -> dict[str, Any]:
    async def empty() -> ArmOutcome:
        return ArmOutcome()

    return {BLOCK_CANONICAL: empty, BLOCK_ARC: empty, BLOCK_OBSERVED_CLAIMS: empty}


def _score(envelope: ContextEnvelopeV1 | None, scenario: scenarios.Scenario, **kw: Any) -> judge.ScenarioScore:
    return judge.score(
        scenario_id=scenario.scenario_id,
        configuration=protocol.CONFIG_TREATMENT_A,
        envelope=envelope,
        required_item_keys=scenario.required_item_keys,
        relevant_item_keys=scenario.relevant_item_keys,
        facts=scenario.facts,
        **kw,
    )


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


def test_recall_is_the_fraction_of_required_facts_the_envelope_surfaced() -> None:
    scenario = _scenario(required_item_keys=("a", "b", "c", "d"), relevant_item_keys=("a", "b", "c", "d"))
    scored = _score(_envelope(_item("a"), _item("c")), scenario)
    assert (scored.required_found, scored.required_total) == (2, 4)
    assert scored.recall == 0.5


def test_recall_over_no_required_facts_is_zero_rather_than_a_division_error() -> None:
    found, total = judge.required_fact_recall(_envelope(_item("a")), ())
    assert (found, total) == (0, 0)


def test_a_repeated_required_key_is_counted_once() -> None:
    """Otherwise a scenario could inflate its own denominator by listing a fact
    twice, and the mean would move without the system changing."""
    found, total = judge.required_fact_recall(_envelope(_item("a")), ["a", "a", "b"])
    assert (found, total) == (1, 2)


def test_a_content_digest_matches_as_well_as_an_item_key() -> None:
    scenario = _scenario(required_item_keys=("sha-of-a",), relevant_item_keys=("sha-of-a",))
    assert _score(_envelope(_item("k", digest="sha-of-a")), scenario).recall == 1.0


def test_a_blank_digest_does_not_become_a_matchable_name() -> None:
    identities = judge._identities(_item("k", digest="   "))
    assert identities == frozenset({"k"})


def test_precision_is_zero_when_nothing_was_served() -> None:
    hits, served = judge.precision(_envelope(), ("a",))
    assert (hits, served) == (0, 0)


def test_precision_counts_relevant_over_served() -> None:
    hits, served = judge.precision(_envelope(_item("a"), _item("b"), _item("c")), ("a", "b"))
    assert (hits, served) == (2, 3)


@pytest.mark.parametrize(
    ("item", "kind"),
    [
        (_item("x", tenant_id=_ELSEWHERE), judge.VIOLATION_TENANT),
        (_item("x", task_id=_ELSEWHERE), judge.VIOLATION_AUDIENCE),
        (_item("x", classification="restricted"), judge.VIOLATION_CLASSIFICATION),
    ],
)
def test_each_declared_boundary_produces_its_own_violation_kind(item: ContextItemV1, kind: str) -> None:
    assert [v.kind for v in judge.safety_violations(_envelope(item), _facts())] == [kind]


def test_a_withdrawn_item_is_a_lifecycle_violation() -> None:
    facts = _facts(withdrawn_item_keys=frozenset({"gone"}))
    violations = judge.safety_violations(_envelope(_item("gone")), facts)
    assert [v.kind for v in violations] == [judge.VIOLATION_LIFECYCLE]


def test_a_dimension_the_scenario_makes_no_claim_about_is_not_judged() -> None:
    """`None` says the scenario is silent on that dimension; an empty set would
    say nothing is permitted, and the two must not collapse."""
    facts = judge.AuthorizationFacts(permitted_tenant_ids=None, permitted_task_ids=None)
    assert judge.safety_violations(_envelope(_item("x", tenant_id=_ELSEWHERE, task_id=_ELSEWHERE)), facts) == ()


def test_an_empty_permitted_set_permits_nothing() -> None:
    facts = judge.AuthorizationFacts(permitted_task_ids=frozenset())
    assert [v.kind for v in judge.safety_violations(_envelope(_item("x")), facts)] == [judge.VIOLATION_AUDIENCE]


def test_every_violated_dimension_is_reported_not_just_the_first() -> None:
    item = _item("x", tenant_id=_ELSEWHERE, task_id=_ELSEWHERE, classification="restricted")
    kinds = {v.kind for v in judge.safety_violations(_envelope(item), _facts())}
    assert kinds == {judge.VIOLATION_TENANT, judge.VIOLATION_AUDIENCE, judge.VIOLATION_CLASSIFICATION}


def test_a_classification_at_the_ceiling_is_allowed_and_one_above_is_not() -> None:
    at = _facts(max_classification="internal")
    assert judge.safety_violations(_envelope(_item("x", classification="internal")), at) == ()
    assert judge.safety_violations(_envelope(_item("x", classification="confidential")), at) != ()


def test_an_unreadable_classification_ranks_as_the_most_restrictive() -> None:
    assert judge._classification_rank("moderately-secret") == len(judge.CLASSIFICATION_ORDER) - 1
    assert judge._classification_rank(None) == len(judge.CLASSIFICATION_ORDER) - 1


def test_an_errored_run_scores_zero_and_is_marked_rather_than_dropped() -> None:
    scored = _score(None, _scenario())
    assert scored.errored is True
    assert (scored.recall, scored.precision, scored.served_total) == (0.0, 0.0, 0)
    assert scored.required_total == 1, "the scenario still counts; it failed rather than vanished"


def test_an_envelope_present_but_flagged_errored_still_scores_as_a_failure() -> None:
    assert _score(_envelope(_item("a")), _scenario(), errored=True).errored is True


def test_is_safe_is_the_absence_of_violations() -> None:
    assert _score(_envelope(_item("a")), _scenario()).is_safe is True
    assert _score(_envelope(_item("a", task_id=_ELSEWHERE)), _scenario()).is_safe is False


def test_workspace_items_ignores_the_other_three_blocks() -> None:
    assert judge.workspace_items(_envelope()) == ()
    assert len(judge.workspace_items(_envelope(_item("a")))) == 1


# ---------------------------------------------------------------------------
# The freeze
# ---------------------------------------------------------------------------


def test_the_freeze_digest_covers_both_halves() -> None:
    frozen = protocol.freeze()
    assert frozen.freeze_digest() != frozen.protocol_digest
    assert frozen.freeze_digest() != frozen.judge_digest
    assert frozen.as_json()["freeze_digest"] == frozen.freeze_digest()


def test_two_freezes_of_an_unchanged_tree_agree() -> None:
    assert protocol.freeze().freeze_digest() == protocol.freeze().freeze_digest()


def test_a_scorer_edit_moves_the_freeze_and_names_the_scorer(tmp_path: Path) -> None:
    edited = tmp_path / "judge.py"
    edited.write_text(Path(judge.__file__).read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    with pytest.raises(protocol.ProtocolInvalidated, match="scorer changed"):
        protocol.assert_unchanged(protocol.freeze(), judge_source=edited)


def test_an_unchanged_protocol_passes_silently() -> None:
    assert protocol.assert_unchanged(protocol.freeze()) is None


def test_a_missing_scorer_is_an_invalidation_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(protocol.ProtocolInvalidated, match="not committed"):
        protocol.judge_source_digest(tmp_path / "absent.py")


def test_frozen_values_enumerate_rather_than_sweep() -> None:
    """A sweep over module globals would absorb a new constant into the freeze
    silently, and drop a renamed one just as silently."""
    values = protocol.frozen_values()
    assert set(values) == {
        "protocol_version",
        "judge_version",
        "configurations",
        "scenario_counts",
        "treatment_a_margin",
        "treatment_b_margin",
        "marginal_band",
        "safety_tolerance",
        "latency_median_regression_limit",
        "latency_repeats",
        "latency_tail_rule",
        "human_risk_sample_size",
        "safety_review_is_exhaustive",
        "branches",
    }


# ---------------------------------------------------------------------------
# The corpus loader
# ---------------------------------------------------------------------------


def _corpus_document(scenario_entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"corpus_version": 1, "scenarios": scenario_entries}


def _entry(scenario_id: str, kind: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "scenario_id": scenario_id,
        "kind": kind,
        "description": "d",
        "tenant_id": _TENANT,
        "actor_id": "agent-alpha",
        "term": "retry budget",
        "required_item_keys": ["a"],
        "authorization": {"permitted_tenant_ids": [_TENANT], "permitted_task_ids": [_TASK]},
    }
    entry.update(overrides)
    return entry


def _full_document() -> dict[str, Any]:
    entries = [_entry(f"R{n:02d}", "task_resume") for n in range(20)]
    entries += [_entry(f"X{n:02d}", "cross_task_recall") for n in range(20)]
    return _corpus_document(entries)


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_well_formed_corpus_loads_and_digests_its_own_bytes(tmp_path: Path) -> None:
    import hashlib

    path = _write(tmp_path, _full_document())
    corpus = scenarios.load_corpus(path, expected_digest=None)
    assert len(corpus.scenarios) == 40
    assert corpus.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(corpus.by_kind("task_resume")) == 20


def test_a_missing_corpus_is_refused(tmp_path: Path) -> None:
    with pytest.raises(scenarios.CorpusInvalid, match="not committed"):
        scenarios.load_corpus(tmp_path / "absent.json", expected_digest=None)


def test_unreadable_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(scenarios.CorpusInvalid, match="not readable JSON"):
        scenarios.load_corpus(path, expected_digest=None)


def test_a_corpus_of_another_schema_version_is_refused(tmp_path: Path) -> None:
    document = _full_document()
    document["corpus_version"] = 99
    with pytest.raises(scenarios.CorpusInvalid, match="corpus_version"):
        scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)


def test_a_corpus_with_no_scenarios_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(scenarios.CorpusInvalid, match="no 'scenarios' list"):
        scenarios.load_corpus(_write(tmp_path, {"corpus_version": 1}), expected_digest=None)


def test_a_corpus_of_the_wrong_size_is_refused_before_content_is_trusted(tmp_path: Path) -> None:
    document = _full_document()
    document["scenarios"] = document["scenarios"][:5]
    with pytest.raises(scenarios.CorpusInvalid, match="changed size"):
        scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)


def test_a_duplicate_scenario_id_is_refused(tmp_path: Path) -> None:
    document = _full_document()
    document["scenarios"][1]["scenario_id"] = document["scenarios"][0]["scenario_id"]
    with pytest.raises(scenarios.CorpusInvalid, match="duplicate scenario_id"):
        scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)


def test_an_unknown_scenario_kind_is_refused(tmp_path: Path) -> None:
    document = _full_document()
    document["scenarios"][0]["kind"] = "freeform"
    with pytest.raises(scenarios.CorpusInvalid, match="changed size"):
        scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)


def test_an_unknown_classification_ceiling_is_refused(tmp_path: Path) -> None:
    document = _full_document()
    document["scenarios"][0]["authorization"]["max_classification"] = "top-secret"
    with pytest.raises(scenarios.CorpusInvalid, match="max_classification"):
        scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)


def test_a_scenario_requiring_nothing_is_refused() -> None:
    with pytest.raises(scenarios.CorpusInvalid, match="no required facts"):
        _scenario(required_item_keys=(), relevant_item_keys=())


def test_a_required_fact_outside_the_relevant_set_is_refused() -> None:
    with pytest.raises(scenarios.CorpusInvalid, match="not in the relevant set"):
        _scenario(required_item_keys=("a", "b"), relevant_item_keys=("a",))


def test_the_relevant_set_defaults_to_the_required_facts(tmp_path: Path) -> None:
    corpus = scenarios.load_corpus(_write(tmp_path, _full_document()), expected_digest=None)
    assert corpus.scenarios[0].relevant_item_keys == corpus.scenarios[0].required_item_keys


def test_a_reference_is_carried_through_when_present(tmp_path: Path) -> None:
    document = _full_document()
    document["scenarios"][0]["reference"] = {"source_system": "github", "external_id": "abc"}
    corpus = scenarios.load_corpus(_write(tmp_path, document), expected_digest=None)
    assert corpus.scenarios[0].reference == {"source_system": "github", "external_id": "abc"}


def test_the_corpus_path_is_derived_from_the_repository_root() -> None:
    assert scenarios.corpus_path(Path("/repo")) == Path("/repo/tests/fixtures/workspace_evaluation/scenarios.json")


# ---------------------------------------------------------------------------
# The scan and the configurations
# ---------------------------------------------------------------------------


def _candidate(key: str, text: str) -> treatments.Candidate:
    return treatments.Candidate(item_key=key, text=text, item=_item(key, goal=text))


def test_the_scan_ranks_by_similarity_and_applies_the_floor() -> None:
    scanned = treatments.exact_scan(
        query="retry budget",
        candidates=(_candidate("near", "the retry budget"), _candidate("far", "the salt cache")),
        embedder=_Embedder(),
    )
    assert [i.receipt_item_id.item_key for i in scanned.items] == ["near"]


def test_the_scan_over_no_candidates_returns_nothing_and_embeds_nothing() -> None:
    assert treatments.exact_scan(query="retry budget", candidates=(), embedder=_MiscountingEmbedder()).items == ()


def test_the_scan_truncates_at_its_limit_and_says_so() -> None:
    candidates = tuple(_candidate(f"c{n}", "retry budget drain") for n in range(4))
    scanned = treatments.exact_scan(query="retry budget", candidates=candidates, embedder=_Embedder(), limit=2)
    assert len(scanned.items) == 2
    assert scanned.truncated is True


def test_the_scan_refuses_a_vector_count_that_does_not_match_its_candidates() -> None:
    """A scan that cannot align scores to candidates would be scoring by
    position, which is how one checkpoint's similarity gets attributed to
    another."""
    with pytest.raises(ValueError, match="cannot count"):
        treatments.exact_scan(query="q", candidates=(_candidate("a", "a"),), embedder=_MiscountingEmbedder())


def test_cosine_refuses_vectors_of_different_widths() -> None:
    with pytest.raises(ValueError, match="dimension"):
        treatments.cosine([1.0, 0.0], [1.0])


def test_cosine_of_a_zero_vector_is_zero() -> None:
    assert treatments.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert treatments.cosine([1.0, 1.0], [1.0, 1.0]) == pytest.approx(1.0)


async def test_the_baseline_arm_is_truthfully_empty_and_reads_nothing() -> None:
    source = _Source(lexical_items=(_item("a"),))
    arm = treatments.workspace_arm_for(protocol.CONFIG_BASELINE, scenario=_scenario(), source=source, embedder=None)
    assert (await arm()).items == ()
    assert source.calls == []


async def test_treatment_a_unions_lexical_and_reference_without_double_counting() -> None:
    shared = _item("shared")
    source = _Source(lexical_items=(shared, _item("only-lexical")), reference_items=(shared,))
    arm = treatments.workspace_arm_for(protocol.CONFIG_TREATMENT_A, scenario=_scenario(), source=source, embedder=None)
    outcome = await arm()
    assert sorted(i.receipt_item_id.item_key for i in outcome.items) == ["only-lexical", "shared"]


async def test_an_item_one_arm_served_is_not_reported_as_withheld_by_another() -> None:
    """The withholding arm declined to return it; it did not revoke the other
    arm's authority to."""
    from contextplane.context.assembler import Exclusion

    served = _item("both")
    source = _Source(
        lexical_items=(),
        lexical_exclusions=(Exclusion(item_key="both", reason="classification restricted"),),
        reference_items=(served,),
    )
    arm = treatments.workspace_arm_for(protocol.CONFIG_TREATMENT_A, scenario=_scenario(), source=source, embedder=None)
    outcome = await arm()
    assert [i.receipt_item_id.item_key for i in outcome.items] == ["both"]
    assert outcome.exclusions == ()


async def test_treatment_b_resolves_the_authorized_set_before_it_reads_anything_else() -> None:
    source = _Source(candidates=(_candidate("a", "retry budget"),))
    arm = treatments.workspace_arm_for(
        protocol.CONFIG_TREATMENT_B, scenario=_scenario(), source=source, embedder=_Embedder()
    )
    await arm()
    assert source.calls[0] == "authorized_candidates"


async def test_treatment_b_adds_semantic_matches_to_the_lexical_ones() -> None:
    source = _Source(lexical_items=(_item("lex"),), candidates=(_candidate("sem", "retry budget drain"),))
    arm = treatments.workspace_arm_for(
        protocol.CONFIG_TREATMENT_B, scenario=_scenario(), source=source, embedder=_Embedder()
    )
    outcome = await arm()
    assert sorted(i.receipt_item_id.item_key for i in outcome.items) == ["lex", "sem"]


def test_an_unknown_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown configuration"):
        treatments.workspace_arm_for("treatment-z", scenario=_scenario(), source=_Source(), embedder=None)


def test_the_semantic_treatment_without_a_model_is_refused() -> None:
    with pytest.raises(ValueError, match="needs an embedder"):
        treatments.workspace_arm_for(protocol.CONFIG_TREATMENT_B, scenario=_scenario(), source=_Source(), embedder=None)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


async def test_a_clean_run_scores_the_first_resolution_and_times_every_repeat() -> None:
    scenario = _scenario()
    run = await harness.run_scenario(
        scenario=scenario,
        configuration=protocol.CONFIG_TREATMENT_A,
        source=_Source(lexical_items=(_item("a"),)),
        other_arms=_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=3,
    )
    assert run.score.recall == 1.0
    assert len(run.durations_ms) == 3
    assert run.median_ms > 0


async def test_a_raising_source_is_recorded_as_a_failure_for_that_configuration() -> None:
    scenario = _scenario()
    run = await harness.run_scenario(
        scenario=scenario,
        configuration=protocol.CONFIG_TREATMENT_A,
        source=_Source(raises=RuntimeError("refused")),
        other_arms=_arms(scenario),
        embedder=None,
        now=_NOW,
        repeats=3,
    )
    assert run.score.errored is True
    assert len(run.durations_ms) == 1, "a failing configuration stops repeating rather than timing the failure"


async def test_an_infrastructure_error_propagates_out_of_the_scenario() -> None:
    scenario = _scenario()
    with pytest.raises(harness.InfrastructureError):
        await harness.run_scenario(
            scenario=scenario,
            configuration=protocol.CONFIG_TREATMENT_A,
            source=_Source(raises=harness.InfrastructureError("db gone")),
            other_arms=_arms(scenario),
            embedder=None,
            now=_NOW,
            repeats=1,
        )


async def test_a_batch_runs_every_configuration_over_every_scenario() -> None:
    corpus = _corpus(_scenario("U-01"), _scenario("U-02"))
    batch = await harness.run_batch(
        corpus=corpus,
        source=_Source(lexical_items=(_item("a"),)),
        other_arms=_arms,
        embedder=_Embedder(),
        now=_NOW,
        repeats=1,
    )
    assert [r.configuration for r in batch.results] == list(protocol.CONFIGURATIONS)
    for result in batch.results:
        assert len(result.runs) == 2
    assert batch.corpus_digest == "unit-corpus-digest"


async def test_a_batch_reports_the_baseline_and_the_treatment_differently() -> None:
    """The non-vacuity claim at batch level: the ablation and the treatment must
    not produce the same number over the same corpus."""
    corpus = _corpus(_scenario("U-01"))
    batch = await harness.run_batch(
        corpus=corpus,
        source=_Source(lexical_items=(_item("a"),)),
        other_arms=_arms,
        embedder=_Embedder(),
        now=_NOW,
        repeats=1,
    )
    assert batch.by_configuration(protocol.CONFIG_BASELINE).mean_recall == 0.0
    assert batch.by_configuration(protocol.CONFIG_TREATMENT_A).mean_recall == 1.0


async def test_an_infrastructure_failure_invalidates_the_batch_not_the_scenario() -> None:
    with pytest.raises(harness.BatchInvalidated, match="rerun whole"):
        await harness.run_batch(
            corpus=_corpus(_scenario()),
            source=_Source(raises=harness.InfrastructureError("db gone")),
            other_arms=_arms,
            embedder=None,
            now=_NOW,
            repeats=1,
        )


async def test_an_infrastructure_failure_while_preparing_arms_also_invalidates() -> None:
    def _broken_arms(scenario: scenarios.Scenario) -> dict[str, Any]:
        raise harness.InfrastructureError("the fixture host went away")

    with pytest.raises(harness.BatchInvalidated, match="rerun whole"):
        await harness.run_batch(
            corpus=_corpus(_scenario()),
            source=_Source(),
            other_arms=_broken_arms,
            embedder=None,
            now=_NOW,
            repeats=1,
        )


async def test_a_batch_with_no_configuration_is_refused() -> None:
    with pytest.raises(harness.BatchInvalidated, match="measured nothing"):
        await harness.run_batch(
            corpus=_corpus(_scenario()),
            source=_Source(),
            other_arms=_arms,
            embedder=None,
            now=_NOW,
            configurations=(),
        )


def test_an_empty_configuration_result_reports_zeros_rather_than_raising() -> None:
    empty = harness.ConfigurationResult(configuration=protocol.CONFIG_BASELINE, runs=())
    assert empty.mean_recall == 0.0
    assert empty.mean_precision == 0.0
    assert empty.median_latency_ms == 0.0
    assert empty.slowest_scenario_median_ms == 0.0
    assert empty.errored_scenarios == ()


def test_a_run_with_no_timings_reports_a_zero_median() -> None:
    run = harness.ScenarioRun(score=_score(_envelope(), _scenario()), durations_ms=())
    assert run.median_ms == 0.0


def test_the_configuration_aggregate_separates_the_median_from_the_tail() -> None:
    runs = tuple(
        harness.ScenarioRun(score=_score(_envelope(_item("a")), _scenario(f"U-{n}")), durations_ms=(float(n),))
        for n in (1, 2, 30)
    )
    result = harness.ConfigurationResult(configuration=protocol.CONFIG_TREATMENT_A, runs=runs)
    assert result.median_latency_ms == 2.0
    assert result.slowest_scenario_median_ms == 30.0


def test_the_aggregate_carries_every_safety_failure_and_every_errored_scenario() -> None:
    leaking = _score(_envelope(_item("x", task_id=_ELSEWHERE)), _scenario("U-leak"))
    failing = _score(None, _scenario("U-err"))
    runs = (
        harness.ScenarioRun(score=leaking, durations_ms=(1.0,)),
        harness.ScenarioRun(score=failing, durations_ms=(1.0,)),
    )
    result = harness.ConfigurationResult(configuration=protocol.CONFIG_TREATMENT_B, runs=runs)
    assert [s.scenario_id for s in result.safety_failures] == ["U-leak"]
    assert result.errored_scenarios == ("U-err",)


def test_asking_a_batch_for_a_configuration_it_does_not_hold_names_it() -> None:
    batch = harness.BatchResult(collected_under=protocol.freeze(), corpus_digest="d", results=(), human_risk_sample=())
    with pytest.raises(KeyError, match="treatment-z"):
        batch.by_configuration("treatment-z")


def test_the_human_risk_sample_is_deterministic_and_spans_both_kinds() -> None:
    corpus = _corpus(
        *(_scenario(f"R{n:02d}") for n in range(20)),
        *(_scenario(f"X{n:02d}", kind="cross_task_recall") for n in range(20)),
    )
    drawn = harness.human_risk_sample(corpus)
    assert len(drawn) == protocol.HUMAN_RISK_SAMPLE_SIZE
    assert drawn == harness.human_risk_sample(corpus)
    assert any(i.startswith("R") for i in drawn)
    assert any(i.startswith("X") for i in drawn)


def test_the_sample_survives_a_corpus_holding_only_one_kind() -> None:
    drawn = harness.human_risk_sample(_corpus(*(_scenario(f"R{n:02d}") for n in range(20))))
    assert len(drawn) == 5
    assert all(i.startswith("R") for i in drawn)


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


async def _batch() -> harness.BatchResult:
    return await harness.run_batch(
        corpus=_corpus(_scenario("U-01"), _scenario("U-02")),
        source=_Source(lexical_items=(_item("a"),)),
        other_arms=_arms,
        embedder=_Embedder(),
        now=_NOW,
        repeats=1,
    )


async def test_evidence_is_sealed_and_verifies_against_its_own_content() -> None:
    signed = evidence.build(await _batch(), signing_key=_KEY)
    assert signed.verify(_KEY) is True
    assert json.loads(signed.as_json())["digest"] == signed.digest


async def test_edited_content_fails_verification() -> None:
    signed = evidence.build(await _batch(), signing_key=_KEY)
    edited = dataclasses.replace(signed, document={**signed.document, "corpus_digest": "0" * 64})
    assert edited.verify(_KEY) is False


async def test_a_re_digested_edit_still_fails_without_the_key() -> None:
    """The digest alone seals nothing: an editor can recompute it. The signature
    is what makes the seal need a key."""
    signed = evidence.build(await _batch(), signing_key=_KEY)
    document = {**signed.document, "corpus_digest": "0" * 64}
    forged = dataclasses.replace(signed, document=document, digest=evidence._digest(document))
    assert forged.verify(_KEY) is False


async def test_a_different_key_does_not_verify() -> None:
    signed = evidence.build(await _batch(), signing_key=_KEY)
    assert signed.verify(b"another-key") is False


async def test_an_unsigned_result_is_refused() -> None:
    with pytest.raises(evidence.EvidenceUnsigned, match="cannot be attributed"):
        evidence.build(await _batch(), signing_key=b"")


async def test_a_moved_protocol_blocks_the_write_up(tmp_path: Path) -> None:
    batch = await _batch()
    edited = tmp_path / "judge.py"
    edited.write_text(Path(judge.__file__).read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    with pytest.raises(protocol.ProtocolInvalidated):
        evidence.build(batch, signing_key=_KEY, judge_source=edited)


async def test_the_document_carries_the_counts_a_reader_re_derives_the_mean_from() -> None:
    signed = evidence.build(await _batch(), signing_key=_KEY)
    entry = signed.document["configurations"][0]
    per_scenario = entry["primary_metric"]["per_scenario"]
    assert [s["scenario_id"] for s in per_scenario] == ["U-01", "U-02"]
    assert all({"required_found", "required_total", "recall", "errored"} <= set(s) for s in per_scenario)


async def test_the_document_names_no_branch_and_no_decision() -> None:
    serialized = evidence.build(await _batch(), signing_key=_KEY).as_json()
    assert all(branch not in serialized for branch in protocol.BRANCHES)
    assert '"decision"' not in serialized


async def test_the_document_reports_safety_failures_in_full() -> None:
    batch = await harness.run_batch(
        corpus=_corpus(_scenario("U-01", facts=_facts(permitted_task_ids=frozenset()))),
        source=_Source(lexical_items=(_item("a"),)),
        other_arms=_arms,
        embedder=_Embedder(),
        now=_NOW,
        repeats=1,
    )
    document = evidence.build(batch, signing_key=_KEY).document
    treatment = next(c for c in document["configurations"] if c["configuration"] == protocol.CONFIG_TREATMENT_A)
    assert treatment["safety"]["failure_count"] == 1
    assert treatment["safety"]["review_is_exhaustive"] is True
    assert treatment["safety"]["failures"][0]["violations"][0]["kind"] == judge.VIOLATION_AUDIENCE


async def test_the_document_carries_the_tail_rule_beside_the_latency_it_qualifies() -> None:
    document = evidence.build(await _batch(), signing_key=_KEY).document
    latency = document["configurations"][0]["secondary_metrics"]["resolution_latency_ms"]
    assert latency["tail_rule"] == protocol.LATENCY_TAIL_RULE
    assert "median_of_scenario_medians" in latency


# ---------------------------------------------------------------------------
# The world, and the pairing that makes the corpus usable
# ---------------------------------------------------------------------------


def _world_entry(scenario_id: str, *, actor: str, placements: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "actor_id": actor,
        "checkpoints": [
            {"item_key": key, "task_id": task, "sequence": n + 1, "goal": f"goal {n}", "author": actor}
            for n, (key, task) in enumerate(placements)
        ],
    }


def _world_document(entries: dict[str, Any]) -> dict[str, Any]:
    return {"world_version": 1, "scenarios": entries}


def _write_world(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "world.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _paired(tmp_path: Path) -> tuple[scenarios.Corpus, scenarios.World]:
    """A one-scenario corpus and a world that pairs with it cleanly."""
    entry = _entry("R00", "task_resume", required_item_keys=["k1"])
    document = _corpus_document([entry])
    corpus = scenarios.Corpus(version=1, scenarios=(scenarios._scenario_from(entry),), digest="unit-corpus")
    world = scenarios.load_world(
        _write_world(
            tmp_path, _world_document({"R00": _world_entry("R00", actor="a-r00", placements=[("k1", _TASK)])})
        ),
        expected_digest=None,
    )
    assert document  # the corpus document is built above for shape parity
    return corpus, world


def test_a_well_formed_world_loads_and_digests_its_own_bytes(tmp_path: Path) -> None:
    import hashlib

    path = _write_world(tmp_path, _world_document({"R00": _world_entry("R00", actor="a", placements=[("k1", _TASK)])}))
    world = scenarios.load_world(path, expected_digest=None)
    assert world.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert world.entries["R00"].actor_id == "a"
    assert world.entries["R00"].keys() == frozenset({"k1"})


def test_a_missing_world_is_refused_with_the_reason_it_matters(tmp_path: Path) -> None:
    with pytest.raises(scenarios.WorldInvalid, match="authored at run time"):
        scenarios.load_world(tmp_path / "absent.json", expected_digest=None)


def test_an_unreadable_world_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "world.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(scenarios.WorldInvalid, match="not readable JSON"):
        scenarios.load_world(path, expected_digest=None)


def test_a_world_of_another_schema_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(scenarios.WorldInvalid, match="world_version"):
        scenarios.load_world(_write_world(tmp_path, {"world_version": 9, "scenarios": {}}), expected_digest=None)


def test_a_world_with_no_scenarios_object_is_refused(tmp_path: Path) -> None:
    with pytest.raises(scenarios.WorldInvalid, match="no 'scenarios' object"):
        scenarios.load_world(_write_world(tmp_path, {"world_version": 1}), expected_digest=None)


def test_a_scenario_the_world_places_nothing_for_is_refused(tmp_path: Path) -> None:
    document = _world_document({"R00": {"actor_id": "a", "checkpoints": []}})
    with pytest.raises(scenarios.WorldInvalid, match="nothing can be recalled"):
        scenarios.load_world(_write_world(tmp_path, document), expected_digest=None)


# --- the two pins ---------------------------------------------------------------


def test_a_corpus_whose_bytes_are_not_the_pinned_bytes_is_refused(tmp_path: Path) -> None:
    """The gap this closes: the digest used to be stamped into the result and
    never compared, so a swapped corpus produced a document that faithfully
    reported the digest of whatever it had actually read."""
    with pytest.raises(scenarios.CorpusInvalid, match="not the pinned corpus"):
        scenarios.load_corpus(_write(tmp_path, _full_document()), expected_digest="0" * 64)


def test_a_world_whose_bytes_are_not_the_pinned_bytes_is_refused(tmp_path: Path) -> None:
    document = _world_document({"R00": _world_entry("R00", actor="a", placements=[("k1", _TASK)])})
    with pytest.raises(scenarios.CorpusInvalid, match="not the pinned world"):
        scenarios.load_world(_write_world(tmp_path, document), expected_digest="0" * 64)


def test_the_pins_are_the_digests_of_the_committed_files() -> None:
    """Restated from disk rather than from the constants, so a fixture edited
    without re-pinning fails here instead of at the next campaign."""
    import hashlib

    root = Path(scenarios.__file__).resolve().parents[3]
    assert hashlib.sha256(scenarios.corpus_path(root).read_bytes()).hexdigest() == protocol.FROZEN_CORPUS_DIGEST
    assert hashlib.sha256(scenarios.world_path(root).read_bytes()).hexdigest() == protocol.FROZEN_WORLD_DIGEST


def test_pinning_the_inputs_does_not_move_the_protocol_freeze() -> None:
    """The corpus and world are pinned beside the freeze, not folded into it.

    Folding them in would move `protocol_digest` whenever a scenario's wording
    changed, which conflates "the rules moved" with "the inputs moved" — and
    those need opposite responses.
    """
    assert "corpus" not in protocol.frozen_values()
    assert "world" not in protocol.frozen_values()
    assert protocol.FROZEN_CORPUS_DIGEST not in json.dumps(protocol.frozen_values())


# --- the pairing rules ----------------------------------------------------------


def test_a_corpus_and_world_that_describe_the_same_evaluation_pair(tmp_path: Path) -> None:
    corpus, world = _paired(tmp_path)
    assert scenarios.assert_pairs(corpus, world) is None


def test_a_scenario_with_no_world_is_refused(tmp_path: Path) -> None:
    corpus, world = _paired(tmp_path)
    extra = scenarios._scenario_from(_entry("R01", "task_resume", required_item_keys=["k9"]))
    with pytest.raises(scenarios.WorldInvalid, match="without a world"):
        scenarios.assert_pairs(dataclasses.replace(corpus, scenarios=(*corpus.scenarios, extra)), world)


def test_a_world_entry_with_no_scenario_is_refused(tmp_path: Path) -> None:
    corpus, world = _paired(tmp_path)
    orphan = dict(world.entries)
    orphan["R99"] = world.entries["R00"]
    with pytest.raises(scenarios.WorldInvalid, match="without a scenario"):
        scenarios.assert_pairs(corpus, dataclasses.replace(world, entries=orphan))


def test_a_required_fact_the_world_never_places_is_refused(tmp_path: Path) -> None:
    """Unreachable by construction: the configuration would be scored against an
    answer that does not exist, and score zero for a reason that is not about
    the system."""
    corpus, world = _paired(tmp_path)
    unplaceable = scenarios._scenario_from(_entry("R00", "task_resume", required_item_keys=["k1", "never-placed"]))
    with pytest.raises(scenarios.WorldInvalid, match="not placed by the world"):
        scenarios.assert_pairs(dataclasses.replace(corpus, scenarios=(unplaceable,)), world)


def test_a_world_that_places_a_checkpoint_outside_the_declared_audience_is_refused(tmp_path: Path) -> None:
    """The world supplies content and position; widening an audience would
    manufacture the safety violation it exists to avoid."""
    corpus, _ = _paired(tmp_path)
    wandering = scenarios.load_world(
        _write_world(
            tmp_path,
            _world_document({"R00": _world_entry("R00", actor="a", placements=[("k1", _ELSEWHERE)])}),
        ),
        expected_digest=None,
    )
    with pytest.raises(scenarios.WorldInvalid, match="outside the audience"):
        scenarios.assert_pairs(corpus, wandering)


def test_two_scenarios_sharing_an_actor_are_refused(tmp_path: Path) -> None:
    """The defect this whole file exists to fix, stated as a rule a test can
    hold: one shared actor holds both scenarios' grants, so each serves the
    other's items."""
    first = scenarios._scenario_from(_entry("R00", "task_resume", required_item_keys=["k1"]))
    second = scenarios._scenario_from(_entry("R01", "task_resume", required_item_keys=["k2"]))
    corpus = scenarios.Corpus(version=1, scenarios=(first, second), digest="d")
    shared = scenarios.load_world(
        _write_world(
            tmp_path,
            _world_document(
                {
                    "R00": _world_entry("R00", actor="same-actor", placements=[("k1", _TASK)]),
                    "R01": _world_entry("R01", actor="same-actor", placements=[("k2", _TASK)]),
                }
            ),
        ),
        expected_digest=None,
    )
    with pytest.raises(scenarios.WorldInvalid, match="share actor"):
        scenarios.assert_pairs(corpus, shared)
