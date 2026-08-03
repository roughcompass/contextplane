"""Registering and moving governed artifacts through their lifecycle.

The only write path for ARC artifacts, revisions, directives, and rules.
Everything downstream assumes that: selection trusts that an active revision
was approved, JIT trusts that its audience was set at registration, and the
receipt trusts that the content digest it recorded identifies real upstream
content. A second write path would let a revision become active without any
of that having happened.

**Registration is not authoring.** ARC does not accept a policy someone
typed into a form. It accepts an *already-approved* revision that exists
somewhere upstream, with evidence of that approval, and records where it
came from. The uniqueness constraint on
`(source_system, source_revision_locator, content_digest)` is what makes
that literal: one upstream revision maps to exactly one ARC revision, so
registering the same thing twice is refused rather than silently duplicated.

**A revision is registered draft and activated separately.** Registration
validates and records; activation is what makes a revision bind agents. They
are separate because activation has to serialize against the artifact family
— exactly one revision of an artifact may be active — and because an
operator registering content should not accidentally put it into force.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.approval import assert_evidence_is_trusted
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from registry.audit import actions
from registry.exceptions import ConflictError, LifecycleError, NotFoundError, ValidationError
from registry.types import Clock

# The lifecycle states a revision can hold, and the only transitions allowed
# between them. Expressed as data rather than a chain of `if` statements so
# an illegal transition is a lookup failure rather than a branch someone
# forgot to write.
LIFECYCLE_DRAFT = "draft"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_REVOKED = "revoked"
LIFECYCLE_EXPIRED = "expired"

_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_DRAFT: frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_REVOKED}),
    # An active revision can be superseded by a successor, revoked outright,
    # or expire. It can never go back to draft: agents have already been told
    # to obey it, and un-telling them is not a state change.
    LIFECYCLE_ACTIVE: frozenset({LIFECYCLE_SUPERSEDED, LIFECYCLE_REVOKED, LIFECYCLE_EXPIRED}),
    LIFECYCLE_SUPERSEDED: frozenset({LIFECYCLE_REVOKED}),
    LIFECYCLE_EXPIRED: frozenset({LIFECYCLE_REVOKED, LIFECYCLE_ACTIVE}),
    # Terminal. A revoked revision is evidence of what was once in force and
    # must stay readable, but it can never bind anything again.
    LIFECYCLE_REVOKED: frozenset(),
}

OBLIGATION_SATISFIED = "satisfied"
OBLIGATION_MISSING_REVOKED = "missing_revoked"
OBLIGATION_MISSING_INVALID = "missing_invalid"
OBLIGATION_MISSING_REVIEW_EXPIRED = "missing_review_expired"

# Content storage modes the schema permits.
STORAGE_ENCRYPTED = "encrypted"
STORAGE_NONE = "none"


class ArtifactLifecycleError(LifecycleError):
    """A transition the state machine does not permit."""


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
    capability_ids: tuple[uuid.UUID, ...] = ()
    domain_ids: tuple[str, ...] = ()
    task_kinds: tuple[str, ...] = ()
    action_classes: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    data_sensitivity_tiers: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, object]:
        """The applicability a mandatory obligation retains.

        Retained rather than referenced: when the revision behind an
        obligation is revoked, the obligation must still know who it applied
        to, or a resolution that should block would find nothing to block on.
        """
        return {
            "scope": str(self.scope),
            "target_tenant_id": str(self.target_tenant_id) if self.target_tenant_id else None,
            "capability_ids": sorted(str(c) for c in self.capability_ids),
            "domain_ids": sorted(self.domain_ids),
            "task_kinds": sorted(self.task_kinds),
            "action_classes": sorted(self.action_classes),
            "environments": sorted(self.environments),
            "data_sensitivity_tiers": sorted(self.data_sensitivity_tiers),
        }

    def digest(self) -> str:
        canonical = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


class ArtifactService:
    """The single write path for ARC governed content."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        approval_verification_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._approval_verification_enabled = approval_verification_enabled

    # -- registration ---------------------------------------------------------

    async def register_revision(
        self, ctx: ArcRequestContext, draft: RevisionDraft
    ) -> RegisteredRevision:
        """Record an already-approved upstream revision, as a draft.

        One transaction: the revision, its directives, and its rules land
        together or not at all. A revision whose directives failed to write
        would be an artifact that binds nobody while appearing registered.
        """
        self._validate(draft)
        now = self._clock.now()

        async with self._session_factory() as session, session.begin():
            artifact = await self._load_artifact(session, draft.artifact_id)
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

    # -- lifecycle ------------------------------------------------------------

    async def attach_approval_evidence(
        self, ctx: ArcRequestContext, revision_id: uuid.UUID, evidence_id: uuid.UUID
    ) -> None:
        """Link a revision to the evidence approving it, after registration.

        A separate step because the ordering is forced, not chosen. Evidence
        of type `artifact_activation` must name the revision it approves, and
        the revision does not exist until it has been registered — so the
        evidence cannot precede its revision, and registration cannot demand
        it. Register, approve, attach, activate.

        Refuses once a revision is active: changing the evidence behind a
        rule already in force would rewrite why agents were told to obey it.
        """
        async with self._session_factory() as session, session.begin():
            revision = await self._lock_family(session, revision_id)
            artifact = await self._load_artifact(session, revision.artifact_id)
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
                        "SELECT approved_revision_id FROM arc_approval_evidence WHERE evidence_id = :eid"
                    ),
                    {"eid": evidence_id},
                )
            ).one_or_none()
            if evidence is None:
                msg = f"approval evidence {evidence_id} not found"
                raise NotFoundError(msg)
            await self._assert_evidence_approves(session, evidence_id, revision_id)
            # Refused early so an operator finds out while linking rather than
            # at activation, but it is checked again there -- trust can be
            # withdrawn in between, and activation is what puts a revision
            # into force.
            await assert_evidence_is_trusted(session, evidence_id)

            await session.execute(
                text("UPDATE arc_revisions SET approval_evidence_id = :eid WHERE revision_id = :rid"),
                {"rid": revision_id, "eid": evidence_id},
            )


    async def activate(
        self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, supersedes: uuid.UUID | None = None
    ) -> None:
        """Put a revision into force, superseding its predecessor.

        Locks the whole artifact family `FOR UPDATE` before reading anything.
        Exactly one revision of an artifact may be active, and two concurrent
        activations that each checked "is anything active?" before either
        wrote would both see no. The database has a partial unique index as a
        backstop, but relying on it alone would turn a routine race into a
        failed request rather than a serialized one.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await self._lock_family(session, revision_id)
            artifact = await self._load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)

            self._assert_transition(revision.lifecycle_state, LIFECYCLE_ACTIVE)

            # Re-checked at activation, not trusted from registration: a
            # revision registered months ago may have passed its review date
            # while sitting in draft, and activating it would put stale
            # governance into force.
            if revision.review_expires_at <= now:
                msg = f"revision {revision_id} passed its review date on {revision.review_expires_at.isoformat()}"
                raise ArtifactLifecycleError(msg)
            if not self._approval_verification_enabled:
                # Refuses rather than falling through to the checks below, and
                # the distinction matters more than it looks. Without a
                # registered verifier and a first-party evidence writer, the
                # only way an `artifact_activation` row reaches the database is
                # a direct SQL INSERT -- so the remaining checks are satisfied
                # by exactly the capability they exist to constrain. Whoever
                # can write the evidence row can equally set
                # `lifecycle_state = 'active'` and skip them entirely.
                #
                # That makes the gate vacuous rather than weak, and a vacuous
                # gate is worse than an absent one: it lets a deployment
                # accumulate activated revisions and build a governance surface
                # on a check that never rejected anything, while receipts
                # assert those revisions were approved. Refusing keeps that
                # state unreachable instead of reachable and footnoted.
                #
                # This is an honest statement about the API surface, not a
                # control against an actor with database write access -- who can
                # still set the lifecycle column directly.
                msg = (
                    "approval-evidence verification is not configured on this deployment, so an "
                    "activation cannot be checked against a registered verifier; activating would "
                    "record an approval nothing validated"
                )
                raise ArtifactLifecycleError(msg)
            if revision.approval_evidence_id is None:
                msg = f"revision {revision_id} has no approval evidence and cannot be activated"
                raise ArtifactLifecycleError(msg)
            # Re-checked here, not only where the link was made. `attach` was
            # the sole enforcer of "this evidence approves *this* revision",
            # and `register_revision` can set the column directly -- so a
            # revision could be registered citing an approval granted to
            # something else and then activated, borrowing it. Activation is
            # the step that puts a revision into force, so it is the one that
            # has to hold regardless of how the column was populated.
            await self._assert_evidence_approves(session, revision.approval_evidence_id, revision_id)
            # Having evidence is not the same as having trusted evidence. The
            # revocation cascade withdraws what already stands on a revoked
            # verifier; refusing here is what stops the set being refilled a
            # moment later by a revision that attached the same evidence.
            await assert_evidence_is_trusted(session, revision.approval_evidence_id)

            current = await self._active_revision(session, revision.artifact_id)
            if current is not None and current != revision_id:
                if supersedes is not None and supersedes != current:
                    msg = f"revision {supersedes} is not the active revision of this artifact"
                    raise ArtifactLifecycleError(msg)
                await self._set_state(
                    session, current, LIFECYCLE_SUPERSEDED, now=now, superseded_by=revision_id
                )

            await session.execute(
                text(
                    "UPDATE arc_revisions SET lifecycle_state = :state, activated_at = :now "
                    "WHERE revision_id = :rid"
                ),
                {"rid": revision_id, "state": LIFECYCLE_ACTIVE, "now": now},
            )
            await self._refresh_obligations(session, revision_id, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_ACTIVATED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "superseded_revision_id": str(current) if current and current != revision_id else None,
                },
            )

    async def _assert_evidence_approves(
        self, session: AsyncSession, evidence_id: uuid.UUID, revision_id: uuid.UUID
    ) -> None:
        """The evidence must be about *this* revision.

        Without it a revision could borrow an approval granted to something
        else, which is the whole failure approval evidence exists to prevent.
        One implementation, called from every path that can bind the two, so
        the check cannot be present on one route and absent on another --
        which is exactly how it came to be enforced at attach time and not at
        registration.
        """
        approved = (
            await session.execute(
                text("SELECT approved_revision_id FROM arc_approval_evidence WHERE evidence_id = :eid"),
                {"eid": evidence_id},
            )
        ).scalar_one_or_none()
        if approved is None:
            msg = f"approval evidence {evidence_id} not found"
            raise NotFoundError(msg)
        if approved != revision_id:
            msg = f"approval evidence {evidence_id} does not approve revision {revision_id}"
            raise ValidationError(msg)

    async def revoke(self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, reason: str) -> None:
        """Withdraw a revision from force, permanently.

        Any mandatory obligation it satisfied becomes `missing_revoked`
        rather than disappearing. That is the whole point: a revoked
        obligation must keep blocking matching resolutions until an approved
        successor satisfies it, and an obligation that vanished with its
        revision would silently unblock everything it used to govern.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await self._lock_family(session, revision_id)
            artifact = await self._load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)
            self._assert_transition(revision.lifecycle_state, LIFECYCLE_REVOKED)

            await self._set_state(session, revision_id, LIFECYCLE_REVOKED, now=now)
            await self._tombstone_obligations(session, revision_id, OBLIGATION_MISSING_REVOKED, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_REVOKED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "reason": reason[:200],
                },
            )

    async def invalidate(self, ctx: ArcRequestContext, revision_id: uuid.UUID, *, reason: str) -> None:
        """Mark a revision's content no longer trustworthy.

        Distinct from revocation: revocation is a governance decision that
        this rule no longer applies, invalidation says the content itself is
        wrong or its upstream source is gone. Obligations tombstone as
        `missing_invalid` so an auditor can tell the two apart later.

        Operator-driven rather than automatic, because deciding that
        registered content is wrong is a judgement no worker should make.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            revision = await self._lock_family(session, revision_id)
            artifact = await self._load_artifact(session, revision.artifact_id)
            self._authorization.assert_can_write_artifact(ctx, artifact)
            self._assert_transition(revision.lifecycle_state, LIFECYCLE_REVOKED)

            await self._set_state(session, revision_id, LIFECYCLE_REVOKED, now=now)
            await self._tombstone_obligations(session, revision_id, OBLIGATION_MISSING_INVALID, now=now)
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ARTIFACT_INVALIDATED,
                payload={
                    "artifact_id": str(revision.artifact_id),
                    "revision_id": str(revision_id),
                    "reason": reason[:200],
                },
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
            if rule.scope is AuthorityScope.CAPABILITY and not rule.capability_ids:
                msg = "a capability-scoped applicability rule requires at least one capability id"
                raise ValidationError(msg)

    def _assert_transition(self, current: str, target: str) -> None:
        allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            msg = f"cannot move a revision from {current!r} to {target!r}"
            raise ArtifactLifecycleError(msg)

    # -- row helpers ----------------------------------------------------------

    async def _load_artifact(self, session: AsyncSession, artifact_id: uuid.UUID) -> ArtifactScope:
        row = (
            await session.execute(
                text("SELECT artifact_id, tenant_id FROM arc_artifacts WHERE artifact_id = :aid"),
                {"aid": artifact_id},
            )
        ).one_or_none()
        if row is None:
            msg = f"artifact {artifact_id} not found"
            raise NotFoundError(msg)
        # A NULL tenant means deployment-wide governance; every other row
        # belongs to exactly one tenant.
        if row.tenant_id is None:
            return ArtifactScope(scope=AuthorityScope.GLOBAL)
        return ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=row.tenant_id)

    async def _lock_family(self, session: AsyncSession, revision_id: uuid.UUID) -> Row[Any]:
        """Lock every revision of this revision's artifact, then return it.

        The lock is taken on the family rather than the single row because
        activation reads *and* writes siblings — it supersedes the current
        active revision — and locking only the target would leave that read
        unserialized.
        """
        target = (
            await session.execute(
                text("SELECT artifact_id FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).one_or_none()
        if target is None:
            msg = f"revision {revision_id} not found"
            raise NotFoundError(msg)

        await session.execute(
            text("SELECT revision_id FROM arc_revisions WHERE artifact_id = :aid ORDER BY revision_id FOR UPDATE"),
            {"aid": target.artifact_id},
        )
        row = (
            await session.execute(
                text(
                    "SELECT revision_id, artifact_id, lifecycle_state, review_expires_at, "
                    "       approval_evidence_id FROM arc_revisions WHERE revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).one()
        return row

    async def _active_revision(self, session: AsyncSession, artifact_id: uuid.UUID) -> uuid.UUID | None:
        return (
            await session.execute(
                text(
                    "SELECT revision_id FROM arc_revisions "
                    "WHERE artifact_id = :aid AND lifecycle_state = :state"
                ),
                {"aid": artifact_id, "state": LIFECYCLE_ACTIVE},
            )
        ).scalar_one_or_none()

    async def _set_state(
        self,
        session: AsyncSession,
        revision_id: uuid.UUID,
        state: str,
        *,
        now: datetime.datetime,
        superseded_by: uuid.UUID | None = None,
    ) -> None:
        await session.execute(
            text(
                "UPDATE arc_revisions SET lifecycle_state = :state, "
                "  superseded_by_revision_id = COALESCE(:superseded_by, superseded_by_revision_id), "
                "  revoked_at = CASE WHEN :state = 'revoked' THEN :now ELSE revoked_at END "
                "WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "state": state, "now": now, "superseded_by": superseded_by},
        )

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
                "  revision_id, tenant_id, scope, target_tenant_id, capability_ids, domain_ids,"
                "  task_kinds, action_classes, environments, data_sensitivity_tiers,"
                "  effective_from, effective_until, is_mandatory"
                ") SELECT :rid, r.tenant_id, :scope, :target, :caps, :domains, :kinds, :actions,"
                "         :envs, :tiers, :efrom, :euntil, :mandatory "
                "  FROM arc_revisions r WHERE r.revision_id = :rid"
            ),
            {
                "rid": revision_id,
                "scope": str(rule.scope),
                "target": rule.target_tenant_id,
                "caps": list(rule.capability_ids) or None,
                "domains": list(rule.domain_ids) or None,
                "kinds": list(rule.task_kinds) or None,
                "actions": list(rule.action_classes) or None,
                "envs": list(rule.environments) or None,
                "tiers": list(rule.data_sensitivity_tiers) or None,
                "efrom": rule.effective_from,
                "euntil": rule.effective_until,
                "mandatory": rule.is_mandatory,
            },
        )

    # -- obligations ----------------------------------------------------------

    async def _refresh_obligations(
        self, session: AsyncSession, revision_id: uuid.UUID, *, now: datetime.datetime
    ) -> None:
        """Point each mandatory obligation at the newly active revision.

        An obligation is keyed by the *stable* directive id, so activating a
        successor satisfies the obligation its predecessor left behind rather
        than creating a second one. That is what lets a tombstoned obligation
        be cleared by approving a replacement.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT d.directive_id, r.artifact_id, ar.rule_id, ar.effective_from, ar.effective_until, "
                    "       ar.scope, ar.target_tenant_id, ar.capability_ids, ar.domain_ids, ar.task_kinds, "
                    "       ar.action_classes, ar.environments, ar.data_sensitivity_tiers "
                    "FROM arc_directives d "
                    "JOIN arc_revisions r ON r.revision_id = d.revision_id "
                    "JOIN arc_applicability_rules ar ON ar.revision_id = d.revision_id "
                    "WHERE d.revision_id = :rid AND ar.is_mandatory"
                ),
                {"rid": revision_id},
            )
        ).all()

        for row in rows:
            snapshot = {
                "scope": row.scope,
                "target_tenant_id": str(row.target_tenant_id) if row.target_tenant_id else None,
                "capability_ids": sorted(str(c) for c in (row.capability_ids or [])),
                "domain_ids": sorted(row.domain_ids or []),
                "task_kinds": sorted(row.task_kinds or []),
                "action_classes": sorted(row.action_classes or []),
                "environments": sorted(row.environments or []),
                "data_sensitivity_tiers": sorted(row.data_sensitivity_tiers or []),
            }
            canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

            existing = (
                await session.execute(
                    text(
                        "SELECT obligation_id FROM arc_mandatory_obligations "
                        "WHERE directive_id = :did AND applicability_digest = :digest"
                    ),
                    {"did": row.directive_id, "digest": digest},
                )
            ).scalar_one_or_none()

            if existing is None:
                await session.execute(
                    text(
                        "INSERT INTO arc_mandatory_obligations ("
                        "  artifact_id, directive_id, current_revision_id, applicability_snapshot,"
                        "  applicability_digest, obligation_state, effective_from, effective_until, updated_at"
                        ") VALUES (:aid, :did, :rid, CAST(:snapshot AS JSONB), :digest, :state,"
                        "          :efrom, :euntil, :now)"
                    ),
                    {
                        "aid": row.artifact_id,
                        "did": row.directive_id,
                        "rid": revision_id,
                        "snapshot": canonical,
                        "digest": digest,
                        "state": OBLIGATION_SATISFIED,
                        "efrom": row.effective_from,
                        "euntil": row.effective_until,
                        "now": now,
                    },
                )
            else:
                # The effective window is refreshed along with the revision.
                # The digest is computed over the applicability snapshot,
                # which carries the selectors but not the dates -- so a
                # successor that changes only its rule's window matches an
                # existing obligation and lands here. Leaving the dates
                # behind would strand the obligation on its predecessor's
                # window, and once that window passed, the row would stop
                # being read: a later revocation would tombstone a row
                # nothing loads, and the resolution it should have blocked
                # would come back ready.
                await session.execute(
                    text(
                        "UPDATE arc_mandatory_obligations SET current_revision_id = :rid, "
                        "  obligation_state = :state, effective_from = :efrom, "
                        "  effective_until = :euntil, updated_at = :now WHERE obligation_id = :oid"
                    ),
                    {
                        "oid": existing,
                        "rid": revision_id,
                        "state": OBLIGATION_SATISFIED,
                        "efrom": row.effective_from,
                        "euntil": row.effective_until,
                        "now": now,
                    },
                )

    async def _tombstone_obligations(
        self, session: AsyncSession, revision_id: uuid.UUID, state: str, *, now: datetime.datetime
    ) -> None:
        """Leave the obligation standing, with its revision cleared.

        `current_revision_id` goes NULL because there is no longer a revision
        satisfying it, and the schema's CHECK requires a satisfied obligation
        to name one. The applicability snapshot stays, so resolutions that
        would have matched still block.
        """
        await session.execute(
            text(
                "UPDATE arc_mandatory_obligations SET obligation_state = :state, "
                "  current_revision_id = NULL, updated_at = :now "
                "WHERE current_revision_id = :rid"
            ),
            {"rid": revision_id, "state": state, "now": now},
        )


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


__all__ = [
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_DRAFT",
    "LIFECYCLE_EXPIRED",
    "LIFECYCLE_REVOKED",
    "LIFECYCLE_SUPERSEDED",
    "OBLIGATION_MISSING_INVALID",
    "OBLIGATION_MISSING_REVIEW_EXPIRED",
    "OBLIGATION_MISSING_REVOKED",
    "OBLIGATION_SATISFIED",
    "ApplicabilityDraft",
    "ArtifactLifecycleError",
    "ArtifactService",
    "DirectiveDraft",
    "RegisteredRevision",
    "RevisionDraft",
    "SourceIdentity",
]
