"""The receipt and resume surfaces, over HTTP and over MCP, against real Postgres.

Three things are proved here that nothing else proves.

**The surfaces are reachable at all.** A router `wiring/routes.py` never names
and a tool module `api/mcp/server.py` never registers are both unreachable code
that reviews perfectly. Two earlier tasks in this area shipped a correct
implementation behind a missing mount, so the first two tests exist to fail with
a 404 and an unknown tool the moment either mount is dropped.

**Both transports answer the same question the same way.** Not by each being
written carefully -- by adapting one set of services and sharing the one rule
that is genuinely surface-level, `resume_status`. The parity tests below drive
the same operations both ways and compare, so a change that teaches one
transport something the other does not know fails here.

**Resume says which of three answers it gave.** Resumed, empty and ambiguous
all come back with a 200, and only `status` distinguishes them. That is
deliberate: a correctly formed request that found nothing is not an error, and
a caller made to infer the difference from which fields are empty will read
"ambiguous" as "start fresh" and redo work that already exists.
"""

from __future__ import annotations

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

from contextplane.api.mcp.context import _request_app, _request_token, _request_x_tenant_id
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_REF = ("github", "acme/app", "pull_request", "42")
_UNKNOWN_REF = ("github", "acme/app", "pull_request", "does-not-exist")

type _Surface = dict[str, Any]


async def _seed(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    task_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """A task with two checkpoints, a reference both cite, and a receipt.

    Written as SQL rather than through the surfaces because none of it is what
    this suite is testing: the first owner of a task has nobody to be granted
    by, and a receipt is written by resolution, which is a different slice.
    """
    reference_id, receipt_id = uuid.uuid4(), uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_participant_grants "
                    "(tenant_id, task_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:t, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"t": tenant_id, "task": task_id, "actor": actor_id, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO context_external_references "
                    "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                    " classification, external_authority, collision_key) "
                    "VALUES (:rid, :t, :sys, :ns, :kind, :eid, 'internal', 'github', :ckey)"
                ),
                {
                    "rid": reference_id,
                    "t": tenant_id,
                    "sys": _REF[0],
                    "ns": _REF[1],
                    "kind": _REF[2],
                    "eid": _REF[3],
                    "ckey": "|".join(_REF),
                },
            )
            # Two steps, chained. The predecessor is threaded because a
            # checkpoint past the first that names no parent is refused --
            # that constraint is what stops a step being written into the
            # middle of somebody else's history.
            for checkpoint_id, sequence, predecessor, goal, next_action in (
                (first, 1, None, "read the failing test", "reproduce it"),
                (second, 2, first, "reproduce it", "fix the off-by-one"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO task_checkpoints "
                        "(checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, decisions, "
                        " assumptions, evidence, completed_checks, open_questions, next_action, author, "
                        " recorded_at, retention_policy, digest) "
                        "VALUES (:cid, :t, :task, :seq, :pred, :goal, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                        " '[]'::jsonb, :oq, :next, 'agent-a', :at, 'standard', :digest)"
                    ),
                    {
                        "cid": checkpoint_id,
                        "t": tenant_id,
                        "task": task_id,
                        "seq": sequence,
                        "pred": predecessor,
                        "goal": goal,
                        "oq": json.dumps([f"q{sequence}"]),
                        "next": next_action,
                        "at": _NOW + datetime.timedelta(minutes=sequence),
                        "digest": f"digest-{sequence}",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO context_reference_bindings "
                        "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                        "VALUES (:bid, :t, :rid, 'task_checkpoint', :cid, :now)"
                    ),
                    {
                        "bid": uuid.uuid4(),
                        "t": tenant_id,
                        "rid": reference_id,
                        "cid": checkpoint_id,
                        "now": _NOW,
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO task_heads "
                    "(tenant_id, task_id, head_checkpoint_id, head_sequence, summary, updated_at) "
                    "VALUES (:t, :task, :cid, 2, 'reproduce it', :at)"
                ),
                {"t": tenant_id, "task": task_id, "cid": second, "at": _NOW + datetime.timedelta(minutes=2)},
            )
            await session.execute(
                text(
                    "INSERT INTO context_receipts "
                    "(receipt_id, tenant_id, task_id, state, cacheable, resolved_at, requested_by, "
                    " request_digest) "
                    "VALUES (:rid, :t, :task, 'complete', TRUE, :now, 'agent-a', 'sha256:abc')"
                ),
                {"rid": receipt_id, "t": tenant_id, "task": task_id, "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO context_receipt_exclusions (receipt_id, block, item_key, reason) "
                    "VALUES (:r, 'workspace', 'task-9', 'no active grant')"
                ),
                {"r": receipt_id},
            )
            await session.execute(
                text(
                    "INSERT INTO context_reference_bindings "
                    "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                    "VALUES (:bid, :t, :rid, 'context_item', :receipt, :now)"
                ),
                {
                    "bid": uuid.uuid4(),
                    "t": tenant_id,
                    "rid": reference_id,
                    "receipt": receipt_id,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return {"reference_id": reference_id, "receipt_id": receipt_id, "head_checkpoint_id": second}


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[_Surface]:
    slug = f"rc-{uuid.uuid4().hex[:8]}"
    other_slug = f"rc-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        owner = harness.add_persona(slug, roles=["producer", "consumer"])
        stranger = harness.add_persona(other_slug, roles=["producer", "consumer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(owner)
            with patch_validator_for_actor(owner):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                owner_actor = resp.json()["actor_id"]

            # The other tenant has to exist before anyone can authenticate as
            # it, and the cross-tenant tests need that identity to be real.
            harness.configure_fetcher_for(stranger)
            with patch_validator_for_actor(stranger):
                other = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=other_slug))
                assert other.status_code == 200, other.text

            task_id = uuid.uuid4()
            seeded = await _seed(pg_container, tenant_id=tenant_id, actor_id=str(owner_actor), task_id=task_id)

            yield {
                "client": client,
                "harness": harness,
                "owner": owner,
                "stranger": stranger,
                "slug": slug,
                "other_slug": other_slug,
                "tenant_id": tenant_id,
                "owner_actor": str(owner_actor),
                "task_id": task_id,
                **seeded,
            }


def _as(surface: _Surface, persona: TenantPersona) -> Any:
    surface["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


async def _mcp(surface: _Surface, persona: TenantPersona, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Drive one tool the way `handle_sse` drives every tool.

    The ContextVars are the transport: MCP passes no request object into a tool
    body, so a test that sets them is exercising the real path rather than a
    shortcut around it.
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
    surface["harness"].configure_fetcher_for(persona)
    cv_token = _request_token.set("harness.dummy.jwt")
    cv_app = _request_app.set(app)
    cv_tenant = _request_x_tenant_id.set(persona.slug)
    try:
        with patch_validator_for_actor(persona):
            result = await server.call_tool(tool, args)
    finally:
        _request_token.reset(cv_token)
        _request_app.reset(cv_app)
        _request_x_tenant_id.reset(cv_tenant)
    blocks, _ = result if isinstance(result, tuple) else (result, {})
    return json.loads(blocks[0].text)


def _ref_query(reference: tuple[str, str, str, str]) -> dict[str, str]:
    return {
        "source_system": reference[0],
        "source_namespace": reference[1],
        "kind": reference[2],
        "external_id": reference[3],
    }


# --- The surfaces exist at all ------------------------------------------------


@pytest.mark.asyncio
async def test_the_rest_routes_are_mounted(surface: _Surface) -> None:
    """Fails with a 404 the moment `wiring/routes.py` stops naming this router.

    Worth its own test because unmounted routes look exactly like correct code
    from inside the router file, and this area has shipped that mistake twice.
    """
    with _as(surface, surface["owner"]):
        resp = await surface["client"].get(
            "/v1/receipts/by-reference",
            params=_ref_query(_REF),
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_mcp_tools_are_registered(surface: _Surface) -> None:
    """The same check for the other transport. MCP registration is static, so a
    tool module nobody names is as unreachable as an unmounted router."""
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

    assert {"find_receipts_by_reference", "get_receipt_exclusions", "resume_context"} <= names


# --- Reading a receipt --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_receipt_is_found_by_the_work_it_describes(surface: _Surface) -> None:
    """Nobody holds a receipt id. A receipt reachable only by its own id is not
    evidence anyone can find."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].get(
            "/v1/receipts/by-reference",
            params=_ref_query(_REF),
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    ids = [row["receipt_id"] for row in resp.json()["receipts"]]
    assert str(surface["receipt_id"]) in ids


@pytest.mark.asyncio
async def test_another_tenants_receipt_is_not_found_rather_than_refused(surface: _Surface) -> None:
    """404, not 403. A distinguishable refusal would confirm the id exists, and
    the tenant predicate is inside the SELECT precisely so that the row simply
    is not there to refuse."""
    with _as(surface, surface["stranger"]):
        resp = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}",
            headers=bearer_headers(tenant_slug=surface["other_slug"]),
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_receipts_exclusions_are_published(surface: _Surface) -> None:
    """The read that answers "was there more than this".

    A receipt that records what it withheld and never shows it leaves a reader
    unable to tell a thin answer from a filtered one.
    """
    with _as(surface, surface["owner"]):
        resp = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}/exclusions",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    exclusions = resp.json()["exclusions"]
    assert len(exclusions) == 1
    assert exclusions[0] == {"block": "workspace", "item_key": "task-9", "reason": "no active grant"}


@pytest.mark.asyncio
async def test_exclusions_can_be_narrowed_to_one_block(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        headers = bearer_headers(tenant_slug=surface["slug"])
        matching = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}/exclusions",
            params={"block": "workspace"},
            headers=headers,
        )
        other = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}/exclusions",
            params={"block": "canonical"},
            headers=headers,
        )

    assert len(matching.json()["exclusions"]) == 1
    assert other.json()["exclusions"] == []


@pytest.mark.asyncio
async def test_a_receipt_reports_what_it_was_about(surface: _Surface) -> None:
    """The auditor's read: which piece of external work this resolution claimed
    to be for."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}/references",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    references = resp.json()["references"]
    assert [(r["source_system"], r["source_namespace"], r["kind"], r["external_id"]) for r in references] == [_REF]
    assert references[0]["classification"] == "internal", "an auditor needs to know how the citation is held"


# --- Resume -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_returns_the_head_and_the_next_action(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["status"] == "resumed"
    assert body["task_id"] == str(surface["task_id"])
    assert body["head_checkpoint_id"] == str(surface["head_checkpoint_id"])
    assert body["head_sequence"] == 2
    assert body["next_action"] == "fix the off-by-one"


@pytest.mark.asyncio
async def test_an_unknown_reference_resumes_empty_with_a_200(surface: _Surface) -> None:
    """A correctly formed request that found nothing is an answer, not an
    error. A 404 here would make a caller retry a request that was fine."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_UNKNOWN_REF)]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "empty"
    assert resp.json()["task_id"] is None


@pytest.mark.asyncio
async def test_another_tenant_resumes_empty_rather_than_seeing_the_task(surface: _Surface) -> None:
    """The same reference tuple, a different tenant: identical to never having
    existed. Any other answer makes resume a cross-tenant probe."""
    with _as(surface, surface["stranger"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)]},
            headers=bearer_headers(tenant_slug=surface["other_slug"]),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "empty"
    assert resp.json()["task_id"] is None


@pytest.mark.asyncio
async def test_hitting_a_bound_is_reported_rather_than_silent(surface: _Surface) -> None:
    """A resume that quietly returned the first of two checkpoints would read as
    the whole story, and the caller would carry on from a middle it believed was
    the start."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)], "checkpoint_bound": 1},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    body = resp.json()
    assert len(body["checkpoints"]) == 1
    assert "checkpoints" in body["truncated"]
    assert body["checkpoints"][0]["sequence"] == 2, "a bound keeps the recent end, not the old one"


@pytest.mark.asyncio
async def test_a_bound_of_zero_is_refused(surface: _Surface) -> None:
    """Zero returns nothing while looking like a successful resume."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)], "checkpoint_bound": 0},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_resume_with_no_references_is_refused(surface: _Surface) -> None:
    """Resuming from nothing would mean resuming everything the tenant has ever
    done."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": []},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resume_returns_conclusions_and_never_an_exchange(surface: _Surface) -> None:
    """There is no parameter that can ask for a transcript, and the response
    carries no field one could arrive in. Asserted at the surface because this
    is the boundary a caller can actually reach."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)], "include_transcript": True},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    forbidden = {"transcript", "messages", "history", "exchange", "conversation"}
    assert not forbidden & set(body), "resume must not grow a transcript field"
    assert body["status"] == "resumed", "an unknown key is ignored, not honoured"


# --- The two transports agree -------------------------------------------------


@pytest.mark.asyncio
async def test_both_transports_find_the_same_receipts(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        rest = await surface["client"].get(
            "/v1/receipts/by-reference",
            params=_ref_query(_REF),
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(surface, surface["owner"], "find_receipts_by_reference", dict(_ref_query(_REF)))

    assert [r["receipt_id"] for r in rest.json()["receipts"]] == [r["receipt_id"] for r in mcp["receipts"]]
    assert str(surface["receipt_id"]) in [r["receipt_id"] for r in mcp["receipts"]]


@pytest.mark.asyncio
async def test_both_transports_publish_the_same_exclusions(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        rest = await surface["client"].get(
            f"/v1/receipts/{surface['receipt_id']}/exclusions",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(surface, surface["owner"], "get_receipt_exclusions", {"receipt_id": str(surface["receipt_id"])})

    assert rest.json()["exclusions"] == mcp["exclusions"]


@pytest.mark.asyncio
async def test_both_transports_resume_to_the_same_state(surface: _Surface) -> None:
    """The parity that matters most: an agent resuming over MCP and a caller
    resuming over HTTP must be told to carry on from the same place."""
    with _as(surface, surface["owner"]):
        rest = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(surface, surface["owner"], "resume_context", {"references": [list(_REF)]})

    body = rest.json()
    for field in ("status", "task_id", "head_checkpoint_id", "head_sequence", "next_action", "truncated"):
        assert body[field] == mcp[field], f"the transports disagree about {field}"
    assert [c["checkpoint_id"] for c in body["checkpoints"]] == [c["checkpoint_id"] for c in mcp["checkpoints"]]


@pytest.mark.asyncio
async def test_both_transports_report_an_unknown_reference_as_empty(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        rest = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_UNKNOWN_REF)]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(surface, surface["owner"], "resume_context", {"references": [list(_UNKNOWN_REF)]})

    assert rest.json()["status"] == mcp["status"] == "empty"


@pytest.mark.asyncio
async def test_both_transports_refuse_a_reference_that_is_not_a_four_tuple(surface: _Surface) -> None:
    """The REST body declares a four-tuple and pydantic enforces it; the request
    dataclass does not, so the MCP tool has to enforce it itself. Without this
    the two transports would disagree about what a valid reference is."""
    with _as(surface, surface["owner"]):
        rest = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [["github", "acme/app", "pull_request"]]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(
        surface,
        surface["owner"],
        "resume_context",
        {"references": [["github", "acme/app", "pull_request"]]},
    )

    assert rest.status_code == 422
    assert "error" in mcp, "MCP must refuse it too, rather than passing three fields to a four-field query"


@pytest.mark.asyncio
async def test_both_transports_honour_the_same_bound(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        rest = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [list(_REF)], "checkpoint_bound": 1},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    mcp = await _mcp(surface, surface["owner"], "resume_context", {"references": [list(_REF)], "checkpoint_bound": 1})

    assert len(rest.json()["checkpoints"]) == len(mcp["checkpoints"]) == 1
    assert rest.json()["truncated"] == mcp["truncated"] == ["checkpoints"]
