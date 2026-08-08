"""Unit tests for promotion_eligibility.py: eligibility (may this claim ever
become canonical), impact classification (does a person have to look first),
and the per-tenant policy that parameterizes both.

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session --
no Postgres required, mirroring `test_promotion_sweep_worker.py`'s pattern.
Pure functions (`assess_eligibility`, `_narrows_surface` via `assess_impact`)
need no session at all.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.audit import actions
from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import AUTHORITY_UNATTRIBUTED
from contextplane.service.memory import promotion_eligibility as elig
from tests.helpers.context import tenant_context

_T0 = datetime.datetime(2026, 8, 4, 12, 0, 0, tzinfo=datetime.UTC)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claim(**overrides: Any) -> dict[str, Any]:
    """An otherwise-eligible, otherwise-not-high-impact claim. Every test
    overrides exactly the field(s) its own scenario needs, so an omitted
    field can never silently be the thing that made the assertion pass."""
    subject = overrides.pop("subject_entity_id", uuid.uuid4())
    owning = overrides.pop("owning_tenant_id", uuid.uuid4())
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "subject_entity_id": subject,
        "status": "staged",
        "consolidated_at": _T0,
        "is_contested": False,
        "promotion_state": None,
        "confidence": 0.9,
        "predicate": "owned_by_team",
        "value": "platform",
        "source_authority": "owner_human",
        "owning_tenant_id": owning,
        "author_tenant_id": owning,
    }
    base.update(overrides)
    return base


def _policy(**overrides: Any) -> elig.PromotionPolicy:
    return elig.PromotionPolicy(**overrides)


def _mapping_first(row: dict[str, Any] | None) -> MagicMock:
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    return result


def _bare_first(value: Any) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=value)
    return result


def _scalar_one(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


class _Session:
    """A session whose `execute` is driven by a single canned response,
    used by tests that only issue one query."""

    def __init__(self, response: MagicMock) -> None:
        self.executed: list[str] = []

        async def _execute(stmt: Any, params: Any = None) -> Any:
            self.executed.append(" ".join(str(stmt).split()))
            return response

        self.execute = AsyncMock(side_effect=_execute)


# ---------------------------------------------------------------------------
# assess_eligibility -- pure, no session
# ---------------------------------------------------------------------------


def test_eligible_claim_has_no_reasons() -> None:
    result = elig.assess_eligibility(_claim(), _policy())
    assert result.eligible is True
    assert result.reasons == ()
    assert result.blocked_by is None


def test_unlinked_by_status_is_ineligible() -> None:
    result = elig.assess_eligibility(_claim(status="unlinked"), _policy())
    assert result.eligible is False
    assert elig.INELIGIBLE_UNLINKED in result.reasons


def test_unlinked_by_missing_subject_is_ineligible() -> None:
    result = elig.assess_eligibility(_claim(subject_entity_id=None), _policy())
    assert elig.INELIGIBLE_UNLINKED in result.reasons


def test_superseded_status_is_not_settled() -> None:
    result = elig.assess_eligibility(_claim(status="superseded"), _policy())
    assert elig.INELIGIBLE_NOT_SETTLED in result.reasons


def test_rejected_status_is_not_settled() -> None:
    result = elig.assess_eligibility(_claim(status="rejected"), _policy())
    assert elig.INELIGIBLE_NOT_SETTLED in result.reasons


def test_missing_consolidated_at_is_not_settled() -> None:
    result = elig.assess_eligibility(_claim(consolidated_at=None), _policy())
    assert elig.INELIGIBLE_NOT_SETTLED in result.reasons


def test_two_paths_to_not_settled_are_deduplicated_but_order_preserved() -> None:
    """Rejected status *and* missing consolidated_at both name
    INELIGIBLE_NOT_SETTLED -- a curator must see it once, not twice."""
    result = elig.assess_eligibility(_claim(status="rejected", consolidated_at=None), _policy())
    assert result.reasons.count(elig.INELIGIBLE_NOT_SETTLED) == 1


def test_contested_claim_is_ineligible() -> None:
    result = elig.assess_eligibility(_claim(is_contested=True), _policy())
    assert elig.INELIGIBLE_CONTESTED in result.reasons


@pytest.mark.parametrize("state", ["proposed", "promoted"])
def test_already_proposed_or_promoted_is_ineligible(state: str) -> None:
    result = elig.assess_eligibility(_claim(promotion_state=state), _policy())
    assert elig.INELIGIBLE_ALREADY in result.reasons


def test_confidence_below_the_tenant_floor_is_ineligible() -> None:
    result = elig.assess_eligibility(_claim(confidence=0.2), _policy(confidence_floor=0.5))
    assert elig.INELIGIBLE_BELOW_FLOOR in result.reasons


def test_confidence_at_or_above_the_floor_is_not_blocked_on_that_ground() -> None:
    result = elig.assess_eligibility(_claim(confidence=0.5), _policy(confidence_floor=0.5))
    assert elig.INELIGIBLE_BELOW_FLOOR not in result.reasons


def test_missing_confidence_is_not_evaluated_against_the_floor() -> None:
    """`None` confidence (never scored) is not the same as a low score --
    the floor check is skipped rather than treating absence as zero."""
    result = elig.assess_eligibility(_claim(confidence=None), _policy(confidence_floor=0.9))
    assert elig.INELIGIBLE_BELOW_FLOOR not in result.reasons


def test_unmapped_predicate_has_no_canonical_target() -> None:
    result = elig.assess_eligibility(_claim(predicate="session_summary"), _policy())
    assert elig.INELIGIBLE_NO_TARGET in result.reasons


def test_unattributed_source_authority_is_ineligible() -> None:
    result = elig.assess_eligibility(_claim(source_authority=AUTHORITY_UNATTRIBUTED), _policy())
    assert elig.INELIGIBLE_UNATTRIBUTED in result.reasons


def test_blocked_by_reports_the_first_reason_in_the_checked_order() -> None:
    """`blocked_by` is a curator's headline -- it must be the first blocking
    reason found, not an arbitrary one from the set."""
    result = elig.assess_eligibility(_claim(status="unlinked", is_contested=True), _policy())
    assert result.blocked_by == elig.INELIGIBLE_UNLINKED


# ---------------------------------------------------------------------------
# assess_impact -- the classifier that never reads confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_confidence_alone_never_makes_a_claim_high_impact() -> None:
    """The module's own stated invariant: certainty about a change is a
    reason to review it, not a reason to skip review, so nothing here reads
    the score. A maximally-confident, otherwise-unremarkable claim must
    come back with zero impact reasons."""
    session = _Session(_bare_first(None))
    claim = _claim(confidence=1.0, predicate="owned_by_team")

    result = await elig.assess_impact(session, claim, _policy())

    assert result.high_impact is False
    assert result.reasons == ()


@pytest.mark.asyncio
async def test_blast_radius_over_the_threshold_is_high_impact() -> None:
    session = _Session(_bare_first(None))
    claim = _claim()

    result = await elig.assess_impact(session, claim, _policy(blast_radius_threshold=5), blast_radius=6)

    assert elig.IMPACT_BLAST_RADIUS in result.reasons


@pytest.mark.asyncio
async def test_blast_radius_at_the_threshold_is_not_high_impact_on_that_ground() -> None:
    session = _Session(_bare_first(None))
    claim = _claim()

    result = await elig.assess_impact(session, claim, _policy(blast_radius_threshold=5), blast_radius=5)

    assert elig.IMPACT_BLAST_RADIUS not in result.reasons


@pytest.mark.asyncio
async def test_contested_claim_is_high_impact() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(is_contested=True)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_CONTESTED in result.reasons


@pytest.mark.asyncio
async def test_cross_tenant_authorship_is_high_impact() -> None:
    owning = uuid.uuid4()
    author = uuid.uuid4()
    session = _Session(_bare_first(None))
    claim = _claim(owning_tenant_id=owning, author_tenant_id=author)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_CROSS_TENANT in result.reasons


@pytest.mark.asyncio
async def test_same_tenant_authorship_is_not_cross_tenant() -> None:
    owning = uuid.uuid4()
    session = _Session(_bare_first(None))
    claim = _claim(owning_tenant_id=owning, author_tenant_id=owning)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_CROSS_TENANT not in result.reasons


@pytest.mark.asyncio
async def test_predicate_on_the_always_review_list_is_high_impact() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="owned_by_team")

    result = await elig.assess_impact(session, claim, _policy(always_review=frozenset({"owned_by_team"})))

    assert elig.IMPACT_ALWAYS_REVIEW in result.reasons


@pytest.mark.asyncio
async def test_supersedes_a_confirmed_claim_is_high_impact() -> None:
    session = _Session(_bare_first((1,)))
    claim = _claim()

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_SUPERSEDES_CONFIRMED in result.reasons


@pytest.mark.asyncio
async def test_no_confirmation_neighbour_is_not_high_impact_on_that_ground() -> None:
    session = _Session(_bare_first(None))
    claim = _claim()

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_SUPERSEDES_CONFIRMED not in result.reasons


@pytest.mark.asyncio
async def test_multiple_impact_reasons_all_surface_at_once() -> None:
    owning = uuid.uuid4()
    author = uuid.uuid4()
    session = _Session(_bare_first((1,)))
    claim = _claim(owning_tenant_id=owning, author_tenant_id=author, is_contested=True)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_CONTESTED in result.reasons
    assert elig.IMPACT_CROSS_TENANT in result.reasons
    assert elig.IMPACT_SUPERSEDES_CONFIRMED in result.reasons


# --- surface-narrowing, per predicate --------------------------------------


@pytest.mark.parametrize("state", ["deprecated", "retired", "sunset", "DEPRECATED"])
@pytest.mark.asyncio
async def test_lifecycle_state_withdrawal_narrows_the_surface(state: str) -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="lifecycle_state", value=state)

    result = await elig.assess_impact(session, claim, _policy())

    assert result.surface_evaluated is True
    assert elig.IMPACT_NARROWS_SURFACE in result.reasons


@pytest.mark.asyncio
async def test_lifecycle_state_non_withdrawal_does_not_narrow() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="lifecycle_state", value="active")

    result = await elig.assess_impact(session, claim, _policy())

    assert result.surface_evaluated is True
    assert elig.IMPACT_NARROWS_SURFACE not in result.reasons


@pytest.mark.asyncio
async def test_deprecated_after_always_narrows() -> None:
    """Naming a deprecation date is the announcement itself."""
    session = _Session(_bare_first(None))
    claim = _claim(predicate="deprecated_after", value=_T0)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_NARROWS_SURFACE in result.reasons


@pytest.mark.asyncio
async def test_is_publicly_callable_false_narrows() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="is_publicly_callable", value=False)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_NARROWS_SURFACE in result.reasons


@pytest.mark.asyncio
async def test_is_publicly_callable_true_does_not_narrow() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="is_publicly_callable", value=True)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_NARROWS_SURFACE not in result.reasons


@pytest.mark.parametrize("value", ["<2.0", "!=1.5", "!1.0"])
@pytest.mark.asyncio
async def test_version_predicate_with_an_exclusion_token_narrows(value: str) -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="interface_version", value=value)

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_NARROWS_SURFACE in result.reasons


@pytest.mark.asyncio
async def test_version_predicate_without_an_exclusion_token_does_not_narrow() -> None:
    session = _Session(_bare_first(None))
    claim = _claim(predicate="depends_on_version", value=">=1.0")

    result = await elig.assess_impact(session, claim, _policy())

    assert elig.IMPACT_NARROWS_SURFACE not in result.reasons


@pytest.mark.asyncio
async def test_exposes_operation_is_additive_and_never_narrows() -> None:
    """Removal is expressed by the claim's interval ending, not its value."""
    session = _Session(_bare_first(None))
    claim = _claim(predicate="exposes_operation", value="anything")

    result = await elig.assess_impact(session, claim, _policy())

    assert result.surface_evaluated is True
    assert elig.IMPACT_NARROWS_SURFACE not in result.reasons


@pytest.mark.asyncio
async def test_a_non_surface_predicate_is_never_evaluated_for_narrowing() -> None:
    """`surface_evaluated=False` is the difference between "checked and
    found nothing" and "the question does not apply" -- reporting the first
    for a predicate outside SURFACE_PREDICATES would claim a guarantee that
    was never checked."""
    session = _Session(_bare_first(None))
    claim = _claim(predicate="owned_by_team", value="platform")

    result = await elig.assess_impact(session, claim, _policy())

    assert result.surface_evaluated is False
    assert elig.IMPACT_NARROWS_SURFACE not in result.reasons


# ---------------------------------------------------------------------------
# _supersedes_a_confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersedes_a_confirmation_short_circuits_on_no_subject() -> None:
    """No subject means no neighbourhood to search -- must not issue a
    query at all."""
    session = _Session(_bare_first((1,)))
    claim = _claim(subject_entity_id=None)

    result = await elig._supersedes_a_confirmation(session, claim)

    assert result is False
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersedes_a_confirmation_query_excludes_the_claim_itself() -> None:
    """The neighbourhood search must not treat the claim's own row as a
    confirming neighbour."""
    session = _Session(_bare_first((1,)))
    claim = _claim()

    await elig._supersedes_a_confirmation(session, claim)

    sql = session.executed[0]
    assert "claim_id <> :cid" in sql
    assert "confirms_claim_id IS NOT NULL" in sql
    assert "t_invalidated_at IS NULL" in sql


# ---------------------------------------------------------------------------
# blast_radius_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blast_radius_for_counts_distinct_live_dependants() -> None:
    session = _Session(_scalar_one(3))
    entity_id = uuid.uuid4()

    result = await elig.blast_radius_for(session, entity_id)

    assert result == 3
    sql = session.executed[0]
    assert "count(DISTINCT src_entity_id)" in sql
    assert "dst_entity_id = :eid" in sql
    assert "'depends_on', 'composes', 'provides_to'" in sql
    assert "t_invalidated_at IS NULL" in sql


# ---------------------------------------------------------------------------
# load_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_policy_returns_the_cautious_defaults_when_none_is_configured() -> None:
    session = _Session(_mapping_first(None))

    policy = await elig.load_policy(session, uuid.uuid4())

    assert policy == elig.PromotionPolicy()
    assert policy.blast_radius_threshold == 5
    assert policy.confidence_floor == 0.0
    assert policy.always_review == frozenset()


@pytest.mark.asyncio
async def test_load_policy_parses_a_configured_row() -> None:
    row = {"blast_radius_threshold": 10, "always_review": ["runbook_url", "owned_by_team"], "confidence_floor": 0.7}
    session = _Session(_mapping_first(row))

    policy = await elig.load_policy(session, uuid.uuid4())

    assert policy.blast_radius_threshold == 10
    assert policy.confidence_floor == 0.7
    assert policy.always_review == frozenset({"runbook_url", "owned_by_team"})


@pytest.mark.asyncio
async def test_load_policy_treats_a_null_always_review_column_as_empty() -> None:
    row = {"blast_radius_threshold": 5, "always_review": None, "confidence_floor": 0.0}
    session = _Session(_mapping_first(row))

    policy = await elig.load_policy(session, uuid.uuid4())

    assert policy.always_review == frozenset()


# ---------------------------------------------------------------------------
# set_policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_policy_requires_the_admin_role() -> None:
    session = _Session(MagicMock())
    ctx = tenant_context(roles=["producer"])

    with pytest.raises(PermissionError):
        await elig.set_policy(
            session,
            ctx,
            confidence_floor=0.5,
            blast_radius_threshold=5,
            always_review=frozenset(),
            now=_T0,
        )
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("confidence_floor", [-0.01, 1.01])
@pytest.mark.asyncio
async def test_set_policy_rejects_a_confidence_floor_outside_zero_to_one(confidence_floor: float) -> None:
    session = _Session(MagicMock())
    ctx = tenant_context(roles=["admin"])

    with pytest.raises(ValidationError, match="confidence_floor"):
        await elig.set_policy(
            session,
            ctx,
            confidence_floor=confidence_floor,
            blast_radius_threshold=5,
            always_review=frozenset(),
            now=_T0,
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_policy_rejects_a_negative_blast_radius_threshold() -> None:
    session = _Session(MagicMock())
    ctx = tenant_context(roles=["admin"])

    with pytest.raises(ValidationError, match="blast_radius_threshold"):
        await elig.set_policy(
            session,
            ctx,
            confidence_floor=0.5,
            blast_radius_threshold=-1,
            always_review=frozenset(),
            now=_T0,
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_policy_writes_upsert_and_audits_the_new_values() -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    ctx = tenant_context(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"])

    calls: dict[str, list[dict[str, Any]]] = {"upsert": [], "audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "INSERT INTO memory_promotion_policy" in sql:
            assert "ON CONFLICT (tenant_id) DO UPDATE" in sql
            calls["upsert"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL: {sql}")

    session = AsyncMock()
    session.execute = _execute

    result = await elig.set_policy(
        session,
        ctx,
        confidence_floor=0.65,
        blast_radius_threshold=8,
        always_review=frozenset({"owned_by_team", "runbook_url"}),
        now=_T0,
    )

    assert result == elig.PromotionPolicy(
        blast_radius_threshold=8,
        always_review=frozenset({"owned_by_team", "runbook_url"}),
        confidence_floor=0.65,
    )

    upsert_params = calls["upsert"][0]
    assert upsert_params["tid"] == tenant_id
    assert upsert_params["threshold"] == 8
    assert upsert_params["floor"] == 0.65
    assert upsert_params["actor"] == actor_id

    audit_params = calls["audit"][0]
    assert audit_params["action"] == actions.PROMOTION_POLICY_SET
    assert audit_params["tid"] == tenant_id
    assert audit_params["aid"] == actor_id
