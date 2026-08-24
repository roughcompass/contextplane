"""The instruction channel, over HTTP against a real Postgres.

E22-T14. Three things are proved here that no fake can prove.

**The submission is idempotent by content, against the real primary key.** A
second submission of the same instruction set is the same row, and the digest
comes back unchanged. A fake would agree with whatever the code did.

**The delta read is tenant-scoped inside the query.** Two tenants submit the
same instruction set -- identical bytes, therefore an identical digest -- and one
of them registers a delta against it. The other declares the same digest and
receives nothing. This is the test that fails if the tenant predicate is dropped
from the read, and the same-digest setup is deliberate: a differently-digested
pair would pass a query with no tenant clause at all.

**A digest whose content was never submitted resolves rather than failing**, and
is recorded as its own state. The three dispositions are distinguished in the
stored row, not only in the response, because the surfaces built on this read
the row.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.instructions import digest_of
from contextplane.context.schemas.envelope import BLOCK_EMPTY, BLOCK_INSTRUCTIONS, BLOCK_SUCCESS
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

_CONTENT = "Always run the deprecation check before proposing an interface change."
_DIGEST = digest_of(_CONTENT)
_NOW = datetime.datetime(2026, 8, 24, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def channel(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    slug = f"instr-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["producer"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                body = resp.json()

            yield {
                "actor_id": uuid.UUID(body["actor_id"]),
                "caller": caller,
                "client": client,
                "harness": harness,
                "pg_url": pg_container,
                "slug": slug,
                "tenant_id": uuid.UUID(body["tenant_id"]),
            }


def _as(channel: dict[str, Any]) -> Any:
    channel["harness"].configure_fetcher_for(channel["caller"])
    return patch_validator_for_actor(channel["caller"])


async def _submit(channel: dict[str, Any], content: str) -> httpx.Response:
    with _as(channel):
        return await channel["client"].post(
            "/v1/context/instruction-sets",
            headers=bearer_headers(tenant_slug=channel["slug"]),
            json={"content": content},
        )


async def _resolve(channel: dict[str, Any], **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {"query": "the state of the migration"}
    payload.update(body)
    with _as(channel):
        return await channel["client"].post(
            "/v1/context/resolve",
            headers=bearer_headers(tenant_slug=channel["slug"]),
            json=payload,
        )


async def _register_delta(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    digest: str | None = None,
    scope: str = "digest",
    target_principal: uuid.UUID | None = None,
    approved_by: uuid.UUID | None = None,
    body: str = "Run it on internal interfaces too.",
    contradicts: bool = False,
    note: str | None = None,
) -> uuid.UUID:
    """Author one delta directly.

    Inserted rather than posted because the authoring surface is an operator
    screen this task does not build -- E22-T14 builds the channel, and which
    delta reaches which agent is retrieval policy that follows it. Writing the
    row is the honest way to exercise the read without inventing the write
    surface's shape a wave early.
    """
    delta_id = uuid.uuid4()
    engine = create_async_engine(pg_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO instruction_deltas (
                        delta_id, tenant_id, scope, target_digest, target_principal, body,
                        contradicts, contradiction_note, authored_by, authored_at,
                        approved_by, approved_at
                    )
                    VALUES (:did, :tid, :scope, :digest, :principal, :body, :contradicts, :note,
                            :actor, :now, :approver, :approved_at)
                    """
                ),
                {
                    "actor": actor_id,
                    "approved_at": _NOW if approved_by else None,
                    "approver": approved_by,
                    "body": body,
                    "contradicts": contradicts,
                    "did": delta_id,
                    "digest": digest,
                    "note": note,
                    "now": _NOW,
                    "principal": target_principal,
                    "scope": scope,
                    "tid": tenant_id,
                },
            )
    finally:
        await engine.dispose()
    return delta_id


async def _declarations(pg_url: str, tenant_id: uuid.UUID) -> list[Any]:
    engine = create_async_engine(pg_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            rows = await session.execute(
                text(
                    """
                    SELECT digest, content_known, contradicted, contradiction_note, receipt_id
                      FROM resolution_instruction_declarations
                     WHERE tenant_id = :tid
                     ORDER BY declared_at
                    """
                ),
                {"tid": tenant_id},
            )
            return list(rows.all())
    finally:
        await engine.dispose()


# --- submission ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_submitting_returns_the_digest_and_submitting_again_returns_the_same_one(
    channel: dict[str, Any],
) -> None:
    """Idempotent without an idempotency key: the digest *is* the content, so a
    repeat submission is the same row and there is no key to get wrong."""
    first = await _submit(channel, _CONTENT)
    second = await _submit(channel, _CONTENT)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["digest"] == second.json()["digest"] == _DIGEST

    engine = create_async_engine(channel["pg_url"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            count = await session.execute(
                text("SELECT count(*) FROM declared_instruction_sets WHERE tenant_id = :tid"),
                {"tid": channel["tenant_id"]},
            )
            assert count.scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_instruction_set_is_refused(channel: dict[str, Any]) -> None:
    response = await _submit(channel, "   ")

    assert response.status_code == 422, response.text


# --- the three dispositions, in the response and in the row -------------------


@pytest.mark.asyncio
async def test_declaring_nothing_is_recorded_as_nothing_declared(channel: dict[str, Any]) -> None:
    response = await _resolve(channel)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instruction_disposition"] == "not_declared"
    assert "declared no instruction set" in body["instruction_block_note"]
    assert await _declarations(channel["pg_url"], channel["tenant_id"]) == [], (
        "the absence is the record; a row per undeclared resolve would put one on every "
        "resolution in the product to say nothing happened"
    )


@pytest.mark.asyncio
async def test_an_unsubmitted_digest_resolves_and_is_recorded_as_content_unknown(
    channel: dict[str, Any],
) -> None:
    """Failing the resolve would punish the caller for a state the service is in."""
    response = await _resolve(channel, instruction_digest=_DIGEST)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instruction_disposition"] == "declared_unknown"
    assert body["state"] == "complete", "an unknown digest is not a degradation"

    (row,) = await _declarations(channel["pg_url"], channel["tenant_id"])
    assert row.digest == _DIGEST
    assert row.content_known is False
    assert row.receipt_id is not None


@pytest.mark.asyncio
async def test_a_submitted_digest_with_no_delta_is_a_different_state_from_an_unsubmitted_one(
    channel: dict[str, Any],
) -> None:
    """Both are an empty block. Only one is a state the caller can leave."""
    await _submit(channel, _CONTENT)

    response = await _resolve(channel, instruction_digest=_DIGEST)

    body = response.json()
    assert body["instruction_disposition"] == "declared_known"
    assert body["instruction_block_note"] != ""
    assert "no governed correction applies" in body["instruction_block_note"]

    (row,) = await _declarations(channel["pg_url"], channel["tenant_id"])
    assert row.content_known is True


@pytest.mark.asyncio
async def test_a_malformed_digest_is_refused_by_the_wire_contract(channel: dict[str, Any]) -> None:
    response = await _resolve(channel, instruction_digest="not-a-digest")

    assert response.status_code == 422, response.text


# --- the delta reaches the agent ----------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_delta_comes_back_in_the_fifth_block(channel: dict[str, Any]) -> None:
    await _submit(channel, _CONTENT)
    delta_id = await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        digest=_DIGEST,
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert block["state"] == BLOCK_SUCCESS
    assert [item["payload"]["delta_id"] for item in block["items"]] == [str(delta_id)]
    assert body["instruction_block_note"] is None


@pytest.mark.asyncio
async def test_a_served_delta_carries_complete_trust_like_every_other_non_canonical_item(
    channel: dict[str, Any],
) -> None:
    """The block is non-canonical, so the envelope contract refuses an item
    without trust. This proves the arm satisfies it rather than that the contract
    exists."""
    await _submit(channel, _CONTENT)
    await _register_delta(
        channel["pg_url"], actor_id=channel["actor_id"], digest=_DIGEST, tenant_id=channel["tenant_id"]
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    trust = block["items"][0]["trust"]
    assert trust is not None
    assert trust["trust"] == "asserted"
    assert trust["assertion_kind"] == "policy"
    assert all(trust[field] is not None for field in ("source", "authority", "mutability", "classification"))


@pytest.mark.asyncio
async def test_a_withdrawn_delta_is_not_served(channel: dict[str, Any]) -> None:
    await _submit(channel, _CONTENT)
    delta_id = await _register_delta(
        channel["pg_url"], actor_id=channel["actor_id"], digest=_DIGEST, tenant_id=channel["tenant_id"]
    )

    engine = create_async_engine(channel["pg_url"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE instruction_deltas SET withdrawn_at = :now, withdrawn_by = :actor " "WHERE delta_id = :did"
                ),
                {"actor": channel["actor_id"], "did": delta_id, "now": _NOW},
            )
    finally:
        await engine.dispose()

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert block["state"] == BLOCK_EMPTY


@pytest.mark.asyncio
async def test_a_contradiction_is_served_flagged_and_recorded(channel: dict[str, Any]) -> None:
    """Served, because the contradicting delta is usually the valuable one and a
    channel that withholds its most useful message is one nobody relies on.
    Flagged, because an instruction that overrides what an operator told their
    agent without saying so is the product changing behaviour behind their back.
    """
    await _submit(channel, _CONTENT)
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        contradicts=True,
        digest=_DIGEST,
        note="the declared set exempts internal interfaces",
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert block["state"] == BLOCK_SUCCESS, "served, not withheld"
    assert block["items"][0]["payload"]["contradicts"] is True
    assert "internal interfaces" in block["items"][0]["payload"]["contradiction_note"]

    (row,) = await _declarations(channel["pg_url"], channel["tenant_id"])
    assert row.contradicted is True
    assert "internal interfaces" in row.contradiction_note


# --- the tenant boundary ------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tenants_delta_never_reaches_another_declaring_the_same_digest(
    pg_container: str,
) -> None:
    """The same instruction set, therefore the same digest, in two tenants.

    Deliberately the same digest: a pair with different digests would pass a
    query that had lost its tenant predicate entirely, so the test would report
    an isolation that was not there.
    """
    async with EntitlementAuthHarness(pg_container) as harness:
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tenants = []
            for index in range(2):
                slug = f"instr-{index}-{uuid.uuid4().hex[:8]}"
                persona = harness.add_persona(slug, roles=["producer"])
                harness.configure_fetcher_for(persona)
                with patch_validator_for_actor(persona):
                    whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                    assert whoami.status_code == 200, whoami.text
                tenants.append(
                    {
                        "actor_id": uuid.UUID(whoami.json()["actor_id"]),
                        "caller": persona,
                        "client": client,
                        "harness": harness,
                        "pg_url": pg_container,
                        "slug": slug,
                        "tenant_id": uuid.UUID(whoami.json()["tenant_id"]),
                    }
                )

            author, reader = tenants
            for tenant in tenants:
                assert (await _submit(tenant, _CONTENT)).json()["digest"] == _DIGEST

            await _register_delta(
                pg_container,
                actor_id=author["actor_id"],
                digest=_DIGEST,
                tenant_id=author["tenant_id"],
            )

            served = (await _resolve(author, instruction_digest=_DIGEST)).json()
            withheld = (await _resolve(reader, instruction_digest=_DIGEST)).json()

    assert next(b for b in served["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)["state"] == BLOCK_SUCCESS
    assert next(b for b in withheld["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)["state"] == BLOCK_EMPTY
    assert (
        withheld["instruction_disposition"] == "declared_known"
    ), "the reader's own submission is known to it; what it does not have is the other tenant's delta"


# --- the schema holds the rules the service states ----------------------------


@pytest.mark.asyncio
async def test_a_contradiction_with_no_note_is_refused_by_the_schema(channel: dict[str, Any]) -> None:
    """A contradiction nobody can name is a flag an evaluator cannot act on, and
    the rule is in the schema rather than only in the writer -- a second writer
    would otherwise have to remember it."""
    await _submit(channel, _CONTENT)

    with pytest.raises(Exception, match="ck_delta_contradiction_is_named"):
        await _register_delta(
            channel["pg_url"],
            actor_id=channel["actor_id"],
            contradicts=True,
            digest=_DIGEST,
            note=None,
            tenant_id=channel["tenant_id"],
        )


@pytest.mark.asyncio
async def test_a_delta_against_content_nobody_submitted_is_refused(channel: dict[str, Any]) -> None:
    """A delta written against a set its author could not have read is not a
    correction to anything."""
    with pytest.raises(Exception, match="instruction_deltas_tenant_id_target_digest_fkey"):
        await _register_delta(
            channel["pg_url"],
            actor_id=channel["actor_id"],
            digest=digest_of("never submitted"),
            tenant_id=channel["tenant_id"],
        )


# --- ADR 0021: what a delta is scoped by -------------------------------------


async def _second_actor(pg_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    """Another principal in the same tenant, for the approval rule."""
    actor_id = uuid.uuid4()
    engine = create_async_engine(pg_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'Second operator', 'human', :now, :a, :now)"
                ),
                {"a": actor_id, "now": _NOW, "sub": f"s-{actor_id.hex[:8]}", "t": tenant_id},
            )
    finally:
        await engine.dispose()
    return actor_id


@pytest.mark.asyncio
async def test_a_principal_scoped_delta_follows_the_agent_across_a_digest_change(
    channel: dict[str, Any],
) -> None:
    """The case digest-only targeting could not serve.

    An operator correcting an agent's behaviour means *that agent*, and an agent
    that edits its instructions the day after would silently stop receiving a
    digest-scoped correction — the digest moved, and nothing would say the
    correction was lost.
    """
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        scope="principal",
        target_principal=channel["actor_id"],
        tenant_id=channel["tenant_id"],
    )

    first = (await _resolve(channel, instruction_digest=_DIGEST)).json()
    other_digest = digest_of("a different instruction set entirely")
    second = (await _resolve(channel, instruction_digest=other_digest)).json()

    for body in (first, second):
        block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
        assert block["state"] == BLOCK_SUCCESS
        assert block["items"][0]["payload"]["scope"] == "principal"


@pytest.mark.asyncio
async def test_a_tenant_scoped_delta_reaches_a_caller_whose_content_was_never_submitted(
    channel: dict[str, Any],
) -> None:
    """ADR 0021's stated consequence. A `declared_unknown` caller receives
    broader corrections, and the disposition still says the contradiction cannot
    be computed rather than reporting none."""
    approver = await _second_actor(channel["pg_url"], channel["tenant_id"])
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        approved_by=approver,
        scope="tenant",
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    assert body["instruction_disposition"] == "declared_unknown"
    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert block["state"] == BLOCK_SUCCESS
    assert block["items"][0]["payload"]["scope"] == "tenant"


@pytest.mark.asyncio
async def test_every_applicable_delta_is_served_and_each_says_its_scope(
    channel: dict[str, Any],
) -> None:
    """Serving only the narrowest was rejected: two corrections about different
    things are not alternatives, and suppressing one because the other exists
    would withhold a governed instruction on the strength of a coincidence.

    **Precedence is in the payload and not in the order**, which is a correction
    ADR 0021 records against its own first draft. `ordered_items` sorts every
    block by receipt item id so a receipt is checkable across two resolutions, so
    an ordering asserted in the ADR would be one the envelope discards. This test
    asserts the *set* and the scope on each, which is what a reader can rely on.
    """
    await _submit(channel, _CONTENT)
    approver = await _second_actor(channel["pg_url"], channel["tenant_id"])
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        approved_by=approver,
        body="tenant-wide",
        scope="tenant",
        tenant_id=channel["tenant_id"],
    )
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        body="principal-wide",
        scope="principal",
        target_principal=channel["actor_id"],
        tenant_id=channel["tenant_id"],
    )
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        body="this set",
        digest=_DIGEST,
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert sorted((item["payload"]["scope"], item["payload"]["body"]) for item in block["items"]) == [
        ("digest", "this set"),
        ("principal", "principal-wide"),
        ("tenant", "tenant-wide"),
    ]


@pytest.mark.asyncio
async def test_another_principals_delta_does_not_reach_this_caller(channel: dict[str, Any]) -> None:
    """`principal` scope is a correction addressed to one agent. A second agent
    receiving it would be the broadcast the tenant scope requires approval for,
    without the approval."""
    other = await _second_actor(channel["pg_url"], channel["tenant_id"])
    await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        scope="principal",
        target_principal=other,
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()

    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert block["state"] == BLOCK_EMPTY


@pytest.mark.asyncio
async def test_a_broadcast_with_no_approver_is_refused_by_the_schema(channel: dict[str, Any]) -> None:
    """ADR 0021's second decision, in the schema rather than in a service. A
    tenant delta reaches every declaring agent including ones whose instructions
    nobody has read, and one person authoring that is the shape ADR 0020's
    dissent warns about with the fleet as the blast radius."""
    with pytest.raises(Exception, match="ck_delta_broadcast_is_approved"):
        await _register_delta(
            channel["pg_url"],
            actor_id=channel["actor_id"],
            scope="tenant",
            tenant_id=channel["tenant_id"],
        )


@pytest.mark.asyncio
async def test_a_self_approved_broadcast_is_refused(channel: dict[str, Any]) -> None:
    """A self-approval is an assertion wearing an approval's shape, which is the
    rule `ck_grant_not_self` already states one table over."""
    with pytest.raises(Exception, match="ck_delta_broadcast_is_approved"):
        await _register_delta(
            channel["pg_url"],
            actor_id=channel["actor_id"],
            approved_by=channel["actor_id"],
            scope="tenant",
            tenant_id=channel["tenant_id"],
        )


@pytest.mark.asyncio
async def test_a_narrow_delta_needs_no_approver(channel: dict[str, Any]) -> None:
    """The control. Requiring two people to correct one agent is the friction
    that makes a channel go unused, and the channel's value is that a correction
    is cheaper than letting an agent stay wrong."""
    await _submit(channel, _CONTENT)

    delta_id = await _register_delta(
        channel["pg_url"],
        actor_id=channel["actor_id"],
        digest=_DIGEST,
        tenant_id=channel["tenant_id"],
    )

    body = (await _resolve(channel, instruction_digest=_DIGEST)).json()
    block = next(b for b in body["blocks"] if b["name"] == BLOCK_INSTRUCTIONS)
    assert [item["payload"]["delta_id"] for item in block["items"]] == [str(delta_id)]


@pytest.mark.asyncio
async def test_a_scope_carrying_the_wrong_target_is_refused(channel: dict[str, Any]) -> None:
    """A delta with both a digest and a principal is two statements in one row,
    and the read would have to choose which one the author meant."""
    await _submit(channel, _CONTENT)

    with pytest.raises(Exception, match="ck_delta_target_matches_scope"):
        await _register_delta(
            channel["pg_url"],
            actor_id=channel["actor_id"],
            digest=_DIGEST,
            scope="principal",
            target_principal=channel["actor_id"],
            tenant_id=channel["tenant_id"],
        )
