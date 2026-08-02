"""ApprovalEvidenceVerifier: verifying one `ApprovalEvidenceV1` object.

No database here -- the real verifier-row lookup locks a row `FOR SHARE`
inside the caller's resolution transaction, but this module only needs the
lookup's *shape*, so tests supply in-memory fakes instead, exactly as
`test_arc_attestation.py` does for host attestation.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from registry.arc.schemas.canonical import canonicalize_approval_evidence
from registry.arc.service.approval import (
    ApprovalEvidence,
    ApprovalEvidenceVerificationError,
    ApprovalEvidenceVerifier,
    ApprovalVerifierRecord,
    VerifiedApprovalEvidence,
)

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
_ARTIFACT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_REVISION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_EXCEPTION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_EVIDENCE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")

_SIGNER_KEY_ID = "opk-1"
_PROVIDER_VERIFIER_ID = "prov-1"
_PROVIDER_ID = "trusted-system-1"

_ALL_EVIDENCE_TYPES = frozenset(
    {"artifact_activation", "exception_approval", "global_exception_approval", "gateway_emergency_bypass"}
)

# Matches `approval.py`'s own tag. Redeclared here rather than imported, the
# same way `test_arc_attestation.py` redeclares its domain tag: the test
# encodes the expected wire contract independently of the module under test.
_SIGNING_DOMAIN = b"ARC-APPROVAL-EVIDENCE-V1\x00"


def _keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw_private, raw_public


# --- evidence construction ----------------------------------------------------


def _base_fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_type": "artifact_activation",
        "scope_kind": "tenant",
        "scope_tenant_id": _TENANT_ID,
        "approved_artifact_id": _ARTIFACT_ID,
        "approved_revision_id": _REVISION_ID,
        "approved_exception_id": None,
        "action_instance_id": None,
        "policy_version": None,
        "approved_payload_digest": "d" * 64,
        "approving_principal": "admin@example.test",
        "approving_role": "admin",
        "source_system_approval_locator": None,
        "approval_timestamp": _NOW - datetime.timedelta(minutes=5),
        "expires_at": None,
        "verification_method": "operator_signed",
        "audit_log_reference": "audit-1",
        "signer_key_id": _SIGNER_KEY_ID,
        "signature": None,
        "approval_verifier_id": None,
        "verifier_attestation": None,
        "verifier_identity": None,
    }
    base.update(overrides)
    return base


def _exception_fields(**overrides: object) -> dict[str, object]:
    fields = _base_fields(
        evidence_type="exception_approval",
        approved_artifact_id=None,
        approved_revision_id=None,
        approved_exception_id=_EXCEPTION_ID,
    )
    fields.update(overrides)
    return fields


def _bypass_fields(**overrides: object) -> dict[str, object]:
    fields = _base_fields(
        evidence_type="gateway_emergency_bypass",
        approved_artifact_id=None,
        approved_revision_id=None,
        action_instance_id="action-1",
        policy_version="runbook-v3",
    )
    fields.update(overrides)
    return fields


def _attested_fields(**overrides: object) -> dict[str, object]:
    fields = _base_fields(
        verification_method="verifier_attested",
        signer_key_id=None,
        signature=None,
        approval_verifier_id=_PROVIDER_VERIFIER_ID,
        verifier_attestation={"approved": True},
    )
    fields.update(overrides)
    return fields


def _evidence(**overrides: object) -> ApprovalEvidence:
    return ApprovalEvidence(**_base_fields(**overrides))  # type: ignore[arg-type]


def _canonical_dict(evidence: ApprovalEvidence) -> dict[str, Any]:
    """Independent of `approval.py`'s own (private) dict builder, matching
    how `test_arc_attestation.py` builds its own envelope dict rather than
    importing the module's internals."""

    def _uuid_str(value: uuid.UUID | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "evidence_type": evidence.evidence_type,
        "scope_kind": evidence.scope_kind,
        "scope_tenant_id": _uuid_str(evidence.scope_tenant_id),
        "approved_artifact_id": _uuid_str(evidence.approved_artifact_id),
        "approved_revision_id": _uuid_str(evidence.approved_revision_id),
        "approved_exception_id": _uuid_str(evidence.approved_exception_id),
        "approved_payload_digest": evidence.approved_payload_digest,
        "approving_principal": evidence.approving_principal,
        "approving_role": evidence.approving_role,
        "source_system_approval_locator": evidence.source_system_approval_locator,
        "approval_timestamp": evidence.approval_timestamp,
        "expires_at": evidence.expires_at,
        "policy_version": evidence.policy_version,
        "action_instance_id": evidence.action_instance_id,
        "verification_method": evidence.verification_method,
        "signer_key_id": evidence.signer_key_id,
        "approval_verifier_id": evidence.approval_verifier_id,
        "verifier_attestation": evidence.verifier_attestation,
        "verifier_identity": evidence.verifier_identity,
        "audit_log_reference": evidence.audit_log_reference,
    }


def _sign(private_raw: bytes, evidence: ApprovalEvidence) -> str:
    canonical = canonicalize_approval_evidence(_canonical_dict(evidence))
    signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(_SIGNING_DOMAIN + canonical)
    return base64.b64encode(signature).decode("ascii")


def _signed(private_raw: bytes, fields: dict[str, object]) -> ApprovalEvidence:
    unsigned = ApprovalEvidence(**{**fields, "signature": None})  # type: ignore[arg-type]
    return dataclasses.replace(unsigned, signature=_sign(private_raw, unsigned))


# --- verifier construction -----------------------------------------------------


def _operator_verifier(public_raw: bytes, **overrides: object) -> ApprovalVerifierRecord:
    base: dict[str, object] = {
        "approval_verifier_id": _SIGNER_KEY_ID,
        "verifier_kind": "operator_public_key",
        "allowed_evidence_types": _ALL_EVIDENCE_TYPES,
        "scope_kind": "tenant",
        "scope_tenant_id": _TENANT_ID,
        "valid_from": _NOW - datetime.timedelta(days=1),
        "valid_to": None,
        "revoked_at": None,
        "algorithm": "Ed25519",
        "public_key": public_raw,
        "provider_id": None,
    }
    base.update(overrides)
    return ApprovalVerifierRecord(**base)  # type: ignore[arg-type]


def _provider_verifier(**overrides: object) -> ApprovalVerifierRecord:
    base: dict[str, object] = {
        "approval_verifier_id": _PROVIDER_VERIFIER_ID,
        "verifier_kind": "trusted_attestation_provider",
        "allowed_evidence_types": _ALL_EVIDENCE_TYPES,
        "scope_kind": "tenant",
        "scope_tenant_id": _TENANT_ID,
        "valid_from": _NOW - datetime.timedelta(days=1),
        "valid_to": None,
        "revoked_at": None,
        "algorithm": None,
        "public_key": None,
        "provider_id": _PROVIDER_ID,
    }
    base.update(overrides)
    return ApprovalVerifierRecord(**base)  # type: ignore[arg-type]


def _accepting_provider(*, canonical_evidence: bytes, verifier_attestation: Any) -> bool:
    return bool(verifier_attestation.get("approved") is True)


def _rejecting_provider(*, canonical_evidence: bytes, verifier_attestation: Any) -> bool:
    return False


# --- fakes and service wiring --------------------------------------------------


class _FakeVerifierLookup:
    """Ignores the session it is handed, matching `_FakeKeyLookup` in
    `test_arc_attestation.py`: the row lock is what the real implementation
    adds, proven elsewhere against a live database, not simulated here."""

    def __init__(self, verifiers: dict[str, ApprovalVerifierRecord]) -> None:
        self._verifiers = verifiers

    async def get(self, session: object, verifier_id: str) -> ApprovalVerifierRecord | None:
        return self._verifiers.get(verifier_id)


class _FakeEvidenceRevocationLookup:
    def __init__(self, revocations: dict[uuid.UUID, datetime.datetime] | None = None) -> None:
        self._revocations = revocations or {}

    async def get(self, session: object, evidence_id: uuid.UUID) -> datetime.datetime | None:
        return self._revocations.get(evidence_id)


def _service(
    verifiers: dict[str, ApprovalVerifierRecord],
    *,
    revocations: dict[uuid.UUID, datetime.datetime] | None = None,
    providers: dict[str, Any] | None = None,
) -> ApprovalEvidenceVerifier:
    return ApprovalEvidenceVerifier(
        _FakeVerifierLookup(verifiers),
        _FakeEvidenceRevocationLookup(revocations),
        attestation_providers=providers,
    )


async def _verify(
    service: ApprovalEvidenceVerifier,
    evidence: ApprovalEvidence,
    *,
    evidence_id: uuid.UUID = _EVIDENCE_ID,
    as_of: datetime.datetime = _NOW,
) -> VerifiedApprovalEvidence:
    # The fake lookups ignore the session; None keeps these tests free of a
    # database they do not need.
    return await service.verify(None, evidence_id=evidence_id, evidence=evidence, as_of=as_of)  # type: ignore[arg-type]


# --- happy path -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_correctly_signed_artifact_activation_verifies() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    verified = await _verify(service, evidence)

    assert verified.evidence_type == "artifact_activation"
    assert verified.approved_artifact_id == _ARTIFACT_ID
    assert verified.approved_revision_id == _REVISION_ID
    assert verified.verification_method == "operator_signed"
    assert verified.verifier_id == _SIGNER_KEY_ID
    assert verified.audit_log_reference == "audit-1"


@pytest.mark.asyncio
async def test_a_correctly_signed_exception_approval_verifies() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _exception_fields())
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    verified = await _verify(service, evidence)

    assert verified.evidence_type == "exception_approval"
    assert verified.approved_exception_id == _EXCEPTION_ID
    assert verified.approved_artifact_id is None


@pytest.mark.asyncio
async def test_a_correctly_signed_gateway_emergency_bypass_verifies() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _bypass_fields())
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    verified = await _verify(service, evidence)

    assert verified.evidence_type == "gateway_emergency_bypass"
    assert verified.action_instance_id == "action-1"
    assert verified.policy_version == "runbook-v3"


@pytest.mark.asyncio
async def test_a_global_verifier_covers_tenant_scoped_evidence() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, scope_kind="global", scope_tenant_id=None)
    service = _service({_SIGNER_KEY_ID: verifier})

    verified = await _verify(service, evidence)
    assert verified.scope_tenant_id == _TENANT_ID


@pytest.mark.asyncio
async def test_a_tenant_verifier_covers_a_domain_scoped_evidence_in_the_same_tenant() -> None:
    """The verifier's own scope is only global-or-tenant; a tenant verifier
    still covers finer-grained (domain/capability/task) evidence as long as
    the tenant matches, since those finer kinds are still within that tenant."""
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields(scope_kind="domain", scope_tenant_id=_TENANT_ID))
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    verified = await _verify(service, evidence)
    assert verified.scope_kind == "domain"


@pytest.mark.asyncio
async def test_verifier_attested_evidence_verifies_via_the_registered_provider() -> None:
    evidence = ApprovalEvidence(**_attested_fields())  # type: ignore[arg-type]
    service = _service(
        {_PROVIDER_VERIFIER_ID: _provider_verifier()},
        providers={_PROVIDER_ID: _accepting_provider},
    )

    verified = await _verify(service, evidence)
    assert verified.verification_method == "verifier_attested"
    assert verified.verifier_id == _PROVIDER_VERIFIER_ID


# --- schema: evidence_type / verification_method / scope ----------------------


@pytest.mark.asyncio
async def test_unknown_evidence_type_is_rejected() -> None:
    evidence = _evidence(evidence_type="something_else")
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="unknown evidence_type"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_unknown_verification_method_is_rejected() -> None:
    evidence = _evidence(verification_method="carrier_pigeon")
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="unknown verification_method"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_unknown_scope_kind_is_rejected() -> None:
    evidence = _evidence(scope_kind="galaxy")
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="unknown scope_kind"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_global_scope_evidence_with_a_tenant_id_is_rejected() -> None:
    evidence = _evidence(scope_kind="global", scope_tenant_id=_TENANT_ID)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="global-scope evidence must not carry"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_tenant_scope_evidence_without_a_tenant_id_is_rejected() -> None:
    evidence = _evidence(scope_kind="tenant", scope_tenant_id=None)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires a scope_tenant_id"):
        await _verify(service, evidence)


# --- schema: discriminated target shape ----------------------------------------


@pytest.mark.asyncio
async def test_artifact_activation_missing_revision_id_is_rejected() -> None:
    evidence = _evidence(approved_revision_id=None)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires both approved_artifact_id"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_artifact_activation_carrying_bypass_fields_is_rejected() -> None:
    evidence = _evidence(action_instance_id="action-1")
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="must not carry exception or bypass"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_exception_approval_missing_exception_id_is_rejected() -> None:
    evidence = ApprovalEvidence(**_exception_fields(approved_exception_id=None))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires approved_exception_id"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_exception_approval_carrying_activation_fields_is_rejected() -> None:
    evidence = ApprovalEvidence(**_exception_fields(approved_artifact_id=_ARTIFACT_ID))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="must not carry activation or bypass"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_gateway_emergency_bypass_missing_policy_version_is_rejected() -> None:
    evidence = ApprovalEvidence(**_bypass_fields(policy_version=None))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires both action_instance_id"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_gateway_emergency_bypass_carrying_exception_fields_is_rejected() -> None:
    evidence = ApprovalEvidence(**_bypass_fields(approved_exception_id=_EXCEPTION_ID))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="must not carry activation or exception"):
        await _verify(service, evidence)


# --- schema: discriminated representation shape --------------------------------


@pytest.mark.asyncio
async def test_operator_signed_evidence_missing_signature_is_rejected() -> None:
    evidence = _evidence(signature=None)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires both signer_key_id and signature"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_operator_signed_evidence_carrying_verifier_attestation_is_rejected() -> None:
    private_raw, _ = _keypair()
    evidence = _signed(private_raw, _base_fields())
    evidence = dataclasses.replace(evidence, verifier_attestation={"approved": True})
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="must not carry approval_verifier_id"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_verifier_attested_evidence_missing_verifier_attestation_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields(verifier_attestation=None))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="requires both approval_verifier_id"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_verifier_attested_evidence_carrying_a_signature_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields(signature=base64.b64encode(b"x" * 64).decode("ascii")))  # type: ignore[arg-type]
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="must not carry signer_key_id or signature"):
        await _verify(service, evidence)


# --- required fields and timestamps ---------------------------------------------

# A placeholder, never-checked signature: these tests exercise checks that run
# before signature verification, so they only need to get past the
# discriminated-representation-shape check (which requires *some* non-None
# signature), not a genuine one.
_PLACEHOLDER_SIGNATURE = base64.b64encode(b"0" * 64).decode("ascii")


@pytest.mark.asyncio
async def test_an_empty_approving_principal_is_rejected() -> None:
    evidence = _evidence(approving_principal="", signature=_PLACEHOLDER_SIGNATURE)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="approving_principal is required"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_naive_approval_timestamp_is_rejected() -> None:
    evidence = _evidence(approval_timestamp=datetime.datetime(2026, 1, 1, 11, 0), signature=_PLACEHOLDER_SIGNATURE)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="approval_timestamp is a naive datetime"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_naive_expires_at_is_rejected() -> None:
    evidence = _evidence(expires_at=datetime.datetime(2027, 1, 1, 0, 0), signature=_PLACEHOLDER_SIGNATURE)
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="expires_at is a naive datetime"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_naive_as_of_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="as_of is a naive datetime"):
        await _verify(service, evidence, as_of=datetime.datetime(2026, 1, 1, 12, 0))


@pytest.mark.asyncio
async def test_an_expired_review_period_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields(expires_at=_NOW - datetime.timedelta(minutes=1)))
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="review period expired"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_review_period_expiring_exactly_at_as_of_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields(expires_at=_NOW))
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="review period expired"):
        await _verify(service, evidence, as_of=_NOW)


# --- verifier resolution ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_verifier_is_rejected() -> None:
    private_raw, _ = _keypair()
    evidence = _signed(private_raw, _base_fields())
    service = _service({})

    with pytest.raises(ApprovalEvidenceVerificationError, match="no approval verifier registered"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_operator_signed_evidence_against_a_provider_kind_verifier_is_rejected() -> None:
    private_raw, _ = _keypair()
    evidence = _signed(private_raw, _base_fields())
    # Same ID, but registered as a trusted_attestation_provider -- an
    # evidence claiming operator_signed must not borrow that trust.
    mismatched = _provider_verifier(approval_verifier_id=_SIGNER_KEY_ID)
    service = _service({_SIGNER_KEY_ID: mismatched})

    with pytest.raises(ApprovalEvidenceVerificationError, match="is registered as"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_verifier_attested_evidence_against_an_operator_key_verifier_is_rejected() -> None:
    _, public_raw = _keypair()
    evidence = ApprovalEvidence(**_attested_fields(approval_verifier_id=_SIGNER_KEY_ID))  # type: ignore[arg-type]
    mismatched = _operator_verifier(public_raw, approval_verifier_id=_SIGNER_KEY_ID)
    service = _service({_SIGNER_KEY_ID: mismatched})

    with pytest.raises(ApprovalEvidenceVerificationError, match="is registered as"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_verifier_not_allowed_for_this_evidence_type_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, allowed_evidence_types=frozenset({"gateway_emergency_bypass"}))
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="not approved for"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_verifier_scoped_to_a_different_tenant_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, scope_tenant_id=_OTHER_TENANT_ID)
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="scoped to a different tenant"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_verifier_not_yet_valid_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, valid_from=_NOW + datetime.timedelta(days=1))
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="expired or revoked"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_an_expired_verifier_window_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, valid_to=_NOW - datetime.timedelta(minutes=1))
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="expired or revoked"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_revoked_verifier_key_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, revoked_at=_NOW - datetime.timedelta(minutes=1))
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="expired or revoked"):
        await _verify(service, evidence)


# --- evidence-level revocation ---------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_evidence_is_rejected_even_though_its_verifier_is_fine() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    service = _service(
        {_SIGNER_KEY_ID: _operator_verifier(public_raw)},
        revocations={_EVIDENCE_ID: _NOW - datetime.timedelta(days=1)},
    )

    with pytest.raises(ApprovalEvidenceVerificationError, match="was revoked at"):
        await _verify(service, evidence)


# --- operator-signed cryptography ------------------------------------------------


@pytest.mark.asyncio
async def test_a_tampered_field_fails_signature_verification() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    # Mutate after signing: the signature covers the original bytes.
    tampered = dataclasses.replace(evidence, approving_role="superadmin")
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="signature did not verify"):
        await _verify(service, tampered)


@pytest.mark.asyncio
async def test_signed_by_a_different_key_fails_verification() -> None:
    signer_private, _ = _keypair()
    _, registered_public = _keypair()  # a different keypair than the signer used
    evidence = _signed(signer_private, _base_fields())
    service = _service({_SIGNER_KEY_ID: _operator_verifier(registered_public)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="signature did not verify"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_malformed_base64_signature_is_rejected() -> None:
    _, public_raw = _keypair()
    evidence = _evidence(signature="not-valid-base64!!!")
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="signature is not valid base64"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_verifier_using_a_non_ed25519_algorithm_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(public_raw, algorithm="RSA-2048")
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="unsupported algorithm"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_an_operator_verifier_missing_a_public_key_is_rejected() -> None:
    private_raw, _ = _keypair()
    evidence = _signed(private_raw, _base_fields())
    verifier = _operator_verifier(b"", public_key=None)
    service = _service({_SIGNER_KEY_ID: verifier})

    with pytest.raises(ApprovalEvidenceVerificationError, match="has no public key recorded"):
        await _verify(service, evidence)


# --- verifier-attested provider ---------------------------------------------------


@pytest.mark.asyncio
async def test_no_registered_provider_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields())  # type: ignore[arg-type]
    service = _service({_PROVIDER_VERIFIER_ID: _provider_verifier()}, providers={})

    with pytest.raises(ApprovalEvidenceVerificationError, match="no in-process attestation provider registered"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_provider_that_rejects_the_attestation_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields())  # type: ignore[arg-type]
    service = _service(
        {_PROVIDER_VERIFIER_ID: _provider_verifier()},
        providers={_PROVIDER_ID: _rejecting_provider},
    )

    with pytest.raises(ApprovalEvidenceVerificationError, match="did not validate the attestation"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_provider_verifier_missing_a_provider_id_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields())  # type: ignore[arg-type]
    verifier = _provider_verifier(provider_id=None)
    service = _service({_PROVIDER_VERIFIER_ID: verifier}, providers={_PROVIDER_ID: _accepting_provider})

    with pytest.raises(ApprovalEvidenceVerificationError, match="has no provider_id recorded"):
        await _verify(service, evidence)


# --- canonicalization -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_nfc_field_makes_the_evidence_uncanonicalizable() -> None:
    """A missing or malformed field makes the evidence uncanonicalizable, so
    there is no "correctly signed" version of it to construct -- signing
    itself requires canonicalizing first. The signature value is therefore
    irrelevant here: verification must fail before it ever reaches signature
    checking, matching `test_arc_attestation.py`'s equivalent case."""
    _, public_raw = _keypair()
    evidence = _evidence(
        approving_principal="Sébastien",  # "e" + combining acute, not NFC
        signature=base64.b64encode(b"0" * 64).decode("ascii"),
    )
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    with pytest.raises(ApprovalEvidenceVerificationError, match="does not canonicalize"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_non_json_value_inside_verifier_attestation_is_rejected() -> None:
    evidence = ApprovalEvidence(**_attested_fields(verifier_attestation={"ref": uuid.uuid4()}))  # type: ignore[arg-type]
    service = _service(
        {_PROVIDER_VERIFIER_ID: _provider_verifier()},
        providers={_PROVIDER_ID: _accepting_provider},
    )

    with pytest.raises(ApprovalEvidenceVerificationError, match="does not canonicalize"):
        await _verify(service, evidence)


# --- scope reach: the escalation path a judge review found ------------------------


@pytest.mark.asyncio
async def test_a_tenant_verifier_with_no_tenant_is_refused() -> None:
    """The forged-approval path that motivated an explicit check.

    Rejecting a tenant verifier for global evidence used to rest entirely on
    `evidence.scope_tenant_id != verifier.scope_tenant_id`. Global evidence
    has no tenant, so a tenant-scoped verifier whose own tenant were also
    NULL would compare `None != None` -- False -- and vouch for
    deployment-wide governance. Tenant verifiers are registrable by a tenant
    admin, so that is a path from "admin of any tenant" to "approves global
    policy".
    """
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, {**_base_fields(), "scope_kind": "global", "scope_tenant_id": None})
    service = _service(
        {_SIGNER_KEY_ID: _operator_verifier(public_raw, scope_kind="tenant", scope_tenant_id=None)}
    )

    with pytest.raises(ApprovalEvidenceVerificationError, match="names no tenant"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_tenant_verifier_cannot_vouch_for_global_evidence() -> None:
    """Scope reaches downward only. Upward reach would let a tenant approve
    governance that binds every other tenant."""
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, {**_base_fields(), "scope_kind": "global", "scope_tenant_id": None})
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw, scope_kind="tenant")})

    with pytest.raises(ApprovalEvidenceVerificationError, match="cannot vouch for"):
        await _verify(service, evidence)


@pytest.mark.asyncio
async def test_a_global_verifier_may_vouch_for_one_tenants_evidence() -> None:
    """The other direction is fine: a global verifier is registrable only by
    the deployment operator, who is already ARC's root of trust. Restricting
    it would be theatre -- that operator could simply register a verifier in
    the target tenant instead."""
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, _base_fields())
    service = _service(
        {_SIGNER_KEY_ID: _operator_verifier(public_raw, scope_kind="global", scope_tenant_id=None)}
    )

    assert await _verify(service, evidence)


@pytest.mark.asyncio
async def test_policy_version_does_not_make_an_activation_look_like_a_bypass() -> None:
    """`policy_version` names the runbook the approver followed, not the
    thing approved -- so an activation carrying it is not ambiguous, and the
    documented request schema emits it unqualified. Closure covers
    target-identity fields only.
    """
    private_raw, public_raw = _keypair()
    evidence = _signed(private_raw, {**_base_fields(), "policy_version": "governance-v4"})
    service = _service({_SIGNER_KEY_ID: _operator_verifier(public_raw)})

    assert await _verify(service, evidence)
