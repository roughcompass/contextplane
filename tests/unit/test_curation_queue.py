"""Unit tests for `CurationQueueService` (registry.service.memory.curation_queue).

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

from registry.service.memory.curation_queue import (
    _QUEUE_BASE,
    ACTIONS_BY_REASON,
    REASON_AWAITING_OWNER,
    REASON_BELOW_FLOOR,
    REASON_CONTESTED,
    REASON_UNLINKED,
    CurationQueueService,
    QueueItem,
)

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

    await service.items_for(tenant_id, page_size=25)

    assert len(calls) == 1
    sql, params = calls[0]
    assert "AND (c.created_at, c.claim_id) >" not in sql
    assert "ORDER BY c.created_at, c.claim_id LIMIT :limit" in sql
    assert params == {"tid": tenant_id, "limit": 26}


@pytest.mark.asyncio
async def test_items_for_with_a_cursor_adds_the_keyset_condition_and_its_params() -> None:
    factory, calls = _items_session([])
    service = CurationQueueService(factory)
    tenant_id = uuid.uuid4()
    cursor_claim_id = uuid.uuid4()
    cursor = (_NOW, cursor_claim_id)

    await service.items_for(tenant_id, cursor=cursor, page_size=10)

    sql, params = calls[0]
    assert "AND (c.created_at, c.claim_id) > (:cursor_created_at, :cursor_claim_id)" in sql
    assert params is not None
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
