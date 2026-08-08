"""Approval-trust revocation: withdrawing a verifier or one piece of evidence
must withdraw everything that trust vouched for.

Two tests matter most here. `test_revoking_a_verifier_tombstones_the_mandatory_obligation`
is the one that makes revocation real rather than cosmetic: if the obligation
disappeared along with its revision, every resolution it used to block would
silently start succeeding. And
`test_operator_signed_evidence_is_cascaded_through_signer_key_id` is the one
that is easiest to get wrong by half -- evidence has two different columns
that can each point at a verifier, and a cascade that only followed one of
them would leave every approval made through the other standing.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.approval import ApprovalTrustWithdrawn
from contextplane.arc.service.approval_trust import ApprovalTrustService
from contextplane.arc.service.artifact import (
    OBLIGATION_MISSING_INVALID,
    OBLIGATION_SATISFIED,
    ArtifactService,
    EvidenceTypeNotWritableError,
)
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-trust")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_activation_evidence(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """`artifact_activation` evidence is what a real deployment's startup
    guard (`_assert_no_legacy_activation_evidence`) counts globally, and
    this suite's database is shared for the whole pytest session -- a row
    left behind here would make an unrelated file's app boot refuse for a
    reason that has nothing to do with what it is testing. Every test below
    plants such rows directly (there is no first-party writer to seed them
    through); this is what keeps none of them outliving their test.
    """
    yield
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_revisions SET approval_evidence_id = NULL WHERE approval_evidence_id IN "
                "(SELECT evidence_id FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation')"
            )
        )
        await session.execute(
            text(
                "DELETE FROM arc_approval_evidence_revocations WHERE evidence_id IN "
                "(SELECT evidence_id FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation')"
            )
        )
        await session.execute(text("DELETE FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation'"))


class _AllVisible:
    async def visible_capability_ids(self, ctx: object, capability_ids: object) -> list[uuid.UUID]:
        return list(capability_ids)  # type: ignore[arg-type]


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=())


def _service(factory: async_sessionmaker[AsyncSession], *, now: datetime.datetime = ARC_NOW) -> ApprovalTrustService:
    return ApprovalTrustService(factory, authorization=_authorization(), clock=FakeClock(now))


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> ApprovalTrustService:
    return _service(factory)


def _ctx(seed: ArcSeed, *, roles: list[str] | None = None) -> ArcRequestContext:
    """The identity that *calls* the service.

    Deliberately unprivileged by default (`roles=[]`): the router's
    `_require_global_operator` is the only gate on this operation, and this
    service must not re-check a role on top of it. A test below calls the
    service with no role at all to prove that.
    """
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=roles or [], oidc_subject="s")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id="h")


# --- fixture builders: verifiers, revisions, evidence, exceptions -------------------


async def _second_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A tenant independent of the `seed` fixture's own, for the cross-tenant test."""
    tenant_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tenant_id, "slug": f"arc-trust-b-{tenant_id.hex[:8]}", "now": now},
        )
    return tenant_id


async def _verifier(factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID | None, kind: str) -> str:
    """`kind` is `'trusted_attestation_provider'` (matched via
    `approval_verifier_id`) or `'operator_public_key'` (matched via
    `signer_key_id`) -- the schema's `ck_arc_verifiers_representation` fixes
    which columns each carries."""
    verifier_id = f"v-{uuid.uuid4().hex[:12]}"
    scope_kind = "tenant" if tenant_id is not None else "global"
    async with factory() as session, session.begin():
        if kind == "trusted_attestation_provider":
            await session.execute(
                text(
                    "INSERT INTO arc_approval_verifiers ("
                    "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                    "  scope_tenant_id, provider_id, valid_from"
                    ") VALUES (:vid, 'trusted_attestation_provider', "
                    "          ARRAY['artifact_activation', 'exception_approval'],"
                    "          :scope, :tid, 'in-process-test', :from)"
                ),
                {
                    "vid": verifier_id,
                    "scope": scope_kind,
                    "tid": tenant_id,
                    "from": ARC_NOW - datetime.timedelta(days=1),
                },
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO arc_approval_verifiers ("
                    "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                    "  scope_tenant_id, algorithm, public_key, valid_from"
                    ") VALUES (:vid, 'operator_public_key', ARRAY['artifact_activation'],"
                    "          :scope, :tid, 'Ed25519', :pub, :from)"
                ),
                {
                    "vid": verifier_id,
                    "scope": scope_kind,
                    "tid": tenant_id,
                    "pub": b"x" * 32,
                    "from": ARC_NOW - datetime.timedelta(days=1),
                },
            )
    return verifier_id


async def _revision_with_obligation(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, lifecycle_state: str = "active"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An artifact, a revision with no approval evidence yet, a `citation_only`
    directive, and a `satisfied` mandatory obligation pointing at the
    revision.

    Built directly against the tables rather than through
    `ArtifactService.activate`, because the cascade under test reads and
    writes these same tables directly -- building the fixture the same way
    keeps a test failure about the cascade's own query, not about
    `ArtifactService`'s unrelated activation invariants.

    Returns `(artifact_id, revision_id, directive_id)`.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts ("
                "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
                ") VALUES (:aid, :tid, :slug, 'policy', :title, :now, :issuer, :subject)"
            ),
            {
                "aid": artifact_id,
                "tid": tenant_id,
                "slug": f"a-{artifact_id.hex[:8]}",
                "title": f"Test artifact {artifact_id.hex[:8]}",
                "now": ARC_NOW,
                "issuer": "https://idp.example.test",
                "subject": "seed-actor",
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
                ") VALUES (:rid, :aid, :tid, 'test', :loc, :rloc, :digest, :state, :efrom,"
                "          :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "          :retention, 'none', 'body', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": tenant_id,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rloc": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "state": lifecycle_state,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
                "now": ARC_NOW,
            },
        )
        await session.execute(
            text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:did, :aid)"),
            {"did": directive_id, "aid": artifact_id},
        )
        await session.execute(
            text(
                "INSERT INTO arc_directives ("
                "  directive_id, revision_id, tenant_id, directive_type,"
                "  compact_statement_plaintext, source_anchor"
                ") VALUES (:did, :rid, :tid, 'citation_only', 'Do the thing', 'anchor-1')"
            ),
            {"did": directive_id, "rid": revision_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO arc_mandatory_obligations ("
                "  obligation_id, artifact_id, directive_id, current_revision_id,"
                "  applicability_snapshot, applicability_digest, obligation_state, effective_from, updated_at"
                ") VALUES (:oid, :aid, :did, :rid, CAST(:snapshot AS JSONB), :digest, :state, :efrom, :now)"
            ),
            {
                "oid": uuid.uuid4(),
                "aid": artifact_id,
                "did": directive_id,
                "rid": revision_id,
                "snapshot": json.dumps({"scope": "tenant", "task_kinds": ["deployment"]}),
                "digest": "d" * 64,
                "state": OBLIGATION_SATISFIED,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "now": ARC_NOW,
            },
        )
    return artifact_id, revision_id, directive_id


async def _activation_evidence(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    verifier_id: str,
    via: str,
) -> uuid.UUID:
    """`artifact_activation` evidence, then attached to the revision exactly
    the way `ArtifactService.attach_approval_evidence` does it.

    `via` selects which of the schema's two verifier links this evidence
    uses -- `'approval_verifier_id'` (`verifier_attested`) or
    `'signer_key_id'` (`operator_signed`) -- and `ck_arc_evidence_representation`
    requires the other three of the four columns to be NULL, which is why
    exactly one branch below is populated.
    """
    evidence_id = uuid.uuid4()
    verifier_attested = via == "approval_verifier_id"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_artifact_id,"
                "  approved_revision_id, approved_payload_digest, approving_principal, approving_role,"
                "  approval_timestamp, verification_method, approval_verifier_id, verifier_attestation,"
                "  verifier_identity, signer_key_id, signature, audit_log_reference, created_at"
                ") VALUES (:eid, 'artifact_activation', 'tenant', :tid, :aid, :rid, :digest,"
                "          'operator@example.test', 'governance_owner', :now, :method, :avid,"
                "          CAST(:attestation AS JSONB), :identity, :skid, :sig, 'audit://approval/1', :now)"
            ),
            {
                "eid": evidence_id,
                "tid": tenant_id,
                "aid": artifact_id,
                "rid": revision_id,
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "method": "verifier_attested" if verifier_attested else "operator_signed",
                "avid": verifier_id if verifier_attested else None,
                "attestation": "{}" if verifier_attested else None,
                "identity": "in-process-test" if verifier_attested else None,
                "skid": None if verifier_attested else verifier_id,
                "sig": None if verifier_attested else "dGVzdC1zaWduYXR1cmU=",
                "now": ARC_NOW,
            },
        )
        await session.execute(
            text("UPDATE arc_revisions SET approval_evidence_id = :eid WHERE revision_id = :rid"),
            {"eid": evidence_id, "rid": revision_id},
        )
    return evidence_id


async def _exception_with_evidence(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    verifier_id: str,
    higher_directive_id: uuid.UUID,
    higher_revision_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """An approved exception plus the verifier-attested evidence approving it.

    Inserted as a pair in one transaction because the schema's cyclic
    foreign key between the two tables is `DEFERRABLE INITIALLY DEFERRED`
    for exactly this: each row names the other, so whichever is written
    first must reference an id that does not exist yet.

    Returns `(exception_id, evidence_id)`.
    """
    exception_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_exception_id,"
                "  approved_payload_digest, approving_principal, approving_role, approval_timestamp,"
                "  verification_method, approval_verifier_id, verifier_attestation, verifier_identity,"
                "  audit_log_reference, created_at"
                ") VALUES (:eid, 'exception_approval', 'tenant', :tid, :xid, :digest,"
                "          'operator@example.test', 'governance_owner', :now, 'verifier_attested', :vid,"
                "          CAST('{}' AS JSONB), 'in-process-test', 'audit://approval/2', :now)"
            ),
            {
                "eid": evidence_id,
                "tid": tenant_id,
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
                "did": higher_directive_id,
                "rid": higher_revision_id,
                "tid": tenant_id,
                "descriptor": json.dumps({"conflict_subject_digest": "f" * 64}),
                "now": ARC_NOW,
                "eid": evidence_id,
            },
        )
    return exception_id, evidence_id


async def _row(factory: async_sessionmaker[AsyncSession], sql: str, params: dict[str, object]):  # type: ignore[no-untyped-def]
    async with factory() as session:
        return (await session.execute(text(sql), params)).one()


async def _scalar(factory: async_sessionmaker[AsyncSession], sql: str, params: dict[str, object]) -> object:
    async with factory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


# --- revoke_verifier: the cascade ---------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_a_verifier_revokes_a_revision_activated_on_its_evidence(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    artifact_id, revision_id, _directive_id = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="verifier compromised")

    row = await _row(
        factory,
        "SELECT lifecycle_state, revoked_at FROM arc_revisions WHERE revision_id = :rid",
        {"rid": revision_id},
    )
    assert row.lifecycle_state == "revoked"
    assert row.revoked_at is not None


@pytest.mark.asyncio
async def test_revoking_a_verifier_tombstones_the_mandatory_obligation(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    """The single most important test in this file.

    If the obligation vanished along with its revision, every resolution it
    used to block would silently start succeeding. It must remain, pointing
    at no revision, so a matching request still blocks until an approved
    successor satisfies it again.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    artifact_id, revision_id, directive_id = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="verifier compromised")

    row = await _row(
        factory,
        "SELECT obligation_state, current_revision_id, applicability_snapshot "
        "FROM arc_mandatory_obligations WHERE directive_id = :did",
        {"did": directive_id},
    )
    assert row.obligation_state == OBLIGATION_MISSING_INVALID
    assert row.current_revision_id is None
    # Tombstoned, not deleted: the snapshot survives so a matching
    # resolution still has something to block on.
    assert row.applicability_snapshot["task_kinds"] == ["deployment"]


@pytest.mark.asyncio
async def test_revoking_a_verifier_revokes_an_exception_it_approved(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    _artifact_id, revision_id, directive_id = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    exception_id, _evidence_id = await _exception_with_evidence(
        factory,
        tenant_id=seed.tenant_id,
        verifier_id=verifier_id,
        higher_directive_id=directive_id,
        higher_revision_id=revision_id,
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="verifier compromised")

    revoked_at = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approved_exceptions WHERE exception_id = :eid",
        {"eid": exception_id},
    )
    assert revoked_at is not None


@pytest.mark.asyncio
async def test_operator_signed_evidence_is_cascaded_through_signer_key_id(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    """The easiest half of the cascade to miss.

    `arc_approval_evidence` carries two different columns that can each
    point at a verifier -- `approval_verifier_id` for verifier-attested
    evidence, `signer_key_id` for operator-signed. A query that only
    followed one of them would leave every approval made through the other
    standing on withdrawn trust.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="operator_public_key")
    artifact_id, revision_id, _directive_id = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="signer_key_id",
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="key compromised")

    state = await _scalar(
        factory, "SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid", {"rid": revision_id}
    )
    assert state == "revoked"


@pytest.mark.asyncio
async def test_revoking_a_verifier_twice_preserves_the_first_timestamp(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    first_at = ARC_NOW
    later_at = ARC_NOW + datetime.timedelta(hours=1)

    await _service(factory, now=first_at).revoke_verifier(_ctx(seed), verifier_id, reason="first")
    # Must not raise, and must not move the timestamp later.
    await _service(factory, now=later_at).revoke_verifier(_ctx(seed), verifier_id, reason="second")

    revoked_at = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approval_verifiers WHERE approval_verifier_id = :vid",
        {"vid": verifier_id},
    )
    assert revoked_at == first_at


@pytest.mark.asyncio
async def test_a_revision_under_a_different_verifier_is_untouched(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    """The cascade is scoped to what the revoked verifier vouched for, not a
    sweep over every revision."""
    revoked_verifier = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    other_verifier = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")

    r_artifact, r_revision, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=r_artifact,
        revision_id=r_revision,
        verifier_id=revoked_verifier,
        via="approval_verifier_id",
    )
    o_artifact, o_revision, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=o_artifact,
        revision_id=o_revision,
        verifier_id=other_verifier,
        via="approval_verifier_id",
    )

    await service.revoke_verifier(_ctx(seed), revoked_verifier, reason="one verifier only")

    untouched_state = await _scalar(
        factory, "SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid", {"rid": o_revision}
    )
    assert untouched_state == "active"
    still_trusted = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approval_verifiers WHERE approval_verifier_id = :vid",
        {"vid": other_verifier},
    )
    assert still_trusted is None


@pytest.mark.asyncio
async def test_the_cascade_crosses_tenants(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    """A global verifier's blast radius is not bounded by one tenant, so
    neither is revoking it."""
    tenant_b = await _second_tenant(factory)
    verifier_id = await _verifier(factory, tenant_id=None, kind="trusted_attestation_provider")

    a_artifact, a_revision, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=a_artifact,
        revision_id=a_revision,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )
    b_artifact, b_revision, _ = await _revision_with_obligation(factory, tenant_id=tenant_b)
    await _activation_evidence(
        factory,
        tenant_id=tenant_b,
        artifact_id=b_artifact,
        revision_id=b_revision,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="cross-tenant verifier compromised")

    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT revision_id, lifecycle_state FROM arc_revisions " "WHERE revision_id = ANY(:rids)"),
                {"rids": [a_revision, b_revision]},
            )
        ).all()
    states = {row.revision_id: row.lifecycle_state for row in rows}
    assert states[a_revision] == "revoked"
    assert states[b_revision] == "revoked"


@pytest.mark.asyncio
async def test_the_service_requires_no_particular_role(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    """`_require_global_operator` in the admin router is the only gate on
    this operation. A service that also demanded a role would be redoing a
    decision the router already made -- and would be wrong to make it, since
    every role here is tenant-scoped and this action is not."""
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")

    await service.revoke_verifier(_ctx(seed, roles=[]), verifier_id, reason="no role needed")

    revoked_at = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approval_verifiers WHERE approval_verifier_id = :vid",
        {"vid": verifier_id},
    )
    assert revoked_at is not None


@pytest.mark.asyncio
async def test_revoking_an_unknown_verifier_is_reported_as_not_found(
    service: ApprovalTrustService, seed: ArcSeed
) -> None:
    with pytest.raises(NotFoundError):
        await service.revoke_verifier(_ctx(seed), "no-such-verifier", reason="does not exist")


@pytest.mark.asyncio
async def test_revoking_a_verifier_emits_an_audit_row_with_counts(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    artifact_id, revision_id, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_verifier(_ctx(seed), verifier_id, reason="audited revocation")

    row = await _row(
        factory,
        "SELECT event_type, event_payload FROM arc_audit_outbox "
        "WHERE event_payload ->> 'approval_verifier_id' = :vid",
        {"vid": verifier_id},
    )
    assert row.event_type == actions.ARC_APPROVAL_VERIFIER_REVOKED
    assert row.event_payload["reason"] == "audited revocation"
    assert row.event_payload["evidence_revoked_count"] == 1
    assert row.event_payload["revisions_revoked_count"] == 1


# --- revoke_evidence: narrower, verifier stays trusted ------------------------------


@pytest.mark.asyncio
async def test_revoke_evidence_revokes_only_its_own_dependents(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")

    target_artifact, target_revision, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    target_evidence = await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=target_artifact,
        revision_id=target_revision,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )
    other_artifact, other_revision, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=other_artifact,
        revision_id=other_revision,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_evidence(_ctx(seed), target_evidence, reason="this one approval was wrong")

    target_state = await _scalar(
        factory, "SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid", {"rid": target_revision}
    )
    other_state = await _scalar(
        factory, "SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid", {"rid": other_revision}
    )
    assert target_state == "revoked"
    assert other_state == "active"


@pytest.mark.asyncio
async def test_revoke_evidence_leaves_the_verifier_trusted(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    artifact_id, revision_id, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    evidence_id = await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_evidence(_ctx(seed), evidence_id, reason="this one approval was wrong")

    revoked_at = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approval_verifiers WHERE approval_verifier_id = :vid",
        {"vid": verifier_id},
    )
    assert revoked_at is None


@pytest.mark.asyncio
async def test_revoking_evidence_twice_does_not_error(
    factory: async_sessionmaker[AsyncSession], service: ApprovalTrustService, seed: ArcSeed
) -> None:
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    artifact_id, revision_id, _ = await _revision_with_obligation(factory, tenant_id=seed.tenant_id)
    evidence_id = await _activation_evidence(
        factory,
        tenant_id=seed.tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        verifier_id=verifier_id,
        via="approval_verifier_id",
    )

    await service.revoke_evidence(_ctx(seed), evidence_id, reason="once")
    await service.revoke_evidence(_ctx(seed), evidence_id, reason="twice")

    revoked_at = await _scalar(
        factory,
        "SELECT revoked_at FROM arc_approval_evidence_revocations WHERE evidence_id = :eid",
        {"eid": evidence_id},
    )
    assert revoked_at == ARC_NOW


@pytest.mark.asyncio
async def test_revoking_unknown_evidence_is_reported_as_not_found(service: ApprovalTrustService, seed: ArcSeed) -> None:
    with pytest.raises(NotFoundError):
        await service.revoke_evidence(_ctx(seed), uuid.uuid4(), reason="does not exist")


# --- the set must not refill after the sweep ------------------------------------


def _artifacts(factory: async_sessionmaker[AsyncSession]) -> ArtifactService:
    return ArtifactService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(ARC_NOW),
    )


async def _draft_revision(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> uuid.UUID:
    """A second, still-draft revision of the seeded artifact.

    Draft because that is the only state approval evidence may be attached
    in, and the point of these tests is what happens on the way to active.
    """
    revision_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext"
                ") VALUES (:rid, :aid, :tid, 'test-system', :loc, :rev, :digest, 'draft', :efrom,"
                "          :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "          :retention, 'none', 'body')"
            ),
            {
                "rid": revision_id,
                "aid": seed.artifact_id,
                "tid": seed.tenant_id,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rev": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": ARC_NOW - datetime.timedelta(days=1),
                "review": ARC_NOW + datetime.timedelta(days=365),
                "retention": ARC_NOW + datetime.timedelta(days=730),
            },
        )
    return revision_id


async def _unattached_evidence(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, *, revision_id: uuid.UUID, verifier_id: str
) -> uuid.UUID:
    """Activation evidence for a revision, not yet linked to it."""
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
                "          'ops@example.com', 'approver', :ts, 'verifier_attested', :vid,"
                "          CAST(:att AS JSONB), 'idp', 'audit://a')"
            ),
            {
                "eid": evidence_id,
                "tid": seed.tenant_id,
                "aid": seed.artifact_id,
                "rid": revision_id,
                "digest": "d" * 64,
                "ts": ARC_NOW,
                "vid": verifier_id,
                "att": json.dumps({"ok": True}),
            },
        )
    return evidence_id


async def _unattached_exception_evidence(
    factory: async_sessionmaker[AsyncSession],
    seed: ArcSeed,
    *,
    revision_id: uuid.UUID,
    exception_id: uuid.UUID,
    verifier_id: str,
) -> uuid.UUID:
    """`exception_approval` evidence naming both `exception_id` and
    `revision_id`, not yet attached.

    `exception_id` must already exist -- `arc_approval_evidence`'s
    `approved_exception_id` is a real (deferred) foreign key into
    `arc_approved_exceptions`, unlike `approved_artifact_id`/
    `approved_revision_id`'s equivalents, so `_exception_with_evidence` is
    what a caller uses to get one; this is a second, independent evidence
    row naming that same exception, not the one `_exception_with_evidence`
    itself returns.

    `exception_approval` is the one `evidence_type` `ATTACHABLE_EVIDENCE_TYPES`
    lets through `attach_approval_evidence`'s type check, which is what makes
    the trust check right after it -- `assert_evidence_is_trusted` -- the
    guard actually reachable on this path today. `ExceptionService`'s own
    writer never populates `approved_revision_id` on a row of this type (see
    `tests/unit/test_arc_evidence_bypass_removed.py`'s
    `TestTheOneAttachableTypeStillCannotActivate`, which pins that omission
    as load-bearing), so a row shaped exactly the way production writes one
    would be refused one check earlier, at `_assert_evidence_approves`, and
    never reach the trust check at all. This helper populates it anyway,
    because the trust check does not care how the column came to match --
    only whether the evidence it is given still holds -- and a test that
    could only build rows failing a step earlier could never tell the trust
    check apart from a step that runs before it.
    """
    evidence_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_exception_id,"
                "  approved_revision_id, approved_payload_digest, approving_principal, approving_role,"
                "  approval_timestamp, verification_method, approval_verifier_id, verifier_attestation,"
                "  verifier_identity, audit_log_reference"
                ") VALUES (:eid, 'exception_approval', 'tenant', :tid, :xid, :rid, :digest,"
                "          'ops@example.com', 'approver', :ts, 'verifier_attested', :vid,"
                "          CAST(:att AS JSONB), 'idp', 'audit://a')"
            ),
            {
                "eid": evidence_id,
                "tid": seed.tenant_id,
                "xid": exception_id,
                "rid": revision_id,
                "digest": "d" * 64,
                "ts": ARC_NOW,
                "vid": verifier_id,
                "att": json.dumps({"ok": True}),
            },
        )
    return evidence_id


async def _bind_evidence(
    factory: async_sessionmaker[AsyncSession], *, revision_id: uuid.UUID, evidence_id: uuid.UUID
) -> None:
    """Set `approval_evidence_id` directly rather than through
    `attach_approval_evidence`.

    `attach_approval_evidence` refuses every `evidence_type` this
    deployment has no first-party writer for, which today is every
    `artifact_activation` row -- exactly the type these fixtures need to
    exercise `activate`'s own evidence and trust checks. This binds it the
    way a direct write would, matching `_activation_evidence`'s own
    docstring above about how it reproduces that call's effect.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET approval_evidence_id = :eid WHERE revision_id = :rid"),
            {"eid": evidence_id, "rid": revision_id},
        )


@pytest.mark.asyncio
async def test_attach_approval_evidence_refuses_artifact_activation_outright(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The write-side half of the restriction this cascade's read side
    depends on: `artifact_activation` evidence has no first-party writer,
    so `attach_approval_evidence` refuses it before ever checking the
    verifier's trust state -- live or revoked makes no difference, because
    the type check runs first.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    revision_id = await _draft_revision(factory, seed)
    evidence_id = await _unattached_evidence(factory, seed, revision_id=revision_id, verifier_id=verifier_id)

    with pytest.raises(EvidenceTypeNotWritableError):
        await _artifacts(factory).attach_approval_evidence(_ctx(seed, roles=["admin"]), revision_id, evidence_id)


@pytest.mark.asyncio
async def test_activation_is_refused_when_evidence_is_revoked_after_being_attached(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Checked at activation, on however the column was populated.

    Trust can be withdrawn in the window between linking evidence and putting
    the revision into force, and activation is the step that makes agents
    obey it. Bound directly rather than through `attach_approval_evidence`
    -- see `_bind_evidence` -- but `activate`'s own recheck does not care how
    the column was populated, only whether the evidence it names still holds.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    revision_id = await _draft_revision(factory, seed)
    evidence_id = await _unattached_evidence(factory, seed, revision_id=revision_id, verifier_id=verifier_id)
    await _bind_evidence(factory, revision_id=revision_id, evidence_id=evidence_id)
    artifacts = _artifacts(factory)
    writer = _ctx(seed, roles=["admin"])

    await _service(factory).revoke_evidence(_ctx(seed), evidence_id, reason="approved in error")

    with pytest.raises(ApprovalTrustWithdrawn, match="revoked"):
        await artifacts.activate(writer, revision_id)


async def _valid_exception_id(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, *, verifier_id: str
) -> uuid.UUID:
    """A real `arc_approved_exceptions` row, satisfying the deferred foreign
    key `_unattached_exception_evidence`'s `approved_exception_id` needs.

    Built from the same two fixture calls `test_revoking_a_verifier_revokes_an_exception_it_approved`
    uses to get one -- a higher-scope revision and directive for the
    exception to name, then `_exception_with_evidence` itself. That call's
    own evidence is discarded here: it cannot name the revision under test
    (only the higher-scope one it was built against), which is exactly why
    `_unattached_exception_evidence` mints a second, independent evidence
    row naming this exception instead of reusing that one.
    """
    _artifact_id, higher_revision_id, higher_directive_id = await _revision_with_obligation(
        factory, tenant_id=seed.tenant_id
    )
    exception_id, _discarded_evidence_id = await _exception_with_evidence(
        factory,
        tenant_id=seed.tenant_id,
        verifier_id=verifier_id,
        higher_directive_id=higher_directive_id,
        higher_revision_id=higher_revision_id,
    )
    return exception_id


@pytest.mark.asyncio
async def test_a_revision_cannot_attach_evidence_whose_verifier_was_revoked(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The attach-time half of `assert_evidence_is_trusted`.

    `exception_approval` is the one evidence_type `ATTACHABLE_EVIDENCE_TYPES`
    lets past the type check in `attach_approval_evidence` -- so once a
    verifier that vouched for one is revoked, the trust check right after
    that type check is the only thing left standing between an attach call
    and binding a revision to evidence nothing trusts anymore. Revoking the
    verifier while the evidence already exists means `revoke_verifier`'s own
    cascade reaches this exact evidence row, which is why the refusal
    asserted here is the evidence's own revocation rather than the
    verifier's.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    exception_id = await _valid_exception_id(factory, seed, verifier_id=verifier_id)
    revision_id = await _draft_revision(factory, seed)
    evidence_id = await _unattached_exception_evidence(
        factory, seed, revision_id=revision_id, exception_id=exception_id, verifier_id=verifier_id
    )

    await _service(factory).revoke_verifier(_ctx(seed), verifier_id, reason="verifier compromised")

    with pytest.raises(ApprovalTrustWithdrawn, match="has been revoked and can no longer approve anything"):
        await _artifacts(factory).attach_approval_evidence(_ctx(seed, roles=["admin"]), revision_id, evidence_id)


@pytest.mark.asyncio
async def test_evidence_minted_after_a_verifier_was_revoked_is_still_refused(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Closes the one-instant hole in the trust cascade.

    `revoke_verifier`'s cascade only reaches evidence that already exists at
    the moment it runs -- it sweeps what exists, it does not watch for what
    comes after. Evidence minted by the same verifier once it is already
    revoked was never in that sweep, so nothing about the verifier record
    that later evidence names has ever been touched by a cascade; only the
    attach-time trust check stands between it and a fresh revision. Without
    that check, withdrawing trust in a verifier would be effective for
    exactly the instant the cascade ran, and every attach after it would
    succeed as though nothing had happened.
    """
    verifier_id = await _verifier(factory, tenant_id=seed.tenant_id, kind="trusted_attestation_provider")
    await _service(factory).revoke_verifier(_ctx(seed), verifier_id, reason="verifier compromised")

    exception_id = await _valid_exception_id(factory, seed, verifier_id=verifier_id)
    revision_id = await _draft_revision(factory, seed)
    evidence_id = await _unattached_exception_evidence(
        factory, seed, revision_id=revision_id, exception_id=exception_id, verifier_id=verifier_id
    )

    with pytest.raises(ApprovalTrustWithdrawn, match="whose trust has been withdrawn"):
        await _artifacts(factory).attach_approval_evidence(_ctx(seed, roles=["admin"]), revision_id, evidence_id)
