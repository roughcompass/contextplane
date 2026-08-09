"""What the signal ingest service decides, one decision per test.

The parity suite proves both transports behave alike. This file proves the thing
they both call is right in the first place: which envelopes are well-formed,
which submissions are the same submission, where authority comes from, what is
refused and in what order, and what each of those leaves in the audit log.

Everything runs against in-memory doubles for the session and the governance
service. Neither is a stand-in for something unproven -- the ledger's uniqueness
is the database's own and the governance policy has its own tests -- so a failure
here is a failure of this module's reasoning rather than of its collaborators.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from contextplane.audit import actions
from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION, AUTHORITY_OWNER_HUMAN
from contextplane.service.memory.source_governance import Admission, SourcePolicy
from contextplane.signals.ingest import (
    MAX_EVIDENCE_HANDLE_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_PAYLOAD_BYTES,
    MAX_REFERENCES,
    OUTCOME_CREATED,
    OUTCOME_RECOGNISED,
    REASON_IDEMPOTENCY_CONFLICT,
    REASON_INGEST_CEILING,
    REASON_PRODUCER_IDENTITY,
    REASON_SOURCE_AUTHORITY_INVALID,
    REASON_SOURCE_UNREGISTERED,
    REJECTION_REASONS,
    SIGNAL_SCHEMA_VERSION,
    TARGET_SIGNAL,
    TARGET_SIGNAL_SOURCE,
    ExternalSignalEnvelopeV1,
    SignalIngestRefused,
    SignalIngestService,
    content_digest_for,
    normalize_references,
    reject_server_assigned,
)
from contextplane.signals.models import ExternalSignal
from contextplane.storage.models import AuditLog
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)
_EVENT_TIME = datetime.datetime(2026, 3, 1, 11, 0, tzinfo=datetime.UTC)
_OBSERVED_TIME = datetime.datetime(2026, 3, 1, 11, 5, tzinfo=datetime.UTC)

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ACTOR = uuid.UUID("33333333-3333-3333-3333-333333333333")
_SOURCE = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _ctx(tenant_id: uuid.UUID = _TENANT, actor_id: uuid.UUID = _ACTOR) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _reference(**overrides: Any) -> ExternalReferenceV1:
    fields: dict[str, Any] = {
        "source_system": "github",
        "source_namespace": "acme/app",
        "kind": "run",
        "external_id": "9182",
        "classification": "internal",
        "external_authority": "github-actions",
    }
    fields.update(overrides)
    return ExternalReferenceV1(**fields)


def _envelope(**overrides: Any) -> ExternalSignalEnvelopeV1:
    fields: dict[str, Any] = {
        "source_id": _SOURCE,
        "source_system": "github",
        "source_event_id": "check_run/9182",
        "producer_id": "actions[bot]",
        "producer_type": "external",
        "idempotency_key": "delivery-7f3c",
        "classification": "internal",
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "event_time": _EVENT_TIME,
        "observed_time": _OBSERVED_TIME,
        "references": (_reference(),),
        "payload": {"conclusion": "success", "run_attempt": 2},
    }
    fields.update(overrides)
    return ExternalSignalEnvelopeV1(**fields)


def _policy(tenant_id: uuid.UUID = _TENANT, tier: str = AUTHORITY_OBSERVER_EXTRACTION) -> SourcePolicy:
    return SourcePolicy(
        source_id=_SOURCE,
        tenant_id=tenant_id,
        authority_tier=tier,
        ingest_ceiling=1000,
        window_seconds=3600,
        breaker_open_until=None,
        breach_count=0,
    )


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[ExternalSignal]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[ExternalSignal]:
        return list(self._rows)


class _Store:
    def __init__(self, rows: list[ExternalSignal] | None = None) -> None:
        self.rows: list[ExternalSignal] = list(rows or [])
        self.added: list[ExternalSignal] = []
        self.audit: list[AuditLog] = []
        #: Set only by the race test: when present, the next commit that staged a
        #: row raises it once. A lost insert race reproduced without two loops.
        self.raise_once: Exception | None = None


class _FakeSession:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def execute(self, _stmt: object) -> _Result:
        return _Result(self._store.rows)

    def add(self, row: ExternalSignal | AuditLog) -> None:
        if isinstance(row, AuditLog):
            self._store.audit.append(row)
            return
        self._store.added.append(row)
        self._store.rows.append(row)

    def begin(self) -> _FakeSession:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _factory(store: _Store) -> Any:
    def make() -> _FakeSession:
        return _FakeSession(store)

    return make


class _FakeGovernance:
    def __init__(self, policy: SourcePolicy | None, admission: Admission | None = None) -> None:
        self._policy = policy
        self._admission = admission or Admission(permitted=True, remaining=999)
        self.policy_calls: list[uuid.UUID] = []
        self.admit_calls: list[uuid.UUID] = []

    async def policy_for(self, source_id: uuid.UUID) -> SourcePolicy | None:
        self.policy_calls.append(source_id)
        return self._policy

    async def admit(self, source_id: uuid.UUID, *, count: int = 1) -> Admission:
        self.admit_calls.append(source_id)
        return self._admission


def _service(store: _Store, governance: _FakeGovernance) -> SignalIngestService:
    return SignalIngestService(_factory(store), clock=FakeClock(_NOW), governance=governance)  # type: ignore[arg-type]


def _ingest(
    store: _Store,
    governance: _FakeGovernance,
    envelope: ExternalSignalEnvelopeV1 | None = None,
    ctx: TenantContext | None = None,
) -> Any:
    return asyncio.run(_service(store, governance).ingest(ctx or _ctx(), envelope or _envelope()))


def _audit_actions(store: _Store) -> list[str]:
    return [str(row.action) for row in store.audit]


# ---------------------------------------------------------------------------
# Envelope validation
# ---------------------------------------------------------------------------


def test_a_well_formed_envelope_constructs() -> None:
    envelope = _envelope()
    assert envelope.source_event_id == "check_run/9182"
    assert envelope.evidence_handle is None


def test_an_unsupported_schema_version_is_refused() -> None:
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        _envelope(schema_version="external_signal.v9")


def test_an_unknown_producer_type_is_refused() -> None:
    with pytest.raises(ValidationError, match="producer_type"):
        _envelope(producer_type="robot")


def test_an_unknown_classification_is_refused() -> None:
    """A classification nobody declared is one no retention policy covers."""
    with pytest.raises(ValidationError, match="classification"):
        _envelope(classification="secret")


@pytest.mark.parametrize(
    "field_name",
    ["source_system", "source_event_id", "producer_id", "idempotency_key"],
)
def test_a_blank_identity_field_is_refused(field_name: str) -> None:
    """A signal missing one of these collides with everything else missing it."""
    with pytest.raises(ValidationError, match=field_name):
        _envelope(**{field_name: "   "})


@pytest.mark.parametrize(
    ("field_name", "bound"),
    [
        ("source_system", MAX_IDENTIFIER_LENGTH),
        ("source_event_id", MAX_IDENTIFIER_LENGTH),
        ("producer_id", MAX_IDENTIFIER_LENGTH),
        ("idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH),
    ],
)
def test_an_overlong_identity_field_is_refused(field_name: str, bound: int) -> None:
    with pytest.raises(ValidationError, match="bound"):
        _envelope(**{field_name: "a" * (bound + 1)})


@pytest.mark.parametrize("field_name", ["team_key", "project_key"])
def test_an_empty_scope_key_is_refused(field_name: str) -> None:
    """An empty string passes an `is not None` check and is then grouped as a scope."""
    with pytest.raises(ValidationError, match=field_name):
        _envelope(**{field_name: ""})


def test_an_absent_scope_key_is_accepted() -> None:
    """Absence is the honest record when a producer knows no team or project."""
    envelope = _envelope(team_key=None, project_key=None)
    assert envelope.team_key is None


@pytest.mark.parametrize("field_name", ["event_time", "observed_time", "expires_at"])
def test_a_naive_timestamp_is_refused(field_name: str) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _envelope(**{field_name: _EVENT_TIME.replace(tzinfo=None)})


def test_carrying_both_a_payload_and_a_handle_is_refused() -> None:
    """Two copies of one observation drift."""
    with pytest.raises(ValidationError, match="exactly one"):
        _envelope(payload={"a": 1}, evidence_handle="handle://x")


def test_carrying_neither_a_payload_nor_a_handle_is_refused() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _envelope(payload=None, evidence_handle=None)


def test_a_blank_evidence_handle_is_refused() -> None:
    with pytest.raises(ValidationError, match="evidence_handle"):
        _envelope(payload=None, evidence_handle="  ")


def test_an_overlong_evidence_handle_is_refused() -> None:
    with pytest.raises(ValidationError, match="bound"):
        _envelope(payload=None, evidence_handle="h" * (MAX_EVIDENCE_HANDLE_LENGTH + 1))


def test_an_evidence_handle_alone_is_accepted() -> None:
    envelope = _envelope(payload=None, evidence_handle="s3://bucket/run/9182")
    assert envelope.payload is None


def test_an_empty_payload_is_refused() -> None:
    """An empty object satisfies "a payload is present" and reports nothing."""
    with pytest.raises(ValidationError, match="empty object"):
        _envelope(payload={})


def test_an_oversized_payload_is_refused() -> None:
    with pytest.raises(ValidationError, match="byte bound"):
        _envelope(payload={"blob": "x" * (MAX_PAYLOAD_BYTES + 1)})


def test_too_many_references_are_refused() -> None:
    many = tuple(_reference(external_id=f"sha-{index}") for index in range(MAX_REFERENCES + 1))
    with pytest.raises(ValidationError, match="at most"):
        _envelope(references=many)


def test_no_references_at_all_is_legal() -> None:
    """A diagnostic observation about no particular piece of work is a real report."""
    assert _envelope(references=()).references == ()


# ---------------------------------------------------------------------------
# Normalization and the digest
# ---------------------------------------------------------------------------


def test_normalization_folds_the_source_system_and_trims_the_keys() -> None:
    normalized = _envelope(
        source_system="GitHub",
        source_event_id="  check_run/9182  ",
        idempotency_key=" delivery-7f3c ",
        producer_id=" actions[bot] ",
    ).normalized()
    assert normalized.source_system == "github"
    assert normalized.source_event_id == "check_run/9182"
    assert normalized.idempotency_key == "delivery-7f3c"
    assert normalized.producer_id == "actions[bot]"


def test_the_external_id_keeps_its_case() -> None:
    """The id belongs to the other system, which may well be case-sensitive."""
    normalized = _envelope(source_event_id="Check_Run/AbC").normalized()
    assert normalized.source_event_id == "Check_Run/AbC"


def test_two_spellings_of_one_reference_fold_to_one() -> None:
    duplicated = (
        _reference(),
        _reference(source_system="GITHUB", source_namespace="Acme/App", kind="RUN"),
    )
    # Both spellings normalize through the shared reference normalizer first, so
    # the envelope receives two objects with one collision key.
    folded = _envelope(references=normalize_references([_as_mapping(r) for r in duplicated])).normalized()
    assert len(folded.references) == 1


def _as_mapping(reference: ExternalReferenceV1) -> dict[str, Any]:
    return {
        "source_system": reference.source_system,
        "source_namespace": reference.source_namespace,
        "kind": reference.kind,
        "external_id": reference.external_id,
        "classification": reference.classification,
        "external_authority": reference.external_authority,
    }


def test_references_are_ordered_by_collision_key() -> None:
    """A stable order, so two submissions listing the same work in two orders
    are one submission rather than two digests."""
    forward = _envelope(references=(_reference(external_id="a"), _reference(external_id="b")))
    backward = _envelope(references=(_reference(external_id="b"), _reference(external_id="a")))
    assert content_digest_for(forward) == content_digest_for(backward)


def test_the_digest_is_stable_across_spellings() -> None:
    plain = _envelope()
    respelled = _envelope(source_system="GITHUB", idempotency_key=" delivery-7f3c ")
    assert content_digest_for(plain) == content_digest_for(respelled)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("payload", {"conclusion": "failure"}),
        ("classification", "confidential"),
        ("producer_type", "agent"),
        ("event_time", _EVENT_TIME + datetime.timedelta(minutes=1)),
        ("observed_time", _OBSERVED_TIME + datetime.timedelta(minutes=1)),
        ("expires_at", _NOW + datetime.timedelta(days=1)),
        ("team_key", "platform"),
        ("project_key", "checkout"),
        ("references", ()),
    ],
)
def test_the_digest_changes_when_the_producer_changes_anything(field_name: str, value: Any) -> None:
    """Everything a producer controls is in the digest, or a changed replay
    would silently converge on the stored row instead of conflicting."""
    baseline = content_digest_for(_envelope())
    changed = _envelope(**{field_name: value})
    if field_name == "producer_type":
        # A human/agent producer must report as itself; the digest question is
        # separate from that rule, so give it a producer id that would pass.
        changed = _envelope(producer_type="agent", producer_id=str(_ACTOR))
    assert content_digest_for(changed) != baseline


def test_a_reference_revision_change_changes_the_digest() -> None:
    """Revision is outside the collision key on purpose -- two revisions are one
    document -- so the digest has to carry it or a resend at a new revision reads
    as the same submission."""
    baseline = content_digest_for(_envelope())
    revised = _envelope(references=(_reference(revision="v2"),))
    assert content_digest_for(revised) != baseline


def test_normalize_references_names_the_offending_index() -> None:
    """A producer sent several; saying which one is what makes the error usable."""
    with pytest.raises(ValidationError, match=r"references\[1\]"):
        normalize_references([_as_mapping(_reference()), {"source_system": "github"}])


def test_normalize_references_accepts_an_empty_list() -> None:
    assert normalize_references([]) == ()


# ---------------------------------------------------------------------------
# What a caller may not supply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["ingested_at", "ingestion_time", "authority", "signal_id", "content_digest"],
)
def test_a_server_assigned_field_is_refused_by_name(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        reject_server_assigned({field_name: "anything"})


def test_an_ordinary_submission_passes_the_reserved_name_check() -> None:
    """The check refuses reserved names and nothing else.

    Asserted by driving a full ingest of a body carrying only ordinary fields:
    a check that refused too much would fail here rather than in production, on
    the submission shape every producer actually sends.
    """
    reject_server_assigned({"source_system": "github", "payload": {"a": 1}})
    store, governance = _Store(), _FakeGovernance(_policy())
    assert _ingest(store, governance).replayed is False


def test_every_reserved_name_is_reported_at_once() -> None:
    """One round trip per typo is one round trip too many."""
    with pytest.raises(ValidationError) as raised:
        reject_server_assigned({"ingested_at": "x", "authority": "y"})
    assert "ingested_at" in str(raised.value)
    assert "authority" in str(raised.value)


# ---------------------------------------------------------------------------
# Admission: identity, authority, ceiling
# ---------------------------------------------------------------------------


def test_a_stored_row_carries_the_declared_authority_and_the_server_clock() -> None:
    store, governance = _Store(), _FakeGovernance(_policy(tier=AUTHORITY_OWNER_HUMAN))
    result = _ingest(store, governance)

    written = store.added[0]
    assert written.authority == AUTHORITY_OWNER_HUMAN
    assert written.ingested_at == _NOW
    assert written.tenant_id == _TENANT
    assert result.replayed is False
    assert result.authority == AUTHORITY_OWNER_HUMAN


def test_a_stored_row_derives_nothing_about_learning() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance)
    assert store.added[0].superseded_for_learning is False


def test_an_unregistered_source_is_refused_without_spending_the_ceiling() -> None:
    store, governance = _Store(), _FakeGovernance(None)
    with pytest.raises(NotFoundError, match="no such source"):
        _ingest(store, governance)
    assert store.added == []
    assert governance.admit_calls == []


def test_another_tenants_source_is_refused_identically() -> None:
    """A distinguishable refusal would make a source id an existence oracle."""
    absent = _FakeGovernance(None)
    foreign = _FakeGovernance(_policy(tenant_id=_OTHER_TENANT))

    with pytest.raises(NotFoundError) as absent_raised:
        _ingest(_Store(), absent)
    with pytest.raises(NotFoundError) as foreign_raised:
        _ingest(_Store(), foreign)

    assert str(absent_raised.value) == str(foreign_raised.value)
    assert foreign.admit_calls == []


def test_an_authority_tier_off_the_ladder_is_refused() -> None:
    """A tier outside the ladder ranks against nothing."""
    store, governance = _Store(), _FakeGovernance(_policy(tier="inventedtier"))
    with pytest.raises(ValidationError, match="authority ladder"):
        _ingest(store, governance)
    assert store.added == []
    assert governance.admit_calls == []


@pytest.mark.parametrize("producer_type", ["human", "agent"])
def test_a_participant_may_only_report_as_itself(producer_type: str) -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    impostor = _envelope(producer_type=producer_type, producer_id=str(uuid.uuid4()))
    with pytest.raises(ValidationError, match="report as itself"):
        _ingest(store, governance, impostor)
    assert store.added == []
    # Refused before the source is even resolved: a submission that will never
    # be stored must not spend a window or probe for a source id.
    assert governance.policy_calls == []


@pytest.mark.parametrize("producer_type", ["human", "agent"])
def test_a_participant_reporting_as_itself_is_stored(producer_type: str) -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance, _envelope(producer_type=producer_type, producer_id=str(_ACTOR)))
    assert store.added[0].producer_id == str(_ACTOR)


def test_an_external_producer_id_is_left_alone() -> None:
    """The id belongs to the source's own space and this service cannot check it."""
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance, _envelope(producer_type="external", producer_id="actions[bot]"))
    assert store.added[0].producer_id == "actions[bot]"


def test_a_source_over_its_ceiling_is_refused_and_stores_nothing() -> None:
    refused = Admission(permitted=False, reason="circuit open until 2026-03-01T13:00:00+00:00")
    store, governance = _Store(), _FakeGovernance(_policy(), refused)
    with pytest.raises(SignalIngestRefused) as raised:
        _ingest(store, governance)
    assert "circuit open" in raised.value.reason
    assert store.added == []


def test_a_refusal_with_no_reason_still_says_something() -> None:
    store, governance = _Store(), _FakeGovernance(_policy(), Admission(permitted=False))
    with pytest.raises(SignalIngestRefused, match="may not write"):
        _ingest(store, governance)


# ---------------------------------------------------------------------------
# Replay and conflict
# ---------------------------------------------------------------------------


def test_an_exact_redelivery_finds_the_stored_row() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    first = _ingest(store, governance)
    second = _ingest(store, governance)

    assert second.replayed is True
    assert second.signal_id == first.signal_id
    assert second.content_digest == first.content_digest
    assert len(store.added) == 1


def test_a_replay_does_not_spend_the_ceiling() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance)
    _ingest(store, governance)
    assert governance.admit_calls == [_SOURCE]


def test_one_occurrence_under_a_fresh_submission_key_still_replays() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    first = _ingest(store, governance)
    second = _ingest(store, governance, _envelope(idempotency_key="delivery-retry-1"))
    assert second.signal_id == first.signal_id
    assert len(store.added) == 1


def test_changed_content_under_a_used_key_conflicts() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance)
    with pytest.raises(ConflictError, match="not a replay"):
        _ingest(store, governance, _envelope(payload={"conclusion": "failure"}))
    assert len(store.added) == 1


def test_a_replay_reports_the_stored_authority_not_the_current_policy() -> None:
    """A governance edit between two calls must not rewrite what backed the row."""
    store = _Store()
    _ingest(store, _FakeGovernance(_policy(tier=AUTHORITY_OWNER_HUMAN)))
    replay = _ingest(store, _FakeGovernance(_policy(tier=AUTHORITY_OBSERVER_EXTRACTION)))
    assert replay.replayed is True
    assert replay.authority == AUTHORITY_OWNER_HUMAN


def test_a_lost_insert_race_resolves_to_the_winner() -> None:
    """Two callers read no row, both insert, and the ledger refuses the loser."""
    store = _Store()
    winner = ExternalSignal(
        signal_id=uuid.uuid4(),
        tenant_id=_TENANT,
        team_key=None,
        project_key=None,
        source_system="github",
        producer_id="actions[bot]",
        producer_type="external",
        source_event_id="check_run/9182",
        idempotency_key="delivery-7f3c",
        content_digest=content_digest_for(_envelope()),
        authority=AUTHORITY_OBSERVER_EXTRACTION,
        classification="internal",
        event_time=_EVENT_TIME,
        observed_time=_OBSERVED_TIME,
        ingested_at=_NOW,
        expires_at=None,
        schema_version=SIGNAL_SCHEMA_VERSION,
        payload={"conclusion": "success", "run_attempt": 2},
        evidence_handle=None,
        superseded_for_learning=False,
    )

    class _Racing(_FakeSession):
        async def __aexit__(self, *_exc: object) -> None:
            if store.raise_once is not None and store.added:
                store.raise_once = None
                store.added.clear()
                store.rows = [winner]
                raise IntegrityError("duplicate key", None, Exception("unique violation"))

    store.raise_once = IntegrityError("duplicate key", None, Exception("unique violation"))

    def factory() -> _Racing:
        return _Racing(store)

    service = SignalIngestService(factory, clock=FakeClock(_NOW), governance=_FakeGovernance(_policy()))  # type: ignore[arg-type]
    resolved = asyncio.run(service.ingest(_ctx(), _envelope()))

    assert resolved.replayed is True
    assert resolved.signal_id == winner.signal_id
    assert store.added == []


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


def test_a_stored_submission_is_audited_as_created() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    result = _ingest(store, governance)

    assert _audit_actions(store) == [actions.SIGNAL_INGESTED]
    row = store.audit[0]
    assert row.target_type == TARGET_SIGNAL
    assert row.target_id == result.signal_id
    assert row.tenant_id == _TENANT
    assert row.actor_id == _ACTOR
    assert row.after_jsonb is not None
    assert row.after_jsonb["outcome"] == OUTCOME_CREATED
    assert row.after_jsonb["authority"] == AUTHORITY_OBSERVER_EXTRACTION
    assert row.after_jsonb["content_digest"] == result.content_digest
    assert row.after_jsonb["reference_count"] == 1


def test_a_recognised_submission_is_audited_as_recognised() -> None:
    """Both outcomes under one action: an auditor counting retries needs both."""
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance)
    _ingest(store, governance)

    outcomes = [row.after_jsonb["outcome"] for row in store.audit if row.after_jsonb is not None]
    assert outcomes == [OUTCOME_CREATED, OUTCOME_RECOGNISED]
    assert _audit_actions(store) == [actions.SIGNAL_INGESTED, actions.SIGNAL_INGESTED]


def test_no_audit_line_carries_the_observation() -> None:
    """The audit log is the one table guaranteed to be retained and read."""
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(
        store,
        governance,
        _envelope(payload={"conclusion": "success", "secret_note": "a very identifying string"}),
    )
    recorded = str(store.audit[0].after_jsonb)
    assert "secret_note" not in recorded
    assert "a very identifying string" not in recorded
    assert "producer_id" not in recorded


def test_an_evidence_handle_never_reaches_the_audit_log() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance, _envelope(payload=None, evidence_handle="s3://bucket/private/run-9182"))
    assert "s3://bucket" not in str(store.audit[0].after_jsonb)


@pytest.mark.parametrize(
    ("governance_factory", "expected_reason"),
    [
        (lambda: _FakeGovernance(None), REASON_SOURCE_UNREGISTERED),
        (lambda: _FakeGovernance(_policy(tenant_id=_OTHER_TENANT)), REASON_SOURCE_UNREGISTERED),
        (lambda: _FakeGovernance(_policy(tier="inventedtier")), REASON_SOURCE_AUTHORITY_INVALID),
        (lambda: _FakeGovernance(_policy(), Admission(permitted=False, reason="open")), REASON_INGEST_CEILING),
    ],
)
def test_every_admission_refusal_is_audited_with_its_reason_class(
    governance_factory: Any, expected_reason: str
) -> None:
    store = _Store()
    with pytest.raises((NotFoundError, ValidationError, SignalIngestRefused)):
        _ingest(store, governance_factory())

    assert _audit_actions(store) == [actions.SIGNAL_REJECTED]
    row = store.audit[0]
    assert row.target_type == TARGET_SIGNAL_SOURCE
    assert row.target_id == _SOURCE
    assert row.after_jsonb is not None
    assert row.after_jsonb["reason_class"] == expected_reason
    assert row.error_code == expected_reason


def test_a_producer_identity_refusal_is_audited() -> None:
    store = _Store()
    with pytest.raises(ValidationError):
        _ingest(store, _FakeGovernance(_policy()), _envelope(producer_type="human", producer_id="somebody-else"))

    assert _audit_actions(store) == [actions.SIGNAL_REJECTED]
    after = store.audit[0].after_jsonb
    assert after is not None
    assert after["reason_class"] == REASON_PRODUCER_IDENTITY


def test_a_changed_replay_is_audited_as_a_conflict() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    _ingest(store, governance)
    with pytest.raises(ConflictError):
        _ingest(store, governance, _envelope(payload={"conclusion": "failure"}))

    assert _audit_actions(store)[-1] == actions.SIGNAL_REJECTED
    after = store.audit[-1].after_jsonb
    assert after is not None
    assert after["reason_class"] == REASON_IDEMPOTENCY_CONFLICT


def test_every_emitted_reason_class_is_in_the_closed_set() -> None:
    """The vocabulary reads as a label; one that grew per reworded message would
    be useless for counting."""
    store = _Store()
    with pytest.raises(NotFoundError):
        _ingest(store, _FakeGovernance(None))
    for row in store.audit:
        after = row.after_jsonb
        assert after is not None
        assert after["reason_class"] in REJECTION_REASONS


def test_a_refusal_is_audited_under_the_callers_own_tenant() -> None:
    """The row records that this tenant named that source and was turned away,
    which says nothing about whether the source exists elsewhere."""
    store = _Store()
    with pytest.raises(NotFoundError):
        _ingest(store, _FakeGovernance(_policy(tenant_id=_OTHER_TENANT)))
    assert store.audit[0].tenant_id == _TENANT
