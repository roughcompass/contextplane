"""Unit tests for `registry/arc/service/review_package.py`: no database.

Everything here is either (a) a mechanical assertion over the profile
shapes' own field-name sets, (b) an AST check that this module delegates
canonicalization rather than re-implementing it, (c) a ground-truth check
against the checked-in AAS-T01 fixtures, or (d) a pure-function check of the
handful of helpers that touch no session. The non-vacuous, session-backed
proofs -- persisted-digest-cache substitution at each authoritative row,
S/R/A end-to-end against real Postgres -- are `tests/integration/
test_arc_digest_chain.py`'s job; a fake session cannot prove a real
Postgres row's cached column disagrees with a fresh recomputation.
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

from registry.arc.schemas import authoring_profiles
from registry.arc.schemas.authoring_profile_shapes import (
    APPROVAL_REVIEW_PACKAGE_PROFILE,
    ARTIFACT_REVISION_PROFILE,
    ARTIFACT_SEMANTICS_PROFILE,
)
from registry.arc.service import review_package as rp
from registry.arc.service.approval_challenge import ReviewPackageDigests
from registry.arc.service.approval_challenge_verification import build_canonical_evidence

# ---------------------------------------------------------------------------
# Item 2: no profile contains its own digest or a later S -> R -> A node.
# Asserted over the canonical input objects' own closed field-name sets
# (`profile_field_names`), not by reading review_package.py's source -- the
# module under test could compute the three digests in any order internally
# and this assertion would still hold or fail on the same evidence.
# ---------------------------------------------------------------------------

_S_FIELD = "artifact_semantics_digest"
_R_FIELD = "review_package_digest"
_A_FIELD = "artifact_revision_digest"


def test_semantics_profile_names_no_digest_field_at_all() -> None:
    """`S` is semantics only: `arc_artifact_semantics_v1` names none of the
    three digest fields -- not even its own."""
    fields = authoring_profiles.profile_field_names(ARTIFACT_SEMANTICS_PROFILE)
    assert fields.isdisjoint({_S_FIELD, _R_FIELD, _A_FIELD})


def test_review_package_profile_includes_s_but_not_r_or_a() -> None:
    """`R` includes `S` (its own dependency) but names neither itself nor
    the later node `A`."""
    fields = authoring_profiles.profile_field_names(APPROVAL_REVIEW_PACKAGE_PROFILE)
    assert "artifact_semantics_digest" in fields
    assert fields.isdisjoint({_R_FIELD, _A_FIELD})


def test_artifact_revision_profile_includes_s_and_r_but_not_itself() -> None:
    """`A` includes `S` and `R` (both earlier nodes) but never itself."""
    fields = authoring_profiles.profile_field_names(ARTIFACT_REVISION_PROFILE)
    assert "artifact_semantics_digest" in fields
    assert "review_package_digest" in fields
    assert _A_FIELD not in fields


# ---------------------------------------------------------------------------
# Reuses the existing canonicalizer, ground-truthed against the checked-in
# fixtures -- the same two-part proof AAS-T14 applied to
# `build_canonical_evidence`.
# ---------------------------------------------------------------------------


def _fixtures_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "arc_authoring"


def _load_fixture_case(profile: str, case_id: str) -> dict[str, Any]:
    manifest = json.loads((_fixtures_root() / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["profiles"]:
        if entry["profile"] != profile:
            continue
        for case in entry["cases"]:
            if case["case_id"] == case_id:
                input_obj = json.loads((_fixtures_root() / case["input_path"]).read_text(encoding="utf-8"))
                return {"expected": case["expected"], "input": input_obj}
    raise AssertionError(f"no {case_id!r} case for profile {profile!r} in the manifest")


def test_review_package_module_delegates_every_digest_to_authoring_profiles() -> None:
    """AST inspection of this module's own source, not behavioral
    coincidence: every canonicalization call it makes is a call straight
    through to `authoring_profiles.canonicalize_*`. A future edit that
    inlines a hand-rolled JSON dump for one of the three profile digests
    instead fails this test rather than silently becoming a third
    canonicalization engine (the standing hazard `AAS-T30` records).
    """
    source = pathlib.Path(rp.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called_names = {
        node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "canonicalize_artifact_semantics_v1" in called_names
    assert "canonicalize_approval_review_package_v1" in called_names
    assert "canonicalize_expected_impact_envelope_v1" in called_names
    assert "canonicalize_observation_class_predicate_v1" in called_names


def test_s_ground_truth_matches_the_authoritative_fixture() -> None:
    """`S = sha256(canonicalize_artifact_semantics_v1(candidate))` computed
    over the exact `typical` fixture input equals that fixture's own
    checked-in digest -- the same formula `ReviewPackageService._assemble`
    applies to `arc_authoring_proposal_versions.semantics`.
    """
    fixture = _load_fixture_case(ARTIFACT_SEMANTICS_PROFILE, "typical")
    canonical_bytes = authoring_profiles.canonicalize_artifact_semantics_v1(dict(fixture["input"]))
    assert base64.b64encode(canonical_bytes).decode("ascii") == fixture["expected"]["canonical_bytes_base64"]
    assert hashlib.sha256(canonical_bytes).hexdigest() == fixture["expected"]["digest"]


def test_r_ground_truth_matches_the_authoritative_fixture() -> None:
    """Same proof for `R`, over `arc_approval_review_package_v1`'s own
    `typical` fixture."""
    fixture = _load_fixture_case(APPROVAL_REVIEW_PACKAGE_PROFILE, "typical")
    canonical_bytes = authoring_profiles.canonicalize_approval_review_package_v1(dict(fixture["input"]))
    assert base64.b64encode(canonical_bytes).decode("ascii") == fixture["expected"]["canonical_bytes_base64"]
    assert hashlib.sha256(canonical_bytes).hexdigest() == fixture["expected"]["digest"]


def test_a_ground_truth_matches_the_authoritative_fixture_via_build_canonical_evidence() -> None:
    """`A` is computed by `approval_challenge_verification.build_canonical_
    evidence`, reused as-is by `ReviewPackageService.get_review_package` --
    ground-truthed here the same way `test_arc_approval_challenge.py`
    already does for that function directly."""
    fixture = _load_fixture_case(ARTIFACT_REVISION_PROFILE, "typical")
    raw = fixture["input"]
    canonical_bytes = build_canonical_evidence(
        artifact_id=uuid.UUID(raw["artifact_id"]),
        revision_id=uuid.UUID(raw["revision_id"]),
        artifact_semantics_digest=raw["artifact_semantics_digest"],
        review_package_digest=raw["review_package_digest"],
    )
    assert base64.b64encode(canonical_bytes).decode("ascii") == fixture["expected"]["canonical_bytes_base64"]
    assert hashlib.sha256(canonical_bytes).hexdigest() == fixture["expected"]["digest"]


# ---------------------------------------------------------------------------
# The Protocol `ApprovalChallengeService` depends on: `assemble` must return
# exactly `ReviewPackageDigests`, not a structurally similar stand-in.
# ---------------------------------------------------------------------------


def test_assemble_signature_matches_the_approval_challenge_protocol() -> None:
    import inspect

    signature = inspect.signature(rp.ReviewPackageService.assemble)
    assert signature.return_annotation in ("ReviewPackageDigests", ReviewPackageDigests)
    params = signature.parameters
    assert "session" in params
    assert "proposal_id" in params
    assert "proposal_version" in params


# ---------------------------------------------------------------------------
# Pure helpers -- no session, no database.
# ---------------------------------------------------------------------------


def test_wrap_none_stays_none() -> None:
    assert rp._wrap(None) is None


def test_wrap_scalar_and_list_values() -> None:
    assert rp._wrap("agent_only") == {"value": "agent_only"}
    assert rp._wrap([1, 2, 3]) == {"value": [1, 2, 3]}
    assert rp._wrap(False) == {"value": False}


def test_rfc3339_matches_the_established_convention() -> None:
    """Byte-identical to `operational_chain.py`'s own private `_rfc3339` --
    both are independent copies of the same three-line helper, and this
    proves they still agree."""
    from registry.arc.service.operational_chain import _rfc3339 as chain_rfc3339

    moment = datetime.datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=datetime.UTC)
    assert rp._rfc3339(moment) == chain_rfc3339(moment)
    assert rp._rfc3339(moment) == "2026-03-01T12:30:45.123456Z"

    whole_second = datetime.datetime(2026, 3, 1, 12, 30, 45, tzinfo=datetime.UTC)
    assert rp._rfc3339(whole_second) == "2026-03-01T12:30:45Z"


def test_deterministic_digest_is_order_independent_and_sensitive_to_content() -> None:
    a = rp._deterministic_digest({"matched": True, "extra": 1})
    b = rp._deterministic_digest({"extra": 1, "matched": True})
    c = rp._deterministic_digest({"matched": False, "extra": 1})
    assert a == b, "key order must not affect the digest"
    assert a != c, "changed content must change the digest"


def test_observation_class_predicate_digest_delegates_to_authoring_profiles() -> None:
    manifest = {
        "profile": "arc_observation_class_predicate_v1",
        "task_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }
    direct = authoring_profiles.canonicalize_observation_class_predicate_v1(dict(manifest))
    assert rp._observation_class_predicate_bytes(manifest) == direct


@pytest.mark.asyncio
async def test_baseline_diff_with_no_reviewed_baseline_short_circuits_before_touching_a_session() -> None:
    """`candidate` names no `reviewed_baseline_revision_id` -> the early
    return fires before `_baseline_diff` ever reads *session*, so passing
    `None` for it here is safe and proves the short-circuit is real (a
    session read would raise `AttributeError` on `None` immediately).
    """
    service = rp.ReviewPackageService.__new__(rp.ReviewPackageService)
    diff = await service._baseline_diff(None, {"reviewed_baseline_revision_id": None})  # type: ignore[arg-type]
    assert diff.baseline_revision_id is None
    assert diff.changes == ()
