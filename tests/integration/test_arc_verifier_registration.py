"""Admitting an approval verifier — ARC's trust root for approvals.

Every check in `ApprovalEvidenceVerifier` reads a row from this table, and
until registration existed nothing in the product could create one. The chain
was not weak, it was unreachable: activation fell through to checks a direct
SQL INSERT satisfied as readily as it satisfied `lifecycle_state = 'active'`.

What these tests pin is mostly *refusal*. A verifier that can be stored but not
used is worse than one rejected, because it fails later, at an approval, and
looks like the approver's problem rather than a bad registration.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.verifier_registry import (
    EVIDENCE_TYPES,
    KIND_OPERATOR_KEY,
    KIND_PROVIDER,
    EvidenceRevocationRegistry,
    VerifierRegistry,
)
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, ValidationError
from registry.types import FakeClock, TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc

_KEY = b"\x11" * 32


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-verifier-reg")


@pytest.fixture
def registry(factory: async_sessionmaker[AsyncSession]) -> VerifierRegistry:
    return VerifierRegistry(factory, clock=FakeClock(ARC_NOW))


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["admin"], oidc_subject="ops@example.com"
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"})


def _vid() -> str:
    return f"v-{uuid.uuid4().hex[:12]}"


async def _register(registry: VerifierRegistry, seed: ArcSeed, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "approval_verifier_id": _vid(),
        "verifier_kind": KIND_OPERATOR_KEY,
        "allowed_evidence_types": frozenset({"artifact_activation"}),
        "scope_kind": "global",
        "algorithm": "Ed25519",
        "public_key": _KEY,
    }
    kwargs.update(overrides)
    return await registry.register(_ctx(seed), **kwargs)  # type: ignore[arg-type]


# --- the happy path, and what it records ----------------------------------------


@pytest.mark.asyncio
async def test_a_registered_verifier_is_readable_by_the_verification_path(
    factory: async_sessionmaker[AsyncSession], registry: VerifierRegistry, seed: ArcSeed
) -> None:
    """Registration and lookup are one table's contract, so they are tested
    together: a row nothing can read back is not a trust root."""
    verifier_id = await _register(registry, seed)

    async with factory() as session, session.begin():
        record = await registry.get(session, verifier_id)

    assert record is not None
    assert record.verifier_kind == KIND_OPERATOR_KEY
    assert record.public_key == _KEY
    assert record.allowed_evidence_types == frozenset({"artifact_activation"})
    assert record.usable_at(ARC_NOW)


@pytest.mark.asyncio
async def test_registration_audits_fingerprints_and_never_the_credential(
    factory: async_sessionmaker[AsyncSession], registry: VerifierRegistry, seed: ArcSeed
) -> None:
    """An audit trail enumerating keys or provider ids would be a directory of
    what to attack. Fingerprints prove two registrations used the same
    credential without recording either."""
    verifier_id = await _register(registry, seed, allowlist_fingerprint="a" * 64)

    async with factory() as session:
        payload = (
            await session.execute(
                text(
                    "SELECT event_payload FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'approval_verifier_id' = :vid"
                ),
                {"vid": verifier_id},
            )
        ).scalar_one()

    assert payload["credential_fingerprint"] != _KEY.hex()
    assert len(payload["credential_fingerprint"]) == 64
    assert payload["allowlist_fingerprint"] == "a" * 64
    assert "public_key" not in payload


@pytest.mark.asyncio
async def test_an_unknown_verifier_reads_back_as_none(
    factory: async_sessionmaker[AsyncSession], registry: VerifierRegistry
) -> None:
    async with factory() as session, session.begin():
        assert await registry.get(session, "v-never-registered") is None


# --- refusals -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verifier_id_is_never_rebound(registry: VerifierRegistry, seed: ArcSeed) -> None:
    """A verifier id names a trust root. Rebinding it to a different key would
    silently re-point every approval that ever cited it, including ones
    already relied upon."""
    verifier_id = await _register(registry, seed)

    with pytest.raises(ConflictError, match="already registered"):
        await _register(registry, seed, approval_verifier_id=verifier_id, public_key=b"\x22" * 32)


@pytest.mark.asyncio
async def test_a_verifier_permitted_for_every_evidence_type_is_refused(
    registry: VerifierRegistry, seed: ArcSeed
) -> None:
    """The one refusal here that is policy rather than shape.

    The permitted-types check and the evidence-target closure both exist to
    stop one kind of approval being presented as another. A verifier trusted
    for all four defeats both, on the day it is created -- which is exactly
    when a bootstrap is most tempting to wave through.
    """
    with pytest.raises(ValidationError, match="every evidence type"):
        await _register(registry, seed, allowed_evidence_types=EVIDENCE_TYPES)


@pytest.mark.asyncio
async def test_a_verifier_permitted_for_nothing_is_refused(registry: VerifierRegistry, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="never approve"):
        await _register(registry, seed, allowed_evidence_types=frozenset())


@pytest.mark.asyncio
async def test_an_unsupported_algorithm_is_refused_at_registration(registry: VerifierRegistry, seed: ArcSeed) -> None:
    """Refused here rather than at first use. A verifier carrying an algorithm
    nothing can verify would register cleanly and then fail every approval,
    which reads as a broken approver rather than a bad registration."""
    with pytest.raises(ValidationError, match="unsupported signature algorithm"):
        await _register(registry, seed, algorithm="RSA-2048")


@pytest.mark.asyncio
async def test_a_wrong_length_public_key_is_refused(registry: VerifierRegistry, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="32 bytes"):
        await _register(registry, seed, public_key=b"\x11" * 31)


@pytest.mark.asyncio
async def test_a_verifier_carrying_both_representations_is_refused(registry: VerifierRegistry, seed: ArcSeed) -> None:
    """One representation, matching the declared kind. A verifier carrying
    both could be validated down whichever path was weaker."""
    with pytest.raises(ValidationError, match="must not also name a provider"):
        await _register(registry, seed, provider_id="idp-1")


@pytest.mark.asyncio
async def test_a_provider_verifier_must_not_carry_a_key(registry: VerifierRegistry, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="must not also carry a key"):
        await _register(registry, seed, verifier_kind=KIND_PROVIDER, provider_id="idp-1", algorithm="Ed25519")


@pytest.mark.asyncio
async def test_a_tenant_scoped_verifier_must_name_its_tenant(registry: VerifierRegistry, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="must name its tenant"):
        await _register(registry, seed, scope_kind="tenant")


@pytest.mark.asyncio
async def test_a_global_verifier_must_not_name_a_tenant(registry: VerifierRegistry, seed: ArcSeed) -> None:
    """The asymmetry that let a tenant-scoped verifier reach global evidence
    through a NULL comparison. Refused at both ends now."""
    with pytest.raises(ValidationError, match="must not name a tenant"):
        await _register(registry, seed, scope_kind="global", scope_tenant_id=seed.tenant_id)


@pytest.mark.asyncio
async def test_an_already_expired_verifier_is_refused(registry: VerifierRegistry, seed: ArcSeed) -> None:
    with pytest.raises(ValidationError, match="already ended"):
        await _register(registry, seed, valid_to=ARC_NOW - datetime.timedelta(days=1))


# --- evidence revocation reads ---------------------------------------------------


@pytest.mark.asyncio
async def test_unrevoked_evidence_reads_back_as_none(factory: async_sessionmaker[AsyncSession]) -> None:
    """Absence means not revoked. Reported as None rather than raising, because
    the overwhelmingly common case is evidence that is perfectly fine."""
    async with factory() as session, session.begin():
        assert await EvidenceRevocationRegistry().get(session, uuid.uuid4()) is None
