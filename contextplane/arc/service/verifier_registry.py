"""The approval-verifier trust root: registration, lookup, and revocation reads.

ARC's approval chain had a hole shaped like a missing table writer. Every check
in `ApprovalEvidenceVerifier` -- signature, verifier kind, permitted evidence
types, scope reach, trust window -- reads an `arc_approval_verifiers` row, and
nothing in the product could create one. The chain was not weak; it was
unreachable, and activation fell back to checks that a direct SQL INSERT
satisfied as easily as it satisfied `lifecycle_state = 'active'`.

**Why registration is an operator action, not a tenant one.** Registering a
verifier decides *who counts as an approver*. Its blast radius is every
activation and exception that verifier will ever vouch for, which is the same
blast radius as revoking one -- already an operator-gated action. So it takes the
same gate: an exact `(issuer, subject)` pair from the deployment allowlist, which
is configuration outside the database and ungrantable by any tenant.

**Why that is not a backdoor.** The allowlist identity decides who may be
admitted as an approver; it cannot forge an approval. Registration records a
*public* key or a provider id -- the private key stays with the approver -- so
registrar and signer are separated by construction. An operator who registers a
key they also hold is both, and no check at this layer can prevent that. What
this module does instead is make it reconstructible: registration audits the key
fingerprint alongside the allowlist fingerprint, so an auditor can later prove
which configuration admitted which verifier.

**Why `allowed_evidence_types` must be narrow.** The permitted-types check and
the evidence-target closure both exist to stop one kind of approval being
presented as another -- an artifact activation filed as an exception approval,
say. A verifier registered for all four types makes both worthless on the day it
is created, which is exactly when a bootstrap is most tempting to wave through.
Registration therefore refuses the full set.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.approval import ApprovalVerifierRecord
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.exceptions import ConflictError, ValidationError
from contextplane.types import Clock

# The two verifier kinds the schema permits, and what each must carry.
KIND_OPERATOR_KEY = "operator_public_key"
KIND_PROVIDER = "trusted_attestation_provider"

# The evidence types a verifier may be permitted. Registering a verifier for
# every one of them defeats the permitted-types check, so the whole set is
# refused -- see the module docstring.
EVIDENCE_TYPES = frozenset(
    {"artifact_activation", "exception_approval", "global_exception_approval", "gateway_emergency_bypass"}
)

# Ed25519 is the only signature algorithm ARC verifies. Recorded rather than
# accepted freely: a verifier registered with an algorithm nothing can check
# would pass registration and then fail every verification, which reads as a
# broken approver rather than a rejected registration.
SUPPORTED_ALGORITHMS = frozenset({"Ed25519"})

_ED25519_PUBLIC_KEY_BYTES = 32


class VerifierRegistry:
    """Writes and reads `arc_approval_verifiers`.

    Registration and lookup live together because they are one table's
    contract. Splitting them invites a second reader that forgets the
    `FOR SHARE` lock the verification path depends on.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    # -- registration ---------------------------------------------------------

    async def register(
        self,
        ctx: ArcRequestContext,
        *,
        approval_verifier_id: str,
        verifier_kind: str,
        allowed_evidence_types: frozenset[str],
        scope_kind: str,
        scope_tenant_id: uuid.UUID | None = None,
        algorithm: str | None = None,
        public_key: bytes | None = None,
        provider_id: str | None = None,
        valid_from: datetime.datetime | None = None,
        valid_to: datetime.datetime | None = None,
        allowlist_fingerprint: str = "",
    ) -> str:
        """Admit a verifier as a trust root, or refuse.

        Authorization is the caller's -- the route holds the operator gate,
        and re-checking it here against a differently-derived identity would
        be a second place for the two to disagree. What this method owns is
        the shape: an unusable verifier registered successfully is worse than
        a refused one, because it fails later, at an approval, and looks like
        the approver's problem.
        """
        self._validate(
            verifier_kind=verifier_kind,
            allowed_evidence_types=allowed_evidence_types,
            scope_kind=scope_kind,
            scope_tenant_id=scope_tenant_id,
            algorithm=algorithm,
            public_key=public_key,
            provider_id=provider_id,
            valid_to=valid_to,
        )

        now = self._clock.now()
        effective_from = valid_from if valid_from is not None else now

        async with self._session_factory() as session, session.begin():
            existing = (
                await session.execute(
                    text(
                        "SELECT approval_verifier_id FROM arc_approval_verifiers " "WHERE approval_verifier_id = :vid"
                    ),
                    {"vid": approval_verifier_id},
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Never an update. A verifier id already names a trust root;
                # rebinding it to a different key would silently re-point every
                # approval that ever cited it, including ones already relied on.
                msg = f"approval verifier {approval_verifier_id!r} is already registered"
                raise ConflictError(msg)

            await session.execute(
                text(
                    "INSERT INTO arc_approval_verifiers ("
                    "  approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                    "  scope_tenant_id, algorithm, public_key, provider_id, valid_from, valid_to"
                    ") VALUES (:vid, :kind, CAST(:types AS TEXT[]), :scope_kind, :scope_tenant,"
                    "          :algorithm, :public_key, :provider_id, :vfrom, :vto)"
                ),
                {
                    "vid": approval_verifier_id,
                    "kind": verifier_kind,
                    "types": sorted(allowed_evidence_types),
                    "scope_kind": scope_kind,
                    "scope_tenant": scope_tenant_id,
                    "algorithm": algorithm,
                    "public_key": public_key,
                    "provider_id": provider_id,
                    "vfrom": effective_from,
                    "vto": valid_to,
                },
            )

            # Global scope, so the audit row has no owning tenant. The
            # fingerprints are the point: neither the key nor the allowlist is
            # recorded, but an auditor can later prove which configuration
            # admitted which verifier.
            await audit_outbox.emit_global(
                session,
                event_type=actions.ARC_APPROVAL_VERIFIER_REGISTERED,
                payload={
                    "approval_verifier_id": approval_verifier_id,
                    "verifier_kind": verifier_kind,
                    "allowed_evidence_types": sorted(allowed_evidence_types),
                    "scope_kind": scope_kind,
                    "scope_tenant_id": str(scope_tenant_id) if scope_tenant_id else None,
                    "credential_fingerprint": _credential_fingerprint(public_key, provider_id),
                    "allowlist_fingerprint": allowlist_fingerprint,
                    "registered_by": ctx.oidc_subject,
                    "valid_from": effective_from.isoformat(),
                    "valid_to": valid_to.isoformat() if valid_to else None,
                },
            )

        return approval_verifier_id

    def _validate(
        self,
        *,
        verifier_kind: str,
        allowed_evidence_types: frozenset[str],
        scope_kind: str,
        scope_tenant_id: uuid.UUID | None,
        algorithm: str | None,
        public_key: bytes | None,
        provider_id: str | None,
        valid_to: datetime.datetime | None,
    ) -> None:
        """Refuse a verifier that could be stored but not used.

        The schema already enforces most of this. Checking here as well is
        not redundant: a CHECK violation surfaces as an integrity error naming
        a constraint, which tells an operator nothing about which field they
        got wrong.
        """
        if verifier_kind not in {KIND_OPERATOR_KEY, KIND_PROVIDER}:
            msg = f"unknown verifier kind {verifier_kind!r}"
            raise ValidationError(msg)

        unknown = allowed_evidence_types - EVIDENCE_TYPES
        if unknown:
            msg = f"unknown evidence types: {sorted(unknown)}"
            raise ValidationError(msg)
        if not allowed_evidence_types:
            msg = "a verifier permitted for no evidence type can never approve anything"
            raise ValidationError(msg)
        if allowed_evidence_types == EVIDENCE_TYPES:
            # See the module docstring. This is the one refusal here that is
            # policy rather than shape.
            msg = (
                "a verifier may not be permitted for every evidence type; the permitted-types "
                "check exists to stop one kind of approval being presented as another, and a "
                "verifier trusted for all of them defeats it"
            )
            raise ValidationError(msg)

        if scope_kind == "tenant" and scope_tenant_id is None:
            msg = "a tenant-scoped verifier must name its tenant"
            raise ValidationError(msg)
        if scope_kind == "global" and scope_tenant_id is not None:
            msg = "a global verifier must not name a tenant"
            raise ValidationError(msg)
        if scope_kind not in {"global", "tenant"}:
            msg = f"unknown verifier scope {scope_kind!r}"
            raise ValidationError(msg)

        if verifier_kind == KIND_OPERATOR_KEY:
            if algorithm not in SUPPORTED_ALGORITHMS:
                msg = f"unsupported signature algorithm {algorithm!r}; ARC verifies {sorted(SUPPORTED_ALGORITHMS)}"
                raise ValidationError(msg)
            if public_key is None or len(public_key) != _ED25519_PUBLIC_KEY_BYTES:
                msg = f"an Ed25519 public key is {_ED25519_PUBLIC_KEY_BYTES} bytes"
                raise ValidationError(msg)
            if provider_id is not None:
                msg = "an operator-key verifier must not also name a provider"
                raise ValidationError(msg)
        else:
            if not provider_id:
                msg = "a provider-attested verifier must name its provider"
                raise ValidationError(msg)
            if algorithm is not None or public_key is not None:
                msg = "a provider-attested verifier must not also carry a key"
                raise ValidationError(msg)

        if valid_to is not None and valid_to <= self._clock.now():
            msg = "a verifier whose validity has already ended cannot be registered"
            raise ValidationError(msg)

    # -- lookups the verification path needs ----------------------------------

    async def get(self, session: AsyncSession, verifier_id: str) -> ApprovalVerifierRecord | None:
        """Read one verifier inside the caller's transaction, locked `FOR SHARE`.

        The lock is the reason this takes a session rather than opening its
        own. A revocation is a plain `UPDATE`, which takes an implicit
        `FOR UPDATE`; without the share lock held for the duration of the
        caller's transaction, an activation could verify against a verifier
        whose revocation commits a moment later and still be recorded as
        approved.
        """
        row = (
            await session.execute(
                text(
                    "SELECT approval_verifier_id, verifier_kind, allowed_evidence_types, scope_kind,"
                    "       scope_tenant_id, algorithm, public_key, provider_id, valid_from,"
                    "       valid_to, revoked_at "
                    "FROM arc_approval_verifiers WHERE approval_verifier_id = :vid FOR SHARE"
                ),
                {"vid": verifier_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return ApprovalVerifierRecord(
            approval_verifier_id=row.approval_verifier_id,
            verifier_kind=row.verifier_kind,
            allowed_evidence_types=frozenset(row.allowed_evidence_types or ()),
            scope_kind=row.scope_kind,
            scope_tenant_id=row.scope_tenant_id,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            revoked_at=row.revoked_at,
            algorithm=row.algorithm,
            public_key=bytes(row.public_key) if row.public_key is not None else None,
            provider_id=row.provider_id,
        )


class EvidenceRevocationRegistry:
    """Whether one piece of approval evidence has itself been withdrawn.

    Separate from verifier revocation on purpose: revoking a verifier
    invalidates everything it vouched for, while revoking one piece of
    evidence must leave everything else that verifier is still trusted for
    untouched.
    """

    async def get(self, session: AsyncSession, evidence_id: uuid.UUID) -> datetime.datetime | None:
        revoked_at: datetime.datetime | None = (
            await session.execute(
                text("SELECT revoked_at FROM arc_approval_evidence_revocations WHERE evidence_id = :eid"),
                {"eid": evidence_id},
            )
        ).scalar_one_or_none()
        return revoked_at


def _credential_fingerprint(public_key: bytes | None, provider_id: str | None) -> str:
    """A digest of whichever credential this verifier carries.

    The credential itself is never audited. A public key is not secret, but a
    provider id can name internal infrastructure, and an audit trail that
    enumerated either would be a directory of what to attack. The fingerprint
    is enough to prove two registrations used the same credential.
    """
    material = public_key if public_key is not None else (provider_id or "").encode("utf-8")
    return hashlib.sha256(material).hexdigest()


__all__ = [
    "EVIDENCE_TYPES",
    "KIND_OPERATOR_KEY",
    "KIND_PROVIDER",
    "SUPPORTED_ALGORITHMS",
    "EvidenceRevocationRegistry",
    "VerifierRegistry",
]
