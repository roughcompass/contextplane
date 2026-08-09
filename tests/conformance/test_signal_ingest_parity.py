"""Signal ingestion behaves the same over REST and over MCP, refusals included.

A human dashboard or a CI job reaches this surface over HTTP; an agent reaches it
over MCP. The two must not merely both exist -- they must admit the same
submissions, refuse the same ones, and derive the same row from the same
envelope. A surface that accepts on one transport what it refuses on the other is
not one contract with two spellings; it is two contracts, and the weaker one is
the one an attacker or a buggy producer will find.

**What this file drives, and what it does not.** Both surfaces are called
directly -- the route function and the tool coroutine -- over the same in-memory
fakes for the session, clock and source-governance service. That covers every
decision this surface makes: which sources may write, what authority a row
carries, whether a resubmission is a replay or a conflict, what bounds one
submission may not exceed, and what the two transports report back. It does *not*
execute SQL. The lookup's own predicate is checked structurally instead (the
compiled statement must name both of the ledger's unique keys and scope by tenant
and producer), and the uniqueness those keys enforce is the database's own,
proven where the migration is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Response
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from contextplane.api.mcp import context as mcp_context
from contextplane.api.mcp.tools import signals as signal_tools
from contextplane.api.routers import signals as signal_router
from contextplane.api.schemas.signals import SignalIngestRequest
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.source_governance import Admission, SourcePolicy
from contextplane.signals.ingest import (
    MAX_PAYLOAD_BYTES,
    MAX_REFERENCES,
    SIGNAL_SCHEMA_VERSION,
    content_digest_for,
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

#: The REST path and the MCP tool that must stay paired. Declared, not derived: a
#: test that discovered the pairing by name would agree with whatever the code
#: happens to do, including after somebody drops one side.
_REST_PATH = "/v1/signals"
_TOOL_NAME = "ingest_signal"

#: Nothing a caller may name on either transport. The credential scopes the call;
#: a tenant or actor parameter would let one producer file observations against
#: somebody else's tenant.
_FORBIDDEN_TOOL_PARAMS = frozenset({"tenant_id", "actor_id", "tenant_slug", "on_behalf_of"})

#: Server-derived, on both transports. A producer that could set the ingestion
#: time would move the audit anchor; one that could set the authority would name
#: the strongest tier and win every later conflict.
_SERVER_DERIVED_PARAMS = frozenset({"ingested_at", "ingestion_time", "authority", "content_digest", "signal_id"})

#: A conclusion is a decision with its own evidence requirement. Neither surface
#: may report one as a property of ingestion.
_CONCLUSION_KEYS = frozenset(
    {"outcome", "success", "succeeded", "failed", "rating", "verdict", "conclusion", "learning_eligible"}
)


def _ctx(tenant_id: uuid.UUID = _TENANT) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=_ACTOR, roles=["producer"])


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
# Fakes. Deliberately small: each one stands in for a collaborator whose real
# behaviour is proven elsewhere, so a failure here is a failure of this surface.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[ExternalSignal]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[ExternalSignal]:
        return list(self._rows)


class _Store:
    """Everything the fake session did, so a test can assert on the writes.

    Ledger rows and audit rows are kept apart on purpose: every assertion below
    about "nothing was written" means nothing was written *to the ledger*, and an
    audit line for the refusal is exactly what should have been written instead.
    """

    def __init__(self, rows: list[ExternalSignal] | None = None) -> None:
        self.rows: list[ExternalSignal] = list(rows or [])
        self.added: list[ExternalSignal] = []
        self.audit: list[AuditLog] = []
        self.statements: list[Any] = []


class _FakeSession:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def execute(self, stmt: Any) -> _Result:
        # Every seeded row is returned rather than the predicate being evaluated
        # in Python. The rows a test seeds *are* the rows either unique key would
        # match; the predicate itself is checked separately, by compiling it.
        self._store.statements.append(stmt)
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

    async def __aexit__(self, *exc: object) -> None:
        return None


# `Any` return types below stand in for `async_sessionmaker` and the `Services`
# container: annotating either faithfully would mean building a real one.
def _session_factory(store: _Store) -> Any:
    def factory() -> _FakeSession:
        return _FakeSession(store)

    return factory


class _FakeGovernance:
    """Declared policy plus admission, with both calls recorded.

    Recording matters for one test in particular: a submission from a source the
    caller does not own must be refused *without* spending that source's ceiling.
    """

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


def _container(store: _Store, governance: _FakeGovernance) -> Any:
    return SimpleNamespace(
        session_factory=_session_factory(store),
        clock=FakeClock(_NOW),
        source_governance=governance,
    )


# ---------------------------------------------------------------------------
# The two call paths, over the same fakes.
# ---------------------------------------------------------------------------


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_id": str(_SOURCE),
        "source_system": "github",
        "source_event_id": "check_run/9182",
        "producer_id": "actions[bot]",
        "producer_type": "external",
        "idempotency_key": "delivery-7f3c",
        "classification": "internal",
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "event_time": _EVENT_TIME.isoformat(),
        "observed_time": _OBSERVED_TIME.isoformat(),
        "references": [
            {
                "source_system": "github",
                "source_namespace": "acme/app",
                "kind": "run",
                "external_id": "9182",
                "classification": "internal",
                "external_authority": "github-actions",
            }
        ],
        "payload": {"conclusion": "success", "run_attempt": 2},
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not _ABSENT}


class _Absent:
    """Marker for "leave this field out of the body entirely"."""


_ABSENT = _Absent()


def _call_rest(store: _Store, governance: _FakeGovernance, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Drive the REST route function directly; returns (status, body)."""
    response = Response()
    result = asyncio.run(
        signal_router.ingest_signal(
            body=SignalIngestRequest(**body),
            ctx=_ctx(),
            container=_container(store, governance),
            response=response,
        )
    )
    return response.status_code, result.model_dump(mode="json")


def _call_mcp(
    store: _Store,
    governance: _FakeGovernance,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Drive the MCP tool coroutine directly; returns the decoded JSON."""
    app = SimpleNamespace(state=SimpleNamespace(services=_container(store, governance)))
    token = mcp_context._request_app.set(app)

    async def _resolve(*_args: object, **_kwargs: object) -> TenantContext:
        return _ctx()

    monkeypatch.setattr(mcp_context, "_resolve_tenant", _resolve)
    try:
        raw = asyncio.run(
            signal_tools.ingest_signal(
                source_id=body["source_id"],
                source_system=body["source_system"],
                source_event_id=body["source_event_id"],
                producer_id=body["producer_id"],
                producer_type=body["producer_type"],
                idempotency_key=body["idempotency_key"],
                classification=body["classification"],
                event_time=body["event_time"],
                observed_time=body["observed_time"],
                references=body.get("references"),
                payload=body.get("payload"),
                evidence_handle=body.get("evidence_handle"),
                team_key=body.get("team_key"),
                project_key=body.get("project_key"),
                expires_at=body.get("expires_at"),
                schema_version=body.get("schema_version", SIGNAL_SCHEMA_VERSION),
                session_factory=_session_factory(store),
                clock=FakeClock(_NOW),
            )
        )
    finally:
        mcp_context._request_app.reset(token)
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


def _temporal_normalized(value: dict[str, Any]) -> dict[str, Any]:
    """Compare bodies without tripping over two legal ISO-8601 spellings.

    Pydantic renders a UTC instant as `...Z` and `datetime.isoformat()` renders
    it as `...+00:00`. Both name the same moment, and a byte comparison would
    report a divergence where there is none -- while still hiding a real one
    behind the noise.
    """
    out = dict(value)
    for key in ("ingested_at",):
        if isinstance(out.get(key), str):
            out[key] = datetime.datetime.fromisoformat(out[key]).isoformat()
    references = out.get("references")
    if isinstance(references, list):
        out["references"] = [
            {
                name: (
                    datetime.datetime.fromisoformat(item[name]).isoformat()
                    if name == "observed_at" and isinstance(item.get(name), str)
                    else item[name]
                )
                for name in item
            }
            for item in references
        ]
    return out


# ---------------------------------------------------------------------------
# The surfaces exist, and are paired.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_tools() -> dict[str, Any]:
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_signal_ingest_is_served_over_rest() -> None:
    paths = {route.path for route in signal_router.router.routes}
    assert _REST_PATH in paths, f"{_REST_PATH} is not served; the paths are {sorted(paths)}"


def test_signal_ingest_router_is_mounted_on_the_app() -> None:
    """An unmounted router passes every shape check and serves nothing.

    Read off the composition root's own module rather than a constructed app,
    which would need a database URL to answer a question about wiring.
    """
    from contextplane import wiring

    text = (Path(wiring.__path__[0]) / "routes.py").read_text(encoding="utf-8")
    assert "signals_router" in text, "wiring/routes.py does not import the signals router"
    assert "app.include_router(signals_router.router)" in text, "the signals router is imported but never mounted"


def test_signal_ingest_is_served_over_mcp(mcp_tools: dict[str, Any]) -> None:
    assert _TOOL_NAME in mcp_tools, f"{_TOOL_NAME} is not registered on the MCP surface"


def test_the_tool_documents_what_it_returns(mcp_tools: dict[str, Any]) -> None:
    """An agent picks a tool from its description, and this one is a write."""
    description = (getattr(mcp_tools[_TOOL_NAME], "description", "") or "").lower()
    assert description.strip(), f"{_TOOL_NAME} has no description"
    assert "returns" in description, f"{_TOOL_NAME} does not say what it returns"


def test_the_tool_takes_no_identity_parameter(mcp_tools: dict[str, Any]) -> None:
    schema = getattr(mcp_tools[_TOOL_NAME], "inputSchema", None) or {}
    offending = set(schema.get("properties", {})) & _FORBIDDEN_TOOL_PARAMS
    assert not offending, f"{_TOOL_NAME} accepts {sorted(offending)}; the credential is what scopes a call"


# ---------------------------------------------------------------------------
# What neither surface lets a caller decide.
# ---------------------------------------------------------------------------


def test_the_tool_cannot_receive_a_server_derived_field(mcp_tools: dict[str, Any]) -> None:
    """Closed by construction: the argument schema simply has no such parameter."""
    schema = getattr(mcp_tools[_TOOL_NAME], "inputSchema", None) or {}
    offending = set(schema.get("properties", {})) & _SERVER_DERIVED_PARAMS
    assert not offending, f"{_TOOL_NAME} accepts {sorted(offending)}, which this service derives"


@pytest.mark.parametrize("field_name", sorted(_SERVER_DERIVED_PARAMS))
def test_rest_refuses_a_supplied_server_derived_field(field_name: str) -> None:
    """A JSON body is open unless something closes it, so REST refuses by name.

    The message has to name the field: a producer that believes it stamped the
    ingestion time and had it dropped will reconcile two systems against a
    timestamp that means something else.
    """
    # Pydantic wraps the service-raised ValidationError, so the type is not the
    # assertion here -- the message naming the field is.
    with pytest.raises(Exception) as raised:
        SignalIngestRequest(**_body(**{field_name: "2026-03-01T12:00:00+00:00"}))
    assert field_name in str(raised.value)


def test_authority_is_not_an_envelope_field() -> None:
    """The envelope cannot carry it, so no adapter can pass one through."""
    from contextplane.signals.ingest import ExternalSignalEnvelopeV1

    fields = {field.name for field in dataclasses.fields(ExternalSignalEnvelopeV1)}
    assert not fields & {"authority", "ingested_at", "content_digest", "signal_id"}


def test_the_stored_row_carries_the_declared_authority_and_the_server_clock() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    status_code, body = _call_rest(store, governance, _body())

    assert status_code == 201
    written = store.added[0]
    assert written.authority == AUTHORITY_OBSERVER_EXTRACTION
    assert written.ingested_at == _NOW
    assert body["authority"] == AUTHORITY_OBSERVER_EXTRACTION


# ---------------------------------------------------------------------------
# Equivalent behaviour, admission and refusal alike.
# ---------------------------------------------------------------------------


def test_both_surfaces_return_the_same_body_for_the_same_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same keys, same values -- except the row id, which is allocated per write.

    `signal_id` is excluded because two separate writes are genuinely two rows;
    it is asserted to be a UUID on both sides instead. Everything else is
    compared, including every normalized reference and its collision key.
    """
    rest_status, rest_body = _call_rest(_Store(), _FakeGovernance(_policy()), _body())
    mcp_body = _call_mcp(_Store(), _FakeGovernance(_policy()), _body(), monkeypatch)

    assert rest_status == 201
    assert set(mcp_body) == set(rest_body)
    uuid.UUID(rest_body["signal_id"])
    uuid.UUID(mcp_body["signal_id"])

    comparable_rest = {k: v for k, v in _temporal_normalized(rest_body).items() if k != "signal_id"}
    comparable_mcp = {k: v for k, v in _temporal_normalized(mcp_body).items() if k != "signal_id"}
    assert comparable_mcp == comparable_rest


def test_neither_surface_reports_a_conclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingestion records what a source said. It decides nothing about it.

    The submission below carries `conclusion: success` in its own payload, which
    is what a real CI source sends -- and the response must not lift that into a
    verdict of this system's own, because a conclusion drawn here would arrive
    with no evidence chain behind it.
    """
    store = _Store()
    _, rest_body = _call_rest(store, _FakeGovernance(_policy()), _body())
    mcp_body = _call_mcp(_Store(), _FakeGovernance(_policy()), _body(), monkeypatch)

    assert not set(rest_body) & _CONCLUSION_KEYS
    assert not set(mcp_body) & _CONCLUSION_KEYS
    assert store.added[0].superseded_for_learning is False


def test_an_exact_redelivery_replays_on_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second submission finds the first rather than storing a second row."""
    rest_store, rest_gov = _Store(), _FakeGovernance(_policy())
    first_status, first = _call_rest(rest_store, rest_gov, _body())
    second_status, second = _call_rest(rest_store, rest_gov, _body())

    assert (first_status, second_status) == (201, 200)
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["signal_id"] == first["signal_id"]
    assert len(rest_store.added) == 1

    mcp_store, mcp_gov = _Store(), _FakeGovernance(_policy())
    mcp_first = _call_mcp(mcp_store, mcp_gov, _body(), monkeypatch)
    mcp_second = _call_mcp(mcp_store, mcp_gov, _body(), monkeypatch)

    assert mcp_first["replayed"] is False
    assert mcp_second["replayed"] is True
    assert mcp_second["signal_id"] == mcp_first["signal_id"]
    assert len(mcp_store.added) == 1


def test_a_replay_does_not_spend_the_ceiling() -> None:
    """A retry is not new work, so it must not charge the source's window.

    Otherwise a client with a flaky connection trips its own source's breaker by
    succeeding slowly.
    """
    store, governance = _Store(), _FakeGovernance(_policy())
    _call_rest(store, governance, _body())
    _call_rest(store, governance, _body())
    assert governance.admit_calls == [_SOURCE]


def test_a_fresh_submission_key_for_one_occurrence_still_replays() -> None:
    """Two questions, not one: "the same thing happened" and "the same request".

    The ledger enforces both keys. A source redelivering one occurrence under a
    new submission key must not create a second row for it.
    """
    store, governance = _Store(), _FakeGovernance(_policy())
    _, first = _call_rest(store, governance, _body())
    _, second = _call_rest(store, governance, _body(idempotency_key="delivery-retry-1"))

    assert second["replayed"] is True
    assert second["signal_id"] == first["signal_id"]
    assert len(store.added) == 1


def test_changed_content_under_a_used_key_conflicts_on_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither overwriting the stored observation nor storing a second is true."""
    rest_store, rest_gov = _Store(), _FakeGovernance(_policy())
    _call_rest(rest_store, rest_gov, _body())
    with pytest.raises(HTTPException) as rest_raised:
        _call_rest(rest_store, rest_gov, _body(payload={"conclusion": "failure"}))
    assert rest_raised.value.status_code == 409
    assert rest_raised.value.detail[0]["code"] == "idempotency_conflict"
    assert len(rest_store.added) == 1

    mcp_store, mcp_gov = _Store(), _FakeGovernance(_policy())
    _call_mcp(mcp_store, mcp_gov, _body(), monkeypatch)
    with pytest.raises(ToolError):
        _call_mcp(mcp_store, mcp_gov, _body(payload={"conclusion": "failure"}), monkeypatch)
    assert len(mcp_store.added) == 1


def test_an_unregistered_source_is_refused_on_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    rest_store, rest_gov = _Store(), _FakeGovernance(None)
    with pytest.raises(HTTPException) as rest_raised:
        _call_rest(rest_store, rest_gov, _body())
    assert rest_raised.value.status_code == 404
    assert rest_store.added == []
    # The ceiling is never consulted: a submission that will be refused must not
    # spend a window, least of all one belonging to somebody else.
    assert rest_gov.admit_calls == []

    with pytest.raises(ToolError):
        _call_mcp(_Store(), _FakeGovernance(None), _body(), monkeypatch)


def test_another_tenants_source_answers_exactly_as_an_absent_one() -> None:
    """A distinguishable refusal would make a source id an existence oracle."""
    absent_store, absent_gov = _Store(), _FakeGovernance(None)
    with pytest.raises(HTTPException) as absent:
        _call_rest(absent_store, absent_gov, _body())

    foreign_store, foreign_gov = _Store(), _FakeGovernance(_policy(tenant_id=_OTHER_TENANT))
    with pytest.raises(HTTPException) as foreign:
        _call_rest(foreign_store, foreign_gov, _body())

    assert absent.value.status_code == foreign.value.status_code
    assert str(absent.value.detail) == str(foreign.value.detail)
    assert foreign_store.added == []
    assert foreign_gov.admit_calls == []


def test_a_source_over_its_ceiling_is_refused_on_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """429, not 403: nothing about the submission is wrong, and it will be
    accepted once the window rolls."""
    refused = Admission(permitted=False, reason="circuit open until 2026-03-01T13:00:00+00:00")

    rest_store = _Store()
    with pytest.raises(HTTPException) as rest_raised:
        _call_rest(rest_store, _FakeGovernance(_policy(), refused), _body())
    assert rest_raised.value.status_code == 429
    assert rest_raised.value.detail[0]["code"] == "source_ingest_ceiling"
    assert rest_store.added == []

    mcp_store = _Store()
    with pytest.raises(ToolError):
        _call_mcp(mcp_store, _FakeGovernance(_policy(), refused), _body(), monkeypatch)
    assert mcp_store.added == []


# ---------------------------------------------------------------------------
# Validation the envelope owns, reachable identically from both transports.
# ---------------------------------------------------------------------------


def test_a_signal_carries_exactly_one_observation() -> None:
    store, governance = _Store(), _FakeGovernance(_policy())
    with pytest.raises(HTTPException) as both:
        _call_rest(store, governance, _body(payload={"a": 1}, evidence_handle="handle://x"))
    assert both.value.status_code == 422

    with pytest.raises(HTTPException) as neither:
        _call_rest(store, governance, _body(payload=_ABSENT))
    assert neither.value.status_code == 422
    assert store.added == []


def test_a_naive_timestamp_is_refused_on_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    naive = _EVENT_TIME.replace(tzinfo=None).isoformat()
    with pytest.raises(HTTPException) as rest_raised:
        _call_rest(_Store(), _FakeGovernance(_policy()), _body(event_time=naive))
    assert rest_raised.value.status_code == 422

    with pytest.raises(ToolError):
        _call_mcp(_Store(), _FakeGovernance(_policy()), _body(event_time=naive), monkeypatch)


def test_an_oversized_payload_is_refused() -> None:
    oversized = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(HTTPException) as raised:
        _call_rest(_Store(), _FakeGovernance(_policy()), _body(payload=oversized))
    assert raised.value.status_code == 422


def test_too_many_references_is_refused() -> None:
    reference = {
        "source_system": "github",
        "source_namespace": "acme/app",
        "kind": "commit",
        "classification": "internal",
        "external_authority": "github",
    }
    many = [{**reference, "external_id": f"sha-{index}"} for index in range(MAX_REFERENCES + 1)]
    # A request-shape rule, so pydantic raises before the envelope is built.
    with pytest.raises(Exception) as raised:
        SignalIngestRequest(**_body(references=many))
    assert "references" in str(raised.value)


def test_an_empty_payload_reports_nothing_and_is_refused() -> None:
    """An empty object satisfies "a payload is present" and carries no observation."""
    with pytest.raises(HTTPException) as raised:
        _call_rest(_Store(), _FakeGovernance(_policy()), _body(payload={}))
    assert raised.value.status_code == 422


def test_a_participant_may_only_report_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """`producer_id` is attribution for a human or agent signal, so it is checked.

    An external system's own id for its runner is unverifiable here and is left
    alone; a `human` or `agent` producer names a participant of this deployment,
    and one actor filing observations under another's name would leave the
    reporter's identity nowhere in the row.
    """
    impostor = _body(producer_type="human", producer_id=str(uuid.uuid4()))
    rest_store = _Store()
    with pytest.raises(HTTPException) as raised:
        _call_rest(rest_store, _FakeGovernance(_policy()), impostor)
    assert raised.value.status_code == 422
    assert rest_store.added == []

    mcp_store = _Store()
    with pytest.raises(ToolError):
        _call_mcp(mcp_store, _FakeGovernance(_policy()), impostor, monkeypatch)
    assert mcp_store.added == []

    # The caller's own id is accepted, and an external producer id is untouched.
    _, own = _call_rest(_Store(), _FakeGovernance(_policy()), _body(producer_type="human", producer_id=str(_ACTOR)))
    assert own["replayed"] is False


def test_a_producer_identity_check_runs_before_the_ceiling() -> None:
    """A refusal that will never be stored must not spend the source's window."""
    governance = _FakeGovernance(_policy())
    with pytest.raises(HTTPException):
        _call_rest(_Store(), governance, _body(producer_type="agent", producer_id="not-the-caller"))
    assert governance.admit_calls == []


def test_an_unknown_schema_version_is_refused() -> None:
    with pytest.raises(HTTPException) as raised:
        _call_rest(_Store(), _FakeGovernance(_policy()), _body(schema_version="external_signal.v9"))
    assert raised.value.status_code == 422


# ---------------------------------------------------------------------------
# Normalized references.
# ---------------------------------------------------------------------------


def test_two_spellings_of_one_reference_are_one_submission() -> None:
    """Normalization runs before the digest, or a respelled redelivery conflicts.

    The second call spells the same reference in a different case. If it were
    normalized after the digest were taken, this would read as changed content
    and be refused as a conflict -- for a submission reporting the identical
    observation.
    """
    store, governance = _Store(), _FakeGovernance(_policy())
    _, first = _call_rest(store, governance, _body())
    respelled = [
        {
            "source_system": "GitHub",
            "source_namespace": "Acme/App",
            "kind": "RUN",
            "external_id": "9182",
            "classification": "internal",
            "external_authority": "github-actions",
        }
    ]
    _, second = _call_rest(store, governance, _body(references=respelled))

    assert second["replayed"] is True
    assert second["content_digest"] == first["content_digest"]


def test_a_duplicated_reference_folds_to_one() -> None:
    duplicated = [
        {
            "source_system": "github",
            "source_namespace": "acme/app",
            "kind": "run",
            "external_id": "9182",
            "classification": "internal",
            "external_authority": "github-actions",
        },
        {
            "source_system": "GITHUB",
            "source_namespace": "acme/app",
            "kind": "run",
            "external_id": "9182",
            "classification": "internal",
            "external_authority": "github-actions",
        },
    ]
    _, body = _call_rest(_Store(), _FakeGovernance(_policy()), _body(references=duplicated))
    assert len(body["references"]) == 1


def test_every_returned_reference_carries_its_collision_key() -> None:
    """A producer correlating its own references should not have to reimplement
    the identity this service matched on."""
    _, body = _call_rest(_Store(), _FakeGovernance(_policy()), _body())
    assert all(reference["collision_key"] for reference in body["references"])


# ---------------------------------------------------------------------------
# The lookup predicate, checked structurally.
# ---------------------------------------------------------------------------


def test_the_replay_lookup_names_both_unique_keys_and_scopes_by_tenant() -> None:
    """The one part of this surface no fake can prove by behaviour.

    Consulting only the submission key would let a redelivered occurrence create
    a second row; omitting the tenant or producer scope would let one tenant's
    submission collide with another's.
    """
    store = _Store()
    _call_rest(store, _FakeGovernance(_policy()), _body())
    compiled = str(store.statements[0].compile(dialect=postgresql.dialect()))
    for column in ("tenant_id", "producer_id", "source_event_id", "idempotency_key"):
        assert column in compiled, f"the replay lookup does not mention {column}"


def test_a_lost_insert_race_resolves_to_the_row_that_won() -> None:
    """Two submissions of one observation, concurrent, must not answer 500.

    The read that decides replay happens before the insert, so two callers can
    both see nothing and both insert. The ledger's unique keys refuse the loser,
    and the loser then finds the winner's row -- which is exactly what a
    concurrent double-submit is. Simulated by a session whose first commit raises
    the integrity error Postgres would raise.
    """
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
        content_digest="",
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

    class _RacingSession(_FakeSession):
        raised = False

        async def __aexit__(self, *exc: object) -> None:
            if not _RacingSession.raised and self._store.added:
                _RacingSession.raised = True
                # The loser's own staged row never lands, so drop it before the
                # re-read: leaving it would make the re-read find the loser's
                # digest rather than the winner's.
                self._store.added.clear()
                self._store.rows = [row for row in self._store.rows if row is not None and row.content_digest != ""]
                self._store.rows = [winner]
                raise IntegrityError("duplicate key", None, Exception("unique violation"))

    store = _Store()

    def factory() -> _RacingSession:
        return _RacingSession(store)

    container = SimpleNamespace(
        session_factory=factory,
        clock=FakeClock(_NOW),
        source_governance=_FakeGovernance(_policy()),
    )
    # The winner's stored digest has to be the digest this submission computes,
    # or the re-read would (correctly) call it a changed replay.
    request = SignalIngestRequest(**_body())
    envelope = signal_router.ExternalSignalEnvelopeV1(
        source_id=request.source_id,
        source_system=request.source_system,
        source_event_id=request.source_event_id,
        producer_id=request.producer_id,
        producer_type=request.producer_type,
        idempotency_key=request.idempotency_key,
        classification=request.classification,
        schema_version=request.schema_version,
        event_time=request.event_time,
        observed_time=request.observed_time,
        references=signal_router.normalize_references([r.model_dump() for r in request.references]),
        payload=request.payload,
    )
    winner.content_digest = content_digest_for(envelope)

    response = Response()
    result = asyncio.run(signal_router.ingest_signal(body=request, ctx=_ctx(), container=container, response=response))
    assert _RacingSession.raised is True
    assert result.replayed is True
    assert result.signal_id == winner.signal_id
    assert response.status_code == 200


def test_the_service_refuses_an_authority_tier_off_the_ladder() -> None:
    """A tier outside the ladder ranks against nothing, so every later conflict
    involving the signal would be decided by an ordering that does not exist."""
    store = _Store()
    with pytest.raises(HTTPException) as raised:
        _call_rest(store, _FakeGovernance(_policy(tier="inventedtier")), _body())
    assert raised.value.status_code == 422
    assert store.added == []
