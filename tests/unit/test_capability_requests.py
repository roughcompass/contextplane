"""Unit tests for `CapabilityRequestService` (contextplane.service.memory.capability_requests).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session,
mirroring `tests/unit/test_promotion.py`'s pattern -- no Postgres required.
Before this file, the module's ~41% unit-scope coverage came entirely from
other files (`test_check_memory_reachability.py`, the router/MCP-tool mocked
tests) importing its dataclasses as fixtures, never from a suite exercising
`raise_request`/`transition`/`link_to_promotion`'s own lifecycle logic --
this file is that suite.

Coverage:
- `raise_request`: category/empty-field validation, the not-found-is-invisible
  refusal when the subject entity does not resolve, the owner-resolved insert
  (including the `cross_tenant` audit flag), and the raised-counter increment.
- `transition`: the three separate guards (`_locked`'s not-found, the
  owning-tenant check, the deciding-role check) kept independently testable
  per the module's own docstring, the closed-transition-table refusal, the
  reason-required refusal for `declined`/`duplicate`, and the success path's
  update + append-only history row + audit action + decided-counter label,
  reloaded through `get`.
- `link_to_promotion`: the owning-tenant guard and the accepted-or-resolved
  guard, and the successful write.
- Reads: `get`'s owner-or-requester visibility, `for_owner`/`raised_by`'s
  keyset cursor construction and `for_owner`'s `open_only` default, and
  `for_subject`'s owner-or-requester involvement scoping.
- `CapabilityRequest.is_open`, the pure dataclass property.

The `# pragma: no cover - written in this transaction` guard in `transition`
(the request "vanishing" between the update and the reload) is not exercised
here, matching how this repo's other tracks treat the same defensive-only
shape elsewhere (`promotion.py`, `source_governance.py`).
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.capability_requests import (
    ALLOWED_TRANSITIONS,
    STATUS_ACCEPTED,
    STATUS_ACKNOWLEDGED,
    STATUS_DECLINED,
    STATUS_DUPLICATE,
    STATUS_RAISED,
    STATUS_RESOLVED,
    CapabilityRequest,
    CapabilityRequestService,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _audit_payload(audit_params: dict[str, Any]) -> dict[str, Any]:
    """`_audit`'s own params carry the payload pre-serialized under `after`
    (`json.dumps(payload, ...)`), matching how it is actually bound into the
    `audit_log` insert -- decode it rather than asserting on a `payload` key
    that was never a param name."""
    return json.loads(audit_params["after"])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Shared helpers -- same shape as tests/unit/test_promotion.py
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _scalar_one_or_none(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _mapping_first(row: dict[str, Any] | None) -> MagicMock:
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    return result


def _mapping_all(rows: list[dict[str, Any]]) -> MagicMock:
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return result


class _SqlRouter:
    """Dispatches `session.execute` calls to a canned response by matching a
    substring against the flattened SQL text, first match in registration
    order wins. Register the more specific substrings first when two
    statements share a common fragment (e.g. an UPDATE's own `WHERE
    request_id = :rid` vs. a plain SELECT's). Records every `(sql, params)`
    pair so a test can assert on how a query was built, not only its result.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._routes: list[tuple[str, Any]] = []

    def route(self, substring: str, response: Any) -> _SqlRouter:
        self._routes.append((substring, response))
        return self

    async def __call__(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        self.calls.append((sql, params))
        for substring, response in self._routes:
            if substring in sql:
                # `_Dynamic` is the only response type ever invoked -- a bare
                # MagicMock is itself callable, so testing for "callable" here
                # would call *it* too and return its auto-vivified
                # `.return_value` instead of the crafted mock.
                return response.fn(params) if isinstance(response, _Dynamic) else response
        raise AssertionError(f"unexpected SQL in test session: {sql}")


class _Dynamic:
    """Wraps a response callback that needs the query's `params` (to record
    them, or to branch the response) -- see `_SqlRouter.__call__`."""

    def __init__(self, fn: Any) -> None:
        self.fn = fn


def _session_factory(execute: Any) -> MagicMock:
    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory


def _ctx(tenant_id: uuid.UUID | None = None, roles: tuple[str, ...] = ("producer",)) -> Any:
    ctx = MagicMock()
    ctx.tenant_id = tenant_id if tenant_id is not None else uuid.uuid4()
    ctx.actor_id = uuid.uuid4()
    ctx.roles = list(roles)
    return ctx


def _request_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_id": uuid.uuid4(),
        "owner_tenant_id": uuid.uuid4(),
        "requester_tenant_id": uuid.uuid4(),
        "subject_entity_id": uuid.uuid4(),
        "request_category": "new_capability",
        "title": "Need a bulk export endpoint",
        "body": "We need to export the full dataset nightly.",
        "status": STATUS_RAISED,
        "decision_reason": None,
        "resulting_promotion_id": None,
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


def _service(router: _SqlRouter) -> CapabilityRequestService:
    return CapabilityRequestService(_session_factory(router), clock=FakeClock(_NOW))


# ---------------------------------------------------------------------------
# raise_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raise_request_rejects_an_unknown_category() -> None:
    router = _SqlRouter()  # no route registered -- proves no query is issued
    service = _service(router)

    with pytest.raises(ValidationError, match="request_category"):
        await service.raise_request(
            _ctx(),
            subject_entity_id=uuid.uuid4(),
            request_category="not_a_real_category",
            title="t",
            body="b",
        )
    assert router.calls == []


@pytest.mark.asyncio
async def test_raise_request_rejects_an_empty_title() -> None:
    router = _SqlRouter()
    service = _service(router)

    with pytest.raises(ValidationError, match="title"):
        await service.raise_request(
            _ctx(),
            subject_entity_id=uuid.uuid4(),
            request_category="defect",
            title="   ",
            body="a real body",
        )
    assert router.calls == []


@pytest.mark.asyncio
async def test_raise_request_raises_not_found_when_the_subject_does_not_resolve() -> None:
    router = _SqlRouter().route("FROM entities WHERE entity_id", _scalar_one_or_none(None))
    service = _service(router)

    with pytest.raises(NotFoundError):
        await service.raise_request(
            _ctx(),
            subject_entity_id=uuid.uuid4(),
            request_category="defect",
            title="t",
            body="b",
        )


@pytest.mark.asyncio
async def test_raise_request_resolves_the_owner_from_the_subject_not_the_caller() -> None:
    owner_tenant_id = uuid.uuid4()
    requester_ctx = _ctx()
    subject_entity_id = uuid.uuid4()
    insert_calls: list[dict[str, Any] | None] = []
    audit_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FROM entities WHERE entity_id", _scalar_one_or_none(owner_tenant_id))
        .route(
            "INSERT INTO memory_capability_request",
            _Dynamic(lambda params: (insert_calls.append(params), MagicMock())[1]),
        )
        .route("INSERT INTO audit_log", _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]))
    )
    service = _service(router)
    before = _sample("contextplane_capability_request_raised_total")

    result = await service.raise_request(
        requester_ctx,
        subject_entity_id=subject_entity_id,
        request_category="new_capability",
        title="Need a bulk export endpoint",
        body="Nightly export, please.",
    )

    assert result.owner_tenant_id == owner_tenant_id
    assert result.requester_tenant_id == requester_ctx.tenant_id
    assert result.status == STATUS_RAISED
    assert result.decision_reason is None
    assert result.resulting_promotion_id is None
    assert insert_calls[0]["owner"] == owner_tenant_id
    assert insert_calls[0]["requester"] == requester_ctx.tenant_id
    assert audit_calls[0]["action"] == actions.REQUEST_RAISED
    assert audit_calls[0]["tid"] == owner_tenant_id
    assert _audit_payload(audit_calls[0])["cross_tenant"] is True
    assert _sample("contextplane_capability_request_raised_total") == before + 1


@pytest.mark.asyncio
async def test_raise_request_marks_cross_tenant_false_when_the_owner_requests_its_own_capability() -> None:
    same_tenant = uuid.uuid4()
    ctx = _ctx(tenant_id=same_tenant)
    audit_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FROM entities WHERE entity_id", _scalar_one_or_none(same_tenant))
        .route("INSERT INTO memory_capability_request", MagicMock())
        .route("INSERT INTO audit_log", _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]))
    )
    service = _service(router)

    await service.raise_request(
        ctx,
        subject_entity_id=uuid.uuid4(),
        request_category="documentation",
        title="t",
        body="b",
    )

    assert _audit_payload(audit_calls[0])["cross_tenant"] is False


# ---------------------------------------------------------------------------
# transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_raises_not_found_for_a_missing_request() -> None:
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(None))
    service = _service(router)

    with pytest.raises(NotFoundError):
        await service.transition(_ctx(), request_id=uuid.uuid4(), to_status=STATUS_ACKNOWLEDGED)


@pytest.mark.asyncio
async def test_transition_denies_a_tenant_that_does_not_own_the_capability() -> None:
    row = _request_row(status=STATUS_RAISED)
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(PermissionError, match="owns the capability"):
        await service.transition(
            _ctx(tenant_id=uuid.uuid4()),  # some other tenant, never the owner
            request_id=row["request_id"],
            to_status=STATUS_ACKNOWLEDGED,
        )


@pytest.mark.asyncio
async def test_transition_denies_an_actor_without_a_deciding_role() -> None:
    row = _request_row(status=STATUS_RAISED)
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(PermissionError, match="producer or admin"):
        await service.transition(
            _ctx(tenant_id=row["owner_tenant_id"], roles=("consumer",)),
            request_id=row["request_id"],
            to_status=STATUS_ACKNOWLEDGED,
        )


@pytest.mark.asyncio
async def test_transition_rejects_an_illegal_transition() -> None:
    row = _request_row(status=STATUS_DECLINED)  # terminal -- nothing is allowed from here
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(ConflictError, match="terminal"):
        await service.transition(
            _ctx(tenant_id=row["owner_tenant_id"]),
            request_id=row["request_id"],
            to_status=STATUS_ACKNOWLEDGED,
        )


@pytest.mark.parametrize("terminal_status", [STATUS_DECLINED, STATUS_DUPLICATE])
@pytest.mark.asyncio
async def test_transition_requires_a_reason_for_declined_or_duplicate(terminal_status: str) -> None:
    row = _request_row(status=STATUS_RAISED)
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(ValidationError, match="requires a reason"):
        await service.transition(
            _ctx(tenant_id=row["owner_tenant_id"]),
            request_id=row["request_id"],
            to_status=terminal_status,
            reason="   ",
        )


@pytest.mark.asyncio
async def test_transition_writes_the_update_and_history_row_and_reloads_through_get() -> None:
    row = _request_row(status=STATUS_RAISED)
    owner_ctx = _ctx(tenant_id=row["owner_tenant_id"])
    update_calls: list[dict[str, Any] | None] = []
    transition_calls: list[dict[str, Any] | None] = []
    audit_calls: list[dict[str, Any] | None] = []
    reloaded = _request_row(
        request_id=row["request_id"],
        owner_tenant_id=row["owner_tenant_id"],
        requester_tenant_id=row["requester_tenant_id"],
        status=STATUS_ACKNOWLEDGED,
    )
    router = (
        _SqlRouter()
        .route("FOR UPDATE", _mapping_first(row))
        .route(
            "UPDATE memory_capability_request",
            _Dynamic(lambda params: (update_calls.append(params), MagicMock())[1]),
        )
        .route(
            "INSERT INTO memory_request_transition",
            _Dynamic(lambda params: (transition_calls.append(params), MagicMock())[1]),
        )
        .route("INSERT INTO audit_log", _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]))
        .route("WHERE request_id = :rid", _mapping_first(reloaded))
    )
    service = _service(router)
    before = _sample("contextplane_capability_request_decided_total", to_status=STATUS_ACKNOWLEDGED)

    result = await service.transition(owner_ctx, request_id=row["request_id"], to_status=STATUS_ACKNOWLEDGED)

    assert result.status == STATUS_ACKNOWLEDGED
    assert update_calls[0]["to"] == STATUS_ACKNOWLEDGED
    assert update_calls[0]["rid"] == row["request_id"]
    assert transition_calls[0]["frm"] == STATUS_RAISED
    assert transition_calls[0]["to"] == STATUS_ACKNOWLEDGED
    assert audit_calls[0]["action"] == actions.REQUEST_ACKNOWLEDGED
    assert _sample("contextplane_capability_request_decided_total", to_status=STATUS_ACKNOWLEDGED) == before + 1


def test_every_non_terminal_status_has_an_audit_action_mapped() -> None:
    """Every status `transition` can actually reach needs its own audit
    action -- a status reachable by `ALLOWED_TRANSITIONS` with no entry in
    `_AUDIT_BY_STATUS` would silently fall back to the generic
    `REQUEST_TRANSITIONED` action, losing which specific decision was made."""
    from contextplane.service.memory.capability_requests import _AUDIT_BY_STATUS

    reachable_targets = {to for targets in ALLOWED_TRANSITIONS.values() for to in targets}
    assert reachable_targets <= _AUDIT_BY_STATUS.keys()


# ---------------------------------------------------------------------------
# link_to_promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_to_promotion_denies_a_tenant_that_does_not_own_the_capability() -> None:
    row = _request_row(status=STATUS_ACCEPTED)
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(PermissionError, match="owning tenant"):
        await service.link_to_promotion(
            _ctx(tenant_id=uuid.uuid4()),
            request_id=row["request_id"],
            promotion_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("status", [STATUS_RAISED, STATUS_ACKNOWLEDGED, STATUS_DECLINED, STATUS_DUPLICATE])
@pytest.mark.asyncio
async def test_link_to_promotion_rejects_a_request_that_is_not_accepted_or_resolved(status: str) -> None:
    row = _request_row(status=status)
    router = _SqlRouter().route("FOR UPDATE", _mapping_first(row))
    service = _service(router)

    with pytest.raises(ConflictError, match="only an accepted or resolved"):
        await service.link_to_promotion(
            _ctx(tenant_id=row["owner_tenant_id"]),
            request_id=row["request_id"],
            promotion_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("status", [STATUS_ACCEPTED, STATUS_RESOLVED])
@pytest.mark.asyncio
async def test_link_to_promotion_writes_the_promotion_id_and_audits(status: str) -> None:
    row = _request_row(status=status)
    promotion_id = uuid.uuid4()
    update_calls: list[dict[str, Any] | None] = []
    audit_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FOR UPDATE", _mapping_first(row))
        .route(
            "UPDATE memory_capability_request",
            _Dynamic(lambda params: (update_calls.append(params), MagicMock())[1]),
        )
        .route("INSERT INTO audit_log", _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]))
    )
    service = _service(router)

    await service.link_to_promotion(
        _ctx(tenant_id=row["owner_tenant_id"]),
        request_id=row["request_id"],
        promotion_id=promotion_id,
    )

    assert update_calls[0]["pid"] == promotion_id
    assert audit_calls[0]["action"] == actions.REQUEST_LINKED_TO_CHANGE
    assert _audit_payload(audit_calls[0])["promotion_id"] == str(promotion_id)


# ---------------------------------------------------------------------------
# get / for_owner / raised_by / for_subject / history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_none_when_the_request_does_not_exist() -> None:
    router = _SqlRouter().route("WHERE request_id = :rid", _mapping_first(None))
    service = _service(router)

    assert await service.get(_ctx(), uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_returns_none_for_a_tenant_that_is_neither_owner_nor_requester() -> None:
    row = _request_row()
    router = _SqlRouter().route("WHERE request_id = :rid", _mapping_first(row))
    service = _service(router)

    assert await service.get(_ctx(tenant_id=uuid.uuid4()), row["request_id"]) is None


@pytest.mark.asyncio
async def test_get_returns_the_request_for_its_requester() -> None:
    row = _request_row()
    router = _SqlRouter().route("WHERE request_id = :rid", _mapping_first(row))
    service = _service(router)

    result = await service.get(_ctx(tenant_id=row["requester_tenant_id"]), row["request_id"])

    assert result is not None
    assert result.request_id == row["request_id"]


@pytest.mark.asyncio
async def test_for_owner_defaults_to_open_statuses_only() -> None:
    router = _SqlRouter().route("WHERE owner_tenant_id = :tid", _mapping_all([]))
    service = _service(router)

    await service.for_owner(_ctx())

    sql, params = router.calls[0]
    assert "status IN ('raised', 'acknowledged')" in sql
    assert "cursor_created_at" not in (params or {})


@pytest.mark.asyncio
async def test_for_owner_can_include_closed_statuses() -> None:
    router = _SqlRouter().route("WHERE owner_tenant_id = :tid", _mapping_all([]))
    service = _service(router)

    await service.for_owner(_ctx(), open_only=False)

    sql, _params = router.calls[0]
    assert "status IN" not in sql


@pytest.mark.asyncio
async def test_for_owner_with_a_cursor_adds_the_keyset_condition() -> None:
    router = _SqlRouter().route("WHERE owner_tenant_id = :tid", _mapping_all([]))
    service = _service(router)
    cursor_request_id = uuid.uuid4()

    await service.for_owner(_ctx(), cursor=(_NOW, cursor_request_id))

    sql, params = router.calls[0]
    assert "(created_at, request_id) > (:cursor_created_at, :cursor_request_id)" in sql
    assert params is not None
    assert params["cursor_request_id"] == cursor_request_id


@pytest.mark.asyncio
async def test_raised_by_scopes_to_the_requester_tenant_not_the_owner() -> None:
    router = _SqlRouter().route("WHERE requester_tenant_id = :tid", _mapping_all([]))
    service = _service(router)
    tenant_id = uuid.uuid4()

    await service.raised_by(_ctx(tenant_id=tenant_id))

    sql, params = router.calls[0]
    assert "requester_tenant_id = :tid" in sql
    assert params is not None
    assert params["tid"] == tenant_id


@pytest.mark.asyncio
async def test_raised_by_with_a_cursor_adds_the_keyset_condition() -> None:
    router = _SqlRouter().route("WHERE requester_tenant_id = :tid", _mapping_all([]))
    service = _service(router)
    cursor_request_id = uuid.uuid4()

    await service.raised_by(_ctx(), cursor=(_NOW, cursor_request_id))

    sql, params = router.calls[0]
    assert "(created_at, request_id) > (:cursor_created_at, :cursor_request_id)" in sql
    assert params is not None
    assert params["cursor_request_id"] == cursor_request_id


@pytest.mark.asyncio
async def test_for_subject_scopes_to_either_the_owner_or_the_requester() -> None:
    router = _SqlRouter().route("WHERE subject_entity_id = :sid", _mapping_all([]))
    service = _service(router)
    subject_entity_id = uuid.uuid4()

    await service.for_subject(_ctx(), subject_entity_id)

    sql, params = router.calls[0]
    assert "owner_tenant_id = :tid OR requester_tenant_id = :tid" in sql
    assert params is not None
    assert params["sid"] == subject_entity_id


@pytest.mark.asyncio
async def test_history_is_empty_when_the_caller_may_not_see_the_request() -> None:
    router = _SqlRouter().route("WHERE request_id = :rid", _mapping_first(None))
    service = _service(router)

    result = await service.history(_ctx(), uuid.uuid4())

    assert result == ()
    # get()'s own not-found short-circuit must skip the transition-history
    # query entirely, not run it and discard the rows.
    assert not any("memory_request_transition" in sql for sql, _ in router.calls)


@pytest.mark.asyncio
async def test_history_returns_transitions_in_occurred_order() -> None:
    row = _request_row()
    transitions = [
        {"from_status": STATUS_RAISED, "to_status": STATUS_ACKNOWLEDGED, "reason": None, "occurred_at": _NOW},
        {
            "from_status": STATUS_ACKNOWLEDGED,
            "to_status": STATUS_ACCEPTED,
            "reason": None,
            "occurred_at": _NOW + datetime.timedelta(hours=1),
        },
    ]
    router = (
        # `history`'s own query also matches "WHERE request_id = :rid" (its
        # own WHERE clause), so the more specific transition-table route
        # must be registered first -- first match in registration order wins.
        _SqlRouter()
        .route("FROM memory_request_transition", _mapping_all(transitions))
        .route("WHERE request_id = :rid", _mapping_first(row))
    )
    service = _service(router)

    result = await service.history(_ctx(tenant_id=row["owner_tenant_id"]), row["request_id"])

    assert [t.to_status for t in result] == [STATUS_ACKNOWLEDGED, STATUS_ACCEPTED]


# ---------------------------------------------------------------------------
# CapabilityRequest.is_open -- pure dataclass property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_open"),
    [
        (STATUS_RAISED, True),
        (STATUS_ACKNOWLEDGED, True),
        (STATUS_ACCEPTED, False),
        (STATUS_DECLINED, False),
        (STATUS_DUPLICATE, False),
        (STATUS_RESOLVED, False),
    ],
)
def test_is_open_reflects_only_the_two_non_final_pre_decision_statuses(status: str, expected_open: bool) -> None:
    request = CapabilityRequest(
        request_id=uuid.uuid4(),
        owner_tenant_id=uuid.uuid4(),
        requester_tenant_id=uuid.uuid4(),
        subject_entity_id=uuid.uuid4(),
        request_category="defect",
        title="t",
        body="b",
        status=status,
        decision_reason=None,
        resulting_promotion_id=None,
        created_at=_NOW,
    )
    assert request.is_open is expected_open
