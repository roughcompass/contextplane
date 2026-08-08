"""Unit tests for `SourceGovernanceService` (contextplane.service.memory.source_governance).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session,
mirroring `tests/unit/test_promotion.py` / `tests/unit/test_capability_requests.py`'s
pattern -- no Postgres required.

Coverage:
- `declare`: authority-tier membership validation, positivity checks on
  `ingest_ceiling`/`window_seconds`, the ownership guard (unknown source vs.
  wrong tenant), the `may_provision_entities` default and its explicit
  opt-in, and the write-then-reread round trip through `policy_for`.
- `policy_for` / `policies_for_tenant`: the missing-row case, and that every
  numeric/boolean field is actually cast (not just passed through) from
  whatever the row happens to hold.
- `admit`: the undeclared-source refusal, the breaker-open refusal and its
  exact boundary (open-until in the future blocks, arriving exactly at
  open-until does not), the fixed-window reset-vs-accumulate branches, and
  the ceiling boundary itself -- admitting exactly up to the ceiling
  succeeds, one claim over it is refused, trips the breaker, and is audited.
- `reset_breaker`: the successful clear and the single-message refusal that
  does not distinguish "no such source" from "not your source".

**The admission gate is deliberately content-blind.** `admit()` takes a
`source_id` and a `count` -- nothing about what the batch asserts. It bounds
how much a registered source may write, never what it writes; a source
within its ceiling is admitted regardless of what its claims say, and a
source over ceiling is refused regardless of how correct they are. The
signature-shape test below pins that this is a property of the API surface,
not an accident of what today's tests happen to pass -- deciding what a
claim's content is worth, or whether it should be believed, is a different
module's job.
"""

from __future__ import annotations

import datetime
import inspect
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.memory.source_governance import (
    BREAKER_COOLDOWN_SECONDS,
    Admission,
    SourceGovernanceService,
    SourcePolicy,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------------------
# Shared helpers -- same shape as tests/unit/test_capability_requests.py
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


def _bare_first(value: Any) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=value)
    return result


class _Dynamic:
    """Wraps a response callback that needs the query's `params`."""

    def __init__(self, fn: Any) -> None:
        self.fn = fn


class _SqlRouter:
    """Dispatches `session.execute` calls to a canned response by matching a
    substring against the flattened SQL text, first match in registration
    order wins. Records every `(sql, params)` pair issued."""

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
                return response.fn(params) if isinstance(response, _Dynamic) else response
        raise AssertionError(f"unexpected SQL in test session: {sql}")


def _session_factory(execute: Any) -> MagicMock:
    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory


def _ctx(tenant_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None) -> Any:
    ctx = MagicMock()
    ctx.tenant_id = tenant_id if tenant_id is not None else uuid.uuid4()
    ctx.actor_id = actor_id if actor_id is not None else uuid.uuid4()
    return ctx


def _service(router: _SqlRouter) -> SourceGovernanceService:
    return SourceGovernanceService(_session_factory(router), clock=FakeClock(_NOW))


def _governance_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "authority_tier": "owner_extraction",
        "ingest_ceiling": 1000,
        "window_seconds": 3600,
        "breaker_open_until": None,
        "breach_count": 0,
        "may_provision_entities": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# declare()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declare_rejects_an_unknown_authority_tier_without_touching_the_database() -> None:
    router = _SqlRouter()
    service = _service(router)

    with pytest.raises(ValidationError, match="authority_tier"):
        await service.declare(_ctx(), source_id=uuid.uuid4(), authority_tier="not_a_real_tier")
    assert router.calls == []


@pytest.mark.asyncio
async def test_declare_rejects_a_non_positive_ceiling_without_touching_the_database() -> None:
    router = _SqlRouter()
    service = _service(router)

    with pytest.raises(ValidationError, match="ingest_ceiling"):
        await service.declare(_ctx(), source_id=uuid.uuid4(), authority_tier="owner_extraction", ingest_ceiling=0)
    assert router.calls == []


@pytest.mark.asyncio
async def test_declare_rejects_a_non_positive_window_without_touching_the_database() -> None:
    router = _SqlRouter()
    service = _service(router)

    with pytest.raises(ValidationError, match="window_seconds"):
        await service.declare(_ctx(), source_id=uuid.uuid4(), authority_tier="owner_extraction", window_seconds=-1)
    assert router.calls == []


@pytest.mark.asyncio
async def test_declare_raises_not_found_when_the_source_is_unregistered() -> None:
    router = _SqlRouter().route("FROM sync_sources", _scalar_one_or_none(None))
    service = _service(router)

    with pytest.raises(NotFoundError):
        await service.declare(_ctx(), source_id=uuid.uuid4(), authority_tier="owner_extraction")


@pytest.mark.asyncio
async def test_declare_refuses_a_tenant_that_does_not_own_the_source() -> None:
    owner = uuid.uuid4()
    caller = _ctx(tenant_id=uuid.uuid4())
    router = _SqlRouter().route("FROM sync_sources", _scalar_one_or_none(owner))
    service = _service(router)

    with pytest.raises(PermissionError):
        await service.declare(caller, source_id=uuid.uuid4(), authority_tier="owner_extraction")


@pytest.mark.asyncio
async def test_declare_defaults_may_provision_entities_false_and_records_it_in_the_audit_payload() -> None:
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    caller = _ctx(tenant_id=tenant_id)
    audit_calls: list[dict[str, Any] | None] = []
    row = _governance_row(source_id=source_id, tenant_id=tenant_id, may_provision_entities=False)
    router = (
        _SqlRouter()
        .route("FROM sync_sources", _scalar_one_or_none(tenant_id))
        .route("INSERT INTO memory_source_governance", MagicMock())
        .route(
            "INSERT INTO audit_log",
            _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]),
        )
        .route("FROM memory_source_governance WHERE source_id", _mapping_first(row))
    )
    service = _service(router)

    policy = await service.declare(caller, source_id=source_id, authority_tier="owner_extraction")

    assert policy.may_provision_entities is False
    payload = json.loads(audit_calls[0]["after"])
    assert payload["may_provision_entities"] is False
    assert audit_calls[0]["action"] == actions.SOURCE_AUTHORITY_DECLARED


@pytest.mark.asyncio
async def test_declare_with_explicit_provisioning_opt_in_persists_and_returns_it() -> None:
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    caller = _ctx(tenant_id=tenant_id)
    insert_calls: list[dict[str, Any] | None] = []
    row = _governance_row(source_id=source_id, tenant_id=tenant_id, may_provision_entities=True)
    router = (
        _SqlRouter()
        .route("FROM sync_sources", _scalar_one_or_none(tenant_id))
        .route(
            "INSERT INTO memory_source_governance",
            _Dynamic(lambda params: (insert_calls.append(params), MagicMock())[1]),
        )
        .route("INSERT INTO audit_log", MagicMock())
        .route("FROM memory_source_governance WHERE source_id", _mapping_first(row))
    )
    service = _service(router)

    policy = await service.declare(
        caller,
        source_id=source_id,
        authority_tier="owner_extraction",
        may_provision_entities=True,
    )

    assert policy.may_provision_entities is True
    assert insert_calls[0]["provision"] is True


# ---------------------------------------------------------------------------
# policy_for() / policies_for_tenant()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_for_returns_none_when_the_source_has_never_declared() -> None:
    router = _SqlRouter().route("FROM memory_source_governance WHERE source_id", _mapping_first(None))
    service = _service(router)

    assert await service.policy_for(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_policy_for_casts_every_numeric_and_boolean_field_rather_than_passing_the_row_through() -> None:
    # Deliberately wrong-typed row values (str/int) so the assertion actually
    # proves the int()/bool() casts run -- if `policy_for` just forwarded the
    # row, this would come back with a str ceiling and an int flag instead of
    # a SourcePolicy.
    row = _governance_row(ingest_ceiling="500", window_seconds="1800", breach_count="2", may_provision_entities=1)
    router = _SqlRouter().route("FROM memory_source_governance WHERE source_id", _mapping_first(row))
    service = _service(router)

    policy = await service.policy_for(row["source_id"])

    assert policy == SourcePolicy(
        source_id=row["source_id"],
        tenant_id=row["tenant_id"],
        authority_tier=row["authority_tier"],
        ingest_ceiling=500,
        window_seconds=1800,
        breaker_open_until=None,
        breach_count=2,
        may_provision_entities=True,
    )
    assert isinstance(policy.ingest_ceiling, int)
    assert isinstance(policy.may_provision_entities, bool)


@pytest.mark.asyncio
async def test_policies_for_tenant_returns_every_declared_source_ordered_by_source_id() -> None:
    tenant_id = uuid.uuid4()
    rows = [
        _governance_row(tenant_id=tenant_id, source_id=uuid.uuid4()),
        _governance_row(tenant_id=tenant_id, source_id=uuid.uuid4(), may_provision_entities=True),
    ]
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    router = _SqlRouter().route("FROM memory_source_governance WHERE tenant_id", result)
    service = _service(router)

    policies = await service.policies_for_tenant(tenant_id)

    assert [p.source_id for p in policies] == [r["source_id"] for r in rows]
    assert policies[1].may_provision_entities is True


# ---------------------------------------------------------------------------
# admit() -- the content-blind ceiling + breaker gate
# ---------------------------------------------------------------------------


def test_admit_signature_carries_no_content_parameter() -> None:
    """Pins the content-blind admission posture at the API-surface level: the
    only inputs are `source_id` and `count`. A future change that threaded a
    claim/candidate payload through here to make the ceiling content-aware
    would be a deliberate contract change, not an accidental one -- this
    fails the moment that happens."""
    params = list(inspect.signature(SourceGovernanceService.admit).parameters)
    assert params == ["self", "source_id", "count"]


@pytest.mark.asyncio
async def test_admit_refuses_a_source_that_never_declared() -> None:
    router = _SqlRouter().route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(None))
    service = _service(router)

    result = await service.admit(uuid.uuid4())

    assert result == Admission(
        permitted=False, reason="the source has not declared an authority tier and may not write"
    )


@pytest.mark.asyncio
async def test_admit_refuses_while_the_breaker_is_open() -> None:
    row = _governance_row(breaker_open_until=_NOW + datetime.timedelta(seconds=1))
    router = _SqlRouter().route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
    service = _service(router)

    result = await service.admit(row["source_id"])

    assert result.permitted is False
    assert result.reason is not None and "circuit open until" in result.reason
    # No UPDATE was ever issued -- the breaker check short-circuits before any write.
    assert not any("UPDATE memory_source_governance" in sql for sql, _ in router.calls)


@pytest.mark.asyncio
async def test_admit_proceeds_once_the_breaker_cooldown_has_exactly_elapsed() -> None:
    # breaker_open_until == now: `now < breaker_open_until` is false, so the
    # breaker no longer blocks -- pins the boundary rather than "well after".
    row = _governance_row(
        breaker_open_until=_NOW,
        window_started_at=_NOW - datetime.timedelta(seconds=10),
        window_count=0,
    )
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route("UPDATE memory_source_governance", MagicMock())
    )
    service = _service(router)

    result = await service.admit(row["source_id"], count=1)

    assert result.permitted is True


@pytest.mark.asyncio
async def test_admit_accumulates_within_an_unexpired_window() -> None:
    row = _governance_row(
        ingest_ceiling=100,
        window_seconds=3600,
        window_started_at=_NOW - datetime.timedelta(seconds=10),
        window_count=40,
    )
    update_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route(
            "UPDATE memory_source_governance",
            _Dynamic(lambda params: (update_calls.append(params), MagicMock())[1]),
        )
    )
    service = _service(router)

    result = await service.admit(row["source_id"], count=10)

    assert result == Admission(permitted=True, remaining=50)
    assert update_calls[0]["expired"] is False
    assert update_calls[0]["count"] == 10


@pytest.mark.asyncio
async def test_admit_resets_the_window_count_instead_of_accumulating_once_expired() -> None:
    row = _governance_row(
        ingest_ceiling=100,
        window_seconds=60,
        window_started_at=_NOW - datetime.timedelta(seconds=120),
        window_count=90,  # would breach if accumulated, but the window is expired
    )
    update_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route(
            "UPDATE memory_source_governance",
            _Dynamic(lambda params: (update_calls.append(params), MagicMock())[1]),
        )
    )
    service = _service(router)

    result = await service.admit(row["source_id"], count=5)

    assert result == Admission(permitted=True, remaining=95)
    assert update_calls[0]["expired"] is True


@pytest.mark.asyncio
async def test_admit_permits_a_batch_landing_exactly_on_the_ceiling() -> None:
    row = _governance_row(
        ingest_ceiling=10,
        window_seconds=3600,
        window_started_at=_NOW,
        window_count=0,
    )
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route("UPDATE memory_source_governance", MagicMock())
    )
    service = _service(router)

    result = await service.admit(row["source_id"], count=10)

    assert result == Admission(permitted=True, remaining=0)


@pytest.mark.asyncio
async def test_admit_refuses_and_trips_the_breaker_one_claim_over_the_ceiling() -> None:
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    row = _governance_row(
        source_id=source_id,
        tenant_id=tenant_id,
        ingest_ceiling=10,
        window_seconds=3600,
        window_started_at=_NOW,
        window_count=0,
    )
    breach_updates: list[dict[str, Any] | None] = []
    audit_calls: list[dict[str, Any] | None] = []
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route(
            "UPDATE memory_source_governance",
            _Dynamic(lambda params: (breach_updates.append(params), MagicMock())[1]),
        )
        .route(
            "INSERT INTO audit_log",
            _Dynamic(lambda params: (audit_calls.append(params), MagicMock())[1]),
        )
    )
    service = _service(router)
    before = _sample("contextplane_source_ingest_breach_total", source_id=str(source_id))

    result = await service.admit(source_id, count=11)

    assert result.permitted is False
    assert result.reason == "ingest ceiling of 10 per 3600s reached"
    assert breach_updates[0]["until"] == _NOW + datetime.timedelta(seconds=BREAKER_COOLDOWN_SECONDS)
    assert audit_calls[0]["action"] == actions.SOURCE_BREAKER_OPENED
    after = _sample("contextplane_source_ingest_breach_total", source_id=str(source_id))
    assert after == before + 1


@pytest.mark.asyncio
async def test_admit_increments_the_admitted_counter_by_the_admitted_count() -> None:
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    row = _governance_row(
        source_id=source_id,
        tenant_id=tenant_id,
        ingest_ceiling=100,
        window_started_at=_NOW,
        window_count=0,
    )
    router = (
        _SqlRouter()
        .route("FROM memory_source_governance WHERE source_id = :sid FOR UPDATE", _mapping_first(row))
        .route("UPDATE memory_source_governance", MagicMock())
    )
    service = _service(router)
    before = _sample("contextplane_source_ingest_admitted_total", source_id=str(source_id))

    await service.admit(source_id, count=7)

    after = _sample("contextplane_source_ingest_admitted_total", source_id=str(source_id))
    assert after == before + 7


# ---------------------------------------------------------------------------
# reset_breaker()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_breaker_clears_the_open_until_and_zeroes_the_window() -> None:
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    caller = _ctx(tenant_id=tenant_id)
    update_calls: list[dict[str, Any] | None] = []
    router = _SqlRouter().route(
        "UPDATE memory_source_governance",
        _Dynamic(lambda params: (update_calls.append(params), _bare_first((source_id,)))[1]),
    )
    service = _service(router)

    await service.reset_breaker(caller, source_id)

    assert update_calls[0]["sid"] == source_id
    assert update_calls[0]["tid"] == tenant_id


@pytest.mark.asyncio
async def test_reset_breaker_raises_permission_error_whether_missing_or_not_owned() -> None:
    """A single message covers both "no such source" and "not your source" --
    the tenant clause is folded into the WHERE, so an empty RETURNING is the
    one signal for either case, deliberately not split back into two."""
    router = _SqlRouter().route("UPDATE memory_source_governance", _bare_first(None))
    service = _service(router)

    with pytest.raises(PermissionError, match="no such source in this tenant"):
        await service.reset_breaker(_ctx(), uuid.uuid4())
