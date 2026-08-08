"""Parametrized SQL for `arc_approval_verifier_enrollment_challenges` and the
principal-binding columns of `arc_approval_verifiers`.

`enrollment.py` owns the challenge/proof protocol shape, canonical-object
construction, and signature/attestation verification; this module owns
getting rows in and out of the two tables. Every function takes an
already-open `AsyncSession` -- none of them opens its own transaction --
matching `queries/operational_chain.py`'s and `queries/source_admission.py`'s
own convention.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class ChallengeRow:
    """One `arc_approval_verifier_enrollment_challenges` row."""

    enrollment_challenge_id: uuid.UUID
    verifier_id: str
    nonce: str
    binding_kind: str
    principal_issuer: str | None
    principal_subject: str | None
    provider_id: str | None
    provider_allowed_principal_issuer: str | None
    owning_scope: str
    target_tenant_id: uuid.UUID | None
    allowed_evidence_types: tuple[str, ...]
    signature_algorithm: str
    credential_material: bytes
    canonical_enrollment_bytes: bytes
    valid_from: datetime.datetime
    valid_to: datetime.datetime
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    consumed_at: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class VerifierRow:
    """One `arc_approval_verifiers` row, principal-binding columns included."""

    approval_verifier_id: str
    verifier_kind: str
    allowed_evidence_types: tuple[str, ...]
    scope_kind: str
    scope_tenant_id: uuid.UUID | None
    provider_id: str | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    revoked_at: datetime.datetime | None
    created_at: datetime.datetime
    principal_binding_kind: str | None
    principal_issuer: str | None
    principal_subject: str | None
    provider_allowed_principal_issuer: str | None
    credential_fingerprint: str | None
    provider_configuration_digest: str | None
    enrollment_challenge_id: uuid.UUID | None
    enrollment_verified_at: datetime.datetime | None


_CHALLENGE_COLUMNS = (
    "enrollment_challenge_id, verifier_id, nonce, binding_kind, principal_issuer, principal_subject, "
    "provider_id, provider_allowed_principal_issuer, owning_scope, target_tenant_id, "
    "allowed_evidence_types, signature_algorithm, credential_material, canonical_enrollment_bytes, "
    "valid_from, valid_to, issued_at, expires_at, consumed_at"
)


def _challenge_row(row: object) -> ChallengeRow:
    return ChallengeRow(
        enrollment_challenge_id=row.enrollment_challenge_id,  # type: ignore[attr-defined]
        verifier_id=row.verifier_id,  # type: ignore[attr-defined]
        nonce=row.nonce,  # type: ignore[attr-defined]
        binding_kind=row.binding_kind,  # type: ignore[attr-defined]
        principal_issuer=row.principal_issuer,  # type: ignore[attr-defined]
        principal_subject=row.principal_subject,  # type: ignore[attr-defined]
        provider_id=row.provider_id,  # type: ignore[attr-defined]
        provider_allowed_principal_issuer=row.provider_allowed_principal_issuer,  # type: ignore[attr-defined]
        owning_scope=row.owning_scope,  # type: ignore[attr-defined]
        target_tenant_id=row.target_tenant_id,  # type: ignore[attr-defined]
        allowed_evidence_types=tuple(row.allowed_evidence_types or ()),  # type: ignore[attr-defined]
        signature_algorithm=row.signature_algorithm,  # type: ignore[attr-defined]
        credential_material=bytes(row.credential_material),  # type: ignore[attr-defined]
        canonical_enrollment_bytes=bytes(row.canonical_enrollment_bytes),  # type: ignore[attr-defined]
        valid_from=row.valid_from,  # type: ignore[attr-defined]
        valid_to=row.valid_to,  # type: ignore[attr-defined]
        issued_at=row.issued_at,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        consumed_at=row.consumed_at,  # type: ignore[attr-defined]
    )


async def insert_challenge(
    session: AsyncSession,
    *,
    enrollment_challenge_id: uuid.UUID,
    verifier_id: str,
    nonce: str,
    binding_kind: str,
    principal_issuer: str | None,
    principal_subject: str | None,
    provider_id: str | None,
    provider_allowed_principal_issuer: str | None,
    owning_scope: str,
    target_tenant_id: uuid.UUID | None,
    allowed_evidence_types: list[str],
    signature_algorithm: str,
    credential_material: bytes,
    canonical_enrollment_bytes: bytes,
    valid_from: datetime.datetime,
    valid_to: datetime.datetime,
    issued_at: datetime.datetime,
    expires_at: datetime.datetime,
    created_by_issuer: str,
    created_by_subject: str,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifier_enrollment_challenges ("
            "  enrollment_challenge_id, verifier_id, nonce, binding_kind, principal_issuer, principal_subject,"
            "  provider_id, provider_allowed_principal_issuer, owning_scope, target_tenant_id,"
            "  allowed_evidence_types, signature_algorithm, credential_material, canonical_enrollment_bytes,"
            "  valid_from, valid_to, issued_at, expires_at, created_by_issuer, created_by_subject, created_at"
            ") VALUES ("
            "  :enrollment_challenge_id, :verifier_id, :nonce, :binding_kind, :principal_issuer, :principal_subject,"
            "  :provider_id, :provider_allowed_principal_issuer, :owning_scope, :target_tenant_id,"
            "  CAST(:allowed_evidence_types AS TEXT[]), :signature_algorithm, :credential_material,"
            "  :canonical_enrollment_bytes, :valid_from, :valid_to, :issued_at, :expires_at,"
            "  :created_by_issuer, :created_by_subject, :created_at"
            ")"
        ),
        {
            "enrollment_challenge_id": enrollment_challenge_id,
            "verifier_id": verifier_id,
            "nonce": nonce,
            "binding_kind": binding_kind,
            "principal_issuer": principal_issuer,
            "principal_subject": principal_subject,
            "provider_id": provider_id,
            "provider_allowed_principal_issuer": provider_allowed_principal_issuer,
            "owning_scope": owning_scope,
            "target_tenant_id": target_tenant_id,
            "allowed_evidence_types": allowed_evidence_types,
            "signature_algorithm": signature_algorithm,
            "credential_material": credential_material,
            "canonical_enrollment_bytes": canonical_enrollment_bytes,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "created_by_issuer": created_by_issuer,
            "created_by_subject": created_by_subject,
            "created_at": created_at,
        },
    )


async def load_challenge(session: AsyncSession, enrollment_challenge_id: uuid.UUID) -> ChallengeRow | None:
    """Unlocked read -- for issuance's own idempotent re-read, never for
    completion, which must lock the row (see `lock_challenge`)."""
    row = (
        await session.execute(
            text(
                f"SELECT {_CHALLENGE_COLUMNS} FROM arc_approval_verifier_enrollment_challenges "  # noqa: S608 - _CHALLENGE_COLUMNS is a module constant, not caller input
                "WHERE enrollment_challenge_id = :cid"
            ),
            {"cid": enrollment_challenge_id},
        )
    ).one_or_none()
    return None if row is None else _challenge_row(row)


async def lock_challenge(session: AsyncSession, enrollment_challenge_id: uuid.UUID) -> ChallengeRow | None:
    """`SELECT ... FOR UPDATE` -- held for the rest of the caller's
    transaction so the single-use compare-and-swap below and the eventual
    `arc_approval_verifiers` insert are never interleaved with a second
    completion attempt for the same challenge."""
    row = (
        await session.execute(
            text(
                f"SELECT {_CHALLENGE_COLUMNS} FROM arc_approval_verifier_enrollment_challenges "  # noqa: S608 - _CHALLENGE_COLUMNS is a module constant, not caller input
                "WHERE enrollment_challenge_id = :cid FOR UPDATE"
            ),
            {"cid": enrollment_challenge_id},
        )
    ).one_or_none()
    return None if row is None else _challenge_row(row)


async def consume_challenge(
    session: AsyncSession, enrollment_challenge_id: uuid.UUID, *, consumed_at: datetime.datetime
) -> int:
    """`UPDATE ... WHERE consumed_at IS NULL` -- the single-use guarantee.

    Returns the affected row count; the caller requires exactly 1 and treats
    anything else as a losing race, matching Appendix B.2's stated
    enforcement mechanism for this exact rule.
    """
    result = await session.execute(
        text(
            "UPDATE arc_approval_verifier_enrollment_challenges SET consumed_at = :consumed_at "
            "WHERE enrollment_challenge_id = :cid AND consumed_at IS NULL"
        ),
        {"cid": enrollment_challenge_id, "consumed_at": consumed_at},
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def insert_verifier(
    session: AsyncSession,
    *,
    approval_verifier_id: str,
    verifier_kind: str,
    allowed_evidence_types: list[str],
    scope_kind: str,
    scope_tenant_id: uuid.UUID | None,
    algorithm: str | None,
    public_key: bytes | None,
    provider_id: str | None,
    valid_from: datetime.datetime,
    valid_to: datetime.datetime,
    created_at: datetime.datetime,
    principal_binding_kind: str,
    principal_issuer: str | None,
    principal_subject: str | None,
    provider_allowed_principal_issuer: str | None,
    credential_fingerprint: str | None,
    provider_configuration_digest: str | None,
    enrollment_challenge_id: uuid.UUID,
    enrollment_verified_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_approval_verifiers ("
            "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
            "  algorithm, public_key, provider_id, valid_from, valid_to, created_at,"
            "  principal_binding_kind, principal_issuer, principal_subject, provider_allowed_principal_issuer,"
            "  credential_fingerprint, provider_configuration_digest, enrollment_challenge_id,"
            "  enrollment_verified_at"
            ") VALUES ("
            "  :approval_verifier_id, :verifier_kind, CAST(:allowed_evidence_types AS TEXT[]), :scope_kind,"
            "  :scope_tenant_id, :algorithm, :public_key, :provider_id, :valid_from, :valid_to, :created_at,"
            "  :principal_binding_kind, :principal_issuer, :principal_subject, :provider_allowed_principal_issuer,"
            "  :credential_fingerprint, :provider_configuration_digest, :enrollment_challenge_id,"
            "  :enrollment_verified_at"
            ")"
        ),
        {
            "approval_verifier_id": approval_verifier_id,
            "verifier_kind": verifier_kind,
            "allowed_evidence_types": allowed_evidence_types,
            "scope_kind": scope_kind,
            "scope_tenant_id": scope_tenant_id,
            "algorithm": algorithm,
            "public_key": public_key,
            "provider_id": provider_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "created_at": created_at,
            "principal_binding_kind": principal_binding_kind,
            "principal_issuer": principal_issuer,
            "principal_subject": principal_subject,
            "provider_allowed_principal_issuer": provider_allowed_principal_issuer,
            "credential_fingerprint": credential_fingerprint,
            "provider_configuration_digest": provider_configuration_digest,
            "enrollment_challenge_id": enrollment_challenge_id,
            "enrollment_verified_at": enrollment_verified_at,
        },
    )


async def load_verifier(session: AsyncSession, approval_verifier_id: str) -> VerifierRow | None:
    row = (
        await session.execute(
            text(
                "SELECT approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind, scope_tenant_id,"
                "       provider_id, valid_from, valid_to, revoked_at, created_at, principal_binding_kind,"
                "       principal_issuer, principal_subject, provider_allowed_principal_issuer,"
                "       credential_fingerprint, provider_configuration_digest, enrollment_challenge_id,"
                "       enrollment_verified_at "
                "FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"
            ),
            {"vid": approval_verifier_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return VerifierRow(
        approval_verifier_id=row.approval_verifier_id,
        verifier_kind=row.verifier_kind,
        allowed_evidence_types=tuple(row.allowed_evidence_types or ()),
        scope_kind=row.scope_kind,
        scope_tenant_id=row.scope_tenant_id,
        provider_id=row.provider_id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        principal_binding_kind=row.principal_binding_kind,
        principal_issuer=row.principal_issuer,
        principal_subject=row.principal_subject,
        provider_allowed_principal_issuer=row.provider_allowed_principal_issuer,
        credential_fingerprint=row.credential_fingerprint,
        provider_configuration_digest=row.provider_configuration_digest,
        enrollment_challenge_id=row.enrollment_challenge_id,
        enrollment_verified_at=row.enrollment_verified_at,
    )


__all__ = [
    "ChallengeRow",
    "VerifierRow",
    "consume_challenge",
    "insert_challenge",
    "insert_verifier",
    "load_challenge",
    "load_verifier",
    "lock_challenge",
]
