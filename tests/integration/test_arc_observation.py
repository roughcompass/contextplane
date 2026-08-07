"""Integration tests for the five observation tables and their two
workers, against real Postgres.

What a fake session cannot prove (the reason this file exists rather than
staying at the unit tier): that `arc_observation_qualifications`'s
eight-column binding tuple is genuinely `UNIQUE NULLS NOT DISTINCT` and not
an ordinary `UNIQUE` that would silently let every unaccepted "exact
retry" insert a duplicate row whenever a nullable member is `NULL`; that
`arc_observation_cohort_members`'s primary key is genuinely the composite
`(cohort_id, tenant_id)` and not accidentally over-constrained on either
column alone; that `load_aggregate_counters` cannot be used to recover a
cohort's per-tenant membership, against a real multi-tenant read; and that
both workers are reachable through the exact scheduler registration a
deployment runs, not only through a worker object a test constructed
directly.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.queries import observation as obs_queries
from registry.arc.workers.observation_fingerprint_reaper import (
    RETENTION_WINDOW,
    ObservationFingerprintReaperWorker,
)
from registry.arc.workers.observation_window_evaluator import ObservationWindowEvaluatorWorker
from registry.main import create_app
from tests.helpers.arc_fixtures import seed_artifact_family
from tests.helpers.auth_harness import default_settings
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_revision(
    session: AsyncSession, *, tenant_id: uuid.UUID | None, artifact_id: uuid.UUID, lifecycle_state: str = "draft"
) -> uuid.UUID:
    revision_id = uuid.uuid4()
    # `ck_arc_revisions_no_global_plaintext`: a global (tenant_id IS NULL)
    # revision may never carry plaintext body content.
    plaintext = "body" if tenant_id is not None else None
    await session.execute(
        text(
            "INSERT INTO arc_revisions (revision_id, artifact_id, tenant_id, source_system, "
            " source_canonical_locator, source_revision_locator, content_digest, lifecycle_state, "
            " effective_from, review_expires_at, detail_audience, freshness_basis, content_classification, "
            " content_retention_until, content_storage_mode, source_body_plaintext, created_at) "
            "VALUES (:rid, :aid, :tid, 'test', :loc, :revloc, :digest, :state, :efrom, :review, "
            " 'all_matched_actors', 'revision_pinned_only', 'internal', :retention, 'none', :plaintext, :now)"
        ),
        {
            "rid": revision_id,
            "aid": artifact_id,
            "tid": tenant_id,
            "loc": f"loc://{revision_id.hex[:8]}",
            "revloc": f"loc://{revision_id.hex[:8]}@1",
            "digest": revision_id.hex + revision_id.hex,
            "state": lifecycle_state,
            "efrom": _NOW - datetime.timedelta(days=1),
            "review": _NOW + datetime.timedelta(days=365),
            "retention": _NOW + datetime.timedelta(days=730),
            "plaintext": plaintext,
            "now": _NOW,
        },
    )
    return revision_id


async def _seed_proposal_version(
    session: AsyncSession, *, artifact_id: uuid.UUID, tenant_id: uuid.UUID | None, revision_id: uuid.UUID
) -> tuple[uuid.UUID, int]:
    """A bare `arc_authoring_proposals`/`arc_authoring_proposal_versions`
    row pair -- the FK target every cohort/qualification row needs.
    Inserted directly (not through `ProposalService`/`submit`): these
    tests exercise the observation tables' own constraints, not the
    submission pipeline that would normally populate them.
    """
    proposal_id = uuid.uuid4()
    source_evidence_id = uuid.uuid4()
    policy_id = f"seed-policy-{uuid.uuid4().hex[:8]}"
    scope = "global" if tenant_id is None else "tenant"
    await session.execute(
        text(
            "INSERT INTO arc_source_upload_policies (policy_id, owning_scope, tenant_id, allowed_media_types, "
            " allowed_verifier_ids, max_bytes) VALUES (:pid, :scope, :tid, ARRAY['text/markdown'], "
            " ARRAY['verifier-1'], 1024)"
        ),
        {"pid": policy_id, "scope": scope, "tid": tenant_id},
    )
    await session.execute(
        text(
            "INSERT INTO arc_source_bodies (source_evidence_id, content_digest, content_bytes, body, created_at) "
            "VALUES (:sid, :digest, 4, :body, :now)"
        ),
        {"sid": source_evidence_id, "digest": "0" * 64, "body": b"test", "now": _NOW},
    )
    await session.execute(
        text(
            "INSERT INTO arc_source_approval_evidence (source_evidence_id, owning_scope, tenant_id, "
            " source_system, source_revision_locator, source_content_type, source_content_digest, claim, "
            " claim_digest, verification_method, verifier_id, signature, admission_method, policy_id, "
            " admitted_at, admitted_by_issuer, admitted_by_subject, verified_at, expires_at, "
            " idempotency_key_digest, admission_request_payload_digest, idempotency_scope_digest) "
            "VALUES (:sid, :scope, :tid, 'test-system', :locator, 'text/markdown', :digest, CAST('{}' AS JSONB), "
            " :cdigest, 'source_signed', 'verifier-1', 'c2lnbmF0dXJl', 'authorized_upload', :pid, :now, "
            " 'https://idp.example.test', 'seed', :now, :expires, :kdigest, :pdigest, :sdigest)"
        ),
        {
            "sid": source_evidence_id,
            "scope": scope,
            "tid": tenant_id,
            "locator": f"loc://{source_evidence_id.hex[:8]}",
            "digest": "0" * 64,
            "cdigest": "1" * 64,
            "pid": policy_id,
            "now": _NOW,
            "expires": _NOW + datetime.timedelta(days=365),
            "kdigest": "2" * 64,
            "pdigest": "3" * 64,
            "sdigest": uuid.uuid4().hex + uuid.uuid4().hex,
        },
    )
    await session.execute(
        text("INSERT INTO arc_authoring_proposals (proposal_id, artifact_id, created_at) VALUES (:pid, :aid, :now)"),
        {"pid": proposal_id, "aid": artifact_id, "now": _NOW},
    )
    await session.execute(
        text(
            "INSERT INTO arc_authoring_proposal_versions (proposal_id, proposal_version, artifact_id, tenant_id, "
            " state, source_evidence_id, revision_id, opened_by_issuer, opened_by_subject, created_at, frozen_at) "
            "VALUES (:pid, 1, :aid, :tid, 'approved', :sid, :rid, 'https://idp.example.test', 'submitter', :now, "
            " :now)"
        ),
        {
            "pid": proposal_id,
            "aid": artifact_id,
            "tid": tenant_id,
            "sid": source_evidence_id,
            "rid": revision_id,
            "now": _NOW,
        },
    )
    return proposal_id, 1


async def _seed_cohort(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    candidate_revision_id: uuid.UUID,
    window_started_at: datetime.datetime = _NOW,
    window_deadline: datetime.datetime | None = None,
    closed_at: datetime.datetime | None = None,
    window_ended_at: datetime.datetime | None = None,
) -> uuid.UUID:
    cohort_id = uuid.uuid4()
    await obs_queries.insert_cohort(
        session,
        cohort_id=cohort_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        candidate_revision_id=candidate_revision_id,
        risk_classification="tenant_mandatory",
        scope_predicate_digest="a" * 64,
        tenant_membership_digest="b" * 64,
        eligibility_predicate_digest="c" * 64,
        frozen_at=window_started_at,
        window_started_at=window_started_at,
        window_deadline=window_deadline or (window_started_at + datetime.timedelta(hours=24)),
    )
    if closed_at is not None:
        await session.execute(
            text(
                "UPDATE arc_observation_cohorts SET closed_at = :closed_at, window_ended_at = :ended "
                "WHERE cohort_id = :cid"
            ),
            {"closed_at": closed_at, "ended": window_ended_at or closed_at, "cid": cohort_id},
        )
    return cohort_id


def _hex64() -> str:
    """A fresh 64-hex-char digest-shaped value, unique per call. The eight
    binding-tuple columns must never default to a literal shared across
    test functions -- rows this suite writes persist for the rest of the
    session (no rollback between tests), so two different tests reusing
    the same literal would collide on exactly the tuple this suite exists
    to test fresh each time."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _qualification_kwargs(
    *,
    cohort_id: uuid.UUID,
    candidate_revision_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    **overrides: object,
) -> dict[str, object]:
    base: dict[str, object] = {
        "qualification_id": uuid.uuid4(),
        "idempotency_key_digest": _hex64(),
        "candidate_review_package_digest": _hex64(),
        "candidate_revision_id": candidate_revision_id,
        "proposal_id": proposal_id,
        "proposal_version": proposal_version,
        "risk_classification": "tenant_mandatory",
        "risk_algorithm_version": "arc_risk_reducer_v1",
        "baseline_revision_id": None,
        "selection_engine_version": "arc_selection_v1",
        "engine_configuration_version": "arc_selection_config_v1",
        "cohort_id": cohort_id,
        "cohort_digest": _hex64(),
        "window_started_at": _NOW,
        "window_ended_at": _NOW + datetime.timedelta(hours=24),
        "eligible_count": 100,
        "observed_count": 100,
        "expected_impact_envelope_digest": _hex64(),
        "counters_by_delta_code": [],
        "unexplained_count": 0,
        "out_of_envelope_count": 0,
        "replay_corpus_digest": None,
        "replay_result_digest": None,
        "qualification_algorithm_version": "arc_observation_qualification_v1",
        "computed_decision": "qualified",
        "computed_at": _NOW,
        "reason_codes": ["window_met"],
    }
    base.update(overrides)
    return base


async def _insert_qualification_row(session: AsyncSession, **kwargs: object) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_observation_qualifications ("
            " qualification_id, idempotency_key_digest, candidate_review_package_digest, candidate_revision_id,"
            " proposal_id, proposal_version, risk_classification, risk_algorithm_version, baseline_revision_id,"
            " selection_engine_version, engine_configuration_version, cohort_id, cohort_digest, window_started_at,"
            " window_ended_at, eligible_count, observed_count, expected_impact_envelope_digest,"
            " counters_by_delta_code, unexplained_count, out_of_envelope_count, replay_corpus_digest,"
            " replay_result_digest, qualification_algorithm_version, computed_decision, computed_at, reason_codes"
            ") VALUES ("
            " :qualification_id, :idempotency_key_digest, :candidate_review_package_digest, :candidate_revision_id,"
            " :proposal_id, :proposal_version, :risk_classification, :risk_algorithm_version, :baseline_revision_id,"
            " :selection_engine_version, :engine_configuration_version, :cohort_id, :cohort_digest,"
            " :window_started_at, :window_ended_at, :eligible_count, :observed_count,"
            " :expected_impact_envelope_digest, CAST(:counters_by_delta_code AS JSONB), :unexplained_count,"
            " :out_of_envelope_count, :replay_corpus_digest, :replay_result_digest, :qualification_algorithm_version,"
            " :computed_decision, :computed_at, :reason_codes)"
        ),
        {**kwargs, "counters_by_delta_code": "[]"},
    )


@dataclasses.dataclass(frozen=True)
class _Scenario:
    tenant_id: uuid.UUID
    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    cohort_id: uuid.UUID


async def _build_scenario(factory: async_sessionmaker[AsyncSession], pg_url: str, *, slug: str) -> _Scenario:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_url, slug=slug)
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id, slug_prefix=slug)
    async with factory() as session, session.begin():
        revision_id = await _seed_revision(session, tenant_id=tenant_id, artifact_id=artifact_id)
        proposal_id, proposal_version = await _seed_proposal_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id, revision_id=revision_id
        )
        cohort_id = await _seed_cohort(
            session, proposal_id=proposal_id, proposal_version=proposal_version, candidate_revision_id=revision_id
        )
    return _Scenario(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        revision_id=revision_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        cohort_id=cohort_id,
    )


# ---------------------------------------------------------------------------
# UNIQUE (qualification_id) -- restated as its own constraint, per this
# task's contract; proven both directions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qualification_id_unique_rejects_a_duplicate_and_accepts_a_distinct_one(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="qual-id-unique")
    shared_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await _insert_qualification_row(
            session,
            **_qualification_kwargs(
                cohort_id=scenario.cohort_id,
                candidate_revision_id=scenario.revision_id,
                proposal_id=scenario.proposal_id,
                proposal_version=scenario.proposal_version,
                qualification_id=shared_id,
            ),
        )

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await _insert_qualification_row(
                session,
                **_qualification_kwargs(
                    cohort_id=scenario.cohort_id,
                    candidate_revision_id=scenario.revision_id,
                    proposal_id=scenario.proposal_id,
                    proposal_version=scenario.proposal_version,
                    qualification_id=shared_id,
                    cohort_digest=_hex64(),  # every binding-tuple column differs; only the id collides
                ),
            )

    async with factory() as session, session.begin():
        await _insert_qualification_row(
            session,
            **_qualification_kwargs(
                cohort_id=scenario.cohort_id,
                candidate_revision_id=scenario.revision_id,
                proposal_id=scenario.proposal_id,
                proposal_version=scenario.proposal_version,
                qualification_id=uuid.uuid4(),
                cohort_digest=_hex64(),
            ),
        )


# ---------------------------------------------------------------------------
# The eight-column binding tuple: UNIQUE NULLS NOT DISTINCT, proven in both
# the NULL-handling direction and the "every component matters" direction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_tuple_rejects_an_exact_duplicate_including_matching_nulls(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two nullable binding-tuple members (`baseline_revision_id`, `replay_
    corpus_digest`) are both `NULL` on the common path. A plain `UNIQUE`
    treats two `NULL`s as distinct from each other, so it would silently
    accept this second insert; `NULLS NOT DISTINCT` must refuse it."""
    scenario = await _build_scenario(factory, pg_container, slug="binding-null")
    kwargs = _qualification_kwargs(
        cohort_id=scenario.cohort_id,
        candidate_revision_id=scenario.revision_id,
        proposal_id=scenario.proposal_id,
        proposal_version=scenario.proposal_version,
        baseline_revision_id=None,
        replay_corpus_digest=None,
    )
    async with factory() as session, session.begin():
        await _insert_qualification_row(session, **kwargs)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await _insert_qualification_row(session, **{**kwargs, "qualification_id": uuid.uuid4()})


@pytest.mark.asyncio
async def test_binding_tuple_accepts_an_otherwise_identical_row_when_any_single_component_differs(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Changing exactly one of the eight columns at a time, holding the
    other seven fixed, must make an otherwise-identical row insertable --
    the direct proof that every named column genuinely participates in
    the constraint, not just a subset of them."""
    scenario = await _build_scenario(factory, pg_container, slug="binding-each")
    async with factory() as session, session.begin():
        second_revision_id = await _seed_revision(
            session, tenant_id=scenario.tenant_id, artifact_id=scenario.artifact_id
        )
        corpus_digest = _hex64()
        await session.execute(
            text(
                "INSERT INTO arc_observation_replay_corpora (corpus_id, generator_version, generator_input_digest,"
                " canonical_corpus_digest, fixture_class_count, owning_scope, target_tenant_id,"
                " approving_authority_issuer, approving_authority_subject, approved_at, expires_at)"
                " VALUES (:cid, 'v1', :digest, :digest, 100, 'global', NULL, 'https://idp.example.test', 'op',"
                " :approved, :expires)"
            ),
            {
                "cid": uuid.uuid4(),
                "digest": corpus_digest,
                "approved": _NOW,
                "expires": _NOW + datetime.timedelta(days=30),
            },
        )

    base = _qualification_kwargs(
        cohort_id=scenario.cohort_id,
        candidate_revision_id=scenario.revision_id,
        proposal_id=scenario.proposal_id,
        proposal_version=scenario.proposal_version,
        baseline_revision_id=None,
        replay_corpus_digest=None,
    )
    async with factory() as session, session.begin():
        await _insert_qualification_row(session, **base)

    variants: list[dict[str, object]] = [
        {"candidate_review_package_digest": _hex64()},
        {"baseline_revision_id": second_revision_id},
        {"selection_engine_version": "arc_selection_v2"},
        {"engine_configuration_version": "arc_selection_config_v2"},
        {"cohort_digest": _hex64()},
        {"expected_impact_envelope_digest": _hex64()},
        {"replay_corpus_digest": corpus_digest},
        {"qualification_algorithm_version": "arc_observation_qualification_v2"},
    ]
    assert len(variants) == 8, "one variant per binding-tuple column, no more, no fewer"

    for variant in variants:
        async with factory() as session, session.begin():
            await _insert_qualification_row(session, **{**base, **variant, "qualification_id": uuid.uuid4()})


# ---------------------------------------------------------------------------
# arc_observation_cohort_members: PK (cohort_id, tenant_id), proven as a
# genuine composite -- not over-constrained on either column alone.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_members_pk_is_genuinely_composite(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="members-pk")
    tenant_a = scenario.tenant_id
    tenant_b, _ = await seed_tenant_and_actor(pg_container, slug="members-pk-b")
    second_artifact_id = await seed_artifact_family(factory, tenant_id=None, slug_prefix="members-pk-2")
    async with factory() as session, session.begin():
        second_revision_id = await _seed_revision(session, tenant_id=None, artifact_id=second_artifact_id)
        second_proposal_id, second_proposal_version = await _seed_proposal_version(
            session, artifact_id=second_artifact_id, tenant_id=None, revision_id=second_revision_id
        )
        cohort_b = await _seed_cohort(
            session,
            proposal_id=second_proposal_id,
            proposal_version=second_proposal_version,
            candidate_revision_id=second_revision_id,
        )

    async with factory() as session, session.begin():
        await obs_queries.insert_cohort_members(
            session, cohort_id=scenario.cohort_id, tenant_ids=[tenant_a], added_at=_NOW
        )

    # Direction 1: the identical (cohort_id, tenant_id) pair a second time must fail.
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await obs_queries.insert_cohort_members(
                session, cohort_id=scenario.cohort_id, tenant_ids=[tenant_a], added_at=_NOW
            )

    # Direction 2a: same cohort_id, a *different* tenant_id must succeed --
    # proves the key is not accidentally a bare UNIQUE on cohort_id alone.
    async with factory() as session, session.begin():
        await obs_queries.insert_cohort_members(
            session, cohort_id=scenario.cohort_id, tenant_ids=[tenant_b], added_at=_NOW
        )

    # Direction 2b: a *different* cohort_id, the same tenant_id must
    # succeed -- proves the key is not accidentally a bare UNIQUE on
    # tenant_id alone either.
    async with factory() as session, session.begin():
        await obs_queries.insert_cohort_members(session, cohort_id=cohort_b, tenant_ids=[tenant_a], added_at=_NOW)


# ---------------------------------------------------------------------------
# Aggregate-only leak prevention: load_aggregate_counters cannot be used to
# recover a cohort's per-tenant membership or contribution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_counters_never_carries_a_tenant_field(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Structural: the dataclass `load_aggregate_counters` returns has no
    field shaped like a tenant identity, by construction -- not by caller
    discipline."""
    field_names = {f.name for f in dataclasses.fields(obs_queries.ResultCounters)}
    assert not any("tenant" in name for name in field_names), field_names


@pytest.mark.asyncio
async def test_aggregate_counters_sums_every_member_tenant(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="agg-sum")
    tenant_b, _ = await seed_tenant_and_actor(pg_container, slug="agg-sum-b")
    async with factory() as session, session.begin():
        await obs_queries.insert_cohort_members(
            session, cohort_id=scenario.cohort_id, tenant_ids=[scenario.tenant_id, tenant_b], added_at=_NOW
        )
        await obs_queries.ensure_result_row(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, now=_NOW
        )
        await obs_queries.ensure_result_row(session, cohort_id=scenario.cohort_id, tenant_id=tenant_b, now=_NOW)

    for tenant_id, count in ((scenario.tenant_id, 5), (tenant_b, 15)):
        async with factory() as session, session.begin():
            for _ in range(count):
                await obs_queries.record_observation(
                    session,
                    cohort_id=scenario.cohort_id,
                    tenant_id=tenant_id,
                    eligible_delta=1,
                    observed_delta=1,
                    unexplained_delta=0,
                    out_of_envelope_delta=0,
                    delta_code="newly_selected",
                    explained=True,
                    fingerprint_digest=uuid.uuid4().hex,
                    now=_NOW,
                )

    async with factory() as session:
        aggregate = await obs_queries.load_aggregate_counters(session, scenario.cohort_id)
        tenant_a_only = await obs_queries.load_tenant_counters(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id
        )
        tenant_b_only = await obs_queries.load_tenant_counters(
            session, cohort_id=scenario.cohort_id, tenant_id=tenant_b
        )

    assert aggregate.eligible_count == 20
    assert aggregate.observed_count == 20
    assert tenant_a_only is not None and tenant_a_only.observed_count == 5
    assert tenant_b_only is not None and tenant_b_only.observed_count == 15


@pytest.mark.asyncio
async def test_aggregate_counters_cannot_distinguish_two_different_per_tenant_splits(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The strong form of the leak-prevention claim: two cohorts with
    *different* per-tenant contributions that happen to sum to the same
    total produce byte-identical aggregates -- there is no way to recover,
    from the aggregate alone, which tenant contributed what, or even
    whether one tenant contributed everything or several tenants split it
    evenly.
    """
    tenant_x, _ = await seed_tenant_and_actor(pg_container, slug="agg-indist-x")
    tenant_y, _ = await seed_tenant_and_actor(pg_container, slug="agg-indist-y")

    async def _cohort_with_split(split: tuple[int, int]) -> uuid.UUID:
        artifact_id = await seed_artifact_family(factory, tenant_id=None, slug_prefix="agg-indist")
        async with factory() as session, session.begin():
            revision_id = await _seed_revision(session, tenant_id=None, artifact_id=artifact_id)
            proposal_id, proposal_version = await _seed_proposal_version(
                session, artifact_id=artifact_id, tenant_id=None, revision_id=revision_id
            )
            cohort_id = await _seed_cohort(
                session, proposal_id=proposal_id, proposal_version=proposal_version, candidate_revision_id=revision_id
            )
            await obs_queries.insert_cohort_members(
                session, cohort_id=cohort_id, tenant_ids=[tenant_x, tenant_y], added_at=_NOW
            )
            await obs_queries.ensure_result_row(session, cohort_id=cohort_id, tenant_id=tenant_x, now=_NOW)
            await obs_queries.ensure_result_row(session, cohort_id=cohort_id, tenant_id=tenant_y, now=_NOW)
        for tenant_id, count in ((tenant_x, split[0]), (tenant_y, split[1])):
            for _ in range(count):
                async with factory() as session, session.begin():
                    await obs_queries.record_observation(
                        session,
                        cohort_id=cohort_id,
                        tenant_id=tenant_id,
                        eligible_delta=1,
                        observed_delta=1,
                        unexplained_delta=0,
                        out_of_envelope_delta=0,
                        delta_code="newly_selected",
                        explained=True,
                        fingerprint_digest=uuid.uuid4().hex,
                        now=_NOW,
                    )
        return cohort_id

    cohort_all_x = await _cohort_with_split((20, 0))
    cohort_even_split = await _cohort_with_split((10, 10))

    async with factory() as session:
        aggregate_all_x = await obs_queries.load_aggregate_counters(session, cohort_all_x)
        aggregate_even_split = await obs_queries.load_aggregate_counters(session, cohort_even_split)

    assert aggregate_all_x == aggregate_even_split, (
        "two cohorts with different per-tenant splits but the same total must be indistinguishable "
        "from the aggregate alone -- that indistinguishability is what makes it safe to serve globally"
    )


# ---------------------------------------------------------------------------
# ObservationWindowEvaluatorWorker: FakeClock boundary + real scheduler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_evaluator_stays_open_one_second_before_the_deadline_and_closes_exactly_at_it(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="window-boundary")
    window_deadline = _NOW + datetime.timedelta(hours=24)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_observation_cohorts SET window_deadline = :deadline WHERE cohort_id = :cid"),
            {"deadline": window_deadline, "cid": scenario.cohort_id},
        )
        # Sufficient count, so the only remaining question is the boundary.
        await obs_queries.insert_cohort_members(
            session, cohort_id=scenario.cohort_id, tenant_ids=[scenario.tenant_id], added_at=_NOW
        )
        await obs_queries.ensure_result_row(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, now=_NOW
        )
    async with factory() as session, session.begin():
        for _ in range(100):
            await obs_queries.record_observation(
                session,
                cohort_id=scenario.cohort_id,
                tenant_id=scenario.tenant_id,
                eligible_delta=1,
                observed_delta=1,
                unexplained_delta=0,
                out_of_envelope_delta=0,
                delta_code="newly_selected",
                explained=True,
                fingerprint_digest=uuid.uuid4().hex,
                now=_NOW,
            )

    clock = FakeClock(window_deadline - datetime.timedelta(seconds=1))
    worker = ObservationWindowEvaluatorWorker(factory, clock=clock)

    result_before = await worker.run_once()
    async with factory() as session:
        cohort_before = await obs_queries.load_cohort(session, scenario.cohort_id)
    assert cohort_before is not None and cohort_before.closed_at is None
    assert result_before.closed == 0

    clock.set(window_deadline)
    result_at = await worker.run_once()
    async with factory() as session:
        cohort_at = await obs_queries.load_cohort(session, scenario.cohort_id)
    assert cohort_at is not None and cohort_at.closed_at is not None
    assert cohort_at.window_ended_at == window_deadline
    assert result_at.closed == 1


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_the_scheduler_registers_and_can_actually_run_the_window_evaluator_job(
    wired_app: FastAPI, pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Runs the exact callable `create_app`'s own scheduler wiring
    registered against the real database that app is wired to -- not a
    worker this test constructed itself. The deployment's own clock is the
    real wall clock, so the cohort's own `window_deadline` is rewritten to
    the database's current time (matching `test_arc_source_status.py`'s
    own precedent) rather than trying to control that clock.
    """
    scenario = await _build_scenario(factory, pg_container, slug="window-scheduler")
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_observation_cohorts SET window_deadline = now() - interval '1 second' "
                "WHERE cohort_id = :cid"
            ),
            {"cid": scenario.cohort_id},
        )

    services = wired_app.state.services
    job = services.scheduler.get_job("arc_observation_window_evaluator")
    assert job is not None, "arc_observation_window_evaluator must be registered by the app's own scheduler wiring"

    await job.func()  # the exact coroutine register_periodic wrapped and scheduled

    async with factory() as session:
        cohort = await obs_queries.load_cohort(session, scenario.cohort_id)
    assert cohort is not None and cohort.closed_at is not None


# ---------------------------------------------------------------------------
# ObservationFingerprintReaperWorker: FakeClock boundary + real scheduler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reaper_preserves_one_second_before_the_window_and_clears_exactly_at_it(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="reaper-boundary")
    closed_at = _NOW
    # Record the observation *before* closing the cohort: `record_
    # observation`'s own guard refuses once `closed_at IS NOT NULL`, so
    # closing first would leave nothing for the reaper to ever find.
    async with factory() as session, session.begin():
        await obs_queries.ensure_result_row(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, now=_NOW
        )
        await obs_queries.record_observation(
            session,
            cohort_id=scenario.cohort_id,
            tenant_id=scenario.tenant_id,
            eligible_delta=1,
            observed_delta=1,
            unexplained_delta=0,
            out_of_envelope_delta=0,
            delta_code="newly_selected",
            explained=True,
            fingerprint_digest="a-real-fingerprint",
            now=_NOW,
        )
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_observation_cohorts SET closed_at = :closed_at, window_ended_at = :closed_at "
                "WHERE cohort_id = :cid"
            ),
            {"closed_at": closed_at, "cid": scenario.cohort_id},
        )

    async def _fingerprints() -> list[str]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT fingerprint_digests FROM arc_observation_results "
                        "WHERE cohort_id = :cid AND tenant_id = :tid"
                    ),
                    {"cid": scenario.cohort_id, "tid": scenario.tenant_id},
                )
            ).one()
        return list(row.fingerprint_digests)

    clock = FakeClock(closed_at + RETENTION_WINDOW - datetime.timedelta(seconds=1))
    worker = ObservationFingerprintReaperWorker(factory, clock=clock)

    # Checked on this row specifically, not the worker's global `reaped`
    # count -- other tests in this same shared database may legitimately
    # have their own eligible rows swept up in the same pass.
    await worker.run_once()
    assert await _fingerprints() == ["a-real-fingerprint"], "one second before the boundary: still preserved"

    clock.set(closed_at + RETENTION_WINDOW)
    await worker.run_once()
    assert await _fingerprints() == [], "exactly at the boundary: cleared"


@pytest.mark.asyncio
async def test_fingerprint_reaper_never_clears_a_row_under_legal_hold(
    pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="reaper-hold")
    closed_at = _NOW
    async with factory() as session, session.begin():
        await obs_queries.ensure_result_row(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, now=_NOW
        )
        await obs_queries.record_observation(
            session,
            cohort_id=scenario.cohort_id,
            tenant_id=scenario.tenant_id,
            eligible_delta=1,
            observed_delta=1,
            unexplained_delta=0,
            out_of_envelope_delta=0,
            delta_code="newly_selected",
            explained=True,
            fingerprint_digest="held-fingerprint",
            now=_NOW,
        )
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_observation_cohorts SET closed_at = :closed_at, window_ended_at = :closed_at "
                "WHERE cohort_id = :cid"
            ),
            {"closed_at": closed_at, "cid": scenario.cohort_id},
        )
        await obs_queries.place_legal_hold(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, placed_at=_NOW
        )

    clock = FakeClock(closed_at + RETENTION_WINDOW + datetime.timedelta(days=365))
    worker = ObservationFingerprintReaperWorker(factory, clock=clock)
    await worker.run_once()

    # Assert this row specifically, not the global `reaped` count: the
    # worker has no per-cohort scope by contract (it sweeps every eligible
    # row in one pass), so other tests' long-past-retention rows in this
    # same shared database are legitimately swept up in the same call --
    # what matters here is that *this* held row survives regardless.
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT fingerprint_digests FROM arc_observation_results "
                    "WHERE cohort_id = :cid AND tenant_id = :tid"
                ),
                {"cid": scenario.cohort_id, "tid": scenario.tenant_id},
            )
        ).one()
    assert row.fingerprint_digests == ["held-fingerprint"]


@pytest.mark.asyncio
async def test_the_scheduler_registers_and_can_actually_run_the_fingerprint_reaper_job(
    wired_app: FastAPI, pg_container: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await _build_scenario(factory, pg_container, slug="reaper-scheduler")
    async with factory() as session, session.begin():
        await obs_queries.ensure_result_row(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id, now=_NOW
        )
        await obs_queries.record_observation(
            session,
            cohort_id=scenario.cohort_id,
            tenant_id=scenario.tenant_id,
            eligible_delta=1,
            observed_delta=1,
            unexplained_delta=0,
            out_of_envelope_delta=0,
            delta_code="newly_selected",
            explained=True,
            fingerprint_digest="scheduler-fingerprint",
            now=_NOW,
        )
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_observation_cohorts SET closed_at = now() - interval '31 days', "
                "window_ended_at = now() - interval '31 days' WHERE cohort_id = :cid"
            ),
            {"cid": scenario.cohort_id},
        )

    services = wired_app.state.services
    job = services.scheduler.get_job("arc_observation_fingerprint_reaper")
    assert job is not None, "arc_observation_fingerprint_reaper must be registered by the app's own scheduler wiring"

    await job.func()

    async with factory() as session:
        counters = await obs_queries.load_tenant_counters(
            session, cohort_id=scenario.cohort_id, tenant_id=scenario.tenant_id
        )
    assert counters is not None
    # `load_tenant_counters` reports only aggregate integers, never the
    # fingerprint list itself -- confirm the underlying row directly.
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT fingerprint_digests FROM arc_observation_results "
                    "WHERE cohort_id = :cid AND tenant_id = :tid"
                ),
                {"cid": scenario.cohort_id, "tid": scenario.tenant_id},
            )
        ).one()
    assert row.fingerprint_digests == []
