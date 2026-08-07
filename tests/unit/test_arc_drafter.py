"""Unit tests for `registry/arc/service/drafter.py` and the pure decision
function in `registry/arc/sandbox/drafter_main.py`.

No database and no real sandbox process, matching `test_arc_provenance.py`'s
and `test_arc_semantic_tests.py`'s own convention: `proposal_queries` is
monkeypatched with an in-memory fake, and the real two-subprocess pipeline
is replaced with an injected fake so these tests exercise `DrafterService`'s
own branching, not process-spawning latency. The real pipeline against real
subprocesses is proven separately by `tests/conformance/test_arc_drafter_sandbox.py`
and `tests/integration/test_arc_drafting.py`.

Three things this file exists to prove, per the task's own contract:

1. **The disabled path costs nothing and touches nothing.** With the model
   flag off (the committed default), `draft()` never opens a database
   session, never calls the decision loader, and never touches the sandbox
   pipeline -- proven by fakes that raise `AssertionError` if invoked at
   all, not merely by asserting a return value they might not have been
   reached to produce. With the flag on against a `human_only` (or any
   non-`accepted`) decision, the same refusal fires, this time *after* the
   decision loader ran but still before any session opens.
2. **The disabled check is not vacuous.** A genuinely enabled + accepted
   decision does not raise `DrafterModelDisabled` -- it proceeds to the
   proposal lookup, so the guard discriminates rather than always
   refusing.
3. **`draft_from_envelope` discriminates.** Two different `target_field_paths`
   inputs produce two different `declined_field_paths` outputs (not a
   hardcoded constant), and a content/envelope digest mismatch -- either
   direction -- is refused rather than silently drafted from.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from registry.arc.sandbox.drafter_main import draft_from_envelope
from registry.arc.schemas import drafter_output as do
from registry.arc.schemas.parser_output import ParsedSourceEnvelope
from registry.arc.service import drafter as d
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries.drafter import ReachConfirmationRow
from registry.arc.service.queries.proposal import FamilyRow, VersionRow
from registry.arc.service.source_admission import SourceEvidence
from registry.arc.service.source_status import SourceStatusUnavailable
from registry.arc.types import ArcRequestContext
from registry.config import Settings
from registry.exceptions import NotFoundError
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"


# ---------------------------------------------------------------------------
# `draft_from_envelope` -- pure, no fakes needed.
# ---------------------------------------------------------------------------


def _envelope(*, source_content_digest: str) -> ParsedSourceEnvelope:
    return ParsedSourceEnvelope(
        profile="arc_parsed_source_envelope_v1",
        source_evidence_id=uuid.uuid4(),
        source_content_digest=source_content_digest,
        media_type="text/markdown",
        parser_id="test-parser",
        parser_version="1",
        title=None,
        sections=[],
        warnings=[],
    )


def test_draft_from_envelope_declines_exactly_the_requested_paths_deduplicated() -> None:
    content = b"hello world"
    digest = hashlib.sha256(content).hexdigest()
    envelope = _envelope(source_content_digest=digest)

    result_a = draft_from_envelope(
        content, envelope, source_content_digest=digest, target_field_paths=["directives", "applicability"]
    )
    result_b = draft_from_envelope(content, envelope, source_content_digest=digest, target_field_paths=["other"])

    assert isinstance(result_a, do.DrafterSuccess)
    assert isinstance(result_b, do.DrafterSuccess)
    # Discrimination: two different inputs produce two different outputs,
    # not a hardcoded constant.
    assert result_a.declined_field_paths == ["directives", "applicability"]
    assert result_b.declined_field_paths == ["other"]
    assert result_a.patch == {}
    assert result_a.citations == []


def test_draft_from_envelope_deduplicates_repeated_field_paths() -> None:
    content = b"hello world"
    digest = hashlib.sha256(content).hexdigest()
    envelope = _envelope(source_content_digest=digest)

    result = draft_from_envelope(content, envelope, source_content_digest=digest, target_field_paths=["a", "b", "a"])

    assert isinstance(result, do.DrafterSuccess)
    assert result.declined_field_paths == ["a", "b"]


def test_draft_from_envelope_refuses_on_actual_content_digest_mismatch() -> None:
    """The sandbox's own defense-in-depth check: even if the API-side
    caller's binding check somehow missed it, drafting from content that
    does not hash to the asserted digest is refused, not attempted."""
    content = b"hello world"
    real_digest = hashlib.sha256(content).hexdigest()
    tampered_digest = hashlib.sha256(b"something else entirely").hexdigest()
    envelope = _envelope(source_content_digest=real_digest)

    result = draft_from_envelope(content, envelope, source_content_digest=tampered_digest, target_field_paths=["a"])

    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "envelope_binding_mismatch"


def test_draft_from_envelope_refuses_on_envelope_declared_digest_mismatch() -> None:
    """The other direction of the same check: content matches the asserted
    digest, but the envelope itself declares a different one."""
    content = b"hello world"
    real_digest = hashlib.sha256(content).hexdigest()
    envelope = _envelope(source_content_digest=hashlib.sha256(b"a different document").hexdigest())

    result = draft_from_envelope(content, envelope, source_content_digest=real_digest, target_field_paths=["a"])

    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "envelope_binding_mismatch"


def test_draft_from_envelope_refuses_over_the_field_path_ceiling() -> None:
    content = b"x"
    digest = hashlib.sha256(content).hexdigest()
    envelope = _envelope(source_content_digest=digest)
    too_many = [f"field-{i}" for i in range(do.MAX_DECLINED_FIELD_PATHS + 1)]

    result = draft_from_envelope(content, envelope, source_content_digest=digest, target_field_paths=too_many)

    assert isinstance(result, do.DrafterRefusal)
    assert result.refusal_code == "output_limit_exceeded"


# ---------------------------------------------------------------------------
# `DrafterService` -- fakes for everything it collaborates with.
# ---------------------------------------------------------------------------


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


def _refusing_session_factory() -> object:
    raise AssertionError("a database session was opened while the model is disabled")


class FakeProposalQueries:
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


class FakeReachConfirmationQueries:
    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, int, str], Any] = {}

    async def upsert_reach_confirmation(self, _session: object, **kwargs: Any) -> None:
        key = (kwargs["proposal_id"], kwargs["proposal_version"], kwargs["field_path"])
        self.rows[key] = kwargs

    async def load_reach_confirmations_for_paths(
        self, _session: object, proposal_id: uuid.UUID, proposal_version: int, field_paths: list[str]
    ) -> list[ReachConfirmationRow]:
        out = []
        for field_path in field_paths:
            row = self.rows.get((proposal_id, proposal_version, field_path))
            if row is None:
                continue
            out.append(
                ReachConfirmationRow(
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    field_path=field_path,
                    confirmed=row["confirmed"],
                    confirmed_at=row["confirmed_at"],
                    confirmed_by_issuer=row["confirmed_by_issuer"],
                    confirmed_by_subject=row["confirmed_by_subject"],
                )
            )
        return sorted(out, key=lambda r: r.field_path)

    async def load_reach_confirmations(
        self, _session: object, proposal_id: uuid.UUID, proposal_version: int
    ) -> list[ReachConfirmationRow]:
        paths = [k[2] for k in self.rows if k[0] == proposal_id and k[1] == proposal_version]
        return await self.load_reach_confirmations_for_paths(_session, proposal_id, proposal_version, paths)


class _RefusingSourceStatus:
    async def check_status(self, _source_evidence_id: uuid.UUID) -> None:
        raise AssertionError("source status was checked while target_field_paths was empty / model disabled")


class _RefusingSourceAdmission:
    async def get_evidence(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("source evidence was read while target_field_paths was empty / model disabled")

    async def get_body(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("source body was read while target_field_paths was empty / model disabled")


class _FakeSourceStatus:
    async def check_status(self, _source_evidence_id: uuid.UUID) -> None:
        return None


def _evidence(source_evidence_id: uuid.UUID) -> SourceEvidence:
    return SourceEvidence(
        source_evidence_id=source_evidence_id,
        source_system="s",
        source_revision_locator="l",
        source_content_digest="0" * 64,
        source_content_type="text/markdown",
        source_content_bytes=1,
        admission_method="authorized_upload",
        connector_id=None,
        policy_id="p",
        verification_method="detached_signature",
        verifier_id="v",
        admitted_at=_NOW,
        verified_at=_NOW,
        expires_at=None,
        status="current",
        status_checked_at=_NOW,
        next_check_at=_NOW,
    )


class _FakeSourceAdmission:
    def __init__(self, source_evidence_id: uuid.UUID) -> None:
        self._evidence = _evidence(source_evidence_id)

    async def get_evidence(self, _ctx: object, _source_evidence_id: uuid.UUID) -> SourceEvidence:
        return self._evidence

    async def get_body(self, _ctx: object, _source_evidence_id: uuid.UUID) -> tuple[bytes, str]:
        return b"content", "text/markdown"


def _refusing_decision_loader() -> dict[str, Any]:
    raise AssertionError("the decision artifact was read while the model flag is disabled")


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proposal_fake: FakeProposalQueries,
    reach_fake: FakeReachConfirmationQueries,
    enabled: bool,
    decision_loader: Any = None,
    source_status: Any = None,
    source_admission: Any = None,
    sandbox_pipeline: Any = None,
) -> d.DrafterService:
    monkeypatch.setattr(d, "proposal_queries", proposal_fake)
    monkeypatch.setattr(d, "queries", reach_fake)
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    settings = Settings(database_url="postgresql+asyncpg://unused/unused", arc_drafter_model_enabled=enabled)
    return d.DrafterService(
        lambda: _SessionCM(_NullSession()),
        authorization=authorization,
        source_admission=source_admission or _RefusingSourceAdmission(),
        source_status=source_status or _RefusingSourceStatus(),
        clock=_FakeClock(),
        settings=settings,
        decision_loader=decision_loader or _refusing_decision_loader,
        sandbox_pipeline=sandbox_pipeline,
    )


# ---------------------------------------------------------------------------
# 1. Disabled path costs nothing and touches nothing.
# ---------------------------------------------------------------------------


async def test_draft_disabled_by_default_never_reads_decision_or_opens_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    monkeypatch.setattr(d, "proposal_queries", proposal_fake)
    monkeypatch.setattr(d, "queries", reach_fake)
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    settings = Settings(database_url="postgresql+asyncpg://unused/unused")
    assert settings.arc_drafter_model_enabled is False

    service = d.DrafterService(
        _refusing_session_factory,
        authorization=authorization,
        source_admission=_RefusingSourceAdmission(),
        source_status=_RefusingSourceStatus(),
        clock=_FakeClock(),
        settings=settings,
        decision_loader=_refusing_decision_loader,
    )

    with pytest.raises(d.DrafterModelDisabled):
        await service.draft(_ctx(), uuid.uuid4(), 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"])


async def test_draft_enabled_against_human_only_decision_refuses_after_reading_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "human_only"},
        source_admission=_RefusingSourceAdmission(),
        source_status=_RefusingSourceStatus(),
    )
    # No proposal seeded at all -- if the disabled check did not fire before
    # the proposal lookup, this would raise NotFoundError instead.
    with pytest.raises(d.DrafterModelDisabled, match="not 'accepted'"):
        await service.draft(_ctx(), uuid.uuid4(), 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"])


# ---------------------------------------------------------------------------
# 2. Not vacuous: enabled + accepted proceeds past the guard.
# ---------------------------------------------------------------------------


async def test_draft_enabled_and_accepted_proceeds_to_the_proposal_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
    )
    proposal_id = uuid.uuid4()
    # No proposal seeded -- a guard that fired would raise DrafterModelDisabled;
    # a guard that did not fire proceeds to the real lookup and raises
    # NotFoundError instead, proving the disabled check discriminates.
    with pytest.raises(NotFoundError):
        await service.draft(_ctx(), proposal_id, 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"])


async def test_draft_refuses_when_caller_lacks_write_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
    )
    other_tenant_ctx = _ctx(tenant_id=uuid.uuid4(), roles=["admin"])
    with pytest.raises(ArcAuthorizationError):
        await service.draft(other_tenant_ctx, proposal_id, 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"])


async def test_draft_refuses_when_version_is_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="submitted")
    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
    )
    with pytest.raises(ProposalStateConflict):
        await service.draft(
            _ctx(tenant_id=tenant_id), proposal_id, 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"]
        )


async def test_draft_with_no_target_field_paths_declines_everything_without_touching_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
        source_admission=_RefusingSourceAdmission(),
        source_status=_RefusingSourceStatus(),
    )
    result = await service.draft(
        _ctx(tenant_id=tenant_id), proposal_id, 1, source_evidence_id=uuid.uuid4(), target_field_paths=[]
    )
    assert result.patch == {}
    assert result.citations == ()
    assert result.declined_field_paths == ()


async def test_draft_happy_path_calls_source_status_then_sandbox_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    source_evidence_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)

    calls: list[tuple[Any, ...]] = []

    async def fake_pipeline(
        content: bytes, media_type: str, sid: uuid.UUID, digest: str, target_field_paths: Sequence[str]
    ) -> tuple[dict[str, Any], tuple[d.CitationRecord, ...], tuple[str, ...]]:
        calls.append((content, media_type, sid, digest, tuple(target_field_paths)))
        citation = d.CitationRecord(
            field_path="directives", source_evidence_id=sid, source_anchor="a1", excerpt_digest="0" * 64
        )
        return {}, (citation,), ()

    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
        source_status=_FakeSourceStatus(),
        source_admission=_FakeSourceAdmission(source_evidence_id),
        sandbox_pipeline=fake_pipeline,
    )

    result = await service.draft(
        _ctx(tenant_id=tenant_id),
        proposal_id,
        1,
        source_evidence_id=source_evidence_id,
        target_field_paths=["directives"],
    )

    assert len(calls) == 1
    assert calls[0][4] == ("directives",)
    assert result.citations == (
        d.CitationRecord(
            field_path="directives", source_evidence_id=source_evidence_id, source_anchor="a1", excerpt_digest="0" * 64
        ),
    )
    assert result.declined_field_paths == ()


async def test_draft_propagates_source_status_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)

    class _RevokedSourceStatus:
        async def check_status(self, _source_evidence_id: uuid.UUID) -> None:
            raise SourceStatusUnavailable("source is revoked")

    service = _build_service(
        monkeypatch,
        proposal_fake=proposal_fake,
        reach_fake=reach_fake,
        enabled=True,
        decision_loader=lambda: {"outcome": "accepted"},
        source_status=_RevokedSourceStatus(),
        source_admission=_RefusingSourceAdmission(),
    )
    with pytest.raises(SourceStatusUnavailable):
        await service.draft(
            _ctx(tenant_id=tenant_id), proposal_id, 1, source_evidence_id=uuid.uuid4(), target_field_paths=["a"]
        )


# ---------------------------------------------------------------------------
# `confirm_reach` / `list_reach_confirmations` -- the human structured form's
# own persisted state, independent of the drafter/model entirely.
# ---------------------------------------------------------------------------


async def test_confirm_reach_persists_and_returns_confirmed_records(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(monkeypatch, proposal_fake=proposal_fake, reach_fake=reach_fake, enabled=False)

    records = await service.confirm_reach(
        _ctx(tenant_id=tenant_id), proposal_id, 1, field_paths=["directives", "applicability"]
    )

    assert {r.field_path for r in records} == {"directives", "applicability"}
    assert all(r.confirmed for r in records)
    assert all(r.confirmed_at == _NOW for r in records)
    assert all(r.confirmed_by_issuer == _ISSUER and r.confirmed_by_subject == _OPERATOR for r in records)


async def test_confirm_reach_deduplicates_repeated_field_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(monkeypatch, proposal_fake=proposal_fake, reach_fake=reach_fake, enabled=False)

    records = await service.confirm_reach(
        _ctx(tenant_id=tenant_id), proposal_id, 1, field_paths=["directives", "directives"]
    )

    assert len(records) == 1


async def test_confirm_reach_does_not_disturb_a_sibling_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-field upsert, matching `provenance.py::edit`'s own invariant:
    confirming field B must not touch field A's already-recorded row."""
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(monkeypatch, proposal_fake=proposal_fake, reach_fake=reach_fake, enabled=False)

    first = await service.confirm_reach(_ctx(tenant_id=tenant_id), proposal_id, 1, field_paths=["directives"])
    second = await service.confirm_reach(_ctx(tenant_id=tenant_id), proposal_id, 1, field_paths=["applicability"])

    assert first[0].confirmed_at == _NOW
    all_confirmations = await service.list_reach_confirmations(_ctx(tenant_id=tenant_id), proposal_id, 1)
    assert {c.field_path for c in all_confirmations} == {"directives", "applicability"}
    assert second[0].field_path == "applicability"


async def test_confirm_reach_refuses_when_version_is_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="submitted")
    service = _build_service(monkeypatch, proposal_fake=proposal_fake, reach_fake=reach_fake, enabled=False)
    with pytest.raises(ProposalStateConflict):
        await service.confirm_reach(_ctx(tenant_id=tenant_id), proposal_id, 1, field_paths=["directives"])


async def test_confirm_reach_refuses_when_caller_lacks_write_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal_fake = FakeProposalQueries()
    reach_fake = FakeReachConfirmationQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    proposal_fake.seed(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id)
    service = _build_service(monkeypatch, proposal_fake=proposal_fake, reach_fake=reach_fake, enabled=False)
    with pytest.raises(ArcAuthorizationError):
        await service.confirm_reach(_ctx(tenant_id=uuid.uuid4()), proposal_id, 1, field_paths=["directives"])


def test_sandbox_subprocesses_receive_no_credential_from_the_parent_environment() -> None:
    """The sandbox's claim is that it holds no credential. `Popen` inherits
    the parent's whole environment by default, so without an explicit
    allowlist the API's `DATABASE_URL` and OIDC secrets reach both sandbox
    children -- harmless only while neither module happens to read them,
    which is not a boundary.

    Asserts the environment is exactly the allowlist, not merely that a
    couple of known-bad names are absent: a deny-list drifts the moment a
    new secret is added, and this is the check that would otherwise miss it.
    """
    import os as _os

    from registry.arc.service import drafter as _drafter

    poisoned = {
        "DATABASE_URL": "postgresql://user:password@host/db",
        "OIDC_CLIENT_SECRET": "super-secret",
        "REGISTRY_ADMIN_TOKEN": "admin-token",
    }
    previous = {k: _os.environ.get(k) for k in poisoned}
    _os.environ.update(poisoned)
    try:
        env = _drafter._sandbox_env()
    finally:
        for k, v in previous.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    assert set(env) == {"PATH", "HOME"}, f"sandbox env must be exactly the allowlist, got {sorted(env)}"
    for name in poisoned:
        assert name not in env
