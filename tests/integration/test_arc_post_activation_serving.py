"""AAS-T20's item-4 proof: activate a real candidate, then show that
mandatory serving (`corpus.py` + `selection.py`) and protected-action
authorization (`authorization.py`) all refuse once any one of
`RevisionIntegrityService.assess`'s five axes goes bad on the now-active
revision.

Setup mirrors `test_arc_activation_predicates.py`'s own pipeline (submit
through a real `ProposalService`/`ArtifactMaterialisationService`, approve
through `arc_approval_challenges` with a real Ed25519 signature, export the
one pending checkpoint, then activate) with one addition: the candidate
here carries one real `citation_only` directive in its own `directives[]`,
so it is a genuine candidate `CorpusReader.assemble` and `select_and_verify`
can select -- `test_arc_activation_predicates.py`'s own candidate carries
none, which is enough for activation's ten predicates but gives selection
nothing to serve at all.

**The directive and its applicability rule arrive through submission
itself, not a seeded `INSERT`.** An earlier version of this file inserted
`arc_directives`/`arc_applicability_rules` rows directly by SQL after
`ArtifactMaterialisationService.submit` returned, because submission wrote
`arc_revisions` only -- the gap this repo's own AAS-T34 exists to close.
That scaffold proved the read path (`corpus.py`/`selection.py`/
`authorization.py`) refuses correctly on an integrity-failed revision; it
never proved the authoring surface itself could produce anything for that
read path to refuse. Now that `submit` materialises the candidate's own
`directives[]`/`applicability[]` in the same transaction as the revision
row, this file's one directive and one rule are just fields on `_candidate`
-- the identical shape `test_arc_submission.py` and `test_arc_
materialisation.py` already exercise for the writer itself, exercised here
end to end through activation and mandatory-context resolution.

**Why the applicability rule stays non-mandatory here.** ADR 041's own
reducer (`risk.py`) classifies *any* `is_mandatory=True` rule as requiring
observation qualification before activation, regardless of scope -- making
this file's candidate mandatory would mean standing up a full shadow/
qualification pipeline just to reach an activated revision at all. The
mandatory-blocks-the-whole-resolution property of `select_and_verify` is
proven directly, without that overhead, in `tests/unit/test_arc_selection.
py`'s own synthetic-fixture suite; this file proves the axis-detection
side against a real, activated revision, and exercises the DEGRADED (not
BLOCKED) branch for the optional directive it actually has.

**Why "checkpoint pending" and "checkpoint unavailable" share one
assertion.** `RevisionIntegrityService`'s own durable-checkpoint axis
(`integrity.py::_check_durable_checkpoint`) returns the identical bounded
code, `arc_operational_integrity_pending`, whether the checkpoint row is
merely unexported or entirely absent -- by design, per that axis's own
"never disclose which check failed" contract. This file plants both
shapes (reverting an exported checkpoint's receipt columns to `NULL`, and
deleting the checkpoint row outright) and asserts the same code from each,
rather than inventing a distinction the production code does not draw.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.service import approval_challenge_verification as acv
from registry.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from registry.arc.service.checkpoint_export import CheckpointExportService, SinkReceipt
from registry.arc.service.integrity import (
    PURPOSE_AUTHORIZATION,
    REASON_OPERATIONAL_INTEGRITY_FAILED,
    REASON_OPERATIONAL_INTEGRITY_PENDING,
    REASON_PROJECTION_EVIDENCE_INVALID,
    REASON_SOURCE_STATUS_UNAVAILABLE,
)
from registry.arc.service.operational_chain import OperationalChainService
from registry.arc.service.proposal import ProposalService
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.risk import RiskEnvelopeValidator
from registry.arc.service.selection import SelectionInput, select_and_verify
from registry.arc.service.submission import ArtifactMaterialisationService
from registry.arc.types import ActionClass, ArcRequestContext, TaskKind, TaskManifest
from registry.main import create_app
from registry.types import TenantContext
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
        async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
            return list(capability_ids)

    return ArcAuthorizationService(visibility=_AllowAll())


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    """The real app, through its own lifespan -- matching every sibling
    activation/approval integration test's own `wired_app` fixture."""
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
    session: AsyncSession, *, approval_verifier_id: str, public_key: bytes, principal_subject: str
) -> None:
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
            "subject": principal_subject,
            "fp": credential_fingerprint,
        },
    )


def _directive(*, directive_id: uuid.UUID) -> dict[str, object]:
    """One real, servable `citation_only` directive for the candidate's own
    `directives[]` -- materialised into `arc_directives` by `submit` itself
    (see `ArtifactMaterialisationService._directive_row`), not seeded by a
    direct `INSERT` the way `tests/helpers/arc_fixtures.py::seed_arc` still
    does for the receipt-path tests that have nothing to do with the
    authoring surface's own writer.

    `citation_only` deliberately: it is the one `directive_type` this
    deployment's persisted vocabulary can materialise today (see
    `_directive_row`'s own docstring on the `verify_before_action`/
    `self_attested` wire literals that have no destination there yet), so
    every other field below is `None` -- not a scaffold, but this
    directive's actual (empty) conflict key and verification profile.
    """
    statement = "Cite the approved runbook."
    return {
        "directive_id": str(directive_id),
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
        "created_at": _NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _candidate(*, artifact_id: uuid.UUID, revision_id: uuid.UUID, directive_id: uuid.UUID) -> dict[str, object]:
    """`test_arc_activation_predicates.py::_candidate`, plus one real
    directive in `directives[]` -- that file's own candidate carries none
    on purpose (enough for activation, nothing for selection to serve);
    this file needs exactly one, and it now reaches `arc_directives`
    through `submit` itself rather than a seeded `INSERT`."""
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
        "directives": [_directive(directive_id=directive_id)],
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


def _manifest() -> TaskManifest:
    """Matches the candidate's own task-scoped, selector-free applicability
    rule: an empty selector on every dimension means "matches any"."""
    return TaskManifest(
        session_id="serving-test",
        task_kind=TaskKind.CODE_CHANGE,
        requested_action_classes=frozenset({ActionClass.MERGE}),
    )


@dataclasses.dataclass
class _RecordingSink:
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


async def _seed_and_activate(wired_app: FastAPI, pg_container: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Submit, approve, export the checkpoint, and activate a real
    candidate carrying one real directive. Returns `(tenant_id,
    revision_id)`.
    """
    factory = wired_app.state.services.session_factory
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=slug)
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    proposal_service = ProposalService(factory, authorization=_authorization(), clock=FakeClock(_NOW))
    version = await proposal_service.open_proposal(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=None,
    )
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    candidate = _candidate(artifact_id=artifact_id, revision_id=revision_id, directive_id=directive_id)
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=version.proposal_id, proposal_version=1, semantics=candidate
        )

    materialisation = ArtifactMaterialisationService(
        factory,
        authorization=_authorization(),
        clock=FakeClock(_NOW),
        operational_chain_appender=OperationalChainService(clock=FakeClock(_NOW), deployment_id="serving-test"),
        risk_envelope_validator=RiskEnvelopeValidator(),
    )
    envelope = {
        "profile": "arc_expected_impact_envelope_v1",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(version.proposal_id),
        "proposal_version": 1,
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
    result = await materialisation.submit(
        _ctx(tenant_id=tenant_id, subject=_OPERATOR), version.proposal_id, 1, expected_impact_envelope=envelope
    )
    assert result.revision_id == revision_id
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
    assert directive_count == 1, "submit must materialise the candidate's own directive -- no seeded INSERT here"
    assert rule_count == 1, "submit must materialise the candidate's own applicability rule -- no seeded INSERT here"

    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(
            session, approval_verifier_id=verifier_id, public_key=public, principal_subject="approver-1"
        )

    approval_service = wired_app.state.services.arc_approval_challenges
    requester_ctx = _ctx(tenant_id=tenant_id, subject="requester-1")
    issued = await approval_service.create_challenge(
        requester_ctx, version.proposal_id, 1, approval_verifier_id=verifier_id, idempotency_key=uuid.uuid4().hex
    )
    signature = _sign(private, issued.canonical_evidence_bytes)
    await approval_service.complete(requester_ctx, issued.approval_challenge_id, proof=_proof(signature))

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

    activation_service = wired_app.state.services.arc_activation
    activator_ctx = _ctx(tenant_id=tenant_id, subject="activator-1")
    activation = await activation_service.activate(
        activator_ctx,
        revision_id=revision_id,
        proposal_id=version.proposal_id,
        proposal_version=1,
        qualification_id=None,
    )
    assert activation.lifecycle_state == "active"
    return tenant_id, revision_id


async def _assert_mandatory_serving_and_authorization_refuse(
    wired_app: FastAPI, *, tenant_id: uuid.UUID, revision_id: uuid.UUID, expected_reason_code: str
) -> None:
    """The shared assertion every planted-axis test below runs: the
    activated revision's one directive is excluded from a fresh corpus
    assembly, `select_and_verify`'s own independent recheck (against the
    corpus's *pre-filter* candidates, so it is genuinely selection's own
    check being exercised, not merely inheriting corpus's) degrades with
    the axis's bounded code, and protected-action authorization for this
    revision is denied with the same code.
    """
    services = wired_app.state.services
    factory = services.session_factory
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)

    # 1. Mandatory corpus assembly: the directive must not survive.
    corpus_input = await services.arc_corpus.assemble(tenant_id=tenant_id, manifest=_manifest(), as_of=as_of)
    assert corpus_input.candidates == (), "corpus assembly served a directive from an integrity-failed revision"

    # 2. Selection's own authoritative recheck, against the *unfiltered*
    #    candidates corpus.py itself reads before applying its own
    #    integrity prefilter -- this isolates selection.py's own call from
    #    corpus.py's, matching item 3's "each caller independently" bar.
    async with factory() as session:
        raw_candidates = await services.arc_corpus._candidates(session, tenant_id=tenant_id, as_of=as_of)
        assert raw_candidates != (), "the fixture itself produced no candidate to test against"
        selection_input = SelectionInput(
            manifest=_manifest(), tenant_id=tenant_id, as_of=as_of, candidates=raw_candidates
        )
        selection_result = await select_and_verify(session, selection_input, services.arc_integrity)
    assert selection_result.optional == (), "an integrity-failed revision's directive was still offered"
    assert expected_reason_code in selection_result.degraded_reasons

    # 3. Protected-action authorization: denied, carrying the same code.
    async with factory() as session:
        with pytest.raises(ArcAuthorizationError) as exc_info:
            await services.arc_authorization.assert_protected_action_authorized(
                session, revision_id, integrity=services.arc_integrity
            )
    assert exc_info.value.reason == expected_reason_code


@pytest.mark.asyncio
async def test_source_revoked_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-source-{uuid.uuid4().hex[:8]}"
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

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app, tenant_id=tenant_id, revision_id=revision_id, expected_reason_code=REASON_SOURCE_STATUS_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_projection_evidence_revoked_refuses_serving_and_authorization(
    wired_app: FastAPI, pg_container: str
) -> None:
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-evidence-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_projection_approval_evidence SET revoked_at = :now, "
                "  revocation_reason_code = 'superseded_by_reapproval' WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=1)},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_PROJECTION_EVIDENCE_INVALID,
    )


@pytest.mark.asyncio
async def test_checkpoint_reverted_to_pending_refuses_serving_and_authorization(
    wired_app: FastAPI, pg_container: str
) -> None:
    """The checkpoint this revision activated with is reverted to
    unexported -- simulating a local/sink reconciliation that un-durables
    it (see `checkpoint_export.py`'s own module docstring for the real
    scenarios this stands in for)."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-checkpoint-pending-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_operational_chain_checkpoints SET exported_at = NULL, sink_receipt_digest = NULL, "
                "  sink_receipt_signature = NULL WHERE revision_id = :rid"
            ),
            {"rid": revision_id},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_PENDING,
    )


@pytest.mark.asyncio
async def test_checkpoint_deleted_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    """No checkpoint row at all for this revision -- `_check_durable_
    checkpoint` treats this identically to an unexported one (see the
    module docstring for why: the bounded code intentionally does not
    distinguish "never durable" from "no longer durable")."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-checkpoint-gone-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_operational_chain_checkpoints WHERE revision_id = :rid"), {"rid": revision_id}
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_PENDING,
    )


@pytest.mark.asyncio
async def test_cached_state_drift_refuses_serving_and_authorization(wired_app: FastAPI, pg_container: str) -> None:
    """The sticky risk classification this revision was approved and
    activated under no longer agrees with what recomputing it from the
    frozen semantics produces -- the axis 2 cache-drift check
    `ReviewPackageService.assemble` performs on every call, never trusting
    the persisted column as truth."""
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-cache-drift-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_risk_classifications SET classification = 'global_mandatory' "
                "WHERE proposal_id = (SELECT proposal_id FROM arc_authoring_proposal_versions WHERE revision_id = :rid)"
            ),
            {"rid": revision_id},
        )

    await _assert_mandatory_serving_and_authorization_refuse(
        wired_app,
        tenant_id=tenant_id,
        revision_id=revision_id,
        expected_reason_code=REASON_OPERATIONAL_INTEGRITY_FAILED,
    )


@pytest.mark.asyncio
async def test_no_refusal_discloses_evidence_verifier_or_digest(wired_app: FastAPI, pg_container: str) -> None:
    """`RevisionIntegrityService.assess` never returns evidence bytes, a
    verifier identity, or a digest, on any path (`AAS-T18`'s own contract,
    proven again at this boundary). This walks every refusal this file
    produces and asserts none of them leaked through `ArcAuthorizationError`,
    `SelectionResult`, or `SelectionInput`'s own `repr`/`str`.
    """
    tenant_id, revision_id = await _seed_and_activate(
        wired_app, pg_container, slug=f"serving-no-leak-{uuid.uuid4().hex[:8]}"
    )
    factory = wired_app.state.services.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_projection_approval_evidence SET revoked_at = :now, "
                "  revocation_reason_code = 'superseded_by_reapproval' WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "now": _NOW + datetime.timedelta(days=1)},
        )

    services = wired_app.state.services
    as_of = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=1)
    async with factory() as session:
        raw_candidates = await services.arc_corpus._candidates(session, tenant_id=tenant_id, as_of=as_of)
        inputs = SelectionInput(manifest=_manifest(), tenant_id=tenant_id, as_of=as_of, candidates=raw_candidates)
        selection_result = await select_and_verify(session, inputs, services.arc_integrity)

    async with factory() as session:
        with pytest.raises(ArcAuthorizationError) as exc_info:
            await services.arc_authorization.assert_protected_action_authorized(
                session, revision_id, integrity=services.arc_integrity
            )

    rendered = f"{selection_result!r} {exc_info.value!r} {exc_info.value}"
    secrets = frozenset({str(revision_id), str(tenant_id), PURPOSE_AUTHORIZATION})
    for secret in secrets:
        assert secret not in rendered, f"{secret!r} leaked through a refusal this file produced"
