"""Approved exceptions: narrowing a higher-scope directive, with authority.

An exception lets a narrower scope weaken a rule set above it -- a tenant
relaxing a global standard for one capability, say. That is a legitimate
governance move and also the most dangerous write in this subsystem, because
its whole purpose is to make something permitted that otherwise would not be.

Three rules keep it honest.

**Only a directive that says it may be excepted can be.** The higher-scope
directive carries `delegable_exception`, and a tenant cannot except a global
directive that does not. Without that, any tenant could opt itself out of
deployment-wide policy, and global governance would be advisory.

**An exception must be approved, by evidence.** `approval_evidence_id` is
NOT NULL in the schema and verified here before the row is written --
otherwise an exception would be a tenant asserting its own permission.

**An exception narrows; it never widens.** The lower scope must sit inside
the tenant creating it, and the replacement must be a genuine weakening of
the same conflict subject rather than a rule about something else entirely.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.approval import ApprovalTrustWithdrawn
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.types import (
    ArcRequestContext,
    ArcVocabularyError,
    AuthorityScope,
    NormalizedConstraint,
)
from registry.audit import actions
from registry.exceptions import NotFoundError, RegistryError, ValidationError
from registry.types import Clock

# The scopes an exception may narrow *to*. `global` is absent deliberately:
# an exception is by definition a narrowing, and a global exception would be
# an amendment to the directive itself, which belongs on the write path that
# registers a new revision.
_LOWER_SCOPES = frozenset(
    {AuthorityScope.TENANT, AuthorityScope.DOMAIN, AuthorityScope.CAPABILITY, AuthorityScope.TASK}
)


class ExceptionNotPermitted(RegistryError):
    """The directive being excepted does not permit exceptions.

    Distinct from an authorization failure: the caller may well be entitled
    to create exceptions in general. This one says the *target* is not
    exceptable, which is a property of the governance rather than of the
    caller.
    """


@dataclasses.dataclass(frozen=True)
class ExceptionApproval:
    """The evidence record that authorizes one exception.

    Written in the *same transaction* as the exception it approves, not
    beforehand. The two reference each other -- the evidence names the
    exception it approved, the exception names the evidence approving it --
    and the schema makes both foreign keys deferrable precisely so one
    transaction can insert the pair in either order.

    Splitting them would mean either an exception briefly existing with no
    approval, or evidence pointing at an exception that may never be
    created. Neither is a state this table should be able to hold.
    """

    evidence_id: uuid.UUID
    approval_verifier_id: str
    approving_principal: str
    approving_role: str
    approved_payload_digest: str
    audit_log_reference: str
    approval_timestamp: datetime.datetime
    verifier_attestation: dict[str, object] = dataclasses.field(default_factory=dict)
    verifier_identity: str = ""


@dataclasses.dataclass(frozen=True)
class ExceptionDraft:
    """One exception being requested."""

    higher_scope_directive_id: uuid.UUID
    higher_scope_revision_id: uuid.UUID
    lower_scope_kind: AuthorityScope
    replacement_conflict_descriptor: dict[str, object]
    approval: ExceptionApproval
    effective_from: datetime.datetime
    exception_statement: str
    justification: str
    effective_until: datetime.datetime | None = None
    lower_scope_domain_id: str | None = None
    lower_scope_capability_id: uuid.UUID | None = None
    lower_scope_task_kind: str | None = None
    lower_scope_action_class: str | None = None
    lower_scope_environment: str | None = None
    lower_scope_data_sensitivity: str | None = None


class ExceptionService:
    """Creates and revokes approved exceptions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    async def approve_exception(self, ctx: ArcRequestContext, draft: ExceptionDraft) -> uuid.UUID:
        """Record an approved exception, or refuse.

        The delegability check reads the *target* directive rather than
        trusting anything the caller sent. A caller-supplied "this is
        delegable" flag would let the exception assert its own legitimacy.

        Authorized as a governance *write* in the requesting tenant, not
        merely as an authenticated request. This used to call only
        `assert_request_tenant`, which rejects the reserved deployment tenant
        and checks nothing else -- and the HTTP route adds no role gate of its
        own. Any authenticated actor of any role could therefore approve an
        exception weakening any delegable directive for their tenant, which is
        the single most dangerous write in this subsystem: its whole purpose is
        to make permitted something that otherwise would not be.
        """
        self._authorization.assert_request_tenant(ctx)
        # Scoped to the requesting tenant. An exception always narrows *to* a
        # scope inside its own tenant, so tenant-write authority is the right
        # bar -- and for a tenant scope that is the admin role, which is what a
        # plain reader lacked.
        self._authorization.assert_can_write_artifact(
            ctx, ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=ctx.tenant_id)
        )
        self._validate_shape(draft)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            target = (
                await session.execute(
                    text(
                        "SELECT d.delegable_exception, d.tenant_id, d.conflict_subject_digest, "
                        "       r.lifecycle_state "
                        "FROM arc_directives d "
                        "JOIN arc_revisions r ON r.revision_id = d.revision_id "
                        "WHERE d.directive_id = :did AND d.revision_id = :rid"
                    ),
                    {"did": draft.higher_scope_directive_id, "rid": draft.higher_scope_revision_id},
                )
            ).one_or_none()
            if target is None:
                msg = "the directive this exception targets does not exist"
                raise NotFoundError(msg)

            if not target.delegable_exception:
                # The rule that keeps global governance binding. Without it a
                # tenant could opt itself out of deployment-wide policy and
                # global standards would be advisory.
                msg = (
                    f"directive {draft.higher_scope_directive_id} does not permit exceptions; "
                    "only a directive marked delegable can be narrowed"
                )
                raise ExceptionNotPermitted(msg)

            # A tenant-owned directive can only be excepted by its own
            # tenant. A global directive (tenant_id NULL) is exceptable by
            # any tenant *if* it is delegable -- that is what delegation
            # means.
            if target.tenant_id is not None and target.tenant_id != ctx.tenant_id:
                msg = "the directive this exception targets belongs to another tenant"
                raise NotFoundError(msg)

            await self._assert_replacement_matches_subject(
                session, draft, subject_digest=target.conflict_subject_digest
            )
            # An exception weakens a control, so the verifier vouching for it
            # has to still be trusted at the moment it is approved. Without
            # this, revoking a verifier would withdraw the exceptions it had
            # already approved and then permit new ones on the same withdrawn
            # trust -- the cascade would sweep a set that immediately refills.
            await self._assert_verifier_is_trusted(session, draft.approval.approval_verifier_id)

            exception_id = uuid.uuid4()
            await self._insert_evidence(session, draft, exception_id=exception_id, tenant_id=ctx.tenant_id)
            await session.execute(
                text(
                    "INSERT INTO arc_approved_exceptions ("
                    "  exception_id, higher_scope_directive_id, higher_scope_revision_id,"
                    "  lower_scope_kind, lower_scope_tenant_id, lower_scope_domain_id,"
                    "  lower_scope_capability_id, lower_scope_task_kind, lower_scope_action_class,"
                    "  lower_scope_environment, lower_scope_data_sensitivity,"
                    "  replacement_conflict_descriptor, exception_statement_plaintext,"
                    "  justification_plaintext, effective_from, effective_until,"
                    "  approval_evidence_id, created_at, created_by_actor_id"
                    ") VALUES ("
                    "  :eid, :did, :rid, :lkind, :ltenant, :ldomain, :lcap, :ltask, :laction,"
                    "  :lenv, :lsens, CAST(:replacement AS JSONB), :statement, :justification,"
                    "  :efrom, :euntil, :evidence, :now, :actor)"
                ),
                {
                    "eid": exception_id,
                    "did": draft.higher_scope_directive_id,
                    "rid": draft.higher_scope_revision_id,
                    "lkind": str(draft.lower_scope_kind),
                    # Always the *requesting* tenant, never a caller-supplied
                    # one: an exception a caller could file against another
                    # tenant would be a way to weaken somebody else's rules.
                    "ltenant": ctx.tenant_id,
                    "ldomain": draft.lower_scope_domain_id,
                    "lcap": draft.lower_scope_capability_id,
                    "ltask": draft.lower_scope_task_kind,
                    "laction": draft.lower_scope_action_class,
                    "lenv": draft.lower_scope_environment,
                    "lsens": draft.lower_scope_data_sensitivity,
                    "replacement": json.dumps(draft.replacement_conflict_descriptor, sort_keys=True),
                    "statement": draft.exception_statement,
                    "justification": draft.justification,
                    "efrom": draft.effective_from,
                    "euntil": draft.effective_until,
                    "evidence": draft.approval.evidence_id,
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_EXCEPTION_APPROVED,
                payload={
                    "exception_id": str(exception_id),
                    "higher_scope_directive_id": str(draft.higher_scope_directive_id),
                    "lower_scope_kind": str(draft.lower_scope_kind),
                    "approval_evidence_id": str(draft.approval.evidence_id),
                },
            )
        return exception_id

    async def revoke_exception(self, ctx: ArcRequestContext, exception_id: uuid.UUID, *, reason: str) -> None:
        """Withdraw an exception, restoring the directive it narrowed.

        Scoped to the requesting tenant in the predicate itself rather than
        checked afterwards: a revocation that could name another tenant's
        exception would be a way to remove their approved relief.
        """
        self._authorization.assert_request_tenant(ctx)
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE arc_approved_exceptions SET revoked_at = :now "
                    "WHERE exception_id = :eid AND lower_scope_tenant_id = :tid AND revoked_at IS NULL"
                ),
                {"eid": exception_id, "tid": ctx.tenant_id, "now": now},
            )
            affected: int = result.rowcount  # type: ignore[attr-defined]
            if affected != 1:
                # Absent, already revoked, and another tenant's are one
                # answer on purpose: distinguishing them would confirm the
                # exception exists.
                msg = f"exception {exception_id} not found"
                raise NotFoundError(msg)

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_EXCEPTION_REVOKED,
                payload={"exception_id": str(exception_id), "reason": reason[:200]},
            )

    # -- validation -------------------------------------------------------------

    def _validate_shape(self, draft: ExceptionDraft) -> None:
        if draft.lower_scope_kind not in _LOWER_SCOPES:
            msg = (
                f"{draft.lower_scope_kind} is not a valid exception scope; an exception narrows, "
                "so it cannot be global"
            )
            raise ValidationError(msg)
        if draft.lower_scope_kind is AuthorityScope.DOMAIN and not draft.lower_scope_domain_id:
            msg = "a domain-scoped exception requires lower_scope_domain_id"
            raise ValidationError(msg)
        if draft.lower_scope_kind is AuthorityScope.CAPABILITY and draft.lower_scope_capability_id is None:
            msg = "a capability-scoped exception requires lower_scope_capability_id"
            raise ValidationError(msg)
        if draft.lower_scope_kind is AuthorityScope.TASK and not (
            draft.lower_scope_task_kind and draft.lower_scope_action_class
        ):
            msg = "a task-scoped exception requires both lower_scope_task_kind and lower_scope_action_class"
            raise ValidationError(msg)
        if not draft.justification.strip():
            msg = "an exception requires a justification; an unexplained weakening is not auditable"
            raise ValidationError(msg)
        if draft.effective_until is not None and draft.effective_until <= draft.effective_from:
            msg = "effective_until must be after effective_from"
            raise ValidationError(msg)

    async def _assert_verifier_is_trusted(self, session: AsyncSession, verifier_id: str) -> None:
        """Refuse a verifier whose trust has been withdrawn.

        Checked on the verifier rather than on the evidence, because this
        path *mints* the evidence -- there is no prior row to look up.

        An absent verifier is reported here rather than left to the foreign
        key. An earlier version of this deferred to the constraint on the
        reasoning that it would report the problem more precisely; it does
        not. That foreign key is immediate rather than deferrable, so the
        insert raises `IntegrityError`, and the admin router's translation
        table has no branch for it -- the exception escapes unmapped as a 500.
        Since nothing can currently register a verifier at all, every
        exception approval on a real deployment took that path.
        """
        row = (
            await session.execute(
                text("SELECT revoked_at FROM arc_approval_verifiers WHERE approval_verifier_id = :vid"),
                {"vid": verifier_id},
            )
        ).one_or_none()
        if row is None:
            msg = f"approval verifier {verifier_id!r} is not registered"
            raise NotFoundError(msg)
        if row.revoked_at is not None:
            msg = (
                f"approval verifier {verifier_id!r} was revoked at " f"{row.revoked_at.isoformat()} and cannot approve"
            )
            raise ApprovalTrustWithdrawn(msg)

    async def _assert_replacement_matches_subject(
        self, session: AsyncSession, draft: ExceptionDraft, *, subject_digest: str | None
    ) -> None:
        """The replacement must govern the same thing it replaces.

        An exception whose replacement addressed a different conflict
        subject would not weaken the directive -- it would sit beside it,
        leaving the original in force while appearing to have relaxed it.
        """
        replacement_subject = draft.replacement_conflict_descriptor.get("conflict_subject_digest")
        if subject_digest is None:
            msg = "the directive this exception targets carries no conflict subject and cannot be narrowed"
            raise ValidationError(msg)
        if replacement_subject != subject_digest:
            msg = (
                "the replacement constraint addresses a different conflict subject than the directive "
                "it claims to narrow"
            )
            raise ValidationError(msg)

        # The replacement must also be a constraint, not just point at the
        # right subject. Nothing downstream can repair a descriptor that will
        # not parse: the resolution path has to fall back to the original
        # stricter directive, so an approver would be told the exception was
        # granted and then watch it never take effect. Refusing it here is
        # the only place the approver is still present to be told.
        descriptor = draft.replacement_conflict_descriptor
        modality = descriptor.get("modality")
        operator = descriptor.get("constraint_operator")
        raw_value = descriptor.get("constraint_value")
        if not isinstance(modality, str) or not isinstance(operator, str):
            msg = "the replacement constraint must declare a modality and a constraint operator"
            raise ValidationError(msg)
        if raw_value is not None and not isinstance(raw_value, str):
            msg = "the replacement constraint value must be a string"
            raise ValidationError(msg)
        try:
            NormalizedConstraint.parse(modality, operator, raw_value)
        except ArcVocabularyError as exc:
            raise ValidationError(str(exc)) from exc

    async def _insert_evidence(
        self,
        session: AsyncSession,
        draft: ExceptionDraft,
        *,
        exception_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Write the approval evidence naming this exception.

        `evidence_type` is fixed to `exception_approval` here rather than
        taken from the caller. A caller able to choose it could file an
        artifact-activation approval as an exception approval, which is the
        laundering the evidence-type closure prevents on the read side --
        there is no reason to allow it on the write side either.
        """
        await session.execute(
            text(
                "INSERT INTO arc_approval_evidence ("
                "  evidence_id, evidence_type, scope_kind, scope_tenant_id, approved_exception_id,"
                "  approved_payload_digest, approving_principal, approving_role, approval_timestamp,"
                "  verification_method, approval_verifier_id, verifier_attestation, verifier_identity,"
                "  audit_log_reference"
                ") VALUES (:eid, 'exception_approval', 'tenant', :tid, :exception_id, :digest,"
                "          :principal, :role, :approved_at, 'verifier_attested', :vid,"
                "          CAST(:attestation AS JSONB), :identity, :audit_ref)"
            ),
            {
                "eid": draft.approval.evidence_id,
                "tid": tenant_id,
                "exception_id": exception_id,
                "digest": draft.approval.approved_payload_digest,
                "principal": draft.approval.approving_principal,
                "role": draft.approval.approving_role,
                "approved_at": draft.approval.approval_timestamp,
                "vid": draft.approval.approval_verifier_id,
                "attestation": json.dumps(draft.approval.verifier_attestation, sort_keys=True),
                "identity": draft.approval.verifier_identity or draft.approval.approving_principal,
                "audit_ref": draft.approval.audit_log_reference,
            },
        )


__all__ = ["ExceptionApproval", "ExceptionDraft", "ExceptionNotPermitted", "ExceptionService"]
