"""Unit tests for claim_history.py: `chain_for`'s supersession walk and
`believed_at`'s point-in-time selection, without Postgres.

Session interaction is mocked via an SQL-string-keyed AsyncMock router (see
test_promotion_sweep_worker.py for the established pattern). The bi-temporal
WHERE predicate itself only Postgres can actually evaluate, so where a
property can't be observed behaviourally at this tier it is pinned
structurally instead -- the same convention `tests/integration/
test_claim_serving.py` already uses for its arm-filter checks (e.g.
`test_only_the_semantic_arm_filters_on_model_version`).

Coverage:
- `chain_for`: walks oldest-to-newest, stops at a cycle instead of hanging,
  and returns an empty list for a claim that never existed.
- `believed_at`: the point-in-time predicate (still-open, or closed after
  `as_of`) and the created-before-`as_of` cutoff are present in the SQL;
  `subject_entity_id`/`predicate`/`as_of` reach the query unchanged; rows map
  to `BelievedClaim` in query order; confidence/bucket are populated only
  when the row was actually scored.
- `visibility_rows_for`: empty input short-circuits without a query; present
  rows map by claim_id and missing ids are simply absent.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.memory.claim_history import ClaimHistoryService

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _believed_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        claim_id=uuid.uuid4(),
        predicate="owned_by_team",
        value_jsonb="platform",
        source_authority="owner_human",
        confidence=None,
        confidence_scored_at=None,
        decay_half_life_days=None,
        confidence_hold_until=None,
        value_type=None,
        status="staged",
        superseded_by=None,
        superseded_reason=None,
        created_at=_NOW,
        t_invalidated_at=None,
        is_contested=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _history_session_factory(
    *,
    believed_rows: list[SimpleNamespace] | None = None,
    chain_rows_by_id: dict[uuid.UUID, SimpleNamespace | None] | None = None,
    visibility_rows: list[dict] | None = None,
) -> tuple[MagicMock, list[tuple[str, dict]]]:
    executed: list[tuple[str, dict]] = []
    by_id = chain_rows_by_id or {}

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        params = params or {}
        executed.append((sql, params))
        if "subject_entity_id = :eid" in sql:
            result = MagicMock()
            result.all = MagicMock(return_value=believed_rows or [])
            return result
        if "WHERE claim_id = :cid" in sql:
            result = MagicMock()
            result.one_or_none = MagicMock(return_value=by_id.get(params.get("cid")))
            return result
        if "claim_id = ANY(:ids)" in sql:
            result = MagicMock()
            mapped = MagicMock()
            mapped.all = MagicMock(return_value=visibility_rows or [])
            result.mappings = MagicMock(return_value=mapped)
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


# ---------------------------------------------------------------------------
# chain_for: the supersession walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_for_walks_the_chain_oldest_first() -> None:
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    row_a1 = _believed_row(claim_id=a1, superseded_by=a2, value_jsonb="platform")
    row_a2 = _believed_row(claim_id=a2, superseded_by=None, value_jsonb="billing", t_invalidated_at=None)
    factory, _ = _history_session_factory(chain_rows_by_id={a1: row_a1, a2: row_a2})

    chain = await ClaimHistoryService(factory).chain_for(a1)

    assert [c.claim_id for c in chain] == [a1, a2]
    assert [c.value for c in chain] == ["platform", "billing"]
    assert chain[-1].was_current is True


@pytest.mark.asyncio
async def test_chain_for_stops_at_a_cycle_instead_of_hanging_forever() -> None:
    """Should be impossible in real data -- a claim cannot supersede itself --
    but a walk that trusts that would hang if it were ever wrong. Two claims
    pointing at each other must still terminate."""
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    row_a1 = _believed_row(claim_id=a1, superseded_by=a2)
    row_a2 = _believed_row(claim_id=a2, superseded_by=a1)
    factory, _ = _history_session_factory(chain_rows_by_id={a1: row_a1, a2: row_a2})

    chain = await ClaimHistoryService(factory).chain_for(a1)

    assert [c.claim_id for c in chain] == [a1, a2]


@pytest.mark.asyncio
async def test_chain_for_a_claim_that_does_not_exist_returns_an_empty_list() -> None:
    factory, _ = _history_session_factory(chain_rows_by_id={})
    chain = await ClaimHistoryService(factory).chain_for(uuid.uuid4())
    assert chain == []


@pytest.mark.asyncio
async def test_chain_for_a_single_unsuperseded_claim_returns_one_current_entry() -> None:
    claim_id = uuid.uuid4()
    row = _believed_row(claim_id=claim_id, superseded_by=None, t_invalidated_at=None)
    factory, _ = _history_session_factory(chain_rows_by_id={claim_id: row})

    chain = await ClaimHistoryService(factory).chain_for(claim_id)

    assert len(chain) == 1
    assert chain[0].was_current is True
    assert chain[0].superseded_by is None


# ---------------------------------------------------------------------------
# believed_at: point-in-time selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_believed_at_passes_subject_predicate_and_as_of_through_unchanged() -> None:
    subject = uuid.uuid4()
    as_of = _NOW
    factory, executed = _history_session_factory(believed_rows=[])

    await ClaimHistoryService(factory).believed_at(subject_entity_id=subject, predicate="owned_by_team", as_of=as_of)

    _, params = next(p for p in executed if "subject_entity_id = :eid" in p[0])
    assert params["eid"] == subject
    assert params["pred"] == "owned_by_team"
    assert params["as_of"] == as_of


@pytest.mark.asyncio
async def test_believed_at_defaults_predicate_to_none_when_omitted() -> None:
    factory, executed = _history_session_factory(believed_rows=[])
    await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)
    _, params = executed[0]
    assert params["pred"] is None


@pytest.mark.asyncio
async def test_believed_at_sql_admits_a_claim_still_open_or_closed_after_as_of() -> None:
    """Structural: the point-in-time predicate itself only Postgres evaluates.
    A claim superseded yesterday must still answer a query about last week --
    this pins the exact clause that makes that true."""
    factory, executed = _history_session_factory(believed_rows=[])
    await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)
    sql, _ = executed[0]
    assert "t_invalidated_at IS NULL" in sql
    assert "t_invalidated_at > CAST(:as_of AS TIMESTAMPTZ)" in sql


@pytest.mark.asyncio
async def test_believed_at_sql_excludes_claims_written_after_as_of() -> None:
    factory, executed = _history_session_factory(believed_rows=[])
    await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)
    sql, _ = executed[0]
    assert "created_at <= CAST(:as_of AS TIMESTAMPTZ)" in sql


@pytest.mark.asyncio
async def test_believed_at_maps_rows_to_believed_claims_in_query_order() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    rows = [
        _believed_row(claim_id=first, value_jsonb="platform"),
        _believed_row(claim_id=second, value_jsonb="billing"),
    ]
    factory, _ = _history_session_factory(believed_rows=rows)

    result = await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)

    assert [b.claim_id for b in result] == [first, second]
    assert [b.value for b in result] == ["platform", "billing"]


@pytest.mark.asyncio
async def test_believed_at_leaves_confidence_and_bucket_unset_when_never_scored() -> None:
    row = _believed_row(confidence=None, confidence_scored_at=None)
    factory, _ = _history_session_factory(believed_rows=[row])

    result = await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)

    assert result[0].confidence is None
    assert result[0].bucket is None


@pytest.mark.asyncio
async def test_believed_at_computes_a_confidence_and_bucket_when_the_row_was_scored() -> None:
    row = _believed_row(
        confidence=0.85,
        confidence_scored_at=_NOW - datetime.timedelta(days=1),
        decay_half_life_days=270,
    )
    factory, _ = _history_session_factory(believed_rows=[row])

    result = await ClaimHistoryService(factory).believed_at(subject_entity_id=uuid.uuid4(), as_of=_NOW)

    assert isinstance(result[0].confidence, float)
    assert result[0].bucket is not None


# ---------------------------------------------------------------------------
# visibility_rows_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visibility_rows_for_returns_an_empty_dict_without_querying_for_no_ids() -> None:
    factory, executed = _history_session_factory(visibility_rows=[])
    result = await ClaimHistoryService(factory).visibility_rows_for([])
    assert result == {}
    assert executed == []


@pytest.mark.asyncio
async def test_visibility_rows_for_maps_by_claim_id_and_omits_missing_ids() -> None:
    present, missing = uuid.uuid4(), uuid.uuid4()
    subject = uuid.uuid4()
    tenant = uuid.uuid4()
    rows = [{"claim_id": present, "subject_entity_id": subject, "visibility": "public", "owning_tenant_id": tenant}]
    factory, _ = _history_session_factory(visibility_rows=rows)

    result = await ClaimHistoryService(factory).visibility_rows_for([present, missing])

    assert set(result) == {present}
    assert result[present].subject_entity_id == subject
    assert result[present].visibility == "public"
    assert result[present].owning_tenant_id == tenant
