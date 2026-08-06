"""Parity between the six by-reference vocabularies and their sources.

Six enums in `arc_authoring_enums.py` are closed **by reference** rather
than restated: `ArtifactKind`, `RevisionLifecycleState`,
`RiskClassification`, `SignatureAlgorithm`, and `DeltaCode` each already
have exactly one normative source elsewhere in this codebase, and
`ReasonCode` names a source too -- but that source turns out not to
enumerate anything (see `test_reason_code` below). "Closed by reference"
is only true while something checks the reference: without a test, the
shipped enum can gain or drop a member and nothing notices it disagreeing
with the place it claims to mirror. Every test below asserts equality in
both directions -- not "is a subset of", which would let the source grow
unnoticed, and not "contains", which would let the enum grow unnoticed --
against that one vocabulary's *actual current* source.

Two of the five Python-reachable sources are not where an earlier note on
`arc_authoring_enums.py` placed them, because a later split moved the
constants after that note was written. This file was written against
where each constant lives today, re-verified directly rather than trusted
from the note:

* `ArtifactKind`'s only source is the `ck_arc_artifacts_kind` CHECK
  constraint in the baseline migration -- a raw SQL string inside a
  triple-quoted DDL constant, not an importable Python symbol. There is
  nothing to import, so `test_artifact_kind` reads that migration's own
  source text instead of trusting a second, hand-copied list somewhere
  else.
* `RevisionLifecycleState`'s `LIFECYCLE_*` constants live in the
  artifact-integrity module, not the lifecycle-transition module the
  wire enum's docstring names -- a service-file split relocated them
  since that docstring was written. `RevisionLifecycleState` is a normal
  closed enum otherwise, so this test still imports its source.
* `SignatureAlgorithm` already derives, by import, from the
  `signature_algorithm` enum closed on the verifier-enrollment profile
  schema -- not from a registered-algorithm set in the approval-trust
  service module. That module exists (it handles verifier *revocation*)
  but declares no such set today; see `test_signature_algorithm`.

`ReasonCode` is deliberately not exercised as a closed-membership test.
Its named source -- the proposal-state transition table -- states, per
state pair, who may make the transition and what it does: prose about
authority and effect, not an enumerated list of reason-code strings. No
landed code names a concrete list either. `test_reason_code` asserts the
opposite, deliberate fact instead: that `ReasonCode` is an open `str`
alias, not a closed enum. A vocabulary described as "closed by reference"
with no reference that actually closes it is a real finding, not
something to paper over by inventing a plausible-looking list here.
"""

from __future__ import annotations

import re
from pathlib import Path

from registry.api.schemas.arc_authoring_enums import (
    ArtifactKind,
    DeltaCode,
    ReasonCode,
    RevisionLifecycleState,
    RiskClassification,
    SignatureAlgorithm,
)
from registry.arc.schemas.authoring_profile_shapes import (
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
    DELTA_CODES,
    RISK_CLASSIFICATIONS,
    SCHEMA_BY_PROFILE,
)
from registry.arc.service.artifact_integrity import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DRAFT,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_REVOKED,
    LIFECYCLE_SUPERSEDED,
)

# Matches `parents[2]` in this repo's other conformance tests that resolve
# paths from a test file two directories under the checkout root holding
# both `registry/` (the package) and `tests/`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_MIGRATION = _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0001_baseline_schema.py"

# `ck_arc_artifacts_kind` is a raw SQL CHECK constraint embedded in a
# Python triple-quoted DDL string, not an importable Python symbol -- see
# the module docstring. This pattern plus the assertion in
# `test_artifact_kind` *is* the reference check for this one vocabulary;
# nothing else in the codebase compares the two.
_ARTIFACT_KIND_CHECK = re.compile(r"ck_arc_artifacts_kind CHECK \(\s*kind IN \(([^)]+)\)")


def _quoted_literals(fragment: str) -> frozenset[str]:
    """Pull the quoted string literals out of a `'a', 'b', 'c'` fragment."""
    return frozenset(re.findall(r"'([^']*)'", fragment))


def test_artifact_kind() -> None:
    """`ArtifactKind` equals the baseline migration's `kind` CHECK constraint.

    This phase adds no new kind, so today's shipped set and today's
    migration set should already agree; the point of the test is to keep
    them agreeing if either ever moves without the other.
    """
    source = _BASELINE_MIGRATION.read_text(encoding="utf-8")
    match = _ARTIFACT_KIND_CHECK.search(source)
    assert match is not None, (
        f"ck_arc_artifacts_kind CHECK constraint not found in {_BASELINE_MIGRATION} "
        "-- the constraint name or shape changed; update the regex above"
    )
    expected = _quoted_literals(match.group(1))
    shipped = {member.value for member in ArtifactKind}
    assert shipped == expected, f"ArtifactKind {sorted(shipped)} != migration CHECK constraint {sorted(expected)}"


def test_revision_lifecycle() -> None:
    """`RevisionLifecycleState` equals the artifact-integrity module's
    `LIFECYCLE_*` constants -- the shared vocabulary both the lifecycle and
    materialisation halves of the artifact service import rather than
    define twice."""
    expected = {LIFECYCLE_DRAFT, LIFECYCLE_ACTIVE, LIFECYCLE_SUPERSEDED, LIFECYCLE_REVOKED, LIFECYCLE_EXPIRED}
    shipped = {member.value for member in RevisionLifecycleState}
    assert shipped == expected, f"RevisionLifecycleState {sorted(shipped)} != LIFECYCLE_* constants {sorted(expected)}"


def test_risk_classification() -> None:
    """`RiskClassification` equals the ten scope/mandatory risk tiers the
    authoring-profile schema layer declares -- every profile field typed
    `risk_classification` closes against this same tuple."""
    expected = frozenset(RISK_CLASSIFICATIONS)
    shipped = {member.value for member in RiskClassification}
    assert shipped == expected, f"RiskClassification {sorted(shipped)} != RISK_CLASSIFICATIONS {sorted(expected)}"


def test_signature_algorithm() -> None:
    """`SignatureAlgorithm` equals the `signature_algorithm` enum already
    closed on the verifier-enrollment profile schema.

    The wire enum's own docstring names the approval-trust service module
    as this vocabulary's eventual home, but that module owns verifier
    *revocation*, not a registered algorithm set -- there is no such set
    there today (checked directly, not assumed from the docstring). If one
    is added there, re-point this test's source import at it instead.
    """
    schema = SCHEMA_BY_PROFILE[APPROVAL_VERIFIER_ENROLLMENT_PROFILE]
    expected = frozenset(schema["properties"]["signature_algorithm"]["enum"])
    shipped = {member.value for member in SignatureAlgorithm}
    assert (
        shipped == expected
    ), f"SignatureAlgorithm {sorted(shipped)} != enrollment profile's signature_algorithm enum {sorted(expected)}"


def test_delta_code() -> None:
    """`DeltaCode` equals the five delta codes the expected-impact-envelope
    and observation-qualification profile schemas declare."""
    expected = frozenset(DELTA_CODES)
    shipped = {member.value for member in DeltaCode}
    assert shipped == expected, f"DeltaCode {sorted(shipped)} != DELTA_CODES {sorted(expected)}"


def test_reason_code() -> None:
    """`ReasonCode` stays an open `str` alias rather than a closed enum,
    because its named source states transition authority and effect in
    prose, not a concrete list of reason-code strings, and no landed code
    has materialized one either.

    This is the one test in this file that is not a source-vs-shipped
    membership comparison, because there is no enumerated source to
    compare against -- asserting that absence is the honest check. Should
    a later change introduce concrete `reason_code` string constants (a
    transition-writer service actually recording them), this assertion
    should be replaced with a real membership test against that source,
    the same shape as the other five in this file -- not left green next
    to a `ReasonCode` that has quietly become a closed enum with an
    invented member list.
    """
    assert ReasonCode.__value__ is str, (
        "ReasonCode was closed into something other than a plain `str` alias, but no source in this "
        "codebase enumerates its literal values -- closing it requires a real source, not a guess"
    )
