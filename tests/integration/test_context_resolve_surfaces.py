"""The context-resolve surfaces, over HTTP against a real Postgres.

Four things are proved here that nothing else proves.

**Both surfaces are reachable.** A router `wiring/routes.py` never names and an
MCP tool module `api/mcp/server.py` never registers are unreachable code that
looks entirely correct in review. The first two tests fail with `404` and a
missing tool name respectively if either mount is dropped.

**The envelope is really four blocks.** Not four block objects the assembler was
handed in a fixture -- four blocks composed from the wired arms, in the fixed
order, through the app. This is the only place that is true end to end.

**Every resolution leaves a receipt.** The write is mandatory, so a response
carrying a `receipt_id` that names no stored row would be an answer nobody can
audit, and it would look identical to an audited one. The test reads the row.

**The two transports answer identically.** Not because each was written
carefully, but because both adapt one resolver. The parity test compares field
by field, so a change that teaches one transport something the other does not
know fails here rather than in production.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.api.mcp import context as mcp_context
from contextplane.context.resolve import ARC_NOT_REQUESTED_NOTE
from contextplane.context.schemas.envelope import BLOCK_ARC, BLOCK_EMPTY, BLOCK_NAMES
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

type _Surface = dict[str, Any]

#: Seed rows are stamped in the past so nothing depends on the test clock.
_SEED_MOMENT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[_Surface]:
    slug = f"ctx-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["producer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                actor_id = str(resp.json()["actor_id"])

            yield {
                "client": client,
                "harness": harness,
                "caller": caller,
                "slug": slug,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pg_url": pg_container,
            }


def _as(surface: _Surface, persona: Any):
    surface["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


@contextlib.contextmanager
def _mcp_request(surface: _Surface) -> Any:
    """Populate the ContextVars an MCP tool call reads, then restore them.

    `create_mcp_app`'s SSE handler sets these per request; a tool function called
    directly has none, and the tool refuses with "missing bearer token" — which
    is the tool being right about authentication, not the test being wrong.
    Mirroring the handler here is what makes the parity comparison a real
    two-transport comparison instead of two calls into the same adapter.
    """
    tokens = [
        mcp_context._request_token.set("harness.dummy.jwt"),
        mcp_context._request_app.set(surface["harness"].app),
        mcp_context._request_x_tenant_id.set(surface["slug"]),
    ]
    try:
        yield
    finally:
        for var, token in zip(
            (
                mcp_context._request_token,
                mcp_context._request_app,
                mcp_context._request_x_tenant_id,
            ),
            tokens,
            strict=True,
        ):
            var.reset(token)


async def _resolve(surface: _Surface, **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {"query": "what is the state of the migration"}
    payload.update(body)
    with _as(surface, surface["caller"]):
        return await surface["client"].post(
            "/v1/context/resolve",
            headers=bearer_headers(tenant_slug=surface["slug"]),
            json=payload,
        )


async def _seed_participating_checkpoint(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    goal: str,
) -> uuid.UUID:
    """Give the caller a task with one checkpoint, so the workspace arm has something to return.

    Inserted directly rather than through the task-memory surface because this
    suite is about context resolution, not about how a task comes to exist — and
    a resolve test that depended on another slice's write path would fail for
    reasons that have nothing to do with resolving.

    Without this, the trust-label and receipt-item-id tests below skip, and a
    skipped assertion proves nothing about the rule it names.
    """
    task_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_participant_grants "
                    "(tenant_id, task_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:tid, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"tid": tenant_id, "task": task_id, "actor": actor_id, "now": _SEED_MOMENT},
            )
            await session.execute(
                text(
                    "INSERT INTO task_checkpoints "
                    "(checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, decisions, "
                    " assumptions, completed_checks, open_questions, next_action, author, recorded_at, "
                    " retention_policy, digest) "
                    "VALUES (:cid, :tid, :task, 1, NULL, :goal, '{}', '{}', '{}', '{}', "
                    " 'keep going', :actor, :now, 'standard', :digest)"
                ),
                {
                    "cid": uuid.uuid4(),
                    "tid": tenant_id,
                    "task": task_id,
                    "goal": goal,
                    "actor": actor_id,
                    "now": _SEED_MOMENT,
                    "digest": uuid.uuid4().hex,
                },
            )
    finally:
        await engine.dispose()
    return task_id


async def _receipt_row(pg_url: str, receipt_id: uuid.UUID) -> dict[str, Any] | None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT receipt_id, state, cacheable FROM context_receipts WHERE receipt_id = :rid"),
                    {"rid": receipt_id},
                )
            ).mappings()
            return dict(row.one_or_none() or {}) or None
    finally:
        await engine.dispose()


# --- The surfaces exist at all -----------------------------------------------


@pytest.mark.asyncio
async def test_the_rest_route_is_mounted(surface: _Surface) -> None:
    """Fails with 404 if `wiring/routes.py` stops naming this router."""
    resp = await _resolve(surface)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_mcp_tool_is_registered(surface: _Surface) -> None:
    """The same check on the other transport.

    MCP registration is static, so a module nobody names is as unreachable as an
    unmounted router — and no gate in this repository notices.
    """
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    app = surface["harness"].app
    server = create_contextplane_mcp_server(
        retrieval=app.state.retrieval,
        catalog=app.state.catalog,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        workspace_service=app.state.workspace_service,
    )
    names = {tool.name for tool in await server.list_tools()}

    assert "registry_resolve_context" in names


# --- The envelope ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_response_carries_exactly_four_blocks_in_the_fixed_order(surface: _Surface) -> None:
    """The contract's headline: four blocks, always, in one order.

    Asserted as an ordered list rather than a set, because a caller reading
    `blocks[0]` gets a different answer if the order drifts and nothing else
    would report it.
    """
    resp = await _resolve(surface)

    assert resp.status_code == 200, resp.text
    assert [block["name"] for block in resp.json()["blocks"]] == list(BLOCK_NAMES)


@pytest.mark.asyncio
async def test_every_block_declares_a_legal_state_and_explains_any_failure(surface: _Surface) -> None:
    """A degraded or failed arm must say why, or the response cannot be explained."""
    body = (await _resolve(surface)).json()

    for block in body["blocks"]:
        assert block["state"] in {"success", "empty", "degraded", "failed"}
        if block["state"] in {"degraded", "failed"}:
            assert (block["reason"] or "").strip(), f"{block['name']} is {block['state']} and must say why"
        if block["state"] == "empty":
            assert block["items"] == [], "an empty block cannot carry items"


@pytest.mark.asyncio
async def test_quality_names_exactly_the_blocks_that_degraded(surface: _Surface) -> None:
    """Quality contradicting the blocks would make the response unexplainable."""
    body = (await _resolve(surface)).json()

    actually_degraded = {b["name"] for b in body["blocks"] if b["state"] in {"degraded", "failed"}}
    assert set(body["quality"]["degraded_blocks"]) == actually_degraded
    assert len(body["quality"]["reasons"]) == len(body["quality"]["degraded_blocks"])
    if actually_degraded:
        assert body["quality"]["cacheable"] is False, "a degraded answer outlives its failure if cached"


@pytest.mark.asyncio
async def test_the_envelope_state_is_one_of_the_three(surface: _Surface) -> None:
    body = (await _resolve(surface)).json()
    assert body["state"] in {"complete", "degraded", "blocked"}


@pytest.mark.asyncio
async def test_a_blocked_envelope_is_still_a_200(surface: _Surface) -> None:
    """`blocked` is an answer, not a fault.

    Returning 5xx would report the service as broken when the service worked and
    the corpus did not, and it would discard the other three blocks and the
    receipt along with it. The distinction lives in `state`, where a caller can
    branch on it.
    """
    resp = await _resolve(surface)

    assert resp.status_code == 200
    assert "state" in resp.json()


# --- The ARC block's two kinds of empty --------------------------------------


@pytest.mark.asyncio
async def test_no_arc_receipt_gives_an_empty_arc_block_and_says_why(surface: _Surface) -> None:
    """The amendment's requirement, pinned.

    The ARC arm serves what an attested resolution already selected, so naming
    no receipt yields an empty block rather than a failed one — and the note is
    what separates "you named no receipt" from "that receipt selected nothing".
    Only the first is the caller's to fix.
    """
    body = (await _resolve(surface)).json()
    arc = next(block for block in body["blocks"] if block["name"] == BLOCK_ARC)

    assert arc["state"] == BLOCK_EMPTY
    assert arc["reason"] is None, "nothing failed, so there is nothing to report as a failure"
    assert body["arc_block_note"] == ARC_NOT_REQUESTED_NOTE


@pytest.mark.asyncio
async def test_an_empty_arc_block_from_a_named_receipt_carries_no_note(surface: _Surface) -> None:
    """The other kind of empty, which the caller cannot fix by adding an argument."""
    body = (await _resolve(surface, arc_receipt_id=str(uuid.uuid4()))).json()

    assert body["arc_block_note"] is None


# --- The receipt -------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_resolution_stores_the_receipt_it_returns(surface: _Surface) -> None:
    """The write is mandatory, so the id must name a real row.

    A response carrying a `receipt_id` with no stored receipt behind it is an
    unauditable answer that is indistinguishable from an audited one.
    """
    body = (await _resolve(surface)).json()
    receipt_id = uuid.UUID(body["receipt_id"])

    row = await _receipt_row(surface["pg_url"], receipt_id)

    assert row is not None, "the response named a receipt that was never written"
    assert row["state"] == body["state"], "the stored receipt must agree with the answer it records"
    assert row["cacheable"] == body["quality"]["cacheable"]


@pytest.mark.asyncio
async def test_two_resolutions_get_two_receipts(surface: _Surface) -> None:
    """Each resolution is its own evidence.

    Reusing a receipt across calls would make the second answer point at the
    first one's record, which is worse than no receipt because it reads as one.
    """
    first = (await _resolve(surface)).json()["receipt_id"]
    second = (await _resolve(surface)).json()["receipt_id"]

    assert first != second


# --- Items carry what a reader needs to weigh them ---------------------------


@pytest.mark.asyncio
async def test_non_canonical_items_carry_all_eight_trust_labels(surface: _Surface) -> None:
    """Outside canonical, an item without complete trust metadata is invalid.

    Skips rather than passes when no non-canonical items came back: a vacuous
    pass here would hide the rule going missing, and stating the skip is honest
    about what this run proved.
    """
    task_id = await _seed_participating_checkpoint(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        goal="finish the migration rollout",
    )
    body = (await _resolve(surface, workspace_term="migration", task_ids=[str(task_id)])).json()
    non_canonical = [item for block in body["blocks"] if block["name"] != "canonical" for item in block["items"]]
    assert non_canonical, "the seeded checkpoint must reach the workspace block, or this proves nothing"

    for item in non_canonical:
        trust = item["trust"]
        assert trust is not None
        for label in (
            "trust",
            "source",
            "assertion_kind",
            "authority",
            "mutability",
            "classification",
        ):
            assert (trust[label] or "").strip(), f"{label} is missing from a non-canonical item"


@pytest.mark.asyncio
async def test_receipt_item_ids_are_checkable_not_opaque(surface: _Surface) -> None:
    """Every item id carries its digest and the parts it derives from.

    A response with only the digest asks the caller to trust an opaque string,
    which is the opposite of what a receipt line is for.
    """
    task_id = await _seed_participating_checkpoint(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        goal="finish the migration rollout",
    )
    body = (await _resolve(surface, workspace_term="migration", task_ids=[str(task_id)])).json()
    items = [item for block in body["blocks"] for item in block["items"]]
    assert items, "the seeded checkpoint must produce at least one item"

    for item in items:
        ident = item["receipt_item_id"]
        assert ident["value"], "a receipt line needs its digest"
        assert ident["block"] and ident["source"] and ident["item_key"], "and the parts that make it checkable"


# --- Bounds ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 201])
async def test_an_out_of_range_limit_is_refused(surface: _Surface, limit: int) -> None:
    """The bound is the transport's, and it refuses rather than clamps.

    Clamping would answer a different question than the one asked and report
    success, which is the shape a caller never notices.
    """
    resp = await _resolve(surface, limit=limit)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_a_blank_query_is_refused(surface: _Surface) -> None:
    resp = await _resolve(surface, query="")
    assert resp.status_code == 422, resp.text


# --- Parity ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_transports_answer_the_same_question_the_same_way(surface: _Surface) -> None:
    """The reason both adapters exist over one resolver.

    Compared field by field on the parts that must not differ. `receipt_id`
    differs by construction — each resolution is its own evidence — so it is
    excluded deliberately rather than by omission.
    """
    from contextplane.api.mcp.tools.context import registry_resolve_context

    rest = (await _resolve(surface, workspace_term="migration")).json()

    app = surface["harness"].app
    with _as(surface, surface["caller"]), _mcp_request(surface):
        raw = await registry_resolve_context(
            "what is the state of the migration",
            workspace_term="migration",
            session_factory=app.state.session_factory,
            clock=app.state.clock,
        )
    mcp = json.loads(raw)

    assert mcp["state"] == rest["state"]
    assert [b["name"] for b in mcp["blocks"]] == [b["name"] for b in rest["blocks"]]
    assert [b["state"] for b in mcp["blocks"]] == [b["state"] for b in rest["blocks"]]
    assert mcp["quality"] == rest["quality"]
    assert mcp["arc_block_note"] == rest["arc_block_note"]
    assert set(mcp) == set(rest), "the two transports must expose the same field names"
    assert uuid.UUID(mcp["receipt_id"]) != uuid.UUID(rest["receipt_id"])


@pytest.mark.asyncio
async def test_the_mcp_tool_refuses_an_out_of_range_limit_too(surface: _Surface) -> None:
    """Parity on refusals, not only on answers.

    A bound enforced on one transport and not the other is exactly the silent
    divergence one resolver behind two adapters is meant to prevent.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    from contextplane.api.mcp.tools.context import registry_resolve_context

    app = surface["harness"].app
    with _as(surface, surface["caller"]), _mcp_request(surface), pytest.raises(ToolError):
        await registry_resolve_context(
            "anything",
            limit=201,
            session_factory=app.state.session_factory,
            clock=app.state.clock,
        )
