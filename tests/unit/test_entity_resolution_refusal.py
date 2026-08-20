"""An ambiguous handle is refused with the types to choose between.

`AmbiguousIdentity` has always carried its candidates -- "so the caller can
requalify without a second query", as its own docstring puts it -- and the HTTP
handler dropped them on the way out, sending a code and a sentence. A client is
required to branch on `errors[].code` and never on `message`, so a refusal
carrying only a code left it with nothing to offer an operator but "that was
ambiguous", and a second round trip to find out between what.

**Tested here rather than against a database, because the condition cannot be
reached through the write path today.** `0001_baseline_schema` created
`uq_entities_tenant_name` on `(tenant_id, lower(name))`, and
`0051_handles_and_provenance` deliberately left it in place: "the old rule keeps
protecting the old read path until the new one is proven, and removing it is a
separate decision taken later against evidence". So two entities of different
types cannot share a bare name in one tenant yet, and an integration test would
be blocked by the constraint rather than reaching the refusal.

That does not make the branch dead. It is reachable now through handles written
outside that constraint, and it becomes reachable through the ordinary write
path the moment the expand contracts. Driving the handler directly tests the
part that was wrong -- what the refusal carries -- without asserting the
condition is producible by a route that currently forbids it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.entities.identity import AmbiguousIdentity
from contextplane.types import TenantContext

_TENANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ACTOR_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=frozenset({"producer"}))


def _client(side_effect: Exception) -> TestClient:
    from contextplane.api.routers.entities import router

    app = FastAPI()
    app.include_router(router)

    catalog = MagicMock()
    catalog.resolve_entity_handle = AsyncMock(side_effect=side_effect)
    app.state.services = MagicMock(catalog=catalog)

    async def _fake_ctx() -> TenantContext:
        return _ctx()

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return TestClient(app, raise_server_exceptions=False)


def _refusal(payload: dict[str, object]) -> dict[str, object]:
    """The error item, whichever envelope this app happens to be using."""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        assert isinstance(first, dict)
        return first
    detail = payload.get("detail")
    assert isinstance(detail, dict), payload
    return detail


def test_the_refusal_names_the_types_to_choose_between() -> None:
    client = _client(AmbiguousIdentity("orders", ["service", "capability"]))

    response = client.get("/v1/entities:resolve", params={"handle": "orders"})

    assert response.status_code == 409, response.text
    refusal = _refusal(response.json())
    assert refusal["code"] == "identity_ambiguous"
    assert refusal["entity_types"] == ["capability", "service"]


def test_the_candidates_are_sorted_so_two_reads_offer_the_same_order() -> None:
    """`AmbiguousIdentity` sorts them; a UI offering choices must not reorder per call."""
    client = _client(AmbiguousIdentity("orders", ["zeta", "alpha", "mu"]))

    refusal = _refusal(client.get("/v1/entities:resolve", params={"handle": "orders"}).json())

    assert refusal["entity_types"] == ["alpha", "mu", "zeta"]


@pytest.mark.parametrize("count", [2, 3, 5])
def test_every_candidate_is_reported_rather_than_the_first_few(count: int) -> None:
    types = [f"type-{index}" for index in range(count)]
    client = _client(AmbiguousIdentity("orders", types))

    refusal = _refusal(client.get("/v1/entities:resolve", params={"handle": "orders"}).json())

    assert refusal["entity_types"] == sorted(types)
