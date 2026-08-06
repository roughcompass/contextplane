"""`ArtifactMaterialisationService.submit` -- the ADR 040 submission
transaction: freeze a proposal version, materialise the one draft revision
it submits into, write the bijection link, record the reviewed baseline,
and audit the transition.

A separate module from `artifact_materialisation.py` for two reasons, not
one. First, cohesion: that module's `_MaterialisationMixin` composes into
`ArtifactService` (defined in `artifact.py`) and owns *registering* an
already-approved upstream revision directly; this class is the counterpart
for a revision that instead arrives through an authoring proposal, has its
own two collaborators (below) neither `ArtifactService` nor its mixin has
ever taken, and never touches `ArtifactService.__init__`. Second, the
repo-wide 800-line file ceiling: `artifact_materialisation.py` was already
substantial, and the class below plus its supporting dataclasses would have
pushed it over. Splitting by responsibility here matches the precedent the
artifact-service split itself set -- cohesion-based, not an arbitrary
line-count slice.

**Why `submit` is written in full despite being unreachable everywhere.**
Both `operational_chain_appender` and `risk_envelope_validator` default to
`None` on every deployment today, and `_require_prerequisites` refuses
before this class opens a session whenever either is missing -- see that
method and `SourceStatusService._refuse_until_appender_exists` for the
identical shape and the same reason: "refused" and "touched nothing" must
be the same fact, not two things a caller has to trust line up. A stub that
skipped straight to that refusal without ever writing the freeze, the draft
revision, the bijection link, or the audit event would look like progress
today and would be discovered only once a later task tried to call
something that was never actually built. Writing the whole transaction now,
gated by one guard clause, means the task that wires the operational-chain
appender and the task that wires the risk/envelope validator each inject
their own collaborator into this constructor -- neither needs to write or
rewrite this method's body to do it.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries import materialisation as materialisation_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries.materialisation import DraftRevision
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

#: `arc_revisions.detail_audience`'s three DB literals (`all_matched_
#: actors`, `tenant_admin_auditor`, `registered_gateway_only`) share no
#: member with `ArtifactSemantics.detail_audience`'s three wire literals
#: (`agent_only`, `human_only`, `agent_and_human`) -- the two enums close
#: different axes (audience breadth vs. agent/human readership), and
#: nothing in this codebase crosswalks them. Guessing a mapping here would
#: be inventing product intent this module has no basis for, so submission
#: writes the DB vocabulary's narrowest, most-restricted member as a named
#: placeholder rather than a silent guess. A real crosswalk (or a decision
#: that one of the two enums should not exist) is a schema-design call for
#: whichever task next touches this vocabulary pair.
_DETAIL_AUDIENCE_SHELL_DEFAULT = "registered_gateway_only"


class SubmissionPrerequisiteUnavailable(RegistryError):
    """The same-transaction collaborators `submit` requires -- the
    operational-chain appender and the risk/envelope validator -- are not
    both wired on this deployment yet (`arc_operational_integrity_pending`,
    409).

    Raised before `submit` opens a session -- see `_require_prerequisites`
    and this module's own docstring for why.
    """


class CandidateSemanticsMissing(RegistryError):
    """The version has no persisted candidate to materialise
    (`arc_proposal_validation_failed`, 422).

    `PATCH {PV}` is what persists `arc_authoring_proposal_versions.
    semantics`; a version submitted without ever having been edited has
    nothing for this transaction to turn into a revision.
    """


@dataclasses.dataclass(frozen=True)
class SubmissionResult:
    """What a won submission hands back.

    Deliberately narrow: the router re-reads the full
    `ProposalVersionResponse` shape through `ProposalService.get_version`
    afterward, matching `PATCH {PV}`'s own fresh-read convention, so this
    carries only what only this transaction knows -- the revision it just
    minted.
    """

    proposal_id: uuid.UUID
    proposal_version: int
    revision_id: uuid.UUID


def _materialisation_scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    """Duplicated from `proposal.py`'s own private `_scope` rather than
    imported: every service module in this package builds this three-line
    mapping itself (`provenance.py`, `semantic_tests.py` do the same) so
    that changing one module's scope handling is never a cross-module edit.
    """
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


def _parse_datetime(value: object) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromisoformat(str(value))


class ArtifactMaterialisationService:
    """`submit`: the ADR 040 materialisation transaction.

    Deliberately unreachable on every deployment today. Both
    `operational_chain_appender` and `risk_envelope_validator` default to
    `None`; `_require_prerequisites` refuses before this class opens a
    session whenever either is missing, so the checks and the write below
    exist and are exercised by test doubles, but no production call reaches
    them until both collaborators are wired. See the module docstring.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        operational_chain_appender: object | None = None,
        risk_envelope_validator: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._operational_chain_appender = operational_chain_appender
        self._risk_envelope_validator = risk_envelope_validator

    async def submit(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        expected_impact_envelope: object,
    ) -> SubmissionResult:
        """Freeze *proposal_version*, materialise its bound draft revision,
        write the bijection link, and audit the transition -- or refuse
        before touching the database at all. See the class docstring for
        why every deployment hits the refusal today.

        *expected_impact_envelope* is accepted but not yet used: computing
        risk and freezing the envelope row against it is a later task's
        contract, not this one's. Threading the parameter through now means
        that task injects its validator and starts consuming it -- it does
        not need to change this signature or the router that calls it.
        """
        self._require_prerequisites(proposal_id, proposal_version)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            current = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if current is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, current.artifact_id)
            if family is None:
                msg = f"proposal version {proposal_id}/{proposal_version} references a vanished artifact family"
                raise RegistryError(msg)
            self._authorization.assert_can_write_artifact(ctx, _materialisation_scope(family.tenant_id))

            candidate = current.semantics
            if candidate is None:
                msg = (
                    f"proposal version {proposal_id}/{proposal_version} has no persisted candidate to "
                    "materialise -- PATCH it before submitting"
                )
                raise CandidateSemanticsMissing(msg)
            draft = self._draft_revision(
                candidate, artifact_id=current.artifact_id, tenant_id=family.tenant_id, now=now
            )

            try:
                await materialisation_queries.insert_draft_revision(session, draft)
            except IntegrityError as exc:
                # The candidate names its own `revision_id` (see
                # `_draft_revision`'s own docstring), so a second attempt to
                # materialise the *same* candidate -- a caller retrying an
                # already-frozen version, or the losing side of a race
                # against a concurrent submit that has not committed yet --
                # collides on `arc_revisions`'s primary key before this
                # transaction ever reaches the compare-and-swap below. That
                # collision means the same thing the compare-and-swap
                # returning no row would have meant, so it is translated
                # identically rather than surfacing as a raw integrity
                # error the router has no mapping for.
                msg = f"proposal version {proposal_id}/{proposal_version} is not open, or was already frozen"
                raise ProposalStateConflict(msg) from exc
            frozen = await materialisation_queries.freeze_and_link(
                session,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                revision_id=draft.revision_id,
                now=now,
            )
            if frozen is None:
                # Rolls back the whole transaction, including the revision
                # row inserted above -- "refused" and "touched nothing"
                # holds here exactly as it does for the guard in
                # `_require_prerequisites`, just one statement later and for
                # a different reason (lost race or already-frozen row
                # rather than a missing collaborator).
                msg = f"proposal version {proposal_id}/{proposal_version} is not open, or was already frozen"
                raise ProposalStateConflict(msg)

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_PROPOSAL_SUBMITTED,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "revision_id": str(draft.revision_id),
                    "source_evidence_id": str(current.source_evidence_id),
                    # The reviewed baseline is recorded here, in the audit
                    # event, rather than on a dedicated `arc_revisions`
                    # column: there is not one, and this task adds no
                    # migration. `arc_authoring_proposal_versions.
                    # reviewed_baseline_revision_id` already carries it
                    # durably since `open_proposal`; this is the actor-
                    # attributed, submission-time record of which baseline
                    # this specific transition reviewed.
                    "reviewed_baseline_revision_id": (
                        str(current.reviewed_baseline_revision_id)
                        if current.reviewed_baseline_revision_id is not None
                        else None
                    ),
                    "submitted_by_issuer": ctx.oidc_issuer,
                    "submitted_by_subject": ctx.oidc_subject,
                },
            )

        return SubmissionResult(
            proposal_id=proposal_id, proposal_version=proposal_version, revision_id=draft.revision_id
        )

    def _require_prerequisites(self, proposal_id: uuid.UUID, proposal_version: int) -> None:
        """Raise before a session opens if either collaborator is unwired.

        Both are checked together and named together: a deployment with
        only the operational-chain appender wired still could not safely
        submit, because the materialised revision would carry no reviewed
        risk or envelope, and the reverse holds too -- so there is one
        guard, not two independent ones that could disagree about whether
        submission is safe.
        """
        if self._operational_chain_appender is None or self._risk_envelope_validator is None:
            msg = (
                f"submitting proposal version {proposal_id}/{proposal_version} requires the same-transaction "
                "operational-chain appender and risk/envelope validator this deployment has not wired yet"
            )
            raise SubmissionPrerequisiteUnavailable(msg)

    def _draft_revision(
        self,
        candidate: Mapping[str, Any],
        *,
        artifact_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        now: datetime.datetime,
    ) -> DraftRevision:
        """Map a validated, persisted `arc_artifact_semantics_v1` candidate
        onto the `arc_revisions` row it materialises into.

        Reuses the candidate's own `revision_id` rather than minting a new
        one: the candidate already names the revision identity its own
        canonical bytes describe, and the digest chain a later task binds
        approval evidence to is computed over exactly that document, so the
        materialised row and the document that gets canonicalised for
        approval must share one id, not two independently generated ones.

        Every field below is one of three things: a direct, same-meaning
        copy from the candidate (`source_system`, `source_revision_locator`,
        `content_digest` from `source_content_digest`, `content_
        classification`, `freshness_basis` from `initial_freshness_basis`,
        `review_expires_at`, `content_retention_until` derived from
        `approved_retention_floor_days`); a value with no candidate
        equivalent because it genuinely is not the candidate's concern
        (`effective_from` -- decided at activation, not authoring); or
        `detail_audience`, which `_DETAIL_AUDIENCE_SHELL_DEFAULT`'s own
        comment names as an unresolved vocabulary mismatch between this
        profile and the pre-existing `arc_revisions` schema.
        """
        revision_id = uuid.UUID(str(candidate["revision_id"]))
        candidate_artifact_id = uuid.UUID(str(candidate["artifact_id"]))
        if candidate_artifact_id != artifact_id:
            msg = (
                f"candidate declares artifact_id {candidate_artifact_id}, but this proposal version "
                f"belongs to artifact {artifact_id}"
            )
            raise RegistryError(msg)
        retention_days = int(candidate["approved_retention_floor_days"])
        return DraftRevision(
            revision_id=revision_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            source_system=str(candidate["source_system"]),
            # The family's own artifact_id is already the stable identity a
            # canonical locator names -- every family has exactly one -- so
            # this is derived rather than read from the candidate, which
            # carries no field for it.
            source_canonical_locator=f"urn:arc-authoring:artifact:{artifact_id}",
            source_revision_locator=str(candidate["source_revision_locator"]),
            content_digest=str(candidate["source_content_digest"]),
            effective_from=now,
            review_expires_at=_parse_datetime(candidate["review_expires_at"]),
            detail_audience=_DETAIL_AUDIENCE_SHELL_DEFAULT,
            freshness_basis=str(candidate["initial_freshness_basis"]),
            content_classification=str(candidate["content_classification"]),
            content_retention_until=now + datetime.timedelta(days=retention_days),
            created_at=now,
        )


__all__ = [
    "ArtifactMaterialisationService",
    "CandidateSemanticsMissing",
    "SubmissionPrerequisiteUnavailable",
    "SubmissionResult",
]
