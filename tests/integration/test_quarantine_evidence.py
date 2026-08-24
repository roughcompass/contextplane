"""The scope is the whole point, so these are boundary tests.

E4-T7b. A bundle that quietly includes rows outside the quarantine is a
disclosure; one that quietly omits rows inside it is an incomplete filing.
Neither shows in the output, so every test here seeds the thing that should
*not* be in the bundle alongside the thing that should.

Against a real database because every property is a property of the SQL.
`claim_quarantine_members` has no `tenant_id`, so the isolation under test is a
join rather than a predicate on the table being read.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import NotFoundError
from contextplane.service.memory.quarantine import (
    SELECTOR_CONNECTOR_RUN,
    QuarantineService,
)
from contextplane.service.memory.quarantine_evidence import QuarantineEvidenceService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_APPLIED = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)
_REVERTED = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Tenant:
    """One tenant with claims under a known connector run."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()

    async def build(self) -> _Tenant:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": self.tenant_id, "s": f"qe-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'op', :sub, :now)"
                ),
                {"a": self.actor_id, "t": self.tenant_id, "sub": f"qe-{self.actor_id.hex[:8]}", "now": _SEEDED},
            )
        return self

    async def claim(self, *, run: str) -> uuid.UUID:
        cid, eid = uuid.uuid4(), uuid.uuid4()
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                    "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                ),
                {"e": eid, "t": self.tenant_id, "n": f"cap-{eid.hex[:8]}", "now": _SEEDED},
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
                {"cid": cid, "t": self.tenant_id, "a": self.actor_id, "e": eid, "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                    "VALUES (:cid, 'connector_run', :ref)"
                ),
                {"cid": cid, "ref": run},
            )
        return cid

    def ctx(self, *, roles: list[str] | None = None) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            roles=roles if roles is not None else ["producer"],
            oidc_subject="op",
        )

    def quarantine(self, *, now: datetime.datetime = _APPLIED) -> QuarantineService:
        return QuarantineService(self.factory, clock=FakeClock(now))

    def evidence(self) -> QuarantineEvidenceService:
        return QuarantineEvidenceService(self.factory)


@pytest_asyncio.fixture
async def tenant(factory: async_sessionmaker[AsyncSession]) -> _Tenant:
    return await _Tenant(factory).build()


@pytest_asyncio.fixture
async def other(factory: async_sessionmaker[AsyncSession]) -> _Tenant:
    """A second tenant, deliberately identical in shape. Every scope assertion
    below is only meaningful because this exists alongside."""
    return await _Tenant(factory).build()


@pytest.mark.asyncio
async def test_the_bundle_carries_the_ledger_row_and_the_recorded_members(tenant: _Tenant) -> None:
    bad = await tenant.claim(run="run-42")
    await tenant.claim(run="run-43")
    applied = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="connector emitted nonsense"
    )

    bundle = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=applied.quarantine_id)

    assert bundle.members == (bad,)
    assert bundle.matched_count == 1
    assert bundle.reason == "connector emitted nonsense"
    assert bundle.applied_by == tenant.actor_id
    assert bundle.applied_at == _APPLIED
    assert bundle.predicate == {"selector": SELECTOR_CONNECTOR_RUN, "value": "run-42"}
    assert not bundle.is_reverted


@pytest.mark.asyncio
async def test_a_second_tenants_identically_shaped_quarantine_is_absent(tenant: _Tenant, other: _Tenant) -> None:
    """The disclosure direction, and the trap this table sets.

    `claim_quarantine_members` has no `tenant_id`. A members query keyed on
    `quarantine_id` alone reads correctly for every well-behaved caller and
    serves another tenant's recorded set to anybody who guesses a UUID, so the
    isolation has to come from the join — and only a second tenant seeded
    alongside can tell the two implementations apart.
    """
    await tenant.claim(run="run-42")
    theirs = await other.claim(run="run-42")
    mine = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="ours went wrong"
    )
    yours = await other.quarantine().apply(
        other.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="theirs went wrong too"
    )
    assert mine.quarantine_id != yours.quarantine_id

    bundle = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=mine.quarantine_id)

    assert theirs not in bundle.members
    assert bundle.reason == "ours went wrong"

    # And from the other side: asking for their quarantine with my context is a
    # refusal rather than a bundle, because the ledger read is tenant-scoped too.
    with pytest.raises(NotFoundError):
        await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=yours.quarantine_id)


@pytest.mark.asyncio
async def test_a_reverted_quarantine_still_exports_everything_it_withheld(tenant: _Tenant) -> None:
    """The omission direction.

    The ledger keeps its row and its membership after revert by design, so that
    "the fact that content was withheld for a period survives the withholding".
    A bundle filtering on what is *currently* withheld would return nothing here
    — and the period a reverted quarantine covers is exactly the period somebody
    asking for this document is asking about.
    """
    bad = await tenant.claim(run="run-42")
    applied = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="connector emitted nonsense"
    )
    restored = await tenant.quarantine(now=_REVERTED).revert(tenant.ctx(), quarantine_id=applied.quarantine_id)
    assert restored == 1

    bundle = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=applied.quarantine_id)

    assert bundle.members == (bad,)
    assert bundle.is_reverted
    assert bundle.reverted_at == _REVERTED
    assert bundle.reverted_by == tenant.actor_id


@pytest.mark.asyncio
async def test_an_unknown_quarantine_is_a_refusal_and_not_an_empty_bundle(tenant: _Tenant) -> None:
    """ "We withheld nothing" and "no such quarantine" are very different answers
    to give somebody, and an empty bundle says the first while meaning the
    second."""
    with pytest.raises(NotFoundError, match="no quarantine"):
        await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_an_auditor_may_export_and_a_consumer_may_not(tenant: _Tenant) -> None:
    """Who may read the ledger, and the asymmetry that is the point.

    An auditor who cannot see what was withheld cannot check the operator who
    withheld it, so the export role set is wider than the apply role set. It is
    wider in one direction only: this grants no ability to withhold anything.
    """
    await tenant.claim(run="run-42")
    applied = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="connector emitted nonsense"
    )

    as_auditor = await tenant.evidence().bundle_for(tenant.ctx(roles=["auditor"]), quarantine_id=applied.quarantine_id)
    assert as_auditor.quarantine_id == applied.quarantine_id

    with pytest.raises(PermissionError, match="requires one of"):
        await tenant.evidence().bundle_for(tenant.ctx(roles=["consumer"]), quarantine_id=applied.quarantine_id)


@pytest.mark.asyncio
async def test_the_bundle_reports_a_ledger_count_that_disagrees_with_its_members(
    tenant: _Tenant, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`matched_count` and `members` are both returned, not one derived.

    They should always agree. If they ever do not, that disagreement is the
    finding an incident review needs to see, and a bundle that computed the
    count from the list could not show it. Forced here by deleting a membership
    row behind the service's back — the only way to produce the state.
    """
    await tenant.claim(run="run-42")
    await tenant.claim(run="run-42")
    applied = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="connector emitted nonsense"
    )
    assert applied.matched_count == 2

    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM claim_quarantine_members WHERE quarantine_id = :q AND claim_id = :c"),
            {"q": applied.quarantine_id, "c": applied.matched[0]},
        )

    bundle = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=applied.quarantine_id)

    assert bundle.matched_count == 2
    assert len(bundle.members) == 1


@pytest.mark.asyncio
async def test_withheld_receipts_are_scoped_to_this_quarantine(tenant: _Tenant) -> None:
    """A receipt withheld by a *different* quarantine belongs in that one's
    bundle and not this one. Two quarantines in the same tenant is the case a
    tenant filter alone would pass and a `withheld_by` filter catches."""
    await tenant.claim(run="run-42")
    await tenant.claim(run="run-43")
    first = await tenant.quarantine().apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="the first incident"
    )
    second = await tenant.quarantine(now=_REVERTED).apply(
        tenant.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-43", reason="a separate incident"
    )

    mine = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=first.quarantine_id)
    theirs = await tenant.evidence().bundle_for(tenant.ctx(), quarantine_id=second.quarantine_id)

    assert {r.receipt_id for r in mine.withheld_receipts}.isdisjoint({r.receipt_id for r in theirs.withheld_receipts})
    assert set(mine.members).isdisjoint(set(theirs.members))
