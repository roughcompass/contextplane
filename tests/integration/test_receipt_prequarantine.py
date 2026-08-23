"""A receipt that quoted a quarantined claim stops being servable. E4-T4.

Contract under test
----------------------------------------------------
A receipt cites a claim by carrying its id as the `item_key` of an
`observed_claims` item. When a quarantine withholds that claim, the receipts
that quoted it are withheld in the *same transaction* — so no reader observes
one without the other — and reverting the quarantine releases exactly the
receipts that quarantine withheld.

Three things this proves that a unit test could not:

**The withholding is atomic with the claim write.** E4-T4 prescribed marking
the downstream set first and reconciling afterwards, to close the window a
row-at-a-time sweep leaves open. There is no such window: `apply` is one
transaction. What is asserted here is the consequence — after `apply` returns,
both the claim and its receipts are unservable, and before it there was no
intermediate state to observe.

**The refusal is in the service, not a router.** The reads are called directly,
past both transports, because the guard used to live in one router and the four
MCP tools over the same reads had none.

**Release is keyed on which quarantine withheld what.** A receipt reached by two
open incidents is not released by reverting the first.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context.receipt_withholding import ReceiptWithholder
from contextplane.context.receipts import (
    NOT_SERVABLE_UNHYDRATED,
    NOT_SERVABLE_WITHHELD,
    ContextReceiptService,
    ReceiptNotServable,
)
from contextplane.context.schemas.envelope import BLOCK_OBSERVED_CLAIMS
from contextplane.service.memory.quarantine import SELECTOR_CONNECTOR_RUN, QuarantineService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_QUARANTINED = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)
#: Reverting is a later act than applying, and the ledger's own CHECK says so —
#: a quarantine reverted at the instant it was applied never withheld anything.
_REVERTED = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


class _World:
    """One tenant, one claim under a known connector run, one receipt citing it."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": self.tenant_id, "s": f"rq-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'op', :sub, :now)"
                ),
                {"a": self.actor_id, "t": self.tenant_id, "sub": f"rq-{self.actor_id.hex[:8]}", "now": _SEEDED},
            )
        return self

    def ctx(self, *, roles: list[str] | None = None) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.actor_id, roles=roles or ["producer"])

    async def claim(self, *, run: str) -> uuid.UUID:
        claim_id, entity_id = uuid.uuid4(), uuid.uuid4()
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                    "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                ),
                {"e": entity_id, "t": self.tenant_id, "n": f"cap-{entity_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                    "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                    "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                    "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                    "  scorer_version, calibration_version, decay_half_life_days, namespace, strategy_id"
                    ") VALUES ("
                    "  :cid, :t, :t, :a, :e, 'ref', 'owned_by_team', 'prose',"
                    "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30, 'team/a', 'extract.v1')"
                ),
                {"cid": claim_id, "t": self.tenant_id, "a": self.actor_id, "e": entity_id, "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                    "VALUES (:cid, 'connector_run', :run)"
                ),
                {"cid": claim_id, "run": run},
            )
        return claim_id

    async def receipt(self, *, cites: uuid.UUID, hydration: str = "complete") -> uuid.UUID:
        """A receipt whose `observed_claims` item names `cites` — the shape
        `observed_claims_arm` writes."""
        receipt_id = uuid.uuid4()
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO context_receipts ("
                    "  receipt_id, tenant_id, state, cacheable, hydration_state, item_count,"
                    "  exclusion_count, resolved_at, requested_by"
                    ") VALUES (:r, :t, 'complete', TRUE, :h, 1, 0, :now, 'agent')"
                ),
                {"h": hydration, "now": _SEEDED, "r": receipt_id, "t": self.tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO context_receipt_items ("
                    "  item_row_id, receipt_id, receipt_item_id, block, source, item_key,"
                    # All eight, because `ck_receipt_items_trust_outside_canonical`
                    # requires them on a non-canonical item -- the schema enforcing
                    # what `arms.py` states as "every non-canonical item carries all
                    # eight". A fixture that omitted them would be testing a row the
                    # service could never write.
                    "  trust, trust_source, assertion_kind, authority, freshness,"
                    "  mutability, attribution, classification"
                    ") VALUES (:i, :r, :rid, :block, 'claims', :key,"
                    "  'observed', 'memory_claim', 'observed', 'observer_extraction', :now,"
                    "  'mutable', 'agent', 'internal')"
                ),
                {
                    "block": BLOCK_OBSERVED_CLAIMS,
                    "i": uuid.uuid4(),
                    "key": str(cites),
                    "now": _SEEDED,
                    "r": receipt_id,
                    "rid": f"item-{receipt_id.hex[:8]}",
                },
            )
        return receipt_id

    def quarantine(self, *, at: datetime.datetime = _QUARANTINED) -> QuarantineService:
        return QuarantineService(self.factory, clock=FakeClock(at), receipts=ReceiptWithholder())

    def receipts(self) -> ContextReceiptService:
        return ContextReceiptService(session_factory=self.factory, clock=FakeClock(_QUARANTINED))


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    engine = create_async_engine(pg_container, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield await _World(factory).build()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_receipt_that_quoted_a_quarantined_claim_stops_serving(world: _World) -> None:
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)

    # Servable before.
    assert await world.receipts().arms_for(world.ctx(), receipt_id=receipt_id) == ()

    await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )

    with pytest.raises(ReceiptNotServable) as refused:
        await world.receipts().exclusions_for(world.ctx(), receipt_id=receipt_id)
    assert refused.value.reason == NOT_SERVABLE_WITHHELD


@pytest.mark.asyncio
async def test_the_refusal_is_in_the_service_so_every_read_inherits_it(world: _World) -> None:
    """Called directly, past both transports. The guard used to live in one
    router, and the four MCP tools over these same reads had none."""
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)
    await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )

    for read in ("exclusions_for", "arms_for"):
        with pytest.raises(ReceiptNotServable):
            await getattr(world.receipts(), read)(world.ctx(), receipt_id=receipt_id)


@pytest.mark.asyncio
async def test_the_header_still_says_why_the_evidence_reads_refuse(world: _World) -> None:
    """`get` deliberately does not refuse. It is the surface a caller polls to
    learn to wait and an operator reads to learn why — refusing it too would
    leave no way to observe the state these columns exist to publish."""
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)
    await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )

    header = await world.receipts().get(world.ctx(), receipt_id=receipt_id)

    assert header is not None
    assert header.withheld_at == _QUARANTINED
    assert header.hydration_state == "complete", "withholding is not a hydration state"


@pytest.mark.asyncio
async def test_an_unhydrated_receipt_refuses_with_the_other_reason(world: _World) -> None:
    """Two refusals, and they call for opposite actions: an unhydrated receipt
    is worth retrying, a withheld one is not until the incident is resolved."""
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id, hydration="pending")

    with pytest.raises(ReceiptNotServable) as refused:
        await world.receipts().exclusions_for(world.ctx(), receipt_id=receipt_id)

    assert refused.value.reason == NOT_SERVABLE_UNHYDRATED


@pytest.mark.asyncio
async def test_a_receipt_citing_nothing_quarantined_keeps_serving(world: _World) -> None:
    quarantined = await world.claim(run="run-42")
    untouched = await world.claim(run="run-other")
    safe_receipt = await world.receipt(cites=untouched)
    await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )
    assert quarantined is not untouched

    assert await world.receipts().arms_for(world.ctx(), receipt_id=safe_receipt) == ()


@pytest.mark.asyncio
async def test_reverting_releases_the_receipts_that_quarantine_withheld(world: _World) -> None:
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )

    await world.quarantine(at=_REVERTED).revert(world.ctx(), quarantine_id=applied.quarantine_id)

    assert await world.receipts().arms_for(world.ctx(), receipt_id=receipt_id) == ()
    header = await world.receipts().get(world.ctx(), receipt_id=receipt_id)
    assert header is not None
    assert header.withheld_at is None
    assert header.withheld_by is None


@pytest.mark.asyncio
async def test_a_receipt_held_by_a_second_incident_is_not_released_by_reverting_the_first(
    world: _World,
) -> None:
    """`withheld_by` records which quarantine took it, so the second incident's
    revert finds nothing of its own to release — which is the correct answer
    rather than a missed one."""
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)
    first = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="first incident"
    )
    # A second quarantine over the same claim, by a different selector.
    second = await world.quarantine().apply(
        world.ctx(), selector="strategy_id", value="extract.v1", reason="second incident"
    )

    await world.quarantine(at=_REVERTED).revert(world.ctx(), quarantine_id=second.quarantine_id)

    header = await world.receipts().get(world.ctx(), receipt_id=receipt_id)
    assert header is not None
    assert header.withheld_by == first.quarantine_id, "the first incident still holds it"
    with pytest.raises(ReceiptNotServable):
        await world.receipts().exclusions_for(world.ctx(), receipt_id=receipt_id)


@pytest.mark.asyncio
async def test_a_deployment_with_no_withholder_quarantines_claims_and_leaves_receipts(
    world: _World,
) -> None:
    """The behaviour before this task, rather than a silent half-application."""
    claim_id = await world.claim(run="run-42")
    receipt_id = await world.receipt(cites=claim_id)

    await QuarantineService(world.factory, clock=FakeClock(_QUARANTINED)).apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad connector run"
    )

    header = await world.receipts().get(world.ctx(), receipt_id=receipt_id)
    assert header is not None
    assert header.withheld_at is None
