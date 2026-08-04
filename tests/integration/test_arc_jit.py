"""JIT detail: one page per transaction, with re-checks that outlive the receipt.

The receipt says what was granted *at resolution*. Detail is served later, so
these tests are mostly about what happens when the world moved in between --
a revoked revision, a receipt whose chain broke, a token replayed, a handle
pointing at something the receipt never selected.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.continuation import (
    ContinuationTokenError,
    ContinuationTokenProvider,
    PageBinding,
    PageState,
    issue,
    open_token,
    token_digest,
)
from registry.arc.service.jit import (
    DENIED_AUDIENCE,
    DENIED_NOT_SELECTED,
    DENIED_RECEIPT_UNUSABLE,
    DENIED_REVOKED,
    DetailDenied,
    DetailIdempotencyConflict,
    DetailRequest,
    JitService,
)
from registry.arc.service.receipt import (
    ReceiptService,
    SelectedDirective,
    SelectedRevision,
    preallocate_receipt_id,
)
from registry.arc.types import ArcRequestContext
from registry.audit import actions
from registry.types import FakeClock, TenantContext
from tests.helpers.arc_fixtures import (
    ARC_NOW,
    ArcSeed,
    consume_challenge,
    provenance,
    ready_bundle,
    replay_envelope,
    seed_arc,
    seed_challenge,
    signing_provider,
)

_HANDLE = "handle-1"
_HANDLE_DIGEST = hashlib.sha256(_HANDLE.encode()).hexdigest()
_TOKEN_KEY = "ct-1"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-jit")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ARC_NOW)


@pytest.fixture
def tokens() -> ContinuationTokenProvider:
    return ContinuationTokenProvider({_TOKEN_KEY: b"k" * 32}, active_key_id=_TOKEN_KEY)


@pytest.fixture
def receipts(clock: FakeClock) -> ReceiptService:
    return ReceiptService(signing_provider(), clock)


@pytest.fixture
def jit(
    factory: async_sessionmaker[AsyncSession],
    receipts: ReceiptService,
    tokens: ContinuationTokenProvider,
    clock: FakeClock,
) -> JitService:
    return JitService(factory, receipts=receipts, tokens=tokens, clock=clock)


def _ctx(seed: ArcSeed, *, roles: list[str] | None = None, mcp: str | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=roles or ["consumer"], oidc_subject="s"
    )
    return ArcRequestContext.from_validated_claims(
        tenant, {"iss": "https://idp.example.test"}, host_id="host-1", mcp_session_id=mcp
    )


async def _receipt_with_detail(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, seed: ArcSeed
) -> uuid.UUID:
    """A committed receipt that selected the seeded directive under `_HANDLE`."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    async with factory() as session, session.begin():
        await receipts.create_receipt(
            session,
            receipt_id=receipt_id,
            challenge_id=challenge_id,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            host_id="host-1",
            session_id="sess-1",
            manifest_fingerprint="f" * 64,
            attestation_id=f"att-{receipt_id}",
            bundle=ready_bundle(1),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
            selected_revisions=(
                SelectedRevision(revision_id=seed.revision_id, artifact_id=seed.artifact_id, is_mandatory=True),
            ),
            selected_directives=(
                SelectedDirective(
                    revision_id=seed.revision_id,
                    directive_id=seed.directive_id,
                    artifact_id=seed.artifact_id,
                    is_mandatory=True,
                    visibility_decision_id="vd-1",
                    source_locator="loc://a",
                    source_revision_locator="loc://a@1",
                    content_digest="e" * 64,
                    obligation_fields={},
                    context_handle_digest=_HANDLE_DIGEST,
                ),
            ),
        )
        await consume_challenge(session, challenge_id)
    return receipt_id


def _request(receipt_id: uuid.UUID, **overrides: object) -> DetailRequest:
    base: dict[str, object] = {
        "receipt_id": receipt_id,
        "context_handle": _HANDLE,
        "request_kind": "directive",
        "selector": {},
        "idempotency_key": uuid.uuid4().hex,
    }
    base.update(overrides)
    return DetailRequest(**base)  # type: ignore[arg-type]


# --- the happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_selected_item_is_served_with_its_citation(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt_with_detail(factory, receipts, seed)

    page = await jit.retrieve(_ctx(seed), _request(receipt_id))

    assert page.page_number == 1
    assert page.complete is True
    assert page.continuation_token is None
    assert len(page.items) == 1
    item = page.items[0]
    assert item["trust_label"] == "source_detail"
    assert item["citation"]["source_system"] == "test-system"  # type: ignore[index]
    assert item["excerpt_digest"] == hashlib.sha256(str(item["excerpt"]).encode()).hexdigest()


@pytest.mark.asyncio
async def test_serving_a_page_appends_a_receipt_event(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Detail retrieval is part of the receipt's history, not a side channel."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    await jit.retrieve(_ctx(seed), _request(receipt_id))

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_type, event_source, sequence FROM arc_receipt_events "
                    "WHERE receipt_id = :rid AND sequence = 1"
                ),
                {"rid": receipt_id},
            )
        ).one()
        # The chain must still verify after the append.
        await receipts.verify_chain(session, receipt_id)

    assert row.event_type == "jit_retrieval"
    assert row.event_source == "host"


@pytest.mark.asyncio
async def test_serving_a_page_emits_an_audit_row(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    await jit.retrieve(_ctx(seed), _request(receipt_id))

    async with factory() as session:
        event_type = (
            await session.execute(
                text(
                    "SELECT event_type FROM arc_audit_outbox WHERE event_payload ->> 'receipt_id' = :rid "
                    "AND event_type = :t"
                ),
                {"rid": str(receipt_id), "t": actions.ARC_JIT_GRANTED},
            )
        ).scalar_one()
    assert event_type == actions.ARC_JIT_GRANTED


# --- the re-checks, which are the point ------------------------------------------


@pytest.mark.asyncio
async def test_a_handle_the_receipt_never_selected_is_denied(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Detail cannot widen scope: a handle points into what was granted."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed), _request(receipt_id, context_handle="a-handle-never-issued"))
    assert exc.value.reason_code == DENIED_NOT_SELECTED


@pytest.mark.asyncio
async def test_a_revision_revoked_after_resolution_is_denied(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """The receipt still references it; the revocation must win anyway."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :at WHERE revision_id = :rid"),
            {"rid": seed.revision_id, "at": ARC_NOW},
        )

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed), _request(receipt_id))
    assert exc.value.reason_code == DENIED_REVOKED


@pytest.mark.asyncio
async def test_a_receipt_whose_chain_failed_cannot_serve_detail(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Content whose provenance can no longer be vouched for is not served."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    await receipts.mark_integrity_failed(factory, receipt_id, reason="chain_broken")

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed), _request(receipt_id))
    assert exc.value.reason_code == DENIED_RECEIPT_UNUSABLE


@pytest.mark.asyncio
async def test_a_narrowed_audience_denies_a_caller_who_could_read_before(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Audience is evaluated now, not as of resolution."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET detail_audience = 'tenant_admin_auditor' WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(receipt_id))
    assert exc.value.reason_code == DENIED_AUDIENCE


@pytest.mark.asyncio
async def test_an_admin_still_reads_a_narrowed_audience(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """The negative control for the test above -- narrowing must not deny
    everyone."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET detail_audience = 'tenant_admin_auditor' WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )

    page = await jit.retrieve(_ctx(seed, roles=["admin"]), _request(receipt_id))
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_a_denial_is_recorded_on_the_chain_and_in_the_audit_trail(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """A denial is evidence too: what an agent was refused, and when."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :at WHERE revision_id = :rid"),
            {"rid": seed.revision_id, "at": ARC_NOW},
        )

    with pytest.raises(DetailDenied):
        await jit.retrieve(_ctx(seed), _request(receipt_id))

    async with factory() as session:
        event = (
            await session.execute(
                text(
                    "SELECT event_type, event_payload FROM arc_receipt_events "
                    "WHERE receipt_id = :rid AND sequence = 1"
                ),
                {"rid": receipt_id},
            )
        ).one()
        audited = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_audit_outbox WHERE event_payload ->> 'receipt_id' = :rid "
                    "AND event_type = :t"
                ),
                {"rid": str(receipt_id), "t": actions.ARC_JIT_DENIED},
            )
        ).scalar_one()
        await receipts.verify_chain(session, receipt_id)

    assert event.event_type == "jit_denied"
    assert event.event_payload["reason_codes"] == [DENIED_REVOKED]
    assert audited == 1


# --- idempotency ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_idempotency_key_with_a_different_request_is_a_conflict(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    key = uuid.uuid4().hex
    await jit.retrieve(_ctx(seed), _request(receipt_id, idempotency_key=key))

    with pytest.raises(DetailIdempotencyConflict):
        await jit.retrieve(_ctx(seed), _request(receipt_id, idempotency_key=key, request_kind="source_anchor"))


@pytest.mark.asyncio
async def test_reusing_an_idempotency_key_cannot_append_twice(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """The unique index is the backstop behind the digest comparison."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    key = uuid.uuid4().hex
    await jit.retrieve(_ctx(seed), _request(receipt_id, idempotency_key=key))

    with pytest.raises(IntegrityError):
        await jit.retrieve(_ctx(seed), _request(receipt_id, idempotency_key=key))


# --- continuation tokens -----------------------------------------------------------


def _binding(seed: ArcSeed, receipt_id: uuid.UUID, base_digest: str) -> PageBinding:
    return PageBinding(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        host_id="host-1",
        receipt_id=receipt_id,
        context_handle_digest=_HANDLE_DIGEST,
        base_request_digest=base_digest,
    )


def _state(clock: FakeClock) -> PageState:
    return PageState(
        page_number=1,
        next_position=1,
        cumulative_bytes=100,
        cumulative_results=1,
        artifact_state_digest="a" * 64,
        issued_at=clock.now(),
        expires_at=clock.now() + datetime.timedelta(minutes=5),
    )


def test_a_token_round_trips_under_its_own_binding(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    binding = _binding(seed, uuid.uuid4(), "d" * 64)
    token = issue(tokens, binding=binding, state=_state(clock))

    reopened = open_token(tokens, token, binding=binding, now=clock.now())
    assert reopened.next_position == 1
    assert reopened.cumulative_bytes == 100


def test_a_token_is_opaque(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    """Sealed rather than signed: the cursor and counters must not be
    readable by the party holding the token."""
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    token = issue(tokens, binding=_binding(seed, uuid.uuid4(), "d" * 64), state=_state(clock))
    assert "next_position" not in token
    assert "cumulative_bytes" not in token


def test_a_token_bound_to_another_receipt_is_refused(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    """The binding is authenticated data, so a mismatch fails at decryption
    rather than at a comparison someone has to remember to write."""
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    token = issue(tokens, binding=_binding(seed, uuid.uuid4(), "d" * 64), state=_state(clock))

    with pytest.raises(ContinuationTokenError):
        open_token(tokens, token, binding=_binding(seed, uuid.uuid4(), "d" * 64), now=clock.now())


def test_a_token_bound_to_another_actor_is_refused(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    receipt_id = uuid.uuid4()
    mine = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    theirs = ArcSeed(mine.tenant_id, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    token = issue(tokens, binding=_binding(mine, receipt_id, "d" * 64), state=_state(clock))

    with pytest.raises(ContinuationTokenError):
        open_token(tokens, token, binding=_binding(theirs, receipt_id, "d" * 64), now=clock.now())


def test_a_token_for_a_different_base_request_is_refused(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    """Paging cannot be redirected mid-chain to a different question."""
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    receipt_id = uuid.uuid4()
    token = issue(tokens, binding=_binding(seed, receipt_id, "d" * 64), state=_state(clock))

    with pytest.raises(ContinuationTokenError):
        open_token(tokens, token, binding=_binding(seed, receipt_id, "b" * 64), now=clock.now())


def test_an_expired_token_is_refused(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    binding = _binding(seed, uuid.uuid4(), "d" * 64)
    token = issue(tokens, binding=binding, state=_state(clock))

    later = clock.now() + datetime.timedelta(minutes=6)
    with pytest.raises(ContinuationTokenError, match="expired"):
        open_token(tokens, token, binding=binding, now=later)


def test_a_tampered_token_is_refused(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    binding = _binding(seed, uuid.uuid4(), "d" * 64)
    token = issue(tokens, binding=binding, state=_state(clock))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    with pytest.raises(ContinuationTokenError):
        open_token(tokens, tampered, binding=binding, now=clock.now())


def test_a_token_sealed_under_an_unheld_key_is_refused(clock: FakeClock) -> None:
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    binding = _binding(seed, uuid.uuid4(), "d" * 64)
    issuer = ContinuationTokenProvider({"old": b"o" * 32}, active_key_id="old")
    token = issue(issuer, binding=binding, state=_state(clock))

    rotated = ContinuationTokenProvider({"new": b"n" * 32}, active_key_id="new")
    with pytest.raises(ContinuationTokenError):
        open_token(rotated, token, binding=binding, now=clock.now())


def test_the_token_digest_is_what_the_chain_records(tokens: ContinuationTokenProvider, clock: FakeClock) -> None:
    """The digest, not the token: the table proves single use without
    storing material that would let a reader resume someone else's paging."""
    seed = ArcSeed(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "hk")
    token = issue(tokens, binding=_binding(seed, uuid.uuid4(), "d" * 64), state=_state(clock))
    assert token_digest(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in token_digest(token)


@pytest.mark.asyncio
async def test_an_invalid_token_is_rejected_without_touching_the_chain(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """The guarantee that stops a stranger appending to someone's audit
    record: an attempt that was never well-bound leaves the chain alone."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed), _request(receipt_id, continuation_token="not-a-real-token"))
    assert exc.value.reason_code == "invalid_continuation"

    async with factory() as session:
        events = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()
        rejected = (
            await session.execute(
                text(
                    "SELECT count(*) FROM arc_audit_outbox WHERE event_payload ->> 'receipt_id' = :rid "
                    "AND event_type = :t"
                ),
                {"rid": str(receipt_id), "t": actions.ARC_JIT_ATTEMPT_REJECTED},
            )
        ).scalar_one()

    # Only the creation event: the rejected attempt appended nothing.
    assert events == 1
    # But it *is* recorded, out of band.
    assert rejected == 1


@pytest.mark.asyncio
async def test_a_replayed_token_is_refused_by_the_database(
    factory: async_sessionmaker[AsyncSession],
    receipts: ReceiptService,
    jit: JitService,
    tokens: ContinuationTokenProvider,
    seed: ArcSeed,
    clock: FakeClock,
) -> None:
    """Single use is enforced by a unique index on the consumed digest, not
    by a check in the service that could be removed."""
    receipt_id = await _receipt_with_detail(factory, receipts, seed)
    request = _request(receipt_id)
    binding = _binding(seed, receipt_id, request.base_digest())
    token = issue(tokens, binding=binding, state=_state(clock))

    async with factory() as session, session.begin():
        await receipts.append_event(
            session,
            receipt_id=receipt_id,
            tenant_id=seed.tenant_id,
            event_type="jit_retrieval",
            event_source="host",
            request_payload_digest=request.base_digest(),
            payload={},
            actor_id=seed.actor_id,
            idempotency_key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            consumed_continuation_token_digest=token_digest(token),
        )

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await receipts.append_event(
                session,
                receipt_id=receipt_id,
                tenant_id=seed.tenant_id,
                event_type="jit_retrieval",
                event_source="host",
                request_payload_digest=request.base_digest(),
                payload={},
                actor_id=seed.actor_id,
                idempotency_key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                consumed_continuation_token_digest=token_digest(token),
            )
