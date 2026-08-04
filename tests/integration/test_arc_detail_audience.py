"""Detail audience: redact the payload, keep the pointer.

An item the caller may not read comes back as an explicit redacted stub, not
as an absence. That distinction is the whole point of this file: "there is
nothing here" and "there is something here you are not cleared for" are
different facts, and an agent that cannot tell them apart will proceed when
it should escalate.

Revocation is the exception and behaves the opposite way — a revoked
revision is not something to hand back a stub of, it is something that
should no longer be served at all.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.continuation import ContinuationTokenProvider
from registry.arc.service.jit import (
    DENIED_AUDIENCE,
    DENIED_REVOKED,
    DetailDenied,
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
_OPEN_BODY = "anyone matched may read this"
_RESTRICTED_BODY = "admins and auditors only"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-audience")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ARC_NOW)


@pytest.fixture
def receipts(clock: FakeClock) -> ReceiptService:
    return ReceiptService(signing_provider(), clock)


@pytest.fixture
def jit(factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, clock: FakeClock) -> JitService:
    return JitService(
        factory,
        receipts=receipts,
        tokens=ContinuationTokenProvider({"ct-1": b"k" * 32}, active_key_id="ct-1"),
        clock=clock,
    )


def _ctx(seed: ArcSeed, *, roles: list[str] | None = None, mcp: str | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=roles or ["consumer"], oidc_subject="s"
    )
    return ArcRequestContext.from_validated_claims(
        tenant, {"iss": "https://idp.example.test"}, host_id="host-1", mcp_session_id=mcp
    )


async def _add_governed_item(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, *, audience: str, body: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One artifact, its active revision, and one directive on it.

    Each item gets its OWN artifact rather than sharing one: the schema
    permits a single active revision per artifact, and audience is a
    property of the revision — so two differently-audienced items are two
    artifacts, which is also what they would be in practice.

    Two items differing in audience is what makes per-item redaction
    observable; a single-item page can only ever be all-or-nothing.
    """
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    now = ARC_NOW
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, created_at) "
                "VALUES (:aid, :tid, :slug, 'policy', :now)"
            ),
            {
                "aid": artifact_id,
                "tid": seed.tenant_id,
                "slug": f"a-{artifact_id.hex[:8]}",
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
                ") VALUES ("
                "  :rid, :aid, :tid, 'test-system', :loc, :rloc, :digest, 'active', :efrom,"
                "  :review, :audience, 'revision_pinned_only', 'internal', :retention, 'none', :body, :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": seed.tenant_id,
                "loc": f"loc://{revision_id.hex[:8]}",
                "rloc": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": now,
                "review": now.replace(year=now.year + 1),
                "retention": now.replace(year=now.year + 2),
                "audience": audience,
                "body": body,
                "now": now,
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
                ") VALUES (:did, :rid, :tid, 'citation_only', 'statement', :anchor)"
            ),
            {"did": directive_id, "rid": revision_id, "tid": seed.tenant_id, "anchor": f"anchor-{audience}"},
        )
    return artifact_id, revision_id, directive_id


async def _receipt_over(
    factory: async_sessionmaker[AsyncSession],
    receipts: ReceiptService,
    seed: ArcSeed,
    selected: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
) -> uuid.UUID:
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
            bundle=ready_bundle(len(selected)),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
            selected_revisions=tuple(
                SelectedRevision(revision_id=rid, artifact_id=aid, is_mandatory=True) for aid, rid, _ in selected
            ),
            selected_directives=tuple(
                SelectedDirective(
                    revision_id=rid,
                    directive_id=did,
                    artifact_id=aid,
                    is_mandatory=True,
                    visibility_decision_id="vd-1",
                    source_locator="loc://a",
                    source_revision_locator="loc://a@1",
                    content_digest="e" * 64,
                    obligation_fields={},
                    # One handle per selection row: the schema requires a
                    # handle to resolve unambiguously, so they cannot share one.
                    context_handle_digest=hashlib.sha256(str(did).encode()).hexdigest(),
                )
                for aid, rid, did in selected
            ),
        )
        await consume_challenge(session, challenge_id)
    return receipt_id


def _request(receipt_id: uuid.UUID) -> DetailRequest:
    """The query shape, deliberately.

    A handle resolves to exactly one selection row, so a handle request can
    never contain two differently-audienced items. Per-item redaction is
    only observable across the receipt's selected set, which is what the
    query shape ranges over -- still unable to widen scope beyond it.
    """
    return DetailRequest(
        receipt_id=receipt_id,
        context_handle=_HANDLE,
        request_kind="query",
        selector={"query_text": "", "match_mode": "prefix", "max_results": 10},
        idempotency_key=uuid.uuid4().hex,
    )


@pytest_asyncio.fixture
async def mixed(factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, seed: ArcSeed) -> uuid.UUID:
    """A receipt over one open item and one admin-only item."""
    open_pair = await _add_governed_item(factory, seed, audience="all_matched_actors", body=_OPEN_BODY)
    restricted_pair = await _add_governed_item(factory, seed, audience="tenant_admin_auditor", body=_RESTRICTED_BODY)
    return await _receipt_over(factory, receipts, seed, [open_pair, restricted_pair])


# --- per-item redaction --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lower_audience_caller_gets_the_open_item_and_a_stub_for_the_other(
    jit: JitService, seed: ArcSeed, mixed: uuid.UUID
) -> None:
    page = await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(mixed))

    assert len(page.items) == 2
    readable = [i for i in page.items if not i["audience_redacted"]]
    redacted = [i for i in page.items if i["audience_redacted"]]
    assert len(readable) == 1
    assert len(redacted) == 1
    assert readable[0]["excerpt"] == _OPEN_BODY


@pytest.mark.asyncio
async def test_a_redacted_stub_carries_no_content_and_no_fingerprint_of_it(
    jit: JitService, seed: ArcSeed, mixed: uuid.UUID
) -> None:
    """Every audience-gated field is emptied, including the excerpt digest --
    a digest of withheld content is still an oracle for guessing it."""
    page = await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(mixed))
    stub = next(i for i in page.items if i["audience_redacted"])

    assert stub["excerpt"] is None
    assert stub["excerpt_digest"] is None
    assert stub["citation"] is None
    assert stub["source_anchor"] is None
    # And the restricted text appears nowhere in the serialized page.
    assert _RESTRICTED_BODY not in str(page.items)


@pytest.mark.asyncio
async def test_a_redacted_stub_still_names_what_was_withheld(jit: JitService, seed: ArcSeed, mixed: uuid.UUID) -> None:
    """The pointer survives so the omission is nameable. Without it the
    caller cannot tell absence from denial, and cannot escalate."""
    page = await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(mixed))
    stub = next(i for i in page.items if i["audience_redacted"])

    assert stub["artifact_id"] is not None
    assert stub["revision_id"] is not None
    assert stub["directive_id"] is not None


@pytest.mark.asyncio
async def test_an_admin_reads_both_items_unredacted(jit: JitService, seed: ArcSeed, mixed: uuid.UUID) -> None:
    """The negative control: redaction must depend on the caller, not be
    unconditional."""
    page = await jit.retrieve(_ctx(seed, roles=["admin"]), _request(mixed))

    assert len(page.items) == 2
    assert all(i["audience_redacted"] is False for i in page.items)
    bodies = {i["excerpt"] for i in page.items}
    assert bodies == {_OPEN_BODY, _RESTRICTED_BODY}


@pytest.mark.asyncio
async def test_an_auditor_also_reads_the_restricted_item(jit: JitService, seed: ArcSeed, mixed: uuid.UUID) -> None:
    page = await jit.retrieve(_ctx(seed, roles=["auditor"]), _request(mixed))
    assert all(i["audience_redacted"] is False for i in page.items)


@pytest.mark.asyncio
async def test_the_page_reports_that_something_was_redacted(jit: JitService, seed: ArcSeed, mixed: uuid.UUID) -> None:
    page = await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(mixed))
    assert DENIED_AUDIENCE in page.reason_codes


@pytest.mark.asyncio
async def test_a_fully_readable_page_reports_no_redaction(jit: JitService, seed: ArcSeed, mixed: uuid.UUID) -> None:
    page = await jit.retrieve(_ctx(seed, roles=["admin"]), _request(mixed))
    assert page.reason_codes == ()


# --- the gateway-only audience ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_gateway_only_item_is_redacted_for_a_plain_rest_caller(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Gateway identity is the server-assigned session, and no role
    substitutes for it — not even admin."""
    open_pair = await _add_governed_item(factory, seed, audience="all_matched_actors", body=_OPEN_BODY)
    gateway_pair = await _add_governed_item(factory, seed, audience="registered_gateway_only", body="gateway text")
    receipt_id = await _receipt_over(factory, receipts, seed, [open_pair, gateway_pair])

    page = await jit.retrieve(_ctx(seed, roles=["admin"]), _request(receipt_id))
    redacted = [i for i in page.items if i["audience_redacted"]]
    assert len(redacted) == 1


@pytest.mark.asyncio
async def test_a_gateway_session_reads_the_gateway_only_item(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    open_pair = await _add_governed_item(factory, seed, audience="all_matched_actors", body=_OPEN_BODY)
    gateway_pair = await _add_governed_item(factory, seed, audience="registered_gateway_only", body="gateway text")
    receipt_id = await _receipt_over(factory, receipts, seed, [open_pair, gateway_pair])

    page = await jit.retrieve(_ctx(seed, roles=["consumer"], mcp="mcp-1"), _request(receipt_id))
    assert all(i["audience_redacted"] is False for i in page.items)


# --- nothing readable, and revocation --------------------------------------------


@pytest.mark.asyncio
async def test_a_page_with_nothing_readable_is_denied_rather_than_all_stubs(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """A page of empty stubs is not a useful answer; it is a denial dressed
    up as a success."""
    restricted = await _add_governed_item(factory, seed, audience="tenant_admin_auditor", body=_RESTRICTED_BODY)
    receipt_id = await _receipt_over(factory, receipts, seed, [restricted])

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(receipt_id))
    assert exc.value.reason_code == DENIED_AUDIENCE


@pytest.mark.asyncio
async def test_a_revoked_item_is_removed_not_redacted(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    """Revocation and audience differ deliberately: a revoked revision is
    not something to hand back a stub of."""
    open_pair = await _add_governed_item(factory, seed, audience="all_matched_actors", body=_OPEN_BODY)
    doomed = await _add_governed_item(factory, seed, audience="all_matched_actors", body="revoked text")
    receipt_id = await _receipt_over(factory, receipts, seed, [open_pair, doomed])

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :at WHERE revision_id = :rid"),
            {"rid": doomed[1], "at": ARC_NOW},
        )

    page = await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(receipt_id))
    assert len(page.items) == 1
    assert page.items[0]["excerpt"] == _OPEN_BODY


@pytest.mark.asyncio
async def test_every_item_revoked_is_a_denial_recorded_on_the_chain(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed: ArcSeed
) -> None:
    doomed = await _add_governed_item(factory, seed, audience="all_matched_actors", body="revoked text")
    receipt_id = await _receipt_over(factory, receipts, seed, [doomed])
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET lifecycle_state = 'revoked', revoked_at = :at WHERE revision_id = :rid"),
            {"rid": doomed[1], "at": ARC_NOW},
        )

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(receipt_id))
    assert exc.value.reason_code == DENIED_REVOKED

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
        await receipts.verify_chain(session, receipt_id)

    assert event.event_type == "jit_denied"
    assert event.event_payload["reason_codes"] == [DENIED_REVOKED]


@pytest.mark.asyncio
async def test_the_receipt_event_records_how_many_items_were_withheld(
    factory: async_sessionmaker[AsyncSession],
    jit: JitService,
    seed: ArcSeed,
    mixed: uuid.UUID,
) -> None:
    """An auditor can see that something was withheld from this actor,
    without the event itself naming what."""
    await jit.retrieve(_ctx(seed, roles=["consumer"]), _request(mixed))

    async with factory() as session:
        payload = (
            await session.execute(
                text("SELECT event_payload FROM arc_receipt_events WHERE receipt_id = :rid AND sequence = 1"),
                {"rid": mixed},
            )
        ).scalar_one()

    assert payload["redacted_count"] == 1
    assert payload["reason_codes"] == [DENIED_AUDIENCE]
    assert _RESTRICTED_BODY not in str(payload)
