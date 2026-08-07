"""Integration tests for `ArtifactMaterialisationService.submit`, against a
real Postgres.

Four things a fake session cannot prove (`tests/unit/test_arc_
materialisation.py` and `tests/unit/test_arc_risk.py`/`test_arc_envelope.py`
cover everything else): that a refused submit -- whether for a missing
collaborator or an invalid risk/envelope input -- leaves the database
byte-identical, not merely "the row I checked happens to match"; that the
whole materialisation transaction actually commits a schema-valid
`arc_revisions` row, the bijection link, the sticky risk classification, the
frozen envelope, and the signed genesis operational event together, against
a real database enforcing every `CHECK` and `UNIQUE` constraint along the
way; and that two truly concurrent submits on the same version resolve to
exactly one winner across every one of those rows -- the bijection race
`AAS-T09` deferred to this task.

This is the first task where `enabled=True` builds the *real* collaborators
(`OperationalChainService`, `RiskEnvelopeValidator`) rather than bare
sentinel objects: before this task, nothing in `submit`'s transaction body
actually called a method on either one, so a bare `object()` was enough to
clear the presence guard. Now that `submit` calls `append_event`/`assess_
and_persist` for real, a sentinel would raise `AttributeError` the instant
it was used -- the collaborators below are the same ones `wiring/services.py`
constructs, exercised directly rather than through the router, since no
route task in this phase has re-registered the submission route against the
now-enabled service (that remains `AAS-T21`'s openapi-freeze scope).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.envelope import EnvelopeInvalid
from registry.arc.service.operational_chain import OperationalChainService
from registry.arc.service.proposal import ProposalService, ProposalStateConflict
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.risk import CURRENT_RISK_ALGORITHM_VERSION, RiskEnvelopeValidator
from registry.arc.service.submission import (
    ArtifactMaterialisationService,
    CandidateGovernanceRowRejected,
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


def _rfc3339(moment: datetime.datetime) -> str:
    """RFC 3339 UTC with a literal `Z` -- the exact spelling `arc_artifact_
    semantics_v1`'s and `arc_expected_impact_envelope_v1`'s timestamp
    patterns require. Plain `.isoformat()` emits `+00:00`, which both
    patterns refuse."""
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


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
    """*enabled=False* leaves both collaborators unwired -- the shape of the
    guard's own combined check regardless of how many real deployments have
    one collaborator wired and not the other. *enabled=True* injects the
    same two real collaborators `wiring.services._wire_arc` constructs
    (`OperationalChainService`, `RiskEnvelopeValidator`) -- not sentinels:
    `submit` now calls a real method on each inside its own transaction, so
    only a genuinely functioning collaborator can complete it."""
    return ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=(
            OperationalChainService(clock=FakeClock(_NOW), deployment_id="submission-test") if enabled else None
        ),
        risk_envelope_validator=RiskEnvelopeValidator() if enabled else None,
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


def _applicability_rule(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rule_id": str(uuid.uuid4()),
        "scope": "task",
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "task_kinds": None,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
        # Non-mandatory, non-global: the lowest-impact tier
        # (`task_non_mandatory`), so this shared fixture does not
        # accidentally exercise the observation-required or three-identity
        # actor-separation paths a later phase's tasks own.
        "is_mandatory": False,
    }
    base.update(overrides)
    return base


def _directive(**overrides: Any) -> dict[str, Any]:
    """A complete `arc_artifact_semantics_v1.directives[]` element,
    `citation_only` by default -- the one `directive_type` this
    deployment's persisted vocabulary can materialise into `arc_directives`
    today (see `ArtifactMaterialisationService._directive_row`'s own
    docstring on `verify_before_action`/`self_attested`, which have no
    destination there yet)."""
    statement = "Cite the approved runbook."
    base: dict[str, Any] = {
        "directive_id": str(uuid.uuid4()),
        "directive_type": "citation_only",
        "compact_statement_plaintext": statement,
        "compact_statement_plaintext_digest": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
        "source_anchor": "anchor-1",
        "conflict_key_schema_version": 1,
        "conflict_key_namespace": None,
        "conflict_key_subject_selector": None,
        "conflict_key_operation": None,
        "conflict_key_action_class": None,
        "conflict_key_target_selector": None,
        "conflict_key_modality": None,
        "conflict_key_constraint_operator": None,
        "conflict_key_constraint_value": None,
        "conflict_subject_digest": None,
        "delegable_exception": False,
        "satisfaction_mode": None,
        "verification_max_age_seconds": None,
        "accepted_verifier_classes": None,
        "accepted_verifier_ids": None,
        "required_evidence_type": None,
        "created_at": _rfc3339(_NOW),
    }
    base.update(overrides)
    return base


def _candidate(
    *, artifact_id: uuid.UUID, revision_id: uuid.UUID, directives: list[dict[str, Any]] | None = None
) -> dict[str, object]:
    """A minimal, valid `arc_artifact_semantics_v1` candidate -- carries no
    directives by default, so no `field_provenance` entry is conditionally
    required for one, and this test can persist it with `queries.proposal.
    update_semantics` directly rather than going through `ProvenanceService.
    edit`'s own validation, which is a different task's test surface
    (`tests/integration/test_arc_validation.py`).

    Carries exactly one applicability rule: a frozen semantic object always
    has at least one, rejected earlier in the authoring flow before
    classification is ever reached. An empty `applicability` list, which
    this fixture carried before `RiskEnvelopeValidator` existed, would now
    fail classification inside `submit` itself, since it is the reducer's
    contract that it never receives one.
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
        "directives": directives if directives is not None else [],
        "applicability": [_applicability_rule()],
        "detail_audience": "agent_only",
        "review_expires_at": _rfc3339(_NOW + datetime.timedelta(days=365)),
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


def _class_predicate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_observation_class_predicate_v1",
        "task_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }
    base.update(overrides)
    return base


def _envelope_item(item_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": item_id,
        "delta_code": "newly_selected",
        "class_predicate": _class_predicate(),
        "minimum_count": 0,
        "maximum_count": None,
        "rationale_code": "expected_low_traffic",
    }
    base.update(overrides)
    return base


def _envelope(
    *, proposal_id: uuid.UUID, proposal_version: int, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A valid, closed `arc_expected_impact_envelope_v1` object naming the
    exact proposal/version this call submits -- `ExpectedImpactEnvelope
    Service.validate` refuses a mismatch."""
    return {
        "profile": "arc_expected_impact_envelope_v1",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "items": items or [_envelope_item("item-1")],
        "author_issuer": _ISSUER,
        "author_subject": _OPERATOR,
        "created_at": _rfc3339(_NOW),
    }


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


@pytest.mark.asyncio
async def test_a_real_functioning_appender_alone_still_leaves_submit_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Injection without enabling, proven with the real collaborator this
    task built -- not a bare sentinel. `wiring.services._wire_arc` injects
    a genuinely working `OperationalChainService` into this exact
    constructor; this proves that alone does not make `submit` reachable,
    because `risk_envelope_validator` is still `None` and the guard is an
    AND over both. If this test starts failing, the injection went further
    than the contract allows.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"submit-real-appender-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    candidate = _candidate(artifact_id=artifact_id, revision_id=uuid.uuid4())
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    before_revisions = await _revision_count(factory, artifact_id=artifact_id)
    real_appender = OperationalChainService(clock=FakeClock(_NOW), deployment_id="submission-test")
    service = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=real_appender,
        # risk_envelope_validator left unwired -- the one collaborator this
        # deployment still lacks.
    )

    with pytest.raises(SubmissionPrerequisiteUnavailable):
        await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=object())

    assert await _revision_count(factory, artifact_id=artifact_id) == before_revisions == 0


@pytest.mark.asyncio
async def test_a_real_risk_envelope_validator_alone_still_leaves_submit_refused(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The symmetric case: a real `RiskEnvelopeValidator` wired with no
    operational-chain appender still refuses. Proves the guard is a true
    AND over both collaborators, not merely "whichever one this
    deployment happened to wire first" -- the previous test proved one
    direction, this proves the other.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"submit-real-validator-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    candidate = _candidate(artifact_id=artifact_id, revision_id=uuid.uuid4())
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    before_revisions = await _revision_count(factory, artifact_id=artifact_id)
    service = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        risk_envelope_validator=RiskEnvelopeValidator(),
        # operational_chain_appender left unwired -- the one collaborator
        # this deployment still lacks, in this direction.
    )

    with pytest.raises(SubmissionPrerequisiteUnavailable):
        await service.submit(
            _ctx(tenant_id=tenant_id),
            proposal_id,
            1,
            expected_impact_envelope=_envelope(proposal_id=proposal_id, proposal_version=1),
        )

    assert await _revision_count(factory, artifact_id=artifact_id) == before_revisions == 0


# ---------------------------------------------------------------------------
# End-to-end submit, once both collaborators are present.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_end_to_end_materialises_a_real_revision(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The defining one-transaction proof: exactly one draft revision, the
    bijection link, the frozen version, the recorded baseline, the
    actor-attributed audit event, the sticky risk classification, the
    frozen expected-impact envelope and its item, and the signed genesis
    operational event plus its pending checkpoint -- all committed
    together by one `submit` call.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-e2e-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    revision_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id)
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    envelope = _envelope(proposal_id=proposal_id, proposal_version=1)
    service = _materialisation_service(factory, enabled=True)
    result = await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=envelope)

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
                    "SELECT state, frozen_at, revision_id, risk_classification, risk_algorithm_version "
                    "FROM arc_authoring_proposal_versions WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).one()
        audit_rows = (
            await session.execute(
                text("SELECT event_type FROM arc_audit_outbox WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).all()
        risk_row = (
            await session.execute(
                text(
                    "SELECT classification, algorithm_version FROM arc_risk_classifications "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).one()
        envelope_row = (
            await session.execute(
                text(
                    "SELECT envelope_id, envelope_digest, author_issuer, author_subject "
                    "FROM arc_expected_impact_envelopes WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).one()
        item_rows = (
            await session.execute(
                text(
                    "SELECT item_id, delta_code, minimum_count, maximum_count "
                    "FROM arc_expected_impact_envelope_items WHERE envelope_id = :eid"
                ),
                {"eid": envelope_row.envelope_id},
            )
        ).all()
        operational_event = (
            await session.execute(
                text(
                    "SELECT sequence, event_type, previous_event_digest, signature, actor_issuer, actor_subject "
                    "FROM arc_operational_events WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
        operational_head = (
            await session.execute(
                text("SELECT next_sequence FROM arc_operational_event_heads WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).one()
        checkpoint = (
            await session.execute(
                text("SELECT sequence, exported_at FROM arc_operational_chain_checkpoints WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).one()

    assert revision.artifact_id == artifact_id
    assert revision.tenant_id == tenant_id
    assert revision.lifecycle_state == "draft"
    assert revision.source_system == "confluence"
    assert revision.content_classification == "internal"
    assert revision.freshness_basis == "revision_pinned_only"
    assert version.state == "submitted"
    assert version.revision_id == revision_id
    assert version.frozen_at is not None
    assert version.risk_classification == "task_non_mandatory"
    assert version.risk_algorithm_version == CURRENT_RISK_ALGORITHM_VERSION
    assert any(row.event_type == "arc.proposal.submitted" for row in audit_rows)

    # The sticky risk-classification row: same values as the read-path cache
    # columns on the proposal version above, but its own durable record.
    assert risk_row.classification == "task_non_mandatory"
    assert risk_row.algorithm_version == CURRENT_RISK_ALGORITHM_VERSION

    # The frozen envelope and its one item, exactly as declared.
    assert str(envelope_row.envelope_id) == envelope["envelope_id"]
    assert envelope_row.author_issuer == _ISSUER
    assert envelope_row.author_subject == _OPERATOR
    assert len(envelope_row.envelope_digest) == 64
    assert len(item_rows) == 1
    assert item_rows[0].item_id == "item-1"
    assert item_rows[0].delta_code == "newly_selected"
    assert item_rows[0].minimum_count == 0
    assert item_rows[0].maximum_count is None

    # The signed genesis operational event, its head, and its pending
    # checkpoint -- all written in the same transaction as everything
    # above.
    assert operational_event.sequence == 0
    assert operational_event.event_type == "operational_state_initialized"
    assert operational_event.previous_event_digest is None
    assert operational_event.signature is not None
    assert operational_event.actor_issuer == "registry://deployment"
    assert operational_event.actor_subject == "arc-operational-state"
    assert operational_head.next_sequence == 1
    assert checkpoint.sequence == 0
    # Pending, not yet exported: no sink is configured on this deployment
    # today (`CheckpointExportService`'s own module docstring) -- the
    # draft's own `operational_integrity_state` stays "pending" for exactly
    # that reason, matching `AAS-T11`'s contract that the draft remains
    # integrity-pending until the receipt is durable.
    assert checkpoint.exported_at is None

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


# ---------------------------------------------------------------------------
# AAS-T34: the candidate's own directives[]/applicability[] materialise into
# arc_directives/arc_applicability_rules, in the same transaction, against a
# real database enforcing every foreign key and CHECK along the way.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_materialises_the_candidates_directive_and_rule(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A candidate carrying one real `citation_only` directive reaches
    `arc_directives` (plus its `arc_directive_identities` row) and
    `arc_applicability_rules`, committed in the same transaction as the
    revision and the frozen version -- the gap this task closes. Before
    this task, `submission.py` wrote `arc_revisions` only, and a revision
    authored through this surface had zero servable directives."""
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-directive-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    candidate = _candidate(
        artifact_id=artifact_id, revision_id=revision_id, directives=[_directive(directive_id=str(directive_id))]
    )
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    service = _materialisation_service(factory, enabled=True)
    await service.submit(
        _ctx(tenant_id=tenant_id),
        proposal_id,
        1,
        expected_impact_envelope=_envelope(proposal_id=proposal_id, proposal_version=1),
    )

    async with factory() as session:
        directive_row = (
            await session.execute(
                text(
                    "SELECT directive_id, revision_id, tenant_id, directive_type, compact_statement_plaintext,"
                    "       source_anchor, conflict_key_schema_version, conflict_subject_digest"
                    " FROM arc_directives WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
        identity_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_directive_identities WHERE directive_id = :did"),
                {"did": directive_id},
            )
        ).scalar()
        rule_row = (
            await session.execute(
                text(
                    "SELECT rule_id, revision_id, tenant_id, scope, is_mandatory, effective_from"
                    " FROM arc_applicability_rules WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()

    assert directive_row.directive_id == directive_id
    assert directive_row.revision_id == revision_id
    assert directive_row.tenant_id == tenant_id, "the composite fk requires the directive tenant to match the revision"
    assert directive_row.directive_type == "citation_only"
    assert directive_row.compact_statement_plaintext == "Cite the approved runbook."
    assert directive_row.source_anchor == "anchor-1"
    # citation_only carries no conflict key -- derived from directive_type,
    # never copied from the candidate's own (unrelated) integer schema-
    # version field.
    assert directive_row.conflict_key_schema_version is None
    assert directive_row.conflict_subject_digest is None
    assert identity_count == 1

    assert rule_row.revision_id == revision_id
    assert rule_row.tenant_id == tenant_id
    assert rule_row.scope == "task"
    assert rule_row.is_mandatory is False
    # The candidate's own rule carries a null effective_from; materialisation
    # falls back to *now* rather than reaching the database as a NOT NULL
    # violation.
    assert rule_row.effective_from is not None


@pytest.mark.asyncio
async def test_submit_with_an_unmaterialisable_directive_leaves_the_database_byte_identical(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A candidate directive naming `directive_type: "verify_before_action"`
    has no destination in this deployment's persisted vocabulary (see
    `ArtifactMaterialisationService._directive_row`'s own docstring) --
    `arc_directives`' own CHECK constraint refuses it, `submit` turns that
    into `CandidateGovernanceRowRejected`, and the whole transaction rolls
    back: no revision, no directive, no frozen version, no audit event --
    proving the new writer's failure is exactly as atomic as every other
    failure inside this transaction.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"submit-bad-directive-{uuid.uuid4().hex[:8]}"
    )
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    revision_id = uuid.uuid4()
    candidate = _candidate(
        artifact_id=artifact_id,
        revision_id=revision_id,
        directives=[_directive(directive_type="verify_before_action")],
    )
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    before_row = await _version_snapshot(factory, proposal_id=proposal_id)
    before_revisions = await _revision_count(factory, artifact_id=artifact_id)
    before_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    service = _materialisation_service(factory, enabled=True)
    with pytest.raises(CandidateGovernanceRowRejected):
        await service.submit(
            _ctx(tenant_id=tenant_id),
            proposal_id,
            1,
            expected_impact_envelope=_envelope(proposal_id=proposal_id, proposal_version=1),
        )

    after_row = await _version_snapshot(factory, proposal_id=proposal_id)
    after_revisions = await _revision_count(factory, artifact_id=artifact_id)
    after_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    assert after_row == before_row, "the proposal-version row must not change at all on a rejected directive"
    assert after_revisions == before_revisions == 0, "no draft revision may exist after a refused submit"
    assert after_audit == before_audit, "no audit event may be written for a refused submit"

    async with factory() as session:
        directive_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_directives WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar()
        rule_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_applicability_rules WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar()
        risk_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_risk_classifications WHERE proposal_id = :pid AND proposal_version = 1"),
                {"pid": proposal_id},
            )
        ).scalar()
    assert directive_count == 0, "no directive row may survive a rejected submit"
    assert rule_count == 0, "no applicability rule row may survive a rejected submit"
    assert risk_count == 0, "classification never runs once directive materialisation has already failed"


@pytest.mark.asyncio
async def test_submit_with_an_invalid_envelope_leaves_the_database_byte_identical(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Both collaborators are wired for real -- the only thing wrong is the
    envelope (a forbidden predicate key). `EnvelopeInvalid` must roll back
    the whole transaction: no draft revision, no frozen version, no audit
    event, no risk-classification row, no envelope row, no operational
    event -- proving a failure *inside* the enabled transaction, not just
    the presence guard, leaves the database untouched.
    """
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=f"submit-bad-envelope-{uuid.uuid4().hex[:8]}")
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, _source_evidence_id = await _open_proposal(factory, tenant_id=tenant_id, artifact_id=artifact_id)
    revision_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id)
    await _persist_candidate(factory, proposal_id=proposal_id, proposal_version=1, candidate=candidate)

    bad_predicate = _class_predicate()
    bad_predicate["tenant_id"] = str(tenant_id)
    bad_envelope = _envelope(
        proposal_id=proposal_id, proposal_version=1, items=[_envelope_item("item-1", class_predicate=bad_predicate)]
    )

    before_row = await _version_snapshot(factory, proposal_id=proposal_id)
    before_revisions = await _revision_count(factory, artifact_id=artifact_id)
    before_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    service = _materialisation_service(factory, enabled=True)
    with pytest.raises(EnvelopeInvalid):
        await service.submit(_ctx(tenant_id=tenant_id), proposal_id, 1, expected_impact_envelope=bad_envelope)

    after_row = await _version_snapshot(factory, proposal_id=proposal_id)
    after_revisions = await _revision_count(factory, artifact_id=artifact_id)
    after_audit = await _audit_outbox_count(factory, tenant_id=tenant_id)

    assert after_row == before_row, "the proposal-version row must not change at all on an envelope refusal"
    assert after_revisions == before_revisions == 0, "no draft revision may exist after a refused submit"
    assert after_audit == before_audit, "no audit event may be written for a refused submit"

    async with factory() as session:
        risk_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_risk_classifications WHERE proposal_id = :pid AND proposal_version = 1"),
                {"pid": proposal_id},
            )
        ).scalar()
        envelope_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_expected_impact_envelopes "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).scalar()
        operational_event_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_operational_events WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar()
    assert risk_count == 0, "no risk-classification row may exist after a refused submit"
    assert envelope_count == 0, "no envelope row may exist after a refused submit"
    assert operational_event_count == 0, "no operational event may exist after a refused submit"


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
    await service.submit(
        _ctx(tenant_id=tenant_id),
        proposal_id,
        1,
        expected_impact_envelope=_envelope(proposal_id=proposal_id, proposal_version=1),
    )

    with pytest.raises(ProposalStateConflict):
        await service.submit(
            _ctx(tenant_id=tenant_id),
            proposal_id,
            1,
            expected_impact_envelope=_envelope(proposal_id=proposal_id, proposal_version=1),
        )

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
    envelope = _envelope(proposal_id=proposal_id, proposal_version=1)

    async def _attempt() -> SubmissionResult | ProposalStateConflict:
        service = _materialisation_service(factory, enabled=True)
        try:
            return await service.submit(ctx, proposal_id, 1, expected_impact_envelope=envelope)
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
        risk_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_risk_classifications WHERE proposal_id = :pid AND proposal_version = 1"),
                {"pid": proposal_id},
            )
        ).scalar()
        envelope_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM arc_expected_impact_envelopes "
                    "WHERE proposal_id = :pid AND proposal_version = 1"
                ),
                {"pid": proposal_id},
            )
        ).scalar()
        operational_event_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_operational_events WHERE revision_id = :rid"),
                {"rid": uuid.UUID(str(candidate["revision_id"]))},
            )
        ).scalar()
    assert version.state == "submitted"
    winner = winners[0]
    assert isinstance(winner, SubmissionResult)
    assert version.revision_id == winner.revision_id
    # The race must leave exactly one of every row this transaction writes
    # -- not just one revision, but one sticky risk classification and one
    # frozen envelope. A losing attempt that had already written either
    # before losing the compare-and-swap would show up here as two.
    assert risk_count == 1, "the race must leave exactly one risk-classification row"
    assert envelope_count == 1, "the race must leave exactly one envelope row"
    assert operational_event_count == 1, "the race must leave exactly one operational event"
