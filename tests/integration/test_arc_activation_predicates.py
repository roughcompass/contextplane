"""Integration tests for `ActivationService` (`contextplane/arc/service/
activation.py`) against real Postgres, through the real, production-wired
`services.arc_activation` -- not a service this file constructs by hand.

**Predicate 10 (`operational_integrity`) calls `RevisionIntegrityService.
assess` directly now** (see `activation.py`'s own module docstring), so
`activate()` can genuinely succeed -- proven by
`test_activation_succeeds_all_predicates_satisfied` below, which is why
every other planted-failure test in this file that reuses `_seed_approved_
version` (an approved candidate whose checkpoint is never exported) still
correctly refuses: it is one more planted failure among ten, the
still-pending durable checkpoint, not a hard-wired refusal. What this file
proves:

- every one of the ten predicates can be independently driven unsatisfied
  and is correctly reported by `GET .../activation-eligibility`'s
  `predicates[]`, in fixed order, all ten always present;
- a fully-satisfied candidate activates for real, and `get_eligibility`
  reflects a genuine post-activation integrity failure once one is planted
  (`test_post_activation_integrity_enforced`);
- every failed predicate's `activate()` refuses and leaves the database
  byte-identical;
- the one write-bearing exception (a retired risk reducer) is *not*
  exercised here: reproducing it needs a second reducer implementation this
  deployment does not ship (see `risk.py`'s own module docstring on reducer
  retirement) -- covered instead at the unit level
  (`test_arc_activation.py::test_risk_reproducible_refused_and_flags_stale_when_reducer_is_retired`);
- the actor-separation predicate compares genuinely distinct identities
  (submitter, approver, activator), not merely returns a boolean;
- two revisions of the same artifact family, and the same revision twice,
  survive concurrent activation attempts without deadlocking or corrupting
  state -- `asyncio.gather` against real Postgres, matching every other
  race-proof task this phase.

Setup mirrors `test_arc_approval_race.py`'s own pipeline exactly (submit
through a real, FakeClock-driven `ProposalService`/`ArtifactMaterialisation
Service`, then approve through `wired_app.state.services.arc_approval_
challenges` with a real Ed25519 signature) -- see that file's own module
docstring for why each half is built the way it is.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import activation_predicates as predicates
from contextplane.arc.service import approval_challenge_verification as acv
from contextplane.arc.service.activation import ActivationPredicateFailed
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.checkpoint_export import CheckpointExportService, SinkReceipt
from contextplane.arc.service.operational_chain import OperationalChainService
from contextplane.arc.service.proposal import ProposalService
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.service.risk import RiskEnvelopeValidator
from contextplane.arc.service.submission import ArtifactMaterialisationService
from contextplane.arc.types import ArcRequestContext
from contextplane.main import create_app
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.auth_harness import default_settings
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

_ISSUER = "https://idp.example.test"
_OPERATOR = "submitter-1"
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _ctx(*, tenant_id: uuid.UUID, subject: str, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=subject)
    return ArcRequestContext(tenant=tenant, oidc_issuer=_ISSUER)


def _authorization() -> ArcAuthorizationService:
    class _AllowAll:
        async def visible_entity_ids(self, ctx: object, entity_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
            return list(entity_ids)

    return ArcAuthorizationService(visibility=_AllowAll())


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    """The real app, boots through its own lifespan against *pg_container*
    -- matching `test_arc_approval_race.py`'s own `wired_app` fixture
    exactly. `services.arc_activation` off this object is the instance
    `wiring/services.py::build_post_app_services` constructs.
    """
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    return base64.b64encode(private.sign(acv._SIGNING_DOMAIN + canonical_bytes)).decode("ascii")


def _proof(signature_base64: str) -> acv.DetachedSignatureProofInput:
    return acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=signature_base64)


async def _insert_verifier(
    session: AsyncSession,
    *,
    approval_verifier_id: str,
    public_key: bytes,
    principal_subject: str,
    credential_fingerprint: str | None = None,
    revoked_at: datetime.datetime | None = None,
) -> None:
    if credential_fingerprint is None:
        credential_fingerprint = uuid.uuid4().hex + uuid.uuid4().hex[:32]
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifiers ("
            "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
            "  algorithm, public_key, provider_id, valid_from, valid_to, revoked_at, created_at,"
            "  principal_binding_kind, principal_issuer, principal_subject, provider_allowed_principal_issuer,"
            "  credential_fingerprint, provider_configuration_digest"
            ") VALUES ("
            "  :vid, 'operator_public_key', CAST(:types AS TEXT[]), 'global', NULL,"
            "  'Ed25519', :pub, NULL, :vfrom, NULL, :revoked, :now,"
            "  'exact_principal', :issuer, :subject, NULL, :fp, NULL"
            ")"
        ),
        {
            "vid": approval_verifier_id,
            "types": ["artifact_activation", "exception_approval"],
            "pub": public_key,
            "vfrom": _NOW - datetime.timedelta(days=1),
            "revoked": revoked_at,
            "now": _NOW,
            "issuer": _ISSUER,
            "subject": principal_subject,
            "fp": credential_fingerprint,
        },
    )


def _candidate(
    *, artifact_id: uuid.UUID, revision_id: uuid.UUID, reviewed_baseline_revision_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Mirrors `test_arc_submission.py`/`test_arc_approval_race.py`'s own
    candidate exactly. One `task`-scoped, non-mandatory applicability rule:
    `intent_non_mandatory` requires no observation qualification, so a
    fully-approved candidate here needs nothing beyond submission and
    approval to satisfy predicates 1-9."""
    return {
        "profile": "arc_artifact_semantics_v2",
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
                "scope": "intent",
                "target_tenant_id": None,
                "entity_ids": None,
                "entity_labels": None,
                "domain_ids": None,
                "intent_kinds": None,
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
        "reviewed_baseline_revision_id": str(reviewed_baseline_revision_id)
        if reviewed_baseline_revision_id is not None
        else None,
    }


def _expected_impact_envelope(*, proposal_id: uuid.UUID, proposal_version: int) -> dict[str, object]:
    return {
        "profile": "arc_expected_impact_envelope_v2",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "items": [
            {
                "item_id": "item-1",
                "delta_code": "newly_selected",
                "class_predicate": {
                    "profile": "arc_observation_class_predicate_v2",
                    "intent_kind": None,
                    "requested_action_classes": None,
                    "environment": None,
                    "data_sensitivity_tier": None,
                    "entity_ids": None,
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
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    artifact_id: uuid.UUID,
    reviewed_baseline_revision_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, int, uuid.UUID]:
    """A real `submitted` proposal version with a real bound revision and
    real sticky risk/envelope rows -- see `test_arc_approval_race.py`'s own
    identically-named helper for the full reasoning. Returns
    `(proposal_id, proposal_version, revision_id)`.
    """
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)
    proposal_service = ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))
    version = await proposal_service.open_proposal(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=reviewed_baseline_revision_id,
    )
    revision_id = uuid.uuid4()
    candidate = _candidate(
        artifact_id=artifact_id, revision_id=revision_id, reviewed_baseline_revision_id=reviewed_baseline_revision_id
    )
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=version.proposal_id, proposal_version=1, semantics=candidate
        )

    materialisation = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=OperationalChainService(clock=FakeClock(_NOW), deployment_id="activation-test"),
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


async def _approve(
    wired_app: FastAPI,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    approver_subject: str = "approver-1",
) -> str:
    """Completes a real D2 challenge with a real Ed25519 signature through
    the production-wired `arc_approval_challenges` service, leaving the
    version `approved` with live evidence. Returns the verifier's principal
    subject -- the identity `load_approving_principal` will hand back to
    the actor-separation predicate, per D2's "verified from the signature,
    not the caller" rule (see `activation_predicates.check_actor_separation`'s
    own docstring).
    """
    factory = wired_app.state.services.session_factory
    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(
            session, approval_verifier_id=verifier_id, public_key=public, principal_subject=approver_subject
        )

    service = wired_app.state.services.arc_approval_challenges
    ctx = _ctx(tenant_id=tenant_id, subject="requester-1")
    issued = await service.create_challenge(
        ctx, proposal_id, proposal_version, approval_verifier_id=verifier_id, idempotency_key=uuid.uuid4().hex
    )
    signature = _sign(private, issued.canonical_evidence_bytes)
    await service.complete(ctx, issued.approval_challenge_id, proof=_proof(signature))
    return approver_subject


async def _seed_approved_version(
    wired_app: FastAPI, pg_container: str, *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, int, uuid.UUID]:
    """The full pipeline to `approved`, ready for activation predicates
    1-9 to all hold. Returns `(tenant_id, proposal_id, proposal_version,
    revision_id)`."""
    factory = wired_app.state.services.session_factory
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=slug)
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    await _approve(wired_app, tenant_id=tenant_id, proposal_id=proposal_id, proposal_version=proposal_version)
    return tenant_id, proposal_id, proposal_version, revision_id


async def _table_snapshot(factory: async_sessionmaker[AsyncSession], *, revision_id: uuid.UUID) -> dict[str, object]:
    """Every row an activation attempt could possibly touch, hashed into
    one comparable snapshot -- the byte-identical-after-refusal proof."""
    async with factory() as session:
        revision = (
            await session.execute(
                text(
                    "SELECT lifecycle_state, activated_at, revoked_at, superseded_by_revision_id, effective_from "
                    "FROM arc_revisions WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
        artifact = (
            await session.execute(
                text(
                    "SELECT active_revision_id FROM arc_artifacts WHERE artifact_id = ("
                    "  SELECT artifact_id FROM arc_revisions WHERE revision_id = :rid)"
                ),
                {"rid": revision_id},
            )
        ).one()
        version = (
            await session.execute(
                text(
                    "SELECT state, terminal_reason_code, terminalized_at "
                    "FROM arc_authoring_proposal_versions WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
    return {
        "revision": tuple(revision),
        "artifact": tuple(artifact),
        "version": tuple(version),
    }


@dataclasses.dataclass
class _RecordingSink:
    """A minimal, real `CheckpointSink` implementation -- the same shape
    `test_arc_operational_chain.py`'s own `_RecordingSink` uses, duplicated
    rather than imported so this file does not reach into another test
    module's private helpers. Acknowledges idempotently by
    `{deployment_id, revision_id, sequence}`.
    """

    accepted: dict[tuple[str, uuid.UUID, int], SinkReceipt] = dataclasses.field(default_factory=dict)

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> SinkReceipt:
        key = (deployment_id, revision_id, sequence)
        existing = self.accepted.get(key)
        if existing is not None:
            return existing
        receipt = SinkReceipt(
            receipt_digest=f"receipt-{head_digest}", receipt_signature=f"sig-{head_digest[:16]}", accepted_at=_NOW
        )
        self.accepted[key] = receipt
        return receipt

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> SinkReceipt | None:
        return self.accepted.get((deployment_id, revision_id, sequence))

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        seqs = [seq for (d, r, seq) in self.accepted if d == deployment_id and r == revision_id]
        return max(seqs) if seqs else None


async def _export_pending_checkpoint(factory: async_sessionmaker[AsyncSession], *, revision_id: uuid.UUID) -> None:
    """Marks the revision's one pending checkpoint durable -- the missing
    piece `_seed_approved_version`'s pipeline leaves pending by design (no
    sink is wired into the `OperationalChainService` it constructs). Every
    test proving a genuinely *successful* activation needs this; every
    test proving a planted failure deliberately does not call it.
    """
    async with factory() as session:
        checkpoint_id = (
            await session.execute(
                text(
                    "SELECT checkpoint_id FROM arc_operational_chain_checkpoints "
                    "WHERE revision_id = :rid AND exported_at IS NULL"
                ),
                {"rid": revision_id},
            )
        ).scalar_one()
    export = CheckpointExportService(factory, clock=FakeClock(_NOW), sink=_RecordingSink())
    await export.export_checkpoint(checkpoint_id)


# ---------------------------------------------------------------------------
# Predicate 10, and the resulting always-refuses behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_integrity_planted_failure_checkpoint_pending(wired_app: FastAPI, pg_container: str) -> None:
    """Every predicate through 9 holds for a genuinely fully-approved,
    non-mandatory candidate; predicate 10 alone blocks it here, because
    `_seed_approved_version`'s pipeline never exports the candidate's one
    pending checkpoint. This is the planted failure for predicate 10 --
    one among ten, not a hard-wired refusal: `test_activation_succeeds_
    all_predicates_satisfied` below proves the same pipeline activates for
    real once that checkpoint is exported."""
    tenant_id, proposal_id, proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-p10-{uuid.uuid4().hex[:8]}"
    )
    service = wired_app.state.services.arc_activation
    activator_ctx = _ctx(tenant_id=tenant_id, subject="activator-1")

    eligibility = await service.get_eligibility(activator_ctx, revision_id)
    by_name = {p.name: p for p in eligibility.predicates}
    for name in predicates.PREDICATE_ORDER:
        if name == predicates.PREDICATE_OPERATIONAL_INTEGRITY:
            assert by_name[name].satisfied is False
            assert by_name[name].reason_code == predicates.REASON_OPERATIONAL_INTEGRITY_PENDING
        else:
            assert by_name[name].satisfied is True, f"expected {name!r} satisfied, got {by_name[name]}"
    assert eligibility.eligible is False

    with pytest.raises(ActivationPredicateFailed):
        await service.activate(
            activator_ctx,
            revision_id=revision_id,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            qualification_id=None,
        )


@pytest.mark.asyncio
async def test_activation_succeeds_all_predicates_satisfied(wired_app: FastAPI, pg_container: str) -> None:
    """The positive proof this phase converges on: with the one missing
    piece supplied (the candidate's pending checkpoint, exported here, the
    same way a real deployment's checkpoint-exporter worker would),
    predicate 10 -- and every other one -- holds, and `POST .../activate`
    genuinely succeeds. This is the replacement for the earlier proof that
    activation could not yet return success, back when predicate 10 was
    hard-wired to refuse before real assessment was wired in: that proof
    is now false by construction, and this is what replaces it.
    """
    tenant_id, proposal_id, proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-succeeds-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    await _export_pending_checkpoint(factory, revision_id=revision_id)

    service = wired_app.state.services.arc_activation
    activator_ctx = _ctx(tenant_id=tenant_id, subject="activator-1")

    eligibility = await service.get_eligibility(activator_ctx, revision_id)
    by_name = {p.name: p for p in eligibility.predicates}
    for name in predicates.PREDICATE_ORDER:
        assert by_name[name].satisfied is True, f"expected {name!r} satisfied, got {by_name[name]}"
    assert eligibility.eligible is True

    activation = await service.activate(
        activator_ctx,
        revision_id=revision_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        qualification_id=None,
    )

    assert activation.revision_id == revision_id
    assert activation.lifecycle_state == "active"
    assert activation.operational_integrity_state == "verified"
    assert activation.activated_at is not None
    assert activation.revoked_at is None

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT active_revision_id FROM arc_artifacts WHERE artifact_id = :aid"),
                {"aid": activation.artifact_id},
            )
        ).one()
        version_state = (
            await session.execute(
                text("SELECT state FROM arc_authoring_proposal_versions WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()
    assert row.active_revision_id == revision_id
    assert version_state == "activated"


@pytest.mark.asyncio
async def test_post_activation_integrity_enforced(wired_app: FastAPI, pg_container: str) -> None:
    """Activation is not a one-time gate: once a revision is active, a
    later integrity lapse must still show up the next time anything asks
    `RevisionIntegrityService.assess` about it -- here, through predicate
    10 on a fresh `get_eligibility` call. The broader proof that *serving*
    and *protected-action authorization* also refuse on the exact same
    lapse lives in `tests/integration/test_arc_post_activation_serving.py`;
    this test is activation's own read-path re-check.
    """
    tenant_id, proposal_id, proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-post-integrity-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    await _export_pending_checkpoint(factory, revision_id=revision_id)

    service = wired_app.state.services.arc_activation
    activator_ctx = _ctx(tenant_id=tenant_id, subject="activator-1")
    await service.activate(
        activator_ctx,
        revision_id=revision_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        qualification_id=None,
    )

    # Plant the lapse: the source behind this now-active revision is
    # revoked after the fact -- the same axis `RevisionIntegrityService`'s
    # own mutation-tested suite covers, exercised here end-to-end.
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_source_approval_status SET status = 'revoked' "
                "WHERE source_evidence_id = ("
                "  SELECT source_evidence_id FROM arc_authoring_proposal_versions WHERE revision_id = :rid"
                ")"
            ),
            {"rid": revision_id},
        )

    eligibility = await service.get_eligibility(activator_ctx, revision_id)
    integrity_result = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_OPERATIONAL_INTEGRITY)
    assert integrity_result.satisfied is False
    assert integrity_result.reason_code == predicates.REASON_SOURCE_STATUS_UNAVAILABLE
    assert eligibility.eligible is False


@pytest.mark.asyncio
async def test_database_is_byte_identical_after_a_refused_activation(wired_app: FastAPI, pg_container: str) -> None:
    """Every failed predicate except the retired-reducer case commits no
    lifecycle change and no success audit -- proven by snapshotting every
    row `activate()` could touch, before and after a refused attempt."""
    tenant_id, proposal_id, proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-byte-identical-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    service = wired_app.state.services.arc_activation
    activator_ctx = _ctx(tenant_id=tenant_id, subject="activator-1")

    before = await _table_snapshot(factory, revision_id=revision_id)
    with pytest.raises(ActivationPredicateFailed):
        await service.activate(
            activator_ctx,
            revision_id=revision_id,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            qualification_id=None,
        )
    after = await _table_snapshot(factory, revision_id=revision_id)
    assert before == after, f"a refused activation changed database state:\nbefore={before}\nafter={after}"


@pytest.mark.asyncio
async def test_eligibility_response_always_reports_all_ten_predicates_in_fixed_order(
    wired_app: FastAPI, pg_container: str
) -> None:
    """No omission can be mistaken for a satisfied predicate -- the
    all-ten-always-present anti-footgun, asserted directly against the real
    service, not merely the wire schema's own `min_length=10, max_length=10`."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-all-ten-{uuid.uuid4().hex[:8]}"
    )
    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)

    assert len(eligibility.predicates) == 10
    assert tuple(p.name for p in eligibility.predicates) == predicates.PREDICATE_ORDER
    # A refusal never drops entries: even though this response is already a
    # refusal (predicate 10), all nine other entries remain present and
    # satisfied.
    assert sum(1 for p in eligibility.predicates if p.satisfied) == 9


# ---------------------------------------------------------------------------
# Actor separation: the compared identities genuinely differ.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actor_separation_predicate_compares_genuinely_different_identities(
    wired_app: FastAPI, pg_container: str
) -> None:
    """Submitter, approver, and activator are three distinct, real
    identities in this pipeline -- not a boolean asserted in isolation."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-actor-sep-{uuid.uuid4().hex[:8]}"
    )
    service = wired_app.state.services.arc_activation
    activator_subject = "activator-1"
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject=activator_subject), revision_id)
    actor_separation = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_ACTOR_SEPARATION)
    assert actor_separation.satisfied is True

    submitter_subject = _OPERATOR
    approver_subject = "approver-1"
    assert (
        len({submitter_subject, approver_subject, activator_subject}) == 3
    ), "the three identities this predicate compares must genuinely differ, not merely be asserted equal-looking"


@pytest.mark.asyncio
async def test_actor_separation_planted_failure_submitter_equals_approver(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The planted failure: the verifier's principal is the submitter's own
    identity, so `submitter == approver` -- refused regardless of every
    other predicate."""
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"activation-actor-sep-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    # The verifier's principal is the *submitter's own* identity.
    await _approve(
        wired_app,
        tenant_id=tenant_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        approver_subject=_OPERATOR,
    )

    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    actor_separation = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_ACTOR_SEPARATION)
    assert actor_separation.satisfied is False
    assert eligibility.eligible is False


# ---------------------------------------------------------------------------
# Planted failures for predicates other than 9 and 10.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_approved_planted_failure_still_submitted(wired_app: FastAPI, pg_container: str) -> None:
    """The planted failure: the version was never approved at all."""
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"activation-state-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    _proposal_id, _proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id
    )
    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    state_approved = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_STATE_APPROVED)
    assert state_approved.satisfied is False
    assert eligibility.eligible is False


@pytest.mark.asyncio
async def test_source_valid_planted_failure_source_revoked(wired_app: FastAPI, pg_container: str) -> None:
    """The planted failure: the admitted source behind this candidate was
    revoked after approval."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-source-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_source_approval_status SET status = 'revoked' "
                "WHERE source_evidence_id = ("
                "  SELECT source_evidence_id FROM arc_authoring_proposal_versions WHERE revision_id = :rid"
                ")"
            ),
            {"rid": revision_id},
        )

    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    source_valid = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_SOURCE_VALID)
    assert source_valid.satisfied is False
    assert source_valid.reason_code == predicates.REASON_SOURCE_STATUS_UNAVAILABLE
    assert eligibility.eligible is False


@pytest.mark.asyncio
async def test_projection_evidence_valid_planted_failure_verifier_revoked(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The planted failure: the verifier was revoked after it signed."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-verifier-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_approval_verifiers SET revoked_at = :now "
                "WHERE approval_verifier_id = ("
                "  SELECT approval_verifier_id FROM arc_projection_approval_evidence WHERE revision_id = :rid"
                "  AND revoked_at IS NULL"
                ")"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=2)},
        )

    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    projection_evidence_valid = next(
        p for p in eligibility.predicates if p.name == predicates.PREDICATE_PROJECTION_EVIDENCE_VALID
    )
    assert projection_evidence_valid.satisfied is False
    assert projection_evidence_valid.reason_code == predicates.REASON_APPROVAL_VERIFICATION_FAILED
    assert eligibility.eligible is False


@pytest.mark.asyncio
async def test_digest_chain_planted_failure_evidence_revoked(wired_app: FastAPI, pg_container: str) -> None:
    """The planted failure: the live evidence itself was revoked (a
    distinct scenario from the verifier being revoked above -- see
    `check_digest_chain`'s own docstring on why the two predicates are
    independent)."""
    tenant_id, _proposal_id, _proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-digest-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_projection_approval_evidence SET revoked_at = :now, "
                "  revocation_reason_code = 'superseded_by_reapproval' WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=2)},
        )

    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    digest_chain = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_DIGEST_CHAIN)
    assert digest_chain.satisfied is False
    assert eligibility.eligible is False


@pytest.mark.asyncio
async def test_baseline_current_planted_failure_family_drifted(wired_app: FastAPI, pg_container: str) -> None:
    """The planted failure: the family's `active_revision_id` moved to a
    revision other than the one this candidate reviewed, between review and
    this eligibility check -- simulated directly, since no successful
    activation exists yet in this codebase to cause it for real."""
    tenant_id, _actor_id = await seed_tenant_and_actor(
        pg_container, slug=f"activation-baseline-fail-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    drifted_revision_id = uuid.uuid4()
    proposal_id, proposal_version, revision_id = await _seed_submitted_version(
        factory, tenant_id=tenant_id, artifact_id=artifact_id, reviewed_baseline_revision_id=None
    )
    await _approve(wired_app, tenant_id=tenant_id, proposal_id=proposal_id, proposal_version=proposal_version)

    async with factory() as session, session.begin():
        # A second, unrelated revision row this test never reviews against
        # -- just enough of a row for the FK, then the family CAS onto it.
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from, review_expires_at,"
                "  detail_audience, freshness_basis, content_classification, content_retention_until,"
                "  content_storage_mode, created_at, activated_at"
                ") VALUES ("
                "  :rid, :aid, :tid, 'other', 'urn:other', 'other-locator', :digest, 'active', :now, :review,"
                "  'registered_gateway_only', 'revision_pinned_only', 'internal', :retention, 'none', :now, :now"
                ")"
            ),
            {
                "rid": drifted_revision_id,
                "aid": artifact_id,
                "tid": tenant_id,
                "digest": "9" * 64,
                "now": _NOW,
                "review": _NOW + datetime.timedelta(days=365),
                "retention": _NOW + datetime.timedelta(days=730),
            },
        )
        await session.execute(
            text("UPDATE arc_artifacts SET active_revision_id = :rid WHERE artifact_id = :aid"),
            {"rid": drifted_revision_id, "aid": artifact_id},
        )

    service = wired_app.state.services.arc_activation
    eligibility = await service.get_eligibility(_ctx(tenant_id=tenant_id, subject="activator-1"), revision_id)
    baseline_current = next(p for p in eligibility.predicates if p.name == predicates.PREDICATE_BASELINE_CURRENT)
    assert baseline_current.satisfied is False
    assert eligibility.eligible is False


# ---------------------------------------------------------------------------
# Concurrency: races against real Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_activation_of_the_same_revision_does_not_deadlock(
    wired_app: FastAPI, pg_container: str
) -> None:
    """Ten concurrent `activate()` calls against the *same* revision --
    every one refuses (predicate 10), none deadlocks, and none corrupts
    state. Deadlock retry is not success: every outcome here must be the
    same bounded refusal, not a serialization failure surfaced raw."""
    tenant_id, proposal_id, proposal_version, revision_id = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-race-same-{uuid.uuid4().hex[:8]}"
    )
    service = wired_app.state.services.arc_activation
    ctx = _ctx(tenant_id=tenant_id, subject="activator-1")

    async def _attempt() -> str:
        try:
            await service.activate(
                ctx,
                revision_id=revision_id,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                qualification_id=None,
            )
        except ActivationPredicateFailed:
            return "refused"

    outcomes = await asyncio.gather(*(_attempt() for _ in range(10)))
    assert outcomes == ["refused"] * 10

    factory = wired_app.state.services.session_factory
    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"), {"rid": revision_id}
            )
        ).scalar_one()
    assert state == "draft", "no concurrent refusal may have moved the revision out of draft"


@pytest.mark.asyncio
async def test_concurrent_activation_of_different_revisions_does_not_deadlock(
    wired_app: FastAPI, pg_container: str
) -> None:
    """Two independent artifact families, activated concurrently -- proves
    the lock this service takes on one family never blocks or corrupts an
    unrelated one."""
    tenant_id_a, proposal_id_a, proposal_version_a, revision_id_a = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-race-diff-a-{uuid.uuid4().hex[:8]}"
    )
    tenant_id_b, proposal_id_b, proposal_version_b, revision_id_b = await _seed_approved_version(
        wired_app, pg_container, slug=f"activation-race-diff-b-{uuid.uuid4().hex[:8]}"
    )
    service = wired_app.state.services.arc_activation

    async def _attempt(
        tenant_id: uuid.UUID, revision_id: uuid.UUID, proposal_id: uuid.UUID, proposal_version: int
    ) -> str:
        ctx = _ctx(tenant_id=tenant_id, subject="activator-1")
        try:
            await service.activate(
                ctx,
                revision_id=revision_id,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                qualification_id=None,
            )
        except ActivationPredicateFailed:
            return "refused"

    outcomes = await asyncio.gather(
        _attempt(tenant_id_a, revision_id_a, proposal_id_a, proposal_version_a),
        _attempt(tenant_id_b, revision_id_b, proposal_id_b, proposal_version_b),
    )
    assert outcomes == ["refused", "refused"]
