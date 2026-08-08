"""Integration tests for the `S -> R -> A` digest chain
(`registry/arc/service/review_package.py`), against real Postgres.

**What this file proves that a fake session cannot.** `ReviewPackageService.
assemble` recomputes `S` and `R` from authoritative rows and cross-checks
two of them (the frozen expected-impact envelope's own digest, the sticky
risk classification) against a persisted cache before trusting either.
`ApprovalChallengeService.complete` recomputes the full chain again at
completion and refuses if it disagrees with what was committed at issuance.
Proving either "refuses because it recomputed and disagreed" rather than
"passes because it read the persisted column" requires a real row to
tamper with -- a unit test that only ever computes a digest and compares it
to its own computation proves nothing about which of those two paths ran.

Every substitution test below follows the same shape: seed a real submitted
proposal version through the real, now-enabled submission transaction
(`ArtifactMaterialisationService.submit`, exactly `test_arc_approval_race.
py`'s own `_seed_submitted_version`), corrupt one persisted column directly
with `UPDATE`, then call the path under test and assert it refuses.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service import approval_challenge as ac
from registry.arc.service import approval_challenge_verification as acv
from registry.arc.service import review_package as rp
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.operational_chain import OperationalChainService
from registry.arc.service.proposal import ProposalService
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.risk import CURRENT_RISK_ALGORITHM_VERSION, RiskEnvelopeValidator
from registry.arc.service.submission import ArtifactMaterialisationService
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


def _ctx(*, tenant_id: uuid.UUID, subject: str | None = None) -> ArcRequestContext:
    if subject is None:
        subject = f"caller-{uuid.uuid4().hex[:12]}"
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=subject)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAll(), global_write_allowlist=((_ISSUER, _OPERATOR),))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _review_package(factory: async_sessionmaker[AsyncSession]) -> rp.ReviewPackageService:
    return rp.ReviewPackageService(factory, authorization=_authorization())


def _approval_challenges(
    factory: async_sessionmaker[AsyncSession], review_package_service: rp.ReviewPackageService
) -> ac.ApprovalChallengeService:
    return ac.ApprovalChallengeService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        review_package_service=review_package_service,
    )


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    return base64.b64encode(private.sign(acv._SIGNING_DOMAIN + canonical_bytes)).decode("ascii")


def _proof(signature_base64: str) -> acv.DetachedSignatureProofInput:
    return acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=signature_base64)


async def _insert_verifier(session: AsyncSession, *, approval_verifier_id: str, public_key: bytes) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifiers ("
            "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
            "  algorithm, public_key, provider_id, valid_from, valid_to, revoked_at, created_at,"
            "  principal_binding_kind, principal_issuer, principal_subject, provider_allowed_principal_issuer,"
            "  credential_fingerprint, provider_configuration_digest"
            ") VALUES ("
            "  :vid, 'operator_public_key', CAST(:types AS TEXT[]), 'global', NULL,"
            "  'Ed25519', :pub, NULL, :vfrom, NULL, NULL, :now,"
            "  'exact_principal', :issuer, :subject, NULL, :fp, NULL"
            ")"
        ),
        {
            "vid": approval_verifier_id,
            "types": ["artifact_activation", "exception_approval"],
            "pub": public_key,
            "vfrom": _NOW - datetime.timedelta(days=1),
            "now": _NOW,
            "issuer": _ISSUER,
            "subject": f"verifier-principal-{approval_verifier_id}",
            "fp": uuid.uuid4().hex + uuid.uuid4().hex[:32],
        },
    )


# ---------------------------------------------------------------------------
# Duplicated from `test_arc_approval_race.py` rather than imported -- each
# integration test file in this package stays a self-contained scenario, so
# a change to one file's seeding shape never silently ripples into another's.
# ---------------------------------------------------------------------------


def _candidate(*, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> dict[str, object]:
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
        "applicability": [
            {
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
                "is_mandatory": False,
            }
        ],
        "detail_audience": "agent_only",
        "review_expires_at": (_NOW + datetime.timedelta(days=365))
        .astimezone(datetime.UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content_classification": "internal",
        "approved_retention_floor_days": 730,
        "initial_freshness_basis": "revision_pinned_only",
        "reviewed_baseline_revision_id": None,
    }


def _expected_impact_envelope(*, proposal_id: uuid.UUID, proposal_version: int) -> dict[str, object]:
    return {
        "profile": "arc_expected_impact_envelope_v1",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "items": [
            {
                "item_id": "item-1",
                "delta_code": "newly_selected",
                "class_predicate": {
                    "profile": "arc_observation_class_predicate_v1",
                    "task_kind": None,
                    "requested_action_classes": None,
                    "environment": None,
                    "data_sensitivity_tier": None,
                    "capability_ids": None,
                    "domain_ids": None,
                },
                "minimum_count": 0,
                "maximum_count": None,
                "rationale_code": "expected_low_traffic",
            }
        ],
        "author_issuer": _ISSUER,
        "author_subject": _OPERATOR,
        "created_at": "2026-01-01T00:00:00Z",
    }


async def _seed_submitted_version(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, artifact_id: uuid.UUID
) -> tuple[uuid.UUID, int, uuid.UUID]:
    """Gets a real `submitted` proposal version with a real bound revision,
    real sticky risk classification, real frozen envelope, and a real
    `arc.proposal.submitted` audit event -- through the real submission
    transaction, exactly `test_arc_approval_race.py`'s own helper of the
    same name. Returns `(proposal_id, proposal_version, revision_id)`.
    """
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    proposal_service = ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))
    version = await proposal_service.open_proposal(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR), artifact_id=artifact_id, source_evidence_id=source_evidence_id
    )
    revision_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id)
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=version.proposal_id, proposal_version=1, semantics=candidate
        )

    materialisation = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=OperationalChainService(clock=FakeClock(_NOW), deployment_id="digest-chain-test"),
        risk_envelope_validator=RiskEnvelopeValidator(),
    )
    result = await materialisation.submit(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR),
        version.proposal_id,
        1,
        expected_impact_envelope=_expected_impact_envelope(proposal_id=version.proposal_id, proposal_version=1),
    )
    assert result.revision_id == revision_id
    return version.proposal_id, 1, revision_id


async def _seed(
    factory: async_sessionmaker[AsyncSession], pg_container: str, *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, int, uuid.UUID]:
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=slug)
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    return tenant_id, proposal_id, proposal_version, revision_id


# ---------------------------------------------------------------------------
# End-to-end: assemble() and get_review_package() over real, untampered data.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_and_get_review_package_agree_on_s_and_r(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """The Protocol method (`assemble`, used by `ApprovalChallengeService`)
    and the router-facing method (`get_review_package`) must compute the
    exact same `S`/`R` for the same version -- one shared assembly, not two
    independent computations that could silently drift apart.
    """
    tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-agree-{uuid.uuid4().hex[:8]}"
    )
    service = _review_package(factory)

    async with factory() as session:
        digests = await service.assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)

    package = await service.get_review_package(_ctx(tenant_id=tenant_id), proposal_id, proposal_version)

    assert package.artifact_semantics_digest == digests.artifact_semantics_digest
    assert package.review_package_digest == digests.review_package_digest
    assert len(package.artifact_revision_digest) == 64
    # The seeded candidate's one applicability rule is `scope="task",
    # is_mandatory=False` -- per `RiskClassificationService`'s own
    # complete-rule-set reducer, that is `task_non_mandatory`.
    assert package.risk_classification == "task_non_mandatory"
    assert package.risk_algorithm_version == CURRENT_RISK_ALGORITHM_VERSION


# ---------------------------------------------------------------------------
# Submitter identity must not depend on the audit outbox. Every other test
# in this file proves "refuses because it recomputed and disagreed"; this
# one proves the opposite shape of claim -- that deleting the same-
# transaction `arc.proposal.submitted` outbox row (standing in for the
# outbox being pruned, archived, or partitioned by age) changes nothing
# about what the review package reports, because the submitter identity is
# no longer read from that row at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_package_submitter_identity_survives_a_missing_outbox_row(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Seed a real submitted version through the real `submit()` path, then
    delete the one `arc.proposal.submitted` outbox row that submission wrote
    -- the only place the submitter's issuer/subject lived before
    `arc_authoring_proposal_versions.submitted_by_issuer`/`submitted_by_
    subject` existed. The review package must still report the real
    submitter correctly: its identity comes from those columns, written by
    the same `freeze_and_link` compare-and-swap that froze this version, not
    from a lookup into the row just deleted.
    """
    tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-submitter-{uuid.uuid4().hex[:8]}"
    )

    async with factory() as session, session.begin():
        deleted = await session.execute(
            text(
                "DELETE FROM arc_audit_outbox WHERE event_type = 'arc.proposal.submitted' "
                "AND event_payload->>'proposal_id' = :pid"
            ),
            {"pid": str(proposal_id)},
        )
    assert deleted.rowcount == 1, "the seeding helper's submit() call must have written exactly one such row"

    service = _review_package(factory)
    package = await service.get_review_package(_ctx(tenant_id=tenant_id), proposal_id, proposal_version)

    assert package.submitted_by_issuer == _ISSUER
    assert package.submitted_by_subject == _OPERATOR


@pytest.mark.asyncio
async def test_baseline_diff_against_a_real_baseline(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A second version, opened against the first version's revision as its
    reviewed baseline, reports the changed `detail_audience` field."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-baseline-{uuid.uuid4().hex[:8]}"
    )

    # The first thread's own version is still `submitted` (nonterminal), and
    # ADR 040's "one nonterminal candidate per thread" rule refuses a second
    # `open_proposal` against the *same* artifact family while that holds.
    # A second, independent family stands in for "some other artifact whose
    # proposal reviews this one's now-frozen revision as its baseline" --
    # `open_proposal` places no constraint tying `reviewed_baseline_
    # revision_id` to the opening artifact's own family, and this test's
    # only concern is the diff mechanism, not which family the baseline
    # revision itself belongs to.
    second_artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    proposal_service = ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))
    second_thread = await proposal_service.open_proposal(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR),
        artifact_id=second_artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=revision_id,
    )
    second_candidate = _candidate(artifact_id=second_artifact_id, revision_id=uuid.uuid4())
    second_candidate["detail_audience"] = "human_only"
    second_candidate["reviewed_baseline_revision_id"] = str(revision_id)
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session,
            proposal_id=second_thread.proposal_id,
            proposal_version=second_thread.proposal_version,
            semantics=second_candidate,
        )

    service = _review_package(factory)
    diff = await service.get_baseline_diff(
        _ctx(tenant_id=tenant_id), second_thread.proposal_id, second_thread.proposal_version
    )

    assert diff.baseline_revision_id == revision_id
    changed_paths = {c.field_path: c for c in diff.changes if c.change_kind == "changed"}
    assert "$.detail_audience" in changed_paths
    assert changed_paths["$.detail_audience"].before == {"value": "agent_only"}
    assert changed_paths["$.detail_audience"].after == {"value": "human_only"}


# ---------------------------------------------------------------------------
# Digest substitution at R: the frozen expected-impact envelope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_digest_substitution_refuses(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Plant a wrong value into the one persisted digest column `assemble`
    reads for the envelope (`arc_expected_impact_envelopes.envelope_digest`)
    and prove the path refuses -- because it recomputed the envelope's own
    canonical digest from `arc_expected_impact_envelope_items` and found it
    disagreed, not because it trusted the corrupted column.
    """
    _tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-envelope-{uuid.uuid4().hex[:8]}"
    )
    service = _review_package(factory)

    # Baseline: assembly succeeds before any tampering.
    async with factory() as session:
        await service.assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_expected_impact_envelopes SET envelope_digest = :wrong "
                "WHERE proposal_id = :pid AND proposal_version = :pv"
            ),
            {"wrong": "f" * 64, "pid": proposal_id, "pv": proposal_version},
        )

    with pytest.raises(rp.ReviewPackageIntegrityError):
        async with factory() as session:
            await service.assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)


# ---------------------------------------------------------------------------
# Digest substitution at R: the sticky risk classification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_classification_substitution_refuses(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """Plant a wrong (but otherwise valid-looking) classification into the
    sticky `arc_risk_classifications` row and prove the path refuses --
    because it recomputed the classification from the frozen candidate under
    the same pinned algorithm version and found it disagreed.
    """
    _tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-risk-{uuid.uuid4().hex[:8]}"
    )
    service = _review_package(factory)

    async with factory() as session:
        digests_before = await service.assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)
    assert digests_before is not None

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_risk_classifications SET classification = :wrong "
                "WHERE proposal_id = :pid AND proposal_version = :pv"
            ),
            {"wrong": "global_mandatory", "pid": proposal_id, "pv": proposal_version},
        )

    with pytest.raises(rp.ReviewPackageIntegrityError):
        async with factory() as session:
            await service.assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)


# ---------------------------------------------------------------------------
# Digest substitution at A: completion recomputes and refuses on drift.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_refuses_when_the_committed_payload_digest_is_tampered(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """A wrong value planted directly into `arc_approval_challenges.
    approved_payload_digest` *after* issuance is a stand-in for `A`'s own
    persisted cache drifting from what a fresh `S, R -> A` recomputation
    produces. The signature still verifies (it covers `canonical_evidence_
    bytes`, untouched), so the honest question is whether `complete` trusts
    the tampered digest anyway or recomputes and disagrees -- it must do the
    latter.
    """
    tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-a-{uuid.uuid4().hex[:8]}"
    )
    review_package_service = _review_package(factory)
    service = _approval_challenges(factory, review_package_service)
    ctx = _ctx(tenant_id=tenant_id)

    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=public)

    issued = await service.create_challenge(
        ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key="k"
    )
    signature = _sign(private, issued.canonical_evidence_bytes)

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_approval_challenges SET approved_payload_digest = :wrong "
                "WHERE approval_challenge_id = :cid"
            ),
            {"wrong": "e" * 64, "cid": issued.approval_challenge_id},
        )

    with pytest.raises(acv.ApprovalVerificationFailed):
        await service.complete(ctx, issued.approval_challenge_id, proof=_proof(signature))

    # Refused without consuming an attempt or terminalizing the challenge --
    # this is drift detected after a *valid* signature, not a forged one.
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT attempt_count, state FROM arc_approval_challenges WHERE approval_challenge_id = :cid"),
                {"cid": issued.approval_challenge_id},
            )
        ).one()
    assert row.attempt_count == 0
    assert row.state == "issued"

    # And the proposal version never moved to `approved`.
    async with factory() as session:
        state = (
            await session.execute(
                text(
                    "SELECT state FROM arc_authoring_proposal_versions "
                    "WHERE proposal_id = :pid AND proposal_version = :pv"
                ),
                {"pid": proposal_id, "pv": proposal_version},
            )
        ).scalar_one()
    assert state == "submitted"


@pytest.mark.asyncio
async def test_completion_refuses_when_the_frozen_semantics_drift_after_issuance(
    factory: async_sessionmaker[AsyncSession], pg_container: str
) -> None:
    """`S` has no persisted digest cache anywhere `assemble` reads -- it is
    computed fresh from `arc_authoring_proposal_versions.semantics` every
    call, which is the strongest form of "never trust a cache" available
    when no cache exists. Proven here by mutating that one authoritative
    row directly (not a digest column, since none exists for `S`) *after*
    issuance and showing completion's fresh recomputation disagrees with
    what was committed and signed at issuance.
    """
    tenant_id, proposal_id, proposal_version, _revision_id = await _seed(
        factory, pg_container, slug=f"digest-chain-s-{uuid.uuid4().hex[:8]}"
    )
    review_package_service = _review_package(factory)
    service = _approval_challenges(factory, review_package_service)
    ctx = _ctx(tenant_id=tenant_id)

    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(session, approval_verifier_id=verifier_id, public_key=public)

    issued = await service.create_challenge(
        ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key="k"
    )
    signature = _sign(private, issued.canonical_evidence_bytes)

    async with factory() as session:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
    assert version is not None and version.semantics is not None
    drifted: dict[str, Any] = dict(version.semantics)
    drifted["detail_audience"] = "human_only"
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=proposal_id, proposal_version=proposal_version, semantics=drifted
        )

    with pytest.raises(acv.ApprovalVerificationFailed):
        await service.complete(ctx, issued.approval_challenge_id, proof=_proof(signature))
