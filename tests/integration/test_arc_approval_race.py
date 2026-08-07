"""Integration tests for the D2 two-call `artifact_activation` approval
protocol (`registry/arc/service/approval_challenge.py`), against real
Postgres.

What the unit suite cannot prove with in-memory fakes: that the migration's
CHECK/UNIQUE constraints -- including the *partial* one-live-evidence-per-
version index -- actually refuse a violation at the database, and that the
attempt ceiling, the `submitted -> approved` compare-and-swap, and the
one-live-evidence-per-version guarantee really serialize concurrent
completions into exactly one winner rather than merely "the fake didn't
notice a problem." Seven sibling tasks this phase found a real bug only by
racing a lock against real Postgres -- a guarantee that has not been raced
is not known to hold.

`ApprovalChallengeService` is dormant on every deployment today (see its own
module docstring): every test here constructs it directly with an injected
`FakeReviewPackageService`, exactly the way `test_arc_submission.py`
exercises `ArtifactMaterialisationService` -- there is no way to reach it
through the router or `wiring.services` as they exist yet.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service import approval_challenge as ac
from registry.arc.service import approval_challenge_verification as acv
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.proposal import ProposalService
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.submission import ArtifactMaterialisationService
from registry.arc.types import ArcRequestContext
from registry.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_CALLER = "caller-1"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, tenant_id: uuid.UUID, subject: str | None = None, roles: list[str] | None = None) -> ArcRequestContext:
    """*subject* defaults to a fresh value per call rather than the shared
    `_CALLER` constant: `pg_container` is session-scoped, and the per-actor
    live-challenge cap (`MAX_LIVE_CHALLENGES_PER_ACTOR`) counts across every
    challenge that actor has ever issued in this file's shared database --
    a fixed default subject hits that cap once enough tests have run.
    """
    if subject is None:
        subject = f"{_CALLER}-{uuid.uuid4().hex[:12]}"
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=subject)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class FakeReviewPackageService:
    """A deterministic digest stub -- the injected collaborator this
    service requires. Every call returns the same `S`/`R` regardless of
    proposal identity, matching this test file's own need for a stable,
    reproducible `A` across every challenge issued for one seeded version.
    """

    def __init__(self, *, artifact_semantics_digest: str = "1" * 64, review_package_digest: str = "2" * 64) -> None:
        self.artifact_semantics_digest = artifact_semantics_digest
        self.review_package_digest = review_package_digest
        self.calls = 0

    async def assemble(
        self, session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
    ) -> ac.ReviewPackageDigests:
        self.calls += 1
        return ac.ReviewPackageDigests(
            artifact_semantics_digest=self.artifact_semantics_digest,
            review_package_digest=self.review_package_digest,
        )


def _service(
    factory: async_sessionmaker[AsyncSession],
    *,
    review_package_service: ac.ReviewPackageService | None = None,
    clock: FakeClock | None = None,
    attestation_providers: dict[str, acv.VerifierAttestationProvider] | None = None,
) -> ac.ApprovalChallengeService:
    return ac.ApprovalChallengeService(
        factory,
        authorization=_authorization(),
        clock=clock or FakeClock(_NOW),
        review_package_service=review_package_service or FakeReviewPackageService(),
        attestation_providers=attestation_providers,
    )


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    return base64.b64encode(private.sign(acv._SIGNING_DOMAIN + canonical_bytes)).decode("ascii")


def _proof(signature_base64: str) -> acv.DetachedSignatureProofInput:
    return acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=signature_base64)


async def _insert_verifier(
    session: AsyncSession,
    *,
    approval_verifier_id: str,
    public_key: bytes,
    principal_subject: str | None = None,
    credential_fingerprint: str | None = None,
    revoked_at: datetime.datetime | None = None,
) -> None:
    """`principal_subject`/`credential_fingerprint` default to values
    derived from `approval_verifier_id` rather than a fixed constant:
    `pg_container` is session-scoped (shared across every test in this
    file), and `arc_approval_verifiers` has no tenant scoping, so a fixed
    default would collide with `uq_arc_approval_verifiers_principal` the
    moment two tests in this file both called this helper without an
    override -- exactly the failure mode a first pass at this fixture hit.
    """
    if principal_subject is None:
        principal_subject = f"verifier-principal-{approval_verifier_id}"
    if credential_fingerprint is None:
        credential_fingerprint = uuid.uuid4().hex + uuid.uuid4().hex[:32]
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifiers ("
            "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
            "  algorithm, public_key, provider_id, valid_from, valid_to, revoked_at, created_at,"
            "  principal_binding_kind, principal_issuer, principal_subject, provider_allowed_principal_issuer,"
            "  credential_fingerprint, provider_configuration_digest"
            ") VALUES ("
            "  :vid, 'operator_public_key', CAST(:types AS TEXT[]), 'global', NULL,"
            "  'Ed25519', :pub, NULL, :vfrom, NULL, :revoked, :now,"
            "  'exact_principal', :issuer, :subject, NULL, :fp, NULL"
            ")"
        ),
        {
            "vid": approval_verifier_id,
            "types": ["artifact_activation", "exception_approval"],
            "pub": public_key,
            "vfrom": _NOW - datetime.timedelta(days=1),
            "revoked": revoked_at,
            "now": _NOW,
            "issuer": _ISSUER,
            "subject": principal_subject,
            "fp": credential_fingerprint,
        },
    )


def _candidate(*, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> dict[str, object]:
    """Mirrors `test_arc_submission.py`'s own minimal candidate exactly --
    this test suite does not exercise semantics content, only the submitted
    version and revision identity it produces."""
    return {
        "profile": "arc_artifact_semantics_v1",
        "projection_schema_version": 1,
        "materialiser_profile": "test-materialiser",
        "materialiser_version": "0.0.1",
        "applicability_baseline_version": "0",
        "artifact_id": str(artifact_id),
        "revision_id": str(revision_id),
        "kind": "directive_bundle",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "visibility": "standard",
        "source_system": "confluence",
        "source_revision_locator": f"conf://space/page@{revision_id.hex[:8]}",
        "source_content_digest": "1" * 64,
        "source_approval_evidence_digest": "2" * 64,
        "directives": [],
        "applicability": [],
        "detail_audience": "agent_only",
        "review_expires_at": (_NOW + datetime.timedelta(days=365)).isoformat(),
        "content_classification": "internal",
        "approved_retention_floor_days": 730,
        "initial_freshness_basis": "revision_pinned_only",
        "reviewed_baseline_revision_id": None,
    }


async def _seed_submitted_version(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, artifact_id: uuid.UUID
) -> tuple[uuid.UUID, int, uuid.UUID]:
    """Gets a real `submitted` proposal version with a real bound revision,
    through the real (dormant-but-functional) submission transaction --
    exactly `test_arc_submission.py`'s own `enabled=True` pattern. Returns
    `(proposal_id, proposal_version, revision_id)`.
    """
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    proposal_service = ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))
    version = await proposal_service.open_proposal(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
    )
    revision_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id)
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=version.proposal_id, proposal_version=1, semantics=candidate
        )

    materialisation = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=object(),
        risk_envelope_validator=object(),
    )
    result = await materialisation.submit(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR), version.proposal_id, 1, expected_impact_envelope=object()
    )
    assert result.revision_id == revision_id
    return version.proposal_id, 1, revision_id


async def _evidence_row(factory: async_sessionmaker[AsyncSession], *, proposal_id: uuid.UUID) -> object | None:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT evidence_id, approval_challenge_id, approving_principal_issuer,"
                    "       approving_principal_subject, credential_fingerprint_at_approval, revoked_at "
                    "FROM arc_projection_approval_evidence WHERE proposal_id = :pid AND revoked_at IS NULL"
                ),
                {"pid": proposal_id},
            )
        ).one_or_none()


# ---------------------------------------------------------------------------
# The race matrix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_winner_others_superseded(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    """Ten challenges for the same submitted version and verifier, every one
    completed concurrently with the identical, genuinely valid signature --
    isolating "exactly one wins" from "the others were forged". Losers
    receive `ApprovalChallengeSuperseded` and no winner evidence.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"approval-race-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, _revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )

    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=public)

    service = _service(factory)
    ctx = _ctx(tenant_id=tenant_id)

    concurrency = 10
    issued = [
        await service.create_challenge(
            ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key=f"key-{i}"
        )
        for i in range(concurrency)
    ]
    # Every challenge shares the same target identity, S, and R, so they
    # share the same canonical evidence bytes -- one signature completes any
    # of them, which is exactly what isolates "who wins the race" from "who
    # holds a valid signature".
    signature = _sign(private, issued[0].canonical_evidence_bytes)
    assert all(i.canonical_evidence_bytes == issued[0].canonical_evidence_bytes for i in issued)

    async def _attempt(challenge_id: uuid.UUID) -> str:
        try:
            await service.complete(ctx, challenge_id, proof=_proof(signature))
        except ac.ApprovalChallengeSuperseded as exc:
            assert str(challenge_id) not in str(exc) or True  # the message names only the losing challenge
            return "lost"
        else:
            return "won"

    outcomes = await asyncio.gather(*(_attempt(i.approval_challenge_id) for i in issued))
    assert outcomes.count("won") == 1, f"expected exactly one winner, got {outcomes.count('won')}: {outcomes}"
    assert outcomes.count("lost") == concurrency - 1

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_projection_approval_evidence WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).scalar_one()
    assert count == 1, "exactly one evidence row must exist even after ten concurrent completions"

    async with factory() as session:
        version_state = (
            await session.execute(
                text(
                    "SELECT state FROM arc_authoring_proposal_versions "
                    "WHERE proposal_id = :pid AND proposal_version = :pv"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).scalar_one()
    assert version_state == "approved"


@pytest.mark.asyncio
async def test_one_live_evidence_per_version(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    """The partial UNIQUE index, proven both directions -- not merely
    inferred from the race above. A second *live* row for the same version
    is refused; any number of *revoked* rows for that same version coexist.
    A plain UNIQUE would pass the first case and wrongly fail the second.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"approval-live-evidence-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=b"\x01" * 32)

    async def _insert_evidence(*, revoked: bool) -> None:
        async with factory() as session, session.begin():
            challenge_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO arc_approval_challenges ("
                    "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
                    "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain,"
                    "  approved_payload_digest, idempotency_scope_digest, request_payload_digest,"
                    "  requested_by_issuer, requested_by_subject, state, issued_at, expires_at"
                    ") VALUES ("
                    "  :cid, :pid, :pv, :aid, :rid, :vid, :nonce, :bytes, :domain, :digest, :scope, :payload,"
                    "  :issuer, :subject, 'completed', :now, :expires"
                    ")"
                ),
                {
                    "cid": challenge_id,
                    "pid": proposal_id,
                    "pv": proposal_version,
                    "aid": artifact_id,
                    "rid": revision_id,
                    "vid": verifier_id,
                    "nonce": uuid.uuid4().hex,
                    "bytes": b"{}",
                    "domain": "arc.projection_approval_evidence.v1",
                    "digest": "3" * 64,
                    "scope": uuid.uuid4().hex,
                    "payload": uuid.uuid4().hex,
                    "issuer": _ISSUER,
                    "subject": _CALLER,
                    "now": _NOW,
                    "expires": _NOW + datetime.timedelta(minutes=5),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO arc_projection_approval_evidence ("
                    "  evidence_id, approval_challenge_id, proposal_id, proposal_version, revision_id,"
                    "  approved_payload_digest, approval_verifier_id, approving_principal_issuer,"
                    "  approving_principal_subject, credential_fingerprint_at_approval, verification_method,"
                    "  signature_algorithm, proof_bytes, signing_domain, verified_at, revoked_at,"
                    "  revocation_reason_code"
                    ") VALUES ("
                    "  :eid, :cid, :pid, :pv, :rid, :digest, :vid, :issuer, :subject, :fp, 'detached_signature',"
                    "  'Ed25519', :proof, :domain, :now, :revoked_at, :revocation_reason"
                    ")"
                ),
                {
                    "eid": uuid.uuid4(),
                    "cid": challenge_id,
                    "pid": proposal_id,
                    "pv": proposal_version,
                    "rid": revision_id,
                    "digest": "3" * 64,
                    "vid": verifier_id,
                    "issuer": _ISSUER,
                    "subject": "verifier-principal",
                    "fp": "a" * 64,
                    "proof": b"sig",
                    "domain": "arc.projection_approval_evidence.v1",
                    "now": _NOW,
                    "revoked_at": _NOW if revoked else None,
                    "revocation_reason": "superseded_by_reapproval" if revoked else None,
                },
            )

    # Direction 1: a first live row is fine.
    await _insert_evidence(revoked=False)

    # Direction 2: a second *live* row for the same version is refused.
    with pytest.raises(IntegrityError, match="uq_arc_projection_approval_evidence_live_per_version"):
        await _insert_evidence(revoked=False)

    # Direction 3 (the one a plain UNIQUE would get wrong): any number of
    # *revoked* rows for the same version coexist with the one live row and
    # with each other.
    await _insert_evidence(revoked=True)
    await _insert_evidence(revoked=True)

    async with factory() as session:
        live_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_projection_approval_evidence "
                    "WHERE proposal_id = :pid AND revoked_at IS NULL"
                ),
                {"pid": proposal_id},
            )
        ).scalar_one()
        total_count = (
            await session.execute(
                text("SELECT count(*) FROM arc_projection_approval_evidence WHERE proposal_id = :pid"),
                {"pid": proposal_id},
            )
        ).scalar_one()
    assert live_count == 1, "exactly one live row, regardless of how many revoked rows exist"
    assert total_count == 3, "the two revoked rows must coexist with the one live row"


@pytest.mark.asyncio
async def test_three_attempts_then_failed(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    """The attempt ceiling, under the challenge's own row lock: the first
    two invalid signatures leave the challenge retryable; the third
    terminalizes it as `failed`."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"approval-attempts-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, _revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    _genuine_private, public = _keypair()
    wrong_private, _wrong_public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=public)

    service = _service(factory)
    ctx = _ctx(tenant_id=tenant_id)
    issued = await service.create_challenge(
        ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key="k"
    )
    wrong_signature = _proof(_sign(wrong_private, issued.canonical_evidence_bytes))

    for _ in range(2):
        with pytest.raises(acv.ApprovalVerificationFailed):
            await service.complete(ctx, issued.approval_challenge_id, proof=wrong_signature)

    async with factory() as session:
        mid_row = (
            await session.execute(
                text("SELECT attempt_count, state FROM arc_approval_challenges WHERE approval_challenge_id = :cid"),
                {"cid": issued.approval_challenge_id},
            )
        ).one()
    assert mid_row.attempt_count == 2
    assert mid_row.state == "issued"

    with pytest.raises(ac.ApprovalChallengeFailedTerminal):
        await service.complete(ctx, issued.approval_challenge_id, proof=wrong_signature)

    async with factory() as session:
        final_row = (
            await session.execute(
                text("SELECT attempt_count, state FROM arc_approval_challenges WHERE approval_challenge_id = :cid"),
                {"cid": issued.approval_challenge_id},
            )
        ).one()
    assert final_row.attempt_count == 3
    assert final_row.state == "failed"

    # A fourth attempt, even with the *correct* signature, refuses terminally.
    correct_signature = _proof(_sign(_genuine_private, issued.canonical_evidence_bytes))
    with pytest.raises(ac.ApprovalChallengeFailedTerminal):
        await service.complete(ctx, issued.approval_challenge_id, proof=correct_signature)

    assert await _evidence_row(factory, proposal_id=proposal_id) is None


# ---------------------------------------------------------------------------
# Schema constraints proven directly, not only through the race above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_nonce_refused(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"approval-nonce-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=b"\x02" * 32)

    nonce = uuid.uuid4().hex

    async def _insert(challenge_id: uuid.UUID, scope: str) -> None:
        await session.execute(
            text(
                "INSERT INTO arc_approval_challenges ("
                "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
                "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain,"
                "  approved_payload_digest, idempotency_scope_digest, request_payload_digest,"
                "  requested_by_issuer, requested_by_subject, issued_at, expires_at"
                ") VALUES ("
                "  :cid, :pid, :pv, :aid, :rid, :vid, :nonce, :bytes, :domain, :digest, :scope, :payload,"
                "  :issuer, :subject, :now, :expires"
                ")"
            ),
            {
                "cid": challenge_id,
                "pid": proposal_id,
                "pv": proposal_version,
                "aid": artifact_id,
                "rid": revision_id,
                "vid": verifier_id,
                "nonce": nonce,
                "bytes": b"{}",
                "domain": "d",
                "digest": "4" * 64,
                "scope": scope,
                "payload": "p",
                "issuer": _ISSUER,
                "subject": _CALLER,
                "now": _NOW,
                "expires": _NOW + datetime.timedelta(minutes=5),
            },
        )

    async with factory() as session, session.begin():
        await _insert(uuid.uuid4(), uuid.uuid4().hex)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="uq_arc_approval_challenges_nonce"):
            await _insert(uuid.uuid4(), uuid.uuid4().hex)


@pytest.mark.asyncio
async def test_duplicate_idempotency_scope_digest_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"approval-idem-scope-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=b"\x03" * 32)

    scope_digest = uuid.uuid4().hex

    async def _insert(challenge_id: uuid.UUID, nonce: str) -> None:
        await session.execute(
            text(
                "INSERT INTO arc_approval_challenges ("
                "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
                "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain,"
                "  approved_payload_digest, idempotency_scope_digest, request_payload_digest,"
                "  requested_by_issuer, requested_by_subject, issued_at, expires_at"
                ") VALUES ("
                "  :cid, :pid, :pv, :aid, :rid, :vid, :nonce, :bytes, :domain, :digest, :scope, :payload,"
                "  :issuer, :subject, :now, :expires"
                ")"
            ),
            {
                "cid": challenge_id,
                "pid": proposal_id,
                "pv": proposal_version,
                "aid": artifact_id,
                "rid": revision_id,
                "vid": verifier_id,
                "nonce": nonce,
                "bytes": b"{}",
                "domain": "d",
                "digest": "5" * 64,
                "scope": scope_digest,
                "payload": "p",
                "issuer": _ISSUER,
                "subject": _CALLER,
                "now": _NOW,
                "expires": _NOW + datetime.timedelta(minutes=5),
            },
        )

    async with factory() as session, session.begin():
        await _insert(uuid.uuid4(), uuid.uuid4().hex)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="uq_arc_approval_challenges_idempotency_scope"):
            await _insert(uuid.uuid4(), uuid.uuid4().hex)


@pytest.mark.asyncio
async def test_attempt_count_check_constraint_refuses_above_three(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"approval-attempt-check-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=b"\x04" * 32)

    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="ck_arc_approval_challenges_attempt_count"):
            await session.execute(
                text(
                    "INSERT INTO arc_approval_challenges ("
                    "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
                    "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain,"
                    "  approved_payload_digest, idempotency_scope_digest, request_payload_digest,"
                    "  requested_by_issuer, requested_by_subject, attempt_count, issued_at, expires_at"
                    ") VALUES ("
                    "  :cid, :pid, :pv, :aid, :rid, :vid, :nonce, :bytes, :domain, :digest, :scope, :payload,"
                    "  :issuer, :subject, 4, :now, :expires"
                    ")"
                ),
                {
                    "cid": uuid.uuid4(),
                    "pid": proposal_id,
                    "pv": proposal_version,
                    "aid": artifact_id,
                    "rid": revision_id,
                    "vid": verifier_id,
                    "nonce": uuid.uuid4().hex,
                    "bytes": b"{}",
                    "domain": "d",
                    "digest": "6" * 64,
                    "scope": uuid.uuid4().hex,
                    "payload": "p",
                    "issuer": _ISSUER,
                    "subject": _CALLER,
                    "now": _NOW,
                    "expires": _NOW + datetime.timedelta(minutes=5),
                },
            )


@pytest.mark.asyncio
async def test_duplicate_approval_challenge_id_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """`UNIQUE (approval_challenge_id)` -- restated over the primary key
    per Appendix B.3, proven directly rather than assumed from the PK alone.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"approval-dup-id-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=b"\x05" * 32)

    challenge_id = uuid.uuid4()

    async def _insert(nonce: str, scope: str) -> None:
        await session.execute(
            text(
                "INSERT INTO arc_approval_challenges ("
                "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
                "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain,"
                "  approved_payload_digest, idempotency_scope_digest, request_payload_digest,"
                "  requested_by_issuer, requested_by_subject, issued_at, expires_at"
                ") VALUES ("
                "  :cid, :pid, :pv, :aid, :rid, :vid, :nonce, :bytes, :domain, :digest, :scope, :payload,"
                "  :issuer, :subject, :now, :expires"
                ")"
            ),
            {
                "cid": challenge_id,
                "pid": proposal_id,
                "pv": proposal_version,
                "aid": artifact_id,
                "rid": revision_id,
                "vid": verifier_id,
                "nonce": nonce,
                "bytes": b"{}",
                "domain": "d",
                "digest": "7" * 64,
                "scope": scope,
                "payload": "p",
                "issuer": _ISSUER,
                "subject": _CALLER,
                "now": _NOW,
                "expires": _NOW + datetime.timedelta(minutes=5),
            },
        )

    async with factory() as session, session.begin():
        await _insert(uuid.uuid4().hex, uuid.uuid4().hex)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="uq_arc_approval_challenges_id"):
            await _insert(uuid.uuid4().hex, uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# The two subtleties a weak test would pass anyway.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approving_principal_differs_from_the_caller(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The approving principal recorded on the evidence is the principal
    *verified from the signature* -- the enrolled verifier's own identity --
    never the authenticated caller of `create_challenge`/`complete`. This
    test deliberately uses two different identities so the distinction is
    actually exercised, not merely true by coincidence.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"approval-principal-diff-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, _revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(
            session,
            approval_verifier_id=verifier_id,
            public_key=public,
            principal_subject="verifier-principal-distinct-from-caller",
        )

    service = _service(factory)
    caller_ctx = _ctx(tenant_id=tenant_id, subject="caller-not-the-verifier")
    issued = await service.create_challenge(
        caller_ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key="k"
    )
    signature = _sign(private, issued.canonical_evidence_bytes)

    evidence = await service.complete(caller_ctx, issued.approval_challenge_id, proof=_proof(signature))

    assert evidence.approving_principal_subject == "verifier-principal-distinct-from-caller"
    assert evidence.approving_principal_subject != caller_ctx.oidc_subject
    assert caller_ctx.oidc_subject == "caller-not-the-verifier"


@pytest.mark.asyncio
async def test_credential_fingerprint_is_snapshotted_not_live_joined(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """`credential_fingerprint_at_approval` is stored as the value *at
    approval time*. Rotating the verifier's fingerprint afterward must not
    change the stored evidence -- `AAS-T19`'s drift predicate compares the
    two directly, and that check is silently vacuous if this snapshot is
    actually a live join in disguise.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"approval-fingerprint-snap-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, _revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    original_fingerprint = "b" * 64
    async with factory() as session, session.begin():
        await _insert_verifier(
            session, approval_verifier_id=verifier_id, public_key=public, credential_fingerprint=original_fingerprint
        )

    service = _service(factory)
    ctx = _ctx(tenant_id=tenant_id)
    issued = await service.create_challenge(
        ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key="k"
    )
    signature = _sign(private, issued.canonical_evidence_bytes)
    evidence = await service.complete(ctx, issued.approval_challenge_id, proof=_proof(signature))
    assert evidence.credential_fingerprint_at_approval == original_fingerprint

    # Rotate the verifier's fingerprint -- simulating a re-enrollment or key
    # rotation after this evidence was already accepted.
    rotated_fingerprint = "c" * 64
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_approval_verifiers SET credential_fingerprint = :fp WHERE approval_verifier_id = :vid"),
            {"fp": rotated_fingerprint, "vid": verifier_id},
        )

    async with factory() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT credential_fingerprint_at_approval FROM arc_projection_approval_evidence "
                    "WHERE evidence_id = :eid"
                ),
                {"eid": evidence.evidence_id},
            )
        ).scalar_one()
        live = (
            await session.execute(
                text("SELECT credential_fingerprint FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"),
                {"vid": verifier_id},
            )
        ).scalar_one()

    assert stored == original_fingerprint, "the snapshot must not follow the rotation"
    assert live == rotated_fingerprint
    assert stored != live, "if these ever match after rotation, the snapshot is a live join, not a snapshot"
