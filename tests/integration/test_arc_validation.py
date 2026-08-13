"""Integration tests for field provenance, conditional validation, and
semantic tests, against a real Postgres.

What the unit suites (`tests/unit/test_arc_provenance.py`,
`tests/unit/test_arc_semantic_tests.py`) cannot prove with a fake session:
that `ProvenanceService.edit`'s per-field upsert actually holds against the
real `arc_authoring_field_provenance` primary key when a second `PATCH`
touches a different field, that a candidate document actually round-trips
through the real `arc_authoring_proposal_versions.semantics` column, that
an invalid `PATCH` leaves the real row and its sibling table byte-identical
to before, that `POST {PV}/semantic-tests` evaluates a real PATCHed
candidate rather than a real reviewed baseline's live rules, and that the
full edit -> validate -> semantic-tests sequence this task's contract names
holds end to end against real rows.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.proposal import ProposalService
from contextplane.arc.service.provenance import ProvenanceInvalid, ProvenanceService, SemanticsValidationFailed
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.service.semantic_tests import SemanticTestService
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_UUID1 = "00000000-0000-4000-8000-000000000001"
_UUID2 = "00000000-0000-4000-8000-000000000002"


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
    factory: async_sessionmaker[AsyncSession], *, revision_id: uuid.UUID, intent_kinds: list[str] | None
) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  rule_id, revision_id, tenant_id, scope, intent_kinds, effective_from, is_mandatory, created_at"
                ") VALUES (:rid, :revid, NULL, 'global', :intent_kinds, :efrom, TRUE, :now)"
            ),
            {
                "rid": uuid.uuid4(),
                "revid": revision_id,
                "intent_kinds": intent_kinds,
                "efrom": now - datetime.timedelta(days=1),
                "now": now,
            },
        )


def _candidate_rule(rule_id: str, *, intent_kinds: list[str] | None = None) -> dict[str, Any]:
    """One `applicability[]` element of a candidate `arc_artifact_semantics_v1`
    document -- every key present, matching `_APPLICABILITY_RULE_SCHEMA`'s
    "every declared property is required as a key" rule."""
    return {
        "rule_id": rule_id,
        "scope": "global",
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "intent_kinds": intent_kinds,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
        "is_mandatory": True,
    }


def _semantics_doc(*, applicability: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A minimal, closed-schema-conforming candidate document -- every field
    `_ARTIFACT_SEMANTICS_SCHEMA` requires, JSON-primitive-shaped exactly as
    `ProposalPatchRequest.semantics.model_dump(mode="json")` would produce
    it, so what this helper builds and what the real route hands
    `ProvenanceService.edit` are the same shape."""
    return {
        "profile": "arc_artifact_semantics_v2",
        "projection_schema_version": 1,
        "materialiser_profile": "directive_bundle_v1",
        "materialiser_version": "1.0.0",
        "applicability_baseline_version": "1",
        "artifact_id": str(uuid.uuid4()),
        "revision_id": str(uuid.uuid4()),
        "kind": "directive_bundle",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "visibility": "standard",
        "source_system": "internal-docs",
        "source_revision_locator": "rev-1",
        "source_content_digest": "1" * 64,
        "source_approval_evidence_digest": "2" * 64,
        "directives": [],
        "applicability": applicability if applicability is not None else [],
        "detail_audience": "agent_and_human",
        "review_expires_at": "2026-06-01T00:00:00Z",
        "content_classification": "internal",
        "approved_retention_floor_days": 90,
        "initial_freshness_basis": "connector_verified",
        "reviewed_baseline_revision_id": None,
    }


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
        tests=[{"test_id": "t1", "manifest": {"intent_kind": ["code_change"]}}],
    )
    # No candidate has been PATCHed onto this version yet (`semantics IS
    # NULL`), so nothing has been shown to cover the predicate -- see
    # semantic_tests.py's own docstring for why an absent candidate
    # reports unmatched rather than fabricating coverage from anywhere
    # else, the reviewed baseline included.
    assert results[0].actual == {"matched": False}
    assert results[0].passed is False

    stored = await _semantic_test_service(factory).list_for_version(ctx, proposal_id, proposal_version)
    assert [r.test_id for r in stored] == ["t1"]


@pytest.mark.asyncio
async def test_semantic_tests_evaluate_the_candidate_not_the_reviewed_baseline(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The correctness fix this task exists for, proven against a real
    reviewed baseline's live `arc_applicability_rules` and a real PATCHed
    candidate: `POST {PV}/semantic-tests` must evaluate the candidate, not
    the baseline. Two predicates, two directions -- one the candidate
    covers and the baseline does not, and the reverse -- so a service that
    fell back to the baseline in either direction fails one assertion or
    the other. A service that evaluated both identically (today's wrong
    behavior) would pass neither `candidate_covers_baseline_does_not` nor
    correctly refuse `baseline_covers_candidate_does_not`.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"discriminate-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    revision_id = await _seed_bare_revision(factory, artifact_id)
    await _seed_applicability_rule(factory, revision_id=revision_id, intent_kinds=["code_change"])
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    version = await _proposal_service(factory).open_proposal(
        _ctx(tenant_id=tenant_id),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=revision_id,
    )
    ctx = _ctx(tenant_id=tenant_id)

    # The candidate's own applicability disagrees with the baseline
    # seeded above: it covers "deployment", not "code_change".
    candidate = _semantics_doc(applicability=[_candidate_rule(_UUID1, intent_kinds=["deployment"])])
    await _provenance_service(factory).edit(
        ctx, version.proposal_id, version.proposal_version, semantics=candidate, entries=[]
    )

    results = await _semantic_test_service(factory).run(
        ctx,
        version.proposal_id,
        version.proposal_version,
        tests=[
            {"test_id": "candidate_covers_baseline_does_not", "manifest": {"intent_kind": ["deployment"]}},
            {"test_id": "baseline_covers_candidate_does_not", "manifest": {"intent_kind": ["code_change"]}},
        ],
    )
    by_id = {r.test_id: r for r in results}
    assert by_id["candidate_covers_baseline_does_not"].actual == {"matched": True}
    assert by_id["candidate_covers_baseline_does_not"].passed is True
    assert by_id["baseline_covers_candidate_does_not"].actual == {"matched": False}
    assert by_id["baseline_covers_candidate_does_not"].passed is False


# ---------------------------------------------------------------------------
# PATCH persists the candidate; an invalid PATCH writes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_persists_the_candidate_semantics_document(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Round trip: what `PATCH` validates and what a fresh read of the row
    returns must be the same document."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"persist-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    candidate = _semantics_doc(applicability=[_candidate_rule(_UUID1, intent_kinds=["code_change"])])

    await _provenance_service(factory).edit(ctx, proposal_id, proposal_version, semantics=candidate, entries=[])

    async with factory() as session:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
    assert version is not None
    assert version.semantics == candidate


@pytest.mark.asyncio
async def test_patch_refuses_an_invalid_candidate_without_writing_the_valid_field_provenance(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A `PATCH` batching a valid `field_provenance` entry with an invalid
    candidate must write neither -- not the entry, and not the (absent)
    candidate. The row stays exactly as it was before the call, the same
    "byte-identical after refusal" property `test_arc_submission.py`
    requires of its own refusal path."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"noparital-a-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    invalid_candidate = _semantics_doc()
    del invalid_candidate["materialiser_version"]  # missing required field

    with pytest.raises(SemanticsValidationFailed):
        await _provenance_service(factory).edit(
            ctx,
            proposal_id,
            proposal_version,
            semantics=invalid_candidate,
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

    async with factory() as session:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
        assert version is not None
        assert version.semantics is None, "an invalid candidate must leave the row exactly as it was"
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_authoring_field_provenance "
                    "WHERE proposal_id = :pid AND proposal_version = :pv"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).scalar()
    assert count == 0, "the entry batched alongside the bad candidate must not have been written either"


@pytest.mark.asyncio
async def test_patch_refuses_an_invalid_field_provenance_entry_without_writing_the_valid_candidate(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The reverse batching: a valid candidate alongside an invalid
    `field_provenance` entry must also write neither."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"noparital-b-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    valid_candidate = _semantics_doc(applicability=[_candidate_rule(_UUID1, intent_kinds=["code_change"])])

    with pytest.raises(ProvenanceInvalid):
        await _provenance_service(factory).edit(
            ctx,
            proposal_id,
            proposal_version,
            semantics=valid_candidate,
            entries=[
                {
                    "field_path": "$.a",
                    "provenance_class": "source_backed",
                    "source_evidence_id": source_evidence_id,
                    # source_anchor/excerpt_digest missing -> source_backed
                    # requires both.
                    "source_anchor": None,
                    "excerpt_digest": None,
                    "author_role": None,
                    "derivation_profile": None,
                }
            ],
        )

    async with factory() as session:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
        assert version is not None
        assert version.semantics is None, "the valid candidate must not have been written when its sibling entry failed"
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_authoring_field_provenance "
                    "WHERE proposal_id = :pid AND proposal_version = :pv"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# validate revalidates the persisted candidate, not a transient one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_revalidates_the_persisted_candidate_not_a_transient_one(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A candidate that was valid when `PATCH` wrote it is re-checked
    against what is *currently* persisted, not against a copy `PATCH`
    validated once and never looked at again. Simulated the same way
    `test_arc_provenance.py` proves the analogous property for
    `field_provenance` rows: a direct write that bypasses `edit()`'s own
    guard, standing in for any future path that could leave the row in a
    shape `PATCH` itself would have refused."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"revalidate-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    candidate = _semantics_doc(applicability=[_candidate_rule(_UUID1, intent_kinds=["code_change"])])

    await _provenance_service(factory).edit(ctx, proposal_id, proposal_version, semantics=candidate, entries=[])
    first = await _provenance_service(factory).revalidate_stored(ctx, proposal_id, proposal_version)
    assert first.valid is True

    # Two distinct rule_ids (ascending, so the reused ordering check has
    # nothing to object to) sharing one identical selector: legal by
    # closed-schema, illegal by the ambiguous-selector rule
    # `validate_candidate_semantics` also enforces.
    ambiguous = dict(candidate)
    ambiguous["applicability"] = [
        _candidate_rule(_UUID1, intent_kinds=["code_change"]),
        _candidate_rule(_UUID2, intent_kinds=["code_change"]),
    ]
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_authoring_proposal_versions SET semantics = CAST(:semantics AS JSONB) "
                "WHERE proposal_id = :pid AND proposal_version = :pv"
            ),
            {"semantics": json.dumps(ambiguous), "pid": proposal_id, "pv": proposal_version},
        )

    second = await _provenance_service(factory).revalidate_stored(ctx, proposal_id, proposal_version)
    assert second.valid is False
    assert any(e.code == "arc_proposal_validation_failed" for e in second.errors)


# ---------------------------------------------------------------------------
# `semantic_tests.py`'s frozen-input binding (`arc_authoring_semantic_tests`
# stores the frozen inputs/results at test-run time), re-verified against
# the real table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_test_rerun_with_a_changed_manifest_overwrites_the_frozen_row_in_the_real_table(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Same `test_id`, changed manifest, real primary key: the second run
    overwrites the first's frozen row in place -- exactly one row, no
    stale result beside it that a later read could mistake for still
    describing the new input."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"freeze-{uuid.uuid4().hex[:8]}")
    proposal_id, proposal_version = await _open_version(factory, tenant_id=tenant_id)
    ctx = _ctx(tenant_id=tenant_id)
    candidate = _semantics_doc(applicability=[_candidate_rule(_UUID1, intent_kinds=["code_change"])])
    await _provenance_service(factory).edit(ctx, proposal_id, proposal_version, semantics=candidate, entries=[])

    await _semantic_test_service(factory).run(
        ctx, proposal_id, proposal_version, tests=[{"test_id": "t1", "manifest": {"intent_kind": ["code_change"]}}]
    )
    await _semantic_test_service(factory).run(
        ctx, proposal_id, proposal_version, tests=[{"test_id": "t1", "manifest": {"intent_kind": ["deployment"]}}]
    )

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT manifest, actual FROM arc_authoring_semantic_tests "
                    "WHERE proposal_id = :pid AND proposal_version = :pv AND test_id = 't1'"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).all()
    assert len(rows) == 1, "the second run must overwrite the frozen row in place, not accumulate a second one"
    assert rows[0].manifest["intent_kind"] == ["deployment"]
    assert rows[0].actual == {"matched": False}


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
