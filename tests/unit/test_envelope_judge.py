"""Unit coverage for the five-block deterministic scorer.

E24-T4. The question this file asks is whether the arithmetic is right against
small hand-built envelopes, so every branch is reachable without a database and
without the frozen 40-scenario corpus.

Two of its cases are regression tests rather than coverage. `judge.py` reads a
tenant off the item payload, and no arm puts one there, so the shipped scorer
records a tenant violation on every real item -- a check that fires on everything
distinguishes nothing. And it ranks an unreadable classification as the most
restrictive thing, which is right for an unreadable label and wrong for a
canonical item that structurally carries none. Both are asserted here in the
direction the new scorer fixes, and the old behaviour is asserted too, because a
version that quietly agreed with its predecessor would not be a new version.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

import pytest

from contextplane.context.assembler import canonical_item, contextual_item
from contextplane.context.evaluation import envelope_judge, judge, protocol
from contextplane.context.quality import derive_quality
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_INSTRUCTIONS,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
    ContextBlockV1,
    ContextEnvelopeV1,
    ContextItemV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import TrustMetadataV1

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)
_TENANT = "11111111-1111-5111-8111-111111111111"
_TASK = "22222222-2222-5222-8222-222222222222"
_ELSEWHERE = "33333333-3333-5333-8333-333333333333"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _trust(*, classification: str = "internal", kind: str = "annotation") -> TrustMetadataV1:
    return TrustMetadataV1(
        trust="asserted",
        source="unit",
        assertion_kind=kind,  # type: ignore[arg-type]
        authority="unit",
        freshness=_NOW,
        mutability="immutable",
        attribution="agent-alpha",
        classification=classification,  # type: ignore[arg-type]
    )


def _workspace_item(
    key: str, *, intent_id: str = _TASK, classification: str = "internal", **extra: Any
) -> ContextItemV1:
    """A workspace item shaped the way the shipped arms actually shape one.

    Deliberately carries no `tenant_id`, because `queries.py` and
    `workspaces/recall.py` do not. A fixture that added one would be testing a
    payload the product never serves.
    """
    payload: dict[str, object] = {"intent_id": intent_id, "goal": key}
    payload.update(extra)
    return contextual_item(
        block=BLOCK_WORKSPACE,
        source="intent_checkpoint",
        item_key=key,
        payload=payload,
        trust=_trust(classification=classification),
    )


def _canonical_item(key: str) -> ContextItemV1:
    """A canonical item, which carries no trust metadata by construction."""
    return canonical_item(
        source="registry",
        item_key=key,
        payload={"entity_id": key, "name": key, "entity_type": "service"},
    )


def _instruction_item(key: str, *, scope: str = "digest", classification: str = "internal") -> ContextItemV1:
    return contextual_item(
        block=BLOCK_INSTRUCTIONS,
        source="instruction_delta",
        item_key=key,
        payload={"body": "prefer the newer runbook", "contradicts": False, "delta_id": key, "scope": scope},
        trust=_trust(classification=classification, kind="policy"),
    )


def _envelope(**by_block: tuple[ContextItemV1, ...]) -> ContextEnvelopeV1:
    blocks = tuple(
        ContextBlockV1(
            name=name,
            state="success" if by_block.get(name) else "empty",
            items=by_block.get(name, ()),
        )
        for name in BLOCK_NAMES
    )
    return ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))


@dataclasses.dataclass(frozen=True)
class _Unvalidated:
    """An envelope-shaped input that never passed the block contract.

    The scorer reads `.blocks` and nothing else, so this is enough to hand it a
    shape the real assembler would refuse -- which is the only way to reach the
    defensive branches that exist for exactly that case.
    """

    blocks: tuple[ContextBlockV1, ...]


def _facts(**overrides: Any) -> envelope_judge.AuthorizationFacts:
    base: dict[str, Any] = {
        "permitted_tenant_ids": frozenset({_TENANT}),
        "permitted_task_ids": frozenset({_TASK}),
    }
    base.update(overrides)
    return envelope_judge.AuthorizationFacts(**base)


# ---------------------------------------------------------------------------
# The regression this version exists to fix
# ---------------------------------------------------------------------------


def test_the_shipped_scorer_flags_every_real_item_as_a_tenant_violation() -> None:
    """The defect, asserted so the fix has something to be a fix of.

    `judge.py` reads `payload["tenant_id"]`; no arm writes one. Against the
    frozen corpus -- every scenario of which declares `permitted_tenant_ids` --
    that makes every served item a violation and `SAFETY_TOLERANCE = 0` then
    disqualifies every configuration.
    """
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    old = judge.safety_violations(envelope, judge.AuthorizationFacts(permitted_tenant_ids=frozenset({_TENANT})))
    assert [v.kind for v in old] == [judge.VIOLATION_TENANT]
    assert "None" in old[0].detail


def test_the_tenant_comes_from_the_resolution_not_the_payload() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    violations, _ = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=_TENANT)
    assert violations == ()


def test_a_resolution_in_a_tenant_the_scenario_forbids_is_a_violation() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    violations, _ = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=_ELSEWHERE)
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_TENANT]
    assert _ELSEWHERE in violations[0].detail


def test_an_item_that_states_its_own_tenant_wins_over_the_resolution() -> None:
    """A fixture describing a leak gets its leak caught."""
    envelope = _envelope(workspace=(_workspace_item("w1", tenant_id=_ELSEWHERE),))
    violations, _ = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=_TENANT)
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_TENANT]


def test_a_tenant_neither_side_named_is_a_violation_rather_than_a_pass() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    violations, _ = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=None)
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_TENANT]
    assert "neither the item nor the resolution named" in violations[0].detail


def test_no_tenant_claim_means_no_tenant_check() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    facts = _facts(permitted_tenant_ids=None)
    violations, _ = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_ELSEWHERE)
    assert violations == ()


# ---------------------------------------------------------------------------
# Unchecked dimensions are recorded, never passed
# ---------------------------------------------------------------------------


def test_canonical_classification_is_recorded_as_unchecked_not_as_a_violation() -> None:
    """The structural absence, distinguished from the unreadable label."""
    envelope = _envelope(canonical=(_canonical_item("e1"),))
    violations, unchecked = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=_TENANT)
    assert violations == ()
    kinds = {(u.block, u.dimension) for u in unchecked}
    assert (BLOCK_CANONICAL, envelope_judge.VIOLATION_CLASSIFICATION) in kinds
    assert (BLOCK_CANONICAL, envelope_judge.VIOLATION_AUDIENCE) in kinds


def test_an_unreadable_label_on_a_non_canonical_item_still_ranks_most_restrictive() -> None:
    """The rule that survives: guessing downward is what publishes it."""
    item = contextual_item(
        block=BLOCK_OBSERVED_CLAIMS,
        source="claims",
        item_key="c1",
        payload={"claim_id": "c1", "predicate": "p", "value": 1, "evidence_event_ids": []},
        trust=_trust(classification="internal"),
    )
    broken = ContextItemV1(receipt_item_id=item.receipt_item_id, payload=item.payload, trust=None)
    # Assembled past the block contract deliberately. `ContextBlockV1` refuses a
    # non-canonical item with no trust, and `TrustMetadataV1` refuses a
    # classification outside the vocabulary, so an unreadable label cannot exist
    # in a validated envelope at all. The scorer's own defence still has to hold
    # for the input it is handed, which is what this asserts.
    envelope = _Unvalidated(blocks=(ContextBlockV1.__new__(ContextBlockV1),))
    object.__setattr__(envelope.blocks[0], "name", BLOCK_OBSERVED_CLAIMS)
    object.__setattr__(envelope.blocks[0], "state", "success")
    object.__setattr__(envelope.blocks[0], "items", (broken,))
    object.__setattr__(envelope.blocks[0], "reason", None)
    facts = _facts(max_classification="internal")
    violations, _ = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)  # type: ignore[arg-type]
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_CLASSIFICATION]


def test_blocks_that_state_no_audience_are_recorded_as_unchecked() -> None:
    envelope = _envelope(
        arc=(
            contextual_item(
                block=BLOCK_ARC,
                source="arc",
                item_key="a1",
                payload={"artifact_id": "a1", "artifact_kind": "policy", "version": 1, "summary": "s"},
                trust=_trust(kind="policy"),
            ),
        )
    )
    _, unchecked = envelope_judge.boundary_violations(envelope, _facts(), served_tenant_id=_TENANT)
    assert [(u.block, u.dimension) for u in unchecked] == [(BLOCK_ARC, envelope_judge.VIOLATION_AUDIENCE)]
    assert unchecked[0].reason == envelope_judge.UNCHECKED_NO_AUDIENCE_FIELD


def test_a_scenario_claiming_no_audience_records_nothing_unchecked() -> None:
    """Nothing was asked, so nothing went unanswered."""
    envelope = _envelope(canonical=(_canonical_item("e1"),))
    facts = _facts(permitted_task_ids=None, permitted_instruction_scopes=None)
    _, unchecked = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)
    assert [u.dimension for u in unchecked] == [envelope_judge.VIOLATION_CLASSIFICATION]


# ---------------------------------------------------------------------------
# The fifth block
# ---------------------------------------------------------------------------


def test_an_instruction_delta_outside_the_permitted_scopes_is_an_audience_violation() -> None:
    envelope = _envelope(instructions=(_instruction_item("d1", scope="tenant"),))
    facts = _facts(permitted_instruction_scopes=frozenset({"digest", "principal"}))
    violations, _ = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_AUDIENCE]
    assert "instruction scope 'tenant'" in violations[0].detail


def test_a_permitted_instruction_scope_passes() -> None:
    envelope = _envelope(instructions=(_instruction_item("d1", scope="principal"),))
    facts = _facts(permitted_instruction_scopes=frozenset({"principal"}))
    violations, unchecked = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)
    assert violations == ()
    assert unchecked == ()


def test_a_scenario_cannot_permit_a_scope_nothing_can_serve() -> None:
    with pytest.raises(ValueError, match="instruction scopes"):
        envelope_judge.AuthorizationFacts(permitted_instruction_scopes=frozenset({"everyone"}))


def test_the_fifth_block_adds_items_to_judge_not_a_fifth_violation_kind() -> None:
    assert envelope_judge.VIOLATION_KINDS == judge.VIOLATION_KINDS
    assert len(envelope_judge.VIOLATION_KINDS) == 4


def test_every_envelope_block_is_scored() -> None:
    assert envelope_judge.SCORED_BLOCKS == BLOCK_NAMES
    assert len(envelope_judge.SCORED_BLOCKS) == 5


# ---------------------------------------------------------------------------
# Recall and precision across five blocks
# ---------------------------------------------------------------------------


def test_recall_counts_a_required_fact_in_any_block() -> None:
    envelope = _envelope(canonical=(_canonical_item("e1"),), instructions=(_instruction_item("d1"),))
    found, total = envelope_judge.required_fact_recall(envelope, ["e1", "d1", "missing"])
    assert (found, total) == (2, 3)


def test_recall_over_the_workspace_arm_alone_is_the_old_measurement() -> None:
    """The generalization is a different number, and the old one is still right about its own question."""
    envelope = _envelope(canonical=(_canonical_item("e1"),), workspace=(_workspace_item("w1"),))
    assert envelope_judge.required_fact_recall(envelope, ["e1", "w1"]) == (2, 2)
    assert judge.required_fact_recall(envelope, ["e1", "w1"]) == (1, 2)


def test_a_required_fact_matches_by_content_digest_too() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1", digest="sha-abc"),))
    assert envelope_judge.required_fact_recall(envelope, ["sha-abc"]) == (1, 1)


def test_requiring_nothing_scores_nothing_rather_than_everything() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    assert envelope_judge.required_fact_recall(envelope, []) == (0, 0)


def test_precision_denominates_over_every_served_item() -> None:
    envelope = _envelope(
        canonical=(_canonical_item("e1"),),
        workspace=(_workspace_item("w1"), _workspace_item("w2")),
    )
    assert envelope_judge.precision(envelope, ["e1", "w1"]) == (2, 3)


def test_precision_of_an_empty_envelope_is_zero_over_zero() -> None:
    assert envelope_judge.precision(_envelope(), ["anything"]) == (0, 0)


def test_a_failed_block_contributes_no_items() -> None:
    blocks = tuple(
        ContextBlockV1(name=name, state="failed", reason="the arm broke")
        if name == BLOCK_WORKSPACE
        else ContextBlockV1(name=name, state="empty")
        for name in BLOCK_NAMES
    )
    envelope = ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))
    assert envelope_judge.served_items(envelope) == ()


# ---------------------------------------------------------------------------
# score(), and the per-block tally that keeps a total attributable
# ---------------------------------------------------------------------------


def test_score_reports_a_tally_for_every_block_including_the_empty_ones() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    result = envelope_judge.score(
        envelope=envelope,
        required_item_keys=["w1"],
        relevant_item_keys=["w1"],
        facts=_facts(),
        served_tenant_id=_TENANT,
    )
    assert [tally.block for tally in result.blocks] == list(BLOCK_NAMES)
    workspace = next(tally for tally in result.blocks if tally.block == BLOCK_WORKSPACE)
    assert (workspace.served, workspace.relevant, workspace.required_found) == (1, 1, 1)
    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.is_safe
    assert result.missing_required == 0
    assert result.rubric_version == envelope_judge.ENVELOPE_JUDGE_VERSION


def test_a_boundary_violation_is_carried_whole_rather_than_counted() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1", intent_id=_ELSEWHERE),))
    result = envelope_judge.score(
        envelope=envelope,
        required_item_keys=["w1"],
        relevant_item_keys=["w1"],
        facts=_facts(),
        served_tenant_id=_TENANT,
    )
    assert result.recall == 1.0
    assert not result.is_safe
    assert [v.block for v in result.violations] == [BLOCK_WORKSPACE]


def test_an_errored_run_scores_zero_and_says_so_rather_than_being_dropped() -> None:
    result = envelope_judge.score(
        envelope=None,
        required_item_keys=["w1", "w2"],
        relevant_item_keys=[],
        facts=_facts(),
        served_tenant_id=_TENANT,
    )
    assert result.errored
    assert (result.recall, result.precision) == (0.0, 0.0)
    assert result.required_total == 2
    assert result.missing_required == 2
    assert result.blocks == ()


def test_an_envelope_present_but_flagged_errored_is_still_a_failure() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    result = envelope_judge.score(
        envelope=envelope,
        required_item_keys=["w1"],
        relevant_item_keys=["w1"],
        facts=_facts(),
        served_tenant_id=_TENANT,
        errored=True,
    )
    assert result.errored
    assert result.recall == 0.0


def test_an_item_can_fail_two_dimensions_and_is_reported_once_per_dimension() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1", intent_id=_ELSEWHERE, classification="restricted"),))
    facts = _facts(max_classification="internal")
    violations, _ = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)
    assert {v.kind for v in violations} == {envelope_judge.VIOLATION_AUDIENCE, envelope_judge.VIOLATION_CLASSIFICATION}


def test_a_withdrawn_item_is_a_lifecycle_violation_whatever_else_is_true() -> None:
    envelope = _envelope(workspace=(_workspace_item("w1"),))
    facts = _facts(withdrawn_item_keys=frozenset({"w1"}))
    violations, _ = envelope_judge.boundary_violations(envelope, facts, served_tenant_id=_TENANT)
    assert [v.kind for v in violations] == [envelope_judge.VIOLATION_LIFECYCLE]


# ---------------------------------------------------------------------------
# The freeze
# ---------------------------------------------------------------------------


def test_the_default_freeze_still_reproduces_the_closed_decisions_protocol_digest() -> None:
    assert protocol.freeze().protocol_digest == protocol.V1_ERA_IDENTITY["protocol_digest"]
    assert protocol.freeze().judge_version == protocol.JUDGE_VERSION


def test_the_five_block_scorer_freezes_to_a_different_digest() -> None:
    v1 = protocol.freeze()
    v2 = protocol.freeze(judge_version=protocol.ENVELOPE_JUDGE_VERSION)
    assert v2.judge_version == envelope_judge.ENVELOPE_JUDGE_VERSION
    assert v2.judge_digest != v1.judge_digest
    assert v2.protocol_digest != v1.protocol_digest
    assert v2.freeze_digest() != v1.freeze_digest()


def test_an_unregistered_scorer_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(protocol.ProtocolInvalidated, match="no scorer is registered"):
        protocol.freeze(judge_version="workspace-eval-judge v9.9.9")


def test_assert_unchanged_re_digests_the_scorer_the_run_actually_used() -> None:
    """Defaulting to the v1 scorer here would report drift on every v2 run."""
    collected = protocol.freeze(judge_version=protocol.ENVELOPE_JUDGE_VERSION)
    protocol.assert_unchanged(collected)


def test_a_moved_scorer_still_invalidates_its_own_freeze(tmp_path: Any) -> None:
    collected = protocol.freeze(judge_version=protocol.ENVELOPE_JUDGE_VERSION)
    moved = tmp_path / "envelope_judge.py"
    moved.write_text("# not the scorer\n", encoding="utf-8")
    with pytest.raises(protocol.ProtocolInvalidated, match="the scorer"):
        protocol.assert_unchanged(collected, judge_source=moved)


def test_a_scorer_that_is_not_committed_cannot_be_frozen(tmp_path: Any) -> None:
    with pytest.raises(protocol.ProtocolInvalidated, match="is not committed"):
        protocol.judge_source_digest(tmp_path / "absent.py", judge_version=protocol.ENVELOPE_JUDGE_VERSION)


def test_every_registered_scorer_resolves_to_a_committed_file() -> None:
    for version in protocol.JUDGE_SOURCES:
        assert protocol.judge_source_digest(judge_version=version)
