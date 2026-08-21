"""Autonomy envelopes: which `policy` revision governs which agent principal.

An envelope is an ARC artifact of kind `policy`. Nothing about the artifact is
new -- `ck_arc_artifacts_kind` has always admitted `policy`, and the authority
matrix it carries lives in `arc_applicability_rules` like every other artifact's
applicability. What is new is the *binding*: the record saying this revision is
the envelope for that principal. Without it an envelope is indistinguishable
from any other policy document.

**A principal is an IAM workload identity, and this module never touches
`actors`.** `WorkloadIdentity` is the `(issuer, subject)` pair every ARC
provenance column already uses, and `ArcRequestContext.operator_identity`
produces one directly. `actors` is tenant-local attribution whose `actor_kind`
admits `human` and `sync_worker`; making an agent's authority depend on a row in
it would be the wrong dependency in the wrong direction.

**Only suspension is authorized at tenant scope. Everything else is authorized
at the envelope's.** Granting, reinstating and revoking a global envelope's
binding need the deployment-operator allowlist and no tenant role reaches them.
Suspending needs tenant admin on the binding's own tenant, because an incident is
what it exists for and requiring a deployment operator to switch off one tenant's
agent would make "instant" depend on finding one.

**The line is drawn at "does this free the principal's slot", not at "does this
narrow".** An intermediate version of this module drew it at narrowing --
suspend *and* revoke at tenant scope -- and that was a privilege escalation,
because at the time a suspension also released the exclusion constraint's hold
on the principal. A tenant admin could suspend the binding a deployment operator
had made to a global envelope, then grant a tenant-scoped envelope of their own
authoring to the same principal, and each step passed its own check. Two changes
close it: the constraint now reserves the principal for any open interval
whatever the state, so suspension releases nothing; and revoking, which does
close the interval, is authorized at the envelope's scope. Reasoning about one
operation at a time said nothing about the sequence.

**A deployment operator may act on a binding in any tenant.** Allowlist
membership is the deployment trust root, and it confers no tenant role -- so
without this an operator who granted a global envelope could never suspend or
revoke it, and a tenant could permanently squat a principal's only slot with a
binding the operator was powerless to remove. It is strictly less authority than
writing the global governance that binds every tenant, which the allowlist
already confers.

**Suspend is a flip; revoke closes the interval; a widen is a revoke followed by
a grant.** A suspension leaves the row in place saying who it governs and why it
is off, so an operator reading the table sees a suspension rather than a gap and
reinstating is one more flip. It reserves the principal's slot throughout. A
widen is the full ARC pipeline producing a new revision, then ending the old
binding and opening a new one -- which is why a binding names a revision and not
an artifact, so nobody's authority changes as a side effect of a publish.

**The bound revision must be `active` at grant time.** A `draft` revision has
been through no approval and no actor separation; binding a principal to one
would let whoever can write a draft decide what an agent may do without the
pipeline that exists to stop exactly that. Checked at grant only: a revision
that is later superseded or revoked leaves its bindings alone, because ending
them is a decision with its own audit trail rather than a side effect, and the
task that builds the decision path is where "what does a binding to a revoked
revision authorize" gets answered.

**Two envelopes over one principal's window are unconstructible, and the
database is what says so.** An exclusion constraint over
`(tenant_id, issuer, subject, [effective_from, effective_to))` enforces it. This
module does not check it first: a check followed by an insert is two statements
a concurrent grant can interleave between, and the constraint is the only thing
that holds under concurrency. The `IntegrityError` is caught and reported as
`EnvelopeAlreadyBound`.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import TextClause

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.audit import actions
from contextplane.exceptions import NotFoundError, RegistryError, ValidationError
from contextplane.types import Clock

#: The one constraint whose violation is a business outcome rather than a bug.
_ONE_PER_PRINCIPAL = "ex_arc_envelope_bindings_one_per_principal"

#: The classification a global envelope must carry, and the whole of what makes
#: "named human authority" enforceable rather than aspirational.
#:
#: `risk.py` classifies a revision as `global_mandatory` when any of its
#: applicability rules is global-scoped and mandatory, and
#: `activation_predicates.py` then requires **three distinct principals** --
#: submitter, approver, activator -- where every other classification requires
#: two. So a global envelope written with mandatory rules cannot reach `active`
#: without three separate people, and this constant is where a binding refuses
#: to trust one that did not.
#:
#: Not a new authority concept, which is the point: the mechanism already
#: shipped, and the only thing missing was somewhere that insisted on it.
_GLOBAL_ENVELOPE_CLASSIFICATION = "global_mandatory"

#: `exclusion_violation`. Matched together with the constraint name, following
#: `service/memory/session_events.py`: a substring search of the message text
#: happens to work today, but it also matches any future constraint whose name
#: contains this one, and asserts nothing about *which* kind of violation
#: occurred. asyncpg carries both as fields.
_EXCLUSION_VIOLATION = "23P01"

#: Kept off the audit payload and the error text alike. A suspension reason is
#: operator prose and there is no reason for it to be able to grow an audit row
#: without bound.
_REASON_LIMIT = 200


class EnvelopeAlreadyBound(RegistryError):
    """This principal already has an envelope in force over the same window.

    Distinct from an authorization failure: the caller may be entitled to grant
    envelopes. This says the principal already has one, and replacing it means
    revoking that one first -- an explicit act, at the displaced envelope's own
    scope, so that nothing silently supersedes an authority record.
    """


class InsufficientEnvelopeApproval(RegistryError):
    """A global envelope that did not go through three-principal approval.

    Distinct from `NotAnEnvelope`: the revision *is* a policy revision and it
    *is* active. What it lacks is the actor separation a deployment-wide
    authority object has to have behind it, and the caller's remedy is to
    re-author it with mandatory rules rather than to point somewhere else.
    """


class NotAnEnvelope(RegistryError):
    """The revision named is not a revision of a `policy` artifact.

    Raised only when the row cannot be found *as an envelope*; the database
    refuses the write independently through a composite foreign key, and this
    exists so the caller gets the reason rather than an integrity error.
    """


@dataclasses.dataclass(frozen=True)
class WorkloadIdentity:
    """The IAM workload identity an envelope governs.

    A named pair rather than a bare `tuple[str, str]`, because the two halves
    are both opaque strings and a transposed pair at an authority boundary would
    resolve to the wrong envelope with nothing to catch it.

    **Both halves are stored stripped.** Resolution matches exactly, so storing
    `" agent-1"` would produce a binding that satisfies every constraint, reads
    as governed in the table, and resolves for nobody -- governance theatre
    rather than governance. Validating whitespace away while storing it raw was
    the earlier bug.
    """

    issuer: str
    subject: str

    @classmethod
    def of_requester(cls, ctx: ArcRequestContext) -> WorkloadIdentity:
        """The identity making this request, as the allowlist already reads it."""
        issuer, subject = ctx.operator_identity
        return cls(issuer=issuer, subject=subject)

    def __post_init__(self) -> None:
        issuer, subject = self.issuer.strip(), self.subject.strip()
        if not issuer or not subject:
            msg = "a workload identity needs both an issuer and a subject"
            raise ValueError(msg)
        # Frozen, so the normalised values go in through `object.__setattr__`.
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "subject", subject)


@dataclasses.dataclass(frozen=True)
class EnvelopeGrant:
    """One envelope being granted to one principal."""

    revision_id: uuid.UUID
    principal: WorkloadIdentity
    reason: str
    effective_from: datetime.datetime | None = None
    effective_to: datetime.datetime | None = None
    audit_reference: str | None = None


@dataclasses.dataclass(frozen=True)
class BoundEnvelope:
    """The envelope in force for a principal, as the decision path reads it.

    Carries `state` rather than filtering on it, because "suspended" and "no
    envelope at all" are different answers and the decision path must be able to
    tell them apart -- one is a posture an operator chose, the other is a
    principal nobody has governed yet.
    """

    binding_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    principal: WorkloadIdentity
    state: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    suspended_at: datetime.datetime | None
    suspension_reason: str | None

    #: The bound revision's own lifecycle state, carried so the decision path can
    #: see it. A revision is only checked for `active` at grant time; it may be
    #: superseded or revoked through the ARC pipeline afterwards, and a binding
    #: to a withdrawn governance document is not a state this read should hide.
    #: What to *do* about it is the decision path's call, not this read's.
    revision_lifecycle_state: str

    @property
    def is_in_force(self) -> bool:
        """Whether the binding itself is switched on.

        Deliberately says nothing about `revision_lifecycle_state`. Every
        `BoundEnvelope` comes from `resolve`, which already filtered to bindings
        whose interval covers the instant asked about, so this is the only
        remaining question about the *binding*. Whether a live binding to a
        revoked revision authorizes anything is a different question with a
        different answer, and folding it in here would hide the distinction
        behind a property that reads as if it were about the switch.
        """
        return self.state == "active"


_INSERT = text(
    """
    INSERT INTO arc_autonomy_envelope_bindings (
        binding_id, tenant_id, revision_id, artifact_id, artifact_kind,
        principal_issuer, principal_subject, state,
        effective_from, effective_to, actor, reason, audit_reference, recorded_at
    )
    VALUES (
        :binding_id, :tenant_id, :revision_id, :artifact_id, 'policy',
        :issuer, :subject, 'active',
        :effective_from, :effective_to, :actor, :reason, :audit_reference, :recorded_at
    )
    """
)

#: The revision, its artifact, the artifact's kind and the revision's lifecycle
#: state in one read. `kind` and `lifecycle_state` are selected rather than
#: filtered on so a caller pointing at a `runbook` or a draft is told which it
#: is, instead of being told the revision does not exist.
_LOAD_REVISION = text(
    """
    SELECT r.artifact_id, r.lifecycle_state, a.kind, a.tenant_id,
           v.risk_classification
    FROM arc_revisions AS r
    JOIN arc_artifacts AS a ON a.artifact_id = r.artifact_id
    LEFT JOIN arc_authoring_proposal_versions AS v ON v.revision_id = r.revision_id
    WHERE r.revision_id = :revision_id
    """
)

#: The hot read.
#:
#: **Active beats suspended, and only then does later beat earlier.** The
#: exclusion constraint forbids two `active` rows covering one instant, so there
#: is at most one to prefer -- but a suspended binding and the replacement
#: granted over the same window both cover `now`, which is exactly what the
#: widen path produces. Ordering by recency alone picks between them by
#: whichever happens to sort first, and a suspended envelope winning that tie
#: would leave a principal reading as suspended immediately after being
#: regranted. A suspended row is still returned when it is the only candidate,
#: because "suspended" and "never governed" are different answers.
_RESOLVE = text(
    """
    SELECT b.binding_id, b.revision_id, b.artifact_id, b.principal_issuer, b.principal_subject,
           b.state, b.effective_from, b.effective_to, b.suspended_at, b.suspension_reason,
           r.lifecycle_state AS revision_lifecycle_state
    FROM arc_autonomy_envelope_bindings AS b
    JOIN arc_revisions AS r ON r.revision_id = b.revision_id
    WHERE b.tenant_id = :tenant_id
      AND b.principal_issuer = :issuer
      AND b.principal_subject = :subject
      AND b.effective_from <= :at
      AND (b.effective_to IS NULL OR b.effective_to > :at)
    ORDER BY (b.state = 'active') DESC, b.effective_from DESC, b.recorded_at DESC
    LIMIT 1
    """
)

#: Both flips also require the interval to still be open. Without that, a
#: revoked binding -- interval closed, `state` still whatever it was -- could be
#: suspended and then reinstated, leaving a row reading `active` with
#: `effective_to` in the past and an audit event claiming authority was restored
#: when `resolve` returns nothing. The state and the interval are two halves of
#: one lifecycle, and only the CHECK constraints tie `state` to the suspension
#: columns; nothing in the schema ties it to the interval.
_SUSPEND = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET state = 'suspended', suspended_at = :now, suspension_reason = :reason
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id AND state = 'active'
      AND (effective_to IS NULL OR effective_to > :now)
    """
)

_REINSTATE = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET state = 'active', suspended_at = NULL, suspension_reason = NULL
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id AND state = 'suspended'
      AND (effective_to IS NULL OR effective_to > :now)
    """
)

#: `GREATEST` so a binding revoked before it ever took effect -- granted and
#: withdrawn in the same instant, or future-dated and cancelled first -- closes
#: to an empty interval rather than an inverted one. Empty is the true record:
#: it was in force for no time. Inverted is not a state the table should hold,
#: and the interval CHECK refuses it.
_REVOKE = text(
    """
    UPDATE arc_autonomy_envelope_bindings
    SET effective_to = GREATEST(:now, effective_from)
    WHERE binding_id = :binding_id AND tenant_id = :tenant_id
      AND (effective_to IS NULL OR effective_to > :now)
    """
)


class AutonomyEnvelopeService:
    """Grants, suspends, reinstates, revokes and resolves envelope bindings."""

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

    # -- writes ---------------------------------------------------------------

    async def grant(self, ctx: ArcRequestContext, grant: EnvelopeGrant) -> uuid.UUID:
        """Bind a principal to an envelope revision, or refuse."""
        self._authorization.assert_request_tenant(ctx)
        _require_reason(grant.reason)
        now = self._clock.now()
        effective_from = grant.effective_from or now
        if grant.effective_to is not None and grant.effective_to <= effective_from:
            msg = "effective_to must be after effective_from"
            raise ValidationError(msg)
        # No backdating. `resolve(at=...)` answers "what governed this principal
        # then", and a later audit of whether an action was within envelope
        # reads exactly that. A binding that may start in the past lets the party
        # being audited write the answer afterwards.
        if effective_from < now:
            msg = "effective_from may not precede now; an envelope binding is not backdatable"
            raise ValidationError(msg)

        binding_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            artifact_id = await self._assert_envelope_revision(session, ctx, grant.revision_id, require_active=True)
            try:
                await session.execute(
                    _INSERT,
                    {
                        "binding_id": binding_id,
                        "tenant_id": ctx.tenant_id,
                        "revision_id": grant.revision_id,
                        "artifact_id": artifact_id,
                        "issuer": grant.principal.issuer,
                        "subject": grant.principal.subject,
                        "effective_from": effective_from,
                        "effective_to": grant.effective_to,
                        "actor": str(ctx.actor_id),
                        "reason": grant.reason,
                        "audit_reference": grant.audit_reference,
                        "recorded_at": now,
                    },
                )
                # No `flush()` here. An earlier version called one, with a
                # comment about forcing a deferred check -- and the constraint
                # is not DEFERRABLE, while `session.execute(text(...))` is
                # eager, so the error already arrives from the `execute` above
                # and the flush sent nothing. A line that does nothing is worse
                # than absent when its comment explains why it is essential.
            except IntegrityError as clash:
                if not _is_principal_clash(clash):
                    raise
                msg = "this principal already has an envelope in force over that window"
                raise EnvelopeAlreadyBound(msg) from clash

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ENVELOPE_BOUND,
                payload={
                    "binding_id": str(binding_id),
                    "revision_id": str(grant.revision_id),
                    "principal_issuer": grant.principal.issuer,
                    "principal_subject": grant.principal.subject,
                },
            )
        return binding_id

    async def suspend(self, ctx: ArcRequestContext, binding_id: uuid.UUID, *, reason: str) -> None:
        """Turn an envelope off without ending it. The instant-suspend path.

        Tenant admin on the binding's own tenant, even for a global envelope. An
        incident is the case this exists for, and requiring a deployment
        operator to switch off one tenant's agent would make "instant" depend on
        finding one.
        """
        await self._flip(
            ctx,
            binding_id,
            statement=_SUSPEND,
            reason=reason,
            event_type=actions.ARC_ENVELOPE_SUSPENDED,
            absent="no active envelope binding",
            at_envelope_scope=False,
        )

    async def reinstate(self, ctx: ArcRequestContext, binding_id: uuid.UUID, *, reason: str) -> None:
        """Turn a suspended envelope back on.

        Authorized like a grant, not like a suspension: this puts authority back
        in force, and a tenant admin who could reinstate at tenant scope could
        undo a deployment operator's suspension of a global envelope.
        """
        await self._flip(
            ctx,
            binding_id,
            statement=_REINSTATE,
            reason=reason,
            event_type=actions.ARC_ENVELOPE_REINSTATED,
            absent="no suspended envelope binding",
            at_envelope_scope=True,
        )

    async def revoke(self, ctx: ArcRequestContext, binding_id: uuid.UUID, *, reason: str) -> None:
        """End the binding, closing its interval.

        Authorized at the envelope's scope even though it only narrows, because
        closing the interval frees the principal's slot and is therefore the
        first half of a substitution. A tenant admin who could revoke a global
        envelope's binding could follow it with a grant of their own authoring.
        """
        await self._flip(
            ctx,
            binding_id,
            statement=_REVOKE,
            reason=reason,
            event_type=actions.ARC_ENVELOPE_REVOKED,
            absent="no open envelope binding",
            at_envelope_scope=True,
        )

    # -- reads ----------------------------------------------------------------

    async def resolve(
        self,
        ctx: ArcRequestContext,
        principal: WorkloadIdentity,
        *,
        at: datetime.datetime | None = None,
    ) -> BoundEnvelope | None:
        """The envelope covering `principal` at `at`, suspended or not.

        Returns `None` when no binding covers the instant, which the decision
        path must distinguish from a suspended one: the first is a principal
        nobody has governed, the second is a posture somebody chose.
        """
        self._authorization.assert_request_tenant(ctx)
        instant = at or self._clock.now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _RESOLVE,
                    {
                        "tenant_id": ctx.tenant_id,
                        "issuer": principal.issuer,
                        "subject": principal.subject,
                        "at": instant,
                    },
                )
            ).first()
        if row is None:
            return None
        return BoundEnvelope(
            binding_id=row.binding_id,
            revision_id=row.revision_id,
            artifact_id=row.artifact_id,
            principal=WorkloadIdentity(issuer=row.principal_issuer, subject=row.principal_subject),
            state=row.state,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            suspended_at=row.suspended_at,
            suspension_reason=row.suspension_reason,
            revision_lifecycle_state=row.revision_lifecycle_state,
        )

    # -- internals ------------------------------------------------------------

    def _is_deployment_operator(self, ctx: ArcRequestContext) -> bool:
        """Whether this caller is on the deployment-operator allowlist.

        Asked through the public authorization surface rather than by reaching
        for the allowlist: "may write a global artifact" *is* the allowlist test,
        and one chokepoint answering both keeps them from drifting apart.
        """
        return self._authorization.can_write_artifact(ctx, ArtifactScope(scope=AuthorityScope.GLOBAL))

    async def _assert_envelope_revision(
        self,
        session: AsyncSession,
        ctx: ArcRequestContext,
        revision_id: uuid.UUID,
        *,
        require_active: bool,
    ) -> uuid.UUID:
        """Resolve the revision, authorize the write at *its* scope, return its artifact.

        The scope comes from the artifact rather than from the caller. A caller
        who could name the scope of their own write could claim `tenant` for a
        global envelope and grant deployment-wide authority with a tenant admin
        role.
        """
        row = (await session.execute(_LOAD_REVISION, {"revision_id": revision_id})).first()
        if row is None:
            msg = f"revision {revision_id} not found"
            raise NotFoundError(msg)
        if row.kind != "policy":
            msg = f"revision {revision_id} belongs to a {row.kind} artifact; an envelope is a policy"
            raise NotAnEnvelope(msg)
        if require_active and row.lifecycle_state != "active":
            msg = f"revision {revision_id} is {row.lifecycle_state}; only an active revision may be bound"
            raise NotAnEnvelope(msg)

        # A tenant-scoped envelope governs only its own tenant's principals.
        # Checked separately from the write gate rather than relying on it,
        # because a deployment operator bypasses that gate and would otherwise
        # be able to bind one tenant's agent to another tenant's governance.
        # Not declarative like the `kind` check above: expressing it in SQL
        # needs the artifact's nullable tenant collapsed to a sentinel and a
        # three-column composite key, and the failure it prevents is a
        # misconfiguration by the deployment trust root rather than a privilege
        # escalation by anyone below it.
        if row.tenant_id is not None and row.tenant_id != ctx.tenant_id:
            msg = f"revision {revision_id} belongs to another tenant; an envelope governs only its own"
            raise NotAnEnvelope(msg)

        if not self._is_deployment_operator(ctx):
            scope = AuthorityScope.GLOBAL if row.tenant_id is None else AuthorityScope.TENANT
            self._authorization.assert_can_write_artifact(ctx, ArtifactScope(scope=scope, tenant_id=row.tenant_id))

        # After authorization, deliberately: a caller who may not write this
        # envelope learns that and nothing else about the document's approval
        # history.
        #
        # A global envelope is deployment-wide authority, so the document behind
        # it must have cleared three-principal approval. Not a new rule --
        # `global_mandatory` already demands exactly that at activation, and
        # this is the first place that insists on having got it. A revision
        # registered directly has no proposal version and so no classification
        # at all, which fails the same way and should: it went through no
        # approval whatsoever. Global only, matching the authority split; a
        # tenant envelope needs tenant admin, not a separation ritual.
        if require_active and row.tenant_id is None and row.risk_classification != _GLOBAL_ENVELOPE_CLASSIFICATION:
            found = row.risk_classification or "no proposal version"
            msg = (
                f"revision {revision_id} is classified {found}; a global envelope must be "
                f"{_GLOBAL_ENVELOPE_CLASSIFICATION}, which is what requires three distinct principals "
                "to have approved it. Author it with mandatory applicability rules."
            )
            raise InsufficientEnvelopeApproval(msg)

        artifact_id: uuid.UUID = row.artifact_id
        return artifact_id

    async def _flip(
        self,
        ctx: ArcRequestContext,
        binding_id: uuid.UUID,
        *,
        statement: TextClause,
        reason: str,
        event_type: str,
        absent: str,
        at_envelope_scope: bool,
    ) -> None:
        """The three status changes, which differ in statement, event and bar.

        `at_envelope_scope` selects the bar, and only `suspend` passes `False`.
        Everything that can free the principal's slot -- which is `revoke`, and
        `grant` on the way back in -- is authorized at the envelope's own scope,
        so a global one needs the deployment-operator allowlist. Passed
        explicitly rather than derived from the statement, because the
        difference is a security decision and should be legible at each call
        site.

        Every one re-resolves the binding rather than trusting that whoever
        holds the id may change it. Each statement carries `tenant_id`, the
        state it expects and an open-interval predicate, so a binding in another
        tenant, already in the destination state, or already ended affects no
        rows and is reported as absent instead of silently succeeding.

        All three parameters go to all three statements; `text()` binds only the
        ones its own SQL names, so a reinstate that stores no reason and a revoke
        that stores none simply do not reference it. The alternative -- a flag
        saying which statement wants which parameter -- restates in Python
        something the SQL above already says.
        """
        self._authorization.assert_request_tenant(ctx)
        _require_reason(reason)
        now = self._clock.now()
        params: dict[str, object] = {
            "binding_id": binding_id,
            "tenant_id": ctx.tenant_id,
            "now": now,
            "reason": reason[:_REASON_LIMIT],
        }

        async with self._session_factory() as session, session.begin():
            revision_id = await self._authorize_existing(session, ctx, binding_id, at_envelope_scope=at_envelope_scope)
            try:
                result = await session.execute(statement, params)
                await session.flush()
            except IntegrityError as clash:
                if not _is_principal_clash(clash):
                    raise
                msg = "another envelope covers this principal's window"
                raise EnvelopeAlreadyBound(msg) from clash
            if _rows_affected(result) == 0:
                msg = f"{absent} {binding_id}"
                raise NotFoundError(msg)

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=event_type,
                payload={
                    "binding_id": str(binding_id),
                    "revision_id": str(revision_id),
                    "reason": reason[:_REASON_LIMIT],
                },
            )

    async def _authorize_existing(
        self,
        session: AsyncSession,
        ctx: ArcRequestContext,
        binding_id: uuid.UUID,
        *,
        at_envelope_scope: bool,
    ) -> uuid.UUID:
        """Find the binding in the requesting tenant and authorize the change.

        The lookup is already tenant-scoped, so a binding belonging to another
        tenant is not found rather than refused -- holding an id from elsewhere
        should not even confirm that the id exists.
        """
        row = (
            await session.execute(
                text(
                    "SELECT revision_id FROM arc_autonomy_envelope_bindings "
                    "WHERE binding_id = :binding_id AND tenant_id = :tenant_id"
                ),
                {"binding_id": binding_id, "tenant_id": ctx.tenant_id},
            )
        ).first()
        if row is None:
            msg = f"envelope binding {binding_id} not found"
            raise NotFoundError(msg)
        if at_envelope_scope:
            # `require_active=False`: this authorizes a change to an *existing*
            # binding, and a revision superseded or revoked since the grant must
            # still be reinstatable and revocable. Only creating a binding
            # demands an active revision.
            await self._assert_envelope_revision(session, ctx, row.revision_id, require_active=False)
        else:
            self._authorization.assert_can_write_artifact(
                ctx, ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=ctx.tenant_id)
            )
        revision_id: uuid.UUID = row.revision_id
        return revision_id


def _is_principal_clash(exc: BaseException) -> bool:
    """Whether this integrity error is the one-envelope-per-principal constraint.

    Read from asyncpg's structured fields rather than from the message text.
    `sqlalchemy.exc.IntegrityError` wraps the asyncpg error as `.orig`, which
    carries `sqlstate`; the underlying `ExclusionViolationError` hangs off that
    as `__cause__` and carries `constraint_name`. Both are checked, because
    `23P01` alone would also catch a future exclusion constraint on this table.
    """
    orig = getattr(exc, "orig", None)
    state = getattr(exc, "sqlstate", None) or getattr(orig, "sqlstate", None)
    if str(state) != _EXCLUSION_VIOLATION:
        return False
    name = getattr(getattr(orig, "__cause__", None), "constraint_name", None)
    return name == _ONE_PER_PRINCIPAL


def _rows_affected(result: object) -> int:
    """How many rows a DML statement touched, as an int rather than an Optional.

    The same cast `context/derivative_handlers.py` and `signals/erasure.py` make,
    for the same reason: `execute` is typed as returning a generic `Result`,
    which has no `rowcount`, and every caller here has just run an UPDATE.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def _require_reason(reason: str) -> None:
    """Every write to this table says why.

    An authority record whose history reads "somebody changed this" is not an
    audit trail, and a blank string satisfying a NOT NULL is the usual way that
    happens.
    """
    if not reason.strip():
        msg = "an envelope binding change requires a reason"
        raise ValidationError(msg)


__all__ = [
    "AutonomyEnvelopeService",
    "BoundEnvelope",
    "EnvelopeAlreadyBound",
    "EnvelopeGrant",
    "InsufficientEnvelopeApproval",
    "NotAnEnvelope",
    "WorkloadIdentity",
]
