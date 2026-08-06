"""Structural and internal-consistency tests for the ARC authoring-surface
canonical vector fixtures.

None of these tests import the fixture generator (`generate_vectors.py`) or
run production canonicalization code -- the profile module those vectors
will eventually be checked against does not exist yet. What is checked here
is narrower and comes first: the manifest is the shape it claims to be, the
sixteen profile directories exist and hold exactly the case files the
manifest says they do, and every published `(canonical_bytes, digest,
signature)` triple is internally consistent -- recomputed from nothing but
those published bytes, never re-derived from the source object. A fixture
that had shrunk to zero cases, or a case whose digest silently drifted from
its own canonical bytes, fails one of these tests before anything downstream
gets a chance to report a clean run over an emptied or inconsistent set.

The cross-implementation claim -- that a from-scratch JavaScript
canonicalizer independently agrees with every one of these vectors -- is
`test_node_reference_verifier_agrees_with_the_manifest` below, which runs
`registry/tools/arc-reference-verifier/verify.mjs` as a subprocess and
requires exit code zero.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess  # noqa: S404 - fixed-argv call to a repo-local script below; no caller input reaches it
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures" / "arc_authoring"
_VERIFIER = _REPO_ROOT / "tools" / "arc-reference-verifier" / "verify.mjs"
_PRODUCTION_ROOT = _REPO_ROOT / "registry"

# The sixteen profile directory names this task publishes. Hardcoded rather
# than derived from whatever the manifest currently says: a manifest that
# lost profiles would otherwise validate itself by definition.
_EXPECTED_DIRS = frozenset(
    {
        "artifact_semantics_v1",
        "approval_review_package_v1",
        "artifact_revision_v1",
        "source_approval_claim_v1",
        "source_approval_evidence_v1",
        "source_verifier_attestation_v1",
        "approval_verifier_enrollment_v1",
        "approval_provider_assertion_v1",
        "operational_event_v1",
        "field_provenance_v1",
        "expected_impact_envelope_v1",
        "observation_class_predicate_v1",
        "observation_cohort_v1",
        "observation_qualification_v1",
        "observation_replay_corpus_v1",
        "actor_separation_v1",
    }
)

_CASE_KIND_VOCABULARY = frozenset({"minimal", "typical", "maximal", "negative"})
_DECISION_VOCABULARY = frozenset({"accept", "refuse"})
_EXPECTED_KEYS = frozenset(
    {
        "canonical_bytes_base64",
        "digest",
        "signing_domain",
        "signature_input_base64",
        "signature_base64",
        "decision",
        "refusal_code",
    }
)
_CASE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_manifest() -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _load_keys() -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / "keys.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Cardinality first. If this fixture set were ever emptied or truncated, this
# is the test that fails on the count -- not a downstream test that finds
# nothing to disagree with and reports a false pass.
# ---------------------------------------------------------------------------


def test_manifest_declares_all_sixteen_profiles_with_a_nonzero_case_each() -> None:
    manifest = _load_manifest()
    profiles = manifest["profiles"]
    dir_names = {Path(p["schema_path"]).parts[0] for p in profiles}
    assert (
        dir_names == _EXPECTED_DIRS
    ), f"manifest does not name exactly the sixteen expected profile directories: {dir_names}"
    for profile in profiles:
        assert len(profile["cases"]) > 0, f"{profile['profile']}: manifest declares zero cases"
        kinds = [c["kind"] for c in profile["cases"]]
        for required_kind in ("minimal", "typical", "maximal"):
            assert required_kind in kinds, f"{profile['profile']}: missing a {required_kind!r} positive vector"
        assert kinds.count("negative") >= 2, f"{profile['profile']}: fewer than two negative vectors"


def test_case_files_on_disk_match_the_manifest_count_exactly() -> None:
    """The count the manifest claims must equal the count that exists. A
    fixture directory that lost files (or gained stray ones) fails here
    before any content is even read."""
    manifest = _load_manifest()
    for profile in manifest["profiles"]:
        dir_name = Path(profile["schema_path"]).parts[0]
        profile_dir = _FIXTURE_ROOT / dir_name
        positive_files = sorted((profile_dir / "positive").glob("*.json"))
        negative_files = sorted((profile_dir / "negative").glob("*.json"))
        on_disk = len(positive_files) + len(negative_files)
        assert on_disk == len(profile["cases"]), (
            f"{profile['profile']}: manifest declares {len(profile['cases'])} case(s) but "
            f"{on_disk} case file(s) exist under {profile_dir}"
        )
        declared_paths = {c["input_path"] for c in profile["cases"]}
        on_disk_paths = {f"{dir_name}/positive/{f.name}" for f in positive_files} | {
            f"{dir_name}/negative/{f.name}" for f in negative_files
        }
        assert (
            declared_paths == on_disk_paths
        ), f"{profile['profile']}: manifest input_path set does not match the files on disk"


# ---------------------------------------------------------------------------
# (a) Manifest schema.
# ---------------------------------------------------------------------------


def test_manifest_schema_is_closed_and_internally_ordered() -> None:
    manifest = _load_manifest()
    assert isinstance(manifest["manifest_version"], int)
    assert set(manifest) == {"manifest_version", "profiles"}

    profiles = manifest["profiles"]
    literals = [p["profile"] for p in profiles]
    assert literals == sorted(literals), "profiles[] is not ordered by profile literal"
    assert len(set(literals)) == len(literals), "duplicate profile literal in manifest"

    for profile in profiles:
        assert set(profile) == {"profile", "schema_path", "cases"}
        assert profile["profile"].startswith("arc_")
        case_ids = [c["case_id"] for c in profile["cases"]]
        assert case_ids == sorted(case_ids), f"{profile['profile']}: cases[] is not ordered by case_id"
        assert len(set(case_ids)) == len(case_ids), f"{profile['profile']}: duplicate case_id"

        for case in profile["cases"]:
            assert set(case) == {"case_id", "kind", "input_path", "expected"}
            assert _CASE_ID_PATTERN.match(case["case_id"]), f"{case['case_id']!r} is not lowercase snake_case"
            assert case["kind"] in _CASE_KIND_VOCABULARY
            expected = case["expected"]
            assert (
                set(expected) == _EXPECTED_KEYS
            ), f"{profile['profile']}/{case['case_id']}: expected block has an unexpected key set"
            assert expected["decision"] in _DECISION_VOCABULARY
            if expected["decision"] == "accept":
                assert expected["refusal_code"] is None
            else:
                assert isinstance(expected["refusal_code"], str) and expected["refusal_code"]


# ---------------------------------------------------------------------------
# (b) Directory-name derivation from the profile literal.
# ---------------------------------------------------------------------------


def test_directory_name_derives_from_the_profile_literal_by_stripping_arc_prefix() -> None:
    manifest = _load_manifest()
    for profile in manifest["profiles"]:
        literal = profile["profile"]
        assert literal.startswith("arc_"), f"{literal}: every profile literal in this set carries the arc_ prefix"
        expected_dir = literal[len("arc_") :]
        actual_dir = Path(profile["schema_path"]).parts[0]
        assert (
            actual_dir == expected_dir
        ), f"{literal}: directory {actual_dir!r} does not match the stripped literal {expected_dir!r}"
        assert (_FIXTURE_ROOT / actual_dir / "schema.json").is_file()
        assert (_FIXTURE_ROOT / actual_dir / "positive").is_dir()
        assert (_FIXTURE_ROOT / actual_dir / "negative").is_dir()


# ---------------------------------------------------------------------------
# (c) Internal consistency: recompute digest and signature *only* from the
# already-published canonical bytes -- never by re-canonicalizing the source
# object. This is what would catch a digest or signature that quietly drifted
# from the bytes actually shipped, independent of how those bytes were
# produced.
# ---------------------------------------------------------------------------


def _iter_cases() -> list[tuple[str, str, dict[str, Any]]]:
    manifest = _load_manifest()
    out: list[tuple[str, str, dict[str, Any]]] = []
    for profile in manifest["profiles"]:
        for case in profile["cases"]:
            out.append((profile["profile"], case["case_id"], case))
    return out


@pytest.mark.parametrize(
    ("profile_literal", "case_id", "case"), _iter_cases(), ids=lambda v: v if isinstance(v, str) else "case"
)
def test_published_digest_and_signature_are_recomputable_from_the_published_bytes(
    profile_literal: str, case_id: str, case: dict[str, Any]
) -> None:
    del profile_literal, case_id  # carried only for readable parametrize ids
    expected = case["expected"]
    keys = _load_keys()

    if expected["canonical_bytes_base64"] is None:
        # Structural family: nothing was ever canonicalized, so nothing else
        # in the expected block may be populated either.
        assert expected["digest"] is None
        assert expected["signature_input_base64"] is None
        assert expected["signature_base64"] is None
        assert expected["signing_domain"] is None
        return

    canonical = base64.b64decode(expected["canonical_bytes_base64"])
    assert hashlib.sha256(canonical).hexdigest() == expected["digest"]

    if expected["signature_base64"] is None:
        assert expected["signature_input_base64"] is None
        return

    signing_key = keys[_profile_for_case(case)]
    domain_prefix = bytes.fromhex(signing_key["domain_prefix_hex"])
    payload = bytes.fromhex(expected["digest"]) if signing_key["sign_over"] == "digest" else canonical
    recomputed_input = domain_prefix + payload
    assert recomputed_input == base64.b64decode(expected["signature_input_base64"])

    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(signing_key["public_key_base64"]))
    signature = base64.b64decode(expected["signature_base64"])
    try:
        public_key.verify(signature, recomputed_input)
        verified = True
    except InvalidSignature:
        verified = False

    if expected["decision"] == "accept":
        assert verified, "an accepted case's published signature must verify against the published key"
    else:
        # Semantic-negative family: the signature is deliberately wrong
        # (wrong domain, wrong key, or stale over tampered content) -- a
        # fixture where it accidentally verified would be testing nothing.
        assert (
            not verified
        ), "a semantic-negative case's signature must NOT verify -- otherwise the vector proves nothing"


def _profile_for_case(case: dict[str, Any]) -> str:
    """The manifest's own case entries do not carry their parent profile
    literal (only `input_path`, which starts with the directory name) --
    this recovers it the same way `keys.json` is keyed, by re-deriving the
    literal from the directory name in `input_path`."""
    dir_name = case["input_path"].split("/", 1)[0]
    return f"arc_{dir_name}"


# ---------------------------------------------------------------------------
# NFC / NUL-freedom of every string actually published. A fixture that
# accidentally shipped non-canonical text in a *positive* case would defeat
# the entire canonicalization contract, so this is checked directly rather
# than only implied by the case's `decision`.
# ---------------------------------------------------------------------------


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _walk_strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _walk_strings(item)]
    return []


def test_every_positive_case_input_is_nfc_and_nul_free() -> None:
    manifest = _load_manifest()
    for profile in manifest["profiles"]:
        for case in profile["cases"]:
            if case["expected"]["decision"] != "accept":
                continue
            obj = json.loads((_FIXTURE_ROOT / case["input_path"]).read_text(encoding="utf-8"))
            for s in _walk_strings(obj):
                assert (
                    unicodedata.normalize("NFC", s) == s
                ), f"{case['input_path']}: non-NFC string {s!r} in a positive case"
                assert "\x00" not in s, f"{case['input_path']}: embedded NUL in a positive case"


# ---------------------------------------------------------------------------
# (d) Agreement between the Node reference verifier and the manifest.
# ---------------------------------------------------------------------------


def test_node_reference_verifier_agrees_with_the_manifest() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH in this environment; the cross-implementation check could not run")
    result = subprocess.run(
        [node, str(_VERIFIER), str(_FIXTURE_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        "the independent Node reference verifier disagreed with the manifest:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Fixture generation is reviewable data, never a side effect of running the
# shipped application.
# ---------------------------------------------------------------------------


def test_no_production_code_path_writes_or_imports_the_fixture_generator() -> None:
    hits: list[str] = []
    for path in _PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "generate_vectors" in text or "arc_authoring/manifest.json" in text or "fixtures/arc_authoring" in text:
            hits.append(str(path.relative_to(_REPO_ROOT)))
    assert not hits, f"production code references the fixture generator or its output path: {hits}"
