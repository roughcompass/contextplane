"""The real authoring pipeline every integration test that needs a
surface-authored, activated revision reuses: submit() through
`ProposalService`/`ArtifactMaterialisationService`, approve through
`arc_approval_challenges` with a real Ed25519 signature, export the one
pending checkpoint, then activate.

Extracted out of `tests/integration/test_arc_post_activation_serving.py`,
which built this pipeline first -- that file's own module docstring still
carries the fuller account of why every directive and applicability rule
here arrives through submission itself rather than a seeded `INSERT`: an
earlier version inserted `arc_directives`/`arc_applicability_rules` rows
directly by SQL after `submit()` returned, which proved the read path
(`corpus.py`/`selection.py`/`authorization.py`) could refuse a bad revision
without ever proving the authoring surface itself could produce anything
for that read path to refuse. A second integration-test file that needs the
identical real pipeline -- to drive something downstream of activation
other than corpus/selection/authorization -- reuses this module rather than
reimplementing the pipeline a second time, which would recreate exactly the
scaffold risk this module exists to retire, just duplicated in the file
that copied it.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.service import approval_challenge_verification as acv
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.checkpoint_export import CheckpointExportService, SinkReceipt
from contextplane.arc.service.operational_chain import OperationalChainService
from contextplane.arc.service.proposal import ProposalService
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.service.risk import RiskEnvelopeValidator
from contextplane.arc.service.submission import ArtifactMaterialisationService
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import seed_artifact_family, seed_source_evidence
from tests.helpers.clock import FakeClock
from tests.helpers.seeding import seed_tenant_and_actor

ISSUER = "https://idp.example.test"
OPERATOR = "submitter-1"
AUTHORING_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

#: Called with `(tenant_id, revision_id, proposal_id, proposal_version)`
#: once submit() has produced the latter three, before checkpoint export
#: and activation -- the one hook `seed_and_activate` offers a caller whose
#: candidate carries an `is_mandatory=True` applicability rule. ADR 041's
#: own reducer classifies any mandatory rule, at any scope, as requiring
#: observation qualification before activation (`contextplane.arc.service.
#: risk`'s own module docstring; `qualification.py`'s `_requires_
#: observation` is the identical rule, transcribed rather than imported for
#: the reason its own docstring gives) -- so a caller whose candidate is
#: mandatory has to hand `activate()` a real `qualification_id` or the
#: `observation_qualified` predicate refuses. `tenant_id` is passed rather
#: than left for the caller to close over: `seed_and_activate` mints it
#: itself from *slug*, so a caller building the provider before calling
#: this function has no real value to close over yet. Returns `None` for a
#: caller with nothing to supply, which is the default and every existing
#: caller's behaviour.
QualificationIdProvider = Callable[[uuid.UUID, uuid.UUID, uuid.UUID, int], Awaitable[uuid.UUID | None]]


def build_ctx(*, tenant_id: uuid.UUID, subject: str, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=roles or ["admin"], oidc_subject=subject)
    return ArcRequestContext(tenant=tenant, oidc_issuer=ISSUER)


def allow_all_authorization() -> ArcAuthorizationService:
    class _AllowAll:
        async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
            return list(capability_ids)

    return ArcAuthorizationService(visibility=_AllowAll())


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
            "vfrom": AUTHORING_NOW - datetime.timedelta(days=1),
            "now": AUTHORING_NOW,
            "issuer": ISSUER,
            "subject": principal_subject,
            "fp": credential_fingerprint,
        },
    )


def directive_row(*, directive_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """One real, servable `citation_only` directive for the candidate's own
    `directives[]` -- materialised into `arc_directives` by `submit` itself
    (see `ArtifactMaterialisationService._directive_row`), not seeded by a
    direct `INSERT` the way `tests/helpers/arc_fixtures.py::seed_arc` still
    does for the receipt-path tests that have nothing to do with the
    authoring surface's own writer.

    `citation_only` by default, so every conflict-key/verification field is
    `None` -- not a scaffold, but this directive's actual (empty) conflict
    key. A caller building an action-protecting directive instead overrides
    `directive_type` and the full conflict-key shape it now requires.
    """
    statement = "Cite the approved runbook."
    base: dict[str, object] = {
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
        "created_at": AUTHORING_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    base.update(overrides)
    return base


def candidate_profile(
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    directive_id: uuid.UUID,
    directives: list[dict[str, object]] | None = None,
    applicability: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """A real `arc_artifact_semantics_v1` candidate, carrying one or more
    real directives and applicability rules -- every one of them reaches
    `arc_directives`/`arc_applicability_rules` through `submit` itself
    rather than a seeded `INSERT`.

    `directives` defaults to a single `citation_only` directive named by
    `directive_id`. `applicability` defaults to a single task-scoped,
    selector-free, non-mandatory rule (matches any manifest, requires no
    observation qualification before activation); a caller that needs a
    mandatory rule instead -- and so a `qualification_id` at activation,
    see `seed_and_activate`'s own `qualification_id_provider` -- passes its
    own list.
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
        "directives": directives if directives is not None else [directive_row(directive_id=directive_id)],
        "applicability": (
            applicability
            if applicability is not None
            else [
                {
                    "rule_id": str(uuid.uuid4()),
                    "scope": "task",
                    "target_tenant_id": None,
                    "capability_ids": None,
                    "capability_labels": None,
                    "domain_ids": None,
                    "intent_kinds": None,
                    "action_classes": None,
                    "environments": None,
                    "data_sensitivity_tiers": None,
                    "effective_from": None,
                    "effective_until": None,
                    "is_mandatory": False,
                }
            ]
        ),
        "detail_audience": "agent_only",
        "review_expires_at": (AUTHORING_NOW + datetime.timedelta(days=365))
        .astimezone(datetime.UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content_classification": "internal",
        "approved_retention_floor_days": 730,
        "initial_freshness_basis": "revision_pinned_only",
        "reviewed_baseline_revision_id": None,
    }


@dataclasses.dataclass
class RecordingSink:
    accepted: dict[tuple[str, uuid.UUID, int], SinkReceipt] = dataclasses.field(default_factory=dict)

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> SinkReceipt:
        key = (deployment_id, revision_id, sequence)
        existing = self.accepted.get(key)
        if existing is not None:
            return existing
        receipt = SinkReceipt(
            receipt_digest=f"receipt-{head_digest}",
            receipt_signature=f"sig-{head_digest[:16]}",
            accepted_at=AUTHORING_NOW,
        )
        self.accepted[key] = receipt
        return receipt

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> SinkReceipt | None:
        return self.accepted.get((deployment_id, revision_id, sequence))

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        seqs = [seq for (d, r, seq) in self.accepted if d == deployment_id and r == revision_id]
        return max(seqs) if seqs else None


async def seed_and_activate(
    wired_app: FastAPI,
    pg_container: str,
    *,
    slug: str,
    directives: list[dict[str, object]] | None = None,
    applicability: list[dict[str, object]] | None = None,
    qualification_id_provider: QualificationIdProvider | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Submit, approve, export the checkpoint, and activate a real
    candidate carrying one real directive (or, if *directives* is given,
    exactly that list) and one real applicability rule (or, if
    *applicability* is given, exactly that list). Returns
    `(tenant_id, revision_id)`.

    *qualification_id_provider*, if given, is awaited once submit() has
    produced `revision_id`/`proposal_id`/`proposal_version` and before
    checkpoint export -- see this module's own `QualificationIdProvider`
    docstring for why a mandatory candidate needs one.
    """
    factory = wired_app.state.services.session_factory
    tenant_id, _actor_id = await seed_tenant_and_actor(pg_container, slug=slug)
    artifact_id = await seed_artifact_family(factory, tenant_id=tenant_id)
    source_evidence_id = await seed_source_evidence(factory, tenant_id=tenant_id)

    proposal_service = ProposalService(factory, authorization=allow_all_authorization(), clock=FakeClock(AUTHORING_NOW))
    version = await proposal_service.open_proposal(
        build_ctx(tenant_id=tenant_id, subject=OPERATOR),
        artifact_id=artifact_id,
        source_evidence_id=source_evidence_id,
        reviewed_baseline_revision_id=None,
    )
    revision_id = uuid.uuid4()
    directive_id = uuid.uuid4()
    candidate = candidate_profile(
        artifact_id=artifact_id,
        revision_id=revision_id,
        directive_id=directive_id,
        directives=directives,
        applicability=applicability,
    )
    async with factory() as session, session.begin():
        await proposal_queries.update_semantics(
            session, proposal_id=version.proposal_id, proposal_version=1, semantics=candidate
        )

    materialisation = ArtifactMaterialisationService(
        factory,
        authorization=allow_all_authorization(),
        clock=FakeClock(AUTHORING_NOW),
        operational_chain_appender=OperationalChainService(
            clock=FakeClock(AUTHORING_NOW), deployment_id="serving-test"
        ),
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
                    "intent_kind": None,
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
        "author_issuer": ISSUER,
        "author_subject": OPERATOR,
        "created_at": "2026-01-01T00:00:00Z",
    }
    result = await materialisation.submit(
        build_ctx(tenant_id=tenant_id, subject=OPERATOR), version.proposal_id, 1, expected_impact_envelope=envelope
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
    expected_directive_count = len(directives) if directives is not None else 1
    expected_rule_count = len(applicability) if applicability is not None else 1
    assert (
        directive_count == expected_directive_count
    ), "submit must materialise every one of the candidate's own directives -- no seeded INSERT here"
    assert (
        rule_count == expected_rule_count
    ), "submit must materialise every one of the candidate's own applicability rules -- no seeded INSERT here"

    qualification_id: uuid.UUID | None = None
    if qualification_id_provider is not None:
        qualification_id = await qualification_id_provider(tenant_id, revision_id, version.proposal_id, 1)

    private, public = _keypair()
    verifier_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        await _insert_verifier(
            session, approval_verifier_id=verifier_id, public_key=public, principal_subject="approver-1"
        )

    approval_service = wired_app.state.services.arc_approval_challenges
    requester_ctx = build_ctx(tenant_id=tenant_id, subject="requester-1")
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
    export = CheckpointExportService(factory, clock=FakeClock(AUTHORING_NOW), sink=RecordingSink())
    await export.export_checkpoint(checkpoint_id)

    activation_service = wired_app.state.services.arc_activation
    activator_ctx = build_ctx(tenant_id=tenant_id, subject="activator-1")
    activation = await activation_service.activate(
        activator_ctx,
        revision_id=revision_id,
        proposal_id=version.proposal_id,
        proposal_version=1,
        qualification_id=qualification_id,
    )
    assert activation.lifecycle_state == "active"
    return tenant_id, revision_id
