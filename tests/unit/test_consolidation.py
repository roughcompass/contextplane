"""Unit tests for ConsolidationService (contextplane.service.memory.consolidation).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session,
mirroring `tests/unit/test_promotion.py`'s pattern -- no Postgres required.
`ClaimService` is a bare `MagicMock` (its own module has its own unit suite;
this file only asserts *how* `ConsolidationService` calls it, never
re-deriving its write-path SQL). `resolve_contests_for` -- a module-level
import from `contest.py`, which has its own suite -- is patched the same way
`test_claims.py` patches `claims.py`'s own collaborator imports.

Everything else runs for real: `values_compatible`, `is_near_duplicate`, and
`intervals_overlap` are the actual functions from `claim_compare.py`, not
mocks, because the decision under test *is* what those functions feed into.

Coverage -- the decision (`_decide`, exercised through the public
`consolidate_in`):
- no neighbours -> ADD
- authority-first-over-recency: a stronger, *older* claim still supersedes a
  weaker, newer one (UPDATE)
- the converse: a weaker claim never supersedes a stronger one, however
  recent (CONTESTED)
- among equal authority, recency breaks the tie both ways (UPDATE / CONTESTED)
- a cross-tenant conflict is routed as a PROPOSAL rather than superseding
- a set-valued (multi-cardinality) predicate never competes, so two
  differing values ADD rather than conflict
- an undecidable comparison (prose) is neither equivalent nor conflicting,
  so it ADDs rather than manufacturing a contested claim out of a
  validation gap
- an exact match is preferred as the collapse survivor over a merely
  near-duplicate one

Coverage -- audit and idempotence:
- every decision, including a genuine NOOP (duplicate collapse), writes an
  audit row -- this is the module's central invariant and is
  mutation-tested (see the report accompanying this change)
- the *already-settled* short-circuit (nothing arrived since the last sweep)
  is the one path that legitimately writes nothing, which this suite pins
  as the deliberate exception to the invariant above
- a claim that is no longer live (already consolidated away) is a no-op
  with nothing to write, reached before any neighbourhood query fires
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.audit import actions
from contextplane.service.catalog.global_vocabulary import CARDINALITY_MULTI, CARDINALITY_SINGLE
from contextplane.service.memory import consolidation as consolidation_module
from contextplane.service.memory.consolidation import (
    DECISION_ADD,
    DECISION_CONTESTED,
    DECISION_NOOP,
    DECISION_PROPOSAL,
    DECISION_UPDATE,
    MATCHED_EXACT,
    REASON_CLUSTER_COLLAPSED,
    REASON_LOST_CONFLICT,
    ConsolidationService,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _candidate_row(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "subject_entity_id": uuid.uuid4(),
        "predicate": "owned_by_team",
        "value_jsonb": "platform",
        "value_type": "string",
        "value_cardinality": CARDINALITY_SINGLE,
        "value_entity_id": None,
        "source_authority": "owner_extraction",
        "owning_tenant_id": None,
        "author_tenant_id": uuid.uuid4(),
        "author_actor_id": uuid.uuid4(),
        "created_at": _NOW,
        "asserted_valid_from": _NOW - datetime.timedelta(days=1),
        "asserted_valid_to": None,
        "consolidated_at": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _neighbour_row(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "value_jsonb": "platform",
        "value_type": "string",
        "value_entity_id": None,
        "value_cardinality": CARDINALITY_SINGLE,
        "source_authority": "owner_extraction",
        "owning_tenant_id": None,
        "author_tenant_id": uuid.uuid4(),
        "created_at": _NOW,
        "asserted_valid_from": _NOW - datetime.timedelta(days=1),
        "asserted_valid_to": None,
        "confidence_hold_until": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _claims_service() -> MagicMock:
    claims = MagicMock()
    claims.mark_consolidated = AsyncMock()
    claims.close_superseded = AsyncMock()
    claims.merge_provenance = AsyncMock()
    claims.rescore_existing = AsyncMock()
    return claims


def _router(
    *,
    candidate: Any | None,
    neighbours: list[Any],
) -> tuple[AsyncMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"audit": [], "cluster": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM memory_claims" in sql and "FOR UPDATE" in sql:
            result.one_or_none = MagicMock(return_value=candidate)
            return result
        if "FROM memory_claims" in sql and "ORDER BY created_at DESC" in sql:
            result.all = MagicMock(return_value=neighbours)
            return result
        if "INSERT INTO memory_claim_cluster" in sql:
            calls["cluster"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, calls


def _service(monkeypatch: pytest.MonkeyPatch, *, claims: MagicMock | None = None) -> ConsolidationService:
    monkeypatch.setattr(consolidation_module, "resolve_contests_for", AsyncMock(return_value=0))
    return ConsolidationService(MagicMock(), clock=FakeClock(_NOW), claims=claims or _claims_service())


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _factory_service(
    monkeypatch: pytest.MonkeyPatch, *, candidate: Any | None, neighbours: list[Any], claims: MagicMock
) -> tuple[ConsolidationService, dict[str, list[dict[str, Any]]]]:
    """A ConsolidationService wired through `consolidate()`'s own
    session-factory path -- distinct from the `consolidate_in(session, ...)`
    entrypoint every other test in this file drives directly."""
    session, calls = _router(candidate=candidate, neighbours=neighbours)
    session.begin = MagicMock(return_value=_AsyncCM(None))

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)

    monkeypatch.setattr(consolidation_module, "resolve_contests_for", AsyncMock(return_value=0))
    service = ConsolidationService(factory, clock=FakeClock(_NOW), claims=claims)
    return service, calls


# ---------------------------------------------------------------------------
# Nothing to reconcile: not live, and already-settled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_that_is_no_longer_live_is_a_noop_reached_before_any_neighbourhood_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, calls = _router(candidate=None, neighbours=[])
    service = _service(monkeypatch)

    outcome = await service.consolidate_in(session, claim_id=uuid.uuid4(), now=_NOW)

    assert outcome.decision == DECISION_NOOP
    assert outcome.already_settled is True
    assert calls["audit"] == []


@pytest.mark.asyncio
async def test_already_settled_writes_nothing_the_deliberate_exception_to_the_audit_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from a genuine NOOP decision below: nothing has arrived in
    the neighbourhood since the last sweep, so there is nothing to *decide*,
    and an audit row here would record that the sweep ran rather than that
    anything happened."""
    consolidated_at = _NOW - datetime.timedelta(hours=1)
    candidate = _candidate_row(consolidated_at=consolidated_at)
    older_neighbour = _neighbour_row(created_at=consolidated_at - datetime.timedelta(minutes=1))
    session, calls = _router(candidate=candidate, neighbours=[older_neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.already_settled is True
    assert calls["audit"] == []
    claims.mark_consolidated.assert_not_awaited()


# ---------------------------------------------------------------------------
# ADD: no neighbours, or nothing equivalent/conflicting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_neighbours_decides_add_and_still_audits_and_marks_consolidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate_row()
    session, calls = _router(candidate=candidate, neighbours=[])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_ADD
    assert len(calls["audit"]) == 1
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_ADD
    claims.mark_consolidated.assert_awaited_once()


@pytest.mark.asyncio
async def test_consolidate_opens_its_own_session_and_transaction_around_the_same_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`consolidate()` is the entrypoint every other test in this file
    bypasses by calling `consolidate_in` on a caller-supplied session
    directly -- this pins that the public wrapper actually opens its own
    session/transaction and reaches the identical decision."""
    candidate = _candidate_row()
    claims = _claims_service()
    service, calls = _factory_service(monkeypatch, candidate=candidate, neighbours=[], claims=claims)

    outcome = await service.consolidate(candidate.claim_id)

    assert outcome.decision == DECISION_ADD
    claims.mark_consolidated.assert_awaited_once()
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_ADD


@pytest.mark.asyncio
async def test_an_undecidable_comparison_adds_rather_than_manufacturing_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose cannot be compared without a model, so `values_compatible`
    reports UNDECIDABLE -- neither equivalent nor conflicting. Treating that
    as a conflict would create a contested claim out of a validation gap."""
    candidate = _candidate_row(value_type="prose", value_jsonb="a paragraph about ownership")
    neighbour = _neighbour_row(value_type="prose", value_jsonb="a different paragraph")
    session, calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_ADD
    claims.close_superseded.assert_not_awaited()
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_ADD


@pytest.mark.asyncio
async def test_a_set_valued_predicate_never_competes_so_differing_values_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two dependency claims are two facts, not two answers to one
    question -- `_competes` refuses to treat a multi-cardinality predicate's
    differing values as a conflict at all."""
    candidate = _candidate_row(value_cardinality=CARDINALITY_MULTI, value_jsonb="service-a")
    neighbour = _neighbour_row(value_cardinality=CARDINALITY_MULTI, value_jsonb="service-b")
    session, _calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_ADD
    claims.close_superseded.assert_not_awaited()


@pytest.mark.asyncio
async def test_competes_requires_both_sides_to_be_single_valued_not_just_the_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The symmetric half of the cardinality guard: a single-valued
    candidate still does not compete with a multi-valued neighbour, because
    `_competes` checks *both* sides."""
    candidate = _candidate_row(value_cardinality=CARDINALITY_SINGLE, value_jsonb="service-a")
    neighbour = _neighbour_row(value_cardinality=CARDINALITY_MULTI, value_jsonb="service-b")
    session, _calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_ADD
    claims.close_superseded.assert_not_awaited()


# ---------------------------------------------------------------------------
# Authority first, recency second
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stronger_but_older_claim_still_supersedes_a_weaker_newer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`owner_extraction` (rank 1) outranks `owner_inference` (rank 2)
    regardless of which was observed later -- the whole point of
    authority-first ordering. Newest-wins would flip this."""
    tenant = uuid.uuid4()
    candidate = _candidate_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW - datetime.timedelta(days=10),
        value_jsonb="platform",
    )
    neighbour = _neighbour_row(
        source_authority="owner_inference",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW,
        value_jsonb="finance",
    )
    session, calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_UPDATE
    assert outcome.superseded == (neighbour.claim_id,)
    claims.close_superseded.assert_awaited_once()
    close_kwargs = claims.close_superseded.await_args.kwargs
    assert close_kwargs["claim_id"] == neighbour.claim_id
    assert close_kwargs["survivor"] == candidate.claim_id
    assert close_kwargs["reason"] == REASON_LOST_CONFLICT
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_UPDATE


@pytest.mark.asyncio
async def test_a_weaker_claim_never_supersedes_a_stronger_one_however_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse of the test above: `owner_inference` cannot displace
    `owner_extraction` even though it is the newer observation."""
    tenant = uuid.uuid4()
    candidate = _candidate_row(
        source_authority="owner_inference",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW,
        value_jsonb="finance",
    )
    neighbour = _neighbour_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW - datetime.timedelta(days=10),
        value_jsonb="platform",
    )
    session, calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_CONTESTED
    assert outcome.contested_with == (neighbour.claim_id,)
    claims.close_superseded.assert_not_awaited()
    assert calls["audit"][0]["action"] == actions.CLAIM_CONTESTED


@pytest.mark.asyncio
async def test_among_equal_authority_the_more_recent_claim_supersedes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.uuid4()
    candidate = _candidate_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW,
        value_jsonb="finance",
    )
    neighbour = _neighbour_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW - datetime.timedelta(days=1),
        value_jsonb="platform",
    )
    session, _ = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_UPDATE


@pytest.mark.asyncio
async def test_among_equal_authority_an_older_candidate_is_contested_not_superseding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the tie-break: equal rank, but the candidate is the
    *older* one, so recency does not favour it either."""
    tenant = uuid.uuid4()
    candidate = _candidate_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW - datetime.timedelta(days=1),
        value_jsonb="finance",
    )
    neighbour = _neighbour_row(
        source_authority="owner_extraction",
        owning_tenant_id=tenant,
        author_tenant_id=tenant,
        created_at=_NOW,
        value_jsonb="platform",
    )
    session, _ = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_CONTESTED


# ---------------------------------------------------------------------------
# Cross-tenant: gated on the tenant columns, never on rank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cross_tenant_conflict_is_routed_as_a_proposal_not_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different tenant routes a proposal regardless of authority -- a
    claim about someone else's capability is never resolved here, however
    strong its authority tier."""
    owner_tenant = uuid.uuid4()
    author_tenant = uuid.uuid4()
    candidate = _candidate_row(
        source_authority="owner_human",
        owning_tenant_id=owner_tenant,
        author_tenant_id=author_tenant,
        value_jsonb="finance",
    )
    neighbour = _neighbour_row(
        source_authority="owner_inference",
        owning_tenant_id=owner_tenant,
        author_tenant_id=owner_tenant,
        value_jsonb="platform",
    )
    session, calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_PROPOSAL
    assert outcome.contested_with == (neighbour.claim_id,)
    claims.close_superseded.assert_not_awaited()
    assert calls["audit"][0]["action"] == actions.CLAIM_PROPOSAL_ROUTED


# ---------------------------------------------------------------------------
# NOOP: duplicate collapse -- and the audit-on-noop invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_genuine_noop_decision_from_duplicate_collapse_still_writes_an_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module's central invariant: even the decision to do nothing is
    audited. This is the case the already-settled short-circuit above is
    deliberately distinct from -- here consolidation actually ran and
    decided NOOP, so the audit row is not optional."""
    candidate = _candidate_row(value_jsonb="Platform  Team")
    neighbour = _neighbour_row(value_jsonb="platform team")
    session, calls = _router(candidate=candidate, neighbours=[neighbour])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_NOOP
    assert outcome.already_settled is False
    assert outcome.collapsed == (neighbour.claim_id,)
    assert len(calls["audit"]) == 1
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_NOOP

    # The newcomer is closed, not the survivor already carrying history.
    claims.close_superseded.assert_awaited_once()
    close_kwargs = claims.close_superseded.await_args.kwargs
    assert close_kwargs["claim_id"] == candidate.claim_id
    assert close_kwargs["survivor"] == neighbour.claim_id
    assert close_kwargs["reason"] == REASON_CLUSTER_COLLAPSED

    claims.merge_provenance.assert_awaited_once_with(session, survivor=neighbour.claim_id, collapsed=candidate.claim_id)
    claims.rescore_existing.assert_awaited_once()
    assert claims.rescore_existing.await_args.kwargs["claim_id"] == neighbour.claim_id

    assert len(calls["cluster"]) == 1
    assert calls["cluster"][0]["survivor"] == neighbour.claim_id
    assert calls["cluster"][0]["collapsed"] == candidate.claim_id


@pytest.mark.asyncio
async def test_an_exact_match_is_preferred_as_the_survivor_over_a_near_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two neighbours both match: one exactly (folded, punctuation aside),
    one only by shared identity tokens. The exact match must be kept as the
    canonical survivor rather than letting arrival order decide."""
    candidate = _candidate_row(value_jsonb="Platform Team")
    exact = _neighbour_row(value_jsonb="platform team")
    near = _neighbour_row(value_jsonb="the platform")
    session, calls = _router(candidate=candidate, neighbours=[near, exact])
    claims = _claims_service()
    service = _service(monkeypatch, claims=claims)

    outcome = await service.consolidate_in(session, claim_id=candidate.claim_id, now=_NOW)

    assert outcome.decision == DECISION_NOOP
    assert outcome.collapse_matched_by == MATCHED_EXACT
    assert outcome.collapsed[0] == exact.claim_id
    assert set(outcome.collapsed) == {exact.claim_id, near.claim_id}
    close_kwargs = claims.close_superseded.await_args.kwargs
    assert close_kwargs["survivor"] == exact.claim_id
    assert calls["audit"][0]["action"] == actions.CLAIM_CONSOLIDATED_NOOP
