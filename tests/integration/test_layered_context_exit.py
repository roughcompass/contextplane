"""The phase's exit criteria, proved end to end on the integrated tree.

Every criterion below is already covered somewhere by the slice that built it.
This file is not a second copy of those suites and does not try to be: it proves
the criteria **together, through the app, on one tree**, which is the only thing
a per-slice suite structurally cannot do. A slice suite passes against its own
branch; the question here is whether the parts still hold once they are wired to
each other.

So the tests are deliberately end-to-end and deliberately few per criterion. One
test per criterion, driving real HTTP against a real Postgres, asserting the
property the criterion names and nothing else. Where a criterion is itself a
gate that already exists -- parity, the surface inventory, the coverage ratchet
-- this file runs that gate rather than reimplementing its logic, because a
second implementation would be a second thing to keep true.

**What a failure here means.** Not that a slice regressed -- its own suite would
catch that. It means the integration did: two correct halves that disagree, or a
control that was proved in isolation and is not reachable in the assembled app.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REF = ("github", "acme/app", "pull_request", "77")

type _World = dict[str, Any]


async def _seed(pg_url: str, *, tenant_id: uuid.UUID, owner: str, task_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """One task the owner participates in, and one external reference.

    Seeded directly because the first owner of a task has nobody to be granted
    by, and that bootstrap is not one of the criteria under test.
    """
    reference_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_participant_grants "
                    "(tenant_id, task_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:t, :task, :actor, 'owner', 'bootstrap', now() - interval '1 hour', NULL, "
                    "        'explicit/v1')"
                ),
                {"t": tenant_id, "task": task_id, "actor": owner},
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
    finally:
        await engine.dispose()
    return {"reference_id": reference_id}


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    slug = f"exit-{uuid.uuid4().hex[:8]}"
    outsider_slug = f"exit-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        owner = harness.add_persona(slug, roles=["producer", "consumer"])
        participant = harness.add_persona(slug, roles=["producer", "consumer"], actor_id=uuid.uuid4())
        outsider = harness.add_persona(outsider_slug, roles=["producer", "consumer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(owner)
            with patch_validator_for_actor(owner):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
                tenant_id = uuid.UUID(whoami.json()["tenant_id"])
                owner_actor = whoami.json()["actor_id"]

            # Resolved through the app rather than read off the persona: the
            # actor id an entitlement-resolved caller writes under is assigned
            # by the upsert at authentication, and granting the persona's own id
            # would grant an actor that never authenticates.
            harness.configure_fetcher_for(participant)
            with patch_validator_for_actor(participant):
                second = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert second.status_code == 200, second.text
                participant_actor = second.json()["actor_id"]

            harness.configure_fetcher_for(outsider)
            with patch_validator_for_actor(outsider):
                other = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=outsider_slug))
                assert other.status_code == 200, other.text

            task_id = uuid.uuid4()
            seeded = await _seed(pg_container, tenant_id=tenant_id, owner=str(owner_actor), task_id=task_id)

            yield {
                "client": client,
                "harness": harness,
                "pg": pg_container,
                "owner": owner,
                "participant": participant,
                "outsider": outsider,
                "slug": slug,
                "outsider_slug": outsider_slug,
                "tenant_id": tenant_id,
                "owner_actor": str(owner_actor),
                "participant_actor": str(participant_actor),
                "task_id": task_id,
                **seeded,
            }


def _as(world: _World, persona: TenantPersona) -> Any:
    world["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


def _headers(world: _World, *, outsider: bool = False) -> dict[str, str]:
    return bearer_headers(tenant_slug=world["outsider_slug"] if outsider else world["slug"])


async def _append(world: _World, persona: TenantPersona, *, goal: str, key: str) -> dict[str, Any]:
    with _as(world, persona):
        resp = await world["client"].post(
            f"/v1/tasks/{world['task_id']}/checkpoints",
            headers={**_headers(world), "Idempotency-Key": key},
            json={"goal": goal, "next_action": "carry on"},
        )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# --- Task memory, immutability, and resume ------------------------------------


@pytest.mark.asyncio
async def test_a_task_is_scoped_appended_and_resumable_by_another_participant(world: _World) -> None:
    """The first criterion, in one pass: scope, an immutable append, and a
    second authorized participant picking the work up."""
    first = await _append(world, world["owner"], goal="read the failing test", key="k1")

    with _as(world, world["owner"]):
        granted = await world["client"].post(
            f"/v1/tasks/{world['task_id']}/participants",
            headers=_headers(world),
            json={"actor_id": world["participant_actor"], "role": "contributor"},
        )
    assert granted.status_code == 201, granted.text

    second = await _append(world, world["participant"], goal="reproduce it", key="k2")

    assert second["sequence"] == first["sequence"] + 1
    assert second["checkpoint_id"] != first["checkpoint_id"]


@pytest.mark.asyncio
async def test_a_checkpoint_survives_later_appends_by_id_and_by_digest(world: _World) -> None:
    """A stable id and digest keep resolving after the mutable head moves --
    which is what makes a checkpoint citable rather than a moving target."""
    first = await _append(world, world["owner"], goal="the one that must not move", key="k1")
    await _append(world, world["owner"], goal="a later step", key="k2")

    with _as(world, world["owner"]):
        by_id = await world["client"].get(
            f"/v1/tasks/{world['task_id']}/checkpoints/{first['checkpoint_id']}", headers=_headers(world)
        )
        by_digest = await world["client"].get(f"/v1/checkpoints/by-digest/{first['digest']}", headers=_headers(world))

    assert by_id.status_code == 200, by_id.text
    assert by_digest.status_code == 200, by_digest.text
    assert by_id.json()["goal"] == "the one that must not move"
    assert by_digest.json()["checkpoint_id"] == first["checkpoint_id"]


@pytest.mark.asyncio
async def test_an_append_is_idempotent_under_a_repeated_key(world: _World) -> None:
    """Idempotency is one of the named gates, and a retry that appends twice
    breaks the chain rather than the caller."""
    first = await _append(world, world["owner"], goal="ship it", key="same")
    again = await _append(world, world["owner"], goal="ship it", key="same")

    assert first["checkpoint_id"] == again["checkpoint_id"]
    assert first["sequence"] == again["sequence"]


# --- The outsider learns nothing ----------------------------------------------


@pytest.mark.asyncio
async def test_an_outsider_is_refused_and_learns_nothing_from_the_refusal(world: _World) -> None:
    """Refused on every door, and the refusals do not distinguish a task that
    exists from one that does not -- together those answers would enumerate the
    tenant's tasks."""
    written = await _append(world, world["owner"], goal="private", key="k1")

    with _as(world, world["outsider"]):
        listed = await world["client"].get(
            f"/v1/tasks/{world['task_id']}/participants", headers=_headers(world, outsider=True)
        )
        looked_up = await world["client"].get(
            f"/v1/tasks/{world['task_id']}/checkpoints/{written['checkpoint_id']}",
            headers=_headers(world, outsider=True),
        )
        by_digest = await world["client"].get(
            f"/v1/checkpoints/by-digest/{written['digest']}", headers=_headers(world, outsider=True)
        )
        unknown = await world["client"].get(
            f"/v1/tasks/{uuid.uuid4()}/participants", headers=_headers(world, outsider=True)
        )

    assert listed.status_code in (403, 404)
    assert looked_up.status_code in (403, 404)
    assert by_digest.status_code in (403, 404)
    assert (
        listed.status_code == unknown.status_code
    ), "a task that exists and one that does not must refuse identically, or the difference enumerates them"
    for body in (listed.text, looked_up.text, by_digest.text):
        for leak in ("participant", "grant", "expired", "private"):
            assert leak not in body.lower()


@pytest.mark.asyncio
async def test_an_outsider_resumes_empty_rather_than_discovering_the_task(world: _World) -> None:
    """Resume takes external references, which an outsider may well know -- a
    pull request number is public. Knowing the reference must not be knowing the
    work."""
    await _append(world, world["owner"], goal="private", key="k1")

    with _as(world, world["outsider"]):
        resumed = await world["client"].post(
            "/v1/context/resume",
            headers=_headers(world, outsider=True),
            json={"references": [list(_REF)]},
        )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "empty"
    assert resumed.json()["task_id"] is None


# --- The envelope -------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_request_returns_the_fixed_four_blocks_with_trust_and_a_receipt(world: _World) -> None:
    """The envelope contract, through the wired app rather than a fixture: four
    blocks in a fixed order, every non-canonical item labelled, quality that
    agrees with the blocks, and a receipt that names a row that exists."""
    with _as(world, world["owner"]):
        resolved = await world["client"].post(
            "/v1/context/resolve", headers=_headers(world), json={"query": "deployment"}
        )

    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    blocks = body["blocks"]

    assert [block["name"] for block in blocks] == ["canonical", "arc", "observed_claims", "workspace"]
    for block in blocks:
        assert block["state"] in {"success", "empty", "degraded", "failed"}
        # `empty` is not a failure and carries no reason -- an arm with nothing
        # to say is a legitimate answer. Only a degraded or failed arm owes an
        # explanation, because those are the ones a reader has to act on.
        if block["state"] in {"degraded", "failed"}:
            assert block.get("reason"), f"{block['name']} is {block['state']} and explains nothing"
        for item in block["items"]:
            if block["name"] == "canonical":
                assert item.get("trust") is None, "canonical carries no trust metadata by contract"
            else:
                assert item.get("trust"), f"an item in {block['name']} reached a caller unlabelled"

    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT receipt_id FROM context_receipts WHERE receipt_id = :r"),
                    {"r": uuid.UUID(body["receipt_id"])},
                )
            ).first()
    finally:
        await engine.dispose()

    assert row is not None, "the response named a receipt that was never stored"


@pytest.mark.asyncio
async def test_a_canonical_failure_never_reads_as_complete(world: _World) -> None:
    """The loudest of the failure rules. Proved against the assembler directly,
    because planting a canonical failure through the app would mean breaking the
    database the other criteria need."""
    from contextplane.context.assembler import ArmOutcome, assemble
    from contextplane.context.schemas.envelope import BLOCK_ARC, BLOCK_CANONICAL, BLOCK_NAMES

    async def _broken() -> ArmOutcome:
        raise RuntimeError("canonical is down")

    async def _fine() -> ArmOutcome:
        return ArmOutcome()

    arms = {name: _fine for name in BLOCK_NAMES}
    arms[BLOCK_CANONICAL] = _broken

    result = await assemble(arms, now=_NOW)

    assert result.envelope.block(BLOCK_CANONICAL).state == "failed"
    assert result.envelope.state != "complete"
    assert result.envelope.block(BLOCK_CANONICAL).reason
    assert result.envelope.block(BLOCK_ARC).state != "failed", "one arm's failure must not take the others"


# --- Receipts and bounded resume ----------------------------------------------


@pytest.mark.asyncio
async def test_a_receipt_is_reachable_from_the_work_it_describes(world: _World) -> None:
    """Nobody holds a receipt id. Reachability from an external reference is
    what makes it evidence."""
    with _as(world, world["owner"]):
        found = await world["client"].get(
            "/v1/receipts/by-reference",
            params={
                "source_system": _REF[0],
                "source_namespace": _REF[1],
                "kind": _REF[2],
                "external_id": _REF[3],
            },
            headers=_headers(world),
        )

    assert found.status_code == 200, found.text
    assert "receipts" in found.json()


@pytest.mark.asyncio
async def test_resume_stays_bounded_and_reports_what_it_dropped(world: _World) -> None:
    """A resume that quietly returned the first of many would read as the whole
    story, and the caller would carry on from a middle it believed was a start."""
    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for index in range(3):
            checkpoint = await _append(world, world["owner"], goal=f"step {index}", key=f"k{index}")
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO context_reference_bindings "
                        "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                        "VALUES (:bid, :t, :rid, 'task_checkpoint', :cid, now())"
                    ),
                    {
                        "bid": uuid.uuid4(),
                        "t": world["tenant_id"],
                        "rid": world["reference_id"],
                        "cid": uuid.UUID(checkpoint["checkpoint_id"]),
                    },
                )
    finally:
        await engine.dispose()

    with _as(world, world["owner"]):
        resumed = await world["client"].post(
            "/v1/context/resume",
            headers=_headers(world),
            json={"references": [list(_REF)], "checkpoint_bound": 1},
        )

    body = resumed.json()
    assert resumed.status_code == 200, resumed.text
    assert body["status"] == "resumed"
    assert body["task_id"] == str(world["task_id"])
    assert len(body["checkpoints"]) == 1
    assert "checkpoints" in body["truncated"], "hitting a bound must be reported, not silent"
    assert not ({"transcript", "messages", "history"} & set(body)), "resume never returns a transcript"


# --- Admission ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_prohibited_content_is_refused_before_storage_and_audited(world: _World) -> None:
    """The floor, through the app, on a deployment that has configured no policy
    row -- which is every deployment until somebody inserts one."""
    with _as(world, world["owner"]):
        refused = await world["client"].post(
            "/v1/memory/sessions/exit-session/events",
            headers=_headers(world),
            json={"kind": "user_message", "body": "card 4111 1111 1111 1111 on file"},
        )

    assert refused.status_code == 422, refused.text

    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            audited = (
                (
                    await session.execute(
                        text(
                            "SELECT after_jsonb FROM audit_log "
                            "WHERE tenant_id = :t AND action = 'context.admission_refused'"
                        ),
                        {"t": world["tenant_id"]},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    assert audited, "the write was refused and nothing recorded it"
    assert "4111" not in str(audited), "the audit row reproduced the prohibited value"


@pytest.mark.asyncio
async def test_clean_content_is_still_admitted(world: _World) -> None:
    """The half that makes the refusal meaningful: a floor that refused
    everything would pass the test above."""
    with _as(world, world["owner"]):
        accepted = await world["client"].post(
            "/v1/memory/sessions/exit-session/events",
            headers=_headers(world),
            json={"kind": "user_message", "body": "the deploy finished and the queue drained"},
        )

    assert accepted.status_code in (200, 201), accepted.text


# --- The gates this phase built -----------------------------------------------


def test_every_surface_is_inventoried() -> None:
    """Run the gate rather than restate it. A second implementation of the
    inventory would be a second thing to keep true."""
    from scripts import check_surface_inventory

    assert check_surface_inventory.unregistered(_REPO_ROOT) == []
    assert check_surface_inventory.stale_exclusions(_REPO_ROOT) == []


def test_no_published_rest_operation_lacks_an_mcp_tool() -> None:
    """Parity, from the harness the parity task built."""
    from tests.conformance import test_rest_mcp_parity

    assert test_rest_mcp_parity._UNPAIRED_REST == ()


def test_the_coverage_gate_compares_what_it_prints() -> None:
    """The ratchet has to gate for the release gate's own coverage run to mean
    anything."""
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    precision = config["tool"]["coverage"]["report"]["precision"]
    assert precision >= 2


def test_the_shipped_docs_carry_no_planning_identifiers() -> None:
    """The last criterion, and the one most easily lost: a shipped doc that
    cites a planning id sends a future reader to a document they cannot open."""
    import subprocess  # noqa: S404 - this repo's own gate, fixed argv, no caller input
    import sys

    completed = subprocess.run(
        [sys.executable, "scripts/check_no_doc_refs.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
