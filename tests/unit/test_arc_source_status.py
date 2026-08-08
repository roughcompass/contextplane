"""Unit tests for `source_status.py` and `source_status_refresh.py`.

No database: the handful of `queries.source_admission` functions these two
modules call (`load_status`, `load_evidence`, `select_due_for_refresh`,
`update_status_refresh`) are monkeypatched with an in-memory fake faithful
enough to the real relational shape -- a due-set filter on `next_check_at`,
a compare-and-swap guard on the refresh write -- to exercise every real
branch without Postgres. What a fake cannot prove (the guard actually
serializes two concurrent refreshes, the schema's own constraints hold) is
`tests/integration/test_arc_source_status.py`'s job.

Every deadline/freshness assertion below drives a `FakeClock`-like double
rather than sleeping: a test that sleeps to observe a five-minute window is
either slow or wrong, and this suite needs to look at both sides of a
boundary in the same test.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

import pytest

from contextplane.arc.service import source_status as ss
from contextplane.arc.service.operational_chain import AppendResult
from contextplane.arc.service.queries.source_admission import DependentRevision, EvidenceRow, StatusRow
from contextplane.arc.workers import source_status_refresh as wr
from contextplane.exceptions import NotFoundError

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_EVIDENCE_ID = uuid.uuid4()


class _FakeClock:
    def __init__(self, moment: datetime.datetime) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment

    def set(self, moment: datetime.datetime) -> None:
        self._moment = moment


def _status_row(**overrides: Any) -> StatusRow:
    base: dict[str, Any] = {
        "source_evidence_id": _EVIDENCE_ID,
        "status": ss.STATUS_CURRENT,
        "checked_at": _NOW,
        "next_check_at": _NOW + ss.FRESHNESS_WINDOW,
        "status_source": "admission",
        "status_evidence_digest": None,
    }
    base.update(overrides)
    return StatusRow(**base)


def _evidence_row(**overrides: Any) -> EvidenceRow:
    base: dict[str, Any] = {
        "source_evidence_id": _EVIDENCE_ID,
        "owning_scope": "global",
        "tenant_id": None,
        "source_system": "confluence",
        "source_revision_locator": "conf://space/page@3",
        "source_content_type": "text/markdown",
        "source_content_digest": "0" * 64,
        "claim": {},
        "claim_digest": "0" * 64,
        "verification_method": "source_signed",
        "verifier_id": "verifier-1",
        "signature": None,
        "verifier_attestation": None,
        "admission_method": "authorized_upload",
        "connector_id": None,
        "policy_id": "policy-1",
        "admitted_at": _NOW,
        "admitted_by_issuer": "https://idp.example.test",
        "admitted_by_subject": "operator",
        "verified_at": _NOW,
        "expires_at": _NOW + datetime.timedelta(days=365),
        "idempotency_key_digest": "0" * 64,
        "admission_request_payload_digest": "0" * 64,
        "idempotency_scope_digest": "0" * 64,
    }
    base.update(overrides)
    return EvidenceRow(**base)


# ---------------------------------------------------------------------------
# Session-factory doubles
# ---------------------------------------------------------------------------


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    """Records every executed statement's SQL text, matching
    `test_arc_materialisation.py`'s own convention -- a test can then assert
    whether an audit-outbox write was reached without needing a real
    database, since `audit_outbox.emit`/`emit_global` are not monkeypatched
    away, only the `queries.source_admission` calls around them are."""

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
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory() -> _SessionCM:
    return _SessionCM(_NullSession())


class _RecordingSessionFactory:
    """Like `_session_factory`, but keeps every session it created so a
    test can inspect `.executed` afterward -- needed only by the
    revocation/expiry cascade tests below, which care whether an audit
    write actually happened."""

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
# In-memory fake for the four queries functions these modules call
# ---------------------------------------------------------------------------


class FakeStatusQueries:
    def __init__(self) -> None:
        self.status: dict[uuid.UUID, StatusRow] = {}
        self.evidence: dict[uuid.UUID, EvidenceRow] = {}
        # Counts every call, whether or not the compare-and-swap guard
        # actually applied it -- a test proving "touched nothing" asserts
        # this is zero, which is stronger than asserting the row's value
        # happens to be unchanged (a call that overwrote it with identical
        # values would pass a value check but not this one).
        self.update_calls = 0
        # The revocation/expiry cascade's own state: which revisions a
        # source's dependents are, and what lifecycle_state each one is
        # currently in.
        self.dependents: dict[uuid.UUID, list[DependentRevision]] = {}
        self.lifecycle_state: dict[uuid.UUID, str] = {}
        self.mark_terminal_calls: list[dict[str, object]] = []
        self.revoke_or_expire_calls: list[dict[str, object]] = []

    def seed(self, *, status: StatusRow | None = None, evidence: EvidenceRow | None = None) -> None:
        if status is not None:
            self.status[status.source_evidence_id] = status
        if evidence is not None:
            self.evidence[evidence.source_evidence_id] = evidence

    def seed_dependents(self, source_evidence_id: uuid.UUID, dependents: list[DependentRevision]) -> None:
        self.dependents[source_evidence_id] = dependents
        for dependent in dependents:
            self.lifecycle_state[dependent.revision_id] = "active"

    async def mark_status_terminal(
        self,
        _session: object,
        *,
        source_evidence_id: uuid.UUID,
        status: str,
        checked_at: datetime.datetime,
        next_check_at: datetime.datetime,
    ) -> bool:
        self.mark_terminal_calls.append({"source_evidence_id": source_evidence_id, "status": status})
        row = self.status.get(source_evidence_id)
        if row is None or row.status in (ss.STATUS_REVOKED, ss.STATUS_EXPIRED):
            return False
        self.status[source_evidence_id] = dataclasses.replace(
            row, status=status, checked_at=checked_at, next_check_at=next_check_at
        )
        return True

    async def find_active_revisions_by_source(
        self, _session: object, source_evidence_id: uuid.UUID
    ) -> list[DependentRevision]:
        return list(self.dependents.get(source_evidence_id, []))

    async def revoke_or_expire_revision(
        self, _session: object, *, revision_id: uuid.UUID, lifecycle_state: str, now: datetime.datetime
    ) -> None:
        self.revoke_or_expire_calls.append({"revision_id": revision_id, "lifecycle_state": lifecycle_state})
        self.lifecycle_state[revision_id] = lifecycle_state

    async def load_status(self, _session: object, source_evidence_id: uuid.UUID) -> StatusRow | None:
        return self.status.get(source_evidence_id)

    async def load_evidence(self, _session: object, source_evidence_id: uuid.UUID) -> EvidenceRow | None:
        return self.evidence.get(source_evidence_id)

    async def select_due_for_refresh(self, _session: object, *, now: datetime.datetime, limit: int) -> list[uuid.UUID]:
        due = sorted(
            (row for row in self.status.values() if row.next_check_at <= now),
            key=lambda row: row.next_check_at,
        )
        return [row.source_evidence_id for row in due[:limit]]

    async def update_status_refresh(
        self,
        _session: object,
        *,
        source_evidence_id: uuid.UUID,
        checked_at: datetime.datetime,
        next_check_at: datetime.datetime,
    ) -> bool:
        self.update_calls += 1
        row = self.status.get(source_evidence_id)
        if row is None or row.next_check_at > checked_at:
            return False
        self.status[source_evidence_id] = dataclasses.replace(
            row, checked_at=checked_at, next_check_at=next_check_at, status_source="worker"
        )
        return True


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> FakeStatusQueries:
    fake = FakeStatusQueries()
    for name in (
        "load_status",
        "load_evidence",
        "select_due_for_refresh",
        "update_status_refresh",
        "mark_status_terminal",
        "find_active_revisions_by_source",
        "revoke_or_expire_revision",
    ):
        monkeypatch.setattr(ss.queries, name, getattr(fake, name))
    return fake


class FakeAppender:
    """A double for `OperationalChainService` -- records every
    `append_event` call rather than touching a database, matching
    `test_arc_materialisation.py`'s own `appender=object()` convention one
    step further: this one is a real double because these tests assert
    *what* was appended, not merely that submission's guard let the call
    through."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def append_event(self, _session: object, **kwargs: object) -> AppendResult:
        self.calls.append(kwargs)
        return AppendResult(event_id=uuid.uuid4(), sequence=len(self.calls), event_digest="a" * 64)


# ---------------------------------------------------------------------------
# SourceStatusService.check_status -- freshness and status vocabulary
# ---------------------------------------------------------------------------


async def test_missing_status_row_raises_not_found(fake_queries: FakeStatusQueries) -> None:
    service = ss.SourceStatusService(_session_factory, clock=_FakeClock(_NOW))
    with pytest.raises(NotFoundError):
        await service.check_status(uuid.uuid4())


async def test_current_status_within_the_freshness_window_succeeds(fake_queries: FakeStatusQueries) -> None:
    fake_queries.seed(status=_status_row(checked_at=_NOW, next_check_at=_NOW + ss.FRESHNESS_WINDOW))
    service = ss.SourceStatusService(_session_factory, clock=_FakeClock(_NOW))

    view = await service.check_status(_EVIDENCE_ID)

    assert view.status == ss.STATUS_CURRENT
    assert view.source_evidence_id == _EVIDENCE_ID


async def test_overdue_fails_closed(fake_queries: FakeStatusQueries) -> None:
    """Named exactly this so the enforcement table's own pointer resolves.

    A row whose freshness window has lapsed fails closed even though its
    stored `status` literal still says `current` -- overdue is judged
    against the caller's clock, never trusted from the column alone.
    """
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT, checked_at=checked_at, next_check_at=next_check_at))
    clock = _FakeClock(next_check_at - datetime.timedelta(seconds=1))  # one second before the boundary: still fresh
    service = ss.SourceStatusService(_session_factory, clock=clock)
    await service.check_status(_EVIDENCE_ID)  # succeeds one second before the boundary

    clock.set(next_check_at)  # exactly at the boundary: already overdue
    with pytest.raises(ss.SourceStatusUnavailable, match="overdue"):
        await service.check_status(_EVIDENCE_ID)


@pytest.mark.parametrize("stored_status", [ss.STATUS_EXPIRED, ss.STATUS_REVOKED, ss.STATUS_UNKNOWN])
async def test_status_vocabulary_fails_closed_on_anything_but_current(
    fake_queries: FakeStatusQueries, stored_status: str
) -> None:
    fake_queries.seed(status=_status_row(status=stored_status))
    service = ss.SourceStatusService(_session_factory, clock=_FakeClock(_NOW))

    with pytest.raises(ss.SourceStatusUnavailable, match=stored_status):
        await service.check_status(_EVIDENCE_ID)


async def test_expiry_fails_closed_on_the_deadline_even_before_any_worker_pass(
    fake_queries: FakeStatusQueries,
) -> None:
    """Admission caps a row's own `next_check_at` at its claim's
    `expires_at` (see `source_admission.py`), so `check_status` starts
    refusing at the exact deadline on its own -- proving the read path does
    not depend on the refresh worker having recorded the expiry first."""
    expires_at = _NOW + datetime.timedelta(seconds=120)
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT, checked_at=_NOW, next_check_at=expires_at))
    service = ss.SourceStatusService(_session_factory, clock=_FakeClock(expires_at - datetime.timedelta(seconds=1)))
    await service.check_status(_EVIDENCE_ID)  # one second before the deadline: still current

    service = ss.SourceStatusService(_session_factory, clock=_FakeClock(expires_at))
    with pytest.raises(ss.SourceStatusUnavailable, match="overdue"):
        await service.check_status(_EVIDENCE_ID)


# ---------------------------------------------------------------------------
# SourceStatusService.record_revocation / record_expiry -- refused, and
# provably untouched, until an operational-chain appender is wired.
# ---------------------------------------------------------------------------


async def test_record_revocation_refuses_before_opening_a_session() -> None:
    service = ss.SourceStatusService(_ExplodingSessionFactory(), clock=_FakeClock(_NOW))
    with pytest.raises(ss.SourceOperationalIntegrityPending):
        await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")


async def test_record_expiry_refuses_before_opening_a_session() -> None:
    service = ss.SourceStatusService(_ExplodingSessionFactory(), clock=_FakeClock(_NOW))
    with pytest.raises(ss.SourceOperationalIntegrityPending):
        await service.record_expiry(_EVIDENCE_ID)


async def test_calling_record_revocation_twice_refuses_identically_both_times() -> None:
    """The refusal itself is idempotent: a second call is not a second,
    different failure -- it is the same refusal, and still touches nothing."""
    service = ss.SourceStatusService(_ExplodingSessionFactory(), clock=_FakeClock(_NOW))
    with pytest.raises(ss.SourceOperationalIntegrityPending):
        await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")
    with pytest.raises(ss.SourceOperationalIntegrityPending):
        await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")


# ---------------------------------------------------------------------------
# SourceStatusService.record_revocation / record_expiry -- the real
# four-part write, once an appender is injected.
# ---------------------------------------------------------------------------


def _dependent(revision_id: uuid.UUID | None = None, artifact_id: uuid.UUID | None = None) -> DependentRevision:
    return DependentRevision(
        artifact_id=artifact_id or uuid.uuid4(),
        revision_id=revision_id or uuid.uuid4(),
    )


async def test_record_revocation_cascades_every_dependent_active_revision(
    fake_queries: FakeStatusQueries,
) -> None:
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT))
    fake_queries.seed(evidence=_evidence_row(tenant_id=None))
    dependents = [_dependent(), _dependent()]
    fake_queries.seed_dependents(_EVIDENCE_ID, dependents)
    appender = FakeAppender()
    factory = _RecordingSessionFactory()
    service = ss.SourceStatusService(factory, clock=_FakeClock(_NOW), operational_chain_appender=appender)

    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")

    assert fake_queries.mark_terminal_calls == [{"source_evidence_id": _EVIDENCE_ID, "status": ss.STATUS_REVOKED}]
    assert fake_queries.status[_EVIDENCE_ID].status == ss.STATUS_REVOKED
    assert {c["revision_id"] for c in fake_queries.revoke_or_expire_calls} == {d.revision_id for d in dependents}
    assert all(c["lifecycle_state"] == "revoked" for c in fake_queries.revoke_or_expire_calls)
    assert fake_queries.lifecycle_state[dependents[0].revision_id] == "revoked"
    assert fake_queries.lifecycle_state[dependents[1].revision_id] == "revoked"

    # One append per dependent revision, each naming that revision and the
    # ADR 039 freshness_downgraded event type -- never the genesis type,
    # which this cascade must never claim to be writing.
    assert len(appender.calls) == 2
    appended_revision_ids = {c["revision_id"] for c in appender.calls}
    assert appended_revision_ids == {d.revision_id for d in dependents}
    for call in appender.calls:
        assert call["event_type"] == "freshness_downgraded"
        assert call["actor"].role == "system"

    # Exactly one audit row, in the same transaction -- global scope here
    # because the seeded evidence has no tenant.
    assert len(factory.sessions) == 1
    assert sum("arc_audit_outbox" in stmt for stmt in factory.sessions[0].executed) == 1


async def test_record_revocation_files_the_audit_event_under_the_sources_own_tenant(
    fake_queries: FakeStatusQueries,
) -> None:
    tenant_id = uuid.uuid4()
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT))
    fake_queries.seed(evidence=_evidence_row(tenant_id=tenant_id))
    appender = FakeAppender()
    factory = _RecordingSessionFactory()
    service = ss.SourceStatusService(factory, clock=_FakeClock(_NOW), operational_chain_appender=appender)

    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")

    [executed] = [stmt for stmt in factory.sessions[0].executed if "arc_audit_outbox" in stmt]
    # `emit` (tenant-scoped) binds a `:tenant_id` parameter; `emit_global`
    # never does. Asserting the parameter name is present is what actually
    # distinguishes the two call sites -- the INSERT text is identical
    # either way.
    assert ":tenant_id" in executed


async def test_record_expiry_cascades_to_expired_not_revoked(fake_queries: FakeStatusQueries) -> None:
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT))
    fake_queries.seed(evidence=_evidence_row(tenant_id=None))
    dependent = _dependent()
    fake_queries.seed_dependents(_EVIDENCE_ID, [dependent])
    appender = FakeAppender()
    service = ss.SourceStatusService(
        _RecordingSessionFactory(), clock=_FakeClock(_NOW), operational_chain_appender=appender
    )

    await service.record_expiry(_EVIDENCE_ID)

    assert fake_queries.status[_EVIDENCE_ID].status == ss.STATUS_EXPIRED
    assert fake_queries.lifecycle_state[dependent.revision_id] == "expired"
    assert len(appender.calls) == 1


async def test_a_source_with_no_dependent_revisions_still_flips_status_with_no_cascade(
    fake_queries: FakeStatusQueries,
) -> None:
    """A source can be revoked before anything was ever materialized from
    it -- the status flip and audit row still happen; there is simply
    nothing for the cascade to touch."""
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT))
    fake_queries.seed(evidence=_evidence_row(tenant_id=None))
    appender = FakeAppender()
    factory = _RecordingSessionFactory()
    service = ss.SourceStatusService(factory, clock=_FakeClock(_NOW), operational_chain_appender=appender)

    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")

    assert fake_queries.status[_EVIDENCE_ID].status == ss.STATUS_REVOKED
    assert appender.calls == []
    assert any("arc_audit_outbox" in stmt for stmt in factory.sessions[0].executed)


async def test_an_already_terminal_status_is_never_re_cascaded(fake_queries: FakeStatusQueries) -> None:
    """The compare-and-swap on `mark_status_terminal` is what makes a
    retry -- or a call losing a race to another one targeting the same
    row -- a genuine no-op: no second cascade, no second audit row, no
    second operational event."""
    fake_queries.seed(status=_status_row(status=ss.STATUS_REVOKED))
    fake_queries.seed(evidence=_evidence_row(tenant_id=None))
    fake_queries.seed_dependents(_EVIDENCE_ID, [_dependent()])
    appender = FakeAppender()
    factory = _RecordingSessionFactory()
    service = ss.SourceStatusService(factory, clock=_FakeClock(_NOW), operational_chain_appender=appender)

    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")

    assert appender.calls == []
    assert fake_queries.revoke_or_expire_calls == []
    assert factory.sessions[0].executed == []


async def test_record_revocation_twice_cascades_exactly_once(fake_queries: FakeStatusQueries) -> None:
    fake_queries.seed(status=_status_row(status=ss.STATUS_CURRENT))
    fake_queries.seed(evidence=_evidence_row(tenant_id=None))
    fake_queries.seed_dependents(_EVIDENCE_ID, [_dependent()])
    appender = FakeAppender()
    service = ss.SourceStatusService(
        _RecordingSessionFactory(), clock=_FakeClock(_NOW), operational_chain_appender=appender
    )

    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")
    await service.record_revocation(_EVIDENCE_ID, reason_code="upstream_revoked")

    assert len(appender.calls) == 1


# ---------------------------------------------------------------------------
# SourceStatusRefreshWorker
# ---------------------------------------------------------------------------


class _RevokedProvider:
    """Reports every source as revoked -- the only honest way to exercise
    the revocation branch without a real upstream to revoke against."""

    async def check(
        self, *, source_evidence_id: uuid.UUID, verifier_id: str, connector_id: str | None, policy_id: str | None
    ) -> wr.RemoteStatusCheck:
        return wr.RemoteStatusCheck(revoked=True, reason_code="test_revoked")


class _RaisingProvider:
    """Simulates a connector/provider call that fails outright."""

    async def check(
        self, *, source_evidence_id: uuid.UUID, verifier_id: str, connector_id: str | None, policy_id: str | None
    ) -> wr.RemoteStatusCheck:
        raise RuntimeError("provider unreachable")


class _SelectiveRaisingProvider:
    """Fails for one named id, succeeds (never revoked) for every other --
    what proves one row's failure does not cost its siblings in the batch."""

    def __init__(self, failing_id: uuid.UUID) -> None:
        self._failing_id = failing_id

    async def check(
        self, *, source_evidence_id: uuid.UUID, verifier_id: str, connector_id: str | None, policy_id: str | None
    ) -> wr.RemoteStatusCheck:
        if source_evidence_id == self._failing_id:
            raise RuntimeError("provider unreachable")
        return wr.RemoteStatusCheck(revoked=False)


def _service(clock: _FakeClock) -> ss.SourceStatusService:
    return ss.SourceStatusService(_session_factory, clock=clock)


async def test_a_due_current_row_is_refreshed_and_its_window_is_extended(fake_queries: FakeStatusQueries) -> None:
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    fake_queries.seed(
        status=_status_row(checked_at=checked_at, next_check_at=next_check_at),
        evidence=_evidence_row(expires_at=_NOW + datetime.timedelta(days=365)),
    )
    later = next_check_at + datetime.timedelta(seconds=1)
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(_FakeClock(later)), clock=_FakeClock(later))

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=1, integrity_pending=0, failed=0)
    row = fake_queries.status[_EVIDENCE_ID]
    assert row.checked_at == later
    assert row.next_check_at == later + ss.FRESHNESS_WINDOW


async def test_the_refreshed_window_is_capped_at_the_evidence_expiry_deadline(fake_queries: FakeStatusQueries) -> None:
    checked_at = _NOW
    expires_at = checked_at + datetime.timedelta(seconds=120)  # sooner than the 300s window
    fake_queries.seed(
        status=_status_row(checked_at=checked_at, next_check_at=checked_at + datetime.timedelta(seconds=60)),
        evidence=_evidence_row(expires_at=expires_at),
    )
    now = checked_at + datetime.timedelta(seconds=60)
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(_FakeClock(now)), clock=_FakeClock(now))

    result = await worker.run_once()

    assert result.refreshed == 1
    assert fake_queries.status[_EVIDENCE_ID].next_check_at == expires_at


async def test_running_twice_immediately_refreshes_only_once(fake_queries: FakeStatusQueries) -> None:
    """The second call finds nothing due: the first call's own write moved
    `next_check_at` past `now`, so this is idempotent by construction, not
    by a special case in the worker."""
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    fake_queries.seed(
        status=_status_row(checked_at=checked_at, next_check_at=next_check_at),
        evidence=_evidence_row(expires_at=_NOW + datetime.timedelta(days=365)),
    )
    now = next_check_at
    clock = _FakeClock(now)
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(clock), clock=clock)

    first = await worker.run_once()
    second = await worker.run_once()

    assert first == wr.SourceStatusRefreshResult(due=1, refreshed=1, integrity_pending=0, failed=0)
    assert second == wr.SourceStatusRefreshResult(due=0, refreshed=0, integrity_pending=0, failed=0)


async def test_before_the_expiry_deadline_the_row_is_refreshed_not_flagged(fake_queries: FakeStatusQueries) -> None:
    checked_at = _NOW
    expires_at = checked_at + datetime.timedelta(seconds=120)
    fake_queries.seed(
        status=_status_row(checked_at=checked_at, next_check_at=checked_at + datetime.timedelta(seconds=60)),
        evidence=_evidence_row(expires_at=expires_at),
    )
    one_second_before = expires_at - datetime.timedelta(seconds=1)
    worker = wr.SourceStatusRefreshWorker(
        _session_factory, _service(_FakeClock(one_second_before)), clock=_FakeClock(one_second_before)
    )

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=1, integrity_pending=0, failed=0)


async def test_at_the_expiry_deadline_the_worker_attempts_expiry_and_leaves_the_row_unchanged(
    fake_queries: FakeStatusQueries,
) -> None:
    """Drives the deadline transition rather than asserting a status column:
    the worker's own attempt fires exactly at `expires_at`, is refused
    (no operational-chain appender is wired), and the row is byte-for-byte
    what it was before the pass -- the refusal and "touched nothing" are
    the same fact, not two things this test has to trust line up."""
    checked_at = _NOW
    expires_at = checked_at + datetime.timedelta(seconds=120)
    before = _status_row(checked_at=checked_at, next_check_at=checked_at + datetime.timedelta(seconds=60))
    fake_queries.seed(status=before, evidence=_evidence_row(expires_at=expires_at))
    worker = wr.SourceStatusRefreshWorker(
        _session_factory, _service(_FakeClock(expires_at)), clock=_FakeClock(expires_at)
    )

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=0, integrity_pending=1, failed=0)
    assert fake_queries.status[_EVIDENCE_ID] == before
    assert fake_queries.update_calls == 0


async def test_running_the_stuck_expiry_pass_twice_produces_the_same_refusal_both_times(
    fake_queries: FakeStatusQueries,
) -> None:
    """Idempotency in the case that matters most: a row the worker cannot
    yet record stays exactly as due, and re-running produces the identical
    outcome rather than drifting or double-counting."""
    checked_at = _NOW
    expires_at = checked_at + datetime.timedelta(seconds=120)
    before = _status_row(checked_at=checked_at, next_check_at=checked_at + datetime.timedelta(seconds=60))
    fake_queries.seed(status=before, evidence=_evidence_row(expires_at=expires_at))
    clock = _FakeClock(expires_at)
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(clock), clock=clock)

    first = await worker.run_once()
    second = await worker.run_once()

    assert first == second == wr.SourceStatusRefreshResult(due=1, refreshed=0, integrity_pending=1, failed=0)
    assert fake_queries.status[_EVIDENCE_ID] == before
    assert fake_queries.update_calls == 0


async def test_an_upstream_revocation_is_noticed_and_attempted_without_anyone_asking(
    fake_queries: FakeStatusQueries,
) -> None:
    """The worker, not an access-triggered check, is what notices: nothing
    calls `check_status` in this test, and the transition is still driven."""
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    before = _status_row(checked_at=checked_at, next_check_at=next_check_at)
    fake_queries.seed(status=before, evidence=_evidence_row(expires_at=_NOW + datetime.timedelta(days=365)))
    now = next_check_at
    worker = wr.SourceStatusRefreshWorker(
        _session_factory, _service(_FakeClock(now)), clock=_FakeClock(now), remote_provider=_RevokedProvider()
    )

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=0, integrity_pending=1, failed=0)
    assert fake_queries.status[_EVIDENCE_ID] == before
    assert fake_queries.update_calls == 0


async def test_a_providers_failure_is_counted_and_does_not_raise(fake_queries: FakeStatusQueries) -> None:
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    fake_queries.seed(
        status=_status_row(checked_at=checked_at, next_check_at=next_check_at),
        evidence=_evidence_row(expires_at=_NOW + datetime.timedelta(days=365)),
    )
    now = next_check_at
    worker = wr.SourceStatusRefreshWorker(
        _session_factory, _service(_FakeClock(now)), clock=_FakeClock(now), remote_provider=_RaisingProvider()
    )

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=0, integrity_pending=0, failed=1)


async def test_one_rows_provider_failure_does_not_abandon_the_rest_of_the_batch(
    fake_queries: FakeStatusQueries,
) -> None:
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    failing_id = uuid.uuid4()
    healthy_id = uuid.uuid4()
    fake_queries.seed(
        status=_status_row(source_evidence_id=failing_id, checked_at=checked_at, next_check_at=next_check_at),
        evidence=_evidence_row(source_evidence_id=failing_id, expires_at=_NOW + datetime.timedelta(days=365)),
    )
    fake_queries.seed(
        status=_status_row(source_evidence_id=healthy_id, checked_at=checked_at, next_check_at=next_check_at),
        evidence=_evidence_row(source_evidence_id=healthy_id, expires_at=_NOW + datetime.timedelta(days=365)),
    )
    now = next_check_at
    worker = wr.SourceStatusRefreshWorker(
        _session_factory,
        _service(_FakeClock(now)),
        clock=_FakeClock(now),
        remote_provider=_SelectiveRaisingProvider(failing_id),
    )

    result = await worker.run_once()

    assert result.due == 2
    assert result.failed == 1
    assert result.refreshed == 1
    assert fake_queries.status[healthy_id].next_check_at == now + ss.FRESHNESS_WINDOW


async def test_one_pass_checks_at_most_the_configured_limit(fake_queries: FakeStatusQueries) -> None:
    checked_at = _NOW
    next_check_at = checked_at + ss.FRESHNESS_WINDOW
    ids = [uuid.uuid4() for _ in range(3)]
    for source_evidence_id in ids:
        fake_queries.seed(
            status=_status_row(
                source_evidence_id=source_evidence_id, checked_at=checked_at, next_check_at=next_check_at
            ),
            evidence=_evidence_row(
                source_evidence_id=source_evidence_id, expires_at=_NOW + datetime.timedelta(days=365)
            ),
        )
    now = next_check_at
    clock = _FakeClock(now)
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(clock), clock=clock, limit=1)

    first = await worker.run_once()
    second = await worker.run_once()
    third = await worker.run_once()
    fourth = await worker.run_once()

    assert (first.due, second.due, third.due, fourth.due) == (1, 1, 1, 0)
    assert first.refreshed == second.refreshed == third.refreshed == 1


async def test_a_row_with_no_evidence_sibling_is_skipped_rather_than_crashing_the_batch(
    fake_queries: FakeStatusQueries,
) -> None:
    """Every real status row has an evidence sibling inserted in the same
    admission transaction; this proves a row that somehow lacks one is
    handled defensively rather than raising out of the whole pass."""
    fake_queries.seed(status=_status_row(checked_at=_NOW, next_check_at=_NOW + ss.FRESHNESS_WINDOW))
    now = _NOW + ss.FRESHNESS_WINDOW
    worker = wr.SourceStatusRefreshWorker(_session_factory, _service(_FakeClock(now)), clock=_FakeClock(now))

    result = await worker.run_once()

    assert result == wr.SourceStatusRefreshResult(due=1, refreshed=0, integrity_pending=0, failed=0)


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        wr.SourceStatusRefreshWorker(_session_factory, _service(_FakeClock(_NOW)), limit=0)
