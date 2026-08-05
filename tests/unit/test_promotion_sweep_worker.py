"""Unit tests for PromotionSweepWorker.

All DB interaction is mocked at session.execute via an SQL-string-keyed router —
no Postgres is required, mirroring test_workspace_service.py's / the closure-
refresh worker's mock-factory pattern. `PromotionService` and `GuardrailService`
are each replaced with lightweight mocks so these tests exercise only the sweep's
own wiring: candidate selection, per-claim outcome bucketing and isolation, the
guardrail-permitted auto-accept path (system-curator actor + roles), and the
sweep's own wrapper audit row.

Coverage:
- `_candidates` / `_refresh_pending_gauge`: the exact predicate the sweep scans on.
- `run_once`: empty batch, an ineligible claim, a guardrail-blocked claim, a
  guardrail-permitted (auto-promoted) claim, per-claim failure isolation for both
  `propose` and `accept`, and a failed wrapper-audit write that must not roll back
  or re-count an already-committed promotion.
- `resolve_system_curator_actor`: provisions on first use, caches thereafter —
  mirrors `registry.ingest.runner.resolve_sync_actor`'s own test coverage.
- The sweep's constructed system context (`roles=frozenset({"admin"})`) passes
  `PromotionService._assert_may_review` for a proposal owned by its own tenant,
  and is refused for one it does not own -- the elevated role must never also
  bypass tenant scoping.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.memory.promotion import PromotionService, Proposal
from registry.service.memory.promotion_guardrails import AutoPromoteDecision
from registry.storage.models import Actor
from registry.workers import promotion_sweep as sweep_module
from registry.workers.promotion_sweep import PromotionSweepWorker, SweepReport
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 4, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _proposal(
    *,
    proposal_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    owner_tenant_id: uuid.UUID | None = None,
    author_tenant_id: uuid.UUID | None = None,
    predicate: str = "owned_by_team",
    high_impact_reasons: tuple[str, ...] = (),
) -> Proposal:
    owner = owner_tenant_id or uuid.uuid4()
    return Proposal(
        proposal_id=proposal_id or uuid.uuid4(),
        claim_id=claim_id or uuid.uuid4(),
        owner_tenant_id=owner,
        author_tenant_id=author_tenant_id or owner,
        subject_entity_id=uuid.uuid4(),
        predicate=predicate,
        target_kind="attribute",
        target_key=predicate,
        current_value=None,
        proposed_value="platform",
        valid_from=_NOW,
        valid_to=None,
        high_impact_reasons=high_impact_reasons,
    )


def _make_session_factory(
    *,
    candidate_ids: list[uuid.UUID] | None = None,
    pending_count: int = 0,
    audit_insert_side_effect: Exception | None = None,
) -> tuple[MagicMock, list[str]]:
    """SQL-string-keyed AsyncMock session factory.

    Routes:
    - ``SELECT claim_id FROM memory_claims ...``  -> ``candidate_ids``
    - ``SELECT count(*) FROM memory_claims ...``  -> ``pending_count``
    - ``INSERT INTO audit_log ...``               -> no-op, or raises if
      ``audit_insert_side_effect`` is given.

    Returns the factory and a list every executed SQL statement (whitespace-
    collapsed) is appended to, so a test can assert on the exact predicate.
    """
    candidates = candidate_ids or []
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()
        if "SELECT claim_id FROM memory_claims" in sql:
            rows = []
            for cid in candidates:
                row = MagicMock()
                row.claim_id = cid
                rows.append(row)
            result.all = MagicMock(return_value=rows)
            return result
        if "SELECT count(*) FROM memory_claims" in sql:
            result.scalar_one = MagicMock(return_value=pending_count)
            return result
        if "INSERT INTO audit_log" in sql:
            if audit_insert_side_effect is not None:
                raise audit_insert_side_effect
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


def _worker(
    *,
    promotion: Any = None,
    guardrails: Any = None,
    candidate_ids: list[uuid.UUID] | None = None,
    pending_count: int = 0,
    audit_insert_side_effect: Exception | None = None,
    batch_size: int = 100,
) -> tuple[PromotionSweepWorker, list[str]]:
    factory, executed = _make_session_factory(
        candidate_ids=candidate_ids,
        pending_count=pending_count,
        audit_insert_side_effect=audit_insert_side_effect,
    )
    worker = PromotionSweepWorker(
        factory,
        promotion or MagicMock(),
        guardrails or MagicMock(),
        clock=FakeClock(_NOW),
        batch_size=batch_size,
    )
    return worker, executed


# ---------------------------------------------------------------------------
# _candidates / _refresh_pending_gauge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_query_scans_staged_consolidated_unproposed_claims() -> None:
    """The exact predicate the sweep scans on: staged, subject-resolved,
    consolidated, and never yet proposed."""
    worker, executed = _worker(candidate_ids=[])
    await worker._candidates()

    sql = next(s for s in executed if "SELECT claim_id FROM memory_claims" in s)
    assert "status = 'staged'" in sql
    assert "subject_entity_id IS NOT NULL" in sql
    assert "consolidated_at IS NOT NULL" in sql
    assert "promotion_state IS NULL" in sql
    assert "t_invalidated_at IS NULL" in sql
    assert "ORDER BY created_at" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_candidates_returns_claim_ids_in_query_order() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    worker, _ = _worker(candidate_ids=ids)
    result = await worker._candidates()
    assert result == ids


@pytest.mark.asyncio
async def test_refresh_pending_gauge_reads_the_same_predicate_without_a_limit() -> None:
    worker, executed = _worker(pending_count=7)
    await worker._refresh_pending_gauge()

    sql = next(s for s in executed if "SELECT count(*) FROM memory_claims" in s)
    assert "status = 'staged'" in sql
    assert "promotion_state IS NULL" in sql
    assert "LIMIT" not in sql
    assert sweep_module._PENDING._value.get() == 7


# ---------------------------------------------------------------------------
# run_once: empty batch, ineligible, guardrail-blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_empty_candidates_is_not_an_error() -> None:
    promotion = MagicMock()
    promotion.propose = AsyncMock()
    worker, _ = _worker(promotion=promotion, candidate_ids=[])

    report = await worker.run_once()

    assert report == SweepReport(considered=0, auto_promoted=0, awaiting_review=0, not_eligible=0, failed=0)
    assert not report.had_work
    promotion.propose.assert_not_awaited()


@pytest.mark.asyncio
async def test_ineligible_claim_is_not_proposed_further_and_not_auto_promoted() -> None:
    """`propose` returning None (below the floor, contested, no target, etc.) is
    an ordinary outcome, not a failure, and never reaches the guardrail check."""
    claim_id = uuid.uuid4()
    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=None)
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock()
    worker, _ = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=[claim_id])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.not_eligible == 1
    assert report.auto_promoted == 0
    assert report.awaiting_review == 0
    assert report.failed == 0
    guardrails.may_auto_promote.assert_not_awaited()


@pytest.mark.asyncio
async def test_guardrail_blocked_proposal_awaits_human_review() -> None:
    """Eligible for promotion but not permitted to skip review: `accept` is
    never called, and the proposal is left open for the queue."""
    claim_id = uuid.uuid4()
    proposal = _proposal(claim_id=claim_id)
    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=proposal)
    promotion.accept = AsyncMock()
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(
        return_value=AutoPromoteDecision(permitted=False, blocked_by=("predicate is not on the tenant's allowlist",))
    )
    worker, _ = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=[claim_id])

    report = await worker.run_once()

    assert report.awaiting_review == 1
    assert report.auto_promoted == 0
    promotion.accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_may_auto_promote_is_called_with_eligible_true_and_the_proposals_own_fields() -> None:
    """`eligible=True` unconditionally: `propose` having returned a proposal at
    all is what already answered the eligibility question."""
    claim_id = uuid.uuid4()
    owner = uuid.uuid4()
    proposal = _proposal(claim_id=claim_id, owner_tenant_id=owner, author_tenant_id=owner, predicate="runbook_url")
    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=proposal)
    promotion.accept = AsyncMock(return_value=uuid.uuid4())
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(return_value=AutoPromoteDecision(permitted=False, blocked_by=("x",)))
    worker, _ = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=[claim_id])

    await worker.run_once()

    guardrails.may_auto_promote.assert_awaited_once_with(
        tenant_id=owner,
        predicate="runbook_url",
        high_impact=False,
        eligible=True,
        author_is_owner=True,
    )


# ---------------------------------------------------------------------------
# run_once: guardrail-permitted auto-promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardrail_permitted_proposal_is_accepted_as_the_system_curator(monkeypatch: pytest.MonkeyPatch) -> None:
    claim_id = uuid.uuid4()
    owner = uuid.uuid4()
    system_actor_id = uuid.uuid4()
    proposal = _proposal(claim_id=claim_id, owner_tenant_id=owner, author_tenant_id=owner)
    promotion_id = uuid.uuid4()

    resolve_actor = AsyncMock(return_value=system_actor_id)
    monkeypatch.setattr(sweep_module, "resolve_system_curator_actor", resolve_actor)

    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=proposal)
    promotion.accept = AsyncMock(return_value=promotion_id)
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(return_value=AutoPromoteDecision(permitted=True, blocked_by=()))
    worker, executed = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=[claim_id])

    report = await worker.run_once()

    assert report.auto_promoted == 1
    assert report.awaiting_review == 0
    assert report.failed == 0

    # The sweep's own roles, never the sync-worker precedent's -- see the
    # module docstring for why ["sync_worker"] would fail the review gate.
    promotion.accept.assert_awaited_once_with(
        proposal.proposal_id,
        actor_tenant_id=owner,
        actor_id=system_actor_id,
        roles=frozenset({"admin"}),
    )

    # The wrapper audit row: a second, sweep-owned record naming the system
    # actor and the guardrail decision, alongside accept()'s own audit write.
    audit_sql = [s for s in executed if "INSERT INTO audit_log" in s]
    assert len(audit_sql) == 1


@pytest.mark.asyncio
async def test_wrapper_audit_row_names_the_promotion_the_actor_and_the_guardrail_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = uuid.uuid4()
    owner = uuid.uuid4()
    system_actor_id = uuid.uuid4()
    proposal = _proposal(claim_id=claim_id, owner_tenant_id=owner, author_tenant_id=owner, predicate="owned_by_team")
    promotion_id = uuid.uuid4()

    monkeypatch.setattr(sweep_module, "resolve_system_curator_actor", AsyncMock(return_value=system_actor_id))

    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=proposal)
    promotion.accept = AsyncMock(return_value=promotion_id)
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(return_value=AutoPromoteDecision(permitted=True, blocked_by=()))

    captured: dict[str, Any] = {}

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "INSERT INTO audit_log" in sql:
            captured["params"] = params
            return MagicMock()
        result = MagicMock()
        if "SELECT claim_id FROM memory_claims" in sql:
            row = MagicMock()
            row.claim_id = claim_id
            result.all = MagicMock(return_value=[row])
            return result
        if "SELECT count(*) FROM memory_claims" in sql:
            result.scalar_one = MagicMock(return_value=0)
            return result
        raise AssertionError(f"unexpected SQL: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    worker = PromotionSweepWorker(factory, promotion, guardrails, clock=FakeClock(_NOW))
    await worker.run_once()

    assert captured["params"]["action"] == sweep_module._AUTO_PROMOTION_AUDIT_ACTION
    assert captured["params"]["aid"] == system_actor_id
    assert captured["params"]["tid"] == owner
    assert captured["params"]["target"] == claim_id
    import json as _json

    after = _json.loads(captured["params"]["after"])
    assert after["promotion_id"] == str(promotion_id)
    assert after["proposal_id"] == str(proposal.proposal_id)
    assert after["auto_promoted"] is True
    assert after["system_actor_id"] == str(system_actor_id)
    assert after["guardrail_decision"] == {"permitted": True, "blocked_by": []}


# ---------------------------------------------------------------------------
# Per-claim failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_claim_failing_propose_does_not_stop_the_batch() -> None:
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    failing = ids[1]

    async def _propose(claim_id: uuid.UUID) -> Proposal | None:
        if claim_id == failing:
            msg = "this neighbourhood is pathological"
            raise RuntimeError(msg)
        return None

    promotion = MagicMock()
    promotion.propose = AsyncMock(side_effect=_propose)
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock()
    worker, _ = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=ids)

    report = await worker.run_once()

    assert report.considered == 3
    assert report.failed == 1
    assert report.not_eligible == 2


@pytest.mark.asyncio
async def test_one_claim_failing_accept_does_not_stop_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_module, "resolve_system_curator_actor", AsyncMock(return_value=uuid.uuid4()))

    ok_claim, failing_claim = uuid.uuid4(), uuid.uuid4()
    proposals = {
        ok_claim: _proposal(claim_id=ok_claim),
        failing_claim: _proposal(claim_id=failing_claim),
    }

    async def _propose(claim_id: uuid.UUID) -> Proposal | None:
        return proposals[claim_id]

    async def _accept(proposal_id: uuid.UUID, **kwargs: Any) -> uuid.UUID:
        if proposal_id == proposals[failing_claim].proposal_id:
            msg = "only the tenant that owns the subject may act on this proposal"
            raise PermissionError(msg)
        return uuid.uuid4()

    promotion = MagicMock()
    promotion.propose = AsyncMock(side_effect=_propose)
    promotion.accept = AsyncMock(side_effect=_accept)
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(return_value=AutoPromoteDecision(permitted=True, blocked_by=()))

    worker, _ = _worker(promotion=promotion, guardrails=guardrails, candidate_ids=[ok_claim, failing_claim])

    report = await worker.run_once()

    assert report.considered == 2
    assert report.auto_promoted == 1
    assert report.failed == 1


@pytest.mark.asyncio
async def test_wrapper_audit_write_failure_does_not_fail_an_already_committed_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promotion itself already committed by the time the wrapper audit row is
    written; a failure writing that second, best-effort row must not roll it back
    or count an already-real promotion as a failed claim."""
    claim_id = uuid.uuid4()
    proposal = _proposal(claim_id=claim_id)

    monkeypatch.setattr(sweep_module, "resolve_system_curator_actor", AsyncMock(return_value=uuid.uuid4()))

    promotion = MagicMock()
    promotion.propose = AsyncMock(return_value=proposal)
    promotion.accept = AsyncMock(return_value=uuid.uuid4())
    guardrails = MagicMock()
    guardrails.may_auto_promote = AsyncMock(return_value=AutoPromoteDecision(permitted=True, blocked_by=()))

    worker, _ = _worker(
        promotion=promotion,
        guardrails=guardrails,
        candidate_ids=[claim_id],
        audit_insert_side_effect=RuntimeError("audit_log write failed"),
    )

    report = await worker.run_once()

    assert report.auto_promoted == 1
    assert report.failed == 0
    promotion.accept.assert_awaited_once()


# ---------------------------------------------------------------------------
# resolve_system_curator_actor: provision + cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_system_curator_actor_provisions_and_caches() -> None:
    sweep_module._actor_cache.clear()
    tenant_id = uuid.uuid4()

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    provisioned: list[Actor] = []
    session.add = MagicMock(side_effect=lambda obj: provisioned.append(obj) if isinstance(obj, Actor) else None)
    session.flush = AsyncMock()

    actor_id_1 = await sweep_module.resolve_system_curator_actor(session, tenant_id, clock=FakeClock(_NOW))

    assert len(provisioned) == 1
    assert provisioned[0].actor_kind == "system_curator"
    assert provisioned[0].display_name == "system-curator"
    assert provisioned[0].oidc_subject == f"system-curator:{tenant_id.hex}"

    execute_count_before = session.execute.call_count
    actor_id_2 = await sweep_module.resolve_system_curator_actor(session, tenant_id, clock=FakeClock(_NOW))
    assert session.execute.call_count == execute_count_before  # cached: no new query
    assert actor_id_1 == actor_id_2

    sweep_module._actor_cache.clear()


@pytest.mark.asyncio
async def test_resolve_system_curator_actor_returns_existing_without_provisioning() -> None:
    sweep_module._actor_cache.clear()
    tenant_id = uuid.uuid4()
    existing_id = uuid.uuid4()

    existing = MagicMock()
    existing.actor_id = existing_id

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()

    actor_id = await sweep_module.resolve_system_curator_actor(session, tenant_id, clock=FakeClock(_NOW))

    assert actor_id == existing_id
    session.add.assert_not_called()

    sweep_module._actor_cache.clear()


# ---------------------------------------------------------------------------
# System-curator roles vs _assert_may_review
# ---------------------------------------------------------------------------


def test_system_curator_roles_pass_review_for_an_owned_proposal_and_fail_for_a_non_owned_one() -> None:
    """The sweep's roles are elevated (`admin`); its tenant scoping is not. A
    proposal owned by the sweep's own tenant passes the same gate a human
    reviewer would need to pass; a proposal owned by a different tenant is
    refused exactly as it would be for a human holding the same roles."""
    promotion = PromotionService(MagicMock(), claims=MagicMock(), clock=FakeClock(_NOW))
    owner_tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    proposal = {"owner_tenant_id": owner_tenant}

    # Passes: same tenant, the sweep's own roles.
    promotion._assert_may_review(proposal, owner_tenant, frozenset({"admin"}))

    # Refused: same roles, a tenant that does not own the proposal. Broad
    # `Exception` on purpose -- the exact refusal type is mid-rebase onto the
    # unified error tree elsewhere and is not this test's concern; what matters
    # is that the elevated role alone never satisfies the tenant check.
    with pytest.raises(Exception):  # noqa: B017
        promotion._assert_may_review(proposal, other_tenant, frozenset({"admin"}))
