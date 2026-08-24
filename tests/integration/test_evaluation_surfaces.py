"""The evaluation-run REST surface, over HTTP against a real Postgres.

E22-T15. Three things are proved here that the service tests cannot.

**The router is mounted.** A router `wiring/routes.py` never names is unreachable
code that looks entirely correct in review, and every one of these routes would
404 without saying anything about why.

**A run goes through the real resolver.** Not a double — the wired one, over the
wired arms, writing real receipts. An evaluation that ran against a copy of the
resolver, or against a path with checks relaxed for evaluation, would measure
something the product does not serve.

**A receipt a run names is a row that exists.** The value of a run is that
somebody can open what it served; a `receipt_id` naming nothing would look
identical to one naming something until they tried.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    slug = f"eval-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["producer"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
            yield {
                "caller": caller,
                "client": client,
                "harness": harness,
                "pg": pg_container,
                "slug": slug,
                "tenant_id": uuid.UUID(whoami.json()["tenant_id"]),
            }


def _as(surface: dict[str, Any]) -> Any:
    surface["harness"].configure_fetcher_for(surface["caller"])
    return patch_validator_for_actor(surface["caller"])


async def _post(surface: dict[str, Any], path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    with _as(surface):
        return await surface["client"].post(path, headers=bearer_headers(tenant_slug=surface["slug"]), json=body or {})


async def _get(surface: dict[str, Any], path: str) -> httpx.Response:
    with _as(surface):
        return await surface["client"].get(path, headers=bearer_headers(tenant_slug=surface["slug"]))


async def _set_with(surface: dict[str, Any], *queries: str) -> str:
    created = await _post(surface, "/v1/evaluation/prompt-sets", {"name": f"set-{uuid.uuid4().hex[:8]}"})
    assert created.status_code == 200, created.text
    set_id = created.json()["set_id"]
    for query in queries:
        added = await _post(
            surface,
            f"/v1/evaluation/prompt-sets/{set_id}/prompts",
            {"request": {"query": query}},
        )
        assert added.status_code == 200, added.text
    return str(set_id)


@pytest.mark.asyncio
async def test_a_set_is_created_listed_and_counted(surface: dict[str, Any]) -> None:
    set_id = await _set_with(surface, "the state of the migration", "who owns checkout")

    listed = await _get(surface, "/v1/evaluation/prompt-sets")

    assert listed.status_code == 200, listed.text
    entry = next(item for item in listed.json()["items"] if item["set_id"] == set_id)
    assert entry["prompt_count"] == 2


@pytest.mark.asyncio
async def test_a_prompt_the_resolver_could_not_take_is_refused_on_the_wire(
    surface: dict[str, Any],
) -> None:
    """Refused when it is saved, not on every run afterwards."""
    set_id = await _set_with(surface)

    response = await _post(
        surface,
        f"/v1/evaluation/prompt-sets/{set_id}/prompts",
        {"request": {"query": "x", "limit": 9999}},
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_run_resolves_through_the_wired_resolver_and_its_receipts_exist(
    surface: dict[str, Any],
) -> None:
    """The whole surface end to end: two prompts, two resolutions, two receipts a
    reader can open."""
    set_id = await _set_with(surface, "the state of the migration", "who owns checkout")

    run = await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")

    assert run.status_code == 200, run.text
    body = run.json()
    assert body["prompt_count"] == 2
    assert len(body["items"]) == 2
    assert body["finished_at"] is not None
    assert body["resolver_fingerprint"].startswith("sha256:")

    receipt_ids = [item["receipt_id"] for item in body["items"]]
    assert all(receipt_ids), f"a run item carried no receipt: {body['items']}"

    engine = create_async_engine(surface["pg"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            found = await session.execute(
                text("SELECT count(*) FROM context_receipts WHERE receipt_id = ANY(:ids)"),
                {"ids": [uuid.UUID(value) for value in receipt_ids]},
            )
            assert found.scalar_one() == 2, "a run named a receipt that is not a stored row"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_items_come_back_in_the_order_the_set_holds_them(surface: dict[str, Any]) -> None:
    """Two runs of one set are read side by side. Ordering by anything else —
    completion, id — would make that a reconciliation rather than a comparison."""
    set_id = await _set_with(surface, "first", "second", "third")
    run = await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")

    reread = await _get(surface, f"/v1/evaluation/runs/{run.json()['run_id']}")

    assert [item["position"] for item in reread.json()["items"]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_two_runs_of_one_set_carry_the_same_fingerprint(surface: dict[str, Any]) -> None:
    """The deployment did not change between them, so a difference in results is
    about retrieval rather than about configuration."""
    set_id = await _set_with(surface, "first")

    first = await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")
    second = await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")

    assert first.json()["resolver_fingerprint"] == second.json()["resolver_fingerprint"]


@pytest.mark.asyncio
async def test_the_runs_list_carries_headers_without_items(surface: dict[str, Any]) -> None:
    set_id = await _set_with(surface, "first")
    await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")

    listed = await _get(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["items"] == []


@pytest.mark.asyncio
async def test_a_verdict_is_recorded_against_an_item_and_read_back_with_the_run(
    surface: dict[str, Any],
) -> None:
    set_id = await _set_with(surface, "first")
    run = (await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")).json()
    item_id = run["items"][0]["item_id"]

    recorded = await _post(
        surface,
        f"/v1/evaluation/runs/items/{item_id}/verdict",
        {"note": "the ARC block was empty", "verdict": "wrong"},
    )

    assert recorded.status_code == 200, recorded.text
    reread = await _get(surface, f"/v1/evaluation/runs/{run['run_id']}")
    (verdict,) = reread.json()["items"][0]["verdicts"]
    assert verdict["verdict"] == "wrong"
    assert verdict["note"] == "the ARC block was empty"


@pytest.mark.asyncio
async def test_an_adverse_verdict_with_no_reason_is_refused_on_the_wire(
    surface: dict[str, Any],
) -> None:
    set_id = await _set_with(surface, "first")
    run = (await _post(surface, f"/v1/evaluation/prompt-sets/{set_id}/runs")).json()

    response = await _post(
        surface,
        f"/v1/evaluation/runs/items/{run['items'][0]['item_id']}/verdict",
        {"verdict": "wrong"},
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_run_of_another_tenants_set_is_not_found_rather_than_empty(
    surface: dict[str, Any],
) -> None:
    """Not found rather than an empty run: an empty run would say the set exists
    and has no prompts, which is a statement about somebody else's tenant."""
    response = await _post(surface, f"/v1/evaluation/prompt-sets/{uuid.uuid4()}/runs")

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_a_missing_run_is_a_404(surface: dict[str, Any]) -> None:
    response = await _get(surface, f"/v1/evaluation/runs/{uuid.uuid4()}")

    assert response.status_code == 404, response.text
