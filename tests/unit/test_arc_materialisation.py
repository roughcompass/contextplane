"""Unit tests for `registry/arc/service/submission.py`.

No database: `queries.proposal.load_version`/`load_family` and
`queries.materialisation.insert_draft_revision`/`freeze_and_link` are
monkeypatched with in-memory fakes faithful enough to the real relational
shape (a compare-and-swap guard on the freeze, a bijection dict keyed by
`revision_id`) to exercise every real branch without Postgres. What a fake
cannot prove -- that the database constraints actually hold under a real
race, and that a refused submit leaves the schema byte-identical -- is
`tests/integration/test_arc_submission.py`'s job.

The defining proof this suite carries: `test_refuses_before_opening_a_
session_*` pass an `_ExplodingSessionFactory` that raises the instant it is
called, matching `test_arc_source_status.py`'s own convention for the
identical claim (`SourceStatusService.record_revocation`). A refusal that
merely leaves the row unchanged could still have opened a session and rolled
back; a refusal that raises before the session factory is ever called did
not touch the database at all -- a strictly stronger claim.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from registry.arc.service import submission as sub
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries.materialisation import DraftRevision, FrozenVersion
from registry.arc.service.queries.proposal import FamilyRow, VersionRow
from registry.arc.types import ArcRequestContext
from registry.exceptions import NotFoundError, RegistryError
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_PROPOSAL_ID = uuid.uuid4()
_PROPOSAL_VERSION = 1
_ARTIFACT_ID = uuid.uuid4()
_SOURCE_EVIDENCE_ID = uuid.uuid4()
_BASELINE_ID = uuid.uuid4()
_CANDIDATE_REVISION_ID = uuid.uuid4()


class _FakeClock:
    def __init__(self, moment: datetime.datetime = _NOW) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _authorization(*, global_operator: bool = True) -> ArcAuthorizationService:
    allowlist = ((_ISSUER, _OPERATOR),) if global_operator else ()
    return ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=allowlist)


def _ctx(*, tenant_id: uuid.UUID | None = None, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id or uuid.uuid4(), actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=_OPERATOR
    )
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


# ---------------------------------------------------------------------------
# Session-factory doubles
# ---------------------------------------------------------------------------


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    """Records every executed statement's SQL text, so a test can assert
    whether the audit outbox was reached without needing a real database."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, clause: object, params: object = None) -> None:
        self.executed.append(str(clause))

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def begin(self) -> _NoopTransactionCM:
        return _NoopTransactionCM()


class _SessionCM:
    def __init__(self, session: _NullSession) -> None:
        self._session = session

    async def __aenter__(self) -> _NullSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _RecordingSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_NullSession] = []

    def __call__(self) -> _SessionCM:
        session = _NullSession()
        self.sessions.append(session)
        return _SessionCM(session)


class _ExplodingSessionFactory:
    """Raises the instant it is called -- proof that a code path opened no
    session at all, which is a stronger claim than "the row was unchanged
    afterward": there is no session for a write to have happened on."""

    def __call__(self) -> object:
        raise AssertionError("this code path must not open a session")


# ---------------------------------------------------------------------------
# In-memory fakes for the two queries modules `submit` calls
# ---------------------------------------------------------------------------


def _version_row(**overrides: Any) -> VersionRow:
    base: dict[str, Any] = {
        "proposal_id": _PROPOSAL_ID,
        "proposal_version": _PROPOSAL_VERSION,
        "artifact_id": _ARTIFACT_ID,
        "tenant_id": None,
        "state": "open",
        "source_evidence_id": _SOURCE_EVIDENCE_ID,
        "reviewed_baseline_revision_id": _BASELINE_ID,
        "revision_id": None,
        "risk_classification": None,
        "risk_algorithm_version": None,
        "opened_by_issuer": _ISSUER,
        "opened_by_subject": _OPERATOR,
        "created_at": _NOW,
        "frozen_at": None,
        "terminal_reason_code": None,
        "terminal_note": None,
        "terminal_by_issuer": None,
        "terminal_by_subject": None,
        "terminalized_at": None,
    }
    # `semantics` is left to the dataclass's own `None` default unless a
    # test passes one through `overrides` -- every test that cares whether
    # a candidate was ever persisted says so explicitly at its own call
    # site (`semantics=_candidate()` or `semantics=None`) rather than this
    # shared helper picking a default either test would have to override.
    base.update(overrides)
    return VersionRow(**base)


def _candidate(**overrides: Any) -> dict[str, Any]:
    """A minimal, valid `arc_artifact_semantics_v1` candidate, as
    `ProvenanceService.edit` would have persisted it (a plain JSON-shaped
    dict, matching `ArtifactSemantics.model_dump(mode="json")`). Only the
    fields `_draft_revision` reads are populated -- this is not a schema
    conformance fixture, `AAS-T04`'s conformance suite owns that.
    """
    base: dict[str, Any] = {
        "revision_id": str(_CANDIDATE_REVISION_ID),
        "artifact_id": str(_ARTIFACT_ID),
        "source_system": "confluence",
        "source_revision_locator": "conf://space/page@3",
        "source_content_digest": "1" * 64,
        "review_expires_at": (_NOW + datetime.timedelta(days=365)).isoformat(),
        "initial_freshness_basis": "revision_pinned_only",
        "content_classification": "internal",
        "approved_retention_floor_days": 730,
    }
    base.update(overrides)
    return base


class FakeProposalQueries:
    def __init__(self) -> None:
        self.versions: dict[tuple[uuid.UUID, int], VersionRow] = {}
        self.families: dict[uuid.UUID, FamilyRow] = {}

    def seed_version(self, row: VersionRow) -> None:
        self.versions[(row.proposal_id, row.proposal_version)] = row

    def seed_family(self, row: FamilyRow) -> None:
        self.families[row.artifact_id] = row

    async def load_version(self, _session: object, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow | None:
        return self.versions.get((proposal_id, proposal_version))

    async def load_family(self, _session: object, artifact_id: uuid.UUID) -> FamilyRow | None:
        return self.families.get(artifact_id)


class FakeMaterialisationQueries:
    def __init__(self, *, cas_succeeds: bool = True) -> None:
        self.cas_succeeds = cas_succeeds
        self.inserted_drafts: list[DraftRevision] = []
        self.freeze_calls: list[dict[str, Any]] = []

    async def insert_draft_revision(self, _session: object, draft: DraftRevision) -> None:
        self.inserted_drafts.append(draft)

    async def freeze_and_link(
        self,
        _session: object,
        *,
        proposal_id: uuid.UUID,
        proposal_version: int,
        revision_id: uuid.UUID,
        now: datetime.datetime,
    ) -> FrozenVersion | None:
        self.freeze_calls.append(
            {"proposal_id": proposal_id, "proposal_version": proposal_version, "revision_id": revision_id, "now": now}
        )
        if not self.cas_succeeds:
            return None
        return FrozenVersion(
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            state="submitted",
            revision_id=revision_id,
            frozen_at=now,
        )


def _family_row(**overrides: Any) -> FamilyRow:
    base: dict[str, Any] = {
        "artifact_id": _ARTIFACT_ID,
        "tenant_id": None,
        "slug": "family-1",
        "kind": "policy",
        "title": "Test family",
        "active_revision_id": None,
        "created_at": _NOW,
        "created_by_issuer": _ISSUER,
        "created_by_subject": _OPERATOR,
    }
    base.update(overrides)
    return FamilyRow(**base)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeProposalQueries, FakeMaterialisationQueries]:
    proposal_fake = FakeProposalQueries()
    materialisation_fake = FakeMaterialisationQueries()
    monkeypatch.setattr(sub, "proposal_queries", proposal_fake)
    monkeypatch.setattr(sub, "materialisation_queries", materialisation_fake)
    return proposal_fake, materialisation_fake


def _service(
    session_factory: object, *, appender: object | None = None, validator: object | None = None
) -> sub.ArtifactMaterialisationService:
    return sub.ArtifactMaterialisationService(
        session_factory,  # type: ignore[arg-type]
        authorization=_authorization(),
        clock=_FakeClock(),
        operational_chain_appender=appender,
        risk_envelope_validator=validator,
    )


# ---------------------------------------------------------------------------
# The guard: refuses before opening a session, on every combination of a
# missing collaborator -- including the real default, both missing.
# ---------------------------------------------------------------------------


async def test_refuses_before_opening_a_session_when_both_collaborators_are_missing() -> None:
    service = _service(_ExplodingSessionFactory())
    with pytest.raises(sub.SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())


async def test_refuses_before_opening_a_session_when_only_the_appender_is_missing() -> None:
    service = _service(_ExplodingSessionFactory(), validator=object())
    with pytest.raises(sub.SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())


async def test_refuses_before_opening_a_session_when_only_the_validator_is_missing() -> None:
    service = _service(_ExplodingSessionFactory(), appender=object())
    with pytest.raises(sub.SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())


async def test_calling_submit_twice_refuses_identically_both_times() -> None:
    """The refusal itself is idempotent: a second call is not a second,
    different failure -- it is the same refusal, and still touches nothing."""
    service = _service(_ExplodingSessionFactory())
    with pytest.raises(sub.SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())
    with pytest.raises(sub.SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())


# ---------------------------------------------------------------------------
# The transaction, once both collaborators are present (a test double, not
# a production wiring -- neither collaborator exists yet).
# ---------------------------------------------------------------------------


async def test_submit_materialises_a_draft_revision_and_returns_it(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    proposal_fake, materialisation_fake = fakes
    proposal_fake.seed_version(_version_row(semantics=_candidate()))
    proposal_fake.seed_family(_family_row())
    factory = _RecordingSessionFactory()
    service = _service(factory, appender=object(), validator=object())

    result = await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())

    assert result.proposal_id == _PROPOSAL_ID
    assert result.proposal_version == _PROPOSAL_VERSION
    assert result.revision_id == _CANDIDATE_REVISION_ID
    assert len(materialisation_fake.inserted_drafts) == 1
    assert len(materialisation_fake.freeze_calls) == 1
    assert materialisation_fake.freeze_calls[0]["revision_id"] == _CANDIDATE_REVISION_ID
    # The audit event committed in the same transaction as the write --
    # `session.execute` is the one seam both `insert_draft_revision`'s real
    # SQL and `audit_outbox.emit`'s real SQL go through; here it is the
    # fake's own executed log, populated only by `audit_outbox.emit`, since
    # the two queries functions are faked above rather than hitting the
    # session at all.
    assert len(factory.sessions) == 1
    assert any("arc_audit_outbox" in stmt for stmt in factory.sessions[0].executed)


async def test_draft_revision_maps_the_persisted_candidate(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    """Every field `_draft_revision` sets traces to a named source: a direct
    copy from the candidate, a derivation with a stated reason, or the
    documented vocabulary-mismatch placeholder -- never a bare guess."""
    proposal_fake, materialisation_fake = fakes
    proposal_fake.seed_version(_version_row(semantics=_candidate()))
    proposal_fake.seed_family(_family_row())
    service = _service(_RecordingSessionFactory(), appender=object(), validator=object())

    await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())

    draft = materialisation_fake.inserted_drafts[0]
    assert draft.revision_id == _CANDIDATE_REVISION_ID
    assert draft.artifact_id == _ARTIFACT_ID
    assert draft.source_system == "confluence"
    assert draft.source_revision_locator == "conf://space/page@3"
    assert draft.content_digest == "1" * 64
    assert draft.content_classification == "internal"
    assert draft.freshness_basis == "revision_pinned_only"
    assert draft.review_expires_at == _NOW + datetime.timedelta(days=365)
    assert draft.content_retention_until == _NOW + datetime.timedelta(days=730)
    assert draft.detail_audience == sub._DETAIL_AUDIENCE_SHELL_DEFAULT
    assert draft.source_canonical_locator == f"urn:arc-authoring:artifact:{_ARTIFACT_ID}"


async def test_submit_refuses_when_no_candidate_was_ever_persisted(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    proposal_fake, materialisation_fake = fakes
    proposal_fake.seed_version(_version_row(semantics=None))
    proposal_fake.seed_family(_family_row())
    service = _service(_RecordingSessionFactory(), appender=object(), validator=object())

    with pytest.raises(sub.CandidateSemanticsMissing):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())

    assert materialisation_fake.inserted_drafts == []
    assert materialisation_fake.freeze_calls == []


async def test_submit_refuses_when_the_version_does_not_exist(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    service = _service(_RecordingSessionFactory(), appender=object(), validator=object())
    with pytest.raises(NotFoundError):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())


async def test_submit_refuses_when_the_candidate_names_a_different_artifact(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    proposal_fake, materialisation_fake = fakes
    other_artifact = uuid.uuid4()
    proposal_fake.seed_version(_version_row(semantics=_candidate(artifact_id=str(other_artifact))))
    proposal_fake.seed_family(_family_row())
    service = _service(_RecordingSessionFactory(), appender=object(), validator=object())

    with pytest.raises(RegistryError):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())

    assert materialisation_fake.inserted_drafts == []


async def test_submit_refuses_when_the_caller_lacks_write_authority(
    fakes: tuple[FakeProposalQueries, FakeMaterialisationQueries],
) -> None:
    proposal_fake, materialisation_fake = fakes
    tenant_id = uuid.uuid4()
    proposal_fake.seed_version(_version_row(tenant_id=tenant_id, semantics=_candidate()))
    proposal_fake.seed_family(_family_row(tenant_id=tenant_id))
    service = _service(_RecordingSessionFactory(), appender=object(), validator=object())

    with pytest.raises(ArcAuthorizationError):
        await service.submit(
            _ctx(tenant_id=uuid.uuid4()), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object()
        )

    assert materialisation_fake.inserted_drafts == []


# ---------------------------------------------------------------------------
# Bijection / frozen-version immutability: a lost compare-and-swap refuses
# and writes no audit event, even though the draft revision insert (which
# must precede the bijection write to satisfy its own foreign key) already
# ran -- the whole transaction rolls back together against a real database;
# here, the absence of the audit statement is this fake's own proof that
# the code path never reaches it.
# ---------------------------------------------------------------------------


async def test_a_lost_compare_and_swap_refuses_and_never_reaches_the_audit_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    materialisation_fake = FakeMaterialisationQueries(cas_succeeds=False)
    monkeypatch.setattr(sub, "proposal_queries", proposal_fake)
    monkeypatch.setattr(sub, "materialisation_queries", materialisation_fake)
    proposal_fake.seed_version(_version_row(semantics=_candidate()))
    proposal_fake.seed_family(_family_row())
    factory = _RecordingSessionFactory()
    service = _service(factory, appender=object(), validator=object())

    with pytest.raises(ProposalStateConflict):
        await service.submit(_ctx(), _PROPOSAL_ID, _PROPOSAL_VERSION, expected_impact_envelope=object())

    assert len(materialisation_fake.inserted_drafts) == 1  # the insert that precedes the CAS did run
    assert len(materialisation_fake.freeze_calls) == 1  # and the CAS itself was attempted
    assert factory.sessions[0].executed == []  # but the audit event was never written


def test_draft_revision_is_immutable() -> None:
    """`DraftRevision` and `FrozenVersion` are frozen dataclasses: a caller
    holding one cannot mutate it out from under whatever already read it,
    matching every other row-shape dataclass in this package."""
    draft = DraftRevision(
        revision_id=uuid.uuid4(),
        artifact_id=_ARTIFACT_ID,
        tenant_id=None,
        source_system="s",
        source_canonical_locator="c",
        source_revision_locator="r",
        content_digest="0" * 64,
        effective_from=_NOW,
        review_expires_at=_NOW,
        detail_audience="registered_gateway_only",
        freshness_basis="revision_pinned_only",
        content_classification="internal",
        content_retention_until=_NOW,
        created_at=_NOW,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft.revision_id = uuid.uuid4()  # type: ignore[misc]
