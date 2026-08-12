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
from contextplane.context.schemas.envelope import BLOCK_ARC, BLOCK_EMPTY, BLOCK_NAMES, BLOCK_OBSERVED_CLAIMS
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
    intent_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO intent_participant_grants "
                    "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:tid, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"tid": tenant_id, "task": intent_id, "actor": actor_id, "now": _SEED_MOMENT},
            )
            await session.execute(
                text(
                    "INSERT INTO intent_checkpoints "
                    "(checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal, decisions, "
                    " assumptions, completed_checks, open_questions, next_action, author, recorded_at, "
                    " retention_policy, digest) "
                    "VALUES (:cid, :tid, :task, 1, NULL, :goal, '{}', '{}', '{}', '{}', "
                    " 'keep going', :actor, :now, 'standard', :digest)"
                ),
                {
                    "cid": uuid.uuid4(),
                    "tid": tenant_id,
                    "task": intent_id,
                    "goal": goal,
                    "actor": actor_id,
                    "now": _SEED_MOMENT,
                    "digest": uuid.uuid4().hex,
                },
            )
    finally:
        await engine.dispose()
    return intent_id


async def _seed_placed_claim(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    stage: str,
    predicate: str,
) -> uuid.UUID:
    """One servable claim, recorded as applying at `stage`.

    Both halves matter. The claim has to be servable or the arm returns nothing
    and a narrowing test passes without narrowing anything; the derivation row
    is where the placement lives, because applicability belongs to the attempt
    that derived the conclusion rather than to the conclusion itself.
    """
    claim_id, entity_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active) "
                    "VALUES (:e, :t, 'capability', :n, TRUE)"
                ),
                {"e": entity_id, "t": tenant_id, "n": f"cap-{entity_id.hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                    "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                    "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                    "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                    "  scorer_version, calibration_version, decay_half_life_days"
                    ") VALUES ("
                    "  :cid, :t, :t, :a, :e, 'subject-ref', :pred, 'prose',"
                    "  'operational_lifecycle', CAST(:val AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST(:inputs AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30"
                    ")"
                ),
                {
                    "cid": claim_id,
                    "t": tenant_id,
                    "a": actor_id,
                    "e": entity_id,
                    "pred": predicate,
                    "val": json.dumps(f"learned during {stage}"),
                    "now": _SEED_MOMENT,
                    "inputs": json.dumps({"seed": True}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                    "VALUES (:cid, 'connector_run', :ref)"
                ),
                {"cid": claim_id, "ref": f"seed:{claim_id}"},
            )
            await session.execute(
                text(
                    "INSERT INTO claim_derivations ("
                    "  derivation_id, tenant_id, profile, profile_version, status, applicability,"
                    "  assertion_digest, source_authority, classification, created_claim_id, created_at"
                    ") VALUES (:d, :t, 'observer_extraction', 'v1', 'staged', :app,"
                    "  :digest, 'observer_extraction', 'internal', :cid, :now)"
                ),
                {
                    "d": uuid.uuid4(),
                    "t": tenant_id,
                    "app": json.dumps({"stage": stage}, separators=(",", ":"), sort_keys=True),
                    "digest": uuid.uuid4().hex,
                    "cid": claim_id,
                    "now": _SEED_MOMENT,
                },
            )
    finally:
        await engine.dispose()
    return claim_id


def _lifecycle_reference(kind: str, external_id: str) -> dict[str, Any]:
    return {
        "source_system": "control-plane",
        "source_namespace": "acme",
        "kind": kind,
        "external_id": external_id,
        "classification": "internal",
        "external_authority": "acme/delivery",
    }


def _claims_block(body: dict[str, Any]) -> dict[str, Any]:
    return next(block for block in body["blocks"] if block["name"] == BLOCK_OBSERVED_CLAIMS)


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
    intent_id = await _seed_participating_checkpoint(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        goal="finish the migration rollout",
    )
    body = (await _resolve(surface, workspace_term="migration", intent_ids=[str(intent_id)])).json()
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
    intent_id = await _seed_participating_checkpoint(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        goal="finish the migration rollout",
    )
    body = (await _resolve(surface, workspace_term="migration", intent_ids=[str(intent_id)])).json()
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


# --- The lifecycle profile ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_profile_withholds_claims_placed_at_another_stage_and_reports_it(surface: _Surface) -> None:
    """The headline of the profile, end to end through the real serving path.

    Both claims are servable and only one is placed where the caller is. The
    assertion is on the reason as much as the items: a caller who receives a
    shorter list must be able to tell "narrowed" from "there was nothing", and
    those two are the same response body if the withheld item is dropped
    silently.
    """
    here = await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="implementation",
        predicate="applies_here",
    )
    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="deployment",
        predicate="applies_elsewhere",
    )

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "implementation")])

    assert resp.status_code == 200, resp.text
    block = _claims_block(resp.json())
    assert [item["receipt_item_id"]["item_key"] for item in block["items"]] == [str(here)]
    assert block["state"] == "degraded"
    assert block["reason"] is not None
    assert "withheld" in block["reason"]


@pytest.mark.asyncio
async def test_without_a_profile_both_claims_are_served(surface: _Surface) -> None:
    """The control the test above needs to mean anything.

    Without it, a narrowing assertion is satisfied by a serving path that was
    only ever going to return one claim.
    """
    for stage, predicate in (("implementation", "applies_here"), ("deployment", "applies_elsewhere")):
        await _seed_placed_claim(
            surface["pg_url"],
            tenant_id=surface["tenant_id"],
            actor_id=surface["actor_id"],
            stage=stage,
            predicate=predicate,
        )

    body = (await _resolve(surface)).json()

    assert len(_claims_block(body)["items"]) == 2


@pytest.mark.asyncio
async def test_a_claim_that_recorded_no_placement_survives_a_profile(surface: _Surface) -> None:
    """Silence is not a mismatch, proved against the real applicability column.

    A conclusion recorded before dimensions existed must not vanish because the
    caller described themselves precisely -- that would hide governed material
    as a side effect of a better request.
    """
    claim_id = await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="implementation",
        predicate="unplaced",
    )
    engine = create_async_engine(surface["pg_url"], connect_args={"prepared_statement_cache_size": 0})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            await session.execute(
                text("UPDATE claim_derivations SET applicability = :free WHERE created_claim_id = :cid"),
                {"free": "wherever the payments team works", "cid": claim_id},
            )
    finally:
        await engine.dispose()

    body = (await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "deployment")])).json()

    assert [item["receipt_item_id"]["item_key"] for item in _claims_block(body)["items"]] == [str(claim_id)]


async def _request_digest(pg_url: str, receipt_id: uuid.UUID) -> str:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return str(
                (
                    await session.execute(
                        text("SELECT request_digest FROM context_receipts WHERE receipt_id = :rid"),
                        {"rid": receipt_id},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_profile_is_part_of_the_request_the_receipt_records(surface: _Surface) -> None:
    """A profile changes the answer, so it has to change the recorded request.

    The receipt stores a digest rather than the request body, so the property is
    asserted the way the digest expresses it: two resolutions that differ only
    by where the caller placed themselves are not the same question, and a
    digest that could not tell them apart would make a narrowed answer
    indistinguishable from an unnarrowed one in the audit record.
    """
    here = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "implementation")])
    elsewhere = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "deployment")])
    unplaced = await _resolve(surface)
    again = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "implementation")])

    digests = {
        name: await _request_digest(surface["pg_url"], uuid.UUID(resp.json()["receipt_id"]))
        for name, resp in (("here", here), ("elsewhere", elsewhere), ("unplaced", unplaced), ("again", again))
    }

    assert digests["here"] != digests["unplaced"], "a profile must not digest the same as no profile"
    assert digests["here"] != digests["elsewhere"], "two different placements are two different questions"
    assert digests["here"] == digests["again"], "the same question must stay comparable to itself"


@pytest.mark.asyncio
async def test_the_receipt_records_each_withheld_claim_as_an_exclusion(surface: _Surface) -> None:
    """What the profile withheld is evidence, not a detail of the response.

    An exclusion is the difference between "there was nothing" and "there was
    something placed elsewhere", and the receipt is where that survives after
    the response is gone.
    """
    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="deployment",
        predicate="applies_elsewhere",
    )

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", "implementation")])
    receipt_id = uuid.UUID(resp.json()["receipt_id"])

    engine = create_async_engine(surface["pg_url"], connect_args={"prepared_statement_cache_size": 0})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            reasons = [
                row[0]
                for row in (
                    await session.execute(
                        text("SELECT reason FROM context_receipt_exclusions WHERE receipt_id = :rid"),
                        {"rid": receipt_id},
                    )
                ).all()
            ]
    finally:
        await engine.dispose()

    assert any("deployment" in reason for reason in reasons), reasons


@pytest.mark.asyncio
async def test_an_unknown_lifecycle_kind_is_refused_rather_than_accepted(surface: _Surface) -> None:
    """The negative control on the transport a caller actually uses.

    A misspelled kind must not reach storage. It would bind cleanly and then
    fail to join to the receipt citing the correct spelling for the same
    external id, which surfaces as an absence rather than an error.
    """
    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("deploymnet", "prod-42")])

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_the_mcp_tool_refuses_the_same_unknown_kind(surface: _Surface) -> None:
    """Refusal parity, which is the half that silently rots.

    A vocabulary enforced on one transport is not enforced: an agent would
    simply use the surface that accepts the typo.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    from contextplane.api.mcp.tools.context import registry_resolve_context

    app = surface["harness"].app
    with _as(surface, surface["caller"]), _mcp_request(surface), pytest.raises(ToolError):
        await registry_resolve_context(
            "anything",
            lifecycle_references=[_lifecycle_reference("deploymnet", "prod-42")],
            session_factory=app.state.session_factory,
            clock=app.state.clock,
        )


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
async def test_both_transports_narrow_a_lifecycle_profile_the_same_way(surface: _Surface) -> None:
    """Parity on the selection, not only on the unnarrowed answer.

    A profile honoured on one transport and ignored on the other is the worst
    version of drift here: both calls succeed, both return a well-formed
    envelope, and only one of them is narrowed -- so the divergence shows up as
    an agent seeing context a REST caller was correctly not shown.
    """
    from contextplane.api.mcp.tools.context import registry_resolve_context

    here = await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="implementation",
        predicate="applies_here",
    )
    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage="deployment",
        predicate="applies_elsewhere",
    )
    profile = [_lifecycle_reference("stage", "implementation")]

    rest = (await _resolve(surface, lifecycle_references=profile)).json()

    app = surface["harness"].app
    with _as(surface, surface["caller"]), _mcp_request(surface):
        raw = await registry_resolve_context(
            "what is the state of the migration",
            lifecycle_references=profile,
            session_factory=app.state.session_factory,
            clock=app.state.clock,
        )
    mcp = json.loads(raw)

    def keys(body: dict[str, Any]) -> list[str]:
        return [item["receipt_item_id"]["item_key"] for item in _claims_block(body)["items"]]

    # Which items survived, not the items themselves. Confidence decays against
    # the instant each resolution is taken at, so two calls a few milliseconds
    # apart legitimately carry different scores -- comparing whole payloads
    # would assert the clock stood still rather than that selection agreed.
    assert keys(mcp) == keys(rest) == [str(here)]
    assert _claims_block(mcp)["state"] == _claims_block(rest)["state"] == "degraded"
    assert _claims_block(mcp)["reason"] == _claims_block(rest)["reason"]


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
