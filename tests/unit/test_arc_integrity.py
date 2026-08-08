"""Unit tests for `RevisionIntegrityService`.

No database: `proposal_queries`/`approval_queries`/`operational_chain_
queries` are monkeypatched module functions on the real imported module
objects (matching `source_status.py`'s own tests' convention of patching a
queries sibling's functions rather than faking `session.execute`), and
`review_package_service`/`source_status_service`/`operational_chain_
service` are trivial fakes satisfying this module's own narrow protocols.

Signing is real for the projection-evidence axis, the same "signing itself
is real" convention `test_arc_operational_chain.py` and `test_arc_approval_
challenge.py` both state for their own suites: every signature this file
presents is an actual Ed25519 signature (or, for the negative cases, a
deliberately wrong one) verified through the real `verify_proof`, over the
real canonical bytes `build_canonical_evidence` produces.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import datetime
import hashlib
import inspect
import pathlib
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from contextplane.arc.service import integrity
from contextplane.arc.service.approval_challenge import ReviewPackageDigests
from contextplane.arc.service.approval_challenge_verification import build_canonical_evidence
from contextplane.arc.service.operational_chain import OperationalChainIntegrityError
from contextplane.arc.service.queries.approval import LiveEvidenceRow, VerifierRow
from contextplane.arc.service.queries.operational_chain import CheckpointRow
from contextplane.arc.service.queries.proposal import VersionRow
from contextplane.arc.service.review_package import ReviewPackageIntegrityError, ReviewPackageUnavailable
from contextplane.arc.service.source_status import SourceStatusUnavailable
from contextplane.exceptions import NotFoundError, ValidationError

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_ARTIFACT_ID = uuid.uuid4()
_REVISION_ID = uuid.uuid4()
_PROPOSAL_ID = uuid.uuid4()
_PROPOSAL_VERSION = 3
_SOURCE_EVIDENCE_ID = uuid.uuid4()
_APPROVAL_VERIFIER_ID = "verifier-1"
_EVIDENCE_ID = uuid.uuid4()
_APPROVAL_CHALLENGE_ID = uuid.uuid4()

_S = "a" * 64
_R = "b" * 64


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


_PRIVATE_KEY, _PUBLIC_KEY = _keypair()
_CREDENTIAL_FINGERPRINT = hashlib.sha256(_PUBLIC_KEY).hexdigest()

# `A`'s real preimage, and the real Ed25519 signature over it -- ground-truth
# cryptography, not a stand-in, matching this suite's own stated convention.
_CANONICAL_EVIDENCE_BYTES = build_canonical_evidence(
    artifact_id=_ARTIFACT_ID, revision_id=_REVISION_ID, artifact_semantics_digest=_S, review_package_digest=_R
)
_APPROVED_PAYLOAD_DIGEST = hashlib.sha256(_CANONICAL_EVIDENCE_BYTES).hexdigest()
_SIGNING_DOMAIN = b"ARC-PROJECTION-APPROVAL-EVIDENCE-V1\x00"  # matches approval_challenge_verification's own value
_SIGNATURE = _PRIVATE_KEY.sign(_SIGNING_DOMAIN + _CANONICAL_EVIDENCE_BYTES)

_SECRETS = frozenset(
    {
        str(_EVIDENCE_ID),
        str(_APPROVAL_CHALLENGE_ID),
        str(_SOURCE_EVIDENCE_ID),
        _APPROVAL_VERIFIER_ID,
        _APPROVED_PAYLOAD_DIGEST,
        _CREDENTIAL_FINGERPRINT,
        _S,
        _R,
        base64.b64encode(_PUBLIC_KEY).decode("ascii"),
        base64.b64encode(_SIGNATURE).decode("ascii"),
    }
)


# ---------------------------------------------------------------------------
# Row builders -- one authoritative shape per fixture, overridable per test.
# ---------------------------------------------------------------------------


def _identity(**overrides: Any) -> VersionRow:
    fields: dict[str, Any] = {
        "proposal_id": _PROPOSAL_ID,
        "proposal_version": _PROPOSAL_VERSION,
        "artifact_id": _ARTIFACT_ID,
        "tenant_id": None,
        "state": "approved",
        "source_evidence_id": _SOURCE_EVIDENCE_ID,
        "reviewed_baseline_revision_id": None,
        "revision_id": _REVISION_ID,
        "risk_classification": "standard",
        "risk_algorithm_version": "v1",
        "opened_by_issuer": "https://idp.example.test",
        "opened_by_subject": "author-1",
        "created_at": _NOW,
        "frozen_at": _NOW,
        "terminal_reason_code": None,
        "terminal_note": None,
        "terminal_by_issuer": None,
        "terminal_by_subject": None,
        "terminalized_at": None,
        "semantics": None,
    }
    fields.update(overrides)
    return VersionRow(**fields)


def _evidence(**overrides: Any) -> LiveEvidenceRow:
    fields: dict[str, Any] = {
        "evidence_id": _EVIDENCE_ID,
        "approval_challenge_id": _APPROVAL_CHALLENGE_ID,
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


def _verifier_row(**overrides: Any) -> VerifierRow:
    fields: dict[str, Any] = {
        "approval_verifier_id": _APPROVAL_VERIFIER_ID,
        "allowed_evidence_types": ("artifact_activation",),
        "valid_from": _NOW - datetime.timedelta(days=1),
        "valid_to": None,
        "revoked_at": None,
        "principal_binding_kind": "exact_principal",
        "principal_issuer": "https://idp.example.test",
        "principal_subject": "verifier-1",
        "provider_id": None,
        "algorithm": "Ed25519",
        "public_key": _PUBLIC_KEY,
        "credential_fingerprint": _CREDENTIAL_FINGERPRINT,
    }
    fields.update(overrides)
    return VerifierRow(**fields)


def _checkpoint(**overrides: Any) -> CheckpointRow:
    fields: dict[str, Any] = {
        "checkpoint_id": uuid.uuid4(),
        "deployment_id": "dep-1",
        "revision_id": _REVISION_ID,
        "sequence": 0,
        "head_digest": "c" * 64,
        "exported_at": _NOW,
        "sink_receipt_digest": "d" * 64,
        "sink_receipt_signature": "e" * 64,
    }
    fields.update(overrides)
    return CheckpointRow(**fields)


# ---------------------------------------------------------------------------
# Fakes for the three injected collaborators.
# ---------------------------------------------------------------------------


class FakeReviewPackageService:
    """Satisfies the `ReviewPackageService` protocol `approval_challenge.py`
    defines. `outcome` selects what `assemble` does: a fixed `S`/`R` pair
    (default), or one of the two exceptions the real service may raise on
    cache drift or an unsubmitted candidate.
    """

    def __init__(self, outcome: ReviewPackageDigests | Exception | None = None) -> None:
        self.outcome: ReviewPackageDigests | Exception = outcome or ReviewPackageDigests(
            artifact_semantics_digest=_S, review_package_digest=_R
        )
        self.calls: list[tuple[uuid.UUID, int]] = []

    async def assemble(self, session: object, *, proposal_id: uuid.UUID, proposal_version: int) -> ReviewPackageDigests:
        self.calls.append((proposal_id, proposal_version))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeSourceStatusService:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[uuid.UUID] = []

    async def check_status(self, source_evidence_id: uuid.UUID) -> object:
        self.calls.append(source_evidence_id)
        if self.outcome is not None:
            raise self.outcome
        return None


class FakeChainVerifier:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[uuid.UUID] = []

    async def verify_chain(self, session: object, revision_id: uuid.UUID) -> None:
        self.calls.append(revision_id)
        if self.outcome is not None:
            raise self.outcome


# ---------------------------------------------------------------------------
# In-memory fake for the four queries functions `assess` calls directly.
# ---------------------------------------------------------------------------


class FakeIntegrityQueries:
    def __init__(self) -> None:
        self.identity: VersionRow | None = _identity()
        self.evidence: LiveEvidenceRow | None = _evidence()
        self.verifier: VerifierRow | None = _verifier_row()
        self.checkpoint: CheckpointRow | None = _checkpoint()

    async def load_version_by_revision_id(self, _session: object, revision_id: uuid.UUID) -> VersionRow | None:
        if self.identity is not None and self.identity.revision_id == revision_id:
            return self.identity
        return None

    async def load_live_evidence_by_revision(self, _session: object, _revision_id: uuid.UUID) -> LiveEvidenceRow | None:
        return self.evidence

    async def load_verifier_for_share(self, _session: object, approval_verifier_id: str) -> VerifierRow | None:
        if self.verifier is not None and self.verifier.approval_verifier_id == approval_verifier_id:
            return self.verifier
        return None

    async def load_latest_checkpoint(self, _session: object, _revision_id: uuid.UUID) -> CheckpointRow | None:
        return self.checkpoint


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> FakeIntegrityQueries:
    fake = FakeIntegrityQueries()
    monkeypatch.setattr(integrity.proposal_queries, "load_version_by_revision_id", fake.load_version_by_revision_id)
    monkeypatch.setattr(
        integrity.approval_queries, "load_live_evidence_by_revision", fake.load_live_evidence_by_revision
    )
    monkeypatch.setattr(integrity.approval_queries, "load_verifier_for_share", fake.load_verifier_for_share)
    monkeypatch.setattr(integrity.operational_chain_queries, "load_latest_checkpoint", fake.load_latest_checkpoint)
    return fake


class _FakeClock:
    def now(self) -> datetime.datetime:
        return _NOW


def _service(
    *,
    review_package: FakeReviewPackageService | None = None,
    source_status: FakeSourceStatusService | None = None,
    chain: FakeChainVerifier | None = None,
) -> integrity.RevisionIntegrityService:
    return integrity.RevisionIntegrityService(
        review_package_service=review_package or FakeReviewPackageService(),
        source_status_service=source_status or FakeSourceStatusService(),
        operational_chain_service=chain or FakeChainVerifier(),
        clock=_FakeClock(),
    )


async def _assess(service: integrity.RevisionIntegrityService, *, purpose: str = integrity.PURPOSE_ACTIVATION) -> Any:
    return await service.assess(object(), _REVISION_ID, purpose)


# ---------------------------------------------------------------------------
# Happy path and the boundary-input check.
# ---------------------------------------------------------------------------


async def test_assess_returns_valid_when_every_axis_is_satisfied(fake_queries: FakeIntegrityQueries) -> None:
    review_package = FakeReviewPackageService()
    source_status = FakeSourceStatusService()
    chain = FakeChainVerifier()
    service = _service(review_package=review_package, source_status=source_status, chain=chain)

    result = await _assess(service)

    assert result == integrity.IntegrityAssessment(valid=True, reason_code=None)
    assert review_package.calls == [(_PROPOSAL_ID, _PROPOSAL_VERSION)]
    assert source_status.calls == [_SOURCE_EVIDENCE_ID]
    assert chain.calls == [_REVISION_ID]


async def test_assess_rejects_a_purpose_outside_the_closed_set(fake_queries: FakeIntegrityQueries) -> None:
    service = _service()
    with pytest.raises(ValidationError):
        await service.assess(object(), _REVISION_ID, "not-a-real-purpose")


async def test_assess_accepts_every_closed_purpose_value(fake_queries: FakeIntegrityQueries) -> None:
    service = _service()
    for purpose in (
        integrity.PURPOSE_ACTIVATION,
        integrity.PURPOSE_CORPUS_ASSEMBLY,
        integrity.PURPOSE_SELECTION,
        integrity.PURPOSE_AUTHORIZATION,
    ):
        result = await _assess(service, purpose=purpose)
        assert result.valid is True


async def test_assess_refuses_when_no_proposal_version_binds_this_revision(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.identity = None
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_FAILED
    )


# ---------------------------------------------------------------------------
# Axis 1: source status. The happy-path test above is this axis's "passes
# when present" half; this is its "fails when the underlying check itself
# fires" half.
# ---------------------------------------------------------------------------


async def test_source_status_axis_refuses_when_the_source_is_unavailable(
    fake_queries: FakeIntegrityQueries,
) -> None:
    service = _service(source_status=FakeSourceStatusService(SourceStatusUnavailable("stale")))
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_SOURCE_STATUS_UNAVAILABLE)


async def test_source_status_axis_refuses_when_the_source_status_row_is_missing(
    fake_queries: FakeIntegrityQueries,
) -> None:
    service = _service(source_status=FakeSourceStatusService(NotFoundError("no status row")))
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_SOURCE_STATUS_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Axis 2: cached derived state -- `ReviewPackageService.assemble` reused,
# never a second recomputation. The happy path already proves it is called
# for every assessment (`review_package.calls`).
# ---------------------------------------------------------------------------


async def test_cached_state_axis_refuses_on_recomputed_digest_drift(fake_queries: FakeIntegrityQueries) -> None:
    service = _service(review_package=FakeReviewPackageService(ReviewPackageIntegrityError("cache drift")))
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_FAILED
    )


async def test_cached_state_axis_refuses_when_the_candidate_is_not_yet_submitted(
    fake_queries: FakeIntegrityQueries,
) -> None:
    service = _service(review_package=FakeReviewPackageService(ReviewPackageUnavailable("no candidate")))
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_PROPOSAL_STATE_CONFLICT)


# ---------------------------------------------------------------------------
# Axis 3: projection evidence.
# ---------------------------------------------------------------------------


async def test_projection_evidence_axis_refuses_when_no_live_evidence_exists(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.evidence = None
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_refuses_when_the_evidence_is_revoked(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.evidence = _evidence(revoked_at=_NOW)
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_refuses_when_the_verifier_no_longer_exists(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.verifier = None
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_refuses_when_the_verifier_is_revoked(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.verifier = _verifier_row(revoked_at=_NOW - datetime.timedelta(minutes=1))
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_refuses_on_credential_fingerprint_drift(
    fake_queries: FakeIntegrityQueries,
) -> None:
    """The verifier re-enrolled or rotated its credential since this
    evidence was accepted: the *current* fingerprint no longer matches the
    one snapshotted at approval time, even though nothing else about the
    stored evidence changed."""
    fake_queries.verifier = _verifier_row(credential_fingerprint="f" * 64)
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_refuses_on_recomputed_digest_mismatch(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.evidence = _evidence(approved_payload_digest="0" * 64)
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_catches_a_public_key_tampered_without_updating_its_fingerprint(
    fake_queries: FakeIntegrityQueries,
) -> None:
    """The specific gap a bare fingerprint comparison would miss: the
    verifier's `public_key` column is replaced with an unrelated key, but
    its `credential_fingerprint` column is left exactly as it was --
    `credential_fingerprint_at_approval` still matches, so the fingerprint
    check alone would pass this straight through. Re-verifying the stored
    signature against the *current* public key is what still catches it.
    """
    _, unrelated_public_key = _keypair()
    fake_queries.verifier = _verifier_row(
        public_key=unrelated_public_key, credential_fingerprint=_CREDENTIAL_FINGERPRINT
    )
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID
    )


async def test_projection_evidence_axis_passes_when_every_check_agrees(fake_queries: FakeIntegrityQueries) -> None:
    """The positive control for every negative test above: the exact same
    fixtures, unmodified, pass. Also proves this axis is exercised on the
    happy path, not merely skipped."""
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(valid=True, reason_code=None)


# ---------------------------------------------------------------------------
# Axis 4: operational chain.
# ---------------------------------------------------------------------------


async def test_operational_chain_axis_refuses_when_the_chain_does_not_verify(
    fake_queries: FakeIntegrityQueries,
) -> None:
    service = _service(
        chain=FakeChainVerifier(OperationalChainIntegrityError("tampered", reason_code="changed_predecessor"))
    )
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_FAILED
    )


# ---------------------------------------------------------------------------
# Axis 5: durable checkpoint.
# ---------------------------------------------------------------------------


async def test_durable_checkpoint_axis_refuses_when_no_checkpoint_exists_yet(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.checkpoint = None
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_PENDING
    )


async def test_durable_checkpoint_axis_refuses_when_the_latest_checkpoint_is_still_pending(
    fake_queries: FakeIntegrityQueries,
) -> None:
    fake_queries.checkpoint = _checkpoint(exported_at=None, sink_receipt_digest=None, sink_receipt_signature=None)
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_PENDING
    )


async def test_durable_checkpoint_axis_refuses_when_exported_but_missing_a_receipt(
    fake_queries: FakeIntegrityQueries,
) -> None:
    """`exported_at` alone is not durability -- a sink acknowledgment
    without its own receipt digest/signature is not distinguishable from a
    write that never actually reached the sink."""
    fake_queries.checkpoint = _checkpoint(sink_receipt_digest=None, sink_receipt_signature=None)
    service = _service()
    result = await _assess(service)
    assert result == integrity.IntegrityAssessment(
        valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_PENDING
    )


# ---------------------------------------------------------------------------
# Bounded result -- a security property, tested directly.
# ---------------------------------------------------------------------------


def test_integrity_assessment_rejects_a_reason_code_on_a_valid_result() -> None:
    with pytest.raises(ValueError):
        integrity.IntegrityAssessment(valid=True, reason_code=integrity.REASON_SOURCE_STATUS_UNAVAILABLE)


def test_integrity_assessment_rejects_a_refusal_with_no_reason_code() -> None:
    with pytest.raises(ValueError):
        integrity.IntegrityAssessment(valid=False, reason_code=None)


def test_integrity_assessment_rejects_a_reason_code_outside_the_closed_set() -> None:
    with pytest.raises(ValueError):
        integrity.IntegrityAssessment(valid=False, reason_code="not_a_registered_refusal_code")


@pytest.mark.parametrize(
    "result",
    [
        integrity.IntegrityAssessment(valid=True, reason_code=None),
        integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_SOURCE_STATUS_UNAVAILABLE),
        integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_PROJECTION_EVIDENCE_INVALID),
        integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_FAILED),
        integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_OPERATIONAL_INTEGRITY_PENDING),
        integrity.IntegrityAssessment(valid=False, reason_code=integrity.REASON_PROPOSAL_STATE_CONFLICT),
    ],
)
def test_integrity_assessment_never_carries_more_than_the_two_bounded_fields(
    result: integrity.IntegrityAssessment,
) -> None:
    field_names = {f.name for f in dataclasses.fields(result)}
    assert field_names == {"valid", "reason_code"}


async def test_every_refusal_scenario_discloses_no_evidence_verifier_or_digest(
    fake_queries: FakeIntegrityQueries,
) -> None:
    """The security property under test, not ergonomics: walks every
    refusal-producing scenario this file exercises above -- including the
    refusal paths, where the temptation to add "helpful" detail (which
    verifier, which digest, whose evidence) is strongest -- and asserts the
    returned object's `repr`/`str` never contains a verifier id, an
    evidence id, a source-evidence id, a digest, a credential fingerprint,
    or key/signature material.
    """
    scenarios: list[Callable[[], Coroutine[Any, Any, integrity.IntegrityAssessment]]] = [
        lambda: _assess(_service(source_status=FakeSourceStatusService(SourceStatusUnavailable("x")))),
        lambda: _assess(_service(review_package=FakeReviewPackageService(ReviewPackageIntegrityError("x")))),
        lambda: _assess(_service(review_package=FakeReviewPackageService(ReviewPackageUnavailable("x")))),
        lambda: _assess(
            _service(chain=FakeChainVerifier(OperationalChainIntegrityError("x", reason_code="sequence_gap")))
        ),
    ]

    def _no_live_evidence() -> Coroutine[Any, Any, integrity.IntegrityAssessment]:
        fake_queries.evidence = None
        return _assess(_service())

    def _pending_checkpoint() -> Coroutine[Any, Any, integrity.IntegrityAssessment]:
        fake_queries.evidence = _evidence()
        fake_queries.checkpoint = _checkpoint(exported_at=None, sink_receipt_digest=None, sink_receipt_signature=None)
        return _assess(_service())

    scenarios.extend([_no_live_evidence, _pending_checkpoint])

    checked = 0
    for build in scenarios:
        result = await build()
        assert result.valid is False
        rendered = f"{result!r} {result}"
        for secret in _SECRETS:
            assert secret not in rendered, f"{secret!r} leaked through {result!r}"
        checked += 1
    assert checked == len(scenarios)


# ---------------------------------------------------------------------------
# Construction and wiring protections.
# ---------------------------------------------------------------------------


def test_the_service_requires_every_collaborator_with_no_default() -> None:
    signature = inspect.signature(integrity.RevisionIntegrityService.__init__)
    for name in ("review_package_service", "source_status_service", "operational_chain_service", "clock"):
        assert signature.parameters[name].default is inspect.Parameter.empty


def test_the_service_is_wired_into_the_typed_container() -> None:
    from contextplane.api.container import Services

    field_names = {f.name for f in dataclasses.fields(Services)}
    assert "arc_integrity" in field_names


# ---------------------------------------------------------------------------
# Wired into exactly the four §6.3 callers, and nowhere else -- proven, not
# stated. This test replaces `test_no_production_caller_references_
# revision_integrity_service_yet`, which asserted the opposite (zero
# references in these same four files) before those four callers were
# wired to call it; that assertion became false by design the moment
# wiring landed, so it is
# replaced rather than merely inverted -- the property below is strictly
# stronger than "these four files call `assess`": it also proves no *fifth*
# service module reaches for `RevisionIntegrityService` outside the four
# files the TDD actually names, which the original test never checked at
# all (it never looked past its own four-path allowlist).
# ---------------------------------------------------------------------------

_WIRED_CALLER_RELATIVE_PATHS = (
    "arc/service/activation.py",
    "arc/service/corpus.py",
    "arc/service/selection.py",
    "arc/service/authorization.py",
)

#: This module's own file, scanned for completeness below. It is the one
#: legitimate module-scope reference outside the four callers -- the class
#: is defined here, and (unlike every caller) it never needs to name
#: itself.
_INTEGRITY_MODULE_RELATIVE_PATH = "arc/service/integrity.py"


def _registry_package_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2] / "contextplane"


def _references_revision_integrity_service(path: pathlib.Path) -> list[str]:
    """AST-based, not a text grep, matching `check_arc_approval_writers.py`'s
    own anchor discipline: a reference has to be an actual `Name`/
    `Attribute`/`alias`/`ImportFrom.module`, not a comment or docstring that
    merely discusses the class or module name.
    """
    watched_name = "RevisionIntegrityService"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and node.id == watched_name:
            name = node.id
        elif isinstance(node, ast.Attribute) and node.attr == watched_name:
            name = node.attr
        elif isinstance(node, ast.alias) and (node.name == watched_name or node.asname == watched_name):
            name = node.name
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.rsplit(".", 1)[-1] == "integrity"
        ):
            # Catches `from contextplane.arc.service import integrity` and
            # `from contextplane.arc.service.integrity import ...` alike, but
            # not `from contextplane.arc.service import artifact_integrity` --
            # the last dotted component must be exactly "integrity".
            name = "integrity"
        if name is not None:
            hits.append(f"{name}@{getattr(node, 'lineno', '?')}")
    return hits


def test_every_wired_caller_references_revision_integrity_service() -> None:
    """The positive half: each of the four files the TDD names as a §6.3
    caller actually imports/references `RevisionIntegrityService` or the
    `integrity` module -- not merely "has a test asserting it calls
    `assess`", which a caller could satisfy by mocking the import away.
    A reference here is the necessary (not sufficient -- the conformance
    suite's own call-graph test covers sufficiency) precondition for the
    call this file's other tests, and `tests/conformance/
    test_arc_integrity_callers.py`, prove happens.
    """
    root = _registry_package_root()
    for relative in _WIRED_CALLER_RELATIVE_PATHS:
        path = root / relative
        hits = _references_revision_integrity_service(path)
        assert hits != [], f"{relative} does not reference RevisionIntegrityService/integrity at all"


def test_no_other_service_module_references_revision_integrity_service() -> None:
    """The negative half, and the actually stronger property: scanning
    every other file under `contextplane/arc/service/` (the same root the
    earlier, replaced `test_no_production_caller_references_
    revision_integrity_service_yet` scoped itself to) finds no *fifth*
    caller. A
    future service module that starts calling `assess` without being one
    of the four the TDD names would be caught here, not waved through
    because it happened to also be correct.
    """
    root = _registry_package_root()
    service_root = root / "arc" / "service"
    wired = {root / relative for relative in _WIRED_CALLER_RELATIVE_PATHS}
    integrity_module = root / _INTEGRITY_MODULE_RELATIVE_PATH

    unexpected: dict[str, list[str]] = {}
    for path in sorted(service_root.rglob("*.py")):
        if path in wired or path == integrity_module:
            continue
        hits = _references_revision_integrity_service(path)
        if hits:
            unexpected[str(path.relative_to(root))] = hits

    assert unexpected == {}, f"unexpected RevisionIntegrityService/integrity reference(s): {unexpected}"


def test_the_scanner_detects_a_planted_reference_in_a_fifth_module(tmp_path: pathlib.Path) -> None:
    """The mechanism proof `test_no_other_service_module_references_
    revision_integrity_service` itself depends on: without this, a scanner
    that matched nothing would pass every file, including a real violation.
    Plants a reference shaped exactly like a legitimate caller's own (a
    `from contextplane.arc.service.integrity import RevisionIntegrityService`
    import) in a throwaway file, proves the walker reports it, then -- the
    "remove it, confirm clean" half -- proves an unrelated file with no
    such import reports nothing.
    """
    planted = tmp_path / "not_a_real_caller.py"
    planted.write_text(
        "from contextplane.arc.service.integrity import RevisionIntegrityService\n\n"
        "def use(x: RevisionIntegrityService) -> None: ...\n",
        encoding="utf-8",
    )
    hits = _references_revision_integrity_service(planted)
    assert hits != [], "the walker failed to detect a planted, unambiguous reference"

    planted.write_text("def use(x: object) -> None: ...\n", encoding="utf-8")
    assert _references_revision_integrity_service(planted) == []
