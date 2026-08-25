"""The scope cuts both ways, so a second obligation is seeded alongside.

E4-T7's own words: *"A bundle that quietly includes rows outside the case is a
disclosure; one that quietly omits rows inside it is an incomplete regulatory
filing. Neither can be checked by reading the output, so the scope predicate
belongs in the query and the test belongs on the boundary — a second case's rows
seeded alongside, and asserted absent."*

Every test here does that. The chain under test is the one the governing
decision named and E4-T5d made expressible:

    obligation -> binding -> external reference (incident) -> claim provenance

so a claim reaches a bundle only by citing an incident that obligation cites.
Two obligations citing two incidents is the case a tenant filter alone would
pass and this join catches.
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
from contextplane.service.governance.obligation_evidence import ObligationEvidenceService
from contextplane.service.governance.obligations import (
    MATERIALITY_UNCLASSIFIED,
    ReportingObligationService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 9, 0, tzinfo=datetime.UTC)
_SUMMARY = "A connector fetched customer records it was not scoped to read."


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"oev-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'op', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"oev-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"])


async def _incident(
    factory: async_sessionmaker[AsyncSession], ctx: TenantContext, *, external_id: str, kind: str = "incident"
) -> uuid.UUID:
    reference_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_external_references "
                "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                " classification, external_authority, collision_key) "
                "VALUES (:r, :t, 'pagerduty', 'prod', :k, :e, 'internal', 'pagerduty', :c)"
            ),
            {"r": reference_id, "t": ctx.tenant_id, "k": kind, "e": external_id, "c": reference_id.hex},
        )
    return reference_id


async def _claim_citing(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    incident: str,
    evidence_kind: str = "incident",
) -> uuid.UUID:
    """One claim whose provenance names an external incident."""
    claim_id, entity_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
            ),
            {"e": entity_id, "t": ctx.tenant_id, "n": f"cap-{entity_id.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                "  scorer_version, calibration_version, decay_half_life_days"
                ") VALUES ("
                "  :c, :t, :t, :a, :e, 'ref', 'owned_by_team', 'prose',"
                "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'staged', 'private',"
                "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                "  'scorer.v1', 'calib.v1', 30)"
            ),
            {"c": claim_id, "t": ctx.tenant_id, "a": ctx.actor_id, "e": entity_id, "now": _NOW},
        )
        await session.execute(
            text("INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) " "VALUES (:c, :k, :r)"),
            {"c": claim_id, "k": evidence_kind, "r": incident},
        )
    return claim_id


def _obligations(factory: async_sessionmaker[AsyncSession]) -> ReportingObligationService:
    return ReportingObligationService(factory, clock=FakeClock(_NOW))


def _evidence(factory: async_sessionmaker[AsyncSession]) -> ObligationEvidenceService:
    return ObligationEvidenceService(factory, obligations=_obligations(factory))


@pytest.mark.asyncio
async def test_the_bundle_reaches_a_claim_through_the_incident_it_cites(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The chain the decision named, end to end. Before E4-T5d there was no link
    at all, so an obligation-scoped bundle carried no evidence."""
    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)
    reference = await _incident(factory, ctx, external_id="INC-4417")
    await _obligations(factory).cite_incident(ctx, obligation_id=obligation.obligation_id, reference_id=reference)
    claim = await _claim_citing(factory, ctx, incident="INC-4417")

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    assert bundle.is_matched
    assert [i.external_id for i in bundle.incidents] == ["INC-4417"]
    assert [row["claim_id"] for row in bundle.citing_claims] == [str(claim)]
    assert bundle.obligation.summary == _SUMMARY


@pytest.mark.asyncio
async def test_a_second_obligations_evidence_is_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The disclosure direction, and the case a tenant filter alone would pass.

    Two obligations in one tenant, each citing its own incident, each with its
    own claim. A bundle that returned both would be indistinguishable from a
    correct one by reading it.
    """
    ctx = await _tenant(factory)
    service = _obligations(factory)
    mine = await service.nominate(ctx, summary=_SUMMARY)
    theirs = await service.nominate(ctx, summary="A separate matter entirely.")
    await service.cite_incident(
        ctx, obligation_id=mine.obligation_id, reference_id=await _incident(factory, ctx, external_id="INC-1")
    )
    await service.cite_incident(
        ctx, obligation_id=theirs.obligation_id, reference_id=await _incident(factory, ctx, external_id="INC-2")
    )
    my_claim = await _claim_citing(factory, ctx, incident="INC-1")
    their_claim = await _claim_citing(factory, ctx, incident="INC-2")

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=mine.obligation_id)

    found = {row["claim_id"] for row in bundle.citing_claims}
    assert str(my_claim) in found
    assert str(their_claim) not in found


@pytest.mark.asyncio
async def test_another_tenants_claim_citing_the_same_incident_id_is_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`memory_claim_provenance` carries no `tenant_id`, so the isolation is a
    join through `memory_claims` — the same trap `claim_quarantine_members` sets.

    Two tenants can hold the same upstream incident id, and their claims about
    it must not meet.
    """
    mine, theirs = await _tenant(factory), await _tenant(factory)
    service = _obligations(factory)
    obligation = await service.nominate(mine, summary=_SUMMARY)
    await service.cite_incident(
        mine,
        obligation_id=obligation.obligation_id,
        reference_id=await _incident(factory, mine, external_id="INC-SHARED"),
    )
    my_claim = await _claim_citing(factory, mine, incident="INC-SHARED")
    their_claim = await _claim_citing(factory, theirs, incident="INC-SHARED")

    bundle = await _evidence(factory).bundle_for(mine, obligation_id=obligation.obligation_id)

    found = {row["claim_id"] for row in bundle.citing_claims}
    assert found == {str(my_claim)}
    assert str(their_claim) not in found


@pytest.mark.asyncio
async def test_a_claim_citing_a_different_kind_of_evidence_is_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`evidence_kind` is part of the predicate, not decoration.

    A claim citing `INC-4417` as a `connector_run` is a claim about an ingest
    that happens to share a string with an incident id, and reporting it as
    evidence about the incident would be a coincidence presented as a finding.
    """
    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)
    await _obligations(factory).cite_incident(
        ctx,
        obligation_id=obligation.obligation_id,
        reference_id=await _incident(factory, ctx, external_id="INC-4417"),
    )
    await _claim_citing(factory, ctx, incident="INC-4417", evidence_kind="connector_run")

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    assert bundle.citing_claims == ()


@pytest.mark.asyncio
async def test_an_unmatched_obligation_says_so_rather_than_looking_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The omission direction's honest case.

    An obligation nobody has matched to a record is the state most of them start
    in — 0076 made `summary` free text precisely so a nomination need not wait
    for the link. A reader has to be able to tell that from a bundle that failed
    to find anything.
    """
    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    assert not bundle.is_matched
    assert bundle.incidents == ()
    assert bundle.citing_claims == ()
    assert bundle.obligation.materiality == MATERIALITY_UNCLASSIFIED


@pytest.mark.asyncio
async def test_an_unknown_obligation_is_a_refusal_and_not_an_empty_bundle(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "This obligation has no evidence" and "there is no such obligation" are
    different answers, and an empty bundle says the first while meaning the
    second."""
    ctx = await _tenant(factory)
    with pytest.raises(NotFoundError, match="reporting obligation"):
        await _evidence(factory).bundle_for(ctx, obligation_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_the_bundle_carries_no_deadline_and_no_computed_materiality(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """E4-T6 is blocked on ratified thresholds, and this must not quietly ship
    the half that needs them.

    The bundle reports the materiality **as recorded** — `unclassified` here,
    which is where most obligations sit — and carries no deadline, due date or
    at-risk state. Reading a row is not classifying one, and a placeholder
    presented as a compliance feature is worse than an absent one.
    """
    import dataclasses

    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    fields = {field.name for field in dataclasses.fields(bundle)}
    forbidden = {"deadline", "due_at", "at_risk", "report_window", "filed_at", "initial_deadline"}
    assert not fields & forbidden, f"the bundle carries {sorted(fields & forbidden)}; that is E4-T6"
    assert bundle.obligation.materiality == MATERIALITY_UNCLASSIFIED
    assert bundle.obligation.classified_at is None


@pytest.mark.asyncio
async def test_the_bundle_states_what_it_evidences_and_claims_nothing_more(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An exported document travels away from every docstring explaining it, so
    the caveat is a field. The word the decision on digest chains forbids does
    not appear."""
    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    assert "mutable rows" in bundle.provenance
    assert "repudiation" not in bundle.provenance.lower()


@pytest.mark.asyncio
async def test_the_bundle_names_claims_and_does_not_serve_what_they_assert(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ids, and the incident each cites. An export that served claim content
    would be a second read path for material the rest of the system governs
    carefully, and the question a bundle answers is *which* records bear on this
    obligation."""
    ctx = await _tenant(factory)
    obligation = await _obligations(factory).nominate(ctx, summary=_SUMMARY)
    await _obligations(factory).cite_incident(
        ctx,
        obligation_id=obligation.obligation_id,
        reference_id=await _incident(factory, ctx, external_id="INC-4417"),
    )
    await _claim_citing(factory, ctx, incident="INC-4417")

    bundle = await _evidence(factory).bundle_for(ctx, obligation_id=obligation.obligation_id)

    assert bundle.citing_claims
    for row in bundle.citing_claims:
        assert set(row) == {"claim_id", "incident"}
        assert "platform" not in str(row), "the bundle is carrying what a claim asserts"


def test_the_bundle_is_reachable_over_a_transport() -> None:
    """The gap this route closed: the service was the deliverable and nothing
    could call it.

    `ObligationEvidenceService` shipped wired into the container and reached by
    no route and no tool, so an export nobody could call was recorded as
    delivered. Asserted against the mounted route table rather than by calling,
    because a call also exercises authorization and a 403 would look like a pass
    — what is being checked is that the path exists at all.
    """
    from contextplane.api.routers import admin_obligations

    mounted = {
        (list(route.methods or {})[0], route.path)
        for route in admin_obligations.router.routes  # type: ignore[attr-defined]
    }

    assert (
        "GET",
        "/v1/admin/reporting-obligations/{obligation_id}:evidence",
    ) in mounted, f"the evidence export is not mounted; {sorted(mounted)}"


def test_the_export_serves_claim_ids_and_never_claim_content() -> None:
    """An export that inlined values would be a second serving path with none of
    the servability rules the real one applies — the same shape as the
    agent-performance read that was disclosing withheld content."""
    from contextplane.api.routers import admin_obligations

    fields = admin_obligations.ObligationEvidenceResponse.model_fields

    assert "citing_claims" in fields
    assert not any("value" in name for name in fields), "no field on the export may carry a claim value"
