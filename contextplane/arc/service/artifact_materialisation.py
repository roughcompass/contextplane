"""Bringing a revision into existence and readying it for activation.

Registering an already-approved upstream revision as a draft, and (once
real-world approval evidence exists) binding that evidence to the revision
it approves, are the two operations that create the row a later activation
will act on. Both are here, together, because they share the same "revision
does not yet bind anyone" precondition and the same draft-scoped writes.

Distinguished from `artifact.py`, which contains the transitions that move
an *existing*, already-registered revision between lifecycle states
(activation, revocation, expiry). `_MaterialisationMixin` below supplies
`register_revision` and `attach_approval_evidence` as part of the single
`ArtifactService` class that `artifact.py` assembles; splitting the class
across two files by responsibility, rather than moving the whole class,
is what lets each file stay legible on its own without changing the one
public service callers already hold.

Proposal-submission materialisation (`ArtifactMaterialisationService.
submit`) is a sibling module, `submission.py`, not a third responsibility
here: see that module's own docstring for why it is a separate file rather
than a fourth section of this one.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.approval import assert_evidence_is_trusted
from contextplane.arc.service.artifact_integrity import (
    ATTACHABLE_EVIDENCE_TYPES,
    LIFECYCLE_DRAFT,
    ArtifactLifecycleError,
    EvidenceTypeNotWritableError,
    _assert_evidence_approves,
    _load_artifact,
    _lock_family,
    applicability_digest,
    applicability_snapshot,
)
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.sensitivity import TIERS, is_tier
from contextplane.types import Clock

# Content storage modes the schema permits.
STORAGE_ENCRYPTED = "encrypted"
STORAGE_NONE = "none"


@dataclasses.dataclass(frozen=True)
class SourceIdentity:
    """Where this revision came from upstream.

    All three parts together are unique across the deployment. That is the
    mechanism behind "registration, not authoring": ARC can point at the
    exact upstream revision it is recording, and cannot record two ARC
    revisions for one upstream one.
    """

    source_system: str
    source_canonical_locator: str
    source_revision_locator: str
    content_digest: str


@dataclasses.dataclass(frozen=True)
class DirectiveDraft:
    """One directive within a revision being registered.

    `conflict_*` fields are required for every type except `citation_only`,
    which the schema also enforces. A directive that can make an action
    blocked must be comparable with other directives, and it cannot be
    compared without a conflict key.
    """

    directive_id: uuid.UUID
    directive_type: str
    source_anchor: str
    compact_statement: str
    conflict_key: dict[str, str] | None = None
    delegable_exception: bool = False


@dataclasses.dataclass(frozen=True)
class ApplicabilityDraft:
    """One rule saying who a revision applies to."""

    scope: AuthorityScope
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None = None
    is_mandatory: bool = True
    target_tenant_id: uuid.UUID | None = None
    entity_ids: tuple[uuid.UUID, ...] = ()
    domain_ids: tuple[str, ...] = ()
    intent_kinds: tuple[str, ...] = ()
    action_classes: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    data_sensitivity_tiers: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, object]:
        """The applicability a mandatory obligation retains.

        Retained rather than referenced: when the revision behind an
        obligation is revoked, the obligation must still know who it applied
        to, or a resolution that should block would find nothing to block on.
        """
        return applicability_snapshot(
            scope=str(self.scope),
            target_tenant_id=self.target_tenant_id,
            entity_ids=self.entity_ids,
            domain_ids=self.domain_ids,
            intent_kinds=self.intent_kinds,
            action_classes=self.action_classes,
            environments=self.environments,
            data_sensitivity_tiers=self.data_sensitivity_tiers,
        )

    def digest(self) -> str:
        return applicability_digest(self.snapshot())


@dataclasses.dataclass(frozen=True)
class RevisionDraft:
    """Everything one registration call records.

    Deliberately carries no `approval_evidence_id`. Approval evidence must
    name the revision it approves, and the revision id is minted inside
    `register_revision` -- so evidence that existed beforehand can only ever
    name a *different* revision. The field used to exist and was written
    straight into the row without the "approves this revision" check that
    `attach_approval_evidence` applies, which made it a way to register a
    revision citing somebody else's approval and then activate on it.
    Nothing ever set it. Evidence is attached after registration, once there
    is a revision id for it to name.
    """

    artifact_id: uuid.UUID
    source: SourceIdentity
    content_classification: str
    detail_audience: DetailAudience
    freshness_basis: str
    effective_from: datetime.datetime
    review_expires_at: datetime.datetime
    content_retention_until: datetime.datetime
    directives: tuple[DirectiveDraft, ...]
    rules: tuple[ApplicabilityDraft, ...]
    body_plaintext: str | None = None
    effective_until: datetime.datetime | None = None
    legal_hold: bool = False


@dataclasses.dataclass(frozen=True)
class RegisteredRevision:
    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    lifecycle_state: str


def _conflict_subject_digest(key: dict[str, str]) -> str:
    """The subject half of a conflict key, hashed.

    Deliberately excludes modality, operator, and value: two directives
    conflict when they govern the same *subject* and disagree, so the
    grouping key must not include the thing they disagree about.
    """
    parts = [
        key.get("namespace", ""),
        key.get("subject_selector", ""),
        key.get("operation", ""),
        key.get("action_class", ""),
        key.get("target_selector", ""),
    ]
    material = "|".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _MaterialisationMixin:
    """`register_revision` and `attach_approval_evidence`.

    Combined into `ArtifactService` (defined in `artifact.py`, which
    supplies `__init__`), not usable on its own -- it assumes its concrete
    class provides `_session_factory`, `_authorization`, and `_clock`, the
    same three collaborators `ArtifactService.__init__` sets. Declared here
    with types only, so mypy can check the methods below without needing to
    see that class.
    """

    _session_factory: async_sessionmaker[AsyncSession]
    _authorization: ArcAuthorizationService
    _clock: Clock

    # -- registration ---------------------------------------------------------

    async def register_revision(self, ctx: ArcRequestContext, draft: RevisionDraft) -> RegisteredRevision:
        """Record an already-approved upstream revision, as a draft.

        One transaction: the revision, its directives, and its rules land
        together or not at all. A revision whose directives failed to write
        would be an artifact that binds nobody while appearing registered.
        """
        self._validate(draft)
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            artifact = await _load_artifact(session, draft.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)

            revision_id = uuid.uuid4()
            try:
                await self._insert_revision(session, draft, revision_id=revision_id, now=now)
            except IntegrityError as exc:
                # The uniqueness constraint on source identity is the one that
                # makes "one upstream revision, one ARC revision" real, so a
                # duplicate is reported as a conflict rather than a crash.
                msg = (
                    f"a revision for {draft.source.source_revision_locator!r} with this content digest "
                    "is already registered"
                )
                raise ConflictError(msg) from exc

            for directive in draft.directives:
                await self._insert_directive(session, directive, revision_id=revision_id, draft=draft)
            for rule in draft.rules:
                await self._insert_rule(session, rule, revision_id=revision_id, draft=draft)

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_REGISTERED,
                payload={
                    "artifact_id": str(draft.artifact_id),
                    "revision_id": str(revision_id),
                    "source_system": draft.source.source_system,
                    "source_revision_locator": draft.source.source_revision_locator,
                    "content_digest": draft.source.content_digest,
                    "directive_count": len(draft.directives),
                    "rule_count": len(draft.rules),
                },
            )

        return RegisteredRevision(
            revision_id=revision_id, artifact_id=draft.artifact_id, lifecycle_state=LIFECYCLE_DRAFT
        )

    async def attach_approval_evidence(
        self, ctx: ArcRequestContext, revision_id: uuid.UUID, evidence_id: uuid.UUID
    ) -> None:
        """Link a revision to evidence of a type this deployment can write, after registration.

        A separate step because the ordering is forced, not chosen. Evidence
        that names a revision it approves must do so after the revision
        exists — so the evidence cannot precede its revision, and
        registration cannot demand it. Register, approve, attach, activate.

        Only `ATTACHABLE_EVIDENCE_TYPES` may be bound this way; everything
        else -- most importantly `artifact_activation`, which no production
        code in this deployment writes -- is refused before the row's
        content is even inspected. A row of a type this call refuses can
        only have reached the table through something other than a writer
        this system trusts, and binding it to a revision would let that
        origin buy activation eligibility it was never granted.

        Refuses once a revision is active: changing the evidence behind a
        rule already in force would rewrite why agents were told to obey it.
        """
        async with self._session_factory() as session, session.begin():
            revision = await _lock_family(session, revision_id)
            artifact = await _load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)

            if revision.lifecycle_state != LIFECYCLE_DRAFT:
                msg = (
                    f"revision {revision_id} is {revision.lifecycle_state!r}; approval evidence can only "
                    "be attached while it is still a draft"
                )
                raise ArtifactLifecycleError(msg)

            evidence = (
                await session.execute(
                    text(
                        "SELECT evidence_type, approved_revision_id FROM arc_approval_evidence "
                        "WHERE evidence_id = :eid"
                    ),
                    {"eid": evidence_id},
                )
            ).one_or_none()
            if evidence is None:
                msg = f"approval evidence {evidence_id} not found"
                raise NotFoundError(msg)
            if evidence.evidence_type not in ATTACHABLE_EVIDENCE_TYPES:
                msg = (
                    f"evidence_type {evidence.evidence_type!r} has no first-party writer in this "
                    "deployment and cannot be attached to a revision"
                )
                raise EvidenceTypeNotWritableError(msg)
            await _assert_evidence_approves(session, evidence_id, revision_id)
            # Refused early so an operator finds out while linking rather than
            # at activation, but it is checked again there -- trust can be
            # withdrawn in between, and activation is what puts a revision
            # into force.
            await assert_evidence_is_trusted(session, evidence_id)

            await session.execute(
                text("UPDATE arc_revisions SET approval_evidence_id = :eid WHERE revision_id = :rid"),
                {"rid": revision_id, "eid": evidence_id},
            )

    # -- validation -----------------------------------------------------------

    def _validate(self, draft: RevisionDraft) -> None:
        """Reject a draft before any row is written.

        Checked here rather than left to the database because a CHECK
        violation surfaces as an opaque constraint name, and an operator
        registering governance deserves to be told which field was wrong.
        """
        if not draft.directives:
            msg = "a revision must carry at least one directive"
            raise ValidationError(msg)
        if not draft.rules:
            msg = "a revision must carry at least one applicability rule, or it can never match anything"
            raise ValidationError(msg)
        if len(draft.source.content_digest) != 64:
            msg = "content_digest must be a 64-character SHA-256 hex digest"
            raise ValidationError(msg)
        if draft.review_expires_at <= draft.effective_from:
            msg = "review_expires_at must be after effective_from"
            raise ValidationError(msg)

        seen: set[uuid.UUID] = set()
        for directive in draft.directives:
            if directive.directive_id in seen:
                msg = f"directive {directive.directive_id} appears twice in one revision"
                raise ValidationError(msg)
            seen.add(directive.directive_id)
            if directive.directive_type != "citation_only" and not directive.conflict_key:
                msg = (
                    f"directive {directive.directive_id} is {directive.directive_type!r} and must carry a "
                    "conflict key; only citation_only directives may omit one"
                )
                raise ValidationError(msg)
            # And the reverse. Requiring a key for the other types without
            # forbidding one here left `citation_only` able to carry the
            # comparable shape, which made it able to conflict -- so a
            # copy-pasted conflict key on a directive that is meant only to be
            # cited could block every matching resolution. The schema's CHECK
            # permits it (it only constrains the action-protecting types), so
            # this is the layer that has to refuse it.
            if directive.directive_type == "citation_only" and directive.conflict_key:
                msg = (
                    f"directive {directive.directive_id} is citation_only and must not carry a conflict "
                    "key; a directive that can be compared can block an action, which is what "
                    "citation_only means it may not do"
                )
                raise ValidationError(msg)

        for rule in draft.rules:
            if rule.scope is AuthorityScope.TENANT and rule.target_tenant_id is None:
                msg = "a tenant-scoped applicability rule requires target_tenant_id"
                raise ValidationError(msg)
            if rule.scope is AuthorityScope.ENTITY and not rule.entity_ids:
                msg = "an entity-scoped applicability rule requires at least one entity id"
                raise ValidationError(msg)
            # The column is a bare `ARRAY(Text)` and nothing else checks it. A
            # misspelled tier does not widen the rule -- `_matches_scalar`
            # answers False for a value the manifest cannot carry -- it makes
            # the rule match *nothing*, which for a mandatory rule is an
            # obligation that silently stops blocking. Refused here so the typo
            # fails at authoring, where somebody is looking.
            unknown = sorted(t for t in rule.data_sensitivity_tiers if not is_tier(t))
            if unknown:
                msg = (
                    f"applicability rule names sensitivity tier(s) {unknown} that are not on the scale "
                    f"{list(TIERS)}; a rule naming a tier nothing can declare matches nothing"
                )
                raise ValidationError(msg)

    # -- row helpers ----------------------------------------------------------

    async def _insert_revision(
        self, session: AsyncSession, draft: RevisionDraft, *, revision_id: uuid.UUID, now: datetime.datetime
    ) -> None:
        storage_mode = STORAGE_NONE if draft.body_plaintext is not None else STORAGE_ENCRYPTED
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  effective_until, review_expires_at, detail_audience,"
                "  freshness_basis, content_classification, content_retention_until, legal_hold,"
                "  content_storage_mode, source_body_plaintext, created_at"
                ") SELECT :rid, :aid, a.tenant_id, :system, :locator, :rlocator, :digest, :state,"
                "         :efrom, :euntil, :review, :audience, :freshness, :classification,"
                "         :retention, :hold, :storage, :body, :now "
                "  FROM arc_artifacts a WHERE a.artifact_id = :aid"
            ),
            {
                "rid": revision_id,
                "aid": draft.artifact_id,
                "system": draft.source.source_system,
                "locator": draft.source.source_canonical_locator,
                "rlocator": draft.source.source_revision_locator,
                "digest": draft.source.content_digest,
                "state": LIFECYCLE_DRAFT,
                "efrom": draft.effective_from,
                "euntil": draft.effective_until,
                "review": draft.review_expires_at,
                "audience": str(draft.detail_audience),
                "freshness": draft.freshness_basis,
                "classification": draft.content_classification,
                "retention": draft.content_retention_until,
                "hold": draft.legal_hold,
                "storage": storage_mode,
                "body": draft.body_plaintext,
                "now": now,
            },
        )

    async def _insert_directive(
        self,
        session: AsyncSession,
        directive: DirectiveDraft,
        *,
        revision_id: uuid.UUID,
        draft: RevisionDraft,
    ) -> None:
        """Write one directive, creating its stable identity if new.

        The identity row is separate because a directive keeps one id across
        revisions -- that is what lets a successor revision be recognised as
        replacing a specific obligation rather than adding a new one.
        """
        await session.execute(
            text(
                "INSERT INTO arc_directive_identities (directive_id, artifact_id) "
                "VALUES (:did, :aid) ON CONFLICT (directive_id) DO NOTHING"
            ),
            {"did": directive.directive_id, "aid": draft.artifact_id},
        )
        key = directive.conflict_key or {}
        await session.execute(
            text(
                "INSERT INTO arc_directives ("
                "  directive_id, revision_id, tenant_id, directive_type, compact_statement_plaintext,"
                "  source_anchor, conflict_key_schema_version, conflict_key_namespace,"
                "  conflict_key_subject_selector, conflict_key_operation, conflict_key_action_class,"
                "  conflict_key_target_selector, conflict_key_modality, conflict_key_constraint_operator,"
                "  conflict_key_constraint_value, conflict_subject_digest, delegable_exception"
                ") SELECT :did, :rid, r.tenant_id, :dtype, :statement, :anchor, :schema, :ns,"
                "         :subject, :operation, :action, :target, :modality, :operator, :value,"
                "         :subject_digest, :delegable "
                "  FROM arc_revisions r WHERE r.revision_id = :rid"
            ),
            {
                "did": directive.directive_id,
                "rid": revision_id,
                "dtype": directive.directive_type,
                "statement": directive.compact_statement,
                "anchor": directive.source_anchor,
                "schema": "arc_conflict_v1" if key else None,
                "ns": key.get("namespace"),
                "subject": key.get("subject_selector"),
                "operation": key.get("operation"),
                "action": key.get("action_class"),
                "target": key.get("target_selector"),
                "modality": key.get("modality"),
                "operator": key.get("constraint_operator"),
                "value": key.get("constraint_value"),
                "subject_digest": _conflict_subject_digest(key) if key else None,
                "delegable": directive.delegable_exception,
            },
        )

    async def _insert_rule(
        self,
        session: AsyncSession,
        rule: ApplicabilityDraft,
        *,
        revision_id: uuid.UUID,
        draft: RevisionDraft,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO arc_applicability_rules ("
                "  revision_id, tenant_id, scope, target_tenant_id, entity_ids, domain_ids,"
                "  intent_kinds, action_classes, environments, data_sensitivity_tiers,"
                "  effective_from, effective_until, is_mandatory"
                ") SELECT :rid, r.tenant_id, :scope, :target, :caps, :domains, :kinds, :actions,"
                "         :envs, :tiers, :efrom, :euntil, :mandatory "
                "  FROM arc_revisions r WHERE r.revision_id = :rid"
            ),
            {
                "rid": revision_id,
                "scope": str(rule.scope),
                "target": rule.target_tenant_id,
                "caps": list(rule.entity_ids) or None,
                "domains": list(rule.domain_ids) or None,
                "kinds": list(rule.intent_kinds) or None,
                "actions": list(rule.action_classes) or None,
                "envs": list(rule.environments) or None,
                "tiers": list(rule.data_sensitivity_tiers) or None,
                "efrom": rule.effective_from,
                "euntil": rule.effective_until,
                "mandatory": rule.is_mandatory,
            },
        )


__all__ = [
    "ApplicabilityDraft",
    "DirectiveDraft",
    "RegisteredRevision",
    "RevisionDraft",
    "SourceIdentity",
]
