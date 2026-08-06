"""Unit tests for `registry/arc/service/semantic_tests.py`.

No database, matching `test_arc_proposal.py`'s and `test_arc_provenance.py`'s
own convention. Four things this file exists to prove, per the task's own
contract:

1. The matching engine discriminates: a predicate is reported matched when
   its dimensions overlap a candidate rule's, and unmatched when they do
   not -- both directions, so a matcher that always answered one way could
   not pass.
2. `run()` evaluates the *candidate*'s own persisted `semantics.applicability`
   array, never the reviewed baseline revision's live rules -- proven by a
   scenario where the two disagree, in both directions, so a matcher that
   (wrongly) fell back to the baseline in either direction would fail one
   assertion or the other.
3. A frozen semantic-test result stays bound to the input that produced
   it: re-running the same `test_id` against a *changed* manifest updates
   the stored `actual`/`manifest` rather than silently leaving the old,
   now-mismatched pair in place.
4. A version with no candidate yet (`semantics IS NULL`, no `PATCH` has
   landed) evaluates against an empty rule set rather than erroring or
   borrowing coverage from anywhere else.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from registry.arc.service import semantic_tests as st
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries.proposal import FamilyRow, VersionRow
from registry.arc.service.queries.provenance import ApplicabilityRuleRow, SemanticTestRow
from registry.arc.types import ArcRequestContext
from registry.exceptions import NotFoundError
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"


class _FakeClock:
    def now(self) -> datetime.datetime:
        return _NOW


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, tenant_id: uuid.UUID | None = None, roles: list[str] | None = None) -> ArcRequestContext:
    return ArcRequestContext(
        tenant=TenantContext(
            tenant_id=tenant_id or uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=roles or ["admin"],
            oidc_subject=_OPERATOR,
        ),
        oidc_issuer=_ISSUER,
    )


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    async def execute(self, *args: object, **kwargs: object) -> None:
        return None

    def begin(self) -> _NoopTransactionCM:
        return _NoopTransactionCM()


class _SessionCM:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeProposalQueries:
    def __init__(self) -> None:
        self.families: dict[uuid.UUID, FamilyRow] = {}
        self.versions: dict[tuple[uuid.UUID, int], VersionRow] = {}

    async def load_version(self, _session: object, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow | None:
        return self.versions.get((proposal_id, proposal_version))

    async def load_family(self, _session: object, artifact_id: uuid.UUID) -> FamilyRow | None:
        return self.families.get(artifact_id)

    def seed(
        self,
        *,
        proposal_id: uuid.UUID,
        artifact_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        state: str = "open",
        reviewed_baseline_revision_id: uuid.UUID | None = None,
        semantics: dict[str, Any] | None = None,
    ) -> None:
        self.families[artifact_id] = FamilyRow(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
        self.versions[(proposal_id, 1)] = VersionRow(
            proposal_id=proposal_id,
            proposal_version=1,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            state=state,
            source_evidence_id=uuid.uuid4(),
            reviewed_baseline_revision_id=reviewed_baseline_revision_id,
            revision_id=None,
            risk_classification=None,
            risk_algorithm_version=None,
            opened_by_issuer=_ISSUER,
            opened_by_subject=_OPERATOR,
            created_at=_NOW,
            frozen_at=None,
            terminal_reason_code=None,
            terminal_note=None,
            terminal_by_issuer=None,
            terminal_by_subject=None,
            terminalized_at=None,
            semantics=semantics,
        )


class FakeProvenanceQueries:
    """In-memory stand-in for the `queries.provenance` functions
    `semantic_tests.py` calls: freezing test rows and reading live
    applicability rules for a baseline revision."""

    def __init__(self) -> None:
        self.tests: dict[tuple[uuid.UUID, int, str], SemanticTestRow] = {}
        self.rules_by_revision: dict[uuid.UUID, list[ApplicabilityRuleRow]] = {}

    async def upsert_semantic_test(self, _session: object, **kwargs: Any) -> None:
        key = (kwargs["proposal_id"], kwargs["proposal_version"], kwargs["test_id"])
        self.tests[key] = SemanticTestRow(
            proposal_id=kwargs["proposal_id"],
            proposal_version=kwargs["proposal_version"],
            test_id=kwargs["test_id"],
            manifest=kwargs["manifest"],
            passed=kwargs["passed"],
            expected=kwargs["expected"],
            actual=kwargs["actual"],
            executed_at=kwargs["executed_at"],
        )

    async def load_semantic_tests(
        self, _session: object, proposal_id: uuid.UUID, proposal_version: int
    ) -> list[SemanticTestRow]:
        rows = [row for (pid, ver, _tid), row in self.tests.items() if pid == proposal_id and ver == proposal_version]
        return sorted(rows, key=lambda r: r.test_id)

    async def load_applicability_rules_for_revision(
        self, _session: object, revision_id: uuid.UUID
    ) -> list[ApplicabilityRuleRow]:
        return self.rules_by_revision.get(revision_id, [])


def _build_service(
    monkeypatch: pytest.MonkeyPatch, proposal_fake: FakeProposalQueries, provenance_fake: FakeProvenanceQueries
) -> st.SemanticTestService:
    monkeypatch.setattr(st, "proposal_queries", proposal_fake)
    monkeypatch.setattr(st, "queries", provenance_fake)
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    return st.SemanticTestService(lambda: _SessionCM(_NullSession()), authorization=authorization, clock=_FakeClock())


def _rule(**overrides: Any) -> ApplicabilityRuleRow:
    base = dict(
        rule_id=uuid.uuid4(),
        scope="global",
        target_tenant_id=None,
        capability_ids=None,
        capability_labels=None,
        domain_ids=None,
        task_kinds=None,
        action_classes=None,
        environments=None,
        data_sensitivity_tiers=None,
    )
    base.update(overrides)
    return ApplicabilityRuleRow(**base)


# ---------------------------------------------------------------------------
# The matching engine -- both directions.
# ---------------------------------------------------------------------------


def test_rule_covers_predicate_on_overlap_and_not_on_disjoint_values() -> None:
    rule = {"task_kinds": ["code_change", "deployment"]}
    overlapping_predicate = {"task_kind": ["code_change"]}
    disjoint_predicate = {"task_kind": ["data_access"]}

    assert st.rule_covers_predicate(rule, overlapping_predicate) is True
    assert st.rule_covers_predicate(rule, disjoint_predicate) is False


def test_rule_covers_predicate_wildcard_on_either_side_never_blocks() -> None:
    wildcard_rule = {"task_kinds": None}
    specific_predicate = {"task_kind": ["code_change"]}
    assert st.rule_covers_predicate(wildcard_rule, specific_predicate) is True

    specific_rule = {"task_kinds": ["code_change"]}
    wildcard_predicate: dict[str, Any] = {"task_kind": None}
    assert st.rule_covers_predicate(specific_rule, wildcard_predicate) is True


def test_rule_covers_predicate_requires_every_named_dimension_to_pass() -> None:
    """One non-overlapping, non-wildcard dimension refuses the whole
    rule even when every other dimension overlaps."""
    rule = {"task_kinds": ["code_change"], "environments": ["staging"]}
    matches_task_kind_but_not_environment = {"task_kind": ["code_change"], "environment": ["production"]}
    assert st.rule_covers_predicate(rule, matches_task_kind_but_not_environment) is False


def test_evaluate_predicate_true_with_a_covering_rule_false_with_none() -> None:
    predicate = {"task_kind": ["code_change"]}
    assert st.evaluate_predicate(predicate, [{"task_kinds": ["code_change"]}]) is True
    assert st.evaluate_predicate(predicate, [{"task_kinds": ["deployment"]}]) is False
    assert st.evaluate_predicate(predicate, []) is False


# ---------------------------------------------------------------------------
# run() -- frozen-input binding and gating.
# ---------------------------------------------------------------------------


async def test_run_freezes_matched_true_against_a_covering_candidate_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(
        proposal_id=proposal_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        state="open",
        semantics={"applicability": [{"task_kinds": ["code_change"]}]},
    )
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)

    results = await service.run(
        ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {"task_kind": ["code_change"]}}]
    )
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].actual == {"matched": True}
    assert results[0].expected == {"matched": True}


async def test_run_freezes_matched_false_with_no_candidate_persisted_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `PATCH` has landed on this version (`semantics IS NULL`) --
    nothing has been shown to cover anything, so every non-wildcard
    predicate reports unmatched rather than erroring or borrowing coverage
    from anywhere else."""
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)

    results = await service.run(
        ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {"task_kind": ["code_change"]}}]
    )
    assert results[0].passed is False
    assert results[0].actual == {"matched": False}


async def test_run_evaluates_the_candidates_own_rules_not_the_reviewed_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The correctness fix this task exists for: `run()` must evaluate the
    PATCHed candidate's own `semantics.applicability`, never the reviewed
    baseline revision's live rules -- even when a `reviewed_baseline_
    revision_id` is set and its rules say the opposite. Two manifests, two
    directions: one the candidate covers and the baseline does not, and
    the reverse, so a matcher that (wrongly) fell back to the baseline in
    either direction fails one assertion or the other.
    """
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    proposal_fake.seed(
        proposal_id=proposal_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        state="open",
        reviewed_baseline_revision_id=revision_id,
        semantics={"applicability": [{"task_kinds": ["deployment"]}]},
    )
    # The baseline's own live rules say the opposite of the candidate. If
    # `run()` ever fell back to reading them, one of the two assertions
    # below flips.
    provenance_fake.rules_by_revision[revision_id] = [_rule(task_kinds=["code_change"])]
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)

    results = await service.run(
        ctx,
        proposal_id,
        1,
        tests=[
            {"test_id": "candidate_covers_baseline_does_not", "manifest": {"task_kind": ["deployment"]}},
            {"test_id": "baseline_covers_candidate_does_not", "manifest": {"task_kind": ["code_change"]}},
        ],
    )
    by_id = {r.test_id: r for r in results}
    assert by_id["candidate_covers_baseline_does_not"].passed is True
    assert by_id["candidate_covers_baseline_does_not"].actual == {"matched": True}
    assert by_id["baseline_covers_candidate_does_not"].passed is False
    assert by_id["baseline_covers_candidate_does_not"].actual == {"matched": False}


async def test_rerunning_a_test_id_with_a_changed_manifest_overwrites_the_frozen_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of freezing: a second run under the same `test_id`
    with a *different* manifest must not leave the first run's `actual`
    silently describing the new input. The stored row must reflect
    exactly what the latest input computed to."""
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(
        proposal_id=proposal_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        state="open",
        semantics={"applicability": [{"task_kinds": ["code_change"]}]},
    )
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)

    first = await service.run(
        ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {"task_kind": ["code_change"]}}]
    )
    assert first[0].passed is True
    stored_first = provenance_fake.tests[(proposal_id, 1, "t1")]
    assert stored_first.manifest["task_kind"] == ["code_change"]
    assert stored_first.actual == {"matched": True}

    second = await service.run(
        ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {"task_kind": ["deployment"]}}]
    )
    assert second[0].passed is False
    stored_second = provenance_fake.tests[(proposal_id, 1, "t1")]
    assert stored_second.manifest["task_kind"] == ["deployment"]
    assert stored_second.actual == {"matched": False}
    # Exactly one row for this test_id -- the second run replaced the
    # first in place, it did not accumulate a stale second row beside it.
    assert len([k for k in provenance_fake.tests if k[:2] == (proposal_id, 1)]) == 1


async def test_run_refuses_when_not_open_and_when_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="submitted")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)

    with pytest.raises(ProposalStateConflict):
        await service.run(_ctx(tenant_id=tenant_id), proposal_id, 1, tests=[{"test_id": "t1", "manifest": {}}])

    proposal_fake.versions[(proposal_id, 1)] = proposal_fake.versions[(proposal_id, 1)].__class__(
        **{**proposal_fake.versions[(proposal_id, 1)].__dict__, "state": "open"}
    )
    other_tenant_ctx = _ctx(tenant_id=uuid.uuid4())
    with pytest.raises(ArcAuthorizationError):
        await service.run(other_tenant_ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {}}])


async def test_run_unknown_version_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _build_service(monkeypatch, FakeProposalQueries(), FakeProvenanceQueries())
    with pytest.raises(NotFoundError):
        await service.run(_ctx(), uuid.uuid4(), 1, tests=[{"test_id": "t1", "manifest": {}}])


async def test_list_for_version_returns_frozen_results(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)
    await service.run(ctx, proposal_id, 1, tests=[{"test_id": "t1", "manifest": {"task_kind": ["code_change"]}}])

    results = await service.list_for_version(ctx, proposal_id, 1)
    assert [r.test_id for r in results] == ["t1"]
