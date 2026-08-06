"""Integration tests for `ArtifactMaterialisationService.submit`, against a
real Postgres.

Three things a fake session cannot prove (`tests/unit/test_arc_
materialisation.py` covers everything else): that a refused submit leaves
the database byte-identical, not merely "the row I checked happens to
match"; that the whole materialisation transaction actually commits a
schema-valid `arc_revisions` row and the bijection link when both
collaborators are present (a real database enforcing every `CHECK` and
`UNIQUE` constraint along the way); and that two truly concurrent submits on
the same version resolve to exactly one winner -- the bijection race
`AAS-T09` deferred to this task.

Every submission call here injects test-double collaborators directly into
`ArtifactMaterialisationService`'s constructor; no production wiring
anywhere in this deployment supplies real ones yet (see the service's own
module docstring), so there is no way to exercise the enabled transaction
through the router or `wiring.services` as they exist today.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.proposal import ProposalService, ProposalStateConflict
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.submission import (
    ArtifactMaterialisationService,
    SubmissionPrerequisiteUnavailable,
    SubmissionResult,
)
from registry.arc.types import ArcRequestContext
from registry.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, tenant_id: uuid.UUID, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=_OPERATOR)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))


def _proposal_service(factory: async_sessionmaker[AsyncSession]) -> ProposalService:
    return ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))


def _materialisation_service(
    factory: async_sessionmaker[AsyncSession], *, enabled: bool
) -> ArtifactMaterialisationService:
    """*enabled=False* matches every real deployment today: neither
    collaborator wired, matching `wiring.services._wire_arc`'s own
    unconditional construction. *enabled=True* injects two bare sentinel
    objects -- not a shape either real collaborator will eventually have,
    only enough to clear the presence guard, so the transaction beneath it
    can be proven against a real database before either task exists."""
    return ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=object() if enabled else None,
        risk_envelope_validator=object() if enabled else None,
    )


async def _open_proposal(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, artifact_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Opens a real proposal version through `ProposalService`, the same
    production path a client uses. Returns `(proposal_id, source_evidence_id)`;
    the opened version is always `proposal_version == 1`.
    """
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    service = _proposal_service(factory)
    version = await service.open_proposal(
        _ctx(tenant_id=tenant_id), artifact_id=artifact_id, source_evidence_id=source_evidence_id
    )
    return version.proposal_id, source_evidence_id


def _candidate(*, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> dict[str, object]:
    """A minimal, valid `arc_artifact_semantics_v1` candidate -- carries no
    directives or applicability rules, so no `field_provenance` entry is
    conditionally required for it, and this test can persist it with
    `queries.proposal.update_semantics` directly rather than going through
    `ProvenanceService.edit`'s own validation, which is a different task's
    test surface (`tests/integration/test_arc_validation.py`).
    """
    return {
        "profile": "arc_artifact_semantics_v1",
        "projection_schema_version": 1,
        "materialiser_profile": "test-materialiser",
        "materialiser_version": "0.0.1",
        "applicability_baseline_version": "0",
        "artifact_id": str(artifact_id),
        "revision_id": str(revision_id),
        "kind": "directive_bundle",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "visibility": "standard",
        "source_system": "confluence",
        "source_revision_locator": f"conf://space/page@{revision_id.hex[:8]}",
        "source_content_digest": "1" * 64,
        "source_approval_evidence_digest": "2" * 64,
        "directives": [],
        "applicability": [],
        "detail_audience": "agent_only",
        "review_expires_at": (_NOW + datetime.timedelta(days=365)).isoformat(),
        "content_classification": "internal",
        "approved_retention_floor_days": 730,
        "initial_freshness_basis": "revision_pinned_only",
        "reviewed_baseline_revision_id": None,
    }


async def _persist_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    candidate: dict[str, object],
) -> None:
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=proposal_id, proposal_version=proposal_version, semantics=candidate
        )


async def _version_snapshot(factory: async_sessionmaker[AsyncSession], *, proposal_id: uuid.UUID) -> tuple[object, ...]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
                    "       reviewed_baseline_revision_id, revision_id, risk_classification,"
                    "       risk_algorithm_version, opened_by_issuer, opened_by_subject, created_at, frozen_at,"
                    "       terminal_reason_code, terminal_note, terminal_by_issuer, terminal_by_subject,"
                    "       terminalized_at, semantics "
                    "FROM arc_authoring_proposal_versions WHERE proposal_id = :pid"
                ),
                {"pid": proposal_id},
            )
        ).one()
    return tuple(row)


async def _revision_count(factory: async_sessionmaker[AsyncSession], *, artifact_id: uuid.UUID) -> int:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_revisions WHERE artifact_id = :aid"), {"aid": artifact_id}
            )
        ).scalar()  # type: ignore[return-value]


async def _audit_outbox_count(factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID) -> int:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_audit_outbox WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).scalar()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The defining proof: refused before any write, database byte-identical.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_refuses_and_the_database_is_byte_identical(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Neither collaborator is wired -- the shape of every real deployment
    today. `submit` must refuse with `SubmissionPrerequisiteUnavailable`
    *before* the proposal-version row, the revision table, or the audit
    outbox are touched at all: a full snapshot of the affected row and two
    isolated counts, taken before and after the call, must be identical.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-refuse-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    candidate = _candidate(artifact_id=artifact_id, revision_id=uuid.uuid4())
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    before_row = await _version_snapshot(factory, proposal_id=proposal_id)
    before_revisions = await _revision_count(factory, artifact_id=artifact_id)
    before_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    service = _materialisation_service(factory, enabled=False)
    with pytest.raises(SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    after_row = await _version_snapshot(factory, proposal_id=proposal_id)
    after_revisions = await _revision_count(factory, artifact_id=artifact_id)
    after_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    assert after_row == before_row, "the proposal-version row must not change at all on refusal"
    assert after_revisions == before_revisions == 0, "no draft revision may exist after a refused submit"
    assert after_audit == before_audit, "no audit event may be written for a refused submit"


@pytest.mark.asyncio
async def test_submit_refuses_even_with_no_candidate_ever_patched(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The prerequisite guard fires before `submit` ever reads the
    persisted candidate, so a version nobody has `PATCH`ed yet refuses
    identically to one that has -- there is no path through this method
    that reaches `CandidateSemanticsMissing` while the two collaborators
    are unwired.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-refuse2-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)

    service = _materialisation_service(factory, enabled=False)
    with pytest.raises(SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    assert await _revision_count(factory, artifact_id=artifact_id) == 0


# ---------------------------------------------------------------------------
# End-to-end submit, once both collaborators are present.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_end_to_end_materialises_a_real_revision(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-e2e-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    revision_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id)
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    service = _materialisation_service(factory, enabled=True)
    result = await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    assert result.revision_id == revision_id
    async with factory() as session:
        revision = (
            await session.execute(
                text(
                    "SELECT artifact_id, tenant_id, lifecycle_state, source_system, source_revision_locator,"
                    "       content_digest, content_classification, freshness_basis "
                    "FROM arc_revisions WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
        version = (
            await session.execute(
                text(
                    "SELECT state, frozen_at, revision_id FROM arc_authoring_proposal_versions "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).one()
        audit_rows = (
            await session.execute(
                text("SELECT event_type FROM arc_audit_outbox WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).all()

    assert revision.artifact_id == artifact_id
    assert revision.tenant_id == tenant_id
    assert revision.lifecycle_state == "draft"
    assert revision.source_system == "confluence"
    assert revision.content_classification == "internal"
    assert revision.freshness_basis == "revision_pinned_only"
    assert version.state == "submitted"
    assert version.revision_id == revision_id
    assert version.frozen_at is not None
    assert any(row.event_type == "arc.proposal.submitted" for row in audit_rows)
    # Idempotency of the bijection itself: `source_evidence_id` is untouched
    # by submission -- it names what the proposal was authored from, not
    # anything submission writes.
    async with factory() as session:
        source_still_bound = (
            await session.execute(
                text(
                    "SELECT source_evidence_id FROM arc_authoring_proposal_versions "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).scalar()
    assert source_still_bound == source_evidence_id


@pytest.mark.asyncio
async def test_a_second_submit_on_the_same_version_is_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Frozen-version immutability, sequentially: once submitted, the same
    version can never be submitted again -- `state = 'open'` is no longer
    true, and `frozen_at IS NOT NULL` fails the compare-and-swap's second
    half too.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-twice-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    candidate = _candidate(artifact_id=artifact_id, revision_id=uuid.uuid4())
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    service = _materialisation_service(factory, enabled=True)
    await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    with pytest.raises(ProposalStateConflict):
        await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    assert (
        await _revision_count(factory, artifact_id=artifact_id) == 1
    ), "the second attempt must not materialise a second revision"


# ---------------------------------------------------------------------------
# Concurrency: the bijection race deferred from AAS-T09.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_submit_resolves_to_exactly_one_winner(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Two truly concurrent `submit` calls against the same open version,
    each on its own connection (via its own `ArtifactMaterialisationService`
    instance sharing the same `factory`), must resolve to exactly one
    winner and one `ProposalStateConflict` -- never two draft revisions,
    and never a crash on either side. The proof this task calls for: a lock
    that is not raced is not known to hold.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-race-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    candidate = _candidate(artifact_id=artifact_id, revision_id=uuid.uuid4())
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    ctx = _ctx(tenant_id=tenant_id)

    async def _attempt() -> SubmissionResult | ProposalStateConflict:
        service = _materialisation_service(factory, enabled=True)
        try:
            return await service.submit(ctx, proposal_id, 1, expected_impact_envelope=object())
        except ProposalStateConflict as exc:
            return exc

    first, second = await asyncio.gather(_attempt(), _attempt())
    outcomes = [first, second]
    winners = [o for o in outcomes if not isinstance(o, ProposalStateConflict)]
    losers = [o for o in outcomes if isinstance(o, ProposalStateConflict)]
    assert len(winners) == 1, f"exactly one call must win the race, got {outcomes}"
    assert len(losers) == 1, f"exactly one call must lose the race, got {outcomes}"

    revision_count = await _revision_count(factory, artifact_id=artifact_id)
    assert revision_count == 1, "the race must leave exactly one materialised revision, not two or zero"

    async with factory() as session:
        version = (
            await session.execute(
                text(
                    "SELECT state, revision_id FROM arc_authoring_proposal_versions "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).one()
    assert version.state == "submitted"
    winner = winners[0]
    assert isinstance(winner, SubmissionResult)
    assert version.revision_id == winner.revision_id
