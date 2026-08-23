"""ARC-area service construction, plus the two startup refusals ARC owns.

One registration entry point per area, called by the composition root. This
area is the largest single registration in the app and the reason the root
stopped enumerating services: ARC brings its own key hierarchy, its own
authorization chokepoint, and a read path whose collaborators are consumed at
several sites each, none of which the composition root has any reason to know
about.

`build_arc_services` takes no `app` and touches no `app.state`. Six of the
fields it returns do still have a live reader outside the typed container;
the root attaches those, because `app.state` writes are gated to the
composition root's own package and each one has to name the reader that
justifies it.

Keys come from settings, and a deployment that configured none gets providers
holding none: the providers themselves fail closed on first use rather than
this module silently inventing key material. A development key generated here
would be indistinguishable, at runtime, from a real one.

The two startup assertions live here rather than in the root for the same
reason the services do — both are statements about ARC's own invariants (an
approval-evidence type with no first-party writer, and a model-backed drafter
enabled beyond what its committed decision earned), and a reader checking
either one should find it beside the wiring it guards.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas.canonical import CANONICAL_PROFILE_VERSIONS
from contextplane.arc.service.activation import ActivationService
from contextplane.arc.service.approval_challenge import ApprovalChallengeService
from contextplane.arc.service.approval_trust import ApprovalTrustService
from contextplane.arc.service.approved_exceptions import ExceptionService
from contextplane.arc.service.artifact import ArtifactService
from contextplane.arc.service.attestation import AttestationService, HostSignerKeyRegistry
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.autonomy_decision import AutonomyDecisionService
from contextplane.arc.service.autonomy_enforcement import AutonomyEnforcementService
from contextplane.arc.service.autonomy_envelope import AutonomyEnvelopeService
from contextplane.arc.service.challenge import ChallengeNonceDeriver, ChallengeService
from contextplane.arc.service.checkpoint_export import CheckpointExportService
from contextplane.arc.service.continuation import ContinuationTokenProvider
from contextplane.arc.service.corpus import CorpusReader
from contextplane.arc.service.detail_retrieval import JitService
from contextplane.arc.service.drafter import DrafterService
from contextplane.arc.service.enrollment import EnrollmentService
from contextplane.arc.service.governance_reads import GovernanceReadService
from contextplane.arc.service.integrity import RevisionIntegrityService
from contextplane.arc.service.operational_chain import OperationalChainService
from contextplane.arc.service.preflight import PreflightRegistry
from contextplane.arc.service.proposal import ProposalService
from contextplane.arc.service.provenance import ProvenanceService
from contextplane.arc.service.qualification import QualificationService
from contextplane.arc.service.receipt import ReceiptProvenance, ReceiptService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.service.replay import ResponseReplayProvider
from contextplane.arc.service.replay_corpus import ReplayCorpusService
from contextplane.arc.service.resolution import ResolutionService
from contextplane.arc.service.review_package import ReviewPackageService
from contextplane.arc.service.risk import RiskEnvelopeValidator
from contextplane.arc.service.selection import (
    SELECTION_ENGINE_VERSION,
    selection_config_digest,
)
from contextplane.arc.service.semantic_tests import SemanticTestService
from contextplane.arc.service.shadow import ShadowService
from contextplane.arc.service.signing import KeyRecord, ReceiptSigningProvider
from contextplane.arc.service.source_admission import SourceAdmissionService
from contextplane.arc.service.source_admission_graph import GraphPromotionAdmissionService
from contextplane.arc.service.source_status import SourceStatusService
from contextplane.arc.service.submission import ArtifactMaterialisationService
from contextplane.arc.service.verifier_registry import VerifierRegistry
from contextplane.arc.types import ArcRequestContext
from contextplane.config import Settings
from contextplane.service.governance.visibility import VisibilityService
from contextplane.types import Clock


@dataclass(frozen=True)
class ArcServices:
    """What this area contributes to the typed container, by container field name.

    Field order follows construction order in `build_arc_services`, not
    alphabetical order, so this reads as a map of the same graph.
    """

    arc_governance_reads: GovernanceReadService

    arc_signing: ReceiptSigningProvider
    arc_authorization: ArcAuthorizationService
    arc_receipts: ReceiptService
    arc_clock: Clock
    arc_corpus: CorpusReader
    arc_challenges: ChallengeService
    arc_attestation: AttestationService
    arc_jit: JitService
    arc_receipt_reader: ReceiptReader
    arc_preflight: PreflightRegistry
    arc_artifacts: ArtifactService
    arc_exceptions: ExceptionService
    arc_envelopes: AutonomyEnvelopeService
    arc_envelope_decisions: AutonomyDecisionService
    arc_envelope_enforcement: AutonomyEnforcementService
    arc_source_admission: SourceAdmissionService
    # The third admission authority. Composes the service above rather than
    # duplicating its transaction: one place writes an evidence row.
    arc_graph_source_admission: GraphPromotionAdmissionService
    # Constructed right after admission, sharing its clock: every later
    # checkpoint (submission, approval, activation, selection, protected-
    # action authorization) reads a source's local status through this one
    # instance rather than re-deriving freshness rules of its own.
    arc_source_status: SourceStatusService
    arc_proposals: ProposalService
    arc_provenance: ProvenanceService
    arc_semantic_tests: SemanticTestService
    arc_operational_chain: OperationalChainService
    arc_checkpoint_export: CheckpointExportService
    arc_risk_envelope: RiskEnvelopeValidator
    arc_materialisation: ArtifactMaterialisationService
    arc_drafter: DrafterService
    arc_verifier_registry: VerifierRegistry
    arc_approval_trust: ApprovalTrustService
    arc_enrollment: EnrollmentService
    # The `S -> R` half of the digest chain -- see that class's own module
    # docstring for what it recomputes and cross-checks rather than trusts.
    arc_review_package: ReviewPackageService
    # The two-call `artifact_activation` writer. Dormant no longer: the line
    # above is what makes constructing this one real rather than a `TypeError`.
    arc_approval_challenges: ApprovalChallengeService
    # Shadow overlay, deterministic replay-corpus generation/approval, and the
    # qualification decision + acceptance rules. `arc_qualification` composes
    # both of the others.
    arc_shadow: ShadowService
    arc_replay_corpus: ReplayCorpusService
    arc_qualification: QualificationService
    # The one read-path integrity chokepoint -- constructed here and called by
    # all four production sites this same object reaches. See that class's own
    # module docstring.
    arc_integrity: RevisionIntegrityService
    # The ten-predicate atomic activation gate -- see `activation.py`'s own
    # module docstring for predicate 10's real `arc_integrity.assess` call.
    arc_activation: ActivationService
    # None on every deployment today: ARC key material is not yet
    # operator-configurable, so resolution has nothing to sign a receipt with.
    # See `build_arc_services` for why an unconfigured deployment gets `None`
    # here rather than a service that would sign with no key.
    arc_resolution: ResolutionService | None


class _ArcVisibilityAdapter:
    """Bridges ARC's narrow capability-visibility need to `VisibilityService`.

    ARC asks "which of these capabilities may this actor see" and nothing
    else. Adapting rather than widening ARC's protocol keeps the dependency
    one-directional: ARC never learns the rest of that service's surface,
    and cannot start depending on it.
    """

    def __init__(self, visibility: VisibilityService) -> None:
        self._visibility = visibility

    async def visible_entity_ids(self, ctx: ArcRequestContext, entity_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return await self._visibility.filter_entities(ctx.tenant, list(entity_ids))


# `arc_operational_chain_checkpoints`' identity is `{deployment_id, revision_id,
# sequence}` so an external sink can disambiguate checkpoints from more than
# one deployment writing to it. There is no operator-configurable
# deployment-identity setting today -- adding one is a `contextplane.config`
# change this wiring module has no license to make on its own -- so every
# deployment names itself with this one literal until that setting exists.
# Fine for now: the sink this ships with is a bare abstraction with no
# production implementation (`CheckpointExportService`'s own module
# docstring), so nothing yet actually writes to a sink two deployments could
# collide on.
_ARC_OPERATIONAL_CHAIN_DEPLOYMENT_ID = "registry-default-deployment"


def build_arc_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    settings: Settings,
    *,
    visibility: VisibilityService,
) -> ArcServices:
    """Construct every ARC service and return them on one object.

    `clock` is the process's one clock, shared rather than re-created: a
    resolution assembles its corpus and then evaluates it, and those two steps
    have to agree on what "now" is or a revision can become effective between
    them.
    """
    # ARC key material is not operator-configurable yet, so every hierarchy
    # starts empty. Named rather than inlined because whether resolution can
    # run at all is decided by whether there is an active key: a provider
    # with none refuses to seal rather than emitting an unsealed envelope.
    # Two shapes: the receipt signer holds full key records because it must
    # know whether a key is retired or compromised before signing with it,
    # while the three AEAD providers hold raw secrets. One active key id
    # across all of them, because they are one hierarchy.
    arc_signing_keys: dict[str, KeyRecord] = {}
    arc_secrets: dict[str, bytes] = {}
    arc_active_key_id: str | None = None

    signing = ReceiptSigningProvider(arc_signing_keys, active_key_id=arc_active_key_id)
    nonce_deriver = ChallengeNonceDeriver(arc_secrets, active_key_id=arc_active_key_id)
    tokens = ContinuationTokenProvider(arc_secrets, active_key_id=arc_active_key_id)
    replay = ResponseReplayProvider(arc_secrets, active_key_id=arc_active_key_id)

    # The allowlist comes from configuration, not from a default here. An
    # empty one permits no global writes at all, which is the correct
    # behaviour for a deployment that configured none: the one surface that
    # binds every tenant must not fall open.
    authorization = ArcAuthorizationService(
        visibility=_ArcVisibilityAdapter(visibility),
        global_write_allowlist=settings.arc_global_operator_allowlist,
    )
    receipts = ReceiptService(signing, clock)

    arc_challenges = ChallengeService(session_factory, nonce_deriver, clock)
    arc_attestation = AttestationService(HostSignerKeyRegistry(), clock=clock)
    arc_jit = JitService(session_factory, receipts=receipts, tokens=tokens, clock=clock)
    arc_receipt_reader = ReceiptReader(session_factory, authorization=authorization)
    # One registry for the process. It holds state about connections this
    # process is serving, so it cannot meaningfully outlive it -- a restart
    # drops every connection, and any record that survived would be a
    # preflight for a caller nobody is on the other end of.
    arc_preflight = PreflightRegistry()
    arc_artifacts = ArtifactService(session_factory, authorization=authorization, clock=clock)
    arc_exceptions = ExceptionService(session_factory, authorization=authorization, clock=clock)
    # The autonomy-envelope trio, wired unconditionally for the same reason as
    # the two above: no key material, only the session factory, the shared
    # authorization chokepoint and the clock. They compose in one direction --
    # enforcement wraps the decision, which reads the binding -- so the
    # construction order is the dependency order.
    arc_envelopes = AutonomyEnvelopeService(session_factory, authorization=authorization, clock=clock)
    arc_envelope_decisions = AutonomyDecisionService(
        session_factory, envelopes=arc_envelopes, authorization=authorization, clock=clock
    )
    arc_envelope_enforcement = AutonomyEnforcementService(session_factory, decisions=arc_envelope_decisions)
    # Wired unconditionally, like the two services above: source admission
    # needs no key material, only the session factory, the shared
    # authorization chokepoint, and the clock.
    arc_source_admission = SourceAdmissionService(session_factory, authorization=authorization, clock=clock)
    arc_graph_source_admission = GraphPromotionAdmissionService(
        session_factory, admission=arc_source_admission, authorization=authorization, clock=clock
    )
    # The real operational-chain appender: signs and appends for real (see
    # its own module docstring for why it needs no operator-configured key
    # to do that), injected into every collaborator below that needs one.
    # One instance for the whole request-serving graph -- `wiring/jobs.py`
    # builds its own second instance for the background-worker graph, and
    # both share that module's process-wide signing key, not two different
    # ones (see `operational_chain.py`'s `_process_signing_key`).
    arc_operational_chain = OperationalChainService(clock=clock, deployment_id=_ARC_OPERATIONAL_CHAIN_DEPLOYMENT_ID)
    # No sink is configured on any deployment today -- see
    # `CheckpointExportService`'s own module docstring for why that is the
    # honest state rather than a stub. Checkpoints this instance's appends
    # create stay safely pending until a real one is wired.
    arc_checkpoint_export = CheckpointExportService(session_factory, clock=clock)
    # Now genuinely live: `record_revocation`/`record_expiry` no longer
    # refuse, because the collaborator their four-part write needs exists.
    # `check_status`'s freshness read never needed one and is unaffected.
    arc_source_status = SourceStatusService(
        session_factory, clock=clock, operational_chain_appender=arc_operational_chain
    )
    # Wired unconditionally, same shape as arc_source_admission above.
    arc_proposals = ProposalService(session_factory, authorization=authorization, clock=clock)
    # Same unconditional wiring as arc_proposals above: no key material
    # needed, and the two services share the same session factory,
    # authorization chokepoint, and clock as the proposal aggregate they
    # both extend.
    arc_provenance = ProvenanceService(session_factory, authorization=authorization, clock=clock)
    arc_semantic_tests = SemanticTestService(session_factory, authorization=authorization, clock=clock)
    # The final submission prerequisite: composes risk classification and
    # expected-impact-envelope validation into the one collaborator
    # `submit` was still missing. `operational_chain_appender` has been
    # real since this module's own earlier construction of
    # `arc_operational_chain` -- injecting this second collaborator is what
    # turns `submit` on for the first time on any deployment, per
    # `ArtifactMaterialisationService`'s own guard (both collaborators
    # present, or neither).
    arc_risk_envelope = RiskEnvelopeValidator()
    arc_materialisation = ArtifactMaterialisationService(
        session_factory,
        authorization=authorization,
        clock=clock,
        operational_chain_appender=arc_operational_chain,
        risk_envelope_validator=arc_risk_envelope,
    )
    # `decision_loader=load_drafter_model_decision` is the same function
    # `assert_drafter_decision_permits_serving` (called on the startup path)
    # reads the committed decision artifact through -- one loader, one
    # validated shape, read fresh on every `draft()` call rather than cached
    # at wiring time, matching the startup guard's own "never more permissive
    # than the artifact" rule.
    arc_drafter = DrafterService(
        session_factory,
        authorization=authorization,
        source_admission=arc_source_admission,
        source_status=arc_source_status,
        clock=clock,
        settings=settings,
        decision_loader=load_drafter_model_decision,
    )
    # The trust root for approvals, deployment-wide and cross-tenant. Wired
    # unconditionally: registering a verifier is how a deployment acquires
    # one, so gating it on already having one would be circular.
    arc_verifier_registry = VerifierRegistry(session_factory, clock=clock)
    arc_approval_trust = ApprovalTrustService(session_factory, authorization=authorization, clock=clock)
    # Principal-bound enrollment. Wired unconditionally, same shape as
    # `arc_verifier_registry` above. `attestation_providers` is left at its
    # empty default -- no deployment configures one today; see
    # `EnrollmentService`'s own module docstring for why `provider_delegated`
    # completion refuses cleanly rather than needing one wired here.
    arc_enrollment = EnrollmentService(session_factory, authorization=authorization, clock=clock)

    # The review package (`S -> R`) and the two-call approval challenge writer
    # (`R -> A`) that depends on it. `ApprovalChallengeService` took a
    # *required* `review_package_service` constructor argument from the day it
    # was written specifically so it could not be constructed on any
    # deployment until this line existed -- see that class's own module
    # docstring.
    arc_review_package = ReviewPackageService(session_factory, authorization=authorization)
    arc_approval_challenges = ApprovalChallengeService(
        session_factory,
        authorization=authorization,
        clock=clock,
        review_package_service=arc_review_package,
    )

    # The one read-path integrity chokepoint. Every collaborator below
    # already exists on this graph for its own reason (`arc_review_package`
    # for S/R plus the cached-state cross-check, `arc_source_status` for the
    # freshness read every other checkpoint already trusts,
    # `arc_operational_chain` for chain re-verification) -- this is the first
    # thing that depends on all three at once. Built ahead of `arc_corpus` so
    # every one of the four callers below can take it as a real
    # constructor/call-time dependency rather than a forward reference.
    arc_integrity = RevisionIntegrityService(
        review_package_service=arc_review_package,
        source_status_service=arc_source_status,
        operational_chain_service=arc_operational_chain,
        clock=clock,
    )
    # Constructed here, not at this module's first ARC assignment, because
    # it needs `arc_integrity` above to filter an integrity-failed candidate
    # out of the corpus before `select()` ever sees it (see that class's own
    # module docstring).
    arc_corpus = CorpusReader(session_factory, integrity=arc_integrity)

    # Shadow overlay, replay-corpus generation/approval, and qualification.
    # `arc_shadow` composes the unmodified `arc_corpus` reader (see
    # `shadow.py`'s own module docstring for why the overlay must be built on
    # top of that exact instance rather than a parallel read path);
    # `arc_qualification` composes `arc_review_package` (for the candidate
    # review-package digest), `arc_shadow`, and `arc_replay_corpus`.
    arc_shadow = ShadowService(arc_corpus, session_factory)
    arc_replay_corpus = ReplayCorpusService(session_factory, authorization=authorization, clock=clock)
    arc_qualification = QualificationService(
        session_factory,
        authorization=authorization,
        clock=clock,
        review_package=arc_review_package,
        shadow=arc_shadow,
        replay_corpus=arc_replay_corpus,
    )

    # The ten-predicate atomic activation gate. `arc_artifacts` is the
    # collaborator this class delegates `revoke()` to -- see `activation.py`'s
    # own `__init__` docstring for why revocation is shared rather than
    # reimplemented; every other collaborator here is the same instance the
    # nine real predicates already depend on elsewhere in this graph.
    # `arc_integrity` is injected for real: predicate 10 calls
    # `arc_integrity.assess` directly now, the same instance every other
    # caller on this graph shares.
    arc_activation = ActivationService(
        session_factory,
        authorization=authorization,
        clock=clock,
        review_package=arc_review_package,
        source_status=arc_source_status,
        artifacts=arc_artifacts,
        integrity=arc_integrity,
    )

    # Resolution is wired only when there is key material behind it. Every
    # resolution signs a receipt and seals the retained response, so without
    # a key it could not produce a receipt it could later stand behind --
    # and the providers refuse rather than emit an unsigned or unsealed one.
    # Left unset, the route answers "not configured on this deployment",
    # which is the truth; wiring it anyway would turn that into a 500 on
    # every call.
    arc_resolution: ResolutionService | None = None
    if arc_active_key_id is not None:
        arc_resolution = ResolutionService(
            session_factory,
            attestation=arc_attestation,
            challenges=arc_challenges,
            receipts=receipts,
            provenance=ReceiptProvenance(
                selection_engine_version=SELECTION_ENGINE_VERSION,
                build_revision=settings.build_revision,
                canonical_profile_versions=dict(CANONICAL_PROFILE_VERSIONS),
                selection_config_digest=selection_config_digest(),
            ),
            clock=clock,
            integrity=arc_integrity,
            seal=replay.seal,
        )

    return ArcServices(
        arc_governance_reads=GovernanceReadService(session_factory, clock=clock),
        arc_signing=signing,
        arc_authorization=authorization,
        arc_receipts=receipts,
        arc_clock=clock,
        arc_corpus=arc_corpus,
        arc_challenges=arc_challenges,
        arc_attestation=arc_attestation,
        arc_jit=arc_jit,
        arc_receipt_reader=arc_receipt_reader,
        arc_preflight=arc_preflight,
        arc_artifacts=arc_artifacts,
        arc_exceptions=arc_exceptions,
        arc_envelopes=arc_envelopes,
        arc_envelope_decisions=arc_envelope_decisions,
        arc_envelope_enforcement=arc_envelope_enforcement,
        arc_source_admission=arc_source_admission,
        arc_graph_source_admission=arc_graph_source_admission,
        arc_source_status=arc_source_status,
        arc_proposals=arc_proposals,
        arc_provenance=arc_provenance,
        arc_semantic_tests=arc_semantic_tests,
        arc_operational_chain=arc_operational_chain,
        arc_checkpoint_export=arc_checkpoint_export,
        arc_risk_envelope=arc_risk_envelope,
        arc_materialisation=arc_materialisation,
        arc_drafter=arc_drafter,
        arc_verifier_registry=arc_verifier_registry,
        arc_approval_trust=arc_approval_trust,
        arc_enrollment=arc_enrollment,
        arc_review_package=arc_review_package,
        arc_approval_challenges=arc_approval_challenges,
        arc_shadow=arc_shadow,
        arc_replay_corpus=arc_replay_corpus,
        arc_qualification=arc_qualification,
        arc_integrity=arc_integrity,
        arc_activation=arc_activation,
        arc_resolution=arc_resolution,
    )


async def assert_no_legacy_activation_evidence(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Refuse to start if `artifact_activation` evidence predates a first-party writer.

    No production code in this deployment inserts `arc_approval_evidence`
    rows of this type -- `ExceptionService` is the only writer, and it is
    hardcoded to `exception_approval`. A row of this type can therefore only
    have reached the table through something other than a writer this
    system trusts (a direct SQL insert, an old deployment's since-removed
    code path, a bootstrap script), and treating it as a real approval on
    this deployment's first boot after that writer's absence became load-
    bearing would be exactly the silent grandfathering the underlying design
    review rejected. Caught here, an operator sees one refusal at startup
    naming the count. Left uncaught, the deployment starts, serves requests,
    and every receipt asserting one of these revisions was approved is
    wrong from the first request onward.
    """
    async with session_factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation'")
            )
        ).scalar_one()

    if not count:
        return

    raise RuntimeError(
        f"found {count} arc_approval_evidence row(s) with evidence_type = 'artifact_activation'. No "
        "production writer of this evidence type exists in this deployment, so every such row predates "
        "one and cannot be trusted to have been produced by a real approval. This deployment refuses to "
        "start with them present. Revoke the dependent active revision(s), or run an explicit, reviewed "
        "bootstrap migration that re-creates equivalent evidence through a first-party writer and records "
        "the bootstrap in the audit log, before starting this deployment again."
    )


# The committed drafter model decision artifact. A fixed repo-relative path,
# not a Settings field -- unlike the model artifact itself (which is
# deployment-local and configured), this file is the reviewed decision *about*
# that deployment, and ships in the same commit as the code that reads it.
_DRAFTER_DECISION_PATH = Path(__file__).resolve().parent / "drafter" / "model_decision.json"

_DRAFTER_DECISION_OUTCOMES = frozenset({"accepted", "human_only"})
_DRAFTER_DECISION_REQUIRED_KEYS = frozenset(
    {
        "decision_version",
        "model_artifact_digest",
        "tokenizer_digest",
        "prompt_profile_version",
        "resource_envelope",
        "license_terms_reference",
        "evaluation_manifest_version",
        "gate_results",
        "outcome",
    }
)


def load_drafter_model_decision(path: Path = _DRAFTER_DECISION_PATH) -> dict[str, Any]:
    """Load and structurally validate the committed drafter model decision.

    The one parser both the startup guard below and the conformance test
    import -- so the two can never validate different shapes of the same
    file. Raises `ValueError` (not `RuntimeError`; nothing here is a startup
    refusal by itself) on a missing file, invalid JSON, a non-closed key
    set, an unrecognized `outcome`, or a `gate_results` entry missing a
    boolean `passed`.
    """
    if not path.is_file():
        raise ValueError(f"drafter model decision artifact not found at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"drafter model decision artifact at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"drafter model decision artifact at {path} must be a JSON object")

    actual_keys = set(raw)
    if actual_keys != _DRAFTER_DECISION_REQUIRED_KEYS:
        missing = sorted(_DRAFTER_DECISION_REQUIRED_KEYS - actual_keys)
        extra = sorted(actual_keys - _DRAFTER_DECISION_REQUIRED_KEYS)
        raise ValueError(
            f"drafter model decision artifact at {path} is not the closed shape: missing {missing}, unexpected {extra}"
        )
    if raw["outcome"] not in _DRAFTER_DECISION_OUTCOMES:
        raise ValueError(
            f"drafter model decision artifact outcome {raw['outcome']!r} is not one of "
            f"{sorted(_DRAFTER_DECISION_OUTCOMES)}"
        )
    gate_results = raw["gate_results"]
    if not isinstance(gate_results, list) or not gate_results:
        raise ValueError(f"drafter model decision artifact at {path}: gate_results must be a non-empty array")
    for entry in gate_results:
        if not isinstance(entry, dict) or not isinstance(entry.get("passed"), bool):
            raise ValueError(
                f"drafter model decision artifact at {path}: every gate_results entry needs a boolean 'passed'"
            )
    return raw


def assert_drafter_decision_permits_serving(settings: Settings) -> None:
    """Refuse to start if the model-backed drafter is enabled beyond what the
    committed decision artifact actually earned.

    `ARC_DRAFTER_MODEL_ENABLED` is a runtime flag; the decision behind it is
    not. Flipping the flag on cannot make a `human_only` verdict, a failed
    evaluation gate, or a swapped model artifact serve just by setting an
    environment variable -- that is what this function is for. When the flag
    is false (the default, including when it is absent from the environment
    entirely), this function returns immediately without reading the
    decision artifact or the configured model-artifact path at all: a
    disabled deployment never touches either, by construction rather than by
    convention.
    """
    if not settings.arc_drafter_model_enabled:
        return

    decision = load_drafter_model_decision()

    if decision["outcome"] != "accepted":
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but {_DRAFTER_DECISION_PATH} records "
            f"outcome={decision['outcome']!r}, not 'accepted'. The model-backed drafter cannot serve on a "
            "verdict nobody made. Set ARC_DRAFTER_MODEL_ENABLED=false, or land a new decision that records "
            "'accepted' with every evaluation gate passed."
        )

    failed_gates = sorted(g.get("gate_id", "<unnamed>") for g in decision["gate_results"] if not g["passed"])
    if failed_gates:
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but {_DRAFTER_DECISION_PATH} records outcome='accepted' with "
            f"failing evaluation gate(s): {failed_gates}. An accepted outcome requires every gate to have "
            "passed; refusing to start rather than serve a model that did not actually clear its own gates."
        )

    artifact_path = settings.arc_drafter_model_artifact_path
    if not artifact_path or not Path(artifact_path).is_file():
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but ARC_DRAFTER_MODEL_ARTIFACT_PATH ({artifact_path!r}) does not "
            "name a file that exists. The decision artifact's recorded model_artifact_digest cannot be "
            "verified against a missing model artifact."
        )

    actual_digest = hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    if actual_digest != decision["model_artifact_digest"]:
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but the file at {artifact_path} hashes to {actual_digest}, not "
            f"the decision artifact's recorded model_artifact_digest={decision['model_artifact_digest']!r}. "
            "The flag can never be more permissive than the artifact the decision actually evaluated; "
            "refusing to start rather than serve an unverified model."
        )
