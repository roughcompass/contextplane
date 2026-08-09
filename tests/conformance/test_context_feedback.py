"""Feedback binds to exactly what it is about, or it is not stored at all.

The rule this suite exists to hold is narrow and easy to lose: feedback that
cannot be tied to what it is about must not become evidence about anything. So
every refusal below is checked twice — that the caller was refused, *and* that
nothing reached the ledger. A surface that returns 404 and writes the row anyway
passes the first check and fails the property.

Both transports are driven for every rule that both can express, because a rule
enforced in two adapters is one that will eventually be enforced differently in
one of them.
"""

from __future__ import annotations

import asyncio
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

from contextplane.api.mcp import context as mcp_context
from contextplane.api.mcp.tools import context_feedback as feedback_tools
from contextplane.api.routers import context_feedback as feedback_router
from contextplane.signals import feedback as feedback_service
from contextplane.types import TenantContext
from tests.conftest import FakeClock

_TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
_ACTOR = uuid.UUID("33333333-3333-4333-8333-333333333333")
_RECEIPT = uuid.UUID("44444444-4444-4444-8444-444444444444")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, 0, tzinfo=datetime.UTC)
_ITEM = "sha256:item-on-the-receipt"
_REST_PATH = "/v1/context/feedback"

# Tables no feedback write may ever touch. Feedback is an observation about a
# served answer; turning one into derivation work is a decision the curation path
# makes later, with evidence, and never a side effect of somebody complaining.
_DERIVATION_TABLES = ("claim_derivations", "derivation_evidence_links", "derivative_work_outbox")


def _ctx(tenant_id: uuid.UUID = _TENANT) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=_ACTOR, roles=["consumer"])


# ---------------------------------------------------------------------------
# Fakes. Small on purpose: each stands in for a collaborator proven elsewhere,
# so a failure here is a failure of this surface.
# ---------------------------------------------------------------------------


class _Row(SimpleNamespace):
    """A result row, addressed by attribute the way the service reads them."""


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Store:
    """What the fake session was asked to do.

    `inserts` is the one that matters: every "nothing was written" assertion in
    this file means nothing was inserted into `context_feedback`, and a refusal
    that still inserted would show up here rather than passing quietly.
    """

    def __init__(
        self,
        *,
        receipts: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
        items: set[tuple[uuid.UUID, str]] | None = None,
        stored: list[_Row] | None = None,
    ) -> None:
        # (receipt_id, tenant_id) pairs that exist. The tenant is part of the key
        # because the service's read carries the tenant predicate in the SELECT.
        self.receipts = receipts if receipts is not None else {(_RECEIPT, _TENANT)}
        self.items = items if items is not None else {(_RECEIPT, _ITEM)}
        self.stored: list[_Row] = list(stored or [])
        self.inserts: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.commits = 0

    def touched(self, table: str) -> bool:
        return any(table in statement for statement in self.statements)


class _FakeSession:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        rendered = str(stmt)
        self._store.statements.append(rendered)

        if "INSERT INTO context_feedback" in rendered:
            self._store.inserts.append(dict(params or {}))
            row = _Row(
                feedback_id=(params or {})["fid"],
                kind=(params or {})["kind"],
                rating=(params or {})["rating"],
                learning_eligible=(params or {})["elig"],
                receipt_id=(params or {})["rid"],
                receipt_item_id=(params or {})["iid"],
                content_digest=(params or {})["dig"],
                created_at=(params or {})["created"],
            )
            self._store.stored.append(row)
            return _Result([])

        if "FROM context_feedback" in rendered:
            key = (params or {}).get("idk")
            reporter = (params or {}).get("rep")
            return _Result(
                [r for r in self._store.stored if getattr(r, "idempotency_key", key) == key and reporter is not None][
                    :1
                ]
            )

        # The two binding reads are SQLAlchemy selects, not text; identify them
        # by the column each projects.
        if "context_receipts.receipt_id" in rendered:
            wanted = _binding.get("receipt"), _binding.get("tenant")
            return _Result([wanted[0]] if (wanted[0], wanted[1]) in self._store.receipts else [])
        if "context_receipt_items.receipt_item_id" in rendered:
            pair = (_binding.get("receipt"), _binding.get("item"))
            return _Result([pair[1]] if pair in self._store.items else [])
        return _Result([])

    async def commit(self) -> None:
        self._store.commits += 1

    async def rollback(self) -> None:
        return None

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


# The binding the service is currently resolving. The fake session cannot read
# bound parameters off a SQLAlchemy select the way it can off a text statement,
# so the submission under test publishes them here. Set by `_call_rest`/`_call_mcp`.
_binding: dict[str, Any] = {}


def _session_factory(store: _Store) -> Any:
    def factory() -> _FakeSession:
        return _FakeSession(store)

    return factory


def _container(store: _Store) -> Any:
    return SimpleNamespace(session_factory=_session_factory(store), clock=FakeClock(_NOW))


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "item_specific",
        "rating": "stale",
        "reporter_id": str(_ACTOR),
        "reporter_type": "human",
        "idempotency_key": f"fb-{uuid.uuid4().hex[:10]}",
        "receipt_id": str(_RECEIPT),
        "receipt_item_id": _ITEM,
    }
    body.update(overrides)
    return body


def _publish_binding(body: dict[str, Any], tenant: uuid.UUID) -> None:
    _binding.clear()
    _binding.update(
        {
            "receipt": uuid.UUID(body["receipt_id"]) if body.get("receipt_id") else None,
            "item": body.get("receipt_item_id"),
            "tenant": tenant,
        }
    )


def _call_rest(store: _Store, body: dict[str, Any], tenant: uuid.UUID = _TENANT) -> tuple[int, dict[str, Any]]:
    """Drive the REST route function directly; returns (status, body)."""
    _publish_binding(body, tenant)
    response = Response()
    result = asyncio.run(
        feedback_router.record_context_feedback(
            body=feedback_router.ContextFeedbackRequest(**body),
            ctx=_ctx(tenant),
            container=_container(store),
            response=response,
        )
    )
    return response.status_code, result.model_dump(mode="json")


def _call_mcp(
    store: _Store,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tenant: uuid.UUID = _TENANT,
) -> dict[str, Any]:
    """Drive the MCP tool coroutine directly; returns the decoded JSON."""
    _publish_binding(body, tenant)

    async def _resolve(*_args: object, **_kwargs: object) -> TenantContext:
        return _ctx(tenant)

    monkeypatch.setattr(mcp_context, "_resolve_tenant", _resolve)
    raw = asyncio.run(
        feedback_tools.record_context_feedback(
            session_factory=_session_factory(store),
            clock=FakeClock(_NOW),
            kind=body["kind"],
            rating=body["rating"],
            reporter_id=body["reporter_id"],
            reporter_type=body["reporter_type"],
            idempotency_key=body["idempotency_key"],
            receipt_id=body.get("receipt_id"),
            receipt_item_id=body.get("receipt_item_id"),
            note=body.get("note"),
            learning_eligible=body.get("learning_eligible", True),
        )
    )
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


# ---------------------------------------------------------------------------
# The surfaces exist, and are mounted.
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


def test_context_feedback_is_served_over_rest() -> None:
    paths = {route.path for route in feedback_router.router.routes}
    assert _REST_PATH in paths, f"{_REST_PATH} is not served; the paths are {sorted(paths)}"


def test_the_context_feedback_router_is_mounted_on_the_app() -> None:
    """An unmounted router passes every shape check and serves nothing.

    Read off the composition root's own module rather than a constructed app,
    which would need a database URL to answer a question about wiring.
    """
    from contextplane import wiring

    text = (Path(wiring.__path__[0]) / "routes.py").read_text(encoding="utf-8")
    assert "context_feedback_router" in text, "wiring/routes.py does not import the feedback router"
    assert (
        "app.include_router(context_feedback_router.router)" in text
    ), "the feedback router is imported but never mounted"


def test_context_feedback_is_served_over_mcp(mcp_tools: dict[str, Any]) -> None:
    assert "record_context_feedback" in mcp_tools, f"tool not registered; tools are {sorted(mcp_tools)}"


def test_the_context_feedback_tool_takes_no_signal_parameter(mcp_tools: dict[str, Any]) -> None:
    """An implicit external outcome is not a rating, and the schema cannot accept one.

    Joining a failed build to a verdict on a served answer is a claim nobody made.
    """
    schema = getattr(mcp_tools["record_context_feedback"], "inputSchema", None) or {}
    assert "signal_id" not in schema.get("properties", {}), "the tool would let an observation be filed as a rating"


# ---------------------------------------------------------------------------
# The union is closed, on both surfaces.
# ---------------------------------------------------------------------------


def test_item_specific_feedback_is_stored_with_its_exact_item() -> None:
    store = _Store()
    code, body = _call_rest(store, _body())
    assert code == 201
    assert body["kind"] == "item_specific"
    assert body["receipt_item_id"] == _ITEM
    assert len(store.inserts) == 1


def test_receipt_level_feedback_is_stored_without_an_item() -> None:
    store = _Store()
    code, body = _call_rest(store, _body(kind="receipt_level", receipt_item_id=None))
    assert code == 201
    assert body["receipt_item_id"] is None
    assert len(store.inserts) == 1


def test_diagnostic_feedback_is_stored_citing_nothing() -> None:
    store = _Store()
    code, body = _call_rest(store, _body(kind="diagnostic_observation", receipt_id=None, receipt_item_id=None))
    assert code == 201
    assert body["receipt_id"] is None
    assert body["receipt_item_id"] is None


def test_item_specific_feedback_without_an_item_is_refused_and_writes_nothing() -> None:
    store = _Store()
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(receipt_item_id=None))
    assert caught.value.status_code == 422
    assert store.inserts == []


def test_receipt_level_feedback_naming_an_item_is_refused_and_writes_nothing() -> None:
    """Feedback about a whole answer is not evidence about any one line of it."""
    store = _Store()
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(kind="receipt_level"))
    assert caught.value.status_code == 422
    assert store.inserts == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"receipt_id": str(_RECEIPT), "receipt_item_id": None}, id="names-a-receipt"),
        pytest.param({"receipt_id": str(_RECEIPT), "receipt_item_id": _ITEM}, id="names-an-item"),
    ],
)
def test_diagnostic_feedback_that_cites_anything_is_refused(overrides: dict[str, Any]) -> None:
    store = _Store()
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(kind="diagnostic_observation", **overrides))
    assert caught.value.status_code == 422
    assert store.inserts == []


def test_a_diagnostic_observation_can_never_be_learning_eligible() -> None:
    """Forced to false rather than refused: the caller never set it, and it defaults true.

    It cites nothing, so nothing can check what it refers to; admitting one to the
    derivation path would let an unattributable complaint become evidence about a
    specific retrieved item.
    """
    store = _Store()
    _, body = _call_rest(
        store,
        _body(kind="diagnostic_observation", receipt_id=None, receipt_item_id=None, learning_eligible=True),
    )
    assert body["learning_eligible"] is False
    assert store.inserts[0]["elig"] is False


def test_a_reporter_may_withhold_bound_feedback_from_learning() -> None:
    """The other direction is legitimate and must keep working."""
    store = _Store()
    _, body = _call_rest(store, _body(learning_eligible=False))
    assert body["learning_eligible"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("kind", "vibes", id="kind"),
        pytest.param("rating", "meh", id="rating"),
        pytest.param("reporter_type", "committee", id="reporter_type"),
    ],
)
def test_a_value_outside_the_vocabulary_is_refused_and_writes_nothing(field: str, value: str) -> None:
    store = _Store()
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(**{field: value}))
    assert caught.value.status_code == 422
    assert store.inserts == []


# ---------------------------------------------------------------------------
# Planted bindings: unauthorized, mismatched, absent. None may write.
# ---------------------------------------------------------------------------


def test_another_tenants_receipt_answers_exactly_as_an_absent_one() -> None:
    """A distinguishable refusal would turn a receipt id into a cross-tenant oracle."""
    absent = _Store(receipts=set())
    with pytest.raises(HTTPException) as absent_caught:
        _call_rest(absent, _body())

    # The receipt exists, but belongs to somebody else: the read carries the
    # tenant predicate, so it comes back empty exactly as an unknown id does.
    foreign = _Store(receipts={(_RECEIPT, _OTHER_TENANT)})
    with pytest.raises(HTTPException) as foreign_caught:
        _call_rest(foreign, _body())

    assert absent_caught.value.status_code == foreign_caught.value.status_code == 404
    assert absent_caught.value.detail == foreign_caught.value.detail
    assert absent.inserts == foreign.inserts == []


def test_an_item_from_another_receipt_is_refused_and_writes_nothing() -> None:
    """The mismatched binding: both ids exist, and the pair is still wrong.

    This is the case a single-column check stores happily — the receipt is the
    caller's and the item is a real item, just not one on this receipt.
    """
    store = _Store(items=set())
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(receipt_item_id="sha256:item-on-a-different-receipt"))
    assert caught.value.status_code == 404
    assert store.inserts == []


def test_an_unauthorized_receipt_is_refused_before_the_item_is_looked_at() -> None:
    """Order is the security property: an item id must not be probeable against
    a receipt the caller may not see."""
    store = _Store(receipts=set(), items=set())
    with pytest.raises(HTTPException):
        _call_rest(store, _body())
    assert not any(
        "context_receipt_items" in statement for statement in store.statements
    ), "the item was read despite the receipt being unauthorized"


def test_a_reporter_may_not_file_feedback_under_another_identity() -> None:
    """Attribution that names somebody else is worse than anonymous: it looks attributed."""
    store = _Store()
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, _body(reporter_id=str(uuid.uuid4())))
    assert caught.value.status_code == 422
    assert store.inserts == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"receipt_item_id": None}, id="shape"),
        pytest.param({"rating": "meh"}, id="vocabulary"),
        pytest.param({"receipt_item_id": "sha256:not-on-this-receipt"}, id="mismatched-binding"),
    ],
)
def test_a_refused_submission_queues_no_derivation_work(overrides: dict[str, Any]) -> None:
    """The property the whole surface exists to protect.

    Feedback that could not be bound must not become evidence about anything —
    not as a row, and not as work for the derivation path to pick up later.
    """
    store = _Store(items=set()) if "receipt_item_id" in overrides and overrides["receipt_item_id"] else _Store()
    with pytest.raises(HTTPException):
        _call_rest(store, _body(**overrides))
    assert store.inserts == []
    for table in _DERIVATION_TABLES:
        assert not store.touched(table), f"a refused submission touched {table}"


def test_an_accepted_submission_queues_no_derivation_work_either() -> None:
    """Accepted feedback is still only an observation.

    Turning one into derivation work is a decision the curation path makes later,
    with evidence — never a side effect of somebody complaining.
    """
    store = _Store()
    _call_rest(store, _body())
    for table in _DERIVATION_TABLES:
        assert not store.touched(table), f"an accepted submission touched {table}"


# ---------------------------------------------------------------------------
# Replay, conflict, and parity between the two surfaces.
# ---------------------------------------------------------------------------


def test_an_exact_replay_is_recognised_rather_than_stored_twice() -> None:
    store = _Store()
    body = _body()
    first_code, first = _call_rest(store, body)
    second_code, second = _call_rest(store, body)

    assert first_code == 201
    assert first["replayed"] is False
    assert second_code == 200
    assert second["replayed"] is True
    assert second["feedback_id"] == first["feedback_id"]
    assert len(store.inserts) == 1, "the replay stored a second row"


def test_a_reused_key_carrying_different_feedback_conflicts() -> None:
    """Both cannot be true: storing a second row would leave two contradictory
    reports under one identity, and overwriting would discard the first."""
    store = _Store()
    body = _body()
    _call_rest(store, body)
    with pytest.raises(HTTPException) as caught:
        _call_rest(store, {**body, "rating": "incorrect"})
    assert caught.value.status_code == 409
    assert caught.value.detail[0]["code"] == "idempotency_conflict"
    assert len(store.inserts) == 1


def test_both_surfaces_return_the_same_body_for_the_same_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule enforced in two adapters eventually differs in one of them."""
    rest_store = _Store()
    mcp_store = _Store()
    body = _body(idempotency_key="fb-parity-0001")

    _, rest_body = _call_rest(rest_store, body)
    mcp_body = _call_mcp(mcp_store, body, monkeypatch)

    # The ids differ by construction; everything describing the submission must not.
    for field in ("kind", "rating", "learning_eligible", "receipt_id", "receipt_item_id", "content_digest", "replayed"):
        assert rest_body[field] == mcp_body[field], f"{field} differs between the surfaces"


def test_both_surfaces_refuse_a_mismatched_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    rest_store = _Store(items=set())
    with pytest.raises(HTTPException):
        _call_rest(rest_store, _body())

    mcp_store = _Store(items=set())
    # The MCP surface puts the service's NotFoundError through the shared
    # translator, which is what turns it into a `ToolError` -- the same refusal,
    # spelled the way that transport spells refusals. Asserting the mapped type
    # rather than the domain one is the point: a tool that let a raw
    # `CatalogError` escape would reach the agent as an internal failure.
    with pytest.raises(ToolError):
        _call_mcp(mcp_store, _body(), monkeypatch)

    assert rest_store.inserts == []
    assert mcp_store.inserts == []


def test_both_surfaces_force_a_diagnostic_to_be_ineligible(monkeypatch: pytest.MonkeyPatch) -> None:
    diagnostic = _body(kind="diagnostic_observation", receipt_id=None, receipt_item_id=None, learning_eligible=True)

    _, rest_body = _call_rest(_Store(), diagnostic)
    mcp_body = _call_mcp(_Store(), diagnostic, monkeypatch)

    assert rest_body["learning_eligible"] is False
    assert mcp_body["learning_eligible"] is False


# ---------------------------------------------------------------------------
# The digest, and the metric families.
# ---------------------------------------------------------------------------


def test_the_content_digest_covers_what_is_asserted_not_the_submission_key() -> None:
    """Folding the key into the digest would make every retry under a fresh key
    look like changed content."""
    one = feedback_service.FeedbackSubmissionV1(
        kind="receipt_level",
        rating="stale",
        reporter_id=str(_ACTOR),
        reporter_type="human",
        idempotency_key="fb-a",
        receipt_id=_RECEIPT,
    )
    other = feedback_service.FeedbackSubmissionV1(**{**vars(one), "idempotency_key": "fb-b"})
    changed = feedback_service.FeedbackSubmissionV1(**{**vars(one), "rating": "incorrect"})

    assert feedback_service.content_digest_for(one) == feedback_service.content_digest_for(other)
    assert feedback_service.content_digest_for(one) != feedback_service.content_digest_for(changed)


def test_acceptance_and_refusal_are_separate_metric_families() -> None:
    """One counter with an outcome label would make "how much feedback are we
    taking" and "how often is a caller getting the binding wrong" the same query.

    The second is the one worth alerting on: a client that has started sending
    mismatched bindings is broken in a way no amount of accepted feedback offsets.
    """
    names = {
        feedback_service.FEEDBACK_ACCEPTED_TOTAL._name,
        feedback_service.FEEDBACK_REPLAYED_TOTAL._name,
        feedback_service.FEEDBACK_REFUSED_TOTAL._name,
    }
    assert len(names) == 3, f"the families collapsed into {names}"
    assert feedback_service.FEEDBACK_REFUSED_TOTAL._labelnames == ("reason",)


def test_the_vocabulary_is_closed_and_matches_what_the_schema_stores() -> None:
    """The service and the migration close the same set.

    Both are deliberate: the database refuses a bad row from any writer, and the
    service refuses a bad *request* with an error the caller can act on.
    """
    migration = (
        Path(__file__).parent.parent.parent
        / "contextplane"
        / "storage"
        / "migrations"
        / "versions"
        / "0041_discriminated_feedback.py"
    ).read_text(encoding="utf-8")
    for rating in feedback_service.RATINGS:
        assert f"'{rating}'" in migration, f"the service accepts {rating!r} but the schema would refuse it"
    for kind in feedback_service.FEEDBACK_KINDS:
        assert f"'{kind}'" in migration, f"the service accepts kind {kind!r} but the schema would refuse it"
