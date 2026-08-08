"""`QualificationService`: the ADR 041 Sec.6 decision computation and
Sec.3 acceptance-actor rules.

**Why the live-traffic branch is honestly inert this phase.** Sec.5 says
observation "evaluates every eligible production context-resolution
request during the frozen window" -- but no raw manifest is ever stored
anywhere in this codebase (`arc_receipts.manifest_fingerprint` is a
fingerprint, not the manifest), so a live-traffic count can only ever be
produced by evaluating a manifest *while it is in memory*, i.e. inline
with `/v1/arc/resolve`. Wiring that is explicitly this task's own
non-goal, so `compute` reads whatever `arc_observation_results` aggregate
already exists (zero rows on every deployment today) and applies the
decision algorithm to that honestly -- which is why every real candidate
this phase ships either reaches its seven-day cap `insufficient` or falls
through to the replay path below. That path *is* fully real: `generate_
corpus`/`execute_corpus` run entirely in-process, need no live traffic at
all, and are exercised end-to-end by this task's own integration tests.

**The cohort's own digest closes the loop into the qualification's binding
tuple.** `_cohort_digest` canonicalizes the frozen `arc_observation_
cohort_v1` object through the one profile engine (`authoring_profiles.
canonicalize_observation_cohort_v1`) the same way `envelope.py`/`review_
package.py` already do for their own profiles -- this is the `cohort_
digest` component of the eight-column binding tuple, not a second,
independent digest.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.schemas.authoring_profiles import (
    canonicalize_expected_impact_envelope_v1,
    canonicalize_observation_cohort_v1,
)
from registry.arc.service.approval_challenge import ReviewPackageDigests
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.queries import observation as obs_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries import qualification as qual_queries
from registry.arc.service.queries import review_package as rp_queries
from registry.arc.service.replay_corpus import ReplayCorpusService, execute_corpus, generate_corpus
from registry.arc.service.review_package import ReviewPackageService
from registry.arc.service.selection import SELECTION_ENGINE_VERSION
from registry.arc.service.shadow import ShadowService
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.exceptions import ConflictError, NotFoundError, RegistryError
from registry.types import Clock

#: This deployment's selection-configuration profile identity -- see
#: `selection.py::selection_config_digest`'s own docstring: there is no
#: tunable configuration axis in this codebase today, so this is a fixed
#: label rather than a digest of something that can currently vary.
ENGINE_CONFIGURATION_VERSION = "arc_selection_config_v1"

#: `arc_observation_qualification_v1`'s own algorithm-version field.
QUALIFICATION_ALGORITHM_VERSION = "arc_observation_qualification_v1"

#: ADR 041 Sec.5's fixed sufficiency table.
_NON_GLOBAL_MANDATORY_MINIMUM = (datetime.timedelta(hours=24), 100)
_GLOBAL_MINIMUM = (datetime.timedelta(hours=72), 1000)
MAX_LIVE_WINDOW = datetime.timedelta(days=7)
ACCEPTANCE_VALIDITY = datetime.timedelta(hours=24)


class QualificationError(RegistryError):
    """Base of every refusal this module raises."""


class QualificationUnavailable(QualificationError):
    """The candidate has not been submitted, or has no sticky risk
    classification or frozen envelope yet (`arc_proposal_state_conflict`,
    409)."""


class QualificationActorInvalid(QualificationError):
    """The accepter is the submitter, or a global-mandatory acceptance did
    not name a third distinct activator identity
    (`arc_qualification_actor_invalid`, 403)."""


class ObservationInsufficient(QualificationError):
    """Accept was called against a non-positive `insufficient` decision
    (`arc_observation_insufficient`, 409)."""


class ObservationFailed(QualificationError):
    """Accept was called against a `failed` decision
    (`arc_observation_failed`, 409)."""


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


def _requires_observation(classification: str) -> bool:
    """ADR 041 Sec.1: required when any rule is global OR any rule is
    mandatory -- the classification vocabulary already folds that into
    its own ten literals, so this is "global, or mandatory-but-not-non-
    mandatory". The second half needs the explicit exclusion: every
    `..._non_mandatory` literal also ends with the substring `_mandatory`
    (it is `_non` + `_mandatory`), so a bare `.endswith("_mandatory")`
    would silently require observation for every non-mandatory
    classification too.
    """
    if classification.startswith("global"):
        return True
    return classification.endswith("_mandatory") and not classification.endswith("_non_mandatory")


def _sufficiency(classification: str) -> tuple[datetime.timedelta, int]:
    return _GLOBAL_MINIMUM if classification.startswith("global") else _NON_GLOBAL_MANDATORY_MINIMUM


async def close_due_cohort(
    session: AsyncSession, cohort: obs_queries.CohortRow, *, now: datetime.datetime
) -> obs_queries.CohortRow:
    """Close *cohort* at its first correct boundary: the planned deadline
    if sufficiency is already met by then, else the seven-day cap
    regardless of sufficiency. Never earlier -- ADR 041 Sec.5 requires
    100% coverage of whichever window actually applies, so an early close
    before that boundary would under-count it.

    Module-level (not a `QualificationService` method) so `observation_
    window_evaluator.py` can call it -- and the `sweep_open_cohorts`
    function below -- with only a session factory and a clock, never the
    full service's authorization/review-package/shadow/replay-corpus
    collaborators a background worker has no legitimate reason to hold.
    `QualificationService._maybe_close` is a one-line forwarding wrapper
    so `compute`'s inline close check runs the identical rule.
    """
    if cohort.closed_at is not None:
        return cohort
    counters = await obs_queries.load_aggregate_counters(session, cohort.cohort_id)
    _, minimum_count = _sufficiency(cohort.risk_classification)
    sufficient = counters.observed_count >= minimum_count and counters.observed_count == counters.eligible_count
    max_deadline = cohort.window_started_at + MAX_LIVE_WINDOW
    if now >= cohort.window_deadline and sufficient:
        await obs_queries.close_cohort(session, cohort.cohort_id, window_ended_at=cohort.window_deadline, closed_at=now)
    elif now >= max_deadline:
        await obs_queries.close_cohort(session, cohort.cohort_id, window_ended_at=max_deadline, closed_at=now)
    else:
        return cohort
    refreshed = await obs_queries.load_cohort(session, cohort.cohort_id)
    return refreshed if refreshed is not None else cohort


async def sweep_open_cohorts(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime.datetime, limit: int
) -> tuple[int, int]:
    """`observation_window_evaluator.py`'s own entry point: close every
    cohort whose window has reached its correct boundary, up to *limit*
    per call. Returns `(checked, closed)`.

    Exists so a cohort closes on time even when nobody happens to call
    `qualify` right at the boundary -- `compute` runs the identical
    `close_due_cohort` check inline, so a human-triggered `qualify` never
    has to wait for this worker either. Both paths share one boundary
    rule; neither can close a window earlier than the other permits.
    """
    async with session_factory() as session:
        open_cohorts = await obs_queries.list_open_cohorts(session, limit=limit)
    closed = 0
    for cohort in open_cohorts:
        async with session_factory() as session, session.begin():
            refreshed = await close_due_cohort(session, cohort, now=now)
        if refreshed.closed_at is not None:
            closed += 1
    return len(open_cohorts), closed


def _deterministic_digest(value: dict[str, Any] | list[Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rfc3339(moment: datetime.datetime) -> str:
    dt = moment.astimezone(datetime.UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _cohort_digest(cohort: obs_queries.CohortRow) -> str:
    obj = {
        "profile": "arc_observation_cohort_v1",
        "cohort_id": str(cohort.cohort_id),
        "risk_classification": cohort.risk_classification,
        "scope_predicate_digest": cohort.scope_predicate_digest,
        "tenant_membership_digest": cohort.tenant_membership_digest,
        "eligibility_predicate_digest": cohort.eligibility_predicate_digest,
        "frozen_at": _rfc3339(cohort.frozen_at),
        "window_started_at": _rfc3339(cohort.window_started_at),
        "window_deadline": _rfc3339(cohort.window_deadline),
    }
    return hashlib.sha256(canonicalize_observation_cohort_v1(obj)).hexdigest()


def _envelope_object(envelope: rp_queries.EnvelopeRow, proposal_id: uuid.UUID, proposal_version: int) -> dict[str, Any]:
    return {
        "profile": "arc_expected_impact_envelope_v1",
        "envelope_id": str(envelope.envelope_id),
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
            for item in envelope.items
        ],
        "author_issuer": envelope.author_issuer,
        "author_subject": envelope.author_subject,
        "created_at": _rfc3339(envelope.created_at),
    }


@dataclasses.dataclass(frozen=True)
class QualificationComputation:
    """Everything `QualificationResponse` needs, plus the internal fields
    `accept`/activation (a later task) also read."""

    qualification_id: uuid.UUID
    decision: str
    candidate_review_package_digest: str
    baseline_revision_id: uuid.UUID | None
    cohort_digest: str
    expected_impact_envelope_digest: str
    replay_corpus_digest: str | None
    qualification_algorithm_version: str
    computed_at: datetime.datetime
    accepted_at: datetime.datetime | None
    accepted_by_issuer: str | None
    accepted_by_subject: str | None
    expires_at: datetime.datetime | None
    reason_codes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ObservationStatus:
    cohort_id: uuid.UUID
    cohort_digest: str
    window_started_at: datetime.datetime
    window_deadline: datetime.datetime
    eligible_count: int
    observed_count: int
    counters_by_delta_code: dict[str, dict[str, int]]
    unexplained_count: int
    out_of_envelope_count: int
    computed_decision: str
    reason_codes: tuple[str, ...]


def _row_to_computation(row: qual_queries.QualificationRow) -> QualificationComputation:
    return QualificationComputation(
        qualification_id=row.qualification_id,
        decision=row.computed_decision,
        candidate_review_package_digest=row.candidate_review_package_digest,
        baseline_revision_id=row.baseline_revision_id,
        cohort_digest=row.cohort_digest,
        expected_impact_envelope_digest=row.expected_impact_envelope_digest,
        replay_corpus_digest=row.replay_corpus_digest,
        qualification_algorithm_version=row.qualification_algorithm_version,
        computed_at=row.computed_at,
        accepted_at=row.accepted_at,
        accepted_by_issuer=row.accepted_by_issuer,
        accepted_by_subject=row.accepted_by_subject,
        expires_at=row.expires_at,
        reason_codes=tuple(row.reason_codes),
    )


class QualificationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        review_package: ReviewPackageService,
        shadow: ShadowService,
        replay_corpus: ReplayCorpusService,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._review_package = review_package
        self._shadow = shadow
        self._replay_corpus = replay_corpus

    # -- cohort lifecycle -------------------------------------------------

    async def _freeze_cohort(
        self,
        session: AsyncSession,
        *,
        version: proposal_queries.VersionRow,
        family: proposal_queries.FamilyRow,
        risk_classification: str,
        applicability_baseline_digest: str,
        now: datetime.datetime,
    ) -> obs_queries.CohortRow:
        cohort_id = uuid.uuid4()
        minimum_window, _ = _sufficiency(risk_classification)
        owning_scope = "global" if family.tenant_id is None else "tenant"
        scope_predicate_digest = _deterministic_digest(
            {
                "risk_classification": risk_classification,
                "owning_scope": owning_scope,
                "target_tenant_id": str(family.tenant_id) if family.tenant_id else None,
            }
        )
        tenant_ids = (
            await obs_queries.list_active_tenant_ids(session) if family.tenant_id is None else [family.tenant_id]
        )
        tenant_membership_digest = _deterministic_digest(sorted(str(t) for t in tenant_ids))
        await obs_queries.insert_cohort(
            session,
            cohort_id=cohort_id,
            proposal_id=version.proposal_id,
            proposal_version=version.proposal_version,
            candidate_revision_id=version.revision_id,  # type: ignore[arg-type]
            risk_classification=risk_classification,
            scope_predicate_digest=scope_predicate_digest,
            tenant_membership_digest=tenant_membership_digest,
            eligibility_predicate_digest=applicability_baseline_digest,
            frozen_at=now,
            window_started_at=now,
            window_deadline=now + minimum_window,
        )
        await obs_queries.insert_cohort_members(session, cohort_id=cohort_id, tenant_ids=tenant_ids, added_at=now)
        cohort = await obs_queries.load_cohort(session, cohort_id)
        if cohort is None:  # pragma: no cover - just inserted in this same session
            msg = "freeze_cohort: cohort vanished immediately after insert"
            raise RegistryError(msg)
        return cohort

    async def _maybe_close(
        self, session: AsyncSession, cohort: obs_queries.CohortRow, *, now: datetime.datetime
    ) -> obs_queries.CohortRow:
        return await close_due_cohort(session, cohort, now=now)

    # -- computation --------------------------------------------------------

    async def compute(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> QualificationComputation:
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            if version.revision_id is None or version.semantics is None:
                raise QualificationUnavailable(
                    f"proposal version {proposal_id}/{proposal_version} has not been submitted"
                )

            risk_row = await rp_queries.load_risk_classification(session, proposal_id, proposal_version)
            if risk_row is None:
                raise QualificationUnavailable("no sticky risk classification yet")
            envelope_row = await rp_queries.load_envelope(session, proposal_id, proposal_version)
            if envelope_row is None:
                raise QualificationUnavailable("no frozen expected-impact envelope yet")

            digests = await self._review_package.assemble(
                session, proposal_id=proposal_id, proposal_version=proposal_version
            )
            envelope_obj = _envelope_object(envelope_row, proposal_id, proposal_version)
            envelope_digest = hashlib.sha256(canonicalize_expected_impact_envelope_v1(envelope_obj)).hexdigest()
            applicability_baseline_digest = _deterministic_digest(list(version.semantics.get("applicability") or ()))

            if not _requires_observation(risk_row.classification):
                # No cohort exists or is ever created for a candidate that
                # does not require observation -- `arc_observation_
                # qualifications.cohort_id` is a NOT NULL FK, so there is
                # nothing to persist here, and nothing to accept later:
                # this result is ephemeral, matching "optional, non-global
                # revisions do not require observation" at face value.
                return QualificationComputation(
                    qualification_id=uuid.uuid4(),
                    decision="qualified",
                    candidate_review_package_digest=digests.review_package_digest,
                    baseline_revision_id=version.reviewed_baseline_revision_id,
                    cohort_digest="",
                    expected_impact_envelope_digest=envelope_digest,
                    replay_corpus_digest=None,
                    qualification_algorithm_version=QUALIFICATION_ALGORITHM_VERSION,
                    computed_at=now,
                    accepted_at=None,
                    accepted_by_issuer=None,
                    accepted_by_subject=None,
                    expires_at=None,
                    reason_codes=("observation_not_required",),
                )

            cohort = await obs_queries.load_cohort_by_version(session, proposal_id, proposal_version)
            if cohort is None:
                cohort = await self._freeze_cohort(
                    session,
                    version=version,
                    family=family,
                    risk_classification=risk_row.classification,
                    applicability_baseline_digest=applicability_baseline_digest,
                    now=now,
                )
            cohort = await self._maybe_close(session, cohort, now=now)
            counters = await obs_queries.load_aggregate_counters(session, cohort.cohort_id)
            semantic_failed = await qual_queries.has_failed_semantic_test(session, proposal_id, proposal_version)
            cohort_digest = _cohort_digest(cohort)

        decision, reason_codes, replay_corpus_digest, replay_result_digest, window_ended_at = await self._decide(
            cohort=cohort,
            counters=counters,
            semantic_failed=semantic_failed,
            family=family,
            version=version,
            envelope_obj=envelope_obj,
            ctx=ctx,
            now=now,
        )

        async with self._session_factory() as session, session.begin():
            return await self._upsert(
                session,
                version=version,
                risk_row=risk_row,
                digests=digests,
                envelope_digest=envelope_digest,
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort_digest,
                window_started_at=cohort.window_started_at,
                window_ended_at=window_ended_at,
                eligible_count=counters.eligible_count,
                observed_count=counters.observed_count,
                counters_by_delta_code=[
                    {
                        "delta_code": code,
                        "explained_count": buckets["explained"],
                        "unexplained_count": buckets["unexplained"],
                    }
                    for code, buckets in sorted(counters.counters_by_delta_code.items())
                ],
                unexplained_count=counters.unexplained_count,
                out_of_envelope_count=counters.out_of_envelope_count,
                replay_corpus_digest=replay_corpus_digest,
                replay_result_digest=replay_result_digest,
                decision=decision,
                reason_codes=reason_codes,
                now=now,
            )

    async def _decide(
        self,
        *,
        cohort: obs_queries.CohortRow,
        counters: obs_queries.ResultCounters,
        semantic_failed: bool,
        family: proposal_queries.FamilyRow,
        version: proposal_queries.VersionRow,
        envelope_obj: dict[str, Any],
        ctx: ArcRequestContext,
        now: datetime.datetime,
    ) -> tuple[str, list[str], str | None, str | None, datetime.datetime]:
        window_ended_at = cohort.window_ended_at if cohort.window_ended_at is not None else now
        if counters.unexplained_count > 0 or counters.out_of_envelope_count > 0 or semantic_failed:
            codes = ["semantic_test_failed"] if semantic_failed else ["unexplained_or_out_of_envelope"]
            return "failed", codes, None, None, window_ended_at

        _, minimum_count = _sufficiency(cohort.risk_classification)
        sufficient = counters.observed_count >= minimum_count and counters.observed_count == counters.eligible_count
        if cohort.closed_at is not None and cohort.window_ended_at == cohort.window_deadline and sufficient:
            return "qualified", ["window_met", "coverage_complete"], None, None, window_ended_at

        if cohort.closed_at is None:
            return "insufficient", ["window_in_progress"], None, None, window_ended_at

        # Seven-day cap reached, still insufficient at the required window --
        # the only remaining path is an approved, matching replay corpus.
        owning_scope = "global" if family.tenant_id is None else "tenant"
        generated = generate_corpus(
            envelope_items=envelope_obj["items"],
            scope_predicate_digest=cohort.scope_predicate_digest,
            applicability_baseline_digest=cohort.eligibility_predicate_digest,
        )
        approved = await self._replay_corpus.current_corpus(
            owning_scope=owning_scope, target_tenant_id=family.tenant_id
        )
        if approved is None or approved.canonical_corpus_digest != generated.canonical_corpus_digest:
            return (
                "insufficient",
                [f"replay_corpus_pending:{generated.canonical_corpus_digest}"],
                None,
                None,
                window_ended_at,
            )

        execution = await execute_corpus(
            generated,
            shadow=self._shadow,
            tenant_id=family.tenant_id if family.tenant_id is not None else ctx.tenant_id,
            as_of=now,
            baseline_revision_id=version.reviewed_baseline_revision_id,
            candidate_revision_id=version.revision_id,  # type: ignore[arg-type]
            candidate_semantics=dict(version.semantics),  # type: ignore[arg-type]
            envelope_items=envelope_obj["items"],
        )
        if execution.unexplained_count > 0 or execution.out_of_envelope_count > 0:
            return (
                "failed",
                ["replay_failed"],
                generated.canonical_corpus_digest,
                execution.replay_result_digest,
                window_ended_at,
            )
        return (
            "qualified_low_traffic",
            ["low_traffic_replay", "replay_complete"],
            generated.canonical_corpus_digest,
            execution.replay_result_digest,
            window_ended_at,
        )

    async def _upsert(
        self,
        session: AsyncSession,
        *,
        version: proposal_queries.VersionRow,
        risk_row: rp_queries.RiskClassificationRow,
        digests: ReviewPackageDigests,
        envelope_digest: str,
        cohort_id: uuid.UUID,
        cohort_digest: str,
        window_started_at: datetime.datetime,
        window_ended_at: datetime.datetime,
        eligible_count: int,
        observed_count: int,
        counters_by_delta_code: list[dict[str, Any]],
        unexplained_count: int,
        out_of_envelope_count: int,
        replay_corpus_digest: str | None,
        replay_result_digest: str | None,
        decision: str,
        reason_codes: list[str],
        now: datetime.datetime,
    ) -> QualificationComputation:
        binding_material = "\x00".join(
            [
                digests.review_package_digest,
                str(version.reviewed_baseline_revision_id or ""),
                SELECTION_ENGINE_VERSION,
                ENGINE_CONFIGURATION_VERSION,
                cohort_digest,
                envelope_digest,
                replay_corpus_digest or "",
                QUALIFICATION_ALGORITHM_VERSION,
            ]
        )
        idempotency_key_digest = hashlib.sha256(binding_material.encode("utf-8")).hexdigest()
        row = await qual_queries.upsert_qualification(
            session,
            qualification_id=uuid.uuid4(),
            idempotency_key_digest=idempotency_key_digest,
            candidate_review_package_digest=digests.review_package_digest,
            candidate_revision_id=version.revision_id,  # type: ignore[arg-type]
            proposal_id=version.proposal_id,
            proposal_version=version.proposal_version,
            risk_classification=risk_row.classification,
            risk_algorithm_version=risk_row.algorithm_version,
            baseline_revision_id=version.reviewed_baseline_revision_id,
            selection_engine_version=SELECTION_ENGINE_VERSION,
            engine_configuration_version=ENGINE_CONFIGURATION_VERSION,
            cohort_id=cohort_id,
            cohort_digest=cohort_digest,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            eligible_count=eligible_count,
            observed_count=observed_count,
            expected_impact_envelope_digest=envelope_digest,
            counters_by_delta_code=counters_by_delta_code,
            unexplained_count=unexplained_count,
            out_of_envelope_count=out_of_envelope_count,
            replay_corpus_digest=replay_corpus_digest,
            replay_result_digest=replay_result_digest,
            qualification_algorithm_version=QUALIFICATION_ALGORITHM_VERSION,
            computed_decision=decision,
            computed_at=now,
            reason_codes=reason_codes,
        )
        return _row_to_computation(row)

    # -- status -------------------------------------------------------------

    async def get_status(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> ObservationStatus:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            cohort = await obs_queries.load_cohort_by_version(session, proposal_id, proposal_version)
            if cohort is None:
                raise NotFoundError(f"no observation cohort exists yet for {proposal_id}/{proposal_version}")
            counters = await obs_queries.load_aggregate_counters(session, cohort.cohort_id)
            qualification = await qual_queries.load_latest_qualification_for_version(
                session, proposal_id, proposal_version
            )
        return ObservationStatus(
            cohort_id=cohort.cohort_id,
            cohort_digest=_cohort_digest(cohort),
            window_started_at=cohort.window_started_at,
            window_deadline=cohort.window_deadline,
            eligible_count=counters.eligible_count,
            observed_count=counters.observed_count,
            counters_by_delta_code=counters.counters_by_delta_code,
            unexplained_count=counters.unexplained_count,
            out_of_envelope_count=counters.out_of_envelope_count,
            computed_decision=qualification.computed_decision if qualification is not None else "insufficient",
            reason_codes=tuple(qualification.reason_codes) if qualification is not None else (),
        )

    # -- acceptance -----------------------------------------------------------

    async def accept(
        self, ctx: ArcRequestContext, *, qualification_id: uuid.UUID, acknowledged_reason_codes: list[str]
    ) -> QualificationComputation:
        now = self._clock.now()
        async with self._session_factory() as session:
            row = await qual_queries.load_qualification(session, qualification_id)
            if row is None:
                raise NotFoundError(f"qualification {qualification_id} not found")
            version = await proposal_queries.load_version(session, row.proposal_id, row.proposal_version)
            if version is None:
                raise RegistryError(f"qualification {qualification_id} references a vanished proposal version")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"qualification {qualification_id} references a vanished artifact family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))

            if row.computed_decision == "failed":
                raise ObservationFailed(f"qualification {qualification_id} decision is 'failed'; nothing to accept")
            if row.computed_decision not in ("qualified", "qualified_low_traffic"):
                raise ObservationInsufficient(
                    f"qualification {qualification_id} decision is {row.computed_decision!r}, not positive yet"
                )

            # `version` is the same `arc_authoring_proposal_versions` row
            # already loaded above -- its `submitted_by_issuer`/
            # `submitted_by_subject` columns are written by the same
            # `freeze_and_link` compare-and-swap that froze this candidate,
            # so this is a read of the row already in hand, not a second
            # lookup into the audit outbox.
            if version.submitted_by_issuer is not None and (ctx.oidc_issuer, ctx.oidc_subject) == (
                version.submitted_by_issuer,
                version.submitted_by_subject,
            ):
                raise QualificationActorInvalid(
                    "the submitter may not accept the qualification of their own submission"
                )

            role = "approver"
            if row.risk_classification == "global_mandatory":
                approver = await qual_queries.load_approving_principal(session, row.candidate_revision_id)
                if approver is not None and (ctx.oidc_issuer, ctx.oidc_subject) == approver:
                    raise QualificationActorInvalid(
                        "a global-mandatory acceptance requires a third distinct activator identity, not the approver"
                    )
                role = "activator"

        accepted_at = now
        expires_at = accepted_at + ACCEPTANCE_VALIDITY
        audit_reference = f"arc.observation.qualification.accepted:{qualification_id}:{_rfc3339(accepted_at)}"
        async with self._session_factory() as session, session.begin():
            applied = await qual_queries.accept_qualification(
                session,
                qualification_id=qualification_id,
                accepted_by_issuer=ctx.oidc_issuer,
                accepted_by_subject=ctx.oidc_subject,
                accepted_by_role=role,
                accepted_at=accepted_at,
                expires_at=expires_at,
                acceptance_audit_reference=audit_reference,
            )
        if not applied:
            raise ConflictError(f"qualification {qualification_id} was already accepted")

        async with self._session_factory() as session:
            refreshed = await qual_queries.load_qualification(session, qualification_id)
        if refreshed is None:  # pragma: no cover - just accepted in this same call
            msg = "accept_qualification: row vanished immediately after acceptance"
            raise RegistryError(msg)
        return _row_to_computation(refreshed)


__all__ = [
    "ACCEPTANCE_VALIDITY",
    "ENGINE_CONFIGURATION_VERSION",
    "MAX_LIVE_WINDOW",
    "QUALIFICATION_ALGORITHM_VERSION",
    "ObservationFailed",
    "ObservationInsufficient",
    "ObservationStatus",
    "QualificationActorInvalid",
    "QualificationComputation",
    "QualificationError",
    "QualificationService",
    "QualificationUnavailable",
    "close_due_cohort",
    "sweep_open_cohorts",
]
