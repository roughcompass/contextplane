"""Integration tests for D1 verifier enrollment, against a real Postgres.

What the unit suite (`tests/unit/test_arc_enrollment.py`) cannot prove with
in-memory fakes: that the migration's CHECK/UNIQUE constraints actually
refuse a violation at the database rather than only in a hand-drawn DDL
reading, and that the `consumed_at IS NULL` compare-and-swap really
serializes two concurrent completions of the same challenge into exactly
one winner rather than merely "the fake didn't notice a problem." Five
sibling tasks this phase (T06, T09, T11, T12, T33) each found a real bug
only by racing a lock against real Postgres -- a single-use guarantee that
has not been raced is not known to hold.
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

from registry.arc.service import enrollment as en
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.types import ArcRequestContext
from registry.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-enrollment")


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(seed: ArcSeed, *, subject: str = "operator") -> ArcRequestContext:
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["admin"], oidc_subject=subject)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


def _service(
    factory: async_sessionmaker[AsyncSession],
    *,
    clock: FakeClock | None = None,
    attestation_providers: dict[str, en.VerifierAttestationProvider] | None = None,
) -> en.EnrollmentService:
    return en.EnrollmentService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllowAll()),
        clock=clock or FakeClock(ARC_NOW),
        attestation_providers=attestation_providers,
    )


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    return base64.b64encode(private.sign(en._SIGNING_DOMAIN + canonical_bytes)).decode("ascii")


def _proof(signature_base64: str) -> en.DetachedSignatureProofInput:
    return en.DetachedSignatureProofInput(
        signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519, signature_base64=signature_base64
    )


async def _issue_exact_principal_challenge(
    service: en.EnrollmentService, seed: ArcSeed, public_key: bytes, **overrides: object
) -> en.IssuedChallenge:
    kwargs: dict[str, object] = {
        "binding_kind": en.BINDING_EXACT_PRINCIPAL,
        "principal_issuer": _ISSUER,
        "principal_subject": "approver-1",
        "provider_id": None,
        "provider_allowed_principal_issuer": None,
        "owning_scope": "global",
        "target_tenant_id": None,
        "evidence_types": ["exception_approval"],
        "signature_algorithm": en.SIGNATURE_ALGORITHM_ED25519,
        "public_key_base64": base64.b64encode(public_key).decode("ascii"),
        "valid_from": ARC_NOW - datetime.timedelta(days=1),
        "valid_to": ARC_NOW + datetime.timedelta(days=365),
    }
    kwargs.update(overrides)
    return await service.create_challenge(_ctx(seed), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Appendix B.3: the binding-shape CHECK on `arc_approval_verifiers`, proven
# against real Postgres for all four cases -- not read off the DDL.
# ---------------------------------------------------------------------------


async def _insert_verifier_row(session: AsyncSession, *, approval_verifier_id: str, **overrides: object) -> None:
    row: dict[str, object] = {
        "approval_verifier_id": approval_verifier_id,
        "verifier_kind": "operator_public_key",
        "allowed_evidence_types": ["exception_approval"],
        "scope_kind": "global",
        "scope_tenant_id": None,
        "algorithm": "Ed25519",
        "public_key": b"\x11" * 32,
        "provider_id": None,
        "valid_from": ARC_NOW,
        "valid_to": None,
        "created_at": ARC_NOW,
        "principal_binding_kind": None,
        "principal_issuer": None,
        "principal_subject": None,
        "provider_allowed_principal_issuer": None,
        "credential_fingerprint": None,
        "provider_configuration_digest": None,
    }
    row.update(overrides)
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifiers ("
            "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
            "  algorithm, public_key, provider_id, valid_from, valid_to, created_at,"
            "  principal_binding_kind, principal_issuer, principal_subject, provider_allowed_principal_issuer,"
            "  credential_fingerprint, provider_configuration_digest"
            ") VALUES ("
            "  :approval_verifier_id, :verifier_kind, CAST(:allowed_evidence_types AS TEXT[]), :scope_kind,"
            "  :scope_tenant_id, :algorithm, :public_key, :provider_id, :valid_from, :valid_to, :created_at,"
            "  :principal_binding_kind, :principal_issuer, :principal_subject, :provider_allowed_principal_issuer,"
            "  :credential_fingerprint, :provider_configuration_digest"
            ")"
        ),
        row,
    )


@pytest.mark.asyncio
async def test_binding_shape_exact_principal_is_accepted(factory: async_sessionmaker[AsyncSession]) -> None:
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier_row(
            session,
            approval_verifier_id=verifier_id,
            principal_binding_kind="exact_principal",
            principal_issuer=_ISSUER,
            principal_subject="approver-1",
            credential_fingerprint="1" * 64,
        )
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT principal_binding_kind, principal_issuer, principal_subject "
                    "FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"
                ),
                {"vid": verifier_id},
            )
        ).one()
    assert row.principal_binding_kind == "exact_principal"
    assert row.principal_issuer == _ISSUER
    assert row.principal_subject == "approver-1"


@pytest.mark.asyncio
async def test_binding_shape_provider_delegated_is_accepted(factory: async_sessionmaker[AsyncSession]) -> None:
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier_row(
            session,
            approval_verifier_id=verifier_id,
            verifier_kind="trusted_attestation_provider",
            algorithm=None,
            public_key=None,
            provider_id="idp-1",
            principal_binding_kind="provider_delegated",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            provider_configuration_digest="2" * 64,
        )
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT principal_binding_kind, principal_issuer, principal_subject, "
                    "       provider_allowed_principal_issuer "
                    "FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"
                ),
                {"vid": verifier_id},
            )
        ).one()
    assert row.principal_binding_kind == "provider_delegated"
    assert row.principal_issuer is None
    assert row.principal_subject is None
    assert row.provider_allowed_principal_issuer == "https://idp.upstream.example/"


@pytest.mark.asyncio
async def test_binding_shape_empty_case_is_refused(factory: async_sessionmaker[AsyncSession]) -> None:
    """`binding_kind` declared but neither shape's required fields present.

    A CHECK that only rejects this case would pass a one-sided test while
    still permitting the hybrid case below -- both are proven here.
    """
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="ck_arc_approval_verifiers_binding_shape"):
            await _insert_verifier_row(
                session,
                approval_verifier_id=str(uuid.uuid4()),
                principal_binding_kind="exact_principal",
                principal_issuer=None,
                principal_subject=None,
            )


@pytest.mark.asyncio
async def test_binding_shape_hybrid_case_is_refused(factory: async_sessionmaker[AsyncSession]) -> None:
    """Both an exact principal and a provider issuer set at once."""
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="ck_arc_approval_verifiers_binding_shape"):
            await _insert_verifier_row(
                session,
                approval_verifier_id=str(uuid.uuid4()),
                principal_binding_kind="exact_principal",
                principal_issuer=_ISSUER,
                principal_subject="approver-1",
                provider_allowed_principal_issuer="https://idp.upstream.example/",
                credential_fingerprint="3" * 64,
            )


@pytest.mark.asyncio
async def test_a_legacy_non_principal_bound_row_is_still_accepted(factory: async_sessionmaker[AsyncSession]) -> None:
    """`principal_binding_kind IS NULL` escapes the shape CHECK entirely --
    the pre-existing `VerifierRegistry.register()` writer must keep
    working unchanged for `exception_approval` verifiers."""
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier_row(session, approval_verifier_id=verifier_id)
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT principal_binding_kind FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"),
                {"vid": verifier_id},
            )
        ).one()
    assert row.principal_binding_kind is None


@pytest.mark.asyncio
async def test_the_principal_credential_unique_constraint_refuses_a_duplicate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """UNIQUE (principal_issuer, principal_subject, credential_fingerprint)."""
    fingerprint = "4" * 64
    async with factory() as session, session.begin():
        await _insert_verifier_row(
            session,
            approval_verifier_id=str(uuid.uuid4()),
            principal_binding_kind="exact_principal",
            principal_issuer=_ISSUER,
            principal_subject="dup-subject",
            credential_fingerprint=fingerprint,
        )
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="uq_arc_approval_verifiers_principal"):
            await _insert_verifier_row(
                session,
                approval_verifier_id=str(uuid.uuid4()),
                principal_binding_kind="exact_principal",
                principal_issuer=_ISSUER,
                principal_subject="dup-subject",
                credential_fingerprint=fingerprint,
            )


# ---------------------------------------------------------------------------
# UNIQUE (nonce) on the challenge table -- replay refused at the database.
# ---------------------------------------------------------------------------


async def _insert_challenge_row(session: AsyncSession, *, nonce: str, **overrides: object) -> None:
    row: dict[str, object] = {
        "enrollment_challenge_id": uuid.uuid4(),
        "verifier_id": str(uuid.uuid4()),
        "nonce": nonce,
        "binding_kind": "exact_principal",
        "principal_issuer": _ISSUER,
        "principal_subject": "approver-1",
        "provider_id": None,
        "provider_allowed_principal_issuer": None,
        "owning_scope": "global",
        "target_tenant_id": None,
        "allowed_evidence_types": ["exception_approval"],
        "signature_algorithm": "Ed25519",
        "credential_material": b"\x11" * 32,
        "canonical_enrollment_bytes": b"{}",
        "valid_from": ARC_NOW,
        "valid_to": ARC_NOW + datetime.timedelta(days=365),
        "issued_at": ARC_NOW,
        "expires_at": ARC_NOW + en.CHALLENGE_TTL,
        "created_by_issuer": _ISSUER,
        "created_by_subject": "operator",
        "created_at": ARC_NOW,
    }
    row.update(overrides)
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifier_enrollment_challenges ("
            "  enrollment_challenge_id, verifier_id, nonce, binding_kind, principal_issuer, principal_subject,"
            "  provider_id, provider_allowed_principal_issuer, owning_scope, target_tenant_id,"
            "  allowed_evidence_types, signature_algorithm, credential_material, canonical_enrollment_bytes,"
            "  valid_from, valid_to, issued_at, expires_at, created_by_issuer, created_by_subject, created_at"
            ") VALUES ("
            "  :enrollment_challenge_id, :verifier_id, :nonce, :binding_kind, :principal_issuer, :principal_subject,"
            "  :provider_id, :provider_allowed_principal_issuer, :owning_scope, :target_tenant_id,"
            "  CAST(:allowed_evidence_types AS TEXT[]), :signature_algorithm, :credential_material,"
            "  :canonical_enrollment_bytes, :valid_from, :valid_to, :issued_at, :expires_at,"
            "  :created_by_issuer, :created_by_subject, :created_at"
            ")"
        ),
        row,
    )


@pytest.mark.asyncio
async def test_a_duplicate_nonce_is_refused_by_the_database(factory: async_sessionmaker[AsyncSession]) -> None:
    nonce = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await _insert_challenge_row(session, nonce=nonce)
    async with factory() as session, session.begin():
        with pytest.raises(IntegrityError, match="uq_arc_verifier_enrollment_challenges_nonce"):
            await _insert_challenge_row(session, nonce=nonce)


# ---------------------------------------------------------------------------
# End-to-end enrollment through the real service, against real Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_exact_principal_enrollment_round_trip(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    service = _service(factory)
    private, public = _keypair()
    issued = await _issue_exact_principal_challenge(service, seed, public)

    row = await service.register_verifier(
        _ctx(seed),
        enrollment_challenge_id=issued.enrollment_challenge_id,
        proof=_proof(_sign(private, issued.canonical_enrollment_bytes)),
    )

    assert row.enrollment_challenge_id == issued.enrollment_challenge_id
    assert row.principal_binding_kind == en.BINDING_EXACT_PRINCIPAL
    assert row.principal_issuer == _ISSUER
    assert row.principal_subject == "approver-1"

    # The challenge is now consumed -- read back directly, bypassing the service.
    async with factory() as session:
        consumed_at = (
            await session.execute(
                text(
                    "SELECT consumed_at FROM arc_approval_verifier_enrollment_challenges "
                    "WHERE enrollment_challenge_id = :cid"
                ),
                {"cid": issued.enrollment_challenge_id},
            )
        ).scalar_one()
    assert consumed_at is not None

    # And an audit row exists for the successful registration.
    async with factory() as session:
        payload = (
            await session.execute(
                text(
                    "SELECT event_payload FROM arc_audit_outbox "
                    "WHERE event_type = 'arc.approval_verifier.registered' "
                    "AND event_payload ->> 'enrollment_challenge_id' = :cid"
                ),
                {"cid": str(issued.enrollment_challenge_id)},
            )
        ).scalar_one()
    assert payload["approval_verifier_id"] == row.approval_verifier_id
    assert "credential_fingerprint" in payload


@pytest.mark.asyncio
async def test_a_wrong_key_signature_is_refused_end_to_end(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    service = _service(factory)
    _genuine, public = _keypair()
    wrong, _ = _keypair()
    issued = await _issue_exact_principal_challenge(service, seed, public)

    with pytest.raises(en.EnrollmentVerificationFailed, match="signature"):
        await service.register_verifier(
            _ctx(seed),
            enrollment_challenge_id=issued.enrollment_challenge_id,
            proof=_proof(_sign(wrong, issued.canonical_enrollment_bytes)),
        )

    # Nothing was written: no verifier row, and the challenge is still live.
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_approval_verifiers WHERE enrollment_challenge_id = :cid"),
                {"cid": issued.enrollment_challenge_id},
            )
        ).scalar_one()
        consumed_at = (
            await session.execute(
                text(
                    "SELECT consumed_at FROM arc_approval_verifier_enrollment_challenges "
                    "WHERE enrollment_challenge_id = :cid"
                ),
                {"cid": issued.enrollment_challenge_id},
            )
        ).scalar_one()
    assert count == 0
    assert consumed_at is None


@pytest.mark.asyncio
async def test_a_sequential_replay_is_refused(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    service = _service(factory)
    private, public = _keypair()
    issued = await _issue_exact_principal_challenge(service, seed, public)
    signature = _sign(private, issued.canonical_enrollment_bytes)

    await service.register_verifier(
        _ctx(seed), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
    )
    with pytest.raises(en.EnrollmentChallengeRequired, match="already consumed"):
        await service.register_verifier(
            _ctx(seed), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
        )


@pytest.mark.asyncio
async def test_concurrent_completions_of_one_challenge_produce_exactly_one_winner(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Race the single-use compare-and-swap with `asyncio.gather` against
    real Postgres. Every attempt presents the identical, genuinely valid
    signature -- isolating the single-use property from signature
    validity: only one may win, and it must be because it was first, not
    because the others were forged."""
    service = _service(factory)
    private, public = _keypair()
    issued = await _issue_exact_principal_challenge(service, seed, public)
    signature = _sign(private, issued.canonical_enrollment_bytes)

    async def _attempt() -> str:
        try:
            row = await service.register_verifier(
                _ctx(seed), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
            )
        except en.EnrollmentChallengeRequired:
            return "lost"
        else:
            assert row.enrollment_challenge_id == issued.enrollment_challenge_id
            return "won"

    concurrency = 20
    outcomes = await asyncio.gather(*(_attempt() for _ in range(concurrency)))

    assert outcomes.count("won") == 1, f"expected exactly one winner, got {outcomes.count('won')}: {outcomes}"
    assert outcomes.count("lost") == concurrency - 1

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_approval_verifiers WHERE enrollment_challenge_id = :cid"),
                {"cid": issued.enrollment_challenge_id},
            )
        ).scalar_one()
    assert count == 1, "exactly one verifier row must exist even after twenty concurrent completions"


@pytest.mark.asyncio
async def test_expiry_refuses_exactly_at_the_deadline_and_accepts_one_second_before(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    clock = FakeClock(ARC_NOW)
    service = _service(factory, clock=clock)
    private, public = _keypair()
    issued = await _issue_exact_principal_challenge(service, seed, public)
    signature = _sign(private, issued.canonical_enrollment_bytes)

    clock.set(issued.expires_at)
    with pytest.raises(en.EnrollmentChallengeRequired, match="expired"):
        await service.register_verifier(
            _ctx(seed), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
        )

    clock.set(issued.expires_at - datetime.timedelta(seconds=1))
    row = await service.register_verifier(
        _ctx(seed), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
    )
    assert row.enrollment_verified_at == issued.expires_at - datetime.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_a_configured_provider_completes_a_provider_delegated_enrollment(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    def _provider(*, canonical_enrollment: bytes, assertion_format: str, assertion_base64: str) -> bool:
        return assertion_format == "jwt" and assertion_base64 == "dHJ1c3RlZA=="

    service = _service(factory, attestation_providers={"idp-1": _provider})
    _, public = _keypair()
    issued = await service.create_challenge(
        _ctx(seed),
        binding_kind=en.BINDING_PROVIDER_DELEGATED,
        principal_issuer=None,
        principal_subject=None,
        provider_id="idp-1",
        provider_allowed_principal_issuer="https://idp.upstream.example/",
        owning_scope="global",
        target_tenant_id=None,
        evidence_types=["exception_approval"],
        signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
        public_key_base64=base64.b64encode(public).decode("ascii"),
        valid_from=ARC_NOW - datetime.timedelta(days=1),
        valid_to=ARC_NOW + datetime.timedelta(days=365),
    )
    row = await service.register_verifier(
        _ctx(seed),
        enrollment_challenge_id=issued.enrollment_challenge_id,
        proof=en.AttestationProofInput(provider_id="idp-1", assertion_format="jwt", assertion_base64="dHJ1c3RlZA=="),
    )
    assert row.principal_binding_kind == en.BINDING_PROVIDER_DELEGATED
    assert row.provider_configuration_digest is not None


@pytest.mark.asyncio
async def test_provider_delegated_refuses_with_no_provider_configured_on_this_deployment(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Every deployment today: `attestation_providers` defaults to empty
    (see `EnrollmentService`'s module docstring)."""
    service = _service(factory)
    _, public = _keypair()
    issued = await service.create_challenge(
        _ctx(seed),
        binding_kind=en.BINDING_PROVIDER_DELEGATED,
        principal_issuer=None,
        principal_subject=None,
        provider_id="idp-1",
        provider_allowed_principal_issuer="https://idp.upstream.example/",
        owning_scope="global",
        target_tenant_id=None,
        evidence_types=["exception_approval"],
        signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
        public_key_base64=base64.b64encode(public).decode("ascii"),
        valid_from=ARC_NOW - datetime.timedelta(days=1),
        valid_to=ARC_NOW + datetime.timedelta(days=365),
    )
    with pytest.raises(en.EnrollmentVerificationFailed, match="no in-process attestation provider"):
        await service.register_verifier(
            _ctx(seed),
            enrollment_challenge_id=issued.enrollment_challenge_id,
            proof=en.AttestationProofInput(provider_id="idp-1", assertion_format="jwt", assertion_base64="ZmFrZQ=="),
        )
