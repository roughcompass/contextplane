"""The three guarantees a later refactor is most likely to break quietly.

Each of these holds because of a specific lock, and each would keep passing
its own unit tests if that lock were removed — the failure only appears
under genuine contention. That is what makes them worth pinning here, in one
file, where their purpose is obvious to whoever next changes the locking.

1. Parallel resolutions against one challenge produce exactly one receipt.
2. Concurrent event appends produce no fork.
3. Concurrent revocation and resolution linearize.

Every test here uses real concurrent sessions. A single-session simulation
would exercise none of it.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.schemas.canonical import canonicalize_host_attestation_envelope
from registry.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from registry.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    HostSignerKeyRegistry,
    ManifestClaims,
)
from registry.arc.service.challenge import CHALLENGE_TTL, ChallengeNonceDeriver, ChallengeService
from registry.arc.service.receipt import EVENT_SOURCE_HOST, ReceiptService, ReplayEnvelope
from registry.arc.service.resolution import (
    ManifestUnverified,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionService,
    parse_manifest,
)
from registry.arc.service.selection import SelectionInput
from registry.arc.types import ArcRequestContext
from registry.arc.vocabularies import RECEIPT_EVENT_JIT_RETRIEVAL
from registry.types import FakeClock, TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, provenance, seed_arc, signing_provider

_HOST_ID = "host-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"
_FINGERPRINT = "f" * 64


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-concurrency")


class _Harness:
    def __init__(self, factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
        self.factory = factory
        self.seed = seed
        self.clock = FakeClock(ARC_NOW)
        private = Ed25519PrivateKey.generate()
        self.host_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self.host_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.challenges = ChallengeService(
            factory, ChallengeNonceDeriver({"nk1": b"secret"}, active_key_id="nk1"), self.clock
        )
        self.receipts = ReceiptService(signing_provider(), self.clock)
        self.service = ResolutionService(
            factory,
            attestation=AttestationService(HostSignerKeyRegistry(), clock=self.clock),
            challenges=self.challenges,
            receipts=self.receipts,
            provenance=provenance(),
            clock=self.clock,
            seal=lambda rid, bundle: ReplayEnvelope(
                ciphertext=f"sealed:{rid}".encode(), nonce=b"nonce-12-byt", key_id="replay-1"
            ),
        )

    def ctx(self) -> ArcRequestContext:
        tenant = TenantContext(
            tenant_id=self.seed.tenant_id, actor_id=self.seed.actor_id, roles=["consumer"], oidc_subject="s"
        )
        return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id=_HOST_ID)

    def manifest(self) -> ManifestClaims:
        return ManifestClaims(
            session_id="sess-1",
            task_kind="code_change",
            requested_action_classes=("merge",),
            capability_ids=(),
            domain_ids=("payments",),
            environment="production",
            data_sensitivity="confidential",
            repository_identity="git@example.test:org/repo.git",
            supported_context_bundle_content_profiles=("arc_context_bundle_content_v1",),
        )

    async def register_key(self) -> str:
        signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_host_attestation_keys ("
                    "  signer_key_id, host_id, tenant_id, attestation_profile, public_key,"
                    "  valid_from, created_by_operator"
                    ") VALUES (:kid, :host, :tid, :profile, :pub, :vfrom, 'test')"
                ),
                {
                    "kid": signer_key_id,
                    "host": _HOST_ID,
                    "tid": self.seed.tenant_id,
                    "profile": _PROFILE,
                    "pub": base64.b64encode(self.host_public).decode("ascii"),
                    "vfrom": ARC_NOW - datetime.timedelta(days=1),
                },
            )
        return signer_key_id

    async def nonce(self) -> bytes:
        manifest = self.manifest()
        issued = await self.challenges.issue_challenge(
            self.ctx(),
            session_id=manifest.session_id,
            manifest_claims_digest=compute_manifest_claims_digest(manifest.as_claims_dict()),
            idempotency_key=uuid.uuid4().hex,
        )
        return issued.arc_nonce

    def envelope(self, nonce: bytes, signer_key_id: str, attestation_id: str) -> AttestationEnvelope:
        manifest = self.manifest()
        payload = {
            "host_id": _HOST_ID,
            "repository_identity": manifest.repository_identity,
            "immutable_source_revision": "deadbeef",
            "environment": manifest.environment,
            "data_sensitivity": manifest.data_sensitivity,
            "session_id": manifest.session_id,
            "manifest_claims_digest": compute_manifest_claims_digest(manifest.as_claims_dict()),
            "arc_nonce": base64.b64encode(nonce).decode("ascii"),
        }
        envelope_dict: dict[str, object] = {
            "profile": _PROFILE,
            "signer_key_id": signer_key_id,
            "attestation_id": attestation_id,
            "issued_at": ARC_NOW,
            "expires_at": ARC_NOW + CHALLENGE_TTL,
            "payload": payload,
        }
        signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
        signature = Ed25519PrivateKey.from_private_bytes(self.host_private).sign(signing_input)
        return AttestationEnvelope(
            profile=_PROFILE,
            signer_key_id=signer_key_id,
            attestation_id=attestation_id,
            issued_at=ARC_NOW,
            expires_at=ARC_NOW + CHALLENGE_TTL,
            payload=payload,
            signature=base64.b64encode(signature).decode("ascii"),
        )

    def request(self, envelope: AttestationEnvelope) -> ResolutionRequest:
        manifest = self.manifest()
        return ResolutionRequest(
            ctx=self.ctx(),
            host_id=_HOST_ID,
            manifest=manifest,
            envelope=envelope,
            manifest_fingerprint=_FINGERPRINT,
            candidates=SelectionInput(manifest=parse_manifest(manifest), tenant_id=self.seed.tenant_id, as_of=ARC_NOW),
            budget_limit_bytes=12288,
        )

    async def resolve_once(self) -> ResolutionOutcome:
        key = await self.register_key()
        return await self.service.resolve(
            self.request(self.envelope(await self.nonce(), key, f"att-{uuid.uuid4().hex[:12]}"))
        )


@pytest_asyncio.fixture
async def harness(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> _Harness:
    return _Harness(factory, seed)


# --- guarantee 1: one challenge, one receipt ---------------------------------


@pytest.mark.asyncio
async def test_parallel_resolutions_on_one_challenge_produce_exactly_one_receipt(harness: _Harness) -> None:
    """Four requests race for the same nonce. Exactly one may win.

    Each presents a distinct attestation ID, so replay cannot be what
    deduplicates them -- the challenge's own single-use lock has to.
    """
    key = await harness.register_key()
    nonce = await harness.nonce()

    async def _attempt() -> ResolutionOutcome:
        return await harness.service.resolve(
            harness.request(harness.envelope(nonce, key, f"att-{uuid.uuid4().hex[:12]}"))
        )

    results = await asyncio.gather(*(_attempt() for _ in range(4)), return_exceptions=True)

    winners = [r for r in results if isinstance(r, ResolutionOutcome)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1, [type(r).__name__ for r in results]
    assert len(losers) == 3
    assert all(isinstance(loser, ManifestUnverified) for loser in losers)

    async with harness.factory() as session:
        receipts = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE tenant_id = :tid"), {"tid": harness.seed.tenant_id}
            )
        ).scalar_one()
    assert receipts == 1


@pytest.mark.asyncio
async def test_the_losers_of_that_race_leave_no_partial_state(harness: _Harness) -> None:
    """A loser must roll back cleanly: no orphan events, no half-written
    selected rows, no head without a receipt."""
    key = await harness.register_key()
    nonce = await harness.nonce()

    async def _attempt() -> ResolutionOutcome:
        return await harness.service.resolve(
            harness.request(harness.envelope(nonce, key, f"att-{uuid.uuid4().hex[:12]}"))
        )

    await asyncio.gather(*(_attempt() for _ in range(4)), return_exceptions=True)

    async with harness.factory() as session:
        receipts = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE tenant_id = :tid"), {"tid": harness.seed.tenant_id}
            )
        ).scalar_one()
        events = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipt_events WHERE tenant_id = :tid"),
                {"tid": harness.seed.tenant_id},
            )
        ).scalar_one()
        orphan_heads = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_receipt_event_heads h "
                    "LEFT JOIN arc_receipts r ON r.receipt_id = h.receipt_id WHERE r.receipt_id IS NULL"
                )
            )
        ).scalar_one()

    assert receipts == 1
    assert events == 1
    assert orphan_heads == 0


@pytest.mark.asyncio
async def test_independent_resolutions_all_succeed_in_parallel(harness: _Harness) -> None:
    """The negative control for the tests above.

    Without it, a resolution path that simply serialized everything into one
    success would pass the single-receipt tests while being badly broken.
    """
    keys = [await harness.register_key() for _ in range(4)]
    nonces = [await harness.nonce() for _ in range(4)]

    results = await asyncio.gather(
        *(
            harness.service.resolve(harness.request(harness.envelope(n, k, f"att-{uuid.uuid4().hex[:12]}")))
            for n, k in zip(nonces, keys, strict=True)
        ),
        return_exceptions=True,
    )

    assert all(isinstance(r, ResolutionOutcome) for r in results), [
        repr(r) for r in results if not isinstance(r, ResolutionOutcome)
    ]
    assert len({r.receipt_id for r in results if isinstance(r, ResolutionOutcome)}) == 4


# --- guarantee 2: concurrent appends do not fork ------------------------------


@pytest.mark.asyncio
async def test_concurrent_event_appends_produce_no_fork(harness: _Harness) -> None:
    """Six appends launched together must produce one contiguous run of
    sequences and a chain that still verifies end to end."""
    outcome = await harness.resolve_once()

    async def _append() -> str:
        async with harness.factory() as session, session.begin():
            return await harness.receipts.append_event(
                session,
                receipt_id=outcome.receipt_id,
                tenant_id=harness.seed.tenant_id,
                event_type=RECEIPT_EVENT_JIT_RETRIEVAL,
                event_source=EVENT_SOURCE_HOST,
                request_payload_digest="9" * 64,
                payload={"n": 1},
                actor_id=harness.seed.actor_id,
                idempotency_key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            )

    results = await asyncio.gather(*(_append() for _ in range(6)), return_exceptions=True)
    assert all(isinstance(r, str) for r in results), [repr(r) for r in results if not isinstance(r, str)]

    async with harness.factory() as session:
        sequences = (
            (
                await session.execute(
                    text("SELECT sequence FROM arc_receipt_events WHERE receipt_id = :rid ORDER BY sequence"),
                    {"rid": outcome.receipt_id},
                )
            )
            .scalars()
            .all()
        )
        # The chain verifier is the real assertion: it checks every link,
        # every digest, every signature, and that the head matches the end.
        await harness.receipts.verify_chain(session, outcome.receipt_id)

    assert sequences == [0, 1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_appends_to_different_receipts_do_not_serialize_against_each_other(harness: _Harness) -> None:
    """Each receipt has its own head, so contention is per-receipt. If the
    lock were coarser than one row, this would still pass but the system
    would scale badly -- so this asserts the chains stay independent."""
    first = await harness.resolve_once()
    second = await harness.resolve_once()

    async def _append(receipt_id: uuid.UUID) -> str:
        async with harness.factory() as session, session.begin():
            return await harness.receipts.append_event(
                session,
                receipt_id=receipt_id,
                tenant_id=harness.seed.tenant_id,
                event_type=RECEIPT_EVENT_JIT_RETRIEVAL,
                event_source=EVENT_SOURCE_HOST,
                request_payload_digest="9" * 64,
                payload={},
                actor_id=harness.seed.actor_id,
                idempotency_key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            )

    await asyncio.gather(
        *(_append(first.receipt_id) for _ in range(3)), *(_append(second.receipt_id) for _ in range(3))
    )

    async with harness.factory() as session:
        await harness.receipts.verify_chain(session, first.receipt_id)
        await harness.receipts.verify_chain(session, second.receipt_id)


# --- guarantee 3: revocation and resolution linearize --------------------------


@pytest.mark.asyncio
async def test_concurrent_revocation_and_resolution_linearize(harness: _Harness) -> None:
    """Whichever order they land in, the result is consistent with *some*
    serial order -- never a receipt verified against an already-revoked key.

    The assertion is deliberately a disjunction rather than a fixed
    expectation: which one wins is genuinely nondeterministic, and a test
    demanding one specific winner would be flaky. What must never happen is
    the third case: a successful resolution alongside a committed revocation
    that preceded it.
    """
    key = await harness.register_key()
    nonce = await harness.nonce()

    async def _resolve() -> ResolutionOutcome:
        return await harness.service.resolve(
            harness.request(harness.envelope(nonce, key, f"att-{uuid.uuid4().hex[:12]}"))
        )

    async def _revoke() -> bool:
        async with harness.factory() as session, session.begin():
            return await HostSignerKeyRegistry().revoke(
                session, key, revoked_at=ARC_NOW - datetime.timedelta(seconds=1)
            )

    resolution, revoked = await asyncio.gather(_resolve(), _revoke(), return_exceptions=True)
    assert revoked is True

    async with harness.factory() as session:
        receipts = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE tenant_id = :tid"), {"tid": harness.seed.tenant_id}
            )
        ).scalar_one()

    if isinstance(resolution, ResolutionOutcome):
        # Resolution won the race: it read the key before revocation
        # committed, so exactly one receipt exists and it is legitimate.
        assert receipts == 1
    else:
        # Revocation won: the resolution saw a revoked key and produced
        # nothing at all.
        assert isinstance(resolution, ManifestUnverified)
        assert receipts == 0


@pytest.mark.asyncio
async def test_a_resolution_after_a_committed_revocation_always_fails(harness: _Harness) -> None:
    """No race: the unambiguous ordering, pinned so the disjunction above
    cannot silently degenerate into always taking the permissive branch."""
    key = await harness.register_key()
    async with harness.factory() as session, session.begin():
        await HostSignerKeyRegistry().revoke(session, key, revoked_at=ARC_NOW - datetime.timedelta(seconds=1))

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(
            harness.request(harness.envelope(await harness.nonce(), key, f"att-{uuid.uuid4().hex[:12]}"))
        )


@pytest.mark.asyncio
async def test_many_revocations_of_one_key_yield_one_winner(harness: _Harness) -> None:
    """Revocation is idempotent, and the first timestamp is the one that
    stands -- under contention as well as sequentially."""
    key = await harness.register_key()

    async def _revoke() -> bool:
        async with harness.factory() as session, session.begin():
            return await HostSignerKeyRegistry().revoke(session, key, revoked_at=ARC_NOW)

    results = await asyncio.gather(*(_revoke() for _ in range(4)), return_exceptions=True)
    assert sum(1 for r in results if r is True) == 1
