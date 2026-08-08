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

**Why `submit` was written in full before it was reachable.** Both
`operational_chain_appender` and `risk_envelope_validator` used to default
to `None` on every deployment, and `_require_prerequisites` refuses before
this class opens a session whenever either is missing -- see that method
and `SourceStatusService._refuse_until_appender_exists` for the identical
shape and the same reason: "refused" and "touched nothing" must be the same
fact, not two things a caller has to trust line up. A stub that skipped
straight to that refusal without ever writing the freeze, the draft
revision, the bijection link, or the audit event would look like progress
without being built, and would be discovered only once a later task tried
to call something that was never actually there. Writing the whole
transaction early, gated by one guard clause, meant the task that wired the
operational-chain appender and the task that wired the risk/envelope
validator (this one) could each inject their own collaborator into this
constructor without rewriting this method's body. That second collaborator
is now wired in `wiring/services.py`, and this is the commit that lets
`submit` complete instead of refusing.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.schemas.authoring_profiles import canonicalize_artifact_semantics_v1
from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.operational_chain import (
    EVENT_INITIALIZED,
    SYSTEM_ACTOR,
    OperationalChainService,
    build_event_payload,
)
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries import materialisation as materialisation_queries
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries.materialisation import (
    DraftRevision,
    MaterialisedApplicabilityRule,
    MaterialisedDirective,
)
from registry.arc.service.risk import RiskEnvelopeValidator
from registry.arc.types import ArcRequestContext, ArcVocabularyError, AuthorityScope, parse_wire_directive_type
from registry.audit import actions
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

#: `arc_revisions.detail_audience`'s three DB literals (`all_matched_
#: actors`, `tenant_admin_auditor`, `registered_gateway_only`) share no
#: member with `ArtifactSemantics.detail_audience`'s three wire literals
#: (`agent_only`, `human_only`, `agent_and_human`) -- the two enums close
#: different axes (audience breadth vs. agent/human readership).
#:
#: **Resolved for this phase, deliberately, rather than left dangling.**
#: Checked against ADR 039, ADR 040, ADR 041, and the TDD this phase
#: implements: none of them names a crosswalk between the two vocabularies,
#: and none of the sixteen authoring profiles carries a second field this
#: value could instead be read from. Inventing a bijective mapping here
#: would be guessing product intent this module has no basis for --
#: exactly what the placeholder comment this replaces warned against. The
#: deliberate resolution is to keep writing the DB vocabulary's narrowest,
#: most-restricted member: a materialised revision fails closed on
#: audience breadth until a real crosswalk exists, rather than a guessed
#: mapping silently granting broader-than-intended read access. A real
#: crosswalk -- or a product decision that one of the two enums should not
#: exist -- is a schema-design call for a future phase, not a guess this
#: one makes silently.
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


class CandidateGovernanceRowRejected(RegistryError):
    """One of the candidate's `directives[]`/`applicability[]` entries was
    refused by `arc_directives`/`arc_applicability_rules`' own constraints,
    or by `_directive_row`'s own wire-vocabulary translation
    (`arc_proposal_validation_failed`, 422).

    Every wire `directive_type` the authoring surface accepts today --
    `citation_only` and `verify_before_action` -- has a persisted
    destination (`registry.arc.types.parse_wire_directive_type`), so this
    is no longer the routine outcome it once was. It still fires if a
    candidate document written directly (bypassing the wire schema's own
    `Literal` validation) names a `directive_type` neither literal
    translates -- most often one of the persisted-only members
    (`require`/`prohibit`/`escalate`) the authoring surface has never been
    able to author -- or if one of the candidate's rows fails
    `arc_directives`/`arc_applicability_rules`' own CHECK constraints for
    some other reason. See `_directive_row`'s own docstring for the
    translation this class wraps.
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

    Refuses before opening a session whenever either
    `operational_chain_appender` or `risk_envelope_validator` is unwired --
    see `_require_prerequisites`. Both are real on every deployment that
    wires this service through `wiring/services.py`, which is what makes
    `submit` reachable there; a caller that constructs this class directly
    with either left at its `None` default (as every guard-only test in
    this package's unit suite still does) gets the same refusal as before.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        operational_chain_appender: OperationalChainService | None = None,
        risk_envelope_validator: RiskEnvelopeValidator | None = None,
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
        expected_impact_envelope: Mapping[str, Any],
    ) -> SubmissionResult:
        """Freeze *proposal_version*, materialise its bound draft revision,
        write the bijection link, classify risk, freeze the expected-impact
        envelope, append the signed genesis operational event and its
        pending checkpoint, and audit the transition -- all in one
        transaction -- or refuse before touching the database at all if
        either collaborator is unwired. See the class docstring.

        Every write below happens inside the one `session.begin()` block:
        an exception raised anywhere in it -- a lost compare-and-swap, an
        invalid envelope, an unclassifiable candidate, an operational-chain
        integrity error -- rolls back everything this call has written so
        far, including the draft revision inserted first. There is no
        partial commit.
        """
        appender, risk_envelope_validator = self._require_prerequisites(proposal_id, proposal_version)

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
                submitted_by_issuer=ctx.oidc_issuer,
                submitted_by_subject=ctx.oidc_subject,
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

            # The candidate's own directives[]/applicability[] materialise
            # into arc_directives/arc_applicability_rules here, in the same
            # transaction as the revision row and the compare-and-swap
            # above -- a revision whose directives failed to write, or
            # never ran at all, would activate with nothing for corpus
            # assembly or selection to serve. Placed after the compare-
            # and-swap succeeds, matching the risk/envelope step
            # immediately below: a losing race never pays for writes it is
            # about to roll back anyway.
            for directive_dict in candidate.get("directives") or ():
                try:
                    await materialisation_queries.insert_directive(
                        session,
                        self._directive_row(
                            directive_dict, revision_id=draft.revision_id, artifact_id=current.artifact_id
                        ),
                    )
                except (IntegrityError, ArcVocabularyError) as exc:
                    # `ArcVocabularyError` reaches here from `_directive_row`
                    # itself -- built before `insert_directive` is ever
                    # called, but still inside this `try` -- whenever the
                    # candidate's own `directive_type` does not translate
                    # into a persisted `DirectiveType`. Folded into the same
                    # refusal as an `IntegrityError` from the database's own
                    # CHECK: both mean the same thing to a caller, one row
                    # this transaction cannot materialise, so both roll back
                    # identically rather than one surfacing as a typed
                    # domain error and the other as a raw constraint failure.
                    msg = (
                        f"directive {directive_dict.get('directive_id')!r} in proposal version "
                        f"{proposal_id}/{proposal_version} could not be materialised into arc_directives -- "
                        "see CandidateGovernanceRowRejected's own docstring for the most likely cause"
                    )
                    raise CandidateGovernanceRowRejected(msg) from exc
            for rule_dict in candidate.get("applicability") or ():
                try:
                    await materialisation_queries.insert_applicability_rule(
                        session, self._applicability_rule_row(rule_dict, revision_id=draft.revision_id, now=now)
                    )
                except IntegrityError as exc:
                    msg = (
                        f"applicability rule {rule_dict.get('rule_id')!r} in proposal version "
                        f"{proposal_id}/{proposal_version} could not be materialised into "
                        "arc_applicability_rules -- its scope/task_kinds/action_classes did not satisfy that "
                        "table's own constraints"
                    )
                    raise CandidateGovernanceRowRejected(msg) from exc

            # Risk classification + expected-impact envelope: computed and
            # persisted only after the compare-and-swap above is actually
            # won, so a losing race never pays for classification or
            # envelope validation it is about to roll back anyway. A
            # rejected envelope or an unclassifiable candidate raises here
            # and rolls back everything written above, in the same
            # transaction -- there is no path where a draft revision or a
            # frozen version survives a risk/envelope failure.
            risk_envelope_assessment = await risk_envelope_validator.assess_and_persist(
                session,
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                artifact_semantics=candidate,
                expected_impact_envelope=expected_impact_envelope,
                now=now,
            )

            # The signed genesis operational event + its pending checkpoint,
            # same transaction. `authority_evidence_digest` at the top level
            # is the artifact-semantics digest (`S`) -- unlike a cascaded
            # `freshness_downgraded` event (see `SourceStatusService._
            # authority_evidence_digest`'s own docstring for the contrast),
            # genesis has no separate determination to point at: the
            # semantics document this revision was materialised from *is*
            # the authority for its own existence.
            semantics_digest = hashlib.sha256(canonicalize_artifact_semantics_v1(dict(candidate))).hexdigest()
            await appender.append_event(
                session,
                artifact_id=current.artifact_id,
                revision_id=draft.revision_id,
                event_type=EVENT_INITIALIZED,
                actor=SYSTEM_ACTOR,
                payload=build_event_payload(
                    initial_freshness_basis=draft.freshness_basis,
                    retention_floor_days=int(candidate["approved_retention_floor_days"]),
                    legal_hold_active=False,
                    artifact_semantics_digest=semantics_digest,
                ),
                authorization_decision_reference=f"arc_proposal_submit:{proposal_id}:{proposal_version}",
                authority_evidence_digest=semantics_digest,
                idempotency_key=f"arc-proposal-submit-{proposal_id}-{proposal_version}",
            )

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
                    "risk_classification": risk_envelope_assessment.classification,
                    "risk_algorithm_version": risk_envelope_assessment.algorithm_version,
                    "expected_impact_envelope_digest": risk_envelope_assessment.envelope_digest,
                    "submitted_by_issuer": ctx.oidc_issuer,
                    "submitted_by_subject": ctx.oidc_subject,
                },
            )

        return SubmissionResult(
            proposal_id=proposal_id, proposal_version=proposal_version, revision_id=draft.revision_id
        )

    def _require_prerequisites(
        self, proposal_id: uuid.UUID, proposal_version: int
    ) -> tuple[OperationalChainService, RiskEnvelopeValidator]:
        """Raise before a session opens if either collaborator is unwired;
        otherwise hand both back narrowed to their non-`None` type.

        Both are checked together and named together: a deployment with
        only the operational-chain appender wired still could not safely
        submit, because the materialised revision would carry no reviewed
        risk or envelope, and the reverse holds too -- so there is one
        guard, not two independent ones that could disagree about whether
        submission is safe. Returning the pair (rather than leaving `submit`
        to re-read `self._operational_chain_appender`/`self._risk_envelope_
        validator` after calling this) is what lets every line below the
        call site treat both as always-present without a second, redundant
        `None` check mypy cannot otherwise narrow away.
        """
        appender = self._operational_chain_appender
        risk_envelope_validator = self._risk_envelope_validator
        if appender is None or risk_envelope_validator is None:
            msg = (
                f"submitting proposal version {proposal_id}/{proposal_version} requires the same-transaction "
                "operational-chain appender and risk/envelope validator this deployment has not wired yet"
            )
            raise SubmissionPrerequisiteUnavailable(msg)
        return appender, risk_envelope_validator

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
        comment names as the deliberate, fail-closed resolution to a
        vocabulary mismatch between this profile and the pre-existing
        `arc_revisions` schema.
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

    def _directive_row(
        self, directive: Mapping[str, Any], *, revision_id: uuid.UUID, artifact_id: uuid.UUID
    ) -> MaterialisedDirective:
        """Map one candidate `arc_artifact_semantics_v1.directives[]`
        element onto the `arc_directives` row it materialises into.

        Every field below is a direct, same-meaning copy from the
        candidate except four, each a deliberate, documented resolution
        rather than a guess:

        `directive_type` is *translated*, through `registry.arc.types.
        parse_wire_directive_type` -- the one definition `shadow.py`'s own
        `_directive_from_dict` also uses, so a candidate this module can
        materialise and one shadow evaluation can build a domain object
        from are always the same set. `verify_before_action` is a
        deliberate two-name design, not a vocabulary gap: it is the wire
        schema's self-documenting name for the same obligation the
        database persists under the shorter `verify` token, and
        `citation_only` needs no translation because the two vocabularies
        already share that literal. Anything else -- including a
        persisted-only member like `require`/`prohibit`/`escalate`, which
        the authoring surface has never been able to author -- raises
        `ArcVocabularyError`, which the call site above folds into
        `CandidateGovernanceRowRejected` alongside a database `CHECK`
        failure, rather than left to reach the database as a raw
        constraint violation.

        `conflict_key_schema_version` is *derived* from the translated
        `directive_type`, not copied. The candidate's own field of that
        name is an unrelated integer versioning tag on the wire document
        (`ArtifactDirective.conflict_key_schema_version: int` in the
        frozen component schema), while this column is a fixed TEXT
        literal the database CHECK requires (`'arc_conflict_v1'` for
        anything action-protecting, `NULL` for `citation_only`). Copying
        the wire integer into this column would fail that CHECK on every
        single directive regardless of type, so this mirrors
        `artifact_materialisation.py`'s own `_insert_directive`, which
        derives the identical literal from "does this directive carry a
        conflict key" rather than from any caller-supplied schema-version
        value -- now read off the persisted type's own `is_action_
        protecting`, so a `verify` directive gets the same conflict-key
        shape regardless of which wire literal produced it.

        `satisfaction_mode` is copied verbatim, needing no translation:
        the wire vocabulary (`authorized_retrieval`/`signed_result`) and
        the persisted one are the same closed set.

        `conflict_subject_digest` is trusted verbatim, never recomputed:
        it is part of the canonical bytes already reviewed and signed into
        this candidate's own digest chain, and recomputing it here from
        the individual conflict-key fields could silently diverge from
        what was actually approved -- exactly the divergence the digest
        chain exists to catch, not create.
        """
        persisted_type = parse_wire_directive_type(str(directive["directive_type"]))
        has_conflict_key = persisted_type.is_action_protecting
        accepted_verifier_classes = directive.get("accepted_verifier_classes")
        accepted_verifier_ids = directive.get("accepted_verifier_ids")
        return MaterialisedDirective(
            directive_id=uuid.UUID(str(directive["directive_id"])),
            revision_id=revision_id,
            artifact_id=artifact_id,
            directive_type=persisted_type.value,
            compact_statement_plaintext=str(directive["compact_statement_plaintext"]),
            source_anchor=str(directive["source_anchor"]),
            conflict_key_schema_version=("arc_conflict_v1" if has_conflict_key else None),
            conflict_key_namespace=directive.get("conflict_key_namespace"),
            conflict_key_subject_selector=directive.get("conflict_key_subject_selector"),
            conflict_key_operation=directive.get("conflict_key_operation"),
            conflict_key_action_class=directive.get("conflict_key_action_class"),
            conflict_key_target_selector=directive.get("conflict_key_target_selector"),
            conflict_key_modality=directive.get("conflict_key_modality"),
            conflict_key_constraint_operator=directive.get("conflict_key_constraint_operator"),
            conflict_key_constraint_value=directive.get("conflict_key_constraint_value"),
            conflict_subject_digest=directive.get("conflict_subject_digest"),
            satisfaction_mode=directive.get("satisfaction_mode"),
            verification_max_age_seconds=directive.get("verification_max_age_seconds"),
            accepted_verifier_classes=(
                tuple(str(v) for v in accepted_verifier_classes) if accepted_verifier_classes else None
            ),
            accepted_verifier_ids=(
                tuple(uuid.UUID(str(v)) for v in accepted_verifier_ids) if accepted_verifier_ids else None
            ),
            required_evidence_type=directive.get("required_evidence_type"),
            delegable_exception=bool(directive.get("delegable_exception", False)),
        )

    def _applicability_rule_row(
        self, rule: Mapping[str, Any], *, revision_id: uuid.UUID, now: datetime.datetime
    ) -> MaterialisedApplicabilityRule:
        """Map one candidate `applicability[]` element onto the
        `arc_applicability_rules` row it materialises into.

        No vocabulary mismatch here, unlike `_directive_row`: the
        candidate's `scope` enum already matches this table's own closed
        CHECK exactly. `effective_from` is the one field with no direct
        candidate value to trust in every case -- the profile permits
        `null` (an author declining to name a start time), but the column
        is `NOT NULL` -- so a `null` candidate value falls back to *now*,
        the same materialisation-time value the revision's own `effective_
        from` uses (`_draft_revision`), rather than inventing a distinct
        default.
        """
        effective_from = rule.get("effective_from")
        effective_until = rule.get("effective_until")
        capability_ids = rule.get("capability_ids")
        capability_labels = rule.get("capability_labels")
        domain_ids = rule.get("domain_ids")
        task_kinds = rule.get("task_kinds")
        action_classes = rule.get("action_classes")
        environments = rule.get("environments")
        data_sensitivity_tiers = rule.get("data_sensitivity_tiers")
        return MaterialisedApplicabilityRule(
            rule_id=uuid.UUID(str(rule["rule_id"])),
            revision_id=revision_id,
            scope=str(rule["scope"]),
            target_tenant_id=(uuid.UUID(str(rule["target_tenant_id"])) if rule.get("target_tenant_id") else None),
            capability_ids=(tuple(uuid.UUID(str(v)) for v in capability_ids) if capability_ids else None),
            capability_labels=(tuple(str(v) for v in capability_labels) if capability_labels else None),
            domain_ids=(tuple(str(v) for v in domain_ids) if domain_ids else None),
            task_kinds=(tuple(str(v) for v in task_kinds) if task_kinds else None),
            action_classes=(tuple(str(v) for v in action_classes) if action_classes else None),
            environments=(tuple(str(v) for v in environments) if environments else None),
            data_sensitivity_tiers=(tuple(str(v) for v in data_sensitivity_tiers) if data_sensitivity_tiers else None),
            effective_from=(_parse_datetime(effective_from) if effective_from is not None else now),
            effective_until=(_parse_datetime(effective_until) if effective_until is not None else None),
            is_mandatory=bool(rule["is_mandatory"]),
        )


__all__ = [
    "ArtifactMaterialisationService",
    "CandidateGovernanceRowRejected",
    "CandidateSemanticsMissing",
    "SubmissionPrerequisiteUnavailable",
    "SubmissionResult",
]
