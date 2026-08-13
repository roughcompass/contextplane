"""Unit tests for `contextplane/arc/service/provenance.py`.

No database: `queries.provenance`'s and `queries.proposal`'s module-level
functions are monkeypatched with small in-memory fakes, matching
`test_arc_proposal.py`'s own convention. Two things this file exists to
prove, per the task's own contract:

1. Conditional validation is genuinely conditional -- each provenance
   class's rule is shown to fire on a violating shape *and* stay silent on
   a conforming one, in the same test, so a validator that always refuses
   could not pass it.
2. Field provenance is per-field and survives a sibling edit -- editing
   field B does not disturb field A's already-recorded row, and
   re-editing field A in place updates only that row.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.api.routers import arc_authoring as router_mod
from contextplane.arc.service import provenance as pv
from contextplane.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from contextplane.arc.service.proposal import ProposalStateConflict
from contextplane.arc.service.queries.proposal import FamilyRow, VersionRow
from contextplane.arc.service.queries.provenance import FieldProvenanceRow
from contextplane.arc.types import ArcRequestContext
from contextplane.exceptions import NotFoundError
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"


class _FakeClock:
    def now(self) -> datetime.datetime:
        return _NOW


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(
    *, tenant_id: uuid.UUID | None = None, subject: str = _OPERATOR, roles: list[str] | None = None
) -> ArcRequestContext:
    return ArcRequestContext(
        tenant=TenantContext(
            tenant_id=tenant_id or uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=roles or ["admin"],
            oidc_subject=subject,
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
    """Read-only stand-in for the two `queries.proposal` reads
    `provenance.py` uses to authorize and gate on state."""

    def __init__(self) -> None:
        self.families: dict[uuid.UUID, FamilyRow] = {}
        self.versions: dict[tuple[uuid.UUID, int], VersionRow] = {}

    async def load_version(self, _session: object, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow | None:
        return self.versions.get((proposal_id, proposal_version))

    async def load_family(self, _session: object, artifact_id: uuid.UUID) -> FamilyRow | None:
        return self.families.get(artifact_id)

    def seed(
        self, *, proposal_id: uuid.UUID, artifact_id: uuid.UUID, tenant_id: uuid.UUID | None, state: str = "open"
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
            reviewed_baseline_revision_id=None,
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
        )


class FakeProvenanceQueries:
    """In-memory stand-in for `queries.provenance`'s field-provenance
    functions, faithful to the real table's PK: `(proposal_id,
    proposal_version, field_path)`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, int, str], FieldProvenanceRow] = {}

    async def upsert_field_provenance(self, _session: object, **kwargs: Any) -> None:
        key = (kwargs["proposal_id"], kwargs["proposal_version"], kwargs["field_path"])
        self.rows[key] = FieldProvenanceRow(
            proposal_id=kwargs["proposal_id"],
            proposal_version=kwargs["proposal_version"],
            field_path=kwargs["field_path"],
            provenance_class=kwargs["provenance_class"],
            source_evidence_id=kwargs["source_evidence_id"],
            source_anchor=kwargs["source_anchor"],
            excerpt_digest=kwargs["excerpt_digest"],
            author_issuer=kwargs["author_issuer"],
            author_subject=kwargs["author_subject"],
            author_role=kwargs["author_role"],
            derivation_profile=kwargs["derivation_profile"],
            created_at=kwargs["created_at"],
        )

    async def load_field_provenance(
        self, _session: object, proposal_id: uuid.UUID, proposal_version: int
    ) -> list[FieldProvenanceRow]:
        rows = [row for (pid, ver, _fp), row in self.rows.items() if pid == proposal_id and ver == proposal_version]
        return sorted(rows, key=lambda r: r.field_path)


def _build_service(
    monkeypatch: pytest.MonkeyPatch, proposal_fake: FakeProposalQueries, provenance_fake: FakeProvenanceQueries
) -> pv.ProvenanceService:
    monkeypatch.setattr(pv, "proposal_queries", proposal_fake)
    monkeypatch.setattr(pv, "queries", provenance_fake)
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    return pv.ProvenanceService(lambda: _SessionCM(_NullSession()), authorization=authorization, clock=_FakeClock())


def _source_backed(field_path: str = "$.directives[0]") -> dict[str, Any]:
    return {
        "field_path": field_path,
        "provenance_class": "source_backed",
        "source_evidence_id": uuid.uuid4(),
        "source_anchor": "p1",
        "excerpt_digest": "1" * 64,
        "author_role": None,
        "derivation_profile": None,
    }


def _human_judgment(field_path: str = "$.directives[0]") -> dict[str, Any]:
    return {
        "field_path": field_path,
        "provenance_class": "human_judgment",
        "source_evidence_id": None,
        "source_anchor": None,
        "excerpt_digest": None,
        "author_role": "reviewer",
        "derivation_profile": None,
    }


def _server_derived(field_path: str = "$.directives[0]") -> dict[str, Any]:
    return {
        "field_path": field_path,
        "provenance_class": "server_derived",
        "source_evidence_id": None,
        "source_anchor": None,
        "excerpt_digest": None,
        "author_role": None,
        "derivation_profile": "risk_engine_v1",
    }


# ---------------------------------------------------------------------------
# Conditional validation -- both directions, per class.
# ---------------------------------------------------------------------------


def test_source_backed_conditional_rule_fires_and_stays_silent() -> None:
    valid = _source_backed()
    pv._check_conditional(valid)  # does not raise: the conforming shape passes

    missing_required = dict(valid, excerpt_digest=None)
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(missing_required)

    forbidden_present = dict(valid, derivation_profile="x")
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(forbidden_present)


def test_human_judgment_conditional_rule_fires_and_stays_silent() -> None:
    valid = _human_judgment()
    pv._check_conditional(valid)  # does not raise

    missing_required = dict(valid, author_role=None)
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(missing_required)

    forbidden_present = dict(valid, source_anchor="p1")
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(forbidden_present)


def test_server_derived_conditional_rule_fires_and_stays_silent() -> None:
    valid = _server_derived()
    pv._check_conditional(valid)  # does not raise

    missing_required = dict(valid, derivation_profile=None)
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(missing_required)

    forbidden_present = dict(valid, author_role="reviewer")
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional(forbidden_present)


def test_unknown_provenance_class_is_invalid() -> None:
    with pytest.raises(pv.ProvenanceInvalid):
        pv._check_conditional({"field_path": "$.x", "provenance_class": "guessed"})


# ---------------------------------------------------------------------------
# Author-is-caller -- both directions.
# ---------------------------------------------------------------------------


def test_author_is_caller_not_body() -> None:
    """An entry that never names an author passes; one that injects
    `author` (or any other reserved actor field) is refused with
    `arc_actor_not_caller_supplied` regardless of how it arrived at this
    service -- see the module docstring on why this is checked here too,
    not only by the closed wire schema upstream of it."""
    clean = _human_judgment()
    pv._assert_no_injected_actor_fields(clean)  # does not raise

    injected = dict(clean, author={"issuer": "someone-else", "subject": "not-the-caller"})
    with pytest.raises(pv.ActorNotCallerSupplied):
        pv._assert_no_injected_actor_fields(injected)

    injected_issuer = dict(clean, author_issuer="someone-else")
    with pytest.raises(pv.ActorNotCallerSupplied):
        pv._assert_no_injected_actor_fields(injected_issuer)


def test_router_maps_provenance_invalid_and_actor_not_caller_supplied() -> None:
    """`arc_provenance_invalid` and `arc_actor_not_caller_supplied` are
    wired at the router's one error-translation chokepoint, matching the
    bounded Appendix A.5 codes rather than a generic 4xx."""
    provenance_exc = router_mod._translate_error(pv.ProvenanceInvalid("bad shape"))
    assert provenance_exc.status_code == 422
    assert provenance_exc.detail[0]["code"] == "arc_provenance_invalid"

    actor_exc = router_mod._translate_error(pv.ActorNotCallerSupplied("injected author"))
    assert actor_exc.status_code == 400
    assert actor_exc.detail[0]["code"] == "arc_actor_not_caller_supplied"

    semantics_exc = router_mod._translate_error(pv.SemanticsValidationFailed("duplicate id"))
    assert semantics_exc.status_code == 422
    assert semantics_exc.detail[0]["code"] == "arc_proposal_validation_failed"


# ---------------------------------------------------------------------------
# Field-provenance per-field survival.
# ---------------------------------------------------------------------------


async def test_edit_persists_a_specific_field_and_a_sibling_edit_does_not_lose_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)

    await service.edit(ctx, proposal_id, 1, entries=[_source_backed("$.directives[0]")])
    field_a_first = provenance_fake.rows[(proposal_id, 1, "$.directives[0]")]
    assert field_a_first.source_anchor == "p1"

    # A sibling edit (a different field_path) must not touch field A's row.
    await service.edit(ctx, proposal_id, 1, entries=[_human_judgment("$.directives[1]")])
    field_a_after_sibling_edit = provenance_fake.rows[(proposal_id, 1, "$.directives[0]")]
    assert field_a_after_sibling_edit == field_a_first
    field_b = provenance_fake.rows[(proposal_id, 1, "$.directives[1]")]
    assert field_b.provenance_class == "human_judgment"
    assert field_b.author_issuer == _ISSUER
    assert field_b.author_subject == _OPERATOR

    # Re-editing field A in place updates only that row -- prove it is
    # retrievable and reflects the new value, not the first one.
    changed = dict(_source_backed("$.directives[0]"), source_anchor="p2")
    await service.edit(ctx, proposal_id, 1, entries=[changed])
    field_a_after_reedit = provenance_fake.rows[(proposal_id, 1, "$.directives[0]")]
    assert field_a_after_reedit.source_anchor == "p2"
    assert field_a_after_reedit.source_anchor != field_a_first.source_anchor
    field_b_untouched = provenance_fake.rows[(proposal_id, 1, "$.directives[1]")]
    assert field_b_untouched == field_b


async def test_edit_refuses_when_not_open_and_when_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="submitted")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)

    with pytest.raises(ProposalStateConflict):
        await service.edit(_ctx(tenant_id=tenant_id), proposal_id, 1, entries=[_source_backed()])

    proposal_fake.versions[(proposal_id, 1)] = proposal_fake.versions[(proposal_id, 1)].__class__(
        **{**proposal_fake.versions[(proposal_id, 1)].__dict__, "state": "open"}
    )
    other_tenant_ctx = _ctx(tenant_id=uuid.uuid4())
    with pytest.raises(ArcAuthorizationError):
        await service.edit(other_tenant_ctx, proposal_id, 1, entries=[_source_backed()])


async def test_edit_unknown_version_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _build_service(monkeypatch, FakeProposalQueries(), FakeProvenanceQueries())
    with pytest.raises(NotFoundError):
        await service.edit(_ctx(), uuid.uuid4(), 1, entries=[_source_backed()])


# ---------------------------------------------------------------------------
# revalidate_stored
# ---------------------------------------------------------------------------


async def test_revalidate_stored_reports_valid_true_when_every_row_still_conforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)
    await service.edit(ctx, proposal_id, 1, entries=[_source_backed(), _human_judgment("$.directives[1]")])

    result = await service.revalidate_stored(ctx, proposal_id, 1)
    assert result.valid is True
    assert result.errors == ()


async def test_revalidate_stored_reports_each_violating_row(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    provenance_fake = FakeProvenanceQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open")
    service = _build_service(monkeypatch, proposal_fake, provenance_fake)
    ctx = _ctx(tenant_id=tenant_id)
    await service.edit(ctx, proposal_id, 1, entries=[_source_backed("$.a")])

    # Simulate a row that no longer conforms (e.g. written by a future
    # direct-DB path this check exists to catch) without going through
    # `edit()`'s own guard.
    row = provenance_fake.rows[(proposal_id, 1, "$.a")]
    provenance_fake.rows[(proposal_id, 1, "$.a")] = row.__class__(**{**row.__dict__, "excerpt_digest": None})

    result = await service.revalidate_stored(ctx, proposal_id, 1)
    assert result.valid is False
    assert len(result.errors) == 1
    assert result.errors[0].field_path == "$.a"
    assert result.errors[0].code == "arc_provenance_invalid"


# ---------------------------------------------------------------------------
# validate_candidate_semantics
# ---------------------------------------------------------------------------


def _valid_semantics(*, extra_rule: dict[str, Any] | None = None) -> dict[str, Any]:
    rule = {
        "rule_id": str(uuid.uuid4()),
        "scope": "global",
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "intent_kinds": None,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
        "is_mandatory": True,
    }
    applicability = [rule] if extra_rule is None else sorted([rule, extra_rule], key=lambda r: r["rule_id"])
    return {
        "profile": "arc_artifact_semantics_v2",
        "projection_schema_version": 1,
        "materialiser_profile": "directive_bundle_v1",
        "materialiser_version": "1.0.0",
        "applicability_baseline_version": "1",
        "artifact_id": str(uuid.uuid4()),
        "revision_id": str(uuid.uuid4()),
        "kind": "directive_bundle",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "visibility": "standard",
        "source_system": "internal-docs",
        "source_revision_locator": "rev-1",
        "source_content_digest": "1" * 64,
        "source_approval_evidence_digest": "2" * 64,
        "directives": [],
        "applicability": applicability,
        "detail_audience": "agent_and_human",
        "review_expires_at": "2026-06-01T00:00:00Z",
        "content_classification": "internal",
        "approved_retention_floor_days": 90,
        "initial_freshness_basis": "connector_verified",
        "reviewed_baseline_revision_id": None,
    }


def test_validate_candidate_semantics_accepts_conforming_and_rejects_missing_a_required_field() -> None:
    conforming = _valid_semantics()
    result = pv.validate_candidate_semantics(conforming)
    assert result is None  # conforming input raises nothing to catch

    missing_required_field = dict(conforming)
    del missing_required_field["materialiser_version"]
    with pytest.raises(pv.SemanticsValidationFailed):
        pv.validate_candidate_semantics(missing_required_field)


def test_validate_candidate_semantics_rejects_duplicate_rule_ids() -> None:
    """Caught by the reused profile validator's own ordering check (two
    equal rule_ids can never be strictly ascending), not a second
    duplicate scan -- see `validate_candidate_semantics`'s own docstring
    for why a second one would be dead code. What this test pins is the
    wrapping: `AuthoringProfileError` still surfaces as
    `SemanticsValidationFailed` for this input."""
    semantics = _valid_semantics()
    semantics["applicability"] = semantics["applicability"] * 2  # same rule_id twice
    with pytest.raises(pv.SemanticsValidationFailed):
        pv.validate_candidate_semantics(semantics)


def test_ambiguous_selector_rule_fires_and_stays_silent() -> None:
    """Two distinct rule_ids (so the reused ordering check has nothing to
    object to -- `_valid_semantics` sorts the array ascending by rule_id)
    sharing one identical selector are refused; two distinct rule_ids
    with a genuinely different selector are not. Both cases go through
    the same `validate_candidate_semantics` call, so a check that always
    refused could not pass the second assertion, and a check that never
    fired could not pass the first."""
    identical_selector_rule = {
        "rule_id": str(uuid.uuid4()),
        "scope": "global",
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "intent_kinds": None,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
        "is_mandatory": True,
    }
    with pytest.raises(pv.SemanticsValidationFailed):
        pv.validate_candidate_semantics(_valid_semantics(extra_rule=identical_selector_rule))

    different_selector_rule = dict(identical_selector_rule, intent_kinds=["code_change"])
    pv.validate_candidate_semantics(_valid_semantics(extra_rule=different_selector_rule))  # does not raise
