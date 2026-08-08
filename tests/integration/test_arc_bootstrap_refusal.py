"""Startup refuses to boot over pre-existing `artifact_activation` evidence.

No production writer in this deployment inserts `arc_approval_evidence` rows
of this type -- `ExceptionService` is the only writer, and it is hardcoded
to `exception_approval`. A row of this type can therefore only predate a
first-party writer (a direct SQL insert, an old code path, a bootstrap
script), and starting a deployment that carries one would let a receipt go
on asserting an approval nothing produced under today's invariants. This
test seeds exactly one such row and proves the startup guard refuses rather
than silently grandfathering it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.wiring.services import _assert_no_legacy_activation_evidence
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[ArcSeed]:
    """A fresh tenant/artifact/revision, cleaned of every evidence and
    exception row it accumulates before the next test runs.

    The guard under test counts `arc_approval_evidence` globally, with no
    tenant filter -- that is the literal query it runs at startup, against
    the whole deployment. Against this suite's session-scoped shared
    database, a row this fixture's tests plant would otherwise outlive the
    test and inflate the count the next test observes.
    """
    s = await seed_arc(factory, slug_prefix="arc-bootstrap")
    yield s
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_approved_exceptions WHERE lower_scope_tenant_id = :tid"),
            {"tid": s.tenant_id},
        )
        await session.execute(
            text("DELETE FROM arc_approval_evidence WHERE scope_tenant_id = :tid"),
            {"tid": s.tenant_id},
        )


async def _verifier(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> str:
    """A verifier row for `evidence.approval_verifier_id`'s foreign key --
    the guard being tested does not consult it; it only needs to exist for
    the seeded evidence row to satisfy the schema's representation CHECK."""
    verifier_id = f"v-{uuid.uuid4().hex[:12]}"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_verifiers ("
                "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                "  scope_tenant_id, provider_id, valid_from"
                ") VALUES (:vid, 'trusted_attestation_provider',"
                "          ARRAY['artifact_activation', 'exception_approval'], 'tenant', :tid,"
                "          'in-process-test', :from)"
            ),
            {"vid": verifier_id, "tid": seed.tenant_id, "from": ARC_NOW},
        )
    return verifier_id


async def _seed_legacy_evidence(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, verifier_id: str
) -> uuid.UUID:
    """One `artifact_activation` evidence row, exactly as a direct SQL
    insert would produce -- no attach, no activation. Its existence alone is
    what the guard refuses to boot over."""
    evidence_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_artifact_id,"
                "  approved_revision_id, approved_payload_digest, approving_principal, approving_role,"
                "  approval_timestamp, verification_method, approval_verifier_id, verifier_attestation,"
                "  verifier_identity, audit_log_reference"
                ") VALUES (:eid, 'artifact_activation', 'tenant', :tid, :aid, :rid, :digest,"
                "          'legacy@example.test', 'governance_owner', :now, 'verifier_attested', :vid,"
                "          CAST('{}' AS JSONB), 'in-process-test', 'audit://legacy/1')"
            ),
            {
                "eid": evidence_id,
                "tid": seed.tenant_id,
                "aid": seed.artifact_id,
                "rid": seed.revision_id,
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "vid": verifier_id,
                "now": ARC_NOW,
            },
        )
    return evidence_id


@pytest.mark.asyncio
async def test_boot_passes_with_no_legacy_evidence(factory: async_sessionmaker[AsyncSession]) -> None:
    assert await _assert_no_legacy_activation_evidence(factory) is None


@pytest.mark.asyncio
async def test_boot_refuses_with_one_pre_existing_activation_row(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, seed)
    await _seed_legacy_evidence(factory, seed, verifier_id)

    with pytest.raises(RuntimeError) as excinfo:
        await _assert_no_legacy_activation_evidence(factory)

    message = str(excinfo.value)
    assert "1" in message
    assert "artifact_activation" in message


@pytest.mark.asyncio
async def test_the_refusal_names_the_count_and_a_migration_requirement(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The operator-facing report must be actionable, not just a bare count."""
    verifier_id = await _verifier(factory, seed)
    await _seed_legacy_evidence(factory, seed, verifier_id)
    await _seed_legacy_evidence(factory, seed, verifier_id)

    with pytest.raises(RuntimeError) as excinfo:
        await _assert_no_legacy_activation_evidence(factory)

    message = str(excinfo.value)
    assert "2" in message
    assert "migration" in message.lower() or "bootstrap" in message.lower()


@pytest.mark.asyncio
async def test_exception_approval_evidence_never_blocks_boot(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Out of scope for this gate: `exception_approval` has a real
    first-party writer and neither blocks nor is rewritten by it.

    Evidence is inserted before its exception -- `arc_approved_exceptions`
    and `arc_approval_evidence` name each other, and the schema's cyclic
    foreign key is `DEFERRABLE INITIALLY DEFERRED` for exactly this reason;
    `approved_exception_id` on the evidence row carries no FK at all, so it
    may name an exception id that does not exist yet.
    """
    verifier_id = await _verifier(factory, seed)
    exception_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_exception_id,"
                "  approved_payload_digest, approving_principal, approving_role, approval_timestamp,"
                "  verification_method, approval_verifier_id, verifier_attestation, verifier_identity,"
                "  audit_log_reference"
                ") VALUES (:eid, 'exception_approval', 'tenant', :tid, :xid, :digest,"
                "          'ops@example.test', 'governance_owner', :now, 'verifier_attested', :vid,"
                "          CAST('{}' AS JSONB), 'in-process-test', 'audit://exception/1')"
            ),
            {
                "eid": evidence_id,
                "tid": seed.tenant_id,
                "xid": exception_id,
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "vid": verifier_id,
                "now": ARC_NOW,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_approved_exceptions ("
                "  exception_id, higher_scope_directive_id, higher_scope_revision_id, lower_scope_kind,"
                "  lower_scope_tenant_id, replacement_conflict_descriptor, exception_statement_plaintext,"
                "  justification_plaintext, effective_from, approval_evidence_id, created_at"
                ") VALUES (:xid, :did, :rid, 'tenant', :tid, CAST(:descriptor AS JSONB), 'stmt', 'just',"
                "          :now, :eid, :now)"
            ),
            {
                "xid": exception_id,
                "did": seed.directive_id,
                "rid": seed.revision_id,
                "tid": seed.tenant_id,
                "descriptor": '{"conflict_subject_digest": "%s"}' % ("f" * 64),
                "now": ARC_NOW,
                "eid": evidence_id,
            },
        )

    assert await _assert_no_legacy_activation_evidence(factory) is None
