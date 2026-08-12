"""The approved retrieval branch is enforced by the product, not by a promise.

A pre-registered campaign measured three retrieval configurations and the
recorded branch is `semantic-adds-value-and-passes-safety`. This suite is what
stops that from being a sentence in a document. It tests the properties that
make the decision binding, in the order a defect in any one of them should be
caught:

1. The committed artifact loads, names a branch the protocol froze, and carries
   the digests the protocol computes **today** -- so a threshold edited after the
   fact stops the process instead of quietly re-scoring it.
2. Loading is fail-closed in every direction a file can be wrong: missing,
   unreadable, unknown branch, moved digest, widened arm, arm list disagreeing
   with the branch.
3. The three branches that do not approve semantic recall do not get it, and the
   branch that fails the baseline refuses to activate the arm at all.
4. The approved arm is authorized-set-first: the scan sees only candidates it is
   handed, honours the floor and the limit the artifact records, and has no path
   that widens either.
5. There is no runtime configuration that can be more permissive than the
   artifact, and the test asserts that by looking for one.

What it deliberately does not do is re-run the campaign or re-derive the numbers.
Collecting observations and interpreting them are separate acts with separate
owners; this suite tests that the product obeys the interpretation, which is a
different claim and the only one code can make.
"""

from __future__ import annotations

import dataclasses
import json
import math
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from contextplane.context import semantic_workspace
from contextplane.context.assembler import ArmOutcome, contextual_item
from contextplane.context.evaluation import protocol
from contextplane.context.schemas.trust import TrustMetadataV1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from contextplane.context.schemas.envelope import ContextItemV1

_COMMITTED = json.loads(semantic_workspace.DECISION_PATH.read_text(encoding="utf-8"))


def _artifact(**overrides: Any) -> dict[str, Any]:
    """The committed artifact with named sections replaced, deep-copied.

    Mutating a section of the real document in place would make one test's edit
    visible to the next, and a fail-closed suite whose fixtures leak is a suite
    that can pass because a previous test broke something.
    """
    raw = json.loads(json.dumps(_COMMITTED))
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(section), dict):
            raw[section].update(value)
        else:
            raw[section] = value
    return raw


def _load(raw: dict[str, Any], tmp_path: Path) -> semantic_workspace.RecallDecision:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "workspace_recall_decision.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    # `load_decision` is cached on its argument, and every call here passes a
    # distinct tmp path, so no test reads another's cached decision.
    return semantic_workspace.load_decision(target)


def _refuses(raw: dict[str, Any], tmp_path: Path) -> str:
    with pytest.raises(semantic_workspace.DecisionUnavailable) as excinfo:
        _load(raw, tmp_path)
    return str(excinfo.value)


# -- 1. the committed artifact is the frozen protocol's ------------------------


def test_the_committed_decision_loads_and_names_a_frozen_branch() -> None:
    decision = semantic_workspace.load_decision()
    assert decision.branch in protocol.BRANCHES
    assert decision.branch == "semantic-adds-value-and-passes-safety"
    assert decision.semantic_approved is True
    assert decision.lexical_approved is True
    assert decision.reviewed_on == "2026-08-10"


def test_the_recorded_digests_are_the_ones_the_decision_was_taken_under() -> None:
    """Not merely present -- equal to the identity this decision names.

    A digest stamped into a document and never compared is a value, not a gate --
    the evaluation corpus shipped with exactly that hole before it was closed.
    Every digest in the artifact is checked here against the identity the run was
    taken under, so editing one after the decision was recorded fails this test
    rather than silently changing what the decision means.

    The right-hand side is deliberately the recorded pre-cut identity rather than
    what this tree computes today. The decision is evidence of a run that
    happened; checking evidence against a value that follows the tree would let
    any later edit present itself as the thing that was measured, which is the
    hole described above reopened from the other side.
    """
    recorded = _COMMITTED["protocol"]
    identity = protocol.V1_ERA_IDENTITY
    assert recorded["protocol_version"] == identity["protocol_version"]
    assert recorded["judge_version"] == identity["judge_version"]
    assert recorded["protocol_digest"] == identity["protocol_digest"]
    assert recorded["judge_digest"] == identity["judge_digest"]
    assert recorded["freeze_digest"] == identity["freeze_digest"]
    assert recorded["corpus_digest"] == identity["corpus_digest"]
    assert recorded["world_digest"] == identity["world_digest"]


def test_the_void_safety_dimension_travels_with_the_decision() -> None:
    """Cross-tenant no-harm is unmeasured, and the artifact has to keep saying so.

    The recorded decision makes this a condition of the branch: a downstream
    artifact citing it as evidence of cross-tenant isolation is citing it wrongly,
    and the only thing that stops the caveat being dropped in a later edit is a
    test that fails when it is.
    """
    decision = semantic_workspace.load_decision()
    assert "cross_tenant" in decision.void_safety_dimensions

    gate = next(g for g in _COMMITTED["safety_gates"] if g["dimension"] == "cross_tenant")
    assert gate["status"] == "void-unmeasured"
    assert "UNMEASURED" in gate["detail"]
    assert "tenant predicates in the workspace recall queries" in gate["detail"]


def test_the_unreviewed_human_sample_is_recorded_as_still_open() -> None:
    decision = semantic_workspace.load_decision()
    assert "human_risk_sample" in decision.open_review_obligations

    obligation = next(o for o in _COMMITTED["open_review_obligations"] if o["item"] == "human_risk_sample")
    assert obligation["reviewed"] is False
    assert obligation["consequence_if_a_concern_is_found"] == "semantic-adds-value-but-fails-safety"


# -- 2. loading is fail-closed -------------------------------------------------


def test_a_missing_artifact_refuses_rather_than_disabling_recall(tmp_path: Path) -> None:
    """Deleting the file must not be a way to turn task memory off quietly."""
    with pytest.raises(semantic_workspace.DecisionUnavailable, match="no approved retrieval branch is committed"):
        semantic_workspace.load_decision(tmp_path / "absent.json")


def test_an_unreadable_artifact_refuses(tmp_path: Path) -> None:
    target = tmp_path / "workspace_recall_decision.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(semantic_workspace.DecisionUnavailable, match="not readable JSON"):
        semantic_workspace.load_decision(target)


def test_a_branch_outside_the_frozen_table_refuses(tmp_path: Path) -> None:
    """A fifth branch is not a branch. The four names were frozen before collection."""
    message = _refuses(_artifact(decision={"branch": "semantic-adds-value-probably"}), tmp_path)
    assert "not one of the four the protocol froze" in message


@pytest.mark.parametrize(
    "field",
    ["protocol_digest", "judge_digest", "freeze_digest", "corpus_digest", "world_digest", "protocol_version"],
)
def test_a_moved_digest_refuses(field: str, tmp_path: Path) -> None:
    """Each pinned input is checked, not just the one a reader happens to know.

    Parameterised rather than asserted once on a representative field: the last
    time this codebase trusted "the digest is recorded" it turned out only one of
    five was compared, and a single-field test would have passed throughout.
    """
    message = _refuses(_artifact(protocol={field: "0" * 64}), tmp_path)
    assert field in message
    assert "no longer holds" in message


def test_the_arm_list_may_not_disagree_with_the_branch(tmp_path: Path) -> None:
    """Turning semantic on under a branch that did not approve it is not an edit
    this file is allowed to express."""
    raw = _artifact(decision={"branch": "lexical-sufficient"})
    # `arms.semantic` is left true from the committed document, which is exactly
    # the edit somebody would make to keep the arm while changing the branch.
    message = _refuses(raw, tmp_path)
    assert "the branch decides which arms run" in message


def test_a_broad_ann_arm_refuses(tmp_path: Path) -> None:
    """No configuration in this protocol measured broad ANN, so nothing can approve it."""
    message = _refuses(_artifact(arms={"broad_ann": True}), tmp_path)
    assert "broad ANN" in message


def test_an_arm_kind_the_code_does_not_implement_refuses(tmp_path: Path) -> None:
    message = _refuses(_artifact(approved_arm={"kind": "approximate-nearest-neighbour"}), tmp_path)
    assert "have come apart" in message


def test_a_semantic_approval_measured_as_another_configuration_refuses(tmp_path: Path) -> None:
    message = _refuses(_artifact(approved_arm={"measured_as": protocol.CONFIG_TREATMENT_A}), tmp_path)
    assert protocol.CONFIG_TREATMENT_B in message


@pytest.mark.parametrize("floor", [-0.1, 1.5, "0.3", None])
def test_a_floor_that_is_not_a_similarity_refuses(floor: object, tmp_path: Path) -> None:
    message = _refuses(_artifact(approved_arm={"similarity_floor": floor}), tmp_path)
    assert "not a cosine similarity" in message


@pytest.mark.parametrize("limit", [0, -1, 2.5, None])
def test_a_limit_that_is_not_a_page_size_refuses(limit: object, tmp_path: Path) -> None:
    message = _refuses(_artifact(approved_arm={"limit": limit}), tmp_path)
    assert "not a page size" in message


def test_an_undated_decision_refuses(tmp_path: Path) -> None:
    """A decision with no review date cannot be revisited, and one that cannot be
    revisited is not reviewable."""
    raw = _artifact()
    del raw["decision"]["reviewed_on"]
    assert "carries no review date" in _refuses(raw, tmp_path)


@pytest.mark.parametrize("section", ["decision", "approved_arm", "protocol", "arms", "safety_gates"])
def test_a_missing_section_refuses(section: str, tmp_path: Path) -> None:
    raw = _artifact()
    del raw[section]
    assert section in _refuses(raw, tmp_path)


# -- 3. the branches that do not approve semantic recall do not get it ---------


def _branch(name: str, tmp_path: Path) -> semantic_workspace.RecallDecision:
    """The committed artifact re-recorded on another branch, arm list made honest.

    Rewriting `arms` to match is not a workaround for the check above -- it is
    what a real re-decision on that branch would say. The check above tests the
    dishonest edit; this helper builds the honest one so the branch's *behaviour*
    can be tested.
    """
    semantic = name == "semantic-adds-value-and-passes-safety"
    lexical = name != "task-memory-fails-baseline"
    return _load(
        _artifact(
            decision={"branch": name},
            arms={"lexical": lexical, "reference": lexical, "semantic": semantic, "broad_ann": False},
        ),
        tmp_path,
    )


@pytest.mark.parametrize(
    "branch",
    ["lexical-sufficient", "semantic-adds-value-but-fails-safety", "task-memory-fails-baseline"],
)
def test_the_three_non_approving_branches_refuse_a_semantic_scan(branch: str, tmp_path: Path) -> None:
    decision = _branch(branch, tmp_path)
    assert decision.semantic_approved is False
    with pytest.raises(semantic_workspace.SemanticRecallNotApproved, match=branch):
        decision.require_semantic()


def test_semantic_failing_safety_keeps_lexical_because_lexical_passed(tmp_path: Path) -> None:
    """The branch names a semantic safety failure, not a lexical one.

    Recorded as its own test because the protocol's rule is conditional -- lexical
    remains only when its own threshold passed -- and a reader of the branch name
    alone could reasonably conclude the whole arm goes away.
    """
    decision = _branch("semantic-adds-value-but-fails-safety", tmp_path)
    assert decision.lexical_approved is True
    decision.require_service()


def test_a_failed_baseline_refuses_to_activate_workspace_recall_at_all(tmp_path: Path) -> None:
    decision = _branch("task-memory-fails-baseline", tmp_path)
    assert decision.lexical_approved is False
    assert decision.semantic_approved is False
    with pytest.raises(semantic_workspace.DecisionUnavailable, match="did not clear its own baseline"):
        decision.require_service()


# -- 4. the approved arm is authorized-set-first -------------------------------


class _Embedder:
    """A deterministic embedder: one axis per vocabulary word, counts as weights.

    Real cosine arithmetic over a toy space, rather than a mock returning
    pre-decided scores. A mock would let the scan return the right items while
    doing the wrong thing with the floor, which is the defect most worth
    catching here.
    """

    model_version = "conformance-fake-v1"

    def __init__(self, vocabulary: Sequence[str]) -> None:
        self._vocabulary = tuple(vocabulary)

    def encode(self, texts: list[str]) -> Sequence[Sequence[float]]:
        return [[float(text.lower().split().count(word)) for word in self._vocabulary] for text in texts]


def _item(key: str) -> ContextItemV1:
    return contextual_item(
        block="workspace",
        source="task-memory",
        item_key=key,
        payload={"goal": key},
        trust=TrustMetadataV1(
            trust="asserted",
            source="task-memory",
            assertion_kind="annotation",
            authority=f"task:{uuid.uuid4()}",
            freshness=None,
            mutability="immutable",
            attribution="agent",
            classification="internal",
        ),
    )


def _candidate(key: str, text: str) -> semantic_workspace.Candidate:
    return semantic_workspace.Candidate(item_key=key, text=text, item=_item(key))


def _approved(tmp_path: Path, **approved_arm: Any) -> semantic_workspace.RecallDecision:
    """The approved branch, optionally with the arm's floor or limit overridden."""
    if not approved_arm:
        return _branch("semantic-adds-value-and-passes-safety", tmp_path)
    return _load(_artifact(approved_arm=approved_arm), tmp_path)


def test_the_scan_serves_only_the_candidates_it_was_handed(tmp_path: Path) -> None:
    """The authorization boundary is the candidate list, and there is no other.

    The scan is handed one candidate and a query that matches a second, absent
    one perfectly. A design that could reach past its inputs would serve the
    absent item; this one cannot, because it has no read of its own.
    """
    decision = _approved(tmp_path)
    embedder = _Embedder(["alpha", "beta"])
    outcome = semantic_workspace.exact_scan(
        query="beta",
        candidates=(_candidate("only-alpha", "alpha"),),
        embedder=embedder,
        decision=decision,
    )
    assert outcome.items == ()


def test_the_scan_honours_the_floor_the_artifact_records(tmp_path: Path) -> None:
    """A candidate below the floor is not served, however few candidates there are.

    Top-k alone always returns something, so a query with no semantically related
    material would be answered with the least-unrelated checkpoint in the task.
    The floor is what makes "nothing matched" expressible.
    """
    decision = _approved(tmp_path)
    embedder = _Embedder(["alpha", "beta", "gamma"])
    # "alpha beta" against "alpha gamma": cosine 0.5, above the 0.30 floor.
    above = semantic_workspace.exact_scan(
        query="alpha beta",
        candidates=(_candidate("half", "alpha gamma"),),
        embedder=embedder,
        decision=decision,
    )
    assert [i.receipt_item_id.item_key for i in above.items] == ["half"]

    # Raising the floor above that similarity withholds the same candidate, so
    # the floor is doing the work rather than the candidate being unmatchable.
    strict = _approved(tmp_path / "strict", similarity_floor=0.75)
    withheld = semantic_workspace.exact_scan(
        query="alpha beta",
        candidates=(_candidate("half", "alpha gamma"),),
        embedder=embedder,
        decision=strict,
    )
    assert withheld.items == ()


def test_the_scan_honours_the_limit_and_reports_truncation(tmp_path: Path) -> None:
    decision = _approved(tmp_path, limit=2)
    embedder = _Embedder(["alpha"])
    candidates = tuple(_candidate(f"c{n}", "alpha") for n in range(5))
    outcome = semantic_workspace.exact_scan(query="alpha", candidates=candidates, embedder=embedder, decision=decision)
    assert len(outcome.items) == 2
    assert outcome.truncated is True


def test_ties_break_on_item_key_so_two_runs_agree(tmp_path: Path) -> None:
    """Two runs over the same authorized set must not differ by row order."""
    decision = _approved(tmp_path, limit=2)
    embedder = _Embedder(["alpha"])
    forward = tuple(_candidate(k, "alpha") for k in ("c", "a", "b"))
    outcome = semantic_workspace.exact_scan(query="alpha", candidates=forward, embedder=embedder, decision=decision)
    reversed_outcome = semantic_workspace.exact_scan(
        query="alpha", candidates=tuple(reversed(forward)), embedder=embedder, decision=decision
    )
    keys = [i.receipt_item_id.item_key for i in outcome.items]
    assert keys == ["a", "b"]
    assert keys == [i.receipt_item_id.item_key for i in reversed_outcome.items]


def test_the_scan_refuses_under_a_branch_that_did_not_approve_it(tmp_path: Path) -> None:
    """The gate is on the scan itself, not only on the caller that reaches it."""
    decision = _branch("lexical-sufficient", tmp_path)
    with pytest.raises(semantic_workspace.SemanticRecallNotApproved):
        semantic_workspace.exact_scan(
            query="alpha",
            candidates=(_candidate("a", "alpha"),),
            embedder=_Embedder(["alpha"]),
            decision=decision,
        )


def test_an_embedder_that_miscounts_its_vectors_raises_rather_than_misaligning(tmp_path: Path) -> None:
    """Scores aligned to the wrong candidates would serve the wrong items silently."""

    class _Short:
        model_version = "short"

        def encode(self, texts: list[str]) -> Sequence[Sequence[float]]:
            return [[1.0] for _ in texts][:-1]

    with pytest.raises(ValueError, match="cannot align scores to candidates"):
        semantic_workspace.exact_scan(
            query="alpha",
            candidates=(_candidate("a", "alpha"), _candidate("b", "beta")),
            embedder=_Short(),
            decision=_approved(tmp_path),
        )


def test_a_zero_vector_scores_zero_rather_than_raising() -> None:
    """The stub embedder produces zero vectors, and they are similar to nothing."""
    assert semantic_workspace.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert math.isclose(semantic_workspace.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
    with pytest.raises(ValueError, match="cannot compare"):
        semantic_workspace.cosine([1.0], [1.0, 0.0])


def test_merging_serves_an_item_one_arm_withheld_and_another_returned() -> None:
    """A checkpoint found lexically and semantically is one item served once."""
    item = _item("shared")
    merged = semantic_workspace.merge_outcomes(
        ArmOutcome(items=(item,)),
        ArmOutcome(items=(item,), truncated=True),
    )
    assert [i.receipt_item_id.item_key for i in merged.items] == ["shared"]
    assert merged.truncated is True


# -- 5. no runtime configuration can be more permissive ------------------------


def test_no_setting_can_turn_semantic_workspace_recall_on() -> None:
    """The evidence is the switch, so there must be no second switch.

    Asserted by reading `Settings` rather than by reviewing the diff that
    introduced this arm: the failure this guards against is a later change adding
    an `enabled` flag for convenience, at which point the artifact stops being
    the thing that decides and nobody notices because every other test still
    passes.
    """
    from contextplane.config import Settings

    suspicious = [
        name
        for name in Settings.model_fields
        if ("workspace" in name or "recall" in name) and ("semantic" in name or "enable" in name or "arm" in name)
    ]
    assert suspicious == []


def test_the_decision_dataclass_is_frozen() -> None:
    """A caller cannot widen the decision it was handed."""
    decision = semantic_workspace.load_decision()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.similarity_floor = 0.0  # type: ignore[misc]
