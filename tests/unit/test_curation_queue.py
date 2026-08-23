"""Unit tests for `CurationQueueService` (contextplane.service.memory.curation_queue).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session,
mirroring `tests/unit/test_promotion.py`'s pattern -- no Postgres required.
`CurationQueueService` reads only (`items_for`, `counts_for`); it has no
`session.begin()` transaction to fake, only the bare `async with self._factory()
as session:` shape, so the session factory here is the simpler
`factory.return_value.__aenter__`/`__aexit__` form `test_operational_health.py`
already uses for the same shape.

Before this file, nothing in the unit suite actually called `items_for` or
`counts_for` -- the module's ~70% unit-scope coverage came entirely from other
files importing its dataclasses and constants as fixtures. That is the real gap
this file closes.

Coverage:
- `items_for`: cursor-absent vs. cursor-present SQL/params construction, the
  `page_size + 1` fetch-ahead convention, and the row-to-`QueueItem` mapping
  (confidence float-or-None, human_backed bool cast, proposal_id passthrough).
- `counts_for`: wraps `_QUEUE_BASE` in a `GROUP BY reason` subquery and returns
  a `{reason: count}` dict.
- The backlog CASE statement's WHEN-clause order -- unlinked > contested >
  awaiting_owner > below_floor -- pinned structurally, since a Postgres round
  trip cannot observe this precedence in a mocked unit test any other way (the
  mocked row already carries whatever `reason` the test hands it).
- `QueueItem.available_actions`, the curator-facing wiring of `ACTIONS_BY_REASON`
  -- every declared reason offers exactly its own action set, and an
  undeclared reason offers none.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.curation_cases import (
    CASE_OPEN,
    CASE_RESOLVED,
    CASE_ROUTED,
    DISPOSITION_ACTOR_KINDS,
    DISPOSITION_BY_HUMAN,
    DISPOSITION_BY_POLICY,
    DISPOSITION_CONFIRM,
    DISPOSITION_PROPOSE_ARC,
    DISPOSITIONS,
    CurationCase,
    CurationCaseService,
)
from contextplane.service.memory.curation_queue import (
    _QUEUE_BASE,
    ACTIONS_BY_REASON,
    ESCALATION_AGE_DAYS,
    REASON_AWAITING_OWNER,
    REASON_BELOW_FLOOR,
    REASON_CONTESTED,
    REASON_UNLINKED,
    CurationQueueService,
    QueueCursor,
    QueueItem,
)
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


def _queue_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "reason": REASON_UNLINKED,
        "subject_reference": "svc:payments",
        "subject_entity_id": None,
        "predicate": "owned_by_team",
        "value": "platform",
        "confidence": 0.4,
        "created_at": _NOW,
        "human_backed": False,
        "proposal_id": None,
        # The three ranking columns, negated in the query so the whole ordering
        # is ascending and a row-constructor cursor can express it.
        "escalation_rank": 1,
        "neg_dependants": 0,
        "neg_sampling": 0,
    }
    base.update(overrides)
    return base


def _session_factory(execute: Any) -> MagicMock:
    """`CurationQueueService` never opens a transaction -- just `async with
    self._factory() as session:` -- so no `session.begin()` fake is needed."""
    session = AsyncMock()
    session.execute = execute
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _items_session(
    rows: list[dict[str, Any]],
) -> tuple[MagicMock, list[tuple[str, dict[str, Any] | None]]]:
    """Router for `items_for`: any non-`GROUP BY` query returns `rows`.
    Records every `(sql, params)` pair so a test can assert on construction."""
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls.append((sql, params))
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        return result

    return _session_factory(execute), calls


def _counts_session(
    rows: list[dict[str, Any]],
) -> tuple[MagicMock, list[tuple[str, dict[str, Any] | None]]]:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls.append((sql, params))
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        return result

    return _session_factory(execute), calls


# ---------------------------------------------------------------------------
# items_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_for_without_a_cursor_fetches_page_size_plus_one_from_the_start() -> None:
    factory, calls = _items_session([])
    service = CurationQueueService(factory)
    tenant_id = uuid.uuid4()

    await service.items_for(tenant_id, page_size=25, now=_NOW)

    assert len(calls) == 1
    sql, params = calls[0]
    assert "escalation_rank, neg_dependants, neg_sampling, created_at, claim_id" not in sql.split("ORDER BY")[0]
    assert "ORDER BY escalation_rank, neg_dependants, neg_sampling, created_at, claim_id LIMIT :limit" in sql
    assert params["tid"] == tenant_id
    assert params["limit"] == 26
    # The escalation cutoff is derived from the caller's instant, not read off
    # the wall clock inside the query -- so a test can pin it and a reviewer can
    # reason about which rows were past the age at the moment of the read.
    assert params["escalation_cutoff"] == _NOW - datetime.timedelta(days=ESCALATION_AGE_DAYS)


@pytest.mark.asyncio
async def test_items_for_with_a_cursor_adds_the_keyset_condition_and_its_params() -> None:
    factory, calls = _items_session([])
    service = CurationQueueService(factory)
    tenant_id = uuid.uuid4()
    cursor_claim_id = uuid.uuid4()
    cursor = QueueCursor(
        escalation_rank=1,
        neg_dependants=-7,
        neg_sampling=-45,
        created_at=_NOW,
        claim_id=cursor_claim_id,
    )

    await service.items_for(tenant_id, cursor=cursor, page_size=10, now=_NOW)

    sql, params = calls[0]
    # The whole sort tuple, in one ascending comparison. A cursor over fewer
    # components than the ordering uses would resume in the wrong place as soon
    # as two rows shared an arrival time.
    assert "(escalation_rank, neg_dependants, neg_sampling, created_at, claim_id)" in sql
    assert params is not None
    assert params["cursor_escalation"] == 1
    assert params["cursor_dependants"] == -7
    assert params["cursor_sampling"] == -45
    assert params["cursor_created_at"] == _NOW
    assert params["cursor_claim_id"] == cursor_claim_id
    assert params["limit"] == 11


@pytest.mark.asyncio
async def test_items_for_maps_every_queue_item_field_from_the_row() -> None:
    proposal_id = uuid.uuid4()
    subject_entity_id = uuid.uuid4()
    row = _queue_row(
        reason=REASON_AWAITING_OWNER,
        subject_entity_id=subject_entity_id,
        confidence=0.87,
        human_backed=1,  # DB drivers return an int for a boolean column at times
        proposal_id=proposal_id,
    )
    factory, _ = _items_session([row])
    service = CurationQueueService(factory)

    items = await service.items_for(uuid.uuid4())

    assert items == (
        QueueItem(
            claim_id=row["claim_id"],
            reason=REASON_AWAITING_OWNER,
            subject_reference=row["subject_reference"],
            subject_entity_id=subject_entity_id,
            predicate=row["predicate"],
            value=row["value"],
            confidence=0.87,
            created_at=_NOW,
            human_backed=True,
            proposal_id=proposal_id,
        ),
    )
    assert isinstance(items[0].human_backed, bool)


@pytest.mark.asyncio
async def test_items_for_maps_a_null_confidence_to_none_not_zero() -> None:
    """`below_floor` claims and unattributed ones can both have a stored
    confidence, but the mapping must not turn a genuinely-absent value into a
    float -- `0.0` and "no score" are different claims about the row."""
    row = _queue_row(confidence=None)
    factory, _ = _items_session([row])
    service = CurationQueueService(factory)

    items = await service.items_for(uuid.uuid4())

    assert items[0].confidence is None


# ---------------------------------------------------------------------------
# counts_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counts_for_wraps_the_queue_base_in_a_group_by_reason_subquery() -> None:
    factory, calls = _counts_session(
        [
            {"reason": REASON_UNLINKED, "n": 3},
            {"reason": REASON_CONTESTED, "n": 1},
        ]
    )
    service = CurationQueueService(factory)
    tenant_id = uuid.uuid4()

    counts = await service.counts_for(tenant_id)

    assert counts == {REASON_UNLINKED: 3, REASON_CONTESTED: 1}
    sql, params = calls[0]
    assert "GROUP BY reason" in sql
    assert "SELECT reason, count(*) AS n FROM (" in sql
    assert params == {"tid": tenant_id}


@pytest.mark.asyncio
async def test_counts_for_with_no_backlogged_claims_returns_an_empty_dict() -> None:
    factory, _ = _counts_session([])
    service = CurationQueueService(factory)

    counts = await service.counts_for(uuid.uuid4())

    assert counts == {}


# ---------------------------------------------------------------------------
# The backlog CASE order -- pinned structurally, since a mocked unit test
# cannot observe which WHEN branch Postgres would actually pick.
# ---------------------------------------------------------------------------


def test_the_backlog_reason_case_checks_unlinked_before_contested_before_awaiting_owner_before_below_floor() -> None:
    """A claim reaches the queue for the *first* reason that applies. That
    precedence lives entirely in the CASE statement's WHEN order -- an
    unlinked+contested claim must read as `unlinked`, never `contested`, and
    so on down the chain. This is exactly the kind of decision a Postgres
    round trip would obscure (the mock hands back whichever `reason` a test
    fixture chooses) and a structural assertion on the SQL text pins cheaply.
    """
    case_start = _QUEUE_BASE.index("CASE")
    case_end = _QUEUE_BASE.index("END AS reason")
    case_text = _QUEUE_BASE[case_start:case_end]

    unlinked_pos = case_text.index("'unlinked'")
    contested_pos = case_text.index("'contested'")
    awaiting_owner_pos = case_text.index("'awaiting_owner'")
    below_floor_pos = case_text.index("'below_floor'")

    assert unlinked_pos < contested_pos < awaiting_owner_pos < below_floor_pos


# ---------------------------------------------------------------------------
# QueueItem.available_actions -- the curator-facing ACTIONS_BY_REASON wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (REASON_UNLINKED, ("link", "discard")),
        (REASON_CONTESTED, ("confirm", "discard", "escalate")),
        (REASON_BELOW_FLOOR, ("confirm", "discard")),
        (REASON_AWAITING_OWNER, ("escalate",)),
    ],
)
def test_available_actions_matches_the_declared_action_set_for_each_reason(
    reason: str, expected: tuple[str, ...]
) -> None:
    item = QueueItem(
        claim_id=uuid.uuid4(),
        reason=reason,
        subject_reference="svc:payments",
        subject_entity_id=None,
        predicate="owned_by_team",
        value="platform",
        confidence=None,
        created_at=_NOW,
        human_backed=False,
    )
    assert item.available_actions == expected
    assert ACTIONS_BY_REASON[reason] == expected


def test_available_actions_never_offers_accept_or_reject() -> None:
    """Those belong to the owner's review path, which checks tenancy and
    role -- offering them here would put a second door on a decision that
    already has an owner."""
    for actions in ACTIONS_BY_REASON.values():
        assert "accept" not in actions
        assert "reject" not in actions


def test_available_actions_is_empty_for_an_undeclared_reason() -> None:
    item = QueueItem(
        claim_id=uuid.uuid4(),
        reason="not_a_real_reason",
        subject_reference="svc:payments",
        subject_entity_id=None,
        predicate="owned_by_team",
        value="platform",
        confidence=None,
        created_at=_NOW,
        human_backed=False,
    )
    assert item.available_actions == ()


# ---------------------------------------------------------------------------
# Contradiction cases -- the one thing this module writes.
#
# These need a `session.begin()` fake the read paths above do not: every case
# mutation runs in a transaction so its audit row commits with the decision or
# neither does.
# ---------------------------------------------------------------------------


def _case_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "subject_reference": "svc:payments",
        "predicate": "owned_by_team",
        "raised_by_derivation_id": None,
        "status": CASE_OPEN,
        "owner_id": None,
        "routed_at": None,
        "disposition": None,
        "disposition_actor_kind": None,
        "approval_authority": None,
        "evidence_threshold": None,
        "resolved_at": None,
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id if actor_id is not None else uuid.uuid4(),
        roles=["curator"],
    )


def _txn_factory(execute: Any) -> MagicMock:
    """`async with self._factory() as session, session.begin():` -- so the
    session needs a `begin()` returning its own async context manager."""
    session = AsyncMock()
    session.execute = execute
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=txn)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _case_session(
    *,
    existing: dict[str, Any] | None = None,
    locked: dict[str, Any] | None = None,
    updated: bool = True,
) -> tuple[MagicMock, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {"insert": [], "update": [], "audit": [], "sql": []}

    async def execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls["sql"].append(sql)
        result = MagicMock()
        if "FOR UPDATE" in sql:
            result.mappings.return_value.one_or_none = MagicMock(return_value=locked)
            return result
        if "SELECT" in sql and "FROM curation_cases" in sql:
            result.mappings.return_value.one_or_none = MagicMock(return_value=existing)
            return result
        if "INSERT INTO curation_cases" in sql:
            calls["insert"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        if "UPDATE curation_cases" in sql:
            calls["update"].append(params or {})
            result.one_or_none = MagicMock(return_value=MagicMock() if updated else None)
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _txn_factory(execute), calls


@pytest.mark.asyncio
async def test_open_case_returns_the_already_open_case_on_the_same_axis() -> None:
    """Idempotent by design: re-detecting the same contradiction is the normal
    path, and a second row would split one disagreement into two entries two
    owners could decide differently."""
    tenant = uuid.uuid4()
    existing = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id="platform-rota", routed_at=_NOW)
    factory, calls = _case_session(existing=existing)

    case = await CurationCaseService(factory).open_case(
        _ctx(tenant), subject_reference="svc:payments", predicate="owned_by_team", now=_NOW
    )

    assert case.case_id == existing["case_id"]
    assert case.status == CASE_ROUTED
    assert calls["insert"] == [], "a second case row was written for one axis"


@pytest.mark.asyncio
async def test_open_case_writes_the_case_and_its_audit_row_together() -> None:
    tenant = uuid.uuid4()
    factory, calls = _case_session(existing=None)

    case = await CurationCaseService(factory).open_case(
        _ctx(tenant), subject_reference="svc:payments", predicate="owned_by_team", now=_NOW
    )

    assert case.status == CASE_OPEN
    assert len(calls["insert"]) == 1
    assert len(calls["audit"]) == 1, "a case was opened with no audit row"
    assert calls["audit"][0]["target"] == case.case_id


@pytest.mark.asyncio
async def test_open_case_refuses_an_axis_it_cannot_name() -> None:
    """A case with no subject or no predicate names no disagreement, so there is
    nothing for an owner to decide."""
    factory, calls = _case_session(existing=None)
    service = CurationCaseService(factory)

    with pytest.raises(ValidationError):
        await service.open_case(_ctx(uuid.uuid4()), subject_reference="", predicate="owned_by_team", now=_NOW)
    with pytest.raises(ValidationError):
        await service.open_case(_ctx(uuid.uuid4()), subject_reference="svc:payments", predicate="", now=_NOW)
    assert calls["insert"] == []


@pytest.mark.asyncio
async def test_route_case_records_the_previous_owner_on_an_escalation() -> None:
    """Escalation is a real move, so re-routing is allowed -- and the audit row
    carries who it came from, which is the whole point of recording a handoff."""
    tenant = uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id="first-owner", routed_at=_NOW)
    factory, calls = _case_session(locked=locked)

    case = await CurationCaseService(factory).route_case(
        _ctx(tenant), case_id=locked["case_id"], owner_id="second-owner", now=_NOW
    )

    assert case.status == CASE_ROUTED
    assert case.owner_id == "second-owner"
    assert calls["audit"][0]["aid"] is not None
    assert len(calls["update"]) == 1


@pytest.mark.asyncio
async def test_route_case_refuses_a_resolved_case() -> None:
    """Routing a decided case would suggest the decision is still to be made."""
    tenant = uuid.uuid4()
    locked = _case_row(
        tenant_id=tenant,
        status=CASE_RESOLVED,
        owner_id="an-owner",
        disposition=DISPOSITION_CONFIRM,
        resolved_at=_NOW,
    )
    factory, calls = _case_session(locked=locked)

    with pytest.raises(ConflictError, match="resolved"):
        await CurationCaseService(factory).route_case(
            _ctx(tenant), case_id=locked["case_id"], owner_id="another-owner", now=_NOW
        )
    assert calls["update"] == []


@pytest.mark.asyncio
async def test_route_case_answers_a_case_in_another_tenant_as_missing() -> None:
    """The tenant-scoped lookup finds nothing, so a case id cannot be used to
    learn that some other tenant is reviewing a contradiction."""
    factory, _ = _case_session(locked=None)

    with pytest.raises(NotFoundError):
        await CurationCaseService(factory).route_case(
            _ctx(uuid.uuid4()), case_id=uuid.uuid4(), owner_id="an-owner", now=_NOW
        )


@pytest.mark.asyncio
async def test_record_disposition_refuses_a_caller_who_is_not_the_routed_owner() -> None:
    """The check that makes "routed to an owner" mean anything: being able to see
    a case is not authority to decide it."""
    tenant = uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(uuid.uuid4()), routed_at=_NOW)
    factory, calls = _case_session(locked=locked)

    with pytest.raises(PermissionError, match="another owner"):
        await CurationCaseService(factory).record_disposition(
            _ctx(tenant), case_id=locked["case_id"], disposition=DISPOSITION_CONFIRM, now=_NOW
        )
    assert calls["update"] == []
    assert calls["audit"] == []


@pytest.mark.asyncio
async def test_record_disposition_refuses_an_unrouted_case() -> None:
    """A disposition on an unrouted case is a decision with no accountable owner
    behind it."""
    tenant = uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_OPEN)
    factory, calls = _case_session(locked=locked)

    with pytest.raises(ConflictError, match="accountable owner"):
        await CurationCaseService(factory).record_disposition(
            _ctx(tenant), case_id=locked["case_id"], disposition=DISPOSITION_CONFIRM, now=_NOW
        )
    assert calls["update"] == []


@pytest.mark.asyncio
async def test_record_disposition_stores_the_targets_authority_and_threshold() -> None:
    """Recorded at disposition time, not derived on read: a decision whose
    approver is decided afterwards is one nobody is accountable for."""
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(actor), routed_at=_NOW)
    factory, calls = _case_session(locked=locked)

    case = await CurationCaseService(factory).record_disposition(
        _ctx(tenant, actor), case_id=locked["case_id"], disposition=DISPOSITION_PROPOSE_ARC, now=_NOW
    )

    expected = DISPOSITIONS[DISPOSITION_PROPOSE_ARC]
    assert case.status == CASE_RESOLVED
    assert case.approval_authority == expected.approval_authority
    assert case.evidence_threshold == expected.evidence_threshold
    assert case.target_kind == expected.target_kind
    assert calls["update"][0]["authority"] == expected.approval_authority
    # The audit payload carries every policy axis, so a later reader can see what
    # the decision committed to without re-deriving it from the verb.
    payload = calls["audit"][0]["after"]
    for axis in (expected.scope, expected.supersession, expected.rollback):
        assert axis in payload


@pytest.mark.asyncio
async def test_record_disposition_loses_a_race_rather_than_overwriting() -> None:
    """The compare-and-swap is the second guard, not the only one: a lost race
    refuses so two owners leave one decision rather than the last writer's."""
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(actor), routed_at=_NOW)
    factory, calls = _case_session(locked=locked, updated=False)

    with pytest.raises(ConflictError, match="another writer"):
        await CurationCaseService(factory).record_disposition(
            _ctx(tenant, actor), case_id=locked["case_id"], disposition=DISPOSITION_CONFIRM, now=_NOW
        )
    assert calls["audit"] == [], "a refused decision still wrote an audit row"


@pytest.mark.asyncio
async def test_record_disposition_refuses_an_unknown_disposition_before_reading() -> None:
    """Vocabulary first: an unknown verb never reaches the row lock, so it cannot
    hold a lock on a case it was never entitled to decide."""
    factory, calls = _case_session(locked=None)

    with pytest.raises(ValidationError, match="unknown disposition"):
        await CurationCaseService(factory).record_disposition(
            _ctx(uuid.uuid4()), case_id=uuid.uuid4(), disposition="promote_everything", now=_NOW
        )
    assert calls["sql"] == []


@pytest.mark.asyncio
async def test_cases_for_rejects_an_unknown_status() -> None:
    factory, _ = _case_session()

    with pytest.raises(ValidationError, match="unknown case status"):
        await CurationCaseService(factory).cases_for(uuid.uuid4(), status="nearly_done")


@pytest.mark.asyncio
async def test_cases_for_pages_by_keyset_and_fetches_one_extra() -> None:
    """Same drain-from-the-front contract as `items_for`: `page_size + 1` so the
    caller can tell whether another page follows without a second query."""
    tenant = uuid.uuid4()
    rows = [_case_row(tenant_id=tenant) for _ in range(3)]
    captured: list[tuple[str, dict[str, Any]]] = []

    async def execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        captured.append((" ".join(str(stmt).split()), params or {}))
        result = MagicMock()
        result.mappings.return_value.all = MagicMock(return_value=rows)
        return result

    cursor = (_NOW, uuid.uuid4())
    cases = await CurationCaseService(_session_factory(execute)).cases_for(tenant, cursor=cursor, page_size=2)

    sql, params = captured[0]
    assert params["limit"] == 3
    assert "(created_at, case_id) > (:cursor_created_at, :cursor_case_id)" in sql
    assert "ORDER BY created_at, case_id" in sql
    assert len(cases) == 3, "cases_for must not truncate; the caller decides the page boundary"


@pytest.mark.asyncio
async def test_case_answers_another_tenants_case_as_missing() -> None:
    factory, _ = _case_session(existing=None)

    with pytest.raises(NotFoundError):
        await CurationCaseService(factory).case(_ctx(uuid.uuid4()), uuid.uuid4())


def test_a_case_with_no_disposition_names_no_target() -> None:
    """`target_kind` is derived, so an open case must not appear to have asked
    for a write."""
    case = CurationCase(
        case_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        subject_reference="svc:payments",
        predicate="owned_by_team",
        status=CASE_OPEN,
        created_at=_NOW,
    )
    assert case.target_kind is None


@pytest.mark.asyncio
async def test_an_unknown_disposition_actor_kind_is_refused_before_the_write() -> None:
    """A closed vocabulary, and the service says so before the database does.

    A caller passing `automated` or `bot` gets a sentence naming the legal values
    rather than a CHECK violation.
    """
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(actor), routed_at=_NOW)
    factory, _calls = _case_session(locked=locked)

    with pytest.raises(ValidationError, match="unknown disposition actor kind"):
        await CurationCaseService(factory).record_disposition(
            _ctx(tenant, actor),
            case_id=locked["case_id"],
            disposition=DISPOSITION_CONFIRM,
            now=_NOW,
            actor_kind="automated",
        )


@pytest.mark.asyncio
async def test_a_disposition_defaults_to_human_and_records_it() -> None:
    """Every path into `record_disposition` today is a transport carrying a
    person's request past the owner check, so `human` is the honest default. A
    policy path that arrives later has to say so, which is the point of the
    default rather than an accident of it."""
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(actor), routed_at=_NOW)
    factory, calls = _case_session(locked=locked)

    case = await CurationCaseService(factory).record_disposition(
        _ctx(tenant, actor), case_id=locked["case_id"], disposition=DISPOSITION_CONFIRM, now=_NOW
    )

    assert case.disposition_actor_kind == DISPOSITION_BY_HUMAN
    assert calls["update"][0]["actor_kind"] == DISPOSITION_BY_HUMAN
    # Recorded in the audit row too: a later reader can tell an automated
    # disposal from a reviewed one without joining anything.
    assert DISPOSITION_BY_HUMAN in calls["audit"][0]["after"]


@pytest.mark.asyncio
async def test_a_policy_disposition_is_stored_as_a_policy_disposition() -> None:
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    locked = _case_row(tenant_id=tenant, status=CASE_ROUTED, owner_id=str(actor), routed_at=_NOW)
    factory, calls = _case_session(locked=locked)

    case = await CurationCaseService(factory).record_disposition(
        _ctx(tenant, actor),
        case_id=locked["case_id"],
        disposition=DISPOSITION_CONFIRM,
        now=_NOW,
        actor_kind=DISPOSITION_BY_POLICY,
    )

    assert case.disposition_actor_kind == DISPOSITION_BY_POLICY
    assert calls["update"][0]["actor_kind"] == DISPOSITION_BY_POLICY


def test_the_actor_kind_vocabulary_is_exactly_two() -> None:
    """Closed on purpose. A third value would need a sampling rule of its own,
    and the argument for excluding automation is precisely that the human sample
    requirement does not move when one is added."""
    assert DISPOSITION_ACTOR_KINDS == {DISPOSITION_BY_HUMAN, DISPOSITION_BY_POLICY}
