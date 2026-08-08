"""ChallengeService: issuance under the `(host, session, idempotency key)` identity.

The identity is permanent. The database's unique index on
`(tenant_id, host_id, session_id, idempotency_key_digest)` never expires, so once
a row exists for a key, issuance can only ever resume it -- never mint a second
row alongside it. That is what makes retry safe: a caller that resends the exact
same request after a timeout cannot end up with two challenges, and a caller that
reuses a key for a different request is told so rather than silently served
someone else's challenge.
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

from registry.arc.service.challenge import (
    CHALLENGE_TTL,
    ChallengeNonceDeriver,
    ChallengeService,
    idempotency_key_digest,
    nonce_digest,
)
from registry.arc.types import ArcRequestContext
from registry.audit import actions
from registry.exceptions import ConflictError
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_HOST = "host-1"
_SESSION = "session-1"
_CLAIMS_DIGEST = "a" * 64
_OTHER_CLAIMS_DIGEST = "b" * 64


def _deriver(active: str = "nk1") -> ChallengeNonceDeriver:
    return ChallengeNonceDeriver({"nk1": b"secret-one", "nk0": b"secret-zero"}, active_key_id=active)


def _ctx(tenant_id: uuid.UUID, *, host_id: str | None = _HOST) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject="agent-host-1")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id=host_id)


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
            {"tid": tid, "slug": f"arc-challenge-{tid.hex[:8]}", "now": datetime.datetime.now(tz=datetime.UTC)},
        )
    return tid


async def _outbox_events_for_challenge(
    factory: async_sessionmaker[AsyncSession],
    challenge_id: uuid.UUID,
    *,
    event_type: str = actions.ARC_CHALLENGE_ISSUED,
) -> list:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT tenant_id, event_payload FROM arc_audit_outbox "
                    "WHERE event_type = :etype AND event_payload ->> 'challenge_id' = :cid"
                ),
                {"etype": event_type, "cid": str(challenge_id)},
            )
        ).all()


@pytest.mark.asyncio
async def test_issuing_a_new_challenge_emits_one_arc_challenge_issued_event(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    issued = await service.issue_challenge(
        _ctx(tenant_id), session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-issued"
    )

    rows = await _outbox_events_for_challenge(factory, issued.challenge_id)

    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_id
    assert rows[0].event_payload["host_id"] == _HOST
    assert rows[0].event_payload["session_id"] == _SESSION
    assert rows[0].event_payload["issued_at"] == issued.issued_at.isoformat()
    assert rows[0].event_payload["expires_at"] == issued.expires_at.isoformat()


@pytest.mark.asyncio
async def test_a_resumed_retry_does_not_emit_a_second_issued_event(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    first = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-resume"
    )

    clock.tick(datetime.timedelta(seconds=1))
    retry = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-resume"
    )

    assert retry.challenge_id == first.challenge_id
    rows = await _outbox_events_for_challenge(factory, first.challenge_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_issuing_a_new_challenge_persists_only_the_nonce_digest(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    issued = await service.issue_challenge(
        _ctx(tenant_id), session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    assert len(issued.arc_nonce) == 32
    assert issued.issued_at == clock.now()
    assert issued.expires_at == clock.now() + CHALLENGE_TTL
    assert issued.manifest_claims_digest == _CLAIMS_DIGEST

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT arc_nonce_digest, nonce_derivation_key_id, host_id, session_id, consumed_at "
                    "FROM arc_context_challenges WHERE challenge_id = :cid"
                ),
                {"cid": issued.challenge_id},
            )
        ).one()

    assert row.arc_nonce_digest == nonce_digest(issued.arc_nonce)
    assert row.arc_nonce_digest != issued.arc_nonce.hex()
    assert row.nonce_derivation_key_id == "nk1"
    assert row.host_id == _HOST
    assert row.session_id == _SESSION
    assert row.consumed_at is None


@pytest.mark.asyncio
async def test_exact_retry_returns_the_original_challenge(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    first = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    clock.tick(datetime.timedelta(seconds=1))
    retry = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    assert retry.challenge_id == first.challenge_id
    assert retry.arc_nonce == first.arc_nonce
    assert retry.issued_at == first.issued_at
    assert retry.expires_at == first.expires_at


@pytest.mark.asyncio
async def test_retry_with_a_different_claims_digest_is_a_conflict(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    with pytest.raises(ConflictError):
        await service.issue_challenge(
            ctx, session_id=_SESSION, manifest_claims_digest=_OTHER_CLAIMS_DIGEST, idempotency_key="key-1"
        )


@pytest.mark.asyncio
async def test_retry_after_expiry_returns_the_same_now_expired_challenge(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    """Issuance never mints a second row for a used key, even once the first has
    expired -- the unique index has no expiry of its own. Whether an expired
    challenge can still be used is the validation path's question, not this one's.
    """
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    first = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    clock.tick(CHALLENGE_TTL + datetime.timedelta(seconds=1))
    retried = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    assert retried.challenge_id == first.challenge_id
    assert retried.expires_at == first.expires_at
    assert retried.expires_at < clock.now()


@pytest.mark.asyncio
async def test_retry_after_key_rotation_still_reproduces_the_original_nonce(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    """A rotation between issuance and retry must not change the returned nonce.

    The retry re-derives under whichever key the row was originally issued
    under, not whatever is currently active.
    """
    ctx = _ctx(tenant_id)
    original = ChallengeService(factory, _deriver(active="nk1"), clock)
    first = await original.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    rotated = ChallengeService(factory, _deriver(active="nk0"), clock)
    retry = await rotated.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
    )

    assert retry.challenge_id == first.challenge_id
    assert retry.arc_nonce == first.arc_nonce


@pytest.mark.asyncio
async def test_different_idempotency_keys_are_independent_challenges(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    a = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-a"
    )
    b = await service.issue_challenge(
        ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-b"
    )

    assert a.challenge_id != b.challenge_id
    assert a.arc_nonce != b.arc_nonce


@pytest.mark.asyncio
async def test_different_sessions_are_independent_even_with_the_same_key(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)
    a = await service.issue_challenge(
        ctx, session_id="session-a", manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="shared-key"
    )
    b = await service.issue_challenge(
        ctx, session_id="session-b", manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="shared-key"
    )

    assert a.challenge_id != b.challenge_id


@pytest.mark.asyncio
async def test_issuance_without_an_authenticated_host_is_rejected(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id, host_id=None)

    with pytest.raises(ValueError, match="authenticated host"):
        await service.issue_challenge(
            ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="key-1"
        )


@pytest.mark.asyncio
async def test_concurrent_issuance_with_the_same_key_resolves_to_one_challenge(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, clock: FakeClock
) -> None:
    """Two requests racing on the same key must not both win an insert.

    The loser's `IntegrityError` is caught and resolved to the winner's row --
    proven here by both concurrent calls returning the same challenge and the
    table holding exactly one row for the key, not by inspecting the exception
    path directly.
    """
    service = ChallengeService(factory, _deriver(), clock)
    ctx = _ctx(tenant_id)

    async def _attempt():
        return await service.issue_challenge(
            ctx, session_id=_SESSION, manifest_claims_digest=_CLAIMS_DIGEST, idempotency_key="race-key"
        )

    first, second = await asyncio.gather(_attempt(), _attempt())

    assert first.challenge_id == second.challenge_id
    assert first.arc_nonce == second.arc_nonce

    async with factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_context_challenges "
                    "WHERE tenant_id = :tid AND session_id = :sid AND idempotency_key_digest = :kd"
                ),
                {"tid": tenant_id, "sid": _SESSION, "kd": idempotency_key_digest("race-key")},
            )
        ).scalar()
    assert count == 1

    # The loser's audit-outbox insert rolls back with the rest of its losing
    # transaction -- proven here rather than assumed, because that insert
    # runs inside the same `try` as the commit specifically so a race loses
    # both together.
    rows = await _outbox_events_for_challenge(factory, first.challenge_id)
    assert len(rows) == 1
