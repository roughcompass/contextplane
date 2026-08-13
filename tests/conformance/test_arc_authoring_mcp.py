"""Conformance gate for the five read-only authoring-surface MCP tools
(Appendix A.2): `arc_list_proposals`, `arc_get_proposal_version`,
`arc_get_review_package`, `arc_get_observation_status`,
`arc_get_activation_eligibility`.

Three separate claims, each checked independently rather than inferred
from the others:

- **name/argument parity** -- the registered tool set, and each new tool's
  argument schema, match Appendix A.2 exactly. Pinned to a checked-in
  snapshot (`snapshots/mcp_tools.json`) so a later change to any ARC tool's
  argument shape or docstring is a reviewed diff, not silent drift -- the
  same role `arc_authoring_schemas.json` plays for REST components.
- **no mutation tool was added** -- the registered ARC tool set is exactly
  the nine names below, not a superset. `test_arc_rest_mcp_parity.py`
  already guards the four pre-existing tools against admin/mutation
  leakage; this file is the authoring-surface-specific half of that same
  promise.
- **result parity** -- each tool, given the same domain object its REST
  counterpart route would render, returns the exact same JSON a fresh
  `.model_dump(mode="json")` of the matching frozen REST response
  component produces. Not merely "the same shape" -- the same bytes.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.api.mcp import context
from contextplane.api.mcp.tools import arc as arc_tools
from contextplane.api.schemas.arc_authoring import (
    ActivationEligibilityResponse,
    ActivationPredicateStatus,
    ObservationStatusResponse,
    PagedProposalSummaries,
    ProposalSummary,
    ProposalVersionResponse,
    ReviewPackageResponse,
)
from contextplane.arc.service.activation import ActivationEligibility
from contextplane.arc.service.activation_predicates import PREDICATE_ORDER, PredicateResult
from contextplane.arc.service.proposal import ProposalPage, ProposalVersion
from contextplane.arc.service.qualification import ObservationStatus
from contextplane.arc.service.review_package import (
    BaselineDiff,
    FieldProvenanceSummary,
    ReachConfirmation,
    ReviewPackage,
    SemanticTestSummary,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext

_SNAPSHOT_PATH = Path(__file__).resolve().parent / "snapshots" / "mcp_tools.json"

_EXPECTED_ARC_TOOL_NAMES = frozenset(
    {
        # Pre-existing (preflight + receipt reads); unchanged by this task.
        "arc_complete_preflight",
        "arc_issue_context_challenge",
        "arc_get_context_resolution_receipt",
        "arc_explain_context_resolution",
        # Appendix A.2's five, added by this task. All read-only.
        "arc_list_proposals",
        "arc_get_proposal_version",
        "arc_get_review_package",
        "arc_get_observation_status",
        "arc_get_activation_eligibility",
    }
)

# Appendix A.2's argument tables, transcribed directly rather than derived
# from the tool signatures under test -- a derived check would pass no
# matter what the signatures said.
_EXPECTED_ARGUMENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # tool name -> (all properties, required properties)
    "arc_list_proposals": (
        frozenset({"artifact_id", "state", "cursor", "limit"}),
        frozenset({"artifact_id", "state", "cursor"}),  # limit defaults to 50
    ),
    "arc_get_proposal_version": (
        frozenset({"proposal_id", "proposal_version"}),
        frozenset({"proposal_id", "proposal_version"}),
    ),
    "arc_get_review_package": (
        frozenset({"proposal_id", "proposal_version"}),
        frozenset({"proposal_id", "proposal_version"}),
    ),
    "arc_get_observation_status": (
        frozenset({"proposal_id", "proposal_version"}),
        frozenset({"proposal_id", "proposal_version"}),
    ),
    "arc_get_activation_eligibility": (
        frozenset({"revision_id"}),
        frozenset({"revision_id"}),
    ),
}


@pytest.fixture(scope="module")
def mcp_server() -> Any:
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    return create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )


@pytest.fixture(scope="module")
def arc_tool_catalog(mcp_server: Any) -> dict[str, dict[str, Any]]:
    tools = asyncio.run(mcp_server.list_tools())
    return {
        t.name: {"description": t.description, "input_schema": t.inputSchema}
        for t in tools
        if t.name.startswith("arc_")
    }


# ---------------------------------------------------------------------------
# Name / argument parity.
# ---------------------------------------------------------------------------


def test_no_mutation_tool_was_added(arc_tool_catalog: dict[str, dict[str, Any]]) -> None:
    """The registered ARC tool set is exactly nine read-only tools -- not a
    superset. A future change that registers a sixth ARC tool here without
    updating this set fails loudly rather than silently shipping an
    authoring, approval, or activation mutation over MCP."""
    assert set(arc_tool_catalog.keys()) == _EXPECTED_ARC_TOOL_NAMES


def test_arc_tool_catalog_matches_the_snapshot() -> None:
    """Pins every ARC tool's description and argument schema so a change
    to either is a reviewed diff. Reuses the exact `create_registry_mcp_
    server` construction `test_arc_rest_mcp_parity.py` already uses."""
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    tools = asyncio.run(server.list_tools())
    live = {
        t.name: {"description": t.description, "input_schema": t.inputSchema}
        for t in tools
        if t.name.startswith("arc_")
    }
    snapshot = json.loads(_SNAPSHOT_PATH.read_text())
    assert live == snapshot


@pytest.mark.parametrize("tool_name", sorted(_EXPECTED_ARGUMENTS.keys()))
def test_new_tool_arguments_match_appendix_a2(tool_name: str, arc_tool_catalog: dict[str, dict[str, Any]]) -> None:
    expected_properties, expected_required = _EXPECTED_ARGUMENTS[tool_name]
    schema = arc_tool_catalog[tool_name]["input_schema"]
    assert set(schema["properties"].keys()) == expected_properties
    assert set(schema.get("required", [])) == expected_required


def test_arc_list_proposals_limit_defaults_to_fifty(arc_tool_catalog: dict[str, dict[str, Any]]) -> None:
    assert arc_tool_catalog["arc_list_proposals"]["input_schema"]["properties"]["limit"]["default"] == 50


# ---------------------------------------------------------------------------
# Result parity: same domain object in, byte-identical wire shape out.
# ---------------------------------------------------------------------------

_ISSUER = "https://issuer.example"


def _ctx() -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
        oidc_subject="test-subject",
        tenant_memberships=[],
    )
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


@pytest.fixture(autouse=True)
def _patch_preflight(monkeypatch: pytest.MonkeyPatch) -> ArcRequestContext:
    ctx = _ctx()

    async def _fake_preflight(session_factory: object, clock: object) -> ArcRequestContext:
        return ctx

    monkeypatch.setattr(arc_tools, "_arc_preflight", _fake_preflight)
    return ctx


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def test_arc_get_proposal_version_result_matches_the_rest_component(monkeypatch: pytest.MonkeyPatch) -> None:
    version = ProposalVersion(
        proposal_id=uuid.uuid4(),
        proposal_version=1,
        artifact_id=uuid.uuid4(),
        state="open",
        revision_id=None,
        source_evidence_id=uuid.uuid4(),
        reviewed_baseline_revision_id=None,
        risk_classification=None,
        risk_algorithm_version=None,
        allowed_transitions=("withdrawn",),
        available_actions=("edit", "validate", "run_semantic_tests", "withdraw"),
        reason_codes=(),
        operational_integrity_state="unavailable",
        created_at=_now(),
        frozen_at=None,
    )
    service = MagicMock()
    service.get_version = AsyncMock(return_value=version)
    monkeypatch.setattr(context, "_arc_state", lambda name: service)

    result = asyncio.run(
        arc_tools.arc_get_proposal_version(str(version.proposal_id), 1, session_factory=None, clock=None)
    )
    expected = ProposalVersionResponse(
        proposal_id=version.proposal_id,
        proposal_version=version.proposal_version,
        artifact_id=version.artifact_id,
        state=version.state,  # type: ignore[arg-type]
        revision_id=version.revision_id,
        source_evidence_id=version.source_evidence_id,
        reviewed_baseline_revision_id=version.reviewed_baseline_revision_id,
        risk_classification=version.risk_classification,  # type: ignore[arg-type]
        risk_algorithm_version=version.risk_algorithm_version,
        allowed_transitions=list(version.allowed_transitions),  # type: ignore[arg-type]
        available_actions=list(version.available_actions),  # type: ignore[arg-type]
        reason_codes=list(version.reason_codes),
        operational_integrity_state=version.operational_integrity_state,  # type: ignore[arg-type]
        created_at=version.created_at,
        frozen_at=version.frozen_at,
    )
    assert json.loads(result) == expected.model_dump(mode="json")


def test_arc_list_proposals_result_matches_the_paged_summaries_component(monkeypatch: pytest.MonkeyPatch) -> None:
    version = ProposalVersion(
        proposal_id=uuid.uuid4(),
        proposal_version=2,
        artifact_id=uuid.uuid4(),
        state="submitted",
        revision_id=uuid.uuid4(),
        source_evidence_id=uuid.uuid4(),
        reviewed_baseline_revision_id=None,
        risk_classification="tenant_mandatory",
        risk_algorithm_version="v1",
        allowed_transitions=(),
        available_actions=(),
        reason_codes=(),
        operational_integrity_state="pending",
        created_at=_now(),
        frozen_at=_now(),
    )
    page = ProposalPage(items=(version,), next_cursor="opaque-cursor")
    service = MagicMock()
    service.list_proposals = AsyncMock(return_value=page)
    monkeypatch.setattr(context, "_arc_state", lambda name: service)

    result = asyncio.run(arc_tools.arc_list_proposals(None, None, None, 50, session_factory=None, clock=None))
    expected = PagedProposalSummaries(
        items=[
            ProposalSummary(
                proposal_id=version.proposal_id,
                proposal_version=version.proposal_version,
                artifact_id=version.artifact_id,
                state=version.state,  # type: ignore[arg-type]
                risk_classification=version.risk_classification,  # type: ignore[arg-type]
                created_at=version.created_at,
            )
        ],
        next_cursor=page.next_cursor,
    )
    assert json.loads(result) == expected.model_dump(mode="json")


def test_arc_get_observation_status_result_matches_the_rest_component(monkeypatch: pytest.MonkeyPatch) -> None:
    status_obj = ObservationStatus(
        cohort_id=uuid.uuid4(),
        cohort_digest="e" * 64,
        window_started_at=_now(),
        window_deadline=_now() + datetime.timedelta(days=7),
        eligible_count=10,
        observed_count=5,
        counters_by_delta_code={"newly_selected": {"explained": 1, "unexplained": 0}},
        unexplained_count=0,
        out_of_envelope_count=0,
        computed_decision="qualified",
        reason_codes=(),
    )
    service = MagicMock()
    service.get_status = AsyncMock(return_value=status_obj)
    monkeypatch.setattr(context, "_arc_state", lambda name: service)

    result = asyncio.run(arc_tools.arc_get_observation_status(str(uuid.uuid4()), 1, session_factory=None, clock=None))
    from contextplane.api.schemas.arc_authoring import DeltaCodeCounter

    expected = ObservationStatusResponse(
        cohort_id=status_obj.cohort_id,
        cohort_digest=status_obj.cohort_digest,
        window_started_at=status_obj.window_started_at,
        window_deadline=status_obj.window_deadline,
        eligible_count=status_obj.eligible_count,
        observed_count=status_obj.observed_count,
        counters_by_delta_code=[DeltaCodeCounter(delta_code="newly_selected", count=1)],  # type: ignore[arg-type]
        unexplained_count=status_obj.unexplained_count,
        out_of_envelope_count=status_obj.out_of_envelope_count,
        computed_decision=status_obj.computed_decision,  # type: ignore[arg-type]
        reason_codes=list(status_obj.reason_codes),
    )
    assert json.loads(result) == expected.model_dump(mode="json")


def test_arc_get_activation_eligibility_result_matches_the_rest_component(monkeypatch: pytest.MonkeyPatch) -> None:
    eligibility = ActivationEligibility(
        eligible=False,
        predicates=tuple(
            PredicateResult(name=name, satisfied=False, reason_code="arc_source_status_unavailable")
            for name in PREDICATE_ORDER
        ),
    )
    service = MagicMock()
    service.get_eligibility = AsyncMock(return_value=eligibility)
    monkeypatch.setattr(context, "_arc_state", lambda name: service)

    result = asyncio.run(arc_tools.arc_get_activation_eligibility(str(uuid.uuid4()), session_factory=None, clock=None))
    expected = ActivationEligibilityResponse(
        eligible=eligibility.eligible,
        predicates=[
            ActivationPredicateStatus(name=p.name, satisfied=p.satisfied, reason_code=p.reason_code)  # type: ignore[arg-type]
            for p in eligibility.predicates
        ],
    )
    assert json.loads(result) == expected.model_dump(mode="json")


def _expected_impact_envelope(*, proposal_id: uuid.UUID, proposal_version: int) -> dict[str, object]:
    """Same minimal valid shape `test_arc_activation_predicates.py` uses to
    build a real `arc_expected_impact_envelope_v1` fixture."""
    return {
        "profile": "arc_expected_impact_envelope_v2",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "items": [
            {
                "item_id": "item-1",
                "delta_code": "newly_selected",
                "class_predicate": {
                    "profile": "arc_observation_class_predicate_v2",
                    "intent_kind": None,
                    "requested_action_classes": None,
                    "environment": None,
                    "data_sensitivity_tier": None,
                    "capability_ids": None,
                    "domain_ids": None,
                },
                "minimum_count": 0,
                "maximum_count": None,
                "rationale_code": "expected_low_traffic",
            }
        ],
        "author_issuer": _ISSUER,
        "author_subject": "test-subject",
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_arc_get_review_package_result_matches_the_rest_component(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_id = uuid.uuid4()
    envelope = _expected_impact_envelope(proposal_id=proposal_id, proposal_version=1)
    pkg = ReviewPackage(
        review_package_digest="a" * 64,
        artifact_semantics_digest="b" * 64,
        artifact_revision_digest="c" * 64,
        baseline_diff=BaselineDiff(baseline_revision_id=None, changes=()),
        field_provenance=(
            FieldProvenanceSummary(
                field_path="x",
                provenance_class="source_backed",
                source_evidence_id=uuid.uuid4(),
                source_anchor="anchor",
                excerpt_digest="d" * 64,
                author_role=None,
                derivation_profile=None,
                author_issuer=None,
                author_subject=None,
            ),
        ),
        prose_readback="readback",
        semantic_tests=(SemanticTestSummary(test_id="t1", passed=True, expected={}, actual={}),),
        expected_impact_envelope=envelope,
        risk_classification="global_mandatory",
        risk_algorithm_version="v1",
        reach_confirmations=(
            ReachConfirmation(
                field_path="x",
                confirmed=True,
                confirmed_at=_now(),
                confirmed_by_issuer=_ISSUER,
                confirmed_by_subject="test-subject",
            ),
        ),
        submitted_by_issuer=_ISSUER,
        submitted_by_subject="test-subject",
    )
    service = MagicMock()
    service.get_review_package = AsyncMock(return_value=pkg)
    monkeypatch.setattr(context, "_arc_state", lambda name: service)

    result = asyncio.run(arc_tools.arc_get_review_package(str(proposal_id), 1, session_factory=None, clock=None))
    # `arc.py`'s own `_review_package_response` is the mapping under test;
    # comparing against `ReviewPackageResponse.model_validate` of the same
    # underlying data, built independently here field-by-field, is what
    # keeps this a real parity check rather than the module checking
    # itself.
    from contextplane.api.schemas.arc_authoring import (
        ActorRef,
        BaselineDiffResponse,
        Citation,
        ExpectedImpactEnvelope,
        FieldProvenance,
        ReachConfirmationItem,
        ReachConfirmationResponse,
        SemanticTestResultItem,
        SemanticTestResultResponse,
    )

    expected = ReviewPackageResponse(
        review_package_digest=pkg.review_package_digest,
        artifact_semantics_digest=pkg.artifact_semantics_digest,
        artifact_revision_digest=pkg.artifact_revision_digest,
        baseline_diff=BaselineDiffResponse(baseline_revision_id=None, changes=[]),
        field_provenance=[
            FieldProvenance(
                field_path=f.field_path,
                provenance_class=f.provenance_class,  # type: ignore[arg-type]
                source_evidence_id=f.source_evidence_id,
                source_anchor=f.source_anchor,
                excerpt_digest=f.excerpt_digest,
                author_role=f.author_role,
                derivation_profile=f.derivation_profile,
                author=None,
            )
            for f in pkg.field_provenance
        ],
        citations=[
            Citation(
                field_path=f.field_path,
                source_evidence_id=f.source_evidence_id,  # type: ignore[arg-type]
                source_anchor=f.source_anchor,  # type: ignore[arg-type]
                excerpt_digest=f.excerpt_digest,  # type: ignore[arg-type]
            )
            for f in pkg.field_provenance
            if f.provenance_class == "source_backed"
        ],
        judgment_authors=[],
        prose_readback=pkg.prose_readback,
        semantic_tests=SemanticTestResultResponse(
            results=[
                SemanticTestResultItem(test_id=t.test_id, passed=t.passed, expected=t.expected, actual=t.actual)
                for t in pkg.semantic_tests
            ]
        ),
        expected_impact_envelope=ExpectedImpactEnvelope.model_validate(pkg.expected_impact_envelope),
        risk_classification=pkg.risk_classification,  # type: ignore[arg-type]
        risk_algorithm_version=pkg.risk_algorithm_version,
        reach_confirmations=ReachConfirmationResponse(
            confirmations=[
                ReachConfirmationItem(
                    field_path=r.field_path,
                    confirmed=r.confirmed,
                    confirmed_at=r.confirmed_at,
                    confirmed_by=ActorRef(issuer=r.confirmed_by_issuer, subject=r.confirmed_by_subject),  # type: ignore[arg-type]
                )
                for r in pkg.reach_confirmations
            ]
        ),
        submission_identity=ActorRef(issuer=pkg.submitted_by_issuer, subject=pkg.submitted_by_subject),
    )
    assert json.loads(result) == expected.model_dump(mode="json")
