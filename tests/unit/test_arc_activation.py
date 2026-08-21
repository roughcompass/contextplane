"""Unit tests for the ten named activation predicates
(`contextplane.arc.service.activation_predicates`) and `ActivationService`'s own
orchestration (`contextplane.arc.service.activation`).

No database: every predicate function in `activation_predicates.py` takes
plain dataclasses (`VersionRow`, `FamilyRow`, `LiveEvidenceRow`, `VerifierRow`,
`RiskClassificationRow`, `QualificationRow`) or a narrow injected protocol,
matching `test_arc_integrity.py`'s own stated convention for this package's
predicate/axis suites. Signing is real for the projection-evidence predicate
-- every signature presented is an actual Ed25519 signature (or, for the
negative cases, a deliberately wrong one) verified through the real
`verify_proof`, over the real canonical bytes `build_canonical_evidence`
produces -- matching that same file's convention.

`ActivationService`'s own lock-order and orchestration are tested with a
recording fake session and monkeypatched queries modules, proving the
*order* SQL executes in rather than merely asserting a comment says so
(TDD: "Prove the ordering rather than asserting it").
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from contextplane.arc.service import activation_predicates as predicates
from contextplane.arc.service.approval_challenge import ReviewPackageDigests
from contextplane.arc.service.approval_challenge_verification import (
    ApprovalVerificationFailed,
    DetachedSignatureProofInput,
    _ed25519_verify,
    build_canonical_evidence,
)
from contextplane.arc.service.integrity import IntegrityAssessment
from contextplane.arc.service.queries.approval import LiveEvidenceRow, VerifierRow
from contextplane.arc.service.queries.proposal import FamilyRow, VersionRow
from contextplane.arc.service.queries.qualification import QualificationRow
from contextplane.arc.service.queries.review_package import RiskClassificationRow
from contextplane.arc.service.risk import CURRENT_RISK_ALGORITHM_VERSION

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_ARTIFACT_ID = uuid.uuid4()
_REVISION_ID = uuid.uuid4()
_PROPOSAL_ID = uuid.uuid4()
_PROPOSAL_VERSION = 3
_SOURCE_EVIDENCE_ID = uuid.uuid4()
_APPROVAL_VERIFIER_ID = "verifier-1"

_S = "a" * 64
_R = "b" * 64


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


_PRIVATE_KEY, _PUBLIC_KEY = _keypair()
_CREDENTIAL_FINGERPRINT = hashlib.sha256(_PUBLIC_KEY).hexdigest()

_CANONICAL_EVIDENCE_BYTES = build_canonical_evidence(
    artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, artifact_semantics_digest=_S, review_package_digest=_R
)
_APPROVED_PAYLOAD_DIGEST = hashlib.sha256(_CANONICAL_EVIDENCE_BYTES).hexdigest()
_SIGNING_DOMAIN = b"ARC-PROJECTION-APPROVAL-EVIDENCE-V1\x00"  # matches approval_challenge_verification's own value
_SIGNATURE = _PRIVATE_KEY.sign(_SIGNING_DOMAIN + _CANONICAL_EVIDENCE_BYTES)

_DIGESTS = ReviewPackageDigests(artifact_semantics_digest=_S, review_package_digest=_R)


# ---------------------------------------------------------------------------
# Row builders -- one authoritative shape per fixture, overridable per test.
# ---------------------------------------------------------------------------


def _version(**overrides: Any) -> VersionRow:
    fields: dict[str, Any] = {
        "proposal_id": _PROPOSAL_ID,
        "proposal_version": _PROPOSAL_VERSION,
        "artifact_id": _ARTIFACT_ID,
        "tenant_id": None,
        "state": "approved",
        "source_evidence_id": _SOURCE_EVIDENCE_ID,
        "reviewed_baseline_revision_id": None,
        "revision_id": _REVISION_ID,
        "risk_classification": "intent_non_mandatory",
        "risk_algorithm_version": CURRENT_RISK_ALGORITHM_VERSION,
        "opened_by_issuer": "https://idp.example.test",
        "opened_by_subject": "submitter-1",
        "created_at": _NOW,
        "frozen_at": _NOW,
        "terminal_reason_code": None,
        "terminal_note": None,
        "terminal_by_issuer": None,
        "terminal_by_subject": None,
        "terminalized_at": None,
        "semantics": {"applicability": [{"scope": "intent", "is_mandatory": False}]},
    }
    fields.update(overrides)
    return VersionRow(**fields)


def _family(**overrides: Any) -> FamilyRow:
    fields: dict[str, Any] = {
        "artifact_id": _ARTIFACT_ID,
        "tenant_id": None,
        "slug": "family-1",
        "kind": "policy",
        "title": "Test family",
        "active_revision_id": None,
        "created_at": _NOW,
        "created_by_issuer": "https://idp.example.test",
        "created_by_subject": "creator-1",
    }
    fields.update(overrides)
    return FamilyRow(**fields)


def _evidence(**overrides: Any) -> LiveEvidenceRow:
    fields: dict[str, Any] = {
        "evidence_id": uuid.uuid4(),
        "approval_challenge_id": uuid.uuid4(),
        "proposal_id": _PROPOSAL_ID,
        "proposal_version": _PROPOSAL_VERSION,
        "revision_id": _REVISION_ID,
        "approved_payload_digest": _APPROVED_PAYLOAD_DIGEST,
        "approval_verifier_id": _APPROVAL_VERIFIER_ID,
        "credential_fingerprint_at_approval": _CREDENTIAL_FINGERPRINT,
        "verification_method": "detached_signature",
        "signature_algorithm": "Ed25519",
        "proof_bytes": _SIGNATURE,
        "revoked_at": None,
    }
    fields.update(overrides)
    return LiveEvidenceRow(**fields)


def _verifier(**overrides: Any) -> VerifierRow:
    fields: dict[str, Any] = {
        "approval_verifier_id": _APPROVAL_VERIFIER_ID,
        "allowed_evidence_types": ("artifact_activation",),
        "valid_from": _NOW - datetime.timedelta(days=1),
        "valid_to": None,
        "revoked_at": None,
        "principal_binding_kind": "exact_principal",
        "principal_issuer": "https://idp.example.test",
        "principal_subject": "verifier-principal",
        "provider_id": None,
        "algorithm": "Ed25519",
        "public_key": _PUBLIC_KEY,
        "credential_fingerprint": _CREDENTIAL_FINGERPRINT,
    }
    fields.update(overrides)
    return VerifierRow(**fields)


def _risk_row(**overrides: Any) -> RiskClassificationRow:
    fields: dict[str, Any] = {
        "classification": "intent_non_mandatory",
        "algorithm_version": CURRENT_RISK_ALGORITHM_VERSION,
        "computed_at": _NOW,
    }
    fields.update(overrides)
    return RiskClassificationRow(**fields)


def _qualification(**overrides: Any) -> QualificationRow:
    fields: dict[str, Any] = {
        "qualification_id": uuid.uuid4(),
        "idempotency_key_digest": "f" * 64,
        "candidate_review_package_digest": _R,
        "candidate_revision_id": _REVISION_ID,
        "proposal_id": _PROPOSAL_ID,
        "proposal_version": _PROPOSAL_VERSION,
        "risk_classification": "global_mandatory",
        "risk_algorithm_version": CURRENT_RISK_ALGORITHM_VERSION,
        "baseline_revision_id": None,
        "selection_engine_version": "v1",
        "engine_configuration_version": "arc_selection_config_v1",
        "cohort_id": uuid.uuid4(),
        "cohort_digest": "c" * 64,
        "window_started_at": _NOW - datetime.timedelta(hours=72),
        "window_ended_at": _NOW,
        "eligible_count": 1000,
        "observed_count": 1000,
        "expected_impact_envelope_digest": "d" * 64,
        "counters_by_delta_code": [],
        "unexplained_count": 0,
        "out_of_envelope_count": 0,
        "replay_corpus_digest": None,
        "replay_result_digest": None,
        "qualification_algorithm_version": "arc_observation_qualification_v1",
        "computed_decision": "qualified",
        "computed_at": _NOW,
        "reason_codes": ["window_met", "coverage_complete"],
        "accepted_by_issuer": "https://idp.example.test",
        "accepted_by_subject": "accepter-1",
        "accepted_by_role": "activator",
        "accepted_at": _NOW,
        "acceptance_audit_reference": "ref",
        "expires_at": _NOW + datetime.timedelta(hours=24),
    }
    fields.update(overrides)
    return QualificationRow(**fields)


class FakeSourceStatusChecker:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[uuid.UUID] = []

    async def check_status(self, source_evidence_id: uuid.UUID) -> object:
        self.calls.append(source_evidence_id)
        if self.outcome is not None:
            raise self.outcome
        return None


# ---------------------------------------------------------------------------
# Predicate 1: latest_version
# ---------------------------------------------------------------------------


def test_latest_version_satisfied_when_this_is_the_latest() -> None:
    version = _version()
    result = predicates.check_latest_version(version, version)
    assert result.satisfied is True
    assert result.reason_code is None
    assert result.name == predicates.PREDICATE_LATEST_VERSION


def test_latest_version_refused_when_a_newer_version_exists() -> None:
    version = _version(proposal_version=1)
    latest = _version(proposal_version=2)
    result = predicates.check_latest_version(version, latest)
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_ACTIVATION_PREDICATE_FAILED


def test_latest_version_refused_when_thread_has_vanished() -> None:
    result = predicates.check_latest_version(_version(), None)
    assert result.satisfied is False


# ---------------------------------------------------------------------------
# Predicate 2: state_approved
# ---------------------------------------------------------------------------


def test_state_approved_satisfied_for_approved_state() -> None:
    assert predicates.check_state_approved(_version(state="approved")).satisfied is True


@pytest.mark.parametrize("state", ["submitted", "activated", "stale", "superseded", "open", "rejected", "withdrawn"])
def test_state_approved_refused_for_every_other_state(state: str) -> None:
    result = predicates.check_state_approved(_version(state=state))
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_ACTIVATION_PREDICATE_FAILED


# ---------------------------------------------------------------------------
# Predicate 3: digest_chain
# ---------------------------------------------------------------------------


def test_digest_chain_satisfied_when_recomputed_a_matches_evidence() -> None:
    result = predicates.check_digest_chain(
        artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, digests=_DIGESTS, live_evidence=_evidence()
    )
    assert result.satisfied is True


def test_digest_chain_refused_when_digests_could_not_be_recomputed() -> None:
    result = predicates.check_digest_chain(
        artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, digests=None, live_evidence=_evidence()
    )
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_ACTIVATION_PREDICATE_FAILED


def test_digest_chain_refused_when_no_live_evidence() -> None:
    result = predicates.check_digest_chain(
        artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, digests=_DIGESTS, live_evidence=None
    )
    assert result.satisfied is False


def test_digest_chain_refused_when_evidence_is_revoked() -> None:
    result = predicates.check_digest_chain(
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        digests=_DIGESTS,
        live_evidence=_evidence(revoked_at=_NOW),
    )
    assert result.satisfied is False


def test_digest_chain_refused_on_tampered_digest() -> None:
    """The planted failure: R changes (a tampered review package), so the
    recomputed A disagrees with the digest committed at approval time."""
    tampered = ReviewPackageDigests(artifact_semantics_digest=_S, review_package_digest="c" * 64)
    result = predicates.check_digest_chain(
        artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, digests=tampered, live_evidence=_evidence()
    )
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_APPROVAL_VERIFICATION_FAILED


# ---------------------------------------------------------------------------
# Predicate 4: baseline_current
# ---------------------------------------------------------------------------


def test_baseline_current_satisfied_on_first_activation_with_no_reviewed_baseline() -> None:
    version = _version(reviewed_baseline_revision_id=None)
    family = _family(active_revision_id=None)
    assert predicates.check_baseline_current(version, family).satisfied is True


def test_baseline_current_satisfied_when_family_matches_reviewed_baseline() -> None:
    baseline_id = uuid.uuid4()
    version = _version(reviewed_baseline_revision_id=baseline_id)
    family = _family(active_revision_id=baseline_id)
    assert predicates.check_baseline_current(version, family).satisfied is True


def test_baseline_current_refused_when_family_drifted_since_review() -> None:
    """The planted failure: someone else activated a different revision
    while this candidate was under review."""
    reviewed_baseline_id = uuid.uuid4()
    drifted_active_id = uuid.uuid4()
    version = _version(reviewed_baseline_revision_id=reviewed_baseline_id)
    family = _family(active_revision_id=drifted_active_id)
    result = predicates.check_baseline_current(version, family)
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_ACTIVATION_PREDICATE_FAILED


def test_baseline_current_refused_when_no_baseline_was_reviewed_but_family_now_has_one() -> None:
    version = _version(reviewed_baseline_revision_id=None)
    family = _family(active_revision_id=uuid.uuid4())
    assert predicates.check_baseline_current(version, family).satisfied is False


# ---------------------------------------------------------------------------
# Predicate 5: source_valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_valid_satisfied_when_status_check_passes() -> None:
    checker = FakeSourceStatusChecker()
    result = await predicates.check_source_valid(checker, _version())
    assert result.satisfied is True
    assert checker.calls == [_SOURCE_EVIDENCE_ID]


@pytest.mark.asyncio
async def test_source_valid_refused_when_status_check_raises() -> None:
    """The planted failure: the source has since been revoked."""
    from contextplane.arc.service.source_status import SourceStatusUnavailable

    checker = FakeSourceStatusChecker(SourceStatusUnavailable("revoked"))
    result = await predicates.check_source_valid(checker, _version())
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_SOURCE_STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Predicate 6: risk_reproducible
# ---------------------------------------------------------------------------


def test_risk_reproducible_satisfied_when_classification_reproduces() -> None:
    version = _version(semantics={"applicability": [{"scope": "intent", "is_mandatory": False}]})
    risk_row = _risk_row(classification="intent_non_mandatory")
    result, stale = predicates.check_risk_reproducible(version, risk_row)
    assert result.satisfied is True
    assert stale is False


def test_risk_reproducible_refused_when_recomputed_classification_disagrees() -> None:
    """The planted failure: the persisted classification no longer matches
    what the pinned reducer produces from the frozen candidate."""
    version = _version(semantics={"applicability": [{"scope": "intent", "is_mandatory": False}]})
    risk_row = _risk_row(classification="global_mandatory")
    result, stale = predicates.check_risk_reproducible(version, risk_row)
    assert result.satisfied is False
    assert stale is False


def test_risk_reproducible_refused_and_flags_stale_when_reducer_is_retired() -> None:
    """The one write-bearing failure: `activate()` reads `stale` to trigger
    the atomic terminalization -- see that method's own docstring."""
    version = _version()
    risk_row = _risk_row(algorithm_version="arc_risk_reducer_v999_retired")
    result, stale = predicates.check_risk_reproducible(version, risk_row)
    assert result.satisfied is False
    assert stale is True


def test_risk_reproducible_refused_when_no_risk_row_exists() -> None:
    result, stale = predicates.check_risk_reproducible(_version(), None)
    assert result.satisfied is False
    assert stale is False


# ---------------------------------------------------------------------------
# Predicate 7: observation_qualified
# ---------------------------------------------------------------------------


def test_observation_qualified_satisfied_when_not_required_and_no_qualification_given() -> None:
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="intent_non_mandatory"),
        qualification=None,
        qualification_id=None,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is True


def test_observation_qualified_refused_when_required_but_missing() -> None:
    """The planted failure: a global-mandatory candidate with no
    qualification named at all."""
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=None,
        qualification_id=None,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is False


def test_observation_qualified_refused_when_forbidden_but_supplied() -> None:
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="intent_non_mandatory"),
        qualification=_qualification(),
        qualification_id=uuid.uuid4(),
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is False


def test_observation_qualified_satisfied_when_required_and_positive_and_unexpired() -> None:
    qid = uuid.uuid4()
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=_qualification(qualification_id=qid, candidate_revision_id=_REVISION_ID),
        qualification_id=qid,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is True


def test_observation_qualified_refused_when_expired() -> None:
    """The planted failure: the 24-hour acceptance validity window passed."""
    qid = uuid.uuid4()
    qualification = _qualification(
        qualification_id=qid,
        candidate_revision_id=_REVISION_ID,
        expires_at=_NOW - datetime.timedelta(minutes=1),
    )
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=qualification,
        qualification_id=qid,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is False


def test_observation_qualified_refused_when_decision_is_not_positive() -> None:
    qid = uuid.uuid4()
    qualification = _qualification(qualification_id=qid, candidate_revision_id=_REVISION_ID, computed_decision="failed")
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=qualification,
        qualification_id=qid,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is False


def test_observation_qualified_refused_when_qualification_names_a_different_revision() -> None:
    qid = uuid.uuid4()
    qualification = _qualification(qualification_id=qid, candidate_revision_id=uuid.uuid4())
    result = predicates.check_observation_qualified(
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=qualification,
        qualification_id=qid,
        revision_id=_REVISION_ID,
        now=_NOW,
    )
    assert result.satisfied is False


# ---------------------------------------------------------------------------
# Predicate 8: projection_evidence_valid
# ---------------------------------------------------------------------------


def _pev(
    *,
    live_evidence: LiveEvidenceRow | None,
    verifier: VerifierRow | None,
    digests: ReviewPackageDigests | None = _DIGESTS,
) -> predicates.PredicateResult:
    return predicates.check_projection_evidence_valid(
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        digests=digests,
        live_evidence=live_evidence,
        verifier=verifier,
        now=_NOW,
        attestation_providers={},
        signature_verifier=_ed25519_verify,
    )


def test_projection_evidence_valid_satisfied_with_a_real_current_signature() -> None:
    result = _pev(live_evidence=_evidence(), verifier=_verifier())
    assert result.satisfied is True


def test_projection_evidence_valid_refused_when_no_live_evidence() -> None:
    result = _pev(live_evidence=None, verifier=_verifier())
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_APPROVAL_VERIFICATION_FAILED


def test_projection_evidence_valid_refused_when_verifier_revoked() -> None:
    """The planted failure: the verifier was revoked after signing."""
    result = _pev(live_evidence=_evidence(), verifier=_verifier(revoked_at=_NOW - datetime.timedelta(minutes=1)))
    assert result.satisfied is False


def test_projection_evidence_valid_refused_on_credential_fingerprint_drift() -> None:
    """The planted failure: the verifier's credential was rotated since
    approval -- the snapshot no longer matches the live row."""
    result = _pev(live_evidence=_evidence(), verifier=_verifier(credential_fingerprint="f" * 64))
    assert result.satisfied is False


def test_projection_evidence_valid_refused_when_signature_does_not_verify_against_current_key() -> None:
    """The planted failure: a tampered key rotated in place without its
    fingerprint column being updated to match -- caught by re-verifying the
    signature against the *current* public key, not merely by the
    fingerprint check."""
    _, other_public_key = _keypair()
    result = _pev(
        live_evidence=_evidence(),
        verifier=_verifier(public_key=other_public_key, credential_fingerprint=_CREDENTIAL_FINGERPRINT),
    )
    assert result.satisfied is False


def test_projection_evidence_valid_refused_when_no_recomputable_digests() -> None:
    result = _pev(live_evidence=_evidence(), verifier=_verifier(), digests=None)
    assert result.satisfied is False


def test_assert_projection_evidence_valid_raises_the_bounded_exception_type() -> None:
    with pytest.raises(ApprovalVerificationFailed):
        predicates._assert_projection_evidence_valid(
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            digests=_DIGESTS,
            live_evidence=None,
            verifier=None,
            now=_NOW,
            attestation_providers={},
            signature_verifier=_ed25519_verify,
        )


def test_detached_signature_proof_input_shape_is_the_one_this_axis_re_encodes() -> None:
    """Documents the exact re-encoding `_assert_projection_evidence_valid`
    performs internally (base64 of the stored raw proof bytes) against the
    real verifier -- proof the re-encoded proof is byte-identical to the one
    presented at completion time, not merely "some signature verifies"."""
    import base64

    proof = DetachedSignatureProofInput(
        signature_algorithm="Ed25519", signature_base64=base64.b64encode(_SIGNATURE).decode("ascii")
    )
    assert base64.b64decode(proof.signature_base64) == _SIGNATURE


# ---------------------------------------------------------------------------
# Predicate 9: actor_separation
# ---------------------------------------------------------------------------


class _FakeSession:
    """A bare stand-in session -- `check_actor_separation` never calls
    `session.execute` itself; it calls `qual_queries.load_approving_
    principal(session, revision_id)`, which this file monkeypatches."""


@pytest.mark.asyncio
async def test_actor_separation_satisfied_when_three_identities_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    approver = ("https://idp.example.test", "approver-1")

    async def _fake_approver(session: object, revision_id: uuid.UUID) -> tuple[str, str] | None:
        return approver

    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_approver)

    version = _version(opened_by_issuer="https://idp.example.test", opened_by_subject="submitter-1")
    result = await predicates.check_actor_separation(
        _FakeSession(),
        version=version,
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=None,
        revision_id=_REVISION_ID,
        activator_issuer="https://idp.example.test",
        activator_subject="activator-1",
    )
    assert result.satisfied is True
    # The three compared identities genuinely differ -- this is the actual
    # proof, not merely that `validate_actor_separation_v1` didn't raise.
    submitter = (version.opened_by_issuer, version.opened_by_subject)
    activator = ("https://idp.example.test", "activator-1")
    assert len({submitter, approver, activator}) == 3


@pytest.mark.asyncio
async def test_actor_separation_refused_when_submitter_is_the_approver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The planted failure: submitter and approver are the same identity --
    refused regardless of risk classification."""

    async def _fake_approver(session: object, revision_id: uuid.UUID) -> tuple[str, str] | None:
        return ("https://idp.example.test", "submitter-1")

    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_approver)

    version = _version(opened_by_issuer="https://idp.example.test", opened_by_subject="submitter-1")
    result = await predicates.check_actor_separation(
        _FakeSession(),
        version=version,
        risk_row=_risk_row(classification="intent_non_mandatory"),
        qualification=None,
        revision_id=_REVISION_ID,
        activator_issuer="https://idp.example.test",
        activator_subject="activator-1",
    )
    assert result.satisfied is False
    submitter = (version.opened_by_issuer, version.opened_by_subject)
    approver = ("https://idp.example.test", "submitter-1")
    assert submitter == approver, "the planted violation: these two identities are equal, not merely compared"


@pytest.mark.asyncio
async def test_actor_separation_refused_for_global_mandatory_without_a_third_distinct_activator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planted failure: a global-mandatory candidate where the
    activator is the same identity as the approver -- only two distinct
    identities exist where three are required."""
    approver = ("https://idp.example.test", "approver-1")

    async def _fake_approver(session: object, revision_id: uuid.UUID) -> tuple[str, str] | None:
        return approver

    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_approver)

    version = _version(opened_by_issuer="https://idp.example.test", opened_by_subject="submitter-1")
    result = await predicates.check_actor_separation(
        _FakeSession(),
        version=version,
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=None,
        revision_id=_REVISION_ID,
        activator_issuer=approver[0],
        activator_subject=approver[1],
    )
    assert result.satisfied is False
    submitter = (version.opened_by_issuer, version.opened_by_subject)
    activator = (approver[0], approver[1])
    assert activator == approver != submitter
    assert len({submitter, approver, activator}) == 2, "only two distinct identities, three are required"


@pytest.mark.asyncio
async def test_actor_separation_refused_when_no_approver_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_approver(session: object, revision_id: uuid.UUID) -> tuple[str, str] | None:
        return None

    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_approver)

    result = await predicates.check_actor_separation(
        _FakeSession(),
        version=_version(),
        risk_row=_risk_row(classification="intent_non_mandatory"),
        qualification=None,
        revision_id=_REVISION_ID,
        activator_issuer="https://idp.example.test",
        activator_subject="activator-1",
    )
    assert result.satisfied is False


@pytest.mark.asyncio
async def test_actor_separation_refused_when_no_activator_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check_eligibility`'s bare core (no caller identity yet) cannot
    confirm the third-distinct-identity rule and fails closed rather than
    defaulting to a value that would make the comparison trivially pass."""

    async def _fake_approver(session: object, revision_id: uuid.UUID) -> tuple[str, str] | None:
        return ("https://idp.example.test", "approver-1")

    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_approver)

    result = await predicates.check_actor_separation(
        _FakeSession(),
        version=_version(),
        risk_row=_risk_row(classification="global_mandatory"),
        qualification=None,
        revision_id=_REVISION_ID,
        activator_issuer=None,
        activator_subject=None,
    )
    assert result.satisfied is False


# ---------------------------------------------------------------------------
# Predicate 10: operational_integrity
# ---------------------------------------------------------------------------


def test_operational_integrity_reports_satisfied_when_the_assessment_is_valid() -> None:
    """No longer hard-wired: `check_operational_integrity` threads through
    whatever `RevisionIntegrityService.assess` (called by `activation.py`'s
    own `_evaluate`, not by this function) already decided -- see this
    function's own docstring."""
    result = predicates.check_operational_integrity(satisfied=True, reason_code=None)
    assert result.satisfied is True
    assert result.reason_code is None
    assert result.name == predicates.PREDICATE_OPERATIONAL_INTEGRITY


def test_operational_integrity_reports_refused_when_the_assessment_is_invalid() -> None:
    result = predicates.check_operational_integrity(
        satisfied=False, reason_code=predicates.REASON_OPERATIONAL_INTEGRITY_PENDING
    )
    assert result.satisfied is False
    assert result.reason_code == predicates.REASON_OPERATIONAL_INTEGRITY_PENDING
    assert result.name == predicates.PREDICATE_OPERATIONAL_INTEGRITY


def test_operational_integrity_decides_nothing_itself() -> None:
    """It has no collaborator and no default: every input it could ever
    branch on arrives as an explicit argument, so the only way this
    predicate is ever satisfied is a caller (`activation.py`) that actually
    computed a real assessment and passed the result in."""
    import inspect

    sig = inspect.signature(predicates.check_operational_integrity)
    assert set(sig.parameters) == {"satisfied", "reason_code"}
    assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values())


# ---------------------------------------------------------------------------
# Predicate manifest
# ---------------------------------------------------------------------------


def test_predicate_order_has_exactly_ten_unique_names_in_the_stated_order() -> None:
    assert predicates.PREDICATE_ORDER == (
        "latest_version",
        "state_approved",
        "digest_chain",
        "baseline_current",
        "source_valid",
        "risk_reproducible",
        "observation_qualified",
        "projection_evidence_valid",
        "actor_separation",
        "operational_integrity",
    )
    assert len(set(predicates.PREDICATE_ORDER)) == 10


def test_requires_observation_matches_adr_041_vocabulary() -> None:
    assert predicates.requires_observation("global_mandatory") is True
    assert predicates.requires_observation("global_non_mandatory") is True
    assert predicates.requires_observation("tenant_mandatory") is True
    assert predicates.requires_observation("tenant_non_mandatory") is False
    assert predicates.requires_observation("intent_non_mandatory") is False


# ---------------------------------------------------------------------------
# `ActivationService._evaluate`: lock order and full aggregation.
# ---------------------------------------------------------------------------

from contextplane.arc.service import activation  # noqa: E402 - grouped with the orchestration tests it belongs to
from contextplane.arc.service.authorization import ArcAuthorizationService  # noqa: E402
from contextplane.arc.types import ArcRequestContext  # noqa: E402
from contextplane.types import TenantContext  # noqa: E402


class _AllowAllVisibility:
    async def visible_entity_ids(self, ctx: object, entity_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(entity_ids)


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAllVisibility())


def _ctx() -> ArcRequestContext:
    tenant = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"], oidc_subject="activator-1")
    return ArcRequestContext(tenant=tenant, oidc_issuer="https://idp.example.test")


class _FakeClock:
    def now(self) -> datetime.datetime:
        return _NOW


class _FakeReviewPackage:
    async def assemble(self, session: object, *, proposal_id: uuid.UUID, proposal_version: int) -> ReviewPackageDigests:
        return _DIGESTS


class FakeIntegrityAssessor:
    """Stands in for `RevisionIntegrityService` -- `_evaluate` only ever
    calls `.assess(session, revision_id, purpose)` and reads the bounded
    result back, so a trivial fake configurable per test is all predicate
    10's own real work needs. Defaults to `valid=True` (satisfied): most
    tests below exercise a different predicate and should not have to know
    or care what this one reports.
    """

    def __init__(self, *, valid: bool = True, reason_code: str | None = None) -> None:
        self.valid = valid
        self.reason_code = reason_code
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def assess(self, session: object, revision_id: uuid.UUID, purpose: str) -> IntegrityAssessment:
        self.calls.append((revision_id, purpose))
        return IntegrityAssessment(valid=self.valid, reason_code=self.reason_code)


def _wire_evaluate_dependencies(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], *, integrity: FakeIntegrityAssessor | None = None
) -> activation.ActivationService:
    """Monkeypatches every module-level query function `_evaluate` calls,
    each recording the order it ran in on *calls*. Not a real session or
    database: `_evaluate` never dereferences the `object()` this file passes
    it as `session`, because every function that would have used it is
    replaced here.
    """
    version = _version()
    family = _family(active_revision_id=None)

    async def _load_version_by_revision_id(session: object, revision_id: uuid.UUID) -> VersionRow:
        return version

    async def _load_live_evidence(session: object, revision_id: uuid.UUID) -> None:
        calls.append("read:evidence")
        return None

    async def _load_family_for_update(session: object, artifact_id: uuid.UUID) -> FamilyRow:
        calls.append("lock:artifact")
        return family

    async def _lock_proposal_version(session: object, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow:
        calls.append("lock:proposal_version")
        return version

    async def _lock_family_fn(session: object, revision_id: uuid.UUID) -> None:
        calls.append("lock:revision")

    async def _load_latest_version(session: object, proposal_id: uuid.UUID) -> VersionRow:
        return version

    async def _load_risk_classification(
        session: object, proposal_id: uuid.UUID, proposal_version: int
    ) -> RiskClassificationRow:
        return _risk_row()

    async def _fake_load_approving_principal(session: object, revision_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(activation.proposal_queries, "load_version_by_revision_id", _load_version_by_revision_id)
    monkeypatch.setattr(activation.approval_queries, "load_live_evidence_by_revision", _load_live_evidence)
    monkeypatch.setattr(activation.proposal_queries, "load_family_for_update", _load_family_for_update)
    monkeypatch.setattr(activation.queries, "lock_proposal_version", _lock_proposal_version)
    monkeypatch.setattr(activation, "_lock_family", _lock_family_fn)
    monkeypatch.setattr(activation.proposal_queries, "load_latest_version", _load_latest_version)
    monkeypatch.setattr(activation.rp_queries, "load_risk_classification", _load_risk_classification)
    monkeypatch.setattr(predicates.qual_queries, "load_approving_principal", _fake_load_approving_principal)

    return activation.ActivationService(
        session_factory=None,  # type: ignore[arg-type] - _evaluate is called directly below, never opens one
        authorization=_authorization(),
        clock=_FakeClock(),  # type: ignore[arg-type]
        review_package=_FakeReviewPackage(),
        source_status=FakeSourceStatusChecker(),
        artifacts=None,  # type: ignore[arg-type] - _evaluate never reads self._artifacts
        integrity=integrity or FakeIntegrityAssessor(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_evaluate_acquires_write_locks_in_the_stated_ascending_class_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module docstring's lock order, proven rather than asserted:
    evidence read first (read-only), then artifact, then proposal version,
    then the revision family -- never any other order."""
    calls: list[str] = []
    service = _wire_evaluate_dependencies(monkeypatch, calls)

    await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )

    assert calls == ["read:evidence", "lock:artifact", "lock:proposal_version", "lock:revision"]


@pytest.mark.asyncio
async def test_evaluate_always_reports_all_ten_predicates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    service = _wire_evaluate_dependencies(monkeypatch, calls)

    evaluation = await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )

    names = [p.name for p in evaluation.eligibility.predicates]
    assert names == list(predicates.PREDICATE_ORDER)
    assert len(evaluation.eligibility.predicates) == 10


@pytest.mark.asyncio
async def test_evaluate_calls_assess_with_this_revision_and_the_activation_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Predicate 10 is no longer hard-wired: `_evaluate` calls
    `RevisionIntegrityService.assess` directly, for this exact revision,
    tagged `activation` -- not some other purpose a different caller uses."""
    calls: list[str] = []
    integrity = FakeIntegrityAssessor()
    service = _wire_evaluate_dependencies(monkeypatch, calls, integrity=integrity)

    await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )

    assert integrity.calls == [(_REVISION_ID, activation.PURPOSE_ACTIVATION)]


@pytest.mark.asyncio
async def test_evaluate_reports_predicate_10_satisfied_when_the_assessment_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service = _wire_evaluate_dependencies(monkeypatch, calls, integrity=FakeIntegrityAssessor(valid=True))

    evaluation = await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )

    integrity_result = next(
        p for p in evaluation.eligibility.predicates if p.name == predicates.PREDICATE_OPERATIONAL_INTEGRITY
    )
    assert integrity_result.satisfied is True
    assert integrity_result.reason_code is None


@pytest.mark.asyncio
async def test_evaluate_is_ineligible_when_the_assessment_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Predicate 10 alone is still enough to block activation -- the same
    property the hard-wired version of this predicate used to prove by
    construction, now proven through a real (faked) refusal instead."""
    calls: list[str] = []
    integrity = FakeIntegrityAssessor(valid=False, reason_code="arc_operational_integrity_pending")
    service = _wire_evaluate_dependencies(monkeypatch, calls, integrity=integrity)

    evaluation = await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )

    integrity_result = next(
        p for p in evaluation.eligibility.predicates if p.name == predicates.PREDICATE_OPERATIONAL_INTEGRITY
    )
    assert integrity_result.satisfied is False
    assert integrity_result.reason_code == "arc_operational_integrity_pending"
    assert evaluation.eligibility.eligible is False


@pytest.mark.asyncio
async def test_evaluate_flags_stale_reducer_retirement_for_activate_to_act_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    service = _wire_evaluate_dependencies(monkeypatch, calls)

    async def _retired_risk_row(
        session: object, proposal_id: uuid.UUID, proposal_version: int
    ) -> RiskClassificationRow:
        return _risk_row(algorithm_version="arc_risk_reducer_v999_retired")

    monkeypatch.setattr(activation.rp_queries, "load_risk_classification", _retired_risk_row)

    evaluation = await service._evaluate(
        object(), _REVISION_ID, activator_issuer=None, activator_subject=None, qualification_id=None
    )
    assert evaluation.stale_reducer_retired is True
