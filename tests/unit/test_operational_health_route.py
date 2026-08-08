"""The operational-health route: who may read it, and what the payload promises.

The gate is the interesting half. This surface is service-global — it reports
the shared deployment's queue depths and parse-failure counts — while the only
identity this API has is tenant-scoped. Admitting the wrong roles would turn an
operator convenience into a cross-tenant information leak, and admitting none
would leave the console unreachable by anyone, since the people who actually run
the service hold no REST identity at all.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from contextplane.api.auth.context import get_tenant_context
from contextplane.api.routers import admin_operational_health
from contextplane.types import TenantContext

_PATH = "/v1/admin/operational-health"


def _app(roles: set[str], *, counts: list[int] | None = None) -> FastAPI:
    from unittest.mock import AsyncMock, MagicMock

    app = FastAPI()
    app.include_router(admin_operational_health.router)

    # One value per `_QUEUE_COUNTS` entry (currently five); the call after those
    # is the oldest-open-proposal age query, answered as "no proposal open"
    # (`scalar_one_or_none` -> None) so it renders as a real zero rather than
    # exhausting this list and reading as unreadable by accident.
    values = list(counts or [4, 0, 9, 2, 3])
    session = AsyncMock()
    calls = 0

    async def execute(*_a: object, **_kw: object) -> MagicMock:
        nonlocal calls
        calls += 1
        result = MagicMock()
        if calls <= len(values):
            result.scalar_one = MagicMock(return_value=values[calls - 1])
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = execute
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.session_factory = factory

    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=frozenset(roles))
    app.dependency_overrides[get_tenant_context] = lambda: ctx
    return app


async def _get(roles: set[str], **kwargs: object):
    app = _app(roles, **kwargs)  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(_PATH)


@pytest.mark.asyncio
async def test_an_admin_may_read_it() -> None:
    response = await _get({"admin"})
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["producer", "consumer", "auditor"])
async def test_every_other_role_is_refused(role: str) -> None:
    """Not `ops:view`'s audience.

    The existing health and readiness endpoints are open to all four roles
    because they are unauthenticated anyway. This one is not: it reports the
    shared deployment's internals, so it is gated on the same role the rest of
    the admin surface uses.
    """
    response = await _get({role})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_every_reading_in_the_payload_carries_scope_and_kind() -> None:
    # The response type forbids extra fields and requires these two, so this is
    # the wire-level proof that the constraint survives serialisation.
    body = (await _get({"admin"})).json()
    readings = [*body["queues"], *body["data_quality"]]
    assert readings
    for reading in readings:
        assert reading["scope"] in {"cluster", "process"}
        assert reading["kind"] in {"gauge", "counter"}


@pytest.mark.asyncio
async def test_queue_depths_and_process_counters_are_distinguishable_on_the_wire() -> None:
    """The distinction the whole endpoint exists to preserve.

    Rendered side by side these are the same shape. Only `scope` says that one
    is true for the deployment and the other is one replica's tally.
    """
    body = (await _get({"admin"})).json()
    assert {r["scope"] for r in body["queues"]} == {"cluster"}
    assert {r["scope"] for r in body["data_quality"]} == {"process"}
    assert all(r["instance"] is None for r in body["queues"])
    assert all(r["instance"] for r in body["data_quality"])


@pytest.mark.asyncio
async def test_the_payload_names_no_tenant_or_actor() -> None:
    # What makes the tenant-admin gate defensible. If this ever fails, the gate
    # is wrong rather than the test.
    body = (await _get({"admin"})).json()
    blob = str(body).lower()
    for word in ("tenant_id", "actor_id", "entity_id", "email"):
        assert word not in blob
