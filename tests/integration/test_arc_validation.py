"""Integration tests for field provenance, conditional validation, and
semantic tests, against a real Postgres.

What the unit suites (`tests/unit/test_arc_provenance.py`,
`tests/unit/test_arc_semantic_tests.py`) cannot prove with a fake session:
that `ProvenanceService.edit`'s per-field upsert actually holds against the
real `arc_authoring_field_provenance` primary key when a second `PATCH`
touches a different field, and that the full edit -> validate ->
semantic-tests sequence this task's contract names holds end to end
against real rows.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.proposal import ProposalService
from registry.arc.service.provenance import ProvenanceService
from registry.arc.service.semantic_tests import SemanticTestService
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


def _ctx(*, tenant_id: uuid.UUID) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=_OPERATOR)
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


def _provenance_service(factory: async_sessionmaker[AsyncSession]) -> ProvenanceService:
    return ProvenanceService(factory, authorization=_authorization(), clock=FakeClock(_NOW))


def _semantic_test_service(factory: async_sessionmaker[AsyncSession]) -> SemanticTestService:
    return SemanticTestService(factory, authorization=_authorization(), clock=FakeClock(_NOW))


async def _open_version(factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID) -> tuple[uuid.UUID, int]:
    """A brand-new family with no baseline -- the shape the edit/validate
    end-to-end test and the per-field survival test both need. The
    baseline-matching test below builds its own version inline instead,
    since it needs a real revision seeded *before* the version opens."""
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    version = await _proposal_service(factory).open_proposal(
        _ctx(tenant_id=tenant_id), artifact_id=artifact_id, source_evidence_id=source_evidence_id
    )
    return version.proposal_id, version.proposal_version


async def _seed_bare_revision(factory: async_sessionmaker[AsyncSession], artifact_id: uuid.UUID) -> uuid.UUID:
    """Same minimal shape `test_arc_proposal_concurrency.py`'s own helper
    builds -- a revision row with no body, just enough for a foreign key
    and for `arc_applicability_rules` to attach a real rule to."""
    revision_id = uuid.uuid4()
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, created_at"
                ") VALUES ("
                "  :rid, :aid, NULL, 'test-system', :locator, :revision_locator, :digest, 'active', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "revision_locator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": now - datetime.timedelta(days=1),
                "review": now + datetime.timedelta(days=365),
                "retention": now + datetime.timedelta(days=730),
                "now": now,
            },
        )
    return revision_id


async def _seed_applicability_rule(
    factory: async_sessionmaker[AsyncSession], *, revision_id: uuid.UUID, task_kinds: list[str] | None
) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  rule_id, revision_id, tenant_id, scope, task_kinds, effective_from, is_mandatory, created_at"
                ") VALUES (:rid, :revid, NULL, 'global', :task_kinds, :efrom, TRUE, :now)"
            ),
            {
                "rid": uuid.uuid4(),
                "revid": revision_id,
                "task_kinds": task_kinds,
                "efrom": now - datetime.timedelta(days=1),
                "now": now,
            },
        )


# ---------------------------------------------------------------------------
# End-to-end: edit -> validate -> semantic-tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_then_validate_then_semantic_tests_end_to_end(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"validation-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)

    edited = await _provenance_service(factory).edit(
        ctx,
        proposal_id,
        proposal_version,
        entries=[
            {
                "field_path": "$.directives[0]",
                "provenance_class": "human_judgment",
                "source_evidence_id": None,
                "source_anchor": None,
                "excerpt_digest": None,
                "author_role": "reviewer",
                "derivation_profile": None,
            }
        ],
    )
    assert edited[0].author_issuer == _ISSUER
    assert edited[0].author_subject == _OPERATOR

    validation = await _provenance_service(factory).revalidate_stored(ctx, proposal_id, proposal_version)
    assert validation.valid is True

    results = await _semantic_test_service(factory).run(
        ctx,
        proposal_id,
        proposal_version,
        tests=[{"test_id": "t1", "manifest": {"task_kind": ["code_change"]}}],
    )
    # No reviewed baseline on this brand-new family, so nothing has been
    # shown to cover the predicate -- see semantic_tests.py's own
    # docstring for why an empty candidate reports unmatched rather than
    # fabricating coverage.
    assert results[0].actual == {"matched": False}
    assert results[0].passed is False

    stored = await _semantic_test_service(factory).list_for_version(ctx, proposal_id, proposal_version)
    assert [r.test_id for r in stored] == ["t1"]


@pytest.mark.asyncio
async def test_semantic_tests_match_against_a_reviewed_baselines_live_rules(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"semtest-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    revision_id = await _seed_bare_revision(factory, artifact_id)
    await _seed_applicability_rule(factory, revision_id=revision_id, task_kinds=["code_change"])
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    version = await _proposal_service(factory).open_proposal(
        _ctx(tenant_id=tenant_id),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=revision_id,
    )
    ctx = _ctx(tenant_id=tenant_id)

    covered = await _semantic_test_service(factory).run(
        ctx,
        version.proposal_id,
        version.proposal_version,
        tests=[{"test_id": "covered", "manifest": {"task_kind": ["code_change"]}}],
    )
    assert covered[0].actual == {"matched": True}
    assert covered[0].passed is True

    uncovered = await _semantic_test_service(factory).run(
        ctx,
        version.proposal_id,
        version.proposal_version,
        tests=[{"test_id": "uncovered", "manifest": {"task_kind": ["deployment"]}}],
    )
    assert uncovered[0].actual == {"matched": False}
    assert uncovered[0].passed is False


# ---------------------------------------------------------------------------
# Per-field survival against the real primary key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_one_field_does_not_disturb_a_siblings_persisted_row(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"survival-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    service = _provenance_service(factory)

    # A real source-evidence row to bind field A's citation to --
    # `arc_authoring_field_provenance.source_evidence_id` is a real foreign
    # key, not a bare UUID.
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    await service.edit(
        ctx,
        proposal_id,
        proposal_version,
        entries=[
            {
                "field_path": "$.a",
                "provenance_class": "source_backed",
                "source_evidence_id": source_evidence_id,
                "source_anchor": "p1",
                "excerpt_digest": "1" * 64,
                "author_role": None,
                "derivation_profile": None,
            }
        ],
    )

    await service.edit(
        ctx,
        proposal_id,
        proposal_version,
        entries=[
            {
                "field_path": "$.b",
                "provenance_class": "server_derived",
                "source_evidence_id": None,
                "source_anchor": None,
                "excerpt_digest": None,
                "author_role": None,
                "derivation_profile": "risk_engine_v1",
            }
        ],
    )

    rows = await service.list_for_version(ctx, proposal_id, proposal_version)
    by_path = {r.field_path: r for r in rows}
    assert by_path["$.a"].source_anchor == "p1"
    assert by_path["$.b"].derivation_profile == "risk_engine_v1"

    async with factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_authoring_field_provenance "
                    "WHERE proposal_id = :pid AND proposal_version = :pv"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).scalar()
    assert count == 2, "each field_path is its own row; editing one must never collapse or duplicate the other"
