"""Integration tests for source admission, against a real Postgres.

What the unit suite (`tests/unit/test_arc_source_admission.py`) cannot
prove with a fake session: that the ADR 039 advisory lock genuinely
serializes two concurrent identical admissions into one row, that the
migrated schema's own constraints hold (unique scope digest, byte
ceilings, scope/tenant coherence), and that admission plus retrieval work
end to end through real transactions.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.source_admission import (
    ApprovalProof,
    ConnectorFetchAdmission,
    ConnectorRegistration,
    SourceAdmissionRefused,
    SourceAdmissionService,
    SourceIdempotencyConflict,
    UploadAdmission,
    UploadPolicyRegistration,
)
from registry.arc.types import ArcRequestContext
from registry.exceptions import NotFoundError
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx() -> ArcRequestContext:
    tenant = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=_OPERATOR)
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER})


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _claim(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "profile": "arc_source_approval_claim_v1",
        "source_system": "confluence",
        "source_revision_locator": "conf://space/page@3",
        "source_content_digest_algorithm": "sha256",
        "source_content_digest": "0" * 64,
        "source_content_type": "text/markdown",
        "approval_locator": "https://confluence.example/approvals/1",
        "approving_authority_issuer": _ISSUER,
        "approving_authority_subject": "owner",
        "approval_scope": "space:eng",
        "approved_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def _proof() -> ApprovalProof:
    return ApprovalProof(
        verification_method="detached_signature",
        signature_algorithm="Ed25519",
        signature_base64="c2lnbmF0dXJl",
    )


async def _bytes_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _service(factory: async_sessionmaker[AsyncSession], *, clock: FakeClock | None = None) -> SourceAdmissionService:
    authorization = ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))
    return SourceAdmissionService(factory, authorization=authorization, clock=clock or FakeClock(_NOW))


async def _register_policy(service: SourceAdmissionService, *, policy_id: str, max_bytes: int = 1024) -> None:
    await service.register_upload_policy(
        _ctx(),
        UploadPolicyRegistration(
            policy_id=policy_id,
            owning_scope="global",
            tenant_id=None,
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("verifier-1",),
            max_bytes=max_bytes,
        ),
    )


async def _register_connector(service: SourceAdmissionService, *, connector_id: str, host: str) -> None:
    await service.register_connector(
        _ctx(),
        ConnectorRegistration(
            connector_id=connector_id,
            owning_scope="global",
            tenant_id=None,
            allowed_schemes=("https",),
            allowed_hosts=(host,),
            allowed_media_types=("text/markdown",),
            allowed_verifier_ids=("verifier-1",),
            max_bytes=1024,
        ),
    )


# ---------------------------------------------------------------------------
# End-to-end admission + retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_admission_and_retrieval_round_trip(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    data = b"a real document body"
    claim = _claim(source_content_digest=_digest_of(data))
    ctx = _ctx()
    admission = UploadAdmission(
        policy_id=policy_id,
        source_system="confluence",
        source_revision_locator="conf://space/page@3",
        source_content_type="text/markdown",
        claim=claim,
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )

    evidence = await service.admit_upload(ctx, admission, _bytes_iter([data]))
    assert evidence.source_content_digest == _digest_of(data)
    assert evidence.status == "current"

    fetched = await service.get_evidence(ctx, evidence.source_evidence_id)
    assert fetched.source_evidence_id == evidence.source_evidence_id

    body, content_type = await service.get_body(ctx, evidence.source_evidence_id)
    assert body == data
    assert content_type == "text/markdown"

    # The persisted row never carries the proof's signature bytes or
    # anything the response model would leak.
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT signature FROM arc_source_approval_evidence WHERE source_evidence_id = :id"),
                {"id": evidence.source_evidence_id},
            )
        ).one()
    assert row.signature == "c2lnbmF0dXJl"  # stored, but never returned by get_evidence's response shape
    assert not hasattr(fetched, "signature")


@pytest.mark.asyncio
async def test_connector_fetch_admission_round_trip(factory: async_sessionmaker[AsyncSession]) -> None:
    connector_id = f"connector-{uuid.uuid4().hex[:8]}"
    service = _service(factory)
    await _register_connector(service, connector_id=connector_id, host="good.example")

    data = b"fetched via connector"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    service._http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )

    claim = _claim(source_content_digest=_digest_of(data), source_revision_locator="https://good.example/doc")
    ctx = _ctx()
    admission = ConnectorFetchAdmission(
        connector_id=connector_id,
        source_revision_locator="https://good.example/doc",
        claim=claim,
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )

    evidence = await service.admit_connector_fetch(ctx, admission)
    assert evidence.admission_method == "connector_fetch"
    assert evidence.connector_id == connector_id

    body, _content_type = await service.get_body(ctx, evidence.source_evidence_id)
    assert body == data


@pytest.mark.asyncio
async def test_unknown_evidence_is_not_found(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    with pytest.raises(NotFoundError):
        await service.get_evidence(_ctx(), uuid.uuid4())


# ---------------------------------------------------------------------------
# The hard byte ceiling, end to end, and the digest-mismatch refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exceeding_the_ceiling_leaves_no_row_behind(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id, max_bytes=8)

    claim = _claim(source_content_digest=_digest_of(b"way too much data for the ceiling"))
    admission = UploadAdmission(
        policy_id=policy_id,
        source_system="confluence",
        source_revision_locator="conf://space/page@3",
        source_content_type="text/markdown",
        claim=claim,
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )

    with pytest.raises(SourceAdmissionRefused, match="byte ceiling"):
        await service.admit_upload(_ctx(), admission, _bytes_iter([b"way too much data for the ceiling"]))

    # Scoped to this test's own policy, not a bare table count -- the
    # session-scoped test database is shared across every test in this
    # module, so an unscoped count would also see every other test's rows.
    async with factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_source_bodies b "
                    "JOIN arc_source_approval_evidence e ON e.source_evidence_id = b.source_evidence_id "
                    "WHERE e.policy_id = :policy_id"
                ),
                {"policy_id": policy_id},
            )
        ).scalar()
    assert count == 0, "a refused admission must leave no partial write behind"


@pytest.mark.asyncio
async def test_a_caller_supplied_digest_disagreeing_with_the_computed_one_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    claim = _claim(source_content_digest=_digest_of(b"the wrong bytes entirely"))
    admission = UploadAdmission(
        policy_id=policy_id,
        source_system="confluence",
        source_revision_locator="conf://space/page@3",
        source_content_type="text/markdown",
        claim=claim,
        verifier_id="verifier-1",
        proof=_proof(),
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )

    with pytest.raises(SourceAdmissionRefused, match="does not match"):
        await service.admit_upload(_ctx(), admission, _bytes_iter([b"the actual admitted bytes"]))

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_source_approval_evidence WHERE policy_id = :policy_id"),
                {"policy_id": policy_id},
            )
        ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# Idempotency: exact retry, changed conflict, and the concurrent race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_retry_returns_the_first_evidence(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    data = b"idempotent document"
    claim = _claim(source_content_digest=_digest_of(data))
    ctx = _ctx()
    key = f"key-{uuid.uuid4().hex[:8]}"

    def admission() -> UploadAdmission:
        return UploadAdmission(
            policy_id=policy_id,
            source_system="confluence",
            source_revision_locator="conf://space/page@3",
            source_content_type="text/markdown",
            claim=claim,
            verifier_id="verifier-1",
            proof=_proof(),
            idempotency_key=key,
        )

    first = await service.admit_upload(ctx, admission(), _bytes_iter([data]))
    second = await service.admit_upload(ctx, admission(), _bytes_iter([data]))
    assert first.source_evidence_id == second.source_evidence_id

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_source_approval_evidence WHERE idempotency_key_digest = :kd"),
                {"kd": hashlib.sha256(key.encode("utf-8")).hexdigest()},
            )
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_a_changed_retry_under_the_same_key_is_a_conflict(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    ctx = _ctx()
    key = f"key-{uuid.uuid4().hex[:8]}"
    data = b"first version of the document"
    claim = _claim(source_content_digest=_digest_of(data))
    await service.admit_upload(
        ctx,
        UploadAdmission(
            policy_id=policy_id,
            source_system="confluence",
            source_revision_locator="conf://space/page@3",
            source_content_type="text/markdown",
            claim=claim,
            verifier_id="verifier-1",
            proof=_proof(),
            idempotency_key=key,
        ),
        _bytes_iter([data]),
    )

    other_data = b"a completely different document body"
    other_claim = _claim(source_content_digest=_digest_of(other_data))
    with pytest.raises(SourceIdempotencyConflict):
        await service.admit_upload(
            ctx,
            UploadAdmission(
                policy_id=policy_id,
                source_system="confluence",
                source_revision_locator="conf://space/page@3",
                source_content_type="text/markdown",
                claim=other_claim,
                verifier_id="verifier-1",
                proof=_proof(),
                idempotency_key=key,
            ),
            _bytes_iter([other_data]),
        )


@pytest.mark.asyncio
async def test_two_concurrent_identical_admissions_resolve_to_one_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The proof this task calls for: a lock that isn't tested under real
    concurrency isn't known to hold. Two truly concurrent calls, each with
    its own connection, must not race each other into two rows."""
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    ctx = _ctx()
    key = f"race-key-{uuid.uuid4().hex[:8]}"
    data = b"the one document both callers admit at once"
    claim = _claim(source_content_digest=_digest_of(data))

    def admission() -> UploadAdmission:
        return UploadAdmission(
            policy_id=policy_id,
            source_system="confluence",
            source_revision_locator="conf://space/page@3",
            source_content_type="text/markdown",
            claim=claim,
            verifier_id="verifier-1",
            proof=_proof(),
            idempotency_key=key,
        )

    async def _attempt() -> object:
        return await service.admit_upload(ctx, admission(), _bytes_iter([data]))

    first, second = await asyncio.gather(_attempt(), _attempt())
    assert first.source_evidence_id == second.source_evidence_id  # type: ignore[attr-defined]

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_source_approval_evidence WHERE idempotency_key_digest = :kd"),
                {"kd": hashlib.sha256(key.encode("utf-8")).hexdigest()},
            )
        ).scalar()
    assert count == 1, "the advisory lock must serialize two concurrent identical admissions into one row"


# ---------------------------------------------------------------------------
# Verifier and media-type allowlist enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_outside_the_policy_allowlist_is_refused(factory: async_sessionmaker[AsyncSession]) -> None:
    service = _service(factory)
    policy_id = f"policy-{uuid.uuid4().hex[:8]}"
    await _register_policy(service, policy_id=policy_id)

    data = b"document"
    admission = UploadAdmission(
        policy_id=policy_id,
        source_system="confluence",
        source_revision_locator="conf://space/page@3",
        source_content_type="text/markdown",
        claim=_claim(source_content_digest=_digest_of(data)),
        verifier_id="not-a-registered-verifier",
        proof=_proof(),
        idempotency_key=f"key-{uuid.uuid4().hex[:8]}",
    )
    with pytest.raises(SourceAdmissionRefused, match="not permitted"):
        await service.admit_upload(_ctx(), admission, _bytes_iter([data]))
