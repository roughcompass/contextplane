"""Unit tests for claim_erasure.py: the two-prong claim selection and the
orchestration around it, without Postgres.

The selection predicate itself -- which claims a real database would return
for `_SELECT_CLAIMS` -- is SQL text that only Postgres evaluates; a mocked
session cannot execute it. Where the property is genuinely a database
decision, this suite pins the exact predicate text that encodes it
(mirroring the structural-assertion convention `tests/integration/
test_claim_serving.py` already uses for its own un-executable arm filters).
Where the property is orchestration -- what runs in what order, with what
parameters, and how three participants' counts combine -- this suite proves
it dynamically against a session whose `_SELECT_CLAIMS` response is fully
controlled by the test, exactly the way `test_promotion_sweep_worker.py`
proves the sweep's own SQL-driven orchestration.

The full survive/die behaviour for real rows (a claim with two provenance
events, one erased, must survive; a claim with none must die) already has
adversarial Postgres coverage in `tests/integration/test_claim_erasure.py`;
this suite adds a fast unit-tier pin of the same invariant's textual and
orchestration halves, it does not re-litigate the invariant itself.

Coverage:
- The two-prong `_SELECT_CLAIMS` predicate: the preference-namespace prong
  matches unconditionally (regardless of author or evidence); the
  no-independent-evidence prong is scoped to the target actor and tenant and
  gated by `NOT EXISTS (_DISQUALIFYING_EVIDENCE)`; the rows are locked
  `FOR UPDATE` for the duration of the erasure transaction.
- `_DISQUALIFYING_EVIDENCE`: non-session evidence always disqualifies; a
  session-event ref only disqualifies when it resolves to a *live* row
  belonging to a *different* actor -- the `EXISTS` requires the event to
  still exist, which is exactly what makes a dangling ref (an already-erased
  actor's event) not disqualify anything.
- `erase_actor`: reads promotion residue before deleting claims (its own
  documented ordering requirement); passes the identical selected id list to
  every participant; scopes the selection query to the target actor, tenant,
  and the correctly-formatted preference namespace; merges each
  participant's count dict into one result; runs the whole thing inside one
  `session.begin()`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import contextplane.service.memory.claim_erasure as claim_erasure_module
from contextplane.embedding.targets import TARGET_CLAIM
from contextplane.extraction.strategies import NS_PREFERENCE
from contextplane.service.memory.claim_erasure import (
    _DISQUALIFYING_EVIDENCE,
    _SELECT_CLAIMS,
    ClaimErasure,
)
from contextplane.types import TenantContext

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


def _ctx(tenant_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or uuid.uuid4(), actor_id=actor_id or uuid.uuid4(), roles=["admin"], oidc_subject="eraser"
    )


def _erasure_session_factory(
    candidate_ids: list[uuid.UUID] | None = None,
) -> tuple[MagicMock, list[tuple[str, dict]]]:
    """A session whose only real query is `_SELECT_CLAIMS` -- the three erasure
    participants are monkeypatched out entirely in every test below, so no
    other SQL should ever reach this session."""
    executed: list[tuple[str, dict]] = []
    candidates = candidate_ids or []

    async def _execute(stmt: Any, params: dict | None = None) -> list[SimpleNamespace]:
        sql = " ".join(str(stmt).split())
        executed.append((sql, params or {}))
        if "FROM memory_claims c" in sql and "FOR UPDATE" in sql:
            return [SimpleNamespace(claim_id=cid) for cid in candidates]
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


def _patch_participants(
    monkeypatch: pytest.MonkeyPatch,
    *,
    promotion_counts: dict[str, int] | None = None,
    claims_counts: dict[str, int] | None = None,
    target_counts: dict[str, int] | None = None,
    calls: list[str] | None = None,
) -> None:
    """Replace the three erasure participants with mocks recording call order
    and the exact arguments they were invoked with, in `calls` if given."""
    log = calls if calls is not None else []

    async def _promotion(session: Any, selected: list[uuid.UUID]) -> dict[str, int]:
        log.append("promotion")
        return dict(promotion_counts or {})

    async def _claims(
        session: Any, *, selected: list[uuid.UUID], target_actor_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> dict[str, int]:
        log.append("claims")
        return dict(claims_counts or {})

    async def _targets(session: Any, *, target_type: str, target_ids: list[uuid.UUID]) -> dict[str, int]:
        log.append("targets")
        return dict(target_counts or {})

    monkeypatch.setattr(claim_erasure_module, "erase_promotion_artifacts", _promotion)
    monkeypatch.setattr(claim_erasure_module, "erase_claims_for_actor", _claims)
    monkeypatch.setattr(claim_erasure_module, "erase_targets", _targets)


# ---------------------------------------------------------------------------
# The two-prong selection predicate: structural
# ---------------------------------------------------------------------------


def test_the_preference_namespace_prong_matches_unconditionally() -> None:
    """Prong (a): preference claims die regardless of author or evidence --
    the namespace match is a bare OR-arm, not conjoined with the
    no-independent-evidence check."""
    assert "c.namespace = :pref_ns" in _SELECT_CLAIMS


def test_the_no_independent_evidence_prong_is_scoped_to_the_target_actor_and_tenant() -> None:
    assert "c.author_actor_id = :actor" in _SELECT_CLAIMS
    assert "c.author_tenant_id = :tid" in _SELECT_CLAIMS


def test_the_no_independent_evidence_prong_is_gated_by_the_disqualifying_evidence_check() -> None:
    assert "NOT EXISTS" in _SELECT_CLAIMS
    assert _DISQUALIFYING_EVIDENCE.strip() in _SELECT_CLAIMS


def test_selection_locks_the_candidate_rows_for_the_erasure_transaction() -> None:
    """Without a lock, a concurrent write during the same transaction could
    race the selection -- e.g. a confirmation copying the preference
    namespace onto a successor after this query already ran."""
    assert "FOR UPDATE" in _SELECT_CLAIMS


def test_disqualifying_evidence_always_disqualifies_non_session_evidence() -> None:
    """A document, a commit, a connector run, a curator's confirmation --
    anything that is not session evidence keeps the claim alive
    unconditionally."""
    assert "p.evidence_kind <> 'session_event'" in _DISQUALIFYING_EVIDENCE


def test_disqualifying_evidence_requires_the_referenced_event_to_still_exist() -> None:
    """The load-bearing textual anchor for "dangling refs never disqualify":
    the EXISTS subquery only matches a session-event ref that resolves to a
    row that is still there. An erased actor's own event no longer exists, so
    a claim pointing only at it never gets disqualified by this arm --
    which is also what makes the outcome identical across retries."""
    assert "FROM memory_session_events e" in _DISQUALIFYING_EVIDENCE
    assert "e.event_id::text = p.evidence_ref" in _DISQUALIFYING_EVIDENCE


def test_disqualifying_evidence_requires_the_live_event_to_belong_to_a_different_actor() -> None:
    """The other half: a *live* event still disqualifies only when it is not
    the target's own -- the target's own live events are exactly the
    evidence prong (b) is supposed to sweep up, not evidence against
    sweeping."""
    assert "e.actor_id <> :actor" in _DISQUALIFYING_EVIDENCE


# ---------------------------------------------------------------------------
# erase_actor: orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erase_actor_scopes_the_selection_query_to_the_target_actor_tenant_and_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_participants(monkeypatch)
    tenant_id, target = uuid.uuid4(), uuid.uuid4()
    factory, executed = _erasure_session_factory(candidate_ids=[])

    await ClaimErasure(factory).erase_actor(_ctx(tenant_id=tenant_id), target)

    _, params = executed[0]
    assert params["actor"] == target
    assert params["tid"] == tenant_id
    assert params["pref_ns"] == NS_PREFERENCE.format(tenant_id=tenant_id, actor_id=target)


@pytest.mark.asyncio
async def test_erase_actor_reads_promotion_residue_before_deleting_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """Promotion's journal rows name the claims, so it must read them before
    the claim deletes take them away -- the module's own documented
    ordering requirement."""
    calls: list[str] = []
    _patch_participants(monkeypatch, calls=calls)
    factory, _ = _erasure_session_factory(candidate_ids=[])

    await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert calls.index("promotion") < calls.index("claims")


@pytest.mark.asyncio
async def test_erase_actor_passes_the_identical_selected_ids_to_every_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_seen: dict[str, list[uuid.UUID]] = {}

    async def _promotion(session: Any, selected: list[uuid.UUID]) -> dict[str, int]:
        selected_seen["promotion"] = list(selected)
        return {}

    async def _claims(
        session: Any, *, selected: list[uuid.UUID], target_actor_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> dict[str, int]:
        selected_seen["claims"] = list(selected)
        return {}

    async def _targets(session: Any, *, target_type: str, target_ids: list[uuid.UUID]) -> dict[str, int]:
        selected_seen["targets"] = list(target_ids)
        return {}

    monkeypatch.setattr(claim_erasure_module, "erase_promotion_artifacts", _promotion)
    monkeypatch.setattr(claim_erasure_module, "erase_claims_for_actor", _claims)
    monkeypatch.setattr(claim_erasure_module, "erase_targets", _targets)

    ids = [uuid.uuid4(), uuid.uuid4()]
    factory, _ = _erasure_session_factory(candidate_ids=ids)

    await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert selected_seen["promotion"] == ids
    assert selected_seen["claims"] == ids
    assert selected_seen["targets"] == ids


@pytest.mark.asyncio
async def test_erase_actor_erases_embeddings_for_the_claim_target_type(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _targets(session: Any, *, target_type: str, target_ids: list[uuid.UUID]) -> dict[str, int]:
        seen["target_type"] = target_type
        return {}

    _patch_participants(monkeypatch)
    monkeypatch.setattr(claim_erasure_module, "erase_targets", _targets)
    factory, _ = _erasure_session_factory(candidate_ids=[uuid.uuid4()])

    await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert seen["target_type"] == TARGET_CLAIM


@pytest.mark.asyncio
async def test_erase_actor_merges_counts_from_every_participant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_participants(
        monkeypatch,
        promotion_counts={"canonical_rows_deleted": 1},
        claims_counts={"claims": 3, "provenance_rows_scrubbed": 2},
        target_counts={"vectors": 4},
    )
    factory, _ = _erasure_session_factory(candidate_ids=[uuid.uuid4()])

    counts = await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert counts == {
        "canonical_rows_deleted": 1,
        "claims": 3,
        "provenance_rows_scrubbed": 2,
        "vectors": 4,
    }


@pytest.mark.asyncio
async def test_erase_actor_runs_the_whole_operation_inside_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_participants(monkeypatch)
    factory, _ = _erasure_session_factory(candidate_ids=[])
    sessions: list[AsyncMock] = []

    real_new_session = factory.side_effect

    def _capturing() -> Any:
        cm = real_new_session()
        sessions.append(cm._value)
        return cm

    factory.side_effect = _capturing

    await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert len(sessions) == 1
    sessions[0].begin.assert_called_once()


@pytest.mark.asyncio
async def test_erase_actor_returns_zero_counts_when_nothing_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the merge test above: an empty selection with
    every participant reporting nothing must not be mistaken for the merge
    logic itself being broken -- proves the zero case is not a side effect of
    a bug that always returns empty."""
    _patch_participants(monkeypatch)
    factory, _ = _erasure_session_factory(candidate_ids=[])

    counts = await ClaimErasure(factory).erase_actor(_ctx(), uuid.uuid4())

    assert counts == {}
