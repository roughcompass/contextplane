"""Unit tests for GuardrailService (`may_auto_promote`'s decision table, the
allowlist reads/writes that feed it, and their audit rows).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` router --
no Postgres required, mirroring `test_promotion_sweep_worker.py`'s pattern.

`may_auto_promote` rests on four independent conditions -- not high-impact,
eligible, author-is-owner, allowlisted -- each checked and reported
separately by the module's own design ("checked separately... so that
removing any single one fails a test rather than quietly widening what
promotes"). The decision-table tests below take that statement literally:
each holds three of the four conditions at their permitting value and flips
exactly one to its blocking value, so a future edit that deletes any one of
the four `if` branches in `may_auto_promote` fails exactly the test named
for that condition -- not some other one, and not all of them at once,
which is what would happen if the checks were folded into a single
predicate.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.audit import actions
from registry.service.memory.promotion_guardrails import (
    BLOCKED_HIGH_IMPACT,
    BLOCKED_INELIGIBLE,
    BLOCKED_NOT_ALLOWLISTED,
    BLOCKED_NOT_OWNER,
    GuardrailService,
)
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


def _router(
    *,
    allowlisted_predicates: frozenset[str] = frozenset(),
) -> tuple[MagicMock, list[str], list[dict[str, Any]]]:
    """SQL-string-keyed router for GuardrailService.

    Routes:
    - ``SELECT predicate FROM memory_autopromote_allowlist`` -> the fixed
      ``allowlisted_predicates`` set, regardless of the tenant param (every
      test here uses one tenant).
    - ``INSERT``/``DELETE INTO memory_autopromote_allowlist`` -> no-op.
    - ``INSERT INTO audit_log`` -> captured, not executed.

    Returns the factory, every executed SQL statement (whitespace-
    collapsed), and every ``audit_log`` insert's params.
    """
    executed: list[str] = []
    audit_calls: list[dict[str, Any]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        if "SELECT predicate FROM memory_autopromote_allowlist" in sql:
            result = MagicMock()
            result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=list(allowlisted_predicates))))
            return result
        if "INSERT INTO memory_autopromote_allowlist" in sql:
            return MagicMock()
        if "DELETE FROM memory_autopromote_allowlist" in sql:
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            audit_calls.append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed, audit_calls


def _service(*, allowlisted_predicates: frozenset[str] = frozenset()) -> tuple[GuardrailService, list[str], list[dict]]:
    factory, executed, audit_calls = _router(allowlisted_predicates=allowlisted_predicates)
    return GuardrailService(factory, clock=FakeClock(_NOW)), executed, audit_calls


_PREDICATE = "owned_by_team"


async def _decision(
    service: GuardrailService,
    *,
    tenant_id: uuid.UUID,
    predicate: str = _PREDICATE,
    high_impact: bool = False,
    eligible: bool = True,
    author_is_owner: bool = True,
) -> Any:
    return await service.may_auto_promote(
        tenant_id=tenant_id,
        predicate=predicate,
        high_impact=high_impact,
        eligible=eligible,
        author_is_owner=author_is_owner,
    )


# ---------------------------------------------------------------------------
# The all-pass baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_conditions_satisfied_permits_auto_promotion() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({_PREDICATE}))

    decision = await _decision(service, tenant_id=tenant_id)

    assert decision.permitted is True
    assert decision.blocked_by == ()


# ---------------------------------------------------------------------------
# Each of the four conditions, pinned independently: exactly the one flipped
# condition blocks, and nothing else does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_impact_alone_blocks_and_names_only_that_reason() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({_PREDICATE}))

    decision = await _decision(service, tenant_id=tenant_id, high_impact=True)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_HIGH_IMPACT,)


@pytest.mark.asyncio
async def test_ineligible_alone_blocks_and_names_only_that_reason() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({_PREDICATE}))

    decision = await _decision(service, tenant_id=tenant_id, eligible=False)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_INELIGIBLE,)


@pytest.mark.asyncio
async def test_author_not_owner_alone_blocks_and_names_only_that_reason() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({_PREDICATE}))

    decision = await _decision(service, tenant_id=tenant_id, author_is_owner=False)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_NOT_OWNER,)


@pytest.mark.asyncio
async def test_not_allowlisted_alone_blocks_and_names_only_that_reason() -> None:
    """The default, empty-allowlist posture: a fresh tenant has opted
    nothing in, so an otherwise-perfect claim still waits for a human."""
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset())

    decision = await _decision(service, tenant_id=tenant_id)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_NOT_ALLOWLISTED,)


# ---------------------------------------------------------------------------
# Multiple simultaneous failures: every one is reported, in the module's own
# checking order, not just the first (an operator asking "why did this not
# promote" needs the whole answer).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_conditions_failing_reports_all_four_in_order() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset())

    decision = await _decision(
        service,
        tenant_id=tenant_id,
        high_impact=True,
        eligible=False,
        author_is_owner=False,
    )

    assert decision.permitted is False
    assert decision.blocked_by == (
        BLOCKED_HIGH_IMPACT,
        BLOCKED_INELIGIBLE,
        BLOCKED_NOT_OWNER,
        BLOCKED_NOT_ALLOWLISTED,
    )


@pytest.mark.asyncio
async def test_two_failing_conditions_are_evaluated_independently_not_short_circuited() -> None:
    """High-impact and ineligible failing together must both surface -- proof
    the four checks are not folded into one predicate that stops at the
    first true condition."""
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({_PREDICATE}))

    decision = await _decision(service, tenant_id=tenant_id, high_impact=True, eligible=False)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_HIGH_IMPACT, BLOCKED_INELIGIBLE)


@pytest.mark.asyncio
async def test_allowlist_membership_is_scoped_to_the_requested_predicate() -> None:
    """A different predicate being allowlisted does not permit this one."""
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({"runbook_url"}))

    decision = await _decision(service, tenant_id=tenant_id, predicate=_PREDICATE)

    assert decision.permitted is False
    assert decision.blocked_by == (BLOCKED_NOT_ALLOWLISTED,)


# ---------------------------------------------------------------------------
# allowlist_for / allow / revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_for_returns_the_tenants_predicates() -> None:
    tenant_id = uuid.uuid4()
    service, _, _ = _service(allowlisted_predicates=frozenset({"owned_by_team", "runbook_url"}))

    result = await service.allowlist_for(tenant_id)

    assert result == frozenset({"owned_by_team", "runbook_url"})


@pytest.mark.asyncio
async def test_allow_inserts_on_conflict_do_nothing_and_audits_the_predicate() -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    service, executed, audit_calls = _service()

    await service.allow(tenant_id, "owned_by_team", actor_id=actor_id)

    insert_sql = next(s for s in executed if "INSERT INTO memory_autopromote_allowlist" in s)
    assert "ON CONFLICT (tenant_id, predicate) DO NOTHING" in insert_sql
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == actions.CLAIM_AUTOPROMOTE_ALLOWED
    assert audit_calls[0]["tid"] == tenant_id
    assert audit_calls[0]["aid"] == actor_id
    assert audit_calls[0]["after"] == '{"predicate": "owned_by_team"}'


@pytest.mark.asyncio
async def test_revoke_deletes_the_entry_and_audits_the_predicate() -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    service, executed, audit_calls = _service()

    await service.revoke(tenant_id, "owned_by_team", actor_id=actor_id)

    delete_sql = next(s for s in executed if "DELETE FROM memory_autopromote_allowlist" in s)
    assert "WHERE tenant_id = :tid AND predicate = :pred" in delete_sql
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == actions.CLAIM_AUTOPROMOTE_REVOKED
    assert audit_calls[0]["tid"] == tenant_id
    assert audit_calls[0]["aid"] == actor_id
