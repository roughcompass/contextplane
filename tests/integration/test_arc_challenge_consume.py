"""Challenge validation and single-use consumption.

Validation and consumption are deliberately two steps. `validate_challenge`
runs the single-use, binding, and freshness checks and locks the row with
`FOR UPDATE`; `consume_challenge` only ever sets `consumed_at`, and does so
without committing, so a caller can compose it into the same transaction as
receipt creation. That split is what lets the two stay atomic with each other
once a receipt service exists to call them, without this task needing one.

A migration-level deferred constraint trigger enforces that atomicity at the
database itself: `consumed_at IS NOT NULL` iff exactly one `arc_receipts` row
references the challenge, checked at COMMIT rather than per statement. So a
transaction that consumes a challenge without also inserting its receipt
cannot commit -- which means these tests insert a minimal, schema-valid stub
receipt row wherever they commit a consumption, deliberately standing in
for `ReceiptService`'s own row so this suite tests only the database
constraint, not receipt-construction logic.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.challenge import (
    CHALLENGE_TTL,
    ChallengeConsumptionError,
    ChallengeNonceDeriver,
    ChallengeService,
    ChallengeValidationError,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_HOST = "host-1"
_SESSION = "session-1"
_CLAIMS_DIGEST = "a" * 64


def _deriver() -> ChallengeNonceDeriver:
    return ChallengeNonceDeriver({"nk1": b"secret-one"}, active_key_id="nk1")


def _ctx(tenant_id: uuid.UUID) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject="agent-host-1")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id=_HOST)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"arc-consume-{tid.hex[:8]}", "now": datetime.datetime.now(tz=datetime.UTC)},
        )
    return tid


@pytest_asyncio.fixture
async def actor_id(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> uuid.UUID:
    """A real `actors` row -- the stub receipts these tests insert need a
    valid FK target, even though challenge validation itself never touches
    actor identity."""
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, :dn, :oidc, :now)"
            ),
            {
                "aid": aid,
                "tid": tenant_id,
                "dn": "agent-host-1",
                "oidc": "agent-host-1",
                "now": datetime.datetime.now(tz=datetime.UTC),
            },
        )
    return aid


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> ChallengeService:
    return ChallengeService(factory, _deriver(), clock)


async def _issue(service: ChallengeService, tenant_id: uuid.UUID, *, idempotency_key: str = "key-1"):
    return await service.issue_challenge(
        _ctx(tenant_id), session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key=idempotency_key
    )


async def _insert_stub_receipt(
    session: AsyncSession,
    *,
    challenge_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """A minimal, schema-valid `arc_receipts` row.

    Real receipt construction is `ReceiptService`'s job; this exists only to
    satisfy the deferred challenge-consumption trigger, which requires
    exactly one receipt referencing a challenge before a transaction that
    consumes it can commit -- the same requirement `ReceiptService` meets
    for real.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    await session.execute(
        text(
            "INSERT INTO arc_receipts ("
            "  receipt_id, challenge_id, tenant_id, actor_id, host_id, session_id,"
            "  manifest_fingerprint, attestation_id, resolution_status,"
            "  selection_engine_version, registry_build_revision, canonical_profile_versions,"
            "  selection_config_digest, evaluated_at, freshness_basis, budget_limit_bytes,"
            "  response_replay_ciphertext, response_replay_nonce, response_replay_key_id"
            ") VALUES ("
            "  :receipt_id, :challenge_id, :tenant_id, :actor_id, :host_id, :session_id,"
            "  :fingerprint, :attestation_id, 'ready',"
            "  'test-engine', 'test-build', '{}',"
            "  :config_digest, :evaluated_at, 'revision_pinned_only', 12288,"
            "  :ciphertext, :nonce, 'test-key'"
            ")"
        ),
        {
            "receipt_id": uuid.uuid4(),
            "challenge_id": challenge_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "host_id": _HOST,
            "session_id": _SESSION,
            "fingerprint": "f" * 64,
            "attestation_id": f"att-{challenge_id}",
            "config_digest": "c" * 64,
            "evaluated_at": now,
            "ciphertext": b"stub-ciphertext",
            "nonce": b"stub-nonce-12",
        },
    )


@pytest.mark.asyncio
async def test_a_freshly_issued_challenge_validates(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    issued = await _issue(service, tenant_id)

    async with factory() as session, session.begin():
        validated = await service.validate_challenge(
            session,
            tenant_id=tenant_id,
            host_id=_HOST,
            session_id=_SESSION,
            manifest_claims_digest=_CLAIMS_DIGEST,
            arc_nonce=issued.arc_nonce,
        )

    assert validated.challenge_id == issued.challenge_id
    assert validated.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_validate_and_consume_compose_in_one_transaction(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Simulates what a future receipt-creation caller does: validate, insert
    the receipt, then consume -- all before one commit, mirroring the order
    the resolution flow itself uses (receipt insert, then challenge update)."""
    issued = await _issue(service, tenant_id)

    async with factory() as session, session.begin():
        validated = await service.validate_challenge(
            session,
            tenant_id=tenant_id,
            host_id=_HOST,
            session_id=_SESSION,
            manifest_claims_digest=_CLAIMS_DIGEST,
            arc_nonce=issued.arc_nonce,
        )
        await _insert_stub_receipt(session, challenge_id=validated.challenge_id, tenant_id=tenant_id, actor_id=actor_id)
        await service.consume_challenge(session, validated.challenge_id)

    async with factory() as session:
        consumed_at = (
            await session.execute(
                text("SELECT consumed_at FROM arc_context_challenges WHERE challenge_id = :cid"),
                {"cid": issued.challenge_id},
            )
        ).scalar_one()
    assert consumed_at is not None


@pytest.mark.asyncio
async def test_consuming_twice_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    issued = await _issue(service, tenant_id)
    async with factory() as session, session.begin():
        validated = await service.validate_challenge(
            session,
            tenant_id=tenant_id,
            host_id=_HOST,
            session_id=_SESSION,
            manifest_claims_digest=_CLAIMS_DIGEST,
            arc_nonce=issued.arc_nonce,
        )
        await _insert_stub_receipt(session, challenge_id=validated.challenge_id, tenant_id=tenant_id, actor_id=actor_id)
        await service.consume_challenge(session, validated.challenge_id)

    with pytest.raises(ChallengeValidationError, match="already consumed"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )


@pytest.mark.asyncio
async def test_wrong_nonce_does_not_validate(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    await _issue(service, tenant_id)

    with pytest.raises(ChallengeValidationError, match="no challenge matches"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=b"\x00" * 32,
            )


@pytest.mark.asyncio
async def test_host_mismatch_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    issued = await _issue(service, tenant_id)

    with pytest.raises(ChallengeValidationError, match="different host"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id="a-different-host",
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )


@pytest.mark.asyncio
async def test_session_mismatch_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    issued = await _issue(service, tenant_id)

    with pytest.raises(ChallengeValidationError, match="different session"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id="a-different-session",
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )


@pytest.mark.asyncio
async def test_claims_digest_mismatch_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    issued = await _issue(service, tenant_id)

    with pytest.raises(ChallengeValidationError, match="different manifest claims digest"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id=_SESSION,
                manifest_claims_digest="b" * 64,
                arc_nonce=issued.arc_nonce,
            )


@pytest.mark.asyncio
async def test_expired_challenge_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    issued = await _issue(service, tenant_id)
    clock.tick(CHALLENGE_TTL + datetime.timedelta(seconds=1))

    with pytest.raises(ChallengeValidationError, match="expired"):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )


@pytest.mark.asyncio
async def test_a_failed_validation_leaves_the_challenge_unconsumed_and_still_valid(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID
) -> None:
    """A rejected attempt must not corrupt or half-consume the row -- proven by
    a subsequent correct attempt still succeeding, which is also the closest
    this task can come to proving "no receipt" without a receipt service."""
    issued = await _issue(service, tenant_id)

    with pytest.raises(ChallengeValidationError):
        async with factory() as session, session.begin():
            await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id="wrong-host",
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )

    async with factory() as session, session.begin():
        validated = await service.validate_challenge(
            session,
            tenant_id=tenant_id,
            host_id=_HOST,
            session_id=_SESSION,
            manifest_claims_digest=_CLAIMS_DIGEST,
            arc_nonce=issued.arc_nonce,
        )
    assert validated.challenge_id == issued.challenge_id


@pytest.mark.asyncio
async def test_consume_without_prior_validation_requires_exactly_one_row(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService
) -> None:
    with pytest.raises(ChallengeConsumptionError, match="affected 0"):
        async with factory() as session, session.begin():
            await service.consume_challenge(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_concurrent_consumption_attempts_yield_exactly_one_winner(
    factory: async_sessionmaker[AsyncSession], service: ChallengeService, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Two transactions racing to consume the same challenge. The `FOR UPDATE`
    lock in `validate_challenge` serializes them: the second to run only sees
    the row after the first commits its consumption, and by then
    `consumed_at` is already set."""
    issued = await _issue(service, tenant_id)

    async def _attempt() -> bool:
        async with factory() as session, session.begin():
            validated = await service.validate_challenge(
                session,
                tenant_id=tenant_id,
                host_id=_HOST,
                session_id=_SESSION,
                manifest_claims_digest=_CLAIMS_DIGEST,
                arc_nonce=issued.arc_nonce,
            )
            await _insert_stub_receipt(
                session, challenge_id=validated.challenge_id, tenant_id=tenant_id, actor_id=actor_id
            )
            await service.consume_challenge(session, validated.challenge_id)
        return True

    results = await asyncio.gather(_attempt(), _attempt(), return_exceptions=True)
    successes = [r for r in results if r is True]
    failures = [r for r in results if isinstance(r, ChallengeValidationError)]

    assert len(successes) == 1
    assert len(failures) == 1
