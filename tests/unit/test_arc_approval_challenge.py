"""Unit tests for the D2 approval-challenge protocol's pure module
(`contextplane/arc/service/approval_challenge_verification.py`) and this
commit's own dormancy proof.

No database: everything here is a function of its arguments alone, which is
the whole point of the pure/stateful split this module's own docstring
describes. Signing is real throughout -- every proof presented is an actual
Ed25519 signature (or, for the negative cases, a deliberately wrong one)
verified through the real `verify_proof`, over the real canonical bytes
`authoring_profiles.canonicalize_artifact_revision_v1` produces. What a fake
cannot prove -- that Postgres's own row locks and CHECK/UNIQUE constraints
actually serialize a race, including the named `asyncio.gather` matrix --
is `tests/integration/test_arc_approval_race.py`'s job.
"""

from __future__ import annotations

import ast
import base64
import datetime
import hashlib
import json
import pathlib
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from contextplane.arc.service import approval_challenge as ac
from contextplane.arc.service import approval_challenge_verification as acv

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


def _sign(private: Ed25519PrivateKey, canonical_bytes: bytes) -> str:
    return base64.b64encode(private.sign(acv._SIGNING_DOMAIN + canonical_bytes)).decode("ascii")


def _verifier(
    *,
    public_key: bytes | None,
    principal_binding_kind: str | None = "exact_principal",
    principal_issuer: str | None = "https://idp.example.test",
    principal_subject: str | None = "verifier-1",
    provider_id: str | None = None,
    algorithm: str | None = "Ed25519",
    allowed_evidence_types: frozenset[str] = frozenset({"artifact_activation"}),
    valid_from: datetime.datetime = _NOW - datetime.timedelta(days=1),
    valid_to: datetime.datetime | None = None,
    revoked_at: datetime.datetime | None = None,
    credential_fingerprint: str | None = "f" * 64,
) -> acv.VerifierMaterial:
    return acv.VerifierMaterial(
        approval_verifier_id="verifier-1",
        allowed_evidence_types=allowed_evidence_types,
        valid_from=valid_from,
        valid_to=valid_to,
        revoked_at=revoked_at,
        principal_binding_kind=principal_binding_kind,
        principal_issuer=principal_issuer,
        principal_subject=principal_subject,
        provider_id=provider_id,
        algorithm=algorithm,
        public_key=public_key,
        credential_fingerprint=credential_fingerprint,
    )


# ---------------------------------------------------------------------------
# Reuses the existing canonicalizer -- ground-truthed against the
# checked-in canonical vector fixture, not merely against itself.
# ---------------------------------------------------------------------------


def _load_fixture_case(profile: str, case_id: str) -> dict[str, Any]:
    fixtures_root = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "arc_authoring"
    manifest = json.loads((fixtures_root / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["profiles"]:
        if entry["profile"] != profile:
            continue
        for case in entry["cases"]:
            if case["case_id"] == case_id:
                input_obj = json.loads((fixtures_root / case["input_path"]).read_text(encoding="utf-8"))
                return {"expected": case["expected"], "input": input_obj}
    raise AssertionError(f"no {case_id!r} case for profile {profile!r} in the manifest")


def test_build_canonical_evidence_reuses_authoring_profiles_and_is_not_a_second_canonicalizer() -> None:
    """`build_canonical_evidence` must call straight through to
    `authoring_profiles.canonicalize_artifact_revision_v1` -- asserted by
    AST inspection of this module's own source, not merely by behavioral
    coincidence, so a future edit that inlines a hand-rolled JSON dump here
    instead fails this test rather than silently becoming a third
    canonicalizer.
    """
    source = pathlib.Path(acv.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "canonicalize_artifact_revision_v1" in calls


def test_canonical_bytes_match_the_authoritative_fixture() -> None:
    """Ground-truth check: build the exact `typical.json` object
    `build_canonical_evidence` would produce for an equivalent target, and
    assert the canonical bytes and digest this module's canonicalizer
    computes equal the checked-in fixture's own values exactly -- loaded
    from the checked-in fixture rather than transcribed by hand, so this
    test cannot drift from it silently. Mirrors `test_arc_enrollment.py`'s
    identically-shaped proof for `arc_approval_verifier_enrollment_v1`.
    """
    fixture = _load_fixture_case("arc_artifact_revision_v1", "typical")
    raw = fixture["input"]

    canonical_bytes = acv.build_canonical_evidence(
        artifact_id=uuid.UUID(raw["artifact_id"]),
        revision_id=uuid.UUID(raw["revision_id"]),
        artifact_semantics_digest=raw["artifact_semantics_digest"],
        review_package_digest=raw["review_package_digest"],
    )

    assert base64.b64encode(canonical_bytes).decode("ascii") == fixture["expected"]["canonical_bytes_base64"]
    assert hashlib.sha256(canonical_bytes).hexdigest() == fixture["expected"]["digest"]


def test_canonical_bytes_change_when_any_input_changes() -> None:
    """Digest substitution at any node must actually change the bytes --
    otherwise a substituted `S` or `R` could canonicalize identically."""
    base = acv.build_canonical_evidence(
        artifact_id=uuid.UUID(int=1),
        revision_id=uuid.UUID(int=2),
        artifact_semantics_digest="1" * 64,
        review_package_digest="2" * 64,
    )
    changed_s = acv.build_canonical_evidence(
        artifact_id=uuid.UUID(int=1),
        revision_id=uuid.UUID(int=2),
        artifact_semantics_digest="9" * 64,
        review_package_digest="2" * 64,
    )
    changed_r = acv.build_canonical_evidence(
        artifact_id=uuid.UUID(int=1),
        revision_id=uuid.UUID(int=2),
        artifact_semantics_digest="1" * 64,
        review_package_digest="9" * 64,
    )
    changed_target = acv.build_canonical_evidence(
        artifact_id=uuid.UUID(int=3),
        revision_id=uuid.UUID(int=2),
        artifact_semantics_digest="1" * 64,
        review_package_digest="2" * 64,
    )
    assert len({base, changed_s, changed_r, changed_target}) == 4


# ---------------------------------------------------------------------------
# Proof verification -- real Ed25519 signatures throughout.
# ---------------------------------------------------------------------------


def test_exact_principal_valid_signature_verifies_and_records_the_verifiers_own_principal() -> None:
    private, public = _keypair()
    verifier = _verifier(public_key=public, principal_subject="verifier-principal-not-a-caller")
    canonical = b'{"artifact_id":"x"}'
    proof = acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=_sign(private, canonical))

    verified = acv.verify_proof(
        verifier=verifier,
        proof=proof,
        canonical_evidence_bytes=canonical,
        as_of=_NOW,
        attestation_providers={},
        signature_verifier=acv._ed25519_verify,
    )
    assert verified.approving_principal_subject == "verifier-principal-not-a-caller"
    assert verified.credential_fingerprint == "f" * 64


def test_a_wrong_key_signature_is_refused() -> None:
    _genuine, public = _keypair()
    wrong, _ = _keypair()
    verifier = _verifier(public_key=public)
    canonical = b'{"artifact_id":"x"}'
    proof = acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=_sign(wrong, canonical))
    with pytest.raises(acv.ApprovalVerificationFailed):
        acv.verify_proof(
            verifier=verifier,
            proof=proof,
            canonical_evidence_bytes=canonical,
            as_of=_NOW,
            attestation_providers={},
            signature_verifier=acv._ed25519_verify,
        )


def test_a_verifier_not_permitted_for_artifact_activation_is_refused() -> None:
    private, public = _keypair()
    verifier = _verifier(public_key=public, allowed_evidence_types=frozenset({"exception_approval"}))
    canonical = b'{"artifact_id":"x"}'
    proof = acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=_sign(private, canonical))
    with pytest.raises(acv.ApprovalVerificationFailed, match="not permitted"):
        acv.verify_proof(
            verifier=verifier,
            proof=proof,
            canonical_evidence_bytes=canonical,
            as_of=_NOW,
            attestation_providers={},
            signature_verifier=acv._ed25519_verify,
        )


def test_a_revoked_verifier_is_refused() -> None:
    private, public = _keypair()
    verifier = _verifier(public_key=public, revoked_at=_NOW - datetime.timedelta(minutes=1))
    canonical = b'{"artifact_id":"x"}'
    proof = acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=_sign(private, canonical))
    with pytest.raises(acv.ApprovalVerificationFailed, match="revoked"):
        acv.verify_proof(
            verifier=verifier,
            proof=proof,
            canonical_evidence_bytes=canonical,
            as_of=_NOW,
            attestation_providers={},
            signature_verifier=acv._ed25519_verify,
        )


def test_a_provider_delegated_verifier_completes_with_a_configured_provider() -> None:
    def _provider(*, canonical_evidence: bytes, assertion_format: str, assertion_base64: str) -> tuple[str, str] | None:
        if assertion_format == "jwt" and assertion_base64 == "dHJ1c3RlZA==":
            return ("https://idp.example.test", "dynamic-subject-1")
        return None

    verifier = _verifier(
        public_key=None,
        principal_binding_kind="provider_delegated",
        principal_issuer=None,
        principal_subject=None,
        provider_id="idp-1",
        algorithm=None,
    )
    proof = acv.AttestationProofInput(provider_id="idp-1", assertion_format="jwt", assertion_base64="dHJ1c3RlZA==")
    verified = acv.verify_proof(
        verifier=verifier,
        proof=proof,
        canonical_evidence_bytes=b"{}",
        as_of=_NOW,
        attestation_providers={"idp-1": _provider},
        signature_verifier=acv._ed25519_verify,
    )
    assert verified.approving_principal_issuer == "https://idp.example.test"
    assert verified.approving_principal_subject == "dynamic-subject-1"


def test_provider_delegated_refuses_with_no_provider_configured() -> None:
    """Every deployment today: no in-process attestation provider is
    configured, matching `enrollment.py`'s own stated reality for D1."""
    verifier = _verifier(
        public_key=None,
        principal_binding_kind="provider_delegated",
        principal_issuer=None,
        principal_subject=None,
        provider_id="idp-1",
        algorithm=None,
    )
    proof = acv.AttestationProofInput(provider_id="idp-1", assertion_format="jwt", assertion_base64="ZmFrZQ==")
    with pytest.raises(acv.ApprovalVerificationFailed, match="no in-process attestation provider"):
        acv.verify_proof(
            verifier=verifier,
            proof=proof,
            canonical_evidence_bytes=b"{}",
            as_of=_NOW,
            attestation_providers={},
            signature_verifier=acv._ed25519_verify,
        )


def test_a_non_principal_bound_verifier_is_refused() -> None:
    """A pre-D1, `exception_approval`-only verifier (no principal binding
    at all) cannot vouch for this principal-bound protocol."""
    private, public = _keypair()
    verifier = _verifier(public_key=public, principal_binding_kind=None, principal_issuer=None, principal_subject=None)
    canonical = b"{}"
    proof = acv.DetachedSignatureProofInput(signature_algorithm="Ed25519", signature_base64=_sign(private, canonical))
    with pytest.raises(acv.ApprovalVerificationFailed, match="no D1 principal binding"):
        acv.verify_proof(
            verifier=verifier,
            proof=proof,
            canonical_evidence_bytes=canonical,
            as_of=_NOW,
            attestation_providers={},
            signature_verifier=acv._ed25519_verify,
        )


# ---------------------------------------------------------------------------
# `VerifierMaterial.usable_at`
# ---------------------------------------------------------------------------


def test_usable_at_windows() -> None:
    verifier = _verifier(
        public_key=b"\x00" * 32,
        valid_from=_NOW,
        valid_to=_NOW + datetime.timedelta(days=1),
    )
    assert not verifier.usable_at(_NOW - datetime.timedelta(seconds=1))
    assert verifier.usable_at(_NOW)
    assert verifier.usable_at(_NOW + datetime.timedelta(hours=12))
    assert not verifier.usable_at(_NOW + datetime.timedelta(days=1))  # equality at the boundary refuses


def test_usable_at_revocation_is_immediate() -> None:
    verifier = _verifier(public_key=b"\x00" * 32, revoked_at=_NOW)
    assert not verifier.usable_at(_NOW)
    assert verifier.usable_at(_NOW - datetime.timedelta(seconds=1))


# ---------------------------------------------------------------------------
# Idempotency digests.
# ---------------------------------------------------------------------------


def test_idempotency_scope_digest_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "issuer": "https://idp.example.test",
        "subject": "actor-1",
        "proposal_id": uuid.UUID(int=1),
        "proposal_version": 1,
        "idempotency_key": "key-1",
    }
    assert acv.idempotency_scope_digest(**kwargs) == acv.idempotency_scope_digest(**kwargs)


@pytest.mark.parametrize(
    "override",
    [
        {"issuer": "https://idp.other.test"},
        {"subject": "actor-2"},
        {"proposal_id": uuid.UUID(int=2)},
        {"proposal_version": 2},
        {"idempotency_key": "key-2"},
    ],
)
def test_idempotency_scope_digest_changes_with_any_field(override: dict[str, Any]) -> None:
    base_kwargs: dict[str, Any] = {
        "issuer": "https://idp.example.test",
        "subject": "actor-1",
        "proposal_id": uuid.UUID(int=1),
        "proposal_version": 1,
        "idempotency_key": "key-1",
    }
    changed_kwargs = {**base_kwargs, **override}
    assert acv.idempotency_scope_digest(**base_kwargs) != acv.idempotency_scope_digest(**changed_kwargs)


def test_request_payload_digest_changes_with_the_verifier() -> None:
    a = acv.request_payload_digest(approval_verifier_id="verifier-a")
    b = acv.request_payload_digest(approval_verifier_id="verifier-b")
    assert a != b
    assert a == acv.request_payload_digest(approval_verifier_id="verifier-a")


# ---------------------------------------------------------------------------
# Structural wiring: this module used to ship these same two tests
# asserting the *opposite* of what they assert below -- "no production
# container or router references ApprovalChallengeService in this commit"
# -- because nothing injected the required `review_package_service`
# collaborator yet. That injection has since landed, so the sentinel these
# tests protected is now false by design, not by regression: the service
# IS wired. What follows
# proves the two protections that dormancy used to guarantee for free still
# hold now that it is real -- the reference exists in exactly the files this
# task's own contract names, and nowhere else; and no standalone `/approve`
# route exists (a second-writer/second-approve-path defect would show up as
# *more* references than expected, or an extra route, not as this test
# failing to compile).
# ---------------------------------------------------------------------------


def _registry_package_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2] / "contextplane"


def _find_references(scan_roots: list[pathlib.Path]) -> dict[str, list[str]]:
    """`ApprovalChallengeService` name/attribute references and
    `approval_challenge` module imports, per file, across *scan_roots*.
    AST-based, not a text grep, matching `check_arc_approval_writers.py`'s
    own anchor discipline: a reference has to be an actual `Name`/
    `Attribute`/`alias`/`ImportFrom.module`, not a comment or docstring that
    merely discusses either name.
    """
    watched = ("ApprovalChallengeService", "approval_challenge")
    hits: dict[str, list[str]] = {}
    for root in scan_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name) and node.id in watched:
                    name = node.id
                elif isinstance(node, ast.Attribute) and node.attr in watched:
                    name = node.attr
                elif isinstance(node, ast.alias):
                    # Check the alias's own original name -- catches `import
                    # approval_challenge as ac`, where every later reference
                    # in the file spells it "ac" and never repeats the
                    # original name again. `node.asname` is also checked so
                    # `import approval_challenge as ApprovalChallengeService`
                    # (deliberately obtuse, but syntactically legal) cannot
                    # hide behind the same blind spot from the other side.
                    if node.name in watched:
                        name = node.name
                    elif node.asname in watched:
                        name = node.asname
                elif isinstance(node, ast.ImportFrom) and node.module and "approval_challenge" in node.module:
                    name = "approval_challenge"
                if name is not None:
                    hits.setdefault(str(path.relative_to(_registry_package_root())), []).append(
                        f"{name}@{getattr(node, 'lineno', '?')}"
                    )
    return hits


#: Exactly the files this task's own contract names as needing to construct
#: or reference `ApprovalChallengeService` -- the typed container's two
#: definition sites, the service-construction module that injects the real
#: `ReviewPackageService` into it, and the one router module that calls it.
#: Any file outside this set referencing the class is a second production
#: caller nothing in this task's design reviewed.
_EXPECTED_APPROVAL_CHALLENGE_REFERENCE_FILES = frozenset(
    {"wiring/container.py", "wiring/services.py", "api/routers/arc_approval.py"}
)


def test_production_wiring_references_approval_challenge_service_only_where_expected() -> None:
    """The service is wired -- and only in the files this task's contract
    names. A reference appearing anywhere else (a second router, a second
    wiring module) fails this test just as loudly as the old "nowhere"
    assertion would have failed the moment a premature wiring attempt
    shipped.
    """
    scan_roots = [_registry_package_root() / "wiring", _registry_package_root() / "api" / "routers"]
    hits = _find_references(scan_roots)
    referenced_files = frozenset(hits)
    assert referenced_files == _EXPECTED_APPROVAL_CHALLENGE_REFERENCE_FILES, (
        f"expected ApprovalChallengeService/approval_challenge references in exactly "
        f"{sorted(_EXPECTED_APPROVAL_CHALLENGE_REFERENCE_FILES)}, found {sorted(referenced_files)}: {hits}"
    )


def test_no_standalone_approve_route_exists_in_production_routing() -> None:
    """The protection dormancy used to guarantee as a side effect -- no way
    to reach `submitted -> approved` except through a verified completion --
    now has to be proven directly: no router in this tree registers a route
    literally named `approve`, under any proposal-version path or otherwise.
    `submitted -> approved` is exclusively a side effect of `POST /v1/arc/
    approval-challenges/{id}/complete` succeeding (`arc_approval.py`).
    """
    import contextplane.api.routers.arc_approval as arc_approval_module
    import contextplane.api.routers.arc_authoring as arc_authoring_module

    hits = [
        route.path
        for module in (arc_approval_module, arc_authoring_module)
        for route in module.router.routes
        if "approve" in route.path.lower()
    ]
    assert hits == [], f"a standalone approve-shaped route exists: {hits}"


def test_the_class_exists_and_requires_a_review_package_service() -> None:
    """The service is real (not a stub deferred to a later task) and its
    one required collaborator has no default -- constructing it without one
    is a `TypeError`, not a runtime refusal, which is the actual dormancy
    mechanism this task relies on (see the module docstring)."""
    import inspect

    signature = inspect.signature(ac.ApprovalChallengeService.__init__)
    review_package_param = signature.parameters["review_package_service"]
    assert review_package_param.default is inspect.Parameter.empty
