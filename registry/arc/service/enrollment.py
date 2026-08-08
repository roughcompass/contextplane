"""Principal-bound approval-verifier enrollment (trust-roots D1).

Closes a specific, previously reported gap: `arc_approval_verifiers` recorded
a public key or a provider id, but registering one was allowlist-operator
membership only -- nothing cryptographically bound the row to a principal
who actually held the credential. A registrant could name any key it liked
for any issuer/subject it liked, and the row would be trusted exactly as
much as one where the registrant genuinely held the private half.

The two-call challenge/proof protocol closes it. `create_challenge` commits
every immutable registration field -- including a server pre-allocated
`verifier_id` and a random `nonce` -- into `canonical_enrollment_bytes`
*before* any `arc_approval_verifiers` row exists, and hands that back to the
caller to sign (or to have a configured provider attest over).
`register_verifier` then verifies that proof against the exact bytes this
service committed to, consumes the challenge exactly once, and only then
writes the row. A registrant who cannot produce a valid signature over
those bytes cannot make the row exist at all.

**Two binding shapes, one verification story.** `exact_principal` is a
detached Ed25519 signature: the caller holds the private key and proves it
directly. `provider_delegated` is a trusted, in-process provider's own
attestation, mirroring the existing `verifier_attested` shape
`ApprovalEvidenceVerifier` already supports for approval evidence (see
`approval.py`'s `VerifierAttestationProvider`) -- `self._attestation_
providers` is empty by default, matching `arc_signing_keys = {}` and every
other not-yet-configured trust-material gate in this deployment, and
`register_verifier` refuses cleanly rather than fabricating trust when no
provider is configured for the named `provider_id`. Unlike
`OperationalChainService`, this module needs no server-held signing key at
all -- enrollment only *verifies* a caller-presented or provider-presented
proof, so there is no analogous "signs with a per-process in-memory key"
gap to inherit here; the one real custody question this module leaves open
is the provider side, and it is answered the same way `approval.py` already
answers it for evidence attestation: an injectable, operator-configured
mapping, empty until an operator configures one.

**What `register_verifier` still cannot represent.** `ApprovalVerifierResponse`
(Appendix A.6) declares `binding_kind`/`principal_issuer`/`principal_subject`
all required, not nullable -- but a `provider_delegated` verifier's
canonical enrollment object legitimately carries `principal_issuer`/
`principal_subject` as `null` (the provider names the exact subject
dynamically, at approval time, not at enrollment time; confirmed against
the checked-in canonical fixture for this exact profile). Since no provider is
configured on any deployment today, `register_verifier`'s `provider_
delegated` branch always refuses before a row -- or a response -- would
ever need to represent that state, so this module does not need to resolve
the tension. It is flagged here, not silently inherited, for whoever
configures the first real provider.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import uuid
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.schemas import authoring_profiles
from registry.arc.schemas.authoring_profile_shapes import APPROVAL_VERIFIER_ENROLLMENT_PROFILE
from registry.arc.schemas.canonical import CanonicalizationError
from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.queries import enrollment as queries
from registry.arc.types import ArcRequestContext
from registry.audit import actions
from registry.exceptions import RegistryError
from registry.types import Clock

# The two closed binding-kind literals (mirrors `PrincipalBindingKind`,
# transcribed rather than imported: `arc/service/` does not import
# `api/schemas/` anywhere else in this codebase).
BINDING_EXACT_PRINCIPAL = "exact_principal"
BINDING_PROVIDER_DELEGATED = "provider_delegated"

# The legacy `arc_approval_verifiers.verifier_kind` vocabulary this module's
# writes must stay consistent with -- see `verifier_registry.py`.
_KIND_OPERATOR_KEY = "operator_public_key"
_KIND_PROVIDER = "trusted_attestation_provider"

# Ed25519 is the only algorithm the enrollment profile's own closed schema
# permits (`authoring_profile_shapes._APPROVAL_VERIFIER_ENROLLMENT_SCHEMA`'s
# `signature_algorithm` enum has exactly one member) -- transcribed here as
# a named constant so a validation message can cite it without importing
# the schema layer's private enum construction.
SIGNATURE_ALGORITHM_ED25519 = "Ed25519"

# D1: the challenge expires five minutes after issuance.
CHALLENGE_TTL = datetime.timedelta(minutes=5)

# Domain separation over the canonical enrollment bytes -- distinct from
# every other ARC signing profile's own tag, so a signature produced for a
# different profile can never verify here even if the canonical bytes
# happened to coincide. Matches the canonical vector suite's reference key
# material for `arc_approval_verifier_enrollment_v1` byte-for-byte.
_SIGNING_DOMAIN = b"ARC-APPROVAL-VERIFIER-ENROLLMENT-V1\x00"

# The human-readable domain label `EnrollmentChallengeResponse.signing_domain`
# reports. Distinct from `_SIGNING_DOMAIN` above on purpose: the fixture
# manifest reports this dotted form as `signing_domain` while the actual
# signed bytes are prefixed with the dashed, NUL-terminated form -- the two
# are deliberately different spellings of the same domain separation, and
# transcribing both from the fixture rather than inventing one avoids a
# drift that only a byte-level comparison would catch.
SIGNING_DOMAIN_LABEL = "arc.approval_verifier_enrollment.v1"

_ED25519_PUBLIC_KEY_BYTES = 32


class EnrollmentError(RegistryError):
    """Base of every enrollment refusal this module raises."""


class EnrollmentChallengeRequired(EnrollmentError):
    """No live, unconsumed enrollment challenge backs this request.

    Covers "no such challenge", "already consumed" (a second completion
    attempt lost the single-use race), and "expired" -- all three collapse
    to `arc_enrollment_challenge_required` (409) at the router, matching
    Appendix A.5's stated meaning for that code.
    """


class EnrollmentVerificationFailed(EnrollmentError):
    """The presented proof of possession or provider attestation is invalid.

    Covers a shape mismatch (a `VerifierAttestationProof` presented against
    an `exact_principal` challenge or vice versa), a wrong signature
    algorithm, a bad signature, and "no provider configured" -- all four
    collapse to `arc_enrollment_verification_failed` (400), and none of them
    discloses which specific check failed to the caller (Appendix A.5: "no
    code discloses ... any cryptographic oracle signal").
    """


@dataclasses.dataclass(frozen=True)
class DetachedSignatureProofInput:
    """Adapted from the wire `DetachedSignatureProof`."""

    signature_algorithm: str
    signature_base64: str


@dataclasses.dataclass(frozen=True)
class AttestationProofInput:
    """Adapted from the wire `VerifierAttestationProof`."""

    provider_id: str
    assertion_format: str
    assertion_base64: str


ProofInput = DetachedSignatureProofInput | AttestationProofInput


class VerifierAttestationProvider(Protocol):
    """One approved in-process provider for `provider_delegated` completion.

    Mirrors `approval.py`'s `VerifierAttestationProvider` exactly -- same
    "in-process, synchronous, no network round trip" contract -- because it
    is the same kind of trust decision: does this provider's own trust basis
    cover this exact canonical object.
    """

    def __call__(self, *, canonical_enrollment: bytes, assertion_format: str, assertion_base64: str) -> bool: ...


class SignatureVerifier(Protocol):
    def __call__(self, public_key: bytes, signature: bytes, payload: bytes) -> bool: ...


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify an Ed25519 signature, returning `False` rather than raising.

    A malformed key, a malformed signature, and a wrong signature are all
    "not verified" to the caller -- collapsing them here is what lets
    `register_verifier` treat verification as a boolean without catching a
    cryptography-library exception itself, matching every other ARC
    verification module's own `_ed25519_verify` helper.
    """
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def _rfc3339(moment: datetime.datetime) -> str:
    """UTC, `Z`-suffixed -- the exact form the profile's timestamp pattern
    requires, matching `source_admission.py`'s and `operational_chain.py`'s
    own identically-named helper."""
    dt = moment.astimezone(datetime.UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _canonical_enrollment_dict(
    *,
    enrollment_challenge_id: uuid.UUID,
    nonce: str,
    verifier_id: str,
    binding_kind: str,
    principal_issuer: str | None,
    principal_subject: str | None,
    provider_allowed_principal_issuer: str | None,
    owning_scope: str,
    target_tenant_id: uuid.UUID | None,
    allowed_evidence_types: list[str],
    signature_algorithm: str,
    key_digest: str,
    valid_from: datetime.datetime,
    valid_to: datetime.datetime,
    issued_at: datetime.datetime,
    expires_at: datetime.datetime,
) -> dict[str, object]:
    """The exact `arc_approval_verifier_enrollment_v1` object a caller signs
    (or a provider attests over). Every key is present unconditionally --
    nullability lives on the value, never on omitting the key -- matching
    `authoring_profile_shapes._profile`'s own closed-object rule.

    No `provider_id` key: the canonical profile's closed schema does not
    carry one (confirmed against the checked-in canonical fixture) -- only
    `provider_allowed_principal_issuer` is signed over, because the provider's own
    identity does not need attesting, only the issuer it is trusted to
    assert for.
    """
    return {
        "profile": APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
        "enrollment_challenge_id": str(enrollment_challenge_id),
        "nonce": nonce,
        "verifier_id": verifier_id,
        "binding_kind": binding_kind,
        "principal_issuer": principal_issuer,
        "principal_subject": principal_subject,
        "provider_allowed_principal_issuer": provider_allowed_principal_issuer,
        "scope_kind": owning_scope,
        "target_tenant_id": str(target_tenant_id) if target_tenant_id is not None else None,
        "allowed_evidence_types": list(allowed_evidence_types),
        "signature_algorithm": signature_algorithm,
        "key_digest": key_digest,
        "valid_from": _rfc3339(valid_from),
        "valid_to": _rfc3339(valid_to),
        "issued_at": _rfc3339(issued_at),
        "expires_at": _rfc3339(expires_at),
    }


@dataclasses.dataclass(frozen=True)
class IssuedChallenge:
    """What `create_challenge` hands back."""

    enrollment_challenge_id: uuid.UUID
    canonical_enrollment_bytes: bytes
    signing_domain: str
    expires_at: datetime.datetime


class EnrollmentService:
    """Issues D1 enrollment challenges and completes them into trust roots.

    `assert_request_tenant` only, matching `ApprovalTrustService`'s own
    reasoning: the deployment-operator gate a verifier trust decision needs
    is a router-level check against an exact `(issuer, subject)` allowlist
    pair, held by `arc_admin.py`'s `_require_global_operator` -- the same
    gate the pre-existing `register_approval_verifier` route already used,
    and re-deriving it here from a differently-scoped permission would be a
    second place for the two to disagree.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        attestation_providers: dict[str, VerifierAttestationProvider] | None = None,
        signature_verifier: SignatureVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._attestation_providers = dict(attestation_providers or {})
        self._signature_verifier = signature_verifier or _ed25519_verify

    # -- issuance -------------------------------------------------------------

    async def create_challenge(
        self,
        ctx: ArcRequestContext,
        *,
        binding_kind: str,
        principal_issuer: str | None,
        principal_subject: str | None,
        provider_id: str | None,
        provider_allowed_principal_issuer: str | None,
        owning_scope: str,
        target_tenant_id: uuid.UUID | None,
        evidence_types: list[str],
        signature_algorithm: str,
        public_key_base64: str,
        valid_from: datetime.datetime,
        valid_to: datetime.datetime,
    ) -> IssuedChallenge:
        """Mint a five-minute, single-use enrollment challenge.

        Pre-allocates the `verifier_id` the eventual `arc_approval_
        verifiers` row will use and commits it, together with every other
        immutable registration field, into `canonical_enrollment_bytes`
        *before* that row exists -- see the module docstring for why that
        ordering is the whole point.
        """
        self._authorization.assert_request_tenant(ctx)
        self._validate_shape(
            binding_kind=binding_kind,
            principal_issuer=principal_issuer,
            principal_subject=principal_subject,
            provider_id=provider_id,
            provider_allowed_principal_issuer=provider_allowed_principal_issuer,
        )
        if not evidence_types:
            msg = "a verifier permitted for no evidence type can never approve anything"
            raise EnrollmentError(msg)
        if signature_algorithm != SIGNATURE_ALGORITHM_ED25519:
            msg = f"unsupported signature algorithm {signature_algorithm!r}; ARC verifies Ed25519 only"
            raise EnrollmentError(msg)
        try:
            credential_material = base64.b64decode(public_key_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = "public_key_base64 is not valid base64"
            raise EnrollmentError(msg) from exc
        if valid_from >= valid_to:
            msg = "valid_from must precede valid_to"
            raise EnrollmentError(msg)

        enrollment_challenge_id = uuid.uuid4()
        verifier_id = str(uuid.uuid4())
        nonce = uuid.uuid4().hex
        issued_at = self._clock.now()
        expires_at = issued_at + CHALLENGE_TTL
        key_digest = hashlib.sha256(credential_material).hexdigest()

        canonical_obj = _canonical_enrollment_dict(
            enrollment_challenge_id=enrollment_challenge_id,
            nonce=nonce,
            verifier_id=verifier_id,
            binding_kind=binding_kind,
            principal_issuer=principal_issuer,
            principal_subject=principal_subject,
            provider_allowed_principal_issuer=provider_allowed_principal_issuer,
            owning_scope=owning_scope,
            target_tenant_id=target_tenant_id,
            allowed_evidence_types=evidence_types,
            signature_algorithm=signature_algorithm,
            key_digest=key_digest,
            valid_from=valid_from,
            valid_to=valid_to,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        try:
            canonical_bytes = authoring_profiles.canonicalize_approval_verifier_enrollment_v1(canonical_obj)
        except CanonicalizationError as exc:
            msg = f"enrollment request does not canonicalize: {exc}"
            raise EnrollmentError(msg) from exc

        async with self._session_factory() as session, session.begin():
            await queries.insert_challenge(
                session,
                enrollment_challenge_id=enrollment_challenge_id,
                verifier_id=verifier_id,
                nonce=nonce,
                binding_kind=binding_kind,
                principal_issuer=principal_issuer,
                principal_subject=principal_subject,
                provider_id=provider_id,
                provider_allowed_principal_issuer=provider_allowed_principal_issuer,
                owning_scope=owning_scope,
                target_tenant_id=target_tenant_id,
                allowed_evidence_types=list(evidence_types),
                signature_algorithm=signature_algorithm,
                credential_material=credential_material,
                canonical_enrollment_bytes=canonical_bytes,
                valid_from=valid_from,
                valid_to=valid_to,
                issued_at=issued_at,
                expires_at=expires_at,
                created_by_issuer=ctx.oidc_issuer,
                created_by_subject=ctx.oidc_subject,
                created_at=issued_at,
            )
            await audit_outbox.emit_global(
                session,
                event_type=actions.ARC_VERIFIER_ENROLLMENT_CHALLENGE_ISSUED,
                payload={
                    "enrollment_challenge_id": str(enrollment_challenge_id),
                    "verifier_id": verifier_id,
                    "binding_kind": binding_kind,
                    "owning_scope": owning_scope,
                    "target_tenant_id": str(target_tenant_id) if target_tenant_id else None,
                    "created_by_issuer": ctx.oidc_issuer,
                    "created_by_subject": ctx.oidc_subject,
                },
            )

        return IssuedChallenge(
            enrollment_challenge_id=enrollment_challenge_id,
            canonical_enrollment_bytes=canonical_bytes,
            signing_domain=SIGNING_DOMAIN_LABEL,
            expires_at=expires_at,
        )

    def _validate_shape(
        self,
        *,
        binding_kind: str,
        principal_issuer: str | None,
        principal_subject: str | None,
        provider_id: str | None,
        provider_allowed_principal_issuer: str | None,
    ) -> None:
        """Defense in depth ahead of the DDL CHECK: the wire model's own
        validator (`EnrollmentChallengeRequest._check_binding_kind`) requires
        the provider fields for `provider_delegated` but does not forbid the
        principal fields also being set -- so the hybrid case is caught here
        and by the migration's CHECK, not left to the request layer alone.
        """
        if binding_kind not in (BINDING_EXACT_PRINCIPAL, BINDING_PROVIDER_DELEGATED):
            msg = f"unknown binding_kind {binding_kind!r}"
            raise EnrollmentError(msg)
        if binding_kind == BINDING_EXACT_PRINCIPAL:
            if principal_issuer is None or principal_subject is None:
                msg = "exact_principal binding requires principal_issuer and principal_subject"
                raise EnrollmentError(msg)
            if provider_id is not None or provider_allowed_principal_issuer is not None:
                msg = "exact_principal binding forbids provider_id and provider_allowed_principal_issuer"
                raise EnrollmentError(msg)
        else:
            if provider_id is None or provider_allowed_principal_issuer is None:
                msg = "provider_delegated binding requires provider_id and provider_allowed_principal_issuer"
                raise EnrollmentError(msg)
            if principal_issuer is not None or principal_subject is not None:
                msg = "provider_delegated binding forbids principal_issuer and principal_subject"
                raise EnrollmentError(msg)

    # -- completion -------------------------------------------------------

    async def register_verifier(
        self, ctx: ArcRequestContext, *, enrollment_challenge_id: uuid.UUID, proof: ProofInput
    ) -> queries.VerifierRow:
        """Verify `proof` against the named challenge's committed bytes,
        consume the challenge exactly once, and write the trust root.

        One transaction: the challenge is locked `FOR UPDATE` for the whole
        call, so a second completion attempt for the same challenge blocks
        until this one commits or rolls back, then re-reads `consumed_at`
        and loses -- there is no interleaving in which two completions both
        observe the challenge unconsumed.
        """
        self._authorization.assert_request_tenant(ctx)
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            challenge = await queries.lock_challenge(session, enrollment_challenge_id)
            if challenge is None:
                msg = f"no enrollment challenge {enrollment_challenge_id}"
                raise EnrollmentChallengeRequired(msg)
            if challenge.consumed_at is not None:
                msg = f"enrollment challenge {enrollment_challenge_id} was already consumed"
                raise EnrollmentChallengeRequired(msg)
            # Valid at issued_at <= now < expires_at -- equality at the
            # deadline refuses, matching the approval-challenge protocol's
            # own stated window and the same equality-at-deadline
            # convention `source_status_refresh.py`'s expiry check
            # already uses.
            if now >= challenge.expires_at:
                msg = f"enrollment challenge {enrollment_challenge_id} expired at {challenge.expires_at.isoformat()}"
                raise EnrollmentChallengeRequired(msg)

            principal_issuer, principal_subject, credential_fingerprint, provider_configuration_digest = (
                self._verify_proof(challenge, proof)
            )

            consumed = await queries.consume_challenge(session, enrollment_challenge_id, consumed_at=now)
            if consumed != 1:
                # Lost the single-use race between the lock above and here.
                # Unreachable under `FOR UPDATE` in a single connection, but
                # this is the same defensive belt-and-braces
                # `advance_head`/`mark_exported` apply to their own
                # compare-and-swaps elsewhere in this subsystem.
                msg = f"enrollment challenge {enrollment_challenge_id} was consumed by a concurrent request"
                raise EnrollmentChallengeRequired(msg)

            verifier_kind = _KIND_OPERATOR_KEY if challenge.binding_kind == BINDING_EXACT_PRINCIPAL else _KIND_PROVIDER
            await queries.insert_verifier(
                session,
                approval_verifier_id=challenge.verifier_id,
                verifier_kind=verifier_kind,
                allowed_evidence_types=list(challenge.allowed_evidence_types),
                scope_kind=challenge.owning_scope,
                scope_tenant_id=challenge.target_tenant_id,
                algorithm=challenge.signature_algorithm if verifier_kind == _KIND_OPERATOR_KEY else None,
                public_key=challenge.credential_material if verifier_kind == _KIND_OPERATOR_KEY else None,
                provider_id=challenge.provider_id,
                valid_from=challenge.valid_from,
                valid_to=challenge.valid_to,
                created_at=now,
                principal_binding_kind=challenge.binding_kind,
                principal_issuer=principal_issuer,
                principal_subject=principal_subject,
                provider_allowed_principal_issuer=challenge.provider_allowed_principal_issuer,
                credential_fingerprint=credential_fingerprint,
                provider_configuration_digest=provider_configuration_digest,
                enrollment_challenge_id=enrollment_challenge_id,
                enrollment_verified_at=now,
            )
            await audit_outbox.emit_global(
                session,
                event_type=actions.ARC_APPROVAL_VERIFIER_REGISTERED,
                payload={
                    "approval_verifier_id": challenge.verifier_id,
                    "binding_kind": challenge.binding_kind,
                    "enrollment_challenge_id": str(enrollment_challenge_id),
                    "credential_fingerprint": credential_fingerprint,
                    "registered_by_issuer": ctx.oidc_issuer,
                    "registered_by_subject": ctx.oidc_subject,
                },
            )

            row = await queries.load_verifier(session, challenge.verifier_id)
            if row is None:
                msg = f"approval verifier {challenge.verifier_id!r} vanished immediately after insert"
                raise RegistryError(msg)
            return row

    async def get_verifier(self, approval_verifier_id: str) -> queries.VerifierRow | None:
        """A plain, unlocked read -- for building a response after
        `ApprovalTrustService.revoke_verifier` has already committed the
        revocation in its own transaction. Never used on a write path: a
        caller that needs the row locked for a subsequent write should read
        it through its own transaction instead, matching `VerifierRegistry.
        get`'s own `FOR SHARE` convention.
        """
        async with self._session_factory() as session:
            return await queries.load_verifier(session, approval_verifier_id)

    def _verify_proof(
        self, challenge: queries.ChallengeRow, proof: ProofInput
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Verify `proof` against `challenge`'s committed canonical bytes.

        Returns `(principal_issuer, principal_subject, credential_
        fingerprint, provider_configuration_digest)` for the row about to be
        written. The refusal is always on the proof itself -- wrong shape,
        wrong algorithm, bad signature, unconfigured provider -- never on
        some other field, matching this task's own requirement that a
        wrong-key refusal be provably about the signature.
        """
        signing_input = _SIGNING_DOMAIN + challenge.canonical_enrollment_bytes

        if challenge.binding_kind == BINDING_EXACT_PRINCIPAL:
            if not isinstance(proof, DetachedSignatureProofInput):
                msg = "exact_principal enrollment requires a detached signature, not a provider attestation"
                raise EnrollmentVerificationFailed(msg)
            if proof.signature_algorithm != challenge.signature_algorithm:
                msg = "signature algorithm does not match the challenge"
                raise EnrollmentVerificationFailed(msg)
            try:
                signature = base64.b64decode(proof.signature_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                msg = "signature_base64 is not valid base64"
                raise EnrollmentVerificationFailed(msg) from exc
            if len(challenge.credential_material) != _ED25519_PUBLIC_KEY_BYTES:
                msg = "challenge credential material is not a valid Ed25519 public key"
                raise EnrollmentVerificationFailed(msg)
            if not self._signature_verifier(challenge.credential_material, signature, signing_input):
                msg = "enrollment proof-of-possession signature did not verify"
                raise EnrollmentVerificationFailed(msg)
            credential_fingerprint = hashlib.sha256(challenge.credential_material).hexdigest()
            return challenge.principal_issuer, challenge.principal_subject, credential_fingerprint, None

        # provider_delegated
        if not isinstance(proof, AttestationProofInput):
            msg = "provider_delegated enrollment requires a provider attestation, not a detached signature"
            raise EnrollmentVerificationFailed(msg)
        if proof.provider_id != challenge.provider_id:
            msg = "attestation names a different provider than the challenge"
            raise EnrollmentVerificationFailed(msg)
        provider = self._attestation_providers.get(proof.provider_id)
        if provider is None:
            # The honest, real-today state of every deployment: see the
            # module docstring for why this refuses rather than fabricates
            # trust, and why the principal-recording question below it is
            # therefore never reached in production.
            msg = f"no in-process attestation provider is configured for {proof.provider_id!r} on this deployment"
            raise EnrollmentVerificationFailed(msg)
        if not provider(
            canonical_enrollment=challenge.canonical_enrollment_bytes,
            assertion_format=proof.assertion_format,
            assertion_base64=proof.assertion_base64,
        ):
            msg = "in-process provider did not validate the attestation"
            raise EnrollmentVerificationFailed(msg)
        provider_configuration_digest = hashlib.sha256(challenge.credential_material).hexdigest()
        # `principal_issuer`/`principal_subject` stay `None`: the canonical
        # enrollment object commits `provider_allowed_principal_issuer`, not
        # a specific subject, and the migration's own CHECK forbids a
        # `provider_delegated` row from carrying either -- see the module
        # docstring's last section for the response-shape tension this
        # leaves for whoever configures the first real provider.
        return None, None, None, provider_configuration_digest


__all__ = [
    "BINDING_EXACT_PRINCIPAL",
    "BINDING_PROVIDER_DELEGATED",
    "CHALLENGE_TTL",
    "SIGNATURE_ALGORITHM_ED25519",
    "SIGNING_DOMAIN_LABEL",
    "AttestationProofInput",
    "DetachedSignatureProofInput",
    "EnrollmentChallengeRequired",
    "EnrollmentError",
    "EnrollmentService",
    "EnrollmentVerificationFailed",
    "IssuedChallenge",
    "ProofInput",
    "SignatureVerifier",
    "VerifierAttestationProvider",
]
