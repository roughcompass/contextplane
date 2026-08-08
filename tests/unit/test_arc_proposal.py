"""Unit tests for `contextplane/arc/service/proposal.py`.

No database: `queries.proposal`'s functions are monkeypatched with an
in-memory fake that mimics the three tables' relational shape (one thread
per artifact, one nonterminal version per thread, compare-and-swap
transitions) closely enough to exercise the service's real branches --
authorization, the ADR 040 state machine, and the two `IntegrityError`
fallback paths. What a fake session cannot prove is that the database
constraints backing those same rules actually hold under a real race; that
proof is `tests/integration/test_arc_proposal_concurrency.py`.

Authorization tests use the real `ArcAuthorizationService`, not a mock of
it, matching `test_arc_source_admission.py`'s own convention: what is under
test here is `proposal.py`'s own scope-building, not a stand-in for the
chokepoint.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from contextplane.arc.service import proposal as p
from contextplane.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from contextplane.arc.service.queries.proposal import FamilyRow, ThreadRow, VersionRow
from contextplane.arc.types import ArcRequestContext
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"


class _FakeClock:
    def __init__(self, moment: datetime.datetime = _NOW) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
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
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        # `audit_outbox.emit` writes through this on every successful
        # create/open/transition; a real assertion on the outbox row is an
        # integration-level concern (a real `arc_audit_outbox` insert), not
        # this fake's job -- it only needs to not blow up when called.
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


class FakeQueries:
    """In-memory stand-in for `queries.proposal`'s module-level functions.

    Faithful to the two real invariants `proposal.py`'s own checks rely on
    the database to also enforce: `insert_family` raises `IntegrityError` on
    a (tenant, slug) collision, and `insert_version` raises it when a
    nonterminal version already exists for the thread -- mirroring the
    partial unique index the migration declares.
    """

    def __init__(self) -> None:
        self.families: dict[uuid.UUID, FamilyRow] = {}
        self.thread_by_artifact: dict[uuid.UUID, uuid.UUID] = {}
        self.threads: dict[uuid.UUID, ThreadRow] = {}
        self.versions: dict[tuple[uuid.UUID, int], VersionRow] = {}
        self.raise_integrity_on_next_insert_version = False

    # -- families -----------------------------------------------------------

    async def insert_family(self, _session: object, **kwargs: Any) -> None:
        scope_key = (kwargs["tenant_id"], kwargs["slug"])
        for existing in self.families.values():
            if (existing.tenant_id, existing.slug) == scope_key:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        row = FamilyRow(
            artifact_id=kwargs["artifact_id"],
            tenant_id=kwargs["tenant_id"],
            slug=kwargs["slug"],
            kind=kwargs["kind"],
            title=kwargs["title"],
            active_revision_id=None,
            created_at=kwargs["created_at"],
            created_by_issuer=kwargs["created_by_issuer"],
            created_by_subject=kwargs["created_by_subject"],
        )
        self.families[row.artifact_id] = row

    async def load_family(self, _session: object, artifact_id: uuid.UUID) -> FamilyRow | None:
        return self.families.get(artifact_id)

    async def load_family_for_update(self, _session: object, artifact_id: uuid.UUID) -> FamilyRow | None:
        return self.families.get(artifact_id)

    def seed_family(self, row: FamilyRow) -> None:
        self.families[row.artifact_id] = row

    # -- thread ----------------------------------------------------------------

    async def get_or_create_thread(
        self, _session: object, *, artifact_id: uuid.UUID, created_at: datetime.datetime
    ) -> uuid.UUID:
        existing = self.thread_by_artifact.get(artifact_id)
        if existing is not None:
            return existing
        proposal_id = uuid.uuid4()
        self.thread_by_artifact[artifact_id] = proposal_id
        self.threads[proposal_id] = ThreadRow(proposal_id=proposal_id, artifact_id=artifact_id, created_at=created_at)
        return proposal_id

    async def lock_thread(self, _session: object, _proposal_id: uuid.UUID) -> None:
        return None

    async def load_thread(self, _session: object, proposal_id: uuid.UUID) -> ThreadRow | None:
        return self.threads.get(proposal_id)

    # -- versions ----------------------------------------------------------------

    async def load_latest_version(self, _session: object, proposal_id: uuid.UUID) -> VersionRow | None:
        candidates = [v for v in self.versions.values() if v.proposal_id == proposal_id]
        if not candidates:
            return None
        return max(candidates, key=lambda v: v.proposal_version)

    async def load_version(self, _session: object, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow | None:
        return self.versions.get((proposal_id, proposal_version))

    async def list_versions_for_thread(self, _session: object, proposal_id: uuid.UUID) -> list[VersionRow]:
        rows = [v for v in self.versions.values() if v.proposal_id == proposal_id]
        return sorted(rows, key=lambda v: v.proposal_version)

    async def insert_version(self, _session: object, **kwargs: Any) -> None:
        if self.raise_integrity_on_next_insert_version:
            self.raise_integrity_on_next_insert_version = False
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        proposal_id = kwargs["proposal_id"]
        for existing in self.versions.values():
            if existing.proposal_id == proposal_id and existing.state in p.NONTERMINAL_STATES:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        row = VersionRow(
            proposal_id=proposal_id,
            proposal_version=kwargs["proposal_version"],
            artifact_id=kwargs["artifact_id"],
            tenant_id=kwargs["tenant_id"],
            state="open",
            source_evidence_id=kwargs["source_evidence_id"],
            reviewed_baseline_revision_id=kwargs["reviewed_baseline_revision_id"],
            revision_id=None,
            risk_classification=None,
            risk_algorithm_version=None,
            opened_by_issuer=kwargs["opened_by_issuer"],
            opened_by_subject=kwargs["opened_by_subject"],
            created_at=kwargs["created_at"],
            frozen_at=None,
            terminal_reason_code=None,
            terminal_note=None,
            terminal_by_issuer=None,
            terminal_by_subject=None,
            terminalized_at=None,
        )
        self.versions[(row.proposal_id, row.proposal_version)] = row

    def seed_version(self, row: VersionRow) -> None:
        self.versions[(row.proposal_id, row.proposal_version)] = row

    async def transition_version(
        self,
        _session: object,
        *,
        proposal_id: uuid.UUID,
        proposal_version: int,
        from_states: Sequence[str],
        to_state: str,
        reason_code: str,
        note: str | None,
        actor_issuer: str,
        actor_subject: str,
        now: datetime.datetime,
    ) -> VersionRow | None:
        current = self.versions.get((proposal_id, proposal_version))
        if current is None or current.state not in from_states:
            return None
        updated = dataclasses.replace(
            current,
            state=to_state,
            terminal_reason_code=reason_code,
            terminal_note=note,
            terminal_by_issuer=actor_issuer,
            terminal_by_subject=actor_subject,
            terminalized_at=now,
        )
        self.versions[(proposal_id, proposal_version)] = updated
        return updated

    async def list_versions(
        self,
        _session: object,
        *,
        tenant_id: uuid.UUID | None,
        artifact_id: uuid.UUID | None,
        state: str | None,
        cursor_created_at: datetime.datetime | None,
        cursor_proposal_id: uuid.UUID | None,
        cursor_proposal_version: int | None,
        page_size: int,
    ) -> list[VersionRow]:
        rows = list(self.versions.values())
        rows = [v for v in rows if v.tenant_id == tenant_id]
        if artifact_id is not None:
            rows = [v for v in rows if v.artifact_id == artifact_id]
        if state is not None:
            rows = [v for v in rows if v.state == state]
        rows.sort(key=lambda v: (v.created_at, v.proposal_id, v.proposal_version), reverse=True)
        if cursor_created_at is not None:
            key = (cursor_created_at, cursor_proposal_id, cursor_proposal_version)
            rows = [v for v in rows if (v.created_at, v.proposal_id, v.proposal_version) < key]
        return rows[:page_size]


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeQueries,
    *,
    global_operator: bool = True,
    clock: _FakeClock | None = None,
) -> p.ProposalService:
    """Patch `proposal.py`'s own `queries` reference to *fake*, scoped to
    the calling test by `monkeypatch`'s own teardown, then build a service
    against it. Every test using a `FakeQueries` goes through this, so the
    patch and the service it backs can never fall out of step."""
    monkeypatch.setattr(p, "queries", fake)
    allowlist = ((_ISSUER, _OPERATOR),) if global_operator else ()
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=allowlist)
    return p.ProposalService(
        lambda: _SessionCM(_NullSession()), authorization=authorization, clock=clock or _FakeClock()
    )


def _version_row(
    *,
    proposal_id: uuid.UUID,
    proposal_version: int = 1,
    artifact_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    state: str = "open",
    opened_by_subject: str = _OPERATOR,
    revision_id: uuid.UUID | None = None,
) -> VersionRow:
    return VersionRow(
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        state=state,
        source_evidence_id=uuid.uuid4(),
        reviewed_baseline_revision_id=None,
        revision_id=revision_id,
        risk_classification=None,
        risk_algorithm_version=None,
        opened_by_issuer=_ISSUER,
        opened_by_subject=opened_by_subject,
        created_at=_NOW,
        frozen_at=None,
        terminal_reason_code=None,
        terminal_note=None,
        terminal_by_issuer=None,
        terminal_by_subject=None,
        terminalized_at=None,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_allowed_transitions_cover_every_state_and_match_adr_040() -> None:
    """Every one of the eight `ProposalState` literals has a table entry,
    and the five terminal states have no outward transition."""
    for state in ("open", "submitted", "approved", "activated", "rejected", "stale", "superseded", "withdrawn"):
        assert state in p._ALLOWED_TRANSITIONS
    assert p._ALLOWED_TRANSITIONS["open"] == ("submitted", "withdrawn")
    assert p._ALLOWED_TRANSITIONS["submitted"] == ("approved", "rejected", "stale", "superseded")
    assert p._ALLOWED_TRANSITIONS["approved"] == ("activated", "stale", "superseded")
    for terminal in ("activated", "rejected", "stale", "superseded", "withdrawn"):
        assert p._ALLOWED_TRANSITIONS[terminal] == ()


def test_owning_scope_derives_from_tenant_id_nullability() -> None:
    assert p._owning_scope(None) == "global"
    assert p._owning_scope(uuid.uuid4()) == "tenant"


def test_operational_integrity_state_unavailable_before_a_revision_exists() -> None:
    assert p._operational_integrity_state(None) == "unavailable"
    assert p._operational_integrity_state(uuid.uuid4()) == "pending"


def test_cursor_round_trips_and_rejects_malformed_input() -> None:
    row = _version_row(proposal_id=uuid.uuid4(), artifact_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    cursor = p._encode_cursor(row)
    created_at, proposal_id, proposal_version = p._decode_cursor(cursor)
    assert (created_at, proposal_id, proposal_version) == (row.created_at, row.proposal_id, row.proposal_version)

    with pytest.raises(RegistryError):
        p._decode_cursor("not-a-real-cursor")


def test_require_reason_accepts_valid_and_rejects_empty_or_overlong() -> None:
    p._require_reason("policy_violation", "a short note")  # does not raise
    with pytest.raises(RegistryError):
        p._require_reason("", None)
    with pytest.raises(RegistryError):
        p._require_reason("policy_violation", "x" * 2001)


# ---------------------------------------------------------------------------
# create_family
# ---------------------------------------------------------------------------


async def test_create_family_global_requires_the_operator_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = _build_service(monkeypatch, FakeQueries(), global_operator=True)
    family = await allowed.create_family(
        _ctx(), slug="directive-a", kind="policy", owning_scope="global", target_tenant_id=None, title="A"
    )
    assert family.owning_scope == "global"
    assert family.target_tenant_id is None

    refused = _build_service(monkeypatch, FakeQueries(), global_operator=False)
    with pytest.raises(ArcAuthorizationError):
        await refused.create_family(
            _ctx(), slug="directive-b", kind="policy", owning_scope="global", target_tenant_id=None, title="B"
        )


async def test_create_family_tenant_requires_admin_role_in_the_same_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    service = _build_service(monkeypatch, fake)
    tenant_id = uuid.uuid4()

    admin_ctx = _ctx(tenant_id=tenant_id, roles=["admin"])
    family = await service.create_family(
        admin_ctx, slug="runbook-a", kind="runbook", owning_scope="tenant", target_tenant_id=tenant_id, title="R"
    )
    assert family.owning_scope == "tenant"
    assert family.target_tenant_id == tenant_id

    non_admin_ctx = _ctx(tenant_id=tenant_id, roles=["member"])
    with pytest.raises(ArcAuthorizationError):
        await service.create_family(
            non_admin_ctx,
            slug="runbook-b",
            kind="runbook",
            owning_scope="tenant",
            target_tenant_id=tenant_id,
            title="R2",
        )


async def test_create_family_slug_collision_is_a_conflict_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _build_service(monkeypatch, FakeQueries())
    ctx = _ctx()
    await service.create_family(ctx, slug="dup", kind="policy", owning_scope="global", target_tenant_id=None, title="T")
    with pytest.raises(ConflictError):
        await service.create_family(
            ctx, slug="dup", kind="policy", owning_scope="global", target_tenant_id=None, title="T2"
        )


# ---------------------------------------------------------------------------
# open_proposal
# ---------------------------------------------------------------------------


async def test_open_proposal_unknown_family_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _build_service(monkeypatch, FakeQueries())
    with pytest.raises(NotFoundError):
        await service.open_proposal(_ctx(), artifact_id=uuid.uuid4(), source_evidence_id=uuid.uuid4())


async def test_open_proposal_defaults_baseline_to_active_revision_unless_given(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    active_revision_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=active_revision_id,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    service = _build_service(monkeypatch, fake)

    defaulted = await service.open_proposal(_ctx(), artifact_id=artifact_id, source_evidence_id=uuid.uuid4())
    assert defaulted.reviewed_baseline_revision_id == active_revision_id

    # A second thread (different artifact) proves an explicit baseline
    # overrides the default rather than the default always winning.
    artifact_id_2 = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id_2,
            tenant_id=None,
            slug="s2",
            kind="policy",
            title="T2",
            active_revision_id=active_revision_id,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    explicit_baseline = uuid.uuid4()
    overridden = await service.open_proposal(
        _ctx(),
        artifact_id=artifact_id_2,
        source_evidence_id=uuid.uuid4(),
        reviewed_baseline_revision_id=explicit_baseline,
    )
    assert overridden.reviewed_baseline_revision_id == explicit_baseline


async def test_open_proposal_second_version_only_after_first_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    service = _build_service(monkeypatch, fake)
    ctx = _ctx()

    first = await service.open_proposal(ctx, artifact_id=artifact_id, source_evidence_id=uuid.uuid4())
    assert first.proposal_version == 1

    # Still open: a second open on the same artifact is rejected.
    with pytest.raises(p.ProposalStateConflict):
        await service.open_proposal(ctx, artifact_id=artifact_id, source_evidence_id=uuid.uuid4())

    # Once terminal, a new version is legal and increments.
    await service.withdraw(ctx, first.proposal_id, 1, reason_code="changed_my_mind")
    second = await service.open_proposal(ctx, artifact_id=artifact_id, source_evidence_id=uuid.uuid4())
    assert second.proposal_version == 2
    assert second.proposal_id == first.proposal_id


async def test_open_proposal_resolves_integrity_error_race_to_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The partial-unique-index backstop: even if the application-level
    check above it were somehow bypassed, the `IntegrityError` path still
    resolves to the same refusal rather than an unhandled crash."""
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    service = _build_service(monkeypatch, fake)
    fake.raise_integrity_on_next_insert_version = True
    with pytest.raises(p.ProposalStateConflict):
        await service.open_proposal(_ctx(), artifact_id=artifact_id, source_evidence_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# withdraw / reject / supersede
# ---------------------------------------------------------------------------


async def test_withdraw_allowed_for_submitter_and_refused_for_a_different_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tenant-scoped, with both actors holding admin in the same tenant: the
    # only thing that can distinguish the two calls below is the submitter
    # check, not general write authority -- a global-scope family would
    # have let the operator-allowlist check confound the result.
    fake = FakeQueries()
    proposal_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
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
    )
    fake.seed_version(
        _version_row(
            proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, opened_by_subject="submitter-a"
        )
    )
    service = _build_service(monkeypatch, fake)

    other_ctx = _ctx(tenant_id=tenant_id, subject="someone-else", roles=["admin"])
    with pytest.raises(ArcAuthorizationError):
        await service.withdraw(other_ctx, proposal_id, 1, reason_code="not_needed")

    submitter_ctx = _ctx(tenant_id=tenant_id, subject="submitter-a", roles=["admin"])
    withdrawn = await service.withdraw(submitter_ctx, proposal_id, 1, reason_code="not_needed")
    assert withdrawn.state == "withdrawn"


async def test_withdraw_refuses_a_version_that_is_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    proposal_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    fake.seed_version(_version_row(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=None, state="submitted"))
    service = _build_service(monkeypatch, fake)
    with pytest.raises(p.ProposalStateConflict):
        await service.withdraw(_ctx(), proposal_id, 1, reason_code="not_needed")


async def test_reject_only_legal_from_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    proposal_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    fake.seed_version(_version_row(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=None, state="submitted"))
    service = _build_service(monkeypatch, fake)

    rejected = await service.reject(_ctx(), proposal_id, 1, reason_code="quality")
    assert rejected.state == "rejected"

    # Rejected is terminal: a second reject attempt is a conflict, proving
    # the CAS actually checks the current state rather than always succeeding.
    with pytest.raises(p.ProposalStateConflict):
        await service.reject(_ctx(), proposal_id, 1, reason_code="quality")


async def test_supersede_legal_from_submitted_or_approved_but_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    service = _build_service(monkeypatch, fake)

    approved_id = uuid.uuid4()
    fake.seed_version(_version_row(proposal_id=approved_id, artifact_id=artifact_id, tenant_id=None, state="approved"))
    superseded = await service.supersede(_ctx(), approved_id, 1, reason_code="abandoned")
    assert superseded.state == "superseded"

    open_id = uuid.uuid4()
    fake.seed_version(_version_row(proposal_id=open_id, artifact_id=artifact_id, tenant_id=None, state="open"))
    with pytest.raises(p.ProposalStateConflict):
        await service.supersede(_ctx(), open_id, 1, reason_code="abandoned")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_get_family_not_found_and_found_but_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    service = _build_service(monkeypatch, fake)
    with pytest.raises(NotFoundError):
        await service.get_family(_ctx(), uuid.uuid4())

    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
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
    )
    other_tenant_ctx = _ctx(tenant_id=uuid.uuid4())
    with pytest.raises(ArcAuthorizationError):
        await service.get_family(other_tenant_ctx, artifact_id)

    same_tenant_ctx = _ctx(tenant_id=tenant_id)
    found = await service.get_family(same_tenant_ctx, artifact_id)
    assert found.artifact_id == artifact_id


async def test_get_version_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _build_service(monkeypatch, FakeQueries())
    with pytest.raises(NotFoundError):
        await service.get_version(_ctx(), uuid.uuid4(), 1)


async def test_version_result_hides_available_actions_from_a_reader_without_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueries()
    tenant_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
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
    )
    fake.seed_version(_version_row(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=tenant_id, state="open"))
    service = _build_service(monkeypatch, fake)

    admin_ctx = _ctx(tenant_id=tenant_id, roles=["admin"])
    with_write = await service.get_version(admin_ctx, proposal_id, 1)
    # edit/validate/run_semantic_tests joined withdraw for `open` once
    # provenance.py/semantic_tests.py gave those three a real route -- see
    # `_AVAILABLE_ACTIONS`'s own comment on why this table is edited from
    # outside proposal.py.
    assert with_write.available_actions == ("edit", "validate", "run_semantic_tests", "withdraw")

    member_ctx = _ctx(tenant_id=tenant_id, roles=["member"])
    without_write = await service.get_version(member_ctx, proposal_id, 1)
    assert without_write.available_actions == ()


async def test_get_thread_orders_versions_and_reports_the_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    fake.seed_family(
        FamilyRow(
            artifact_id=artifact_id,
            tenant_id=None,
            slug="s",
            kind="policy",
            title="T",
            active_revision_id=None,
            created_at=_NOW,
            created_by_issuer=_ISSUER,
            created_by_subject=_OPERATOR,
        )
    )
    fake.threads[proposal_id] = ThreadRow(proposal_id=proposal_id, artifact_id=artifact_id, created_at=_NOW)
    fake.seed_version(
        _version_row(
            proposal_id=proposal_id, proposal_version=1, artifact_id=artifact_id, tenant_id=None, state="rejected"
        )
    )
    fake.seed_version(
        _version_row(proposal_id=proposal_id, proposal_version=2, artifact_id=artifact_id, tenant_id=None, state="open")
    )
    service = _build_service(monkeypatch, fake)

    thread = await service.get_thread(_ctx(), proposal_id)
    assert thread.latest_version == 2
    assert [v.proposal_version for v in thread.versions] == [1, 2]


async def test_list_proposals_reports_a_next_cursor_only_on_a_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueries()
    artifact_id = uuid.uuid4()
    for _i in range(3):
        proposal_id = uuid.uuid4()
        fake.seed_version(
            _version_row(proposal_id=proposal_id, artifact_id=artifact_id, tenant_id=None, state="rejected")
        )
    service = _build_service(monkeypatch, fake)

    full_page = await service.list_proposals(_ctx(), None, page_size=2)
    assert len(full_page.items) == 2
    assert full_page.next_cursor is not None

    partial_page = await service.list_proposals(_ctx(), None, page_size=10)
    assert len(partial_page.items) == 3
    assert partial_page.next_cursor is None
