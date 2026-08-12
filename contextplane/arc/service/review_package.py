"""`ReviewPackageService` -- assembles the `S -> R` half of the acyclic
digest chain (`contextplane.arc.service.approval_challenge` owns the final
`R -> A` step, reusing `approval_challenge_verification.build_canonical_
evidence`) and the full review-package contents a human approver reads
before signing.

**Reuses the existing canonicalizer, does not compete with it.** Every
digest below is `sha256(canonicalize_<profile>(obj))` using the exact
functions `contextplane.arc.schemas.authoring_profiles` already exposes --
`canonicalize_artifact_semantics_v1` for `S`, `canonicalize_approval_review_
package_v1` for `R`. This module never re-implements NFC normalization,
array ordering, or key sorting; the one place that owns those rules stays
`authoring_profiles.py`. A second, independent canonicalization
implementation is a standing hazard -- two implementations of the same
rules can silently diverge on a boundary case, and every digest, the
`S -> R -> A` chain, and every signature in this subsystem depend on
byte-exact canonicalization; `tests/conformance/
test_canonicalization_agreement.py` is what catches that divergence if it
ever happens.

**Persisted digest columns are caches, not truth.** Two tables this service
reads carry a persisted digest that is a cache of something recomputable
from rows in the same or a sibling table:

- `arc_expected_impact_envelopes.envelope_digest` -- a cache of the
  canonical envelope object's own digest, recomputable from that same row
  plus `arc_expected_impact_envelope_items`.
- `arc_risk_classifications.classification` -- not a digest column, but the
  same principle: a sticky *result* of a pure function
  (`RiskClassificationService.classify`) over the frozen candidate and the
  row's own pinned `algorithm_version`, recomputable from those two inputs.

`assemble` recomputes both from their authoritative rows and *cross-checks*
the recomputation against the persisted value before using it, raising
`ReviewPackageIntegrityError` on disagreement rather than either blindly
trusting the persisted column or silently overriding it with the fresh
value. `S` itself has no persisted cache anywhere this module reads: the
candidate `arc_artifact_semantics_v1` document lives in exactly one place
(`arc_authoring_proposal_versions.semantics`), so there is nothing to
cross-check it against -- `S` is simply computed fresh, every call, which is
the strongest form of "never trust a cache" available when no cache exists.

**Submission identity is a column, not an outbox scan.**
`arc_approval_review_package_v1.submitted_by_issuer`/`submitted_by_subject`
are required fields, and `arc_authoring_proposal_versions` carries them
directly -- `submitted_by_issuer`/`submitted_by_subject` columns, written by
`materialisation.py::freeze_and_link` in the same compare-and-swap that sets
`frozen_at` and `revision_id`. `assemble` reads them off the same version row
it already loaded for `semantics`/`revision_id`, not from a second lookup.

An earlier version of this module read the submitter identity back out of
the same-transaction `arc.proposal.submitted` audit-outbox event instead,
because at the time that was the only durable record of *who* called
`submit`. That made a signed approval artifact depend on a table whose
purpose is audit, not authoritative state -- if the outbox were ever pruned,
archived, or partitioned by age, an already-approved revision's submitter
identity would become unreadable and this module would refuse to assemble
its review package at all. The column above removes that dependency
entirely: the outbox still receives the same event for audit, but nothing
in this module reads it back.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas.authoring_profile_shapes import (
    APPROVAL_REVIEW_PACKAGE_PROFILE,
    EXPECTED_IMPACT_ENVELOPE_PROFILE,
)
from contextplane.arc.schemas.authoring_profiles import (
    canonicalize_approval_review_package_v2,
    canonicalize_artifact_semantics_v1,
    canonicalize_expected_impact_envelope_v1,
    canonicalize_observation_class_predicate_v1,
)
from contextplane.arc.service.approval_challenge import ReviewPackageDigests
from contextplane.arc.service.approval_challenge_verification import build_canonical_evidence
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.queries import proposal as proposal_queries
from contextplane.arc.service.queries import provenance as provenance_queries
from contextplane.arc.service.queries import review_package as queries
from contextplane.arc.service.risk import RiskClassificationService
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.exceptions import NotFoundError, RegistryError

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReviewPackageError(RegistryError):
    """Base of every refusal this module raises."""


class ReviewPackageUnavailable(ReviewPackageError):
    """The proposal version has not been submitted yet -- there is no
    frozen candidate, sticky risk classification, frozen envelope, or
    submission identity to assemble a review package from
    (`arc_proposal_state_conflict`, 409)."""


class ReviewPackageIntegrityError(ReviewPackageError):
    """A persisted digest cache disagrees with what this service just
    recomputed from the authoritative rows behind it.

    Persisted digest columns are caches, never truth: `assemble` always
    recomputes and cross-checks before using a cached value, and a
    disagreement here means the cache and the rows it is supposed to mirror
    have drifted apart -- tampering, or a defect that wrote them
    inconsistently. Refusing here is what makes a digest-substitution proof
    observable rather than silently absorbed (`arc_operational_integrity_
    failed`, 409, "cache drift").
    """


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FieldProvenanceSummary:
    field_path: str
    provenance_class: str
    source_evidence_id: uuid.UUID | None
    source_anchor: str | None
    excerpt_digest: str | None
    author_role: str | None
    derivation_profile: str | None
    author_issuer: str | None
    author_subject: str | None


@dataclasses.dataclass(frozen=True)
class SemanticTestSummary:
    test_id: str
    passed: bool
    expected: dict[str, Any]
    actual: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class BaselineDiffChange:
    field_path: str
    change_kind: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


@dataclasses.dataclass(frozen=True)
class BaselineDiff:
    baseline_revision_id: uuid.UUID | None
    changes: tuple[BaselineDiffChange, ...]


@dataclasses.dataclass(frozen=True)
class ReachConfirmation:
    field_path: str
    confirmed: bool
    confirmed_at: datetime.datetime | None
    confirmed_by_issuer: str | None
    confirmed_by_subject: str | None


@dataclasses.dataclass(frozen=True)
class ReviewPackage:
    """Everything `GET {PV}/review-package` returns -- the three digests
    plus the review-package contents they were computed over."""

    review_package_digest: str
    artifact_semantics_digest: str
    artifact_revision_digest: str
    baseline_diff: BaselineDiff
    field_provenance: tuple[FieldProvenanceSummary, ...]
    prose_readback: str
    semantic_tests: tuple[SemanticTestSummary, ...]
    expected_impact_envelope: dict[str, Any]
    risk_classification: str
    risk_algorithm_version: str
    reach_confirmations: tuple[ReachConfirmation, ...]
    submitted_by_issuer: str
    submitted_by_subject: str


@dataclasses.dataclass(frozen=True)
class _Assembled:
    """Everything `assemble`/`get_review_package`/`get_baseline_diff` share
    -- the recomputed digests and the canonical contents they were computed
    from -- so the three public methods below never recompute the same
    thing twice."""

    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_semantics_digest: str
    review_package_digest: str
    baseline_diff: BaselineDiff
    field_provenance: tuple[FieldProvenanceSummary, ...]
    semantic_tests: tuple[SemanticTestSummary, ...]
    expected_impact_envelope: dict[str, Any]
    risk_classification: str
    risk_algorithm_version: str
    reach_confirmations: tuple[ReachConfirmation, ...]
    submitted_by_issuer: str
    submitted_by_subject: str


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    """Duplicated from `proposal.py`'s own private `_scope` rather than
    imported -- matching every service module in this package's own stated
    convention."""
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


def _rfc3339(moment: datetime.datetime) -> str:
    """RFC 3339 UTC with a literal `Z` -- duplicated from `operational_
    chain.py`'s/`enrollment.py`'s own identically-named private helper
    rather than imported, matching this package's stated convention of
    re-deriving a three-line helper instead of a cross-module import for it.
    """
    dt = moment.astimezone(datetime.UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _deterministic_digest(value: Mapping[str, Any] | dict[str, Any]) -> str:
    """`sha256` over plain sorted-key, compact-separator JSON -- the same
    "deterministic JSON" idiom `audit_outbox.py` and every `queries/*.py`
    module's own `_json()` helper already use for a value that is not one
    of the sixteen canonicalization profiles. Deliberately not a second
    canonicalizer: no NFC normalization, no set-array ordering, no digest
    chain of its own -- just a stable byte representation for values (a
    two-key `{"matched": bool}` result, a diff change list) this package
    has never needed a profile for.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Sentinel distinguishing "this key is absent" from "this key is present
#: with value `None`" in the baseline-diff comparison below -- `None` is a
#: legitimate `arc_artifact_semantics_v1` field value (e.g. `owning_tenant_
#: id`), so `dict.get(key, None)` cannot tell the two apart.
_MISSING = object()


def _wrap(value: object) -> dict[str, Any] | None:
    """`BaselineDiffChange.before`/`.after` are wire-typed `dict[str, Any] |
    None` (Appendix A.6), but a top-level `arc_artifact_semantics_v1` field
    is frequently a string, a list, or a bool -- not itself an object.
    Wrapping every non-`None` value in a one-key `{"value": ...}` envelope
    is what lets a scalar or array field's before/after state travel through
    a wire shape that only accepts objects, without inventing a second,
    looser shape for this one response.
    """
    if value is None:
        return None
    return {"value": value}


class ReviewPackageService:
    """Assembles `S`, `R`, and the full review-package contents for one
    submitted (or later) proposal version.

    `assemble` is the one method `ApprovalChallengeService` depends on (see
    that module's own `ReviewPackageService` protocol) -- it takes the
    caller's own open session and authorization is the caller's job, not
    this method's, because it always runs inside a transaction a caller
    already authorized to be in. `get_review_package`/`get_baseline_diff`
    are the public, router-facing surface: they open their own session and
    check read authorization themselves, matching every other read-only ARC
    service method's convention.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, authorization: ArcAuthorizationService
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization

    # -- the collaborator ApprovalChallengeService depends on ------------------

    async def assemble(
        self, session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
    ) -> ReviewPackageDigests:
        assembled = await self._assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)
        return ReviewPackageDigests(
            artifact_semantics_digest=assembled.artifact_semantics_digest,
            review_package_digest=assembled.review_package_digest,
        )

    # -- the router-facing reads ------------------------------------------------

    async def get_review_package(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> ReviewPackage:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            assembled = await self._assemble(session, proposal_id=proposal_id, proposal_version=proposal_version)

        revision_digest = hashlib.sha256(
            build_canonical_evidence(
                artifact_id=assembled.artifact_id,
                revision_id=assembled.revision_id,
                artifact_semantics_digest=assembled.artifact_semantics_digest,
                review_package_digest=assembled.review_package_digest,
            )
        ).hexdigest()

        return ReviewPackage(
            review_package_digest=assembled.review_package_digest,
            artifact_semantics_digest=assembled.artifact_semantics_digest,
            artifact_revision_digest=revision_digest,
            baseline_diff=assembled.baseline_diff,
            field_provenance=assembled.field_provenance,
            prose_readback=_prose_readback(assembled),
            semantic_tests=assembled.semantic_tests,
            expected_impact_envelope=assembled.expected_impact_envelope,
            risk_classification=assembled.risk_classification,
            risk_algorithm_version=assembled.risk_algorithm_version,
            reach_confirmations=assembled.reach_confirmations,
            submitted_by_issuer=assembled.submitted_by_issuer,
            submitted_by_subject=assembled.submitted_by_subject,
        )

    async def get_baseline_diff(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> BaselineDiff:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            if version.semantics is None:
                raise ReviewPackageUnavailable(
                    f"proposal version {proposal_id}/{proposal_version} has no persisted candidate yet"
                )
            return await self._baseline_diff(session, dict(version.semantics))

    # -- shared assembly --------------------------------------------------------

    async def _assemble(self, session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int) -> _Assembled:
        version = await proposal_queries.load_version(session, proposal_id, proposal_version)
        if version is None:
            raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
        if version.semantics is None or version.revision_id is None:
            msg = (
                f"proposal version {proposal_id}/{proposal_version} has not been submitted -- there is no "
                "frozen candidate or bound revision to assemble a review package from"
            )
            raise ReviewPackageUnavailable(msg)
        candidate = dict(version.semantics)

        # S -- always computed fresh from the one place the candidate lives.
        # No persisted cache exists to trust or distrust here.
        artifact_semantics_digest = hashlib.sha256(canonicalize_artifact_semantics_v1(dict(candidate))).hexdigest()

        provenance_rows = await provenance_queries.load_field_provenance(session, proposal_id, proposal_version)
        field_provenance = tuple(
            FieldProvenanceSummary(
                field_path=row.field_path,
                provenance_class=row.provenance_class,
                source_evidence_id=row.source_evidence_id,
                source_anchor=row.source_anchor,
                excerpt_digest=row.excerpt_digest,
                author_role=row.author_role,
                derivation_profile=row.derivation_profile,
                author_issuer=row.author_issuer,
                author_subject=row.author_subject,
            )
            for row in provenance_rows
        )
        provenance_summary = [
            {
                "field_path": r.field_path,
                "provenance_class": r.provenance_class,
                "evidence_digest": r.excerpt_digest,
                "author_issuer": r.author_issuer,
                "author_subject": r.author_subject,
            }
            for r in field_provenance
        ]

        test_rows = await provenance_queries.load_semantic_tests(session, proposal_id, proposal_version)
        semantic_tests = tuple(
            SemanticTestSummary(test_id=row.test_id, passed=row.passed, expected=row.expected, actual=row.actual)
            for row in test_rows
        )
        semantic_tests_summary = [
            {
                "test_id": row.test_id,
                "canonical_input_digest": hashlib.sha256(_observation_class_predicate_bytes(row.manifest)).hexdigest(),
                "expected_result_digest": _deterministic_digest(row.expected),
                "actual_result_digest": _deterministic_digest(row.actual),
                "passed": row.passed,
            }
            for row in test_rows
        ]

        risk_row = await queries.load_risk_classification(session, proposal_id, proposal_version)
        if risk_row is None:
            msg = f"proposal version {proposal_id}/{proposal_version} has no sticky risk classification yet"
            raise ReviewPackageUnavailable(msg)
        fresh_risk = RiskClassificationService().classify(candidate, reducer_version=risk_row.algorithm_version)
        if fresh_risk.classification != risk_row.classification:
            msg = (
                f"proposal version {proposal_id}/{proposal_version}: recomputed risk classification "
                f"{fresh_risk.classification!r} disagrees with the sticky persisted "
                f"{risk_row.classification!r} under the same pinned algorithm version "
                f"{risk_row.algorithm_version!r}"
            )
            raise ReviewPackageIntegrityError(msg)

        envelope_row = await queries.load_envelope(session, proposal_id, proposal_version)
        if envelope_row is None:
            msg = f"proposal version {proposal_id}/{proposal_version} has no frozen expected-impact envelope yet"
            raise ReviewPackageUnavailable(msg)
        envelope_obj: dict[str, Any] = {
            "profile": EXPECTED_IMPACT_ENVELOPE_PROFILE,
            "envelope_id": str(envelope_row.envelope_id),
            "proposal_id": str(proposal_id),
            "proposal_version": proposal_version,
            "items": [
                {
                    "item_id": item.item_id,
                    "delta_code": item.delta_code,
                    "class_predicate": dict(item.class_predicate),
                    "minimum_count": item.minimum_count,
                    "maximum_count": item.maximum_count,
                    "rationale_code": item.rationale_code,
                }
                for item in envelope_row.items
            ],
            "author_issuer": envelope_row.author_issuer,
            "author_subject": envelope_row.author_subject,
            "created_at": _rfc3339(envelope_row.created_at),
        }
        fresh_envelope_digest = hashlib.sha256(canonicalize_expected_impact_envelope_v1(envelope_obj)).hexdigest()
        if fresh_envelope_digest != envelope_row.envelope_digest:
            msg = (
                f"proposal version {proposal_id}/{proposal_version}: recomputed expected-impact-envelope "
                f"digest {fresh_envelope_digest!r} disagrees with the persisted "
                f"{envelope_row.envelope_digest!r}"
            )
            raise ReviewPackageIntegrityError(msg)
        # The digest embeds the *canonical* form of the envelope -- reload it
        # from the canonicalizer's own output rather than the pre-canonical
        # dict, so a caller reading `expected_impact_envelope` back sees
        # exactly what the digest was computed over.
        canonical_envelope_obj = _json_loads(canonicalize_expected_impact_envelope_v1(envelope_obj))

        # `submitted_by_issuer`/`submitted_by_subject` are set together with
        # `frozen_at`/`revision_id` by the same `freeze_and_link` compare-
        # and-swap -- the guard above already refused when `revision_id is
        # None`, so both are always set on `version` by this point for any
        # version submitted through this module's own write path. The
        # explicit check below still fails closed, rather than assuming,
        # for a version frozen before this pair of columns existed. Copied
        # into locals (rather than read off `version` again below) so the
        # `str | None` -> `str` narrowing holds across the `await` calls
        # that follow, the same reasoning `frozen_at`'s own local below uses.
        submitted_by_issuer = version.submitted_by_issuer
        submitted_by_subject = version.submitted_by_subject
        if submitted_by_issuer is None or submitted_by_subject is None:
            msg = (
                f"proposal version {proposal_id}/{proposal_version} has no recorded submission identity -- "
                "submitted_by_issuer/submitted_by_subject are unset on a version that is otherwise frozen"
            )
            raise ReviewPackageUnavailable(msg)

        baseline_diff = await self._baseline_diff(session, candidate)
        baseline_diff_obj = {
            "baseline_revision_id": str(baseline_diff.baseline_revision_id)
            if baseline_diff.baseline_revision_id is not None
            else None,
            "changes": [
                {"field_path": c.field_path, "change_kind": c.change_kind, "before": c.before, "after": c.after}
                for c in baseline_diff.changes
            ],
        }
        baseline_diff_digest = _deterministic_digest(baseline_diff_obj)

        # `frozen_at` is set together with `revision_id` by the same
        # `freeze_and_link` statement, and the guard above already refused
        # when `revision_id is None` -- so `frozen_at` is always set by this
        # point. Falling back to `created_at` rather than asserting keeps
        # this fail-closed-with-a-value instead of an unreachable-branch
        # exception if that invariant is ever weakened.
        submitted_at = version.frozen_at if version.frozen_at is not None else version.created_at

        review_package_obj: dict[str, Any] = {
            "profile": APPROVAL_REVIEW_PACKAGE_PROFILE,
            "artifact_semantics_digest": artifact_semantics_digest,
            "source_approval_evidence_digest": candidate["source_approval_evidence_digest"],
            "field_provenance": provenance_summary,
            "semantic_tests": semantic_tests_summary,
            "risk_classification": risk_row.classification,
            "risk_algorithm_version": risk_row.algorithm_version,
            "expected_impact_envelope_digest": fresh_envelope_digest,
            "baseline_diff_digest": baseline_diff_digest,
            "proposal_id": str(proposal_id),
            "proposal_version": proposal_version,
            "submitted_by_issuer": submitted_by_issuer,
            "submitted_by_subject": submitted_by_subject,
            "submitted_at": _rfc3339(submitted_at),
        }
        review_package_digest = hashlib.sha256(canonicalize_approval_review_package_v2(review_package_obj)).hexdigest()

        reach_rows = await queries.load_reach_confirmations(session, proposal_id, proposal_version)
        reach_confirmations = tuple(
            ReachConfirmation(
                field_path=row.field_path,
                confirmed=row.confirmed,
                confirmed_at=row.confirmed_at,
                confirmed_by_issuer=row.confirmed_by_issuer,
                confirmed_by_subject=row.confirmed_by_subject,
            )
            for row in reach_rows
        )

        return _Assembled(
            artifact_id=version.artifact_id,
            revision_id=version.revision_id,
            artifact_semantics_digest=artifact_semantics_digest,
            review_package_digest=review_package_digest,
            baseline_diff=baseline_diff,
            field_provenance=field_provenance,
            semantic_tests=semantic_tests,
            expected_impact_envelope=canonical_envelope_obj,
            risk_classification=risk_row.classification,
            risk_algorithm_version=risk_row.algorithm_version,
            reach_confirmations=reach_confirmations,
            submitted_by_issuer=submitted_by_issuer,
            submitted_by_subject=submitted_by_subject,
        )

    async def _baseline_diff(self, session: AsyncSession, candidate: dict[str, Any]) -> BaselineDiff:
        baseline_revision_id_raw = candidate.get("reviewed_baseline_revision_id")
        if baseline_revision_id_raw is None:
            return BaselineDiff(baseline_revision_id=None, changes=())
        baseline_revision_id = uuid.UUID(str(baseline_revision_id_raw))
        baseline = await queries.load_semantics_by_revision_id(session, baseline_revision_id)
        if baseline is None:
            # The named baseline revision exists (or once did) but this
            # service has no candidate document for it -- an already-active
            # revision materialised before this phase's proposal aggregate
            # existed, for instance. Reported as "nothing to diff against"
            # rather than a hard failure: the baseline identity itself is
            # still surfaced.
            return BaselineDiff(baseline_revision_id=baseline_revision_id, changes=())

        changes: list[BaselineDiffChange] = []
        for key in sorted(set(candidate) | set(baseline)):
            if key == "profile":
                continue
            before_value = baseline.get(key, _MISSING)
            after_value = candidate.get(key, _MISSING)
            if before_value is _MISSING:
                changes.append(
                    BaselineDiffChange(
                        field_path=f"$.{key}", change_kind="added", before=None, after=_wrap(after_value)
                    )
                )
            elif after_value is _MISSING:
                changes.append(
                    BaselineDiffChange(
                        field_path=f"$.{key}", change_kind="removed", before=_wrap(before_value), after=None
                    )
                )
            elif before_value != after_value:
                changes.append(
                    BaselineDiffChange(
                        field_path=f"$.{key}",
                        change_kind="changed",
                        before=_wrap(before_value),
                        after=_wrap(after_value),
                    )
                )
        return BaselineDiff(baseline_revision_id=baseline_revision_id, changes=tuple(changes))


def _observation_class_predicate_bytes(manifest: dict[str, Any]) -> bytes:
    """The bytes `canonical_input_digest` hashes: the test's own
    `arc_observation_class_predicate_v1` manifest, canonicalized through the
    same profile module every other digest in this file uses -- not a
    second engine for this one field."""
    return canonicalize_observation_class_predicate_v1(dict(manifest))


def _json_loads(data: bytes) -> dict[str, Any]:
    return dict(json.loads(data))


def _prose_readback(assembled: _Assembled) -> str:
    """A deterministic sentence generated from the canonical review-package
    contents this same call assembled -- never free-form, and never a
    second, independent read of the database: every value below is a field
    this method's caller already computed. Same inputs, same sentence,
    every time.
    """
    changed = sum(1 for c in assembled.baseline_diff.changes if c.change_kind == "changed")
    added = sum(1 for c in assembled.baseline_diff.changes if c.change_kind == "added")
    removed = sum(1 for c in assembled.baseline_diff.changes if c.change_kind == "removed")
    tests_passed = sum(1 for t in assembled.semantic_tests if t.passed)
    baseline_clause = (
        "no reviewed baseline"
        if assembled.baseline_diff.baseline_revision_id is None
        else f"baseline revision {assembled.baseline_diff.baseline_revision_id} "
        f"({added} added, {removed} removed, {changed} changed field(s))"
    )
    return (
        f"Risk classification {assembled.risk_classification} under algorithm "
        f"{assembled.risk_algorithm_version}. {len(assembled.field_provenance)} field-provenance record(s), "
        f"{tests_passed}/{len(assembled.semantic_tests)} semantic test(s) passed. Compared against "
        f"{baseline_clause}. Submitted by {assembled.submitted_by_issuer}/{assembled.submitted_by_subject}."
    )


__all__ = [
    "BaselineDiff",
    "BaselineDiffChange",
    "FieldProvenanceSummary",
    "ReachConfirmation",
    "ReviewPackage",
    "ReviewPackageError",
    "ReviewPackageIntegrityError",
    "ReviewPackageService",
    "ReviewPackageUnavailable",
    "SemanticTestSummary",
]
