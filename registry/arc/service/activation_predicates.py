"""The ten named predicate checks `ActivationService` evaluates -- split
into its own module purely for the repo's 800-line file ceiling
(`activation.py`'s own orchestration, locking, and write phase already fill
that budget on their own). Every function here is a pure or session-scoped
check with no lock-acquisition, transaction, or write of its own: `activation.
py`'s `_evaluate` loads and locks every row once, then calls these in the
fixed `PREDICATE_ORDER` and collects the results -- exactly the same "call a
helper, then guard" shape `registry.arc.service.integrity.RevisionIntegrityService.
assess` uses for its own five axes, for the identical reason (see that
module's docstring): a mutation test that neutralises one bracketed guard
must make an otherwise-passing case wrongly report `satisfied=True`, and
proving that requires each check's pass/fail decision to live inside one
clearly bracketed span rather than scattered across a branchy caller.

**Every predicate defaults `satisfied=True` before its own bracket runs.**
`check_operational_integrity` depends on this exactly like every other
predicate here: the bracket is the only place `satisfied`/`reason_code` are
ever set, so removing it leaves the predicate looking satisfied regardless
of what `RevisionIntegrityService.assess` actually decided -- the
premature-enablement bug a bracketed guard, not a branchy caller, is what
makes provably impossible. Every other predicate uses the same shape for
uniformity, even though most of them could invert the default without
changing meaning.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.schemas.authoring_profile_shapes import ACTOR_SEPARATION_PROFILE
from registry.arc.schemas.authoring_profiles import ActorSeparationViolationError, validate_actor_separation_v1
from registry.arc.service.approval_challenge import ReviewPackageDigests
from registry.arc.service.approval_challenge_verification import (
    ApprovalVerificationFailed,
    DetachedSignatureProofInput,
    SignatureVerifier,
    VerifierAttestationProvider,
    VerifierMaterial,
    build_canonical_evidence,
    verify_proof,
)
from registry.arc.service.queries import approval as approval_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries import qualification as qual_queries
from registry.arc.service.queries import review_package as rp_queries
from registry.arc.service.risk import RiskClassificationError, RiskClassificationService, UnknownRiskAlgorithmVersion

# ---------------------------------------------------------------------------
# The ten predicate names, literal and in the fixed §2.2 evaluation/report
# order -- these are the exact `predicates[].name` wire values, and the
# literal test-id stems the integration suite names each of them by.
# ---------------------------------------------------------------------------

PREDICATE_LATEST_VERSION = "latest_version"
PREDICATE_STATE_APPROVED = "state_approved"
PREDICATE_DIGEST_CHAIN = "digest_chain"
PREDICATE_BASELINE_CURRENT = "baseline_current"
PREDICATE_SOURCE_VALID = "source_valid"
PREDICATE_RISK_REPRODUCIBLE = "risk_reproducible"
PREDICATE_OBSERVATION_QUALIFIED = "observation_qualified"
PREDICATE_PROJECTION_EVIDENCE_VALID = "projection_evidence_valid"
PREDICATE_ACTOR_SEPARATION = "actor_separation"
PREDICATE_OPERATIONAL_INTEGRITY = "operational_integrity"

PREDICATE_ORDER: tuple[str, ...] = (
    PREDICATE_LATEST_VERSION,
    PREDICATE_STATE_APPROVED,
    PREDICATE_DIGEST_CHAIN,
    PREDICATE_BASELINE_CURRENT,
    PREDICATE_SOURCE_VALID,
    PREDICATE_RISK_REPRODUCIBLE,
    PREDICATE_OBSERVATION_QUALIFIED,
    PREDICATE_PROJECTION_EVIDENCE_VALID,
    PREDICATE_ACTOR_SEPARATION,
    PREDICATE_OPERATIONAL_INTEGRITY,
)

#: The generic per-predicate refusal reason -- used whenever no single
#: existing bounded code names the failure more precisely than "this named
#: predicate did not hold" (Appendix A.5's own stated purpose for this code:
#: "names the predicate, no evidence"). Two predicates below use a more
#: specific pre-existing code because one already names their exact failure
#: mode elsewhere in this codebase: `source_valid` uses the same code
#: `SourceStatusService`/`RevisionIntegrityService` already raise for an
#: untrustworthy source, and `digest_chain`/`projection_evidence_valid` use
#: the same code the D2 verification path already raises for any
#: binding/signature/digest disagreement. Every other predicate uses the
#: generic code; inventing a distinct one per remaining predicate here would
#: be guessing a taxonomy no source names.
REASON_ACTIVATION_PREDICATE_FAILED = "arc_activation_predicate_failed"
REASON_SOURCE_STATUS_UNAVAILABLE = "arc_source_status_unavailable"
REASON_APPROVAL_VERIFICATION_FAILED = "arc_approval_verification_failed"
REASON_OPERATIONAL_INTEGRITY_PENDING = "arc_operational_integrity_pending"


@dataclasses.dataclass(frozen=True)
class PredicateResult:
    """One row of `ActivationEligibilityResponse.predicates`."""

    name: str
    satisfied: bool
    reason_code: str | None = None


class SourceStatusChecker(Protocol):
    """Narrowed to `SourceStatusService.check_status`, matching `registry.
    arc.service.integrity`'s own identically-named protocol -- both modules
    need only this one capability from source-status storage, and neither
    needs the session-opening service itself to write a unit test."""

    async def check_status(self, source_evidence_id: uuid.UUID) -> object: ...


def requires_observation(classification: str) -> bool:
    """ADR 041 Sec.1's own rule, transcribed identically to `qualification.
    py`'s private `_requires_observation` rather than imported: that
    function is qualification.py's own internal helper (this task's scope
    does not extend to exporting it), and the rule itself is a fixed,
    three-line closed-vocabulary fact, not logic a second copy could drift
    out of sync with -- both copies test the same literal ADR 041 vocabulary
    `RISK_CLASSIFICATIONS` already closes.
    """
    if classification.startswith("global"):
        return True
    return classification.endswith("_mandatory") and not classification.endswith("_non_mandatory")


# -- predicate 1: latest_version ---------------------------------------------


def check_latest_version(
    version: proposal_queries.VersionRow, latest: proposal_queries.VersionRow | None
) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: latest_version
    if latest is None or latest.proposal_version != version.proposal_version:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    # end-mutation-axis: latest_version
    return PredicateResult(name=PREDICATE_LATEST_VERSION, satisfied=satisfied, reason_code=reason_code)


# -- predicate 2: state_approved ----------------------------------------------


def check_state_approved(version: proposal_queries.VersionRow) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    pass  # mutated: state_approved axis removed
    return PredicateResult(name=PREDICATE_STATE_APPROVED, satisfied=satisfied, reason_code=reason_code)


# -- predicate 3: digest_chain -------------------------------------------------


def check_digest_chain(
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    digests: ReviewPackageDigests | None,
    live_evidence: approval_queries.LiveEvidenceRow | None,
) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: digest_chain
    if digests is None or live_evidence is None or live_evidence.revoked_at is not None:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    else:
        recomputed_a = hashlib.sha256(
            build_canonical_evidence(
                artifact_id=artifact_id,
                revision_id=revision_id,
                artifact_semantics_digest=digests.artifact_semantics_digest,
                review_package_digest=digests.review_package_digest,
            )
        ).hexdigest()
        if recomputed_a != live_evidence.approved_payload_digest:
            satisfied = False
            reason_code = REASON_APPROVAL_VERIFICATION_FAILED
    # end-mutation-axis: digest_chain
    return PredicateResult(name=PREDICATE_DIGEST_CHAIN, satisfied=satisfied, reason_code=reason_code)


# -- predicate 4: baseline_current --------------------------------------------


def check_baseline_current(version: proposal_queries.VersionRow, family: proposal_queries.FamilyRow) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: baseline_current
    if family.active_revision_id != version.reviewed_baseline_revision_id:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    # end-mutation-axis: baseline_current
    return PredicateResult(name=PREDICATE_BASELINE_CURRENT, satisfied=satisfied, reason_code=reason_code)


# -- predicate 5: source_valid -------------------------------------------------


async def check_source_valid(
    source_status: SourceStatusChecker, version: proposal_queries.VersionRow
) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: source_valid
    try:
        await source_status.check_status(version.source_evidence_id)
    except Exception:  # noqa: BLE001 - collapsed to one bounded code, matching SourceStatusUnavailable's own convention
        satisfied = False
        reason_code = REASON_SOURCE_STATUS_UNAVAILABLE
    # end-mutation-axis: source_valid
    return PredicateResult(name=PREDICATE_SOURCE_VALID, satisfied=satisfied, reason_code=reason_code)


# -- predicate 6: risk_reproducible --------------------------------------------


def check_risk_reproducible(
    version: proposal_queries.VersionRow, risk_row: rp_queries.RiskClassificationRow | None
) -> tuple[PredicateResult, bool]:
    """Returns `(result, stale_reducer_retired)`. The second element is the
    one failure mode with a write side effect (ADR 041: a retired reducer
    atomically terminalizes the version as `stale`) -- `activation.py`'s own
    `activate()` reads it to decide whether to perform that write; it is not
    part of the public wire response.
    """
    satisfied = True
    reason_code: str | None = None
    stale_reducer_retired = False
    # mutation-axis: risk_reproducible
    if risk_row is None or version.semantics is None:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    else:
        try:
            fresh = RiskClassificationService().classify(
                dict(version.semantics), reducer_version=risk_row.algorithm_version
            )
        except UnknownRiskAlgorithmVersion:
            satisfied = False
            reason_code = REASON_ACTIVATION_PREDICATE_FAILED
            stale_reducer_retired = True
        except RiskClassificationError:
            satisfied = False
            reason_code = REASON_ACTIVATION_PREDICATE_FAILED
        else:
            if fresh.classification != risk_row.classification:
                satisfied = False
                reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    # end-mutation-axis: risk_reproducible
    return (
        PredicateResult(name=PREDICATE_RISK_REPRODUCIBLE, satisfied=satisfied, reason_code=reason_code),
        stale_reducer_retired,
    )


# -- predicate 7: observation_qualified ----------------------------------------


def check_observation_qualified(
    *,
    risk_row: rp_queries.RiskClassificationRow | None,
    qualification: qual_queries.QualificationRow | None,
    qualification_id: uuid.UUID | None,
    revision_id: uuid.UUID,
    now: datetime.datetime,
) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: observation_qualified
    required = risk_row is not None and requires_observation(risk_row.classification)
    if required and qualification_id is None:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    elif not required and qualification_id is not None:
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    elif required:
        if (
            qualification is None
            or qualification.candidate_revision_id != revision_id
            or qualification.computed_decision not in ("qualified", "qualified_low_traffic")
            or qualification.accepted_at is None
            or qualification.expires_at is None
            or now >= qualification.expires_at
        ):
            satisfied = False
            reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    # end-mutation-axis: observation_qualified
    return PredicateResult(name=PREDICATE_OBSERVATION_QUALIFIED, satisfied=satisfied, reason_code=reason_code)


# -- predicate 8: projection_evidence_valid ------------------------------------


def _assert_projection_evidence_valid(
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    digests: ReviewPackageDigests | None,
    live_evidence: approval_queries.LiveEvidenceRow | None,
    verifier: approval_queries.VerifierRow | None,
    now: datetime.datetime,
    attestation_providers: dict[str, VerifierAttestationProvider],
    signature_verifier: SignatureVerifier,
) -> None:
    """The evidence/verifier/signature half of D2 revalidation --
    deliberately independent of `digest_chain`'s own equality check above,
    even though both need `A`: this predicate answers "is the evidence
    itself still trustworthy" (verifier live, fingerprint unmoved, signature
    still verifies against the *current* key); `digest_chain` answers "does
    recomputing the chain still land on what was signed." A verifier revoked
    a minute after signing fails this predicate while `digest_chain` still
    holds, and a tampered digest fails that one while this one still holds
    -- collapsing them into one check would hide which is true.
    """
    if live_evidence is None or live_evidence.revoked_at is not None:
        raise ApprovalVerificationFailed("no live projection approval evidence for this revision")
    if verifier is None:
        raise ApprovalVerificationFailed("the verifier this evidence names no longer exists")
    material = VerifierMaterial(
        approval_verifier_id=verifier.approval_verifier_id,
        allowed_evidence_types=frozenset(verifier.allowed_evidence_types),
        valid_from=verifier.valid_from,
        valid_to=verifier.valid_to,
        revoked_at=verifier.revoked_at,
        principal_binding_kind=verifier.principal_binding_kind,
        principal_issuer=verifier.principal_issuer,
        principal_subject=verifier.principal_subject,
        provider_id=verifier.provider_id,
        algorithm=verifier.algorithm,
        public_key=verifier.public_key,
        credential_fingerprint=verifier.credential_fingerprint,
    )
    if not material.usable_at(now):
        raise ApprovalVerificationFailed("the verifier is no longer usable")
    if (
        verifier.credential_fingerprint is None
        or verifier.credential_fingerprint != live_evidence.credential_fingerprint_at_approval
    ):
        raise ApprovalVerificationFailed(
            "the verifier's current credential fingerprint disagrees with the one snapshotted at approval"
        )
    if digests is None:
        raise ApprovalVerificationFailed("no recomputable review package to verify the evidence against")
    canonical_evidence_bytes = build_canonical_evidence(
        artifact_id=artifact_id,
        revision_id=revision_id,
        artifact_semantics_digest=digests.artifact_semantics_digest,
        review_package_digest=digests.review_package_digest,
    )
    if live_evidence.verification_method == "detached_signature" and live_evidence.signature_algorithm is not None:
        try:
            proof = DetachedSignatureProofInput(
                signature_algorithm=live_evidence.signature_algorithm,
                signature_base64=base64.b64encode(live_evidence.proof_bytes).decode("ascii"),
            )
        except (binascii.Error, ValueError) as exc:
            raise ApprovalVerificationFailed("stored proof bytes could not be re-encoded") from exc
        verify_proof(
            verifier=material,
            proof=proof,
            canonical_evidence_bytes=canonical_evidence_bytes,
            as_of=now,
            attestation_providers=attestation_providers,
            signature_verifier=signature_verifier,
        )


def check_projection_evidence_valid(
    *,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    digests: ReviewPackageDigests | None,
    live_evidence: approval_queries.LiveEvidenceRow | None,
    verifier: approval_queries.VerifierRow | None,
    now: datetime.datetime,
    attestation_providers: dict[str, VerifierAttestationProvider],
    signature_verifier: SignatureVerifier,
) -> PredicateResult:
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: projection_evidence_valid
    try:
        _assert_projection_evidence_valid(
            artifact_id=artifact_id,
            revision_id=revision_id,
            digests=digests,
            live_evidence=live_evidence,
            verifier=verifier,
            now=now,
            attestation_providers=attestation_providers,
            signature_verifier=signature_verifier,
        )
    except ApprovalVerificationFailed:
        satisfied = False
        reason_code = REASON_APPROVAL_VERIFICATION_FAILED
    # end-mutation-axis: projection_evidence_valid
    return PredicateResult(name=PREDICATE_PROJECTION_EVIDENCE_VALID, satisfied=satisfied, reason_code=reason_code)


# -- predicate 9: actor_separation ---------------------------------------------


async def check_actor_separation(
    session: AsyncSession,
    *,
    version: proposal_queries.VersionRow,
    risk_row: rp_queries.RiskClassificationRow | None,
    qualification: qual_queries.QualificationRow | None,
    revision_id: uuid.UUID,
    activator_issuer: str | None,
    activator_subject: str | None,
) -> PredicateResult:
    """Builds and validates the exact `arc_actor_separation_v1` object
    (`validate_actor_separation_v1`, reused rather than reimplemented --
    a second canonicalization engine for the same profile is exactly how
    two enforcement points drift apart on what "distinct principals" means):
    submitter is the proposal version's own opener, approver is
    the live D2 approving principal, accepter is the qualification's
    accepter (if any), activator is the identity attempting -- or, for the
    bare `check_eligibility` core with no known caller -- hypothetically
    attempting -- to activate right now.
    """
    satisfied = True
    reason_code: str | None = None
    # mutation-axis: actor_separation
    approver = await qual_queries.load_approving_principal(session, revision_id)
    risk_classification = risk_row.classification if risk_row is not None else "task_non_mandatory"
    obj: dict[str, object] = {
        "profile": ACTOR_SEPARATION_PROFILE,
        "risk_classification": risk_classification,
        "submitter_issuer": version.opened_by_issuer,
        "submitter_subject": version.opened_by_subject,
        "approver_issuer": approver[0] if approver is not None else version.opened_by_issuer,
        "approver_subject": approver[1] if approver is not None else version.opened_by_subject,
        "accepter_issuer": qualification.accepted_by_issuer if qualification is not None else None,
        "accepter_subject": qualification.accepted_by_subject if qualification is not None else None,
        "activator_issuer": activator_issuer if activator_issuer is not None else version.opened_by_issuer,
        "activator_subject": activator_subject if activator_subject is not None else version.opened_by_subject,
        "required_distinct_count": 3 if risk_classification == "global_mandatory" else 2,
        "satisfied": True,
    }
    if approver is None or activator_issuer is None or activator_subject is None:
        # No live approver, or no known activator to evaluate against (the
        # bare `check_eligibility` core -- see `activation.py`'s own
        # docstring): the rule cannot be confirmed, so it fails closed
        # rather than defaulting the missing identity to something that
        # would make the comparison trivially pass.
        satisfied = False
        reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    else:
        try:
            validate_actor_separation_v1(obj)
        except ActorSeparationViolationError:
            satisfied = False
            reason_code = REASON_ACTIVATION_PREDICATE_FAILED
    # end-mutation-axis: actor_separation
    return PredicateResult(name=PREDICATE_ACTOR_SEPARATION, satisfied=satisfied, reason_code=reason_code)


# -- predicate 10: operational_integrity ---------------------------------------


def check_operational_integrity(*, satisfied: bool, reason_code: str | None) -> PredicateResult:
    """Threads an already-computed signal through, rather than deciding
    anything itself. `activation.py`'s own `_evaluate` is this predicate's
    one real caller: it calls `RevisionIntegrityService.assess(session,
    revision_id, 'activation')` directly (this module does not import
    `integrity.py` at all -- see that class's own module docstring for why
    every §6.3 caller's reference to `RevisionIntegrityService` has to live
    in the caller file the TDD names, not one module away) and passes the
    bounded `(valid, reason_code)` pair in here. This keeps predicate 10
    the same "call a helper, then guard" shape every other predicate in
    this module uses, with the real work living in the one service built
    and mutation-tested for it.
    """
    result_satisfied = True
    result_reason_code: str | None = None
    # mutation-axis: operational_integrity
    result_satisfied = satisfied
    result_reason_code = reason_code
    # end-mutation-axis: operational_integrity
    return PredicateResult(
        name=PREDICATE_OPERATIONAL_INTEGRITY, satisfied=result_satisfied, reason_code=result_reason_code
    )


__all__ = [
    "PREDICATE_ACTOR_SEPARATION",
    "PREDICATE_BASELINE_CURRENT",
    "PREDICATE_DIGEST_CHAIN",
    "PREDICATE_LATEST_VERSION",
    "PREDICATE_OBSERVATION_QUALIFIED",
    "PREDICATE_OPERATIONAL_INTEGRITY",
    "PREDICATE_ORDER",
    "PREDICATE_PROJECTION_EVIDENCE_VALID",
    "PREDICATE_RISK_REPRODUCIBLE",
    "PREDICATE_SOURCE_VALID",
    "PREDICATE_STATE_APPROVED",
    "REASON_ACTIVATION_PREDICATE_FAILED",
    "REASON_APPROVAL_VERIFICATION_FAILED",
    "REASON_OPERATIONAL_INTEGRITY_PENDING",
    "REASON_SOURCE_STATUS_UNAVAILABLE",
    "PredicateResult",
    "SourceStatusChecker",
    "check_actor_separation",
    "check_baseline_current",
    "check_digest_chain",
    "check_latest_version",
    "check_observation_qualified",
    "check_operational_integrity",
    "check_projection_evidence_valid",
    "check_risk_reproducible",
    "check_source_valid",
    "check_state_approved",
    "requires_observation",
]
