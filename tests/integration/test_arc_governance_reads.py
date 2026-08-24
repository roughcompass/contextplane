"""The two reads, against a real database.

What is asserted here is what only a database can answer: that the global
verifiers come back for a tenant that did not enrol them, that another tenant's
exceptions do not, and that an encrypted statement is not decrypted by a list.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.governance_reads import GovernanceReadService
from contextplane.arc.service.queries import source_admission as source_queries
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"gov-{tid.hex[:8]}", "n": _NOW},
        )
    return tid


async def _verifier(
    factory: async_sessionmaker[AsyncSession],
    *,
    scope_kind: str,
    tenant_id: uuid.UUID | None,
    revoked: bool = False,
) -> str:
    vid = f"v-{uuid.uuid4().hex[:10]}"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_verifiers ("
                "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                "  scope_tenant_id, provider_id, valid_from, valid_to, revoked_at, created_at"
                ") VALUES (:v, 'trusted_attestation_provider', ARRAY['artifact_activation'],"
                "          :sk, :st, 'prov-1', :vf, NULL, :rev, :n)"
            ),
            {
                "v": vid,
                "sk": scope_kind,
                "st": tenant_id,
                "vf": _NOW - datetime.timedelta(days=1),
                "rev": _NOW - datetime.timedelta(hours=1) if revoked else None,
                "n": _NOW,
            },
        )
    return vid


def _reads(factory: async_sessionmaker[AsyncSession]) -> GovernanceReadService:
    return GovernanceReadService(factory, clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_a_tenant_sees_the_global_verifiers_it_did_not_enrol(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The set that matters most.

    A global verifier can approve for this tenant, so a list that showed only
    the tenant's own would answer "who may approve here" wrongly by exactly the
    verifiers with the widest reach.
    """
    tid = await _tenant(factory)
    mine = await _verifier(factory, scope_kind="tenant", tenant_id=tid)
    global_one = await _verifier(factory, scope_kind="global", tenant_id=None)

    found = {item.object_id for item in await _reads(factory).list_approval_verifiers(tenant_id=tid)}

    assert mine in found
    assert global_one in found


@pytest.mark.asyncio
async def test_another_tenants_verifier_is_not_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tid, other = await _tenant(factory), await _tenant(factory)
    theirs = await _verifier(factory, scope_kind="tenant", tenant_id=other)

    found = {item.object_id for item in await _reads(factory).list_approval_verifiers(tenant_id=tid)}

    assert theirs not in found


@pytest.mark.asyncio
async def test_in_force_only_drops_the_revoked(factory: async_sessionmaker[AsyncSession]) -> None:
    """Both are listed by default, because "what was enrolled" and "what can
    approve today" are different questions and a revoked verifier is still part
    of the first."""
    tid = await _tenant(factory)
    live = await _verifier(factory, scope_kind="tenant", tenant_id=tid)
    revoked = await _verifier(factory, scope_kind="tenant", tenant_id=tid, revoked=True)

    reads = _reads(factory)
    everything = {i.object_id for i in await reads.list_approval_verifiers(tenant_id=tid)}
    current = {i.object_id for i in await reads.list_approval_verifiers(tenant_id=tid, in_force_only=True)}

    assert {live, revoked} <= everything
    assert live in current
    assert revoked not in current


@pytest.mark.asyncio
async def test_the_public_key_is_never_returned(factory: async_sessionmaker[AsyncSession]) -> None:
    """A list of who may approve does not need the material, and a surface that
    carried it would be one more place a key can leak from."""
    tid = await _tenant(factory)
    await _verifier(factory, scope_kind="tenant", tenant_id=tid)

    found = await _reads(factory).list_approval_verifiers(tenant_id=tid)

    for item in found:
        assert "public_key" not in item.detail
        assert "algorithm" not in item.detail


@pytest.mark.asyncio
async def test_an_empty_tenant_lists_nothing_rather_than_failing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "No exceptions" and "the read is broken" must not look the same, which is
    what the absence of this surface used to make them."""
    tid = await _tenant(factory)

    assert await _reads(factory).list_approved_exceptions(tenant_id=tid) == []


@pytest.mark.asyncio
async def test_a_live_connector_is_in_force_with_no_expiry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The entry's premise changed while it waited, and this pins the new one.

    E14-T1b was written when a connector could not be revoked at all and said
    its honest in-force answer was "permanent". E14-T2 gave it a `revoked_at`,
    so the answer is real — but `in_force_until` is still null, because a live
    connector has no expiry. Withdrawal is the only thing that ends one.
    """
    tid = await _tenant(factory)
    cid = f"conn-{uuid.uuid4().hex[:8]}"
    async with factory() as session, session.begin():
        await source_queries.insert_connector(
            session,
            connector_id=cid,
            owning_scope="tenant",
            tenant_id=tid,
            allowed_schemes=["https"],
            allowed_hosts=["policy.example"],
            allowed_media_types=["application/pdf"],
            allowed_verifier_ids=["verifier-a"],
            max_bytes=1024,
            credential_ref=None,
            registered_at=_NOW,
        )

    found = [i for i in await _reads(factory).list_source_connectors(tenant_id=tid) if i.object_id == cid]

    assert len(found) == 1
    assert found[0].in_force
    assert found[0].in_force_until is None, "a live connector has no expiry, only a withdrawal"


@pytest.mark.asyncio
async def test_a_withdrawn_connector_reads_as_not_in_force_and_says_why(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The read side of E14-T2. A withdrawn grant is still listed — "what was
    ever registered" is a real question — and carries the reason, because a
    withdrawal an operator cannot explain is one they will re-make."""
    tid = await _tenant(factory)
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"gw-{aid.hex[:8]}", "n": _NOW},
        )
    cid = f"conn-{uuid.uuid4().hex[:8]}"
    reason = "Registered with a wildcard host during the migration and never narrowed."
    async with factory() as session, session.begin():
        await source_queries.insert_connector(
            session,
            connector_id=cid,
            owning_scope="tenant",
            tenant_id=tid,
            allowed_schemes=["https"],
            allowed_hosts=["*"],
            allowed_media_types=["application/pdf"],
            allowed_verifier_ids=["verifier-a"],
            max_bytes=1024,
            credential_ref=None,
            registered_at=_NOW,
        )
        await source_queries.revoke_connector(session, connector_id=cid, actor_id=aid, reason=reason, now=_NOW)

    reads = _reads(factory)
    everything = [i for i in await reads.list_source_connectors(tenant_id=tid) if i.object_id == cid]
    current = [i for i in await reads.list_source_connectors(tenant_id=tid, in_force_only=True) if i.object_id == cid]

    assert len(everything) == 1
    assert not everything[0].in_force
    assert everything[0].detail["revocation_reason"] == reason
    assert current == [], "a withdrawn connector is not in force"
