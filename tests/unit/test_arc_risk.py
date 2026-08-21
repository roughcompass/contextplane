"""Unit tests for `contextplane/arc/service/risk.py`: the ADR 041 complete-
rule-set reducer (`RiskClassificationService`) and the collaborator that
composes it with envelope validation and persistence
(`RiskEnvelopeValidator`).

No database for the reducer itself -- `classify` is pure. `RiskEnvelope
Validator.assess_and_persist` is exercised here against a `_NullSession`
double (records executed SQL, asserts nothing about real constraints) to
prove orchestration order and failure propagation; the non-vacuous proof
that this writes real rows against real `CHECK` constraints, inside the
same transaction as the draft revision and the operational-chain genesis
event, is `tests/integration/test_arc_submission.py`'s job.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.arc.schemas.authoring_profile_shapes import RISK_CLASSIFICATIONS
from contextplane.arc.service.envelope import EnvelopeInvalid
from contextplane.arc.service.risk import (
    CURRENT_RISK_ALGORITHM_VERSION,
    RiskClassificationError,
    RiskClassificationService,
    RiskEnvelopeValidator,
    UnknownRiskAlgorithmVersion,
)

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"


def _rule(scope: str, is_mandatory: bool, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"scope": scope, "is_mandatory": is_mandatory}
    base.update(overrides)
    return base


def _semantics(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"applicability": list(rules)}


# ---------------------------------------------------------------------------
# The reducer: every one of the ten closed tiers, by construction.
# ---------------------------------------------------------------------------


def test_risk_classifications_vocabulary_has_exactly_ten_members() -> None:
    """The tuple this module's docstring derives its algorithm from -- a
    sanity check that the fixture below stays aligned with it, not a
    restatement of the vocabulary-parity conformance test."""
    assert len(RISK_CLASSIFICATIONS) == 10


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param([_rule("global", True)], "global_mandatory", id="global_mandatory_alone"),
        pytest.param(
            [_rule("global", True), _rule("intent", False)],
            "global_mandatory",
            id="global_mandatory_buried_among_narrower_rules",
        ),
        pytest.param([_rule("global", False)], "global_non_mandatory", id="global_non_mandatory_alone"),
        pytest.param(
            [_rule("global", False), _rule("tenant", True)],
            "global_non_mandatory",
            id="global_non_mandatory_outranks_every_non_global_rule_even_mandatory_ones",
        ),
        pytest.param([_rule("tenant", True)], "tenant_mandatory", id="tenant_mandatory"),
        pytest.param([_rule("tenant", False)], "tenant_non_mandatory", id="tenant_non_mandatory"),
        pytest.param([_rule("domain", True)], "domain_mandatory", id="domain_mandatory"),
        pytest.param([_rule("domain", False)], "domain_non_mandatory", id="domain_non_mandatory"),
        pytest.param([_rule("entity", True)], "entity_mandatory", id="entity_mandatory"),
        pytest.param([_rule("entity", False)], "entity_non_mandatory", id="entity_non_mandatory"),
        pytest.param([_rule("intent", True)], "intent_mandatory", id="intent_mandatory"),
        pytest.param([_rule("intent", False)], "intent_non_mandatory", id="intent_non_mandatory"),
        pytest.param(
            [_rule("intent", False), _rule("entity", True)],
            "entity_mandatory",
            id="highest_scope_wins_over_lower_scope_even_when_lower_is_mandatory_and_higher_is_not",
        ),
        pytest.param(
            [_rule("tenant", False), _rule("tenant", True)],
            "tenant_mandatory",
            id="mandatory_outranks_non_mandatory_at_equal_scope",
        ),
    ],
)
def test_reducer_classifies_over_the_complete_rule_set(rules: list[dict[str, Any]], expected: str) -> None:
    service = RiskClassificationService()
    result = service.classify(_semantics(*rules))
    assert result.classification == expected
    assert result.classification in RISK_CLASSIFICATIONS
    assert result.algorithm_version == CURRENT_RISK_ALGORITHM_VERSION


def test_reducer_refuses_a_zero_rule_candidate() -> None:
    """A frozen candidate is supposed to always carry at least one
    applicability rule, rejected earlier in the authoring flow before
    classification is ever reached. This is the reducer's own defensive
    backstop for the case that upstream check does not fire -- it must
    never silently pick a default tier for an empty rule set."""
    service = RiskClassificationService()
    with pytest.raises(RiskClassificationError):
        service.classify(_semantics())


def test_unknown_reducer_version_refuses_rather_than_falling_back_to_current() -> None:
    """A stale-bound version this deployment no longer carries must never
    be silently reinterpreted under whatever is current -- that is exactly
    the reclassification ADR 041 §2 forbids."""
    service = RiskClassificationService()
    with pytest.raises(UnknownRiskAlgorithmVersion):
        service.classify(_semantics(_rule("intent", False)), reducer_version="arc_risk_reducer_v99")


# ---------------------------------------------------------------------------
# `RiskEnvelopeValidator.assess_and_persist`: orchestration and ordering,
# against a session double that only records executed statements.
# ---------------------------------------------------------------------------


class _NullSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, clause: object, params: object = None) -> None:
        self.executed.append((str(clause), dict(params) if isinstance(params, dict) else {}))


def _predicate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_observation_class_predicate_v2",
        "intent_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "entity_ids": None,
        "domain_ids": None,
    }
    base.update(overrides)
    return base


def _item(item_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": item_id,
        "delta_code": "newly_selected",
        "class_predicate": _predicate(),
        "minimum_count": 0,
        "maximum_count": None,
        "rationale_code": "expected_low_traffic",
    }
    base.update(overrides)
    return base


def _envelope(proposal_id: uuid.UUID, proposal_version: int, *items: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": "arc_expected_impact_envelope_v2",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "items": list(items) or [_item("item-1")],
        "author_issuer": _ISSUER,
        "author_subject": _OPERATOR,
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_assess_and_persist_writes_the_sticky_classification_and_the_envelope() -> None:
    session = _NullSession()
    proposal_id = uuid.uuid4()
    validator = RiskEnvelopeValidator()

    result = await validator.assess_and_persist(
        session,  # type: ignore[arg-type]
        proposal_id=proposal_id,
        proposal_version=1,
        artifact_semantics=_semantics(_rule("intent", False)),
        expected_impact_envelope=_envelope(proposal_id, 1),
        now=_NOW,
    )

    assert result.classification == "intent_non_mandatory"
    assert result.algorithm_version == CURRENT_RISK_ALGORITHM_VERSION
    statements = "\n".join(text for text, _params in session.executed)
    assert "arc_risk_classifications" in statements
    assert "arc_authoring_proposal_versions" in statements
    assert "arc_expected_impact_envelopes" in statements
    assert "arc_expected_impact_envelope_items" in statements


@pytest.mark.asyncio
async def test_an_invalid_envelope_writes_nothing_at_all() -> None:
    """Validation happens before any write: a forbidden predicate key must
    never leave a partial trail of risk-classification or envelope rows for
    the transaction's rollback to have to undo."""
    session = _NullSession()
    proposal_id = uuid.uuid4()
    validator = RiskEnvelopeValidator()
    bad_envelope = _envelope(proposal_id, 1, _item("item-1", class_predicate=_predicate(tenant_id="nope")))

    with pytest.raises(EnvelopeInvalid):
        await validator.assess_and_persist(
            session,  # type: ignore[arg-type]
            proposal_id=proposal_id,
            proposal_version=1,
            artifact_semantics=_semantics(_rule("intent", False)),
            expected_impact_envelope=bad_envelope,
            now=_NOW,
        )

    assert session.executed == []


@pytest.mark.asyncio
async def test_an_unclassifiable_candidate_writes_nothing_even_with_a_valid_envelope() -> None:
    session = _NullSession()
    proposal_id = uuid.uuid4()
    validator = RiskEnvelopeValidator()

    with pytest.raises(RiskClassificationError):
        await validator.assess_and_persist(
            session,  # type: ignore[arg-type]
            proposal_id=proposal_id,
            proposal_version=1,
            artifact_semantics=_semantics(),
            expected_impact_envelope=_envelope(proposal_id, 1),
            now=_NOW,
        )

    assert session.executed == []
