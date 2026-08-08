"""Unit tests for `contextplane/arc/service/enrollment.py`.

No database: `queries.enrollment`'s functions are monkeypatched with an
in-memory fake faithful to the real relational shape -- in particular its
`consume_challenge`'s `WHERE consumed_at IS NULL` compare-and-swap, which
returns 0 once a challenge is already consumed rather than raising or
silently succeeding twice. That is what lets
`test_a_second_completion_attempt_loses` exercise the single-use logic
without a database.

Signing is real throughout: every proof this suite presents is an actual
Ed25519 signature (or, for the negative cases, a deliberately wrong one)
verified through the service's real `_verify_proof`, over the real
canonical bytes `authoring_profiles.canonicalize_approval_verifier_
enrollment_v1` produces -- so a test that swaps in the wrong key is testing
the real cryptography, not a stand-in for it.

What a fake cannot prove -- that Postgres's own `FOR UPDATE` lock and
`consumed_at IS NULL` compare-and-swap actually serialize two concurrent
completions, and that the migration's CHECK/UNIQUE constraints hold under a
real INSERT -- is `tests/integration/test_arc_enrollment.py`'s job.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import pathlib
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from contextplane.arc.schemas.authoring_profiles import canonicalize_approval_verifier_enrollment_v1
from contextplane.arc.service import enrollment as en
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.queries.enrollment import ChallengeRow, VerifierRow
from contextplane.arc.types import ArcRequestContext
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_ISSUER = "https://idp.example.test"


class _FakeClock:
    def __init__(self, moment: datetime.datetime = _NOW) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment

    def set(self, moment: datetime.datetime) -> None:
        self._moment = moment


class _AllowAll:
    async def visible_capability_ids(self, ctx: object, capability_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
        return list(capability_ids)


def _ctx(*, subject: str = "operator") -> ArcRequestContext:
    return ArcRequestContext(
        tenant=TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"], oidc_subject=subject),
        oidc_issuer=_ISSUER,
    )


def _authorization() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_AllowAll())


# ---------------------------------------------------------------------------
# Session doubles -- matching `test_arc_source_admission.py`'s own shape.
# ---------------------------------------------------------------------------


class _NoopTransactionCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _NullSession:
    def begin(self) -> _NoopTransactionCM:
        return _NoopTransactionCM()

    async def execute(self, *args: object, **kwargs: object) -> None:
        # `audit_outbox.emit_global` writes through this -- a unit test has
        # no real outbox table to check against; the audit row's content is
        # asserted against a real Postgres `arc_audit_outbox` row in
        # `tests/integration/test_arc_enrollment.py` instead.
        return None


class _SessionCM:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory() -> _SessionCM:
    return _SessionCM(_NullSession())


# ---------------------------------------------------------------------------
# In-memory fake for the six `queries.enrollment` module-level functions.
# ---------------------------------------------------------------------------


class FakeEnrollmentQueries:
    def __init__(self) -> None:
        self.challenges: dict[uuid.UUID, ChallengeRow] = {}
        self.verifiers: dict[str, VerifierRow] = {}

    async def insert_challenge(self, _session: object, **kwargs: Any) -> None:
        row = ChallengeRow(
            enrollment_challenge_id=kwargs["enrollment_challenge_id"],
            verifier_id=kwargs["verifier_id"],
            nonce=kwargs["nonce"],
            binding_kind=kwargs["binding_kind"],
            principal_issuer=kwargs["principal_issuer"],
            principal_subject=kwargs["principal_subject"],
            provider_id=kwargs["provider_id"],
            provider_allowed_principal_issuer=kwargs["provider_allowed_principal_issuer"],
            owning_scope=kwargs["owning_scope"],
            target_tenant_id=kwargs["target_tenant_id"],
            allowed_evidence_types=tuple(kwargs["allowed_evidence_types"]),
            signature_algorithm=kwargs["signature_algorithm"],
            credential_material=kwargs["credential_material"],
            canonical_enrollment_bytes=kwargs["canonical_enrollment_bytes"],
            valid_from=kwargs["valid_from"],
            valid_to=kwargs["valid_to"],
            issued_at=kwargs["issued_at"],
            expires_at=kwargs["expires_at"],
            consumed_at=None,
        )
        self.challenges[row.enrollment_challenge_id] = row

    async def load_challenge(self, _session: object, enrollment_challenge_id: uuid.UUID) -> ChallengeRow | None:
        return self.challenges.get(enrollment_challenge_id)

    async def lock_challenge(self, _session: object, enrollment_challenge_id: uuid.UUID) -> ChallengeRow | None:
        return self.challenges.get(enrollment_challenge_id)

    async def consume_challenge(
        self, _session: object, enrollment_challenge_id: uuid.UUID, *, consumed_at: datetime.datetime
    ) -> int:
        row = self.challenges.get(enrollment_challenge_id)
        if row is None or row.consumed_at is not None:
            return 0
        self.challenges[enrollment_challenge_id] = dataclasses.replace(row, consumed_at=consumed_at)
        return 1

    async def insert_verifier(self, _session: object, **kwargs: Any) -> None:
        row = VerifierRow(
            approval_verifier_id=kwargs["approval_verifier_id"],
            verifier_kind=kwargs["verifier_kind"],
            allowed_evidence_types=tuple(kwargs["allowed_evidence_types"]),
            scope_kind=kwargs["scope_kind"],
            scope_tenant_id=kwargs["scope_tenant_id"],
            provider_id=kwargs["provider_id"],
            valid_from=kwargs["valid_from"],
            valid_to=kwargs["valid_to"],
            revoked_at=None,
            created_at=kwargs["created_at"],
            principal_binding_kind=kwargs["principal_binding_kind"],
            principal_issuer=kwargs["principal_issuer"],
            principal_subject=kwargs["principal_subject"],
            provider_allowed_principal_issuer=kwargs["provider_allowed_principal_issuer"],
            credential_fingerprint=kwargs["credential_fingerprint"],
            provider_configuration_digest=kwargs["provider_configuration_digest"],
            enrollment_challenge_id=kwargs["enrollment_challenge_id"],
            enrollment_verified_at=kwargs["enrollment_verified_at"],
        )
        self.verifiers[row.approval_verifier_id] = row

    async def load_verifier(self, _session: object, approval_verifier_id: str) -> VerifierRow | None:
        return self.verifiers.get(approval_verifier_id)


@pytest.fixture(autouse=True)
def _patch_queries(monkeypatch: pytest.MonkeyPatch) -> FakeEnrollmentQueries:
    fake = FakeEnrollmentQueries()
    for name in (
        "insert_challenge",
        "load_challenge",
        "lock_challenge",
        "consume_challenge",
        "insert_verifier",
        "load_verifier",
    ):
        monkeypatch.setattr(en.queries, name, getattr(fake, name))
    return fake


def _build_service(
    *, clock: _FakeClock | None = None, attestation_providers: dict[str, Any] | None = None
) -> en.EnrollmentService:
    return en.EnrollmentService(
        _session_factory,
        authorization=_authorization(),
        clock=clock or _FakeClock(),
        attestation_providers=attestation_providers,
    )


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


async def _create_exact_principal_challenge(
    service: en.EnrollmentService, public_key: bytes, **overrides: object
) -> en.IssuedChallenge:
    kwargs: dict[str, object] = {
        "binding_kind": en.BINDING_EXACT_PRINCIPAL,
        "principal_issuer": _ISSUER,
        "principal_subject": "approver-1",
        "provider_id": None,
        "provider_allowed_principal_issuer": None,
        "owning_scope": "global",
        "target_tenant_id": None,
        "evidence_types": ["exception_approval"],
        "signature_algorithm": en.SIGNATURE_ALGORITHM_ED25519,
        "public_key_base64": base64.b64encode(public_key).decode("ascii"),
        "valid_from": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "valid_to": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
    }
    kwargs.update(overrides)
    return await service.create_challenge(_ctx(), **kwargs)  # type: ignore[arg-type]


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    signature = private.sign(en._SIGNING_DOMAIN + canonical_bytes)
    return base64.b64encode(signature).decode("ascii")


def _proof(signature_base64: str) -> en.DetachedSignatureProofInput:
    return en.DetachedSignatureProofInput(
        signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519, signature_base64=signature_base64
    )


# ---------------------------------------------------------------------------
# Canonicalization: reproduce the checked-in authoritative fixture
# byte-for-byte.
# ---------------------------------------------------------------------------


def _load_fixture_manifest_case(case_id: str) -> dict[str, Any]:
    fixtures_root = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "arc_authoring"
    manifest = json.loads((fixtures_root / "manifest.json").read_text(encoding="utf-8"))
    for profile in manifest["profiles"]:
        if profile["profile"] != "arc_approval_verifier_enrollment_v1":
            continue
        for case in profile["cases"]:
            if case["case_id"] == case_id:
                input_obj = json.loads((fixtures_root / case["input_path"]).read_text(encoding="utf-8"))
                return {"expected": case["expected"], "input": input_obj}
    raise AssertionError(f"no {case_id!r} case in the manifest")


def test_canonical_bytes_match_the_authoritative_fixture() -> None:
    """Ground-truth check: build the exact `typical.json` object this
    module's own `_canonical_enrollment_dict` would produce for an
    equivalent challenge, and assert the canonical bytes and digest this
    module's canonicalizer computes equal the checked-in fixture's own
    `canonical_bytes_base64`/`digest` exactly -- loaded from the checked-in
    fixture rather than transcribed by hand, so this test cannot drift from
    it silently.

    Confirms this module's own domain-separation constants and object shape
    agree with the independently generated, Node-cross-verified vectors
    rather than merely with themselves.
    """
    fixture = _load_fixture_manifest_case("typical")
    raw = fixture["input"]

    obj = en._canonical_enrollment_dict(
        enrollment_challenge_id=uuid.UUID(raw["enrollment_challenge_id"]),
        nonce=raw["nonce"],
        verifier_id=raw["verifier_id"],
        binding_kind=raw["binding_kind"],
        principal_issuer=raw["principal_issuer"],
        principal_subject=raw["principal_subject"],
        provider_allowed_principal_issuer=raw["provider_allowed_principal_issuer"],
        owning_scope=raw["scope_kind"],
        target_tenant_id=uuid.UUID(raw["target_tenant_id"]) if raw["target_tenant_id"] else None,
        allowed_evidence_types=list(raw["allowed_evidence_types"]),
        signature_algorithm=raw["signature_algorithm"],
        key_digest=raw["key_digest"],
        valid_from=datetime.datetime.fromisoformat(raw["valid_from"].replace("Z", "+00:00")),
        valid_to=datetime.datetime.fromisoformat(raw["valid_to"].replace("Z", "+00:00")),
        issued_at=datetime.datetime.fromisoformat(raw["issued_at"].replace("Z", "+00:00")),
        expires_at=datetime.datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00")),
    )
    canonical_bytes = canonicalize_approval_verifier_enrollment_v1(obj)

    assert base64.b64encode(canonical_bytes).decode("ascii") == fixture["expected"]["canonical_bytes_base64"]
    assert hashlib.sha256(canonical_bytes).hexdigest() == fixture["expected"]["digest"]
    assert fixture["expected"]["signing_domain"] == en.SIGNING_DOMAIN_LABEL

    signing_input = en._SIGNING_DOMAIN + canonical_bytes
    assert signing_input.startswith(b"ARC-APPROVAL-VERIFIER-ENROLLMENT-V1\x00")
    assert base64.b64encode(signing_input).decode("ascii") == fixture["expected"]["signature_input_base64"]


# ---------------------------------------------------------------------------
# create_challenge: shape validation
# ---------------------------------------------------------------------------


class TestCreateChallengeShapeValidation:
    async def test_exact_principal_accepted(self) -> None:
        service = _build_service()
        _, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        assert issued.signing_domain == en.SIGNING_DOMAIN_LABEL

    async def test_provider_delegated_accepted(self) -> None:
        service = _build_service()
        _, public = _keypair()
        issued = await service.create_challenge(
            _ctx(),
            binding_kind=en.BINDING_PROVIDER_DELEGATED,
            principal_issuer=None,
            principal_subject=None,
            provider_id="idp-provider-1",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            owning_scope="global",
            target_tenant_id=None,
            evidence_types=["exception_approval"],
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            public_key_base64=base64.b64encode(public).decode("ascii"),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        )
        assert issued.enrollment_challenge_id is not None

    async def test_empty_case_is_refused(self) -> None:
        """binding_kind declared but neither shape's fields are present."""
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="requires"):
            await _create_exact_principal_challenge(service, public, principal_issuer=None, principal_subject=None)

    async def test_hybrid_case_is_refused(self) -> None:
        """Both principal and provider fields set -- the wire model's own
        validator does not catch this for `provider_delegated`; the
        service's own `_validate_shape` must (see its docstring)."""
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="forbids"):
            await _create_exact_principal_challenge(
                service, public, provider_id="idp-1", provider_allowed_principal_issuer="https://idp.example/"
            )

    async def test_no_evidence_types_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="never approve"):
            await _create_exact_principal_challenge(service, public, evidence_types=[])

    async def test_unsupported_algorithm_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="Ed25519"):
            await _create_exact_principal_challenge(service, public, signature_algorithm="RSA-2048")

    async def test_non_base64_public_key_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="base64"):
            await _create_exact_principal_challenge(service, public, public_key_base64="not!base64!")

    async def test_valid_from_after_valid_to_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        with pytest.raises(en.EnrollmentError, match="valid_from"):
            await _create_exact_principal_challenge(
                service,
                public,
                valid_from=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
                valid_to=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            )


# ---------------------------------------------------------------------------
# register_verifier: the real cryptographic proof
# ---------------------------------------------------------------------------


class TestRegisterVerifier:
    async def test_a_genuine_signature_is_accepted_and_recorded(self) -> None:
        service = _build_service()
        private, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)

        row = await service.register_verifier(
            _ctx(),
            enrollment_challenge_id=issued.enrollment_challenge_id,
            proof=_proof(_sign(private, issued.canonical_enrollment_bytes)),
        )
        assert row.principal_binding_kind == en.BINDING_EXACT_PRINCIPAL
        assert row.principal_issuer == _ISSUER
        assert row.principal_subject == "approver-1"
        assert row.credential_fingerprint is not None
        assert len(row.credential_fingerprint) == 64

    async def test_a_wrong_key_signature_is_refused_on_the_signature_itself(self) -> None:
        """The refusal is provably about the signature, not some other
        field: the challenge, the canonical bytes, and the presented
        signature bytes are byte-identical to the accepted case above --
        only the *verifying* key differs (the challenge's own stored
        credential is the genuine one; the signature was produced by a
        different, unrelated key)."""
        service = _build_service()
        genuine, public = _keypair()
        wrong, _ = _keypair()
        issued = await _create_exact_principal_challenge(service, public)

        # Sign with the WRONG key over the exact same signing input a
        # genuine registrant would use.
        bad_signature = _sign(wrong, issued.canonical_enrollment_bytes)
        with pytest.raises(en.EnrollmentVerificationFailed, match="signature"):
            await service.register_verifier(
                _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(bad_signature)
            )

        # The genuine key, same challenge, same canonical bytes: accepted.
        # Proves the refusal above was really about the key, not some
        # incidental difference in how the two calls were made.
        good_signature = _sign(genuine, issued.canonical_enrollment_bytes)
        row = await service.register_verifier(
            _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(good_signature)
        )
        assert row.credential_fingerprint == hashlib.sha256(public).hexdigest()

    async def test_a_tampered_canonical_byte_is_refused(self) -> None:
        """A signature valid over the real bytes must not verify over a
        single flipped byte -- the digest-substitution class of check."""
        service = _build_service()
        private, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        signature = _sign(private, issued.canonical_enrollment_bytes)

        # Simulate a tampered challenge row by re-deriving against altered
        # bytes directly through `_verify_proof`.
        tampered = bytearray(issued.canonical_enrollment_bytes)
        tampered[0] ^= 0xFF
        fake_challenge = ChallengeRow(
            enrollment_challenge_id=issued.enrollment_challenge_id,
            verifier_id="v",
            nonce="n",
            binding_kind=en.BINDING_EXACT_PRINCIPAL,
            principal_issuer=_ISSUER,
            principal_subject="approver-1",
            provider_id=None,
            provider_allowed_principal_issuer=None,
            owning_scope="global",
            target_tenant_id=None,
            allowed_evidence_types=("exception_approval",),
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            credential_material=public,
            canonical_enrollment_bytes=bytes(tampered),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
            issued_at=_NOW,
            expires_at=_NOW + en.CHALLENGE_TTL,
            consumed_at=None,
        )
        with pytest.raises(en.EnrollmentVerificationFailed, match="signature"):
            service._verify_proof(fake_challenge, _proof(signature))

    async def test_an_unknown_challenge_is_refused(self) -> None:
        service = _build_service()
        with pytest.raises(en.EnrollmentChallengeRequired):
            await service.register_verifier(
                _ctx(), enrollment_challenge_id=uuid.uuid4(), proof=_proof(base64.b64encode(b"x" * 64).decode("ascii"))
            )

    async def test_a_second_completion_attempt_loses(self) -> None:
        """Sequential double-completion: the logic half of single-use. The
        real concurrent race against Postgres's own lock is
        `tests/integration/test_arc_enrollment.py`'s job."""
        service = _build_service()
        private, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        signature = _sign(private, issued.canonical_enrollment_bytes)

        await service.register_verifier(
            _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
        )
        with pytest.raises(en.EnrollmentChallengeRequired, match="already consumed"):
            await service.register_verifier(
                _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
            )

    async def test_expiry_refuses_exactly_at_the_deadline(self) -> None:
        """`now == expires_at` refuses; one second earlier accepts --
        the same equality-at-deadline convention `source_status_refresh.py`'s
        expiry check already uses."""
        clock = _FakeClock(_NOW)
        service = _build_service(clock=clock)
        private, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        signature = _sign(private, issued.canonical_enrollment_bytes)

        clock.set(issued.expires_at)
        with pytest.raises(en.EnrollmentChallengeRequired, match="expired"):
            await service.register_verifier(
                _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
            )

    async def test_one_second_before_the_deadline_still_succeeds(self) -> None:
        clock = _FakeClock(_NOW)
        service = _build_service(clock=clock)
        private, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        signature = _sign(private, issued.canonical_enrollment_bytes)

        clock.set(issued.expires_at - datetime.timedelta(seconds=1))
        row = await service.register_verifier(
            _ctx(), enrollment_challenge_id=issued.enrollment_challenge_id, proof=_proof(signature)
        )
        assert row.enrollment_verified_at == issued.expires_at - datetime.timedelta(seconds=1)

    async def test_a_provider_attestation_against_an_exact_principal_challenge_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        issued = await _create_exact_principal_challenge(service, public)
        with pytest.raises(en.EnrollmentVerificationFailed, match="detached signature"):
            await service.register_verifier(
                _ctx(),
                enrollment_challenge_id=issued.enrollment_challenge_id,
                proof=en.AttestationProofInput(
                    provider_id="idp-1", assertion_format="jwt", assertion_base64="ZmFrZQ=="
                ),
            )

    async def test_a_detached_signature_against_a_provider_delegated_challenge_is_refused(self) -> None:
        service = _build_service()
        _, public = _keypair()
        issued = await service.create_challenge(
            _ctx(),
            binding_kind=en.BINDING_PROVIDER_DELEGATED,
            principal_issuer=None,
            principal_subject=None,
            provider_id="idp-1",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            owning_scope="global",
            target_tenant_id=None,
            evidence_types=["exception_approval"],
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            public_key_base64=base64.b64encode(public).decode("ascii"),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        )
        with pytest.raises(en.EnrollmentVerificationFailed, match="provider attestation"):
            await service.register_verifier(
                _ctx(),
                enrollment_challenge_id=issued.enrollment_challenge_id,
                proof=_proof(base64.b64encode(b"x" * 64).decode("ascii")),
            )

    async def test_provider_delegated_refuses_cleanly_with_no_provider_configured(self) -> None:
        """Every deployment today: `attestation_providers` defaults to
        empty. This is the module docstring's flagged gap, proven rather
        than asserted by prose."""
        service = _build_service(attestation_providers={})
        _, public = _keypair()
        issued = await service.create_challenge(
            _ctx(),
            binding_kind=en.BINDING_PROVIDER_DELEGATED,
            principal_issuer=None,
            principal_subject=None,
            provider_id="idp-1",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            owning_scope="global",
            target_tenant_id=None,
            evidence_types=["exception_approval"],
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            public_key_base64=base64.b64encode(public).decode("ascii"),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        )
        with pytest.raises(en.EnrollmentVerificationFailed, match="no in-process attestation provider"):
            await service.register_verifier(
                _ctx(),
                enrollment_challenge_id=issued.enrollment_challenge_id,
                proof=en.AttestationProofInput(
                    provider_id="idp-1", assertion_format="jwt", assertion_base64="ZmFrZQ=="
                ),
            )

    async def test_a_configured_provider_that_validates_is_accepted(self) -> None:
        """The injection point is real and testable even though no
        deployment configures one today (see the module docstring)."""

        def _provider(*, canonical_enrollment: bytes, assertion_format: str, assertion_base64: str) -> bool:
            assert assertion_format == "jwt"
            return assertion_base64 == "dHJ1c3RlZA=="

        service = _build_service(attestation_providers={"idp-1": _provider})
        _, public = _keypair()
        issued = await service.create_challenge(
            _ctx(),
            binding_kind=en.BINDING_PROVIDER_DELEGATED,
            principal_issuer=None,
            principal_subject=None,
            provider_id="idp-1",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            owning_scope="global",
            target_tenant_id=None,
            evidence_types=["exception_approval"],
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            public_key_base64=base64.b64encode(public).decode("ascii"),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        )
        row = await service.register_verifier(
            _ctx(),
            enrollment_challenge_id=issued.enrollment_challenge_id,
            proof=en.AttestationProofInput(
                provider_id="idp-1", assertion_format="jwt", assertion_base64="dHJ1c3RlZA=="
            ),
        )
        assert row.principal_binding_kind == en.BINDING_PROVIDER_DELEGATED
        assert row.principal_issuer is None
        assert row.principal_subject is None
        assert row.provider_configuration_digest is not None

    async def test_a_configured_provider_that_refuses_is_refused(self) -> None:
        def _provider(*, canonical_enrollment: bytes, assertion_format: str, assertion_base64: str) -> bool:
            return False

        service = _build_service(attestation_providers={"idp-1": _provider})
        _, public = _keypair()
        issued = await service.create_challenge(
            _ctx(),
            binding_kind=en.BINDING_PROVIDER_DELEGATED,
            principal_issuer=None,
            principal_subject=None,
            provider_id="idp-1",
            provider_allowed_principal_issuer="https://idp.upstream.example/",
            owning_scope="global",
            target_tenant_id=None,
            evidence_types=["exception_approval"],
            signature_algorithm=en.SIGNATURE_ALGORITHM_ED25519,
            public_key_base64=base64.b64encode(public).decode("ascii"),
            valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            valid_to=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        )
        with pytest.raises(en.EnrollmentVerificationFailed, match="did not validate"):
            await service.register_verifier(
                _ctx(),
                enrollment_challenge_id=issued.enrollment_challenge_id,
                proof=en.AttestationProofInput(
                    provider_id="idp-1", assertion_format="jwt", assertion_base64="ZmFrZQ=="
                ),
            )
