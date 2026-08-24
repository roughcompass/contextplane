"""Whether a refusal refuses: the advisory stage, and what it records.

`AutonomyDecisionService` answers whether a principal's envelope authorises an
act. This module answers the separate question of what to *do* with a refusal,
and the answer depends on how far the tenant has graduated.

**In `advisory`, nothing is refused and every refusal is written down.** Landing
"no envelope, no authority" as specified would refuse every agent in every
deployment on the day it shipped, because no principal has an envelope yet. The
advisory stage is how a deployment finds out which principals it would have
broken before it breaks them.

**In `enforcing`, a refusal refuses, and only the audit event is written.** The
scan row exists to answer "what would this have done", and a refusal that
actually refused has already answered it. The audit event is written either way,
under a different action -- `arc.envelope.authority.advisory` for a refusal that
did not refuse, `arc.envelope.authority.refused` for one that did -- so a count
by action answers how much enforcing a tenant would have broken, or did. That
question has no counter to ask it with: `contextplane/metrics.py` forbids
tenant-labelled series.

**The audit event and the scan row are not redundant.** An audit row could not
serve the graduation scan even in principle -- `audit_log.target_id` is a NOT
NULL UUID and a principal is an `(issuer, subject)` pair, so there is nothing for
the scan to key on. One is the trail; the other is the query surface.

**The stage is read per decision, from the row.** Same reason the envelope is:
no cache, so graduating a tenant takes effect at its next decision and demoting
one takes effect just as fast. This is a `SELECT` on `tenants` at the point of
use, which is what `is_regulated` and `memory_retention_days` already do.

**Permits write no row.** A principal that always acted inside its envelope is
not an offender and produces nothing for the graduation scan to count. Recording
permits would be volume with no reader, and the rate is one -- this is recording,
not sampling, and calling it sampling would imply a rate somebody could lower.

**A recording failure must not become an outage.** In `advisory` the whole point
is that the caller proceeds; if the insert fails, the caller still proceeds and
the failure is logged. The alternative -- letting a bookkeeping write refuse an
act that policy says is allowed -- would make the advisory stage strictly more
dangerous than the enforcing one it precedes. `enforcing` writes nothing, so it
has no such failure mode.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.autonomy_decision import AuthorityDecision, AutonomyDecisionService, EnvelopeVerdict
from contextplane.arc.service.autonomy_envelope import WorkloadIdentity
from contextplane.arc.types import ArcRequestContext, IntentManifest
from contextplane.audit import actions

_log = logging.getLogger(__name__)


class EnforcementStage(enum.StrEnum):
    """How far a tenant has graduated. Two values, and the order is the rollout."""

    ADVISORY = "advisory"
    ENFORCING = "enforcing"


#: The default for a tenant whose row predates the column, and the value the
#: column defaults to. Unusually for this service the safe default is the
#: permissive one: `enforcing` on an unmigrated tenant refuses every agent it
#: runs, and a fleet-wide availability failure is not a safer outcome than a
#: recorded one.
_DEFAULT_STAGE = EnforcementStage.ADVISORY

_READ_STAGE = text("SELECT envelope_enforcement_stage FROM tenants WHERE tenant_id = :tenant_id")

_RECORD = text(
    """
    INSERT INTO arc_envelope_advisory_records (
        record_id, tenant_id, principal_issuer, principal_subject, verdict,
        binding_id, revision_id, intent_kind, session_id, decided_at,
        data_sensitivity
    )
    VALUES (
        :record_id, :tenant_id, :issuer, :subject, :verdict,
        :binding_id, :revision_id, :intent_kind, :session_id, :decided_at,
        :data_sensitivity
    )
    """
)


@dataclasses.dataclass(frozen=True)
class EnforcementOutcome:
    """What the caller should do, and why.

    `blocked` is the only field a caller has to read. The rest is for the audit
    trail and for tests, and is carried rather than recomputed so that "why did
    this proceed" has one answer rather than two that can disagree.
    """

    decision: AuthorityDecision
    stage: EnforcementStage

    #: Whether the caller must refuse. False in `advisory` even for a refused
    #: decision -- that is the entire meaning of the stage.
    blocked: bool

    #: Whether an advisory row was written. False for a permit, false in
    #: `enforcing`, and false when the write failed -- which is why it is
    #: reported rather than assumed from `stage` and `decision`.
    recorded: bool

    @property
    def would_have_been_blocked(self) -> bool:
        """What `blocked` would say once this tenant graduates."""
        return not self.decision.is_permitted


class AutonomyEnforcementService:
    """Applies a tenant's enforcement stage to an authority decision."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        decisions: AutonomyDecisionService,
    ) -> None:
        self._session_factory = session_factory
        self._decisions = decisions

    async def evaluate(
        self,
        ctx: ArcRequestContext,
        manifest: IntentManifest,
        *,
        principal: WorkloadIdentity | None = None,
    ) -> EnforcementOutcome:
        """Decide, then apply the tenant's stage to the verdict."""
        decision = await self._decisions.decide(ctx, manifest, principal=principal)
        stage = await self._stage(ctx.tenant_id)

        if decision.is_permitted:
            return EnforcementOutcome(decision=decision, stage=stage, blocked=False, recorded=False)
        if stage is EnforcementStage.ENFORCING:
            await self._write(ctx.tenant_id, decision, manifest, store=False)
            return EnforcementOutcome(decision=decision, stage=stage, blocked=True, recorded=False)

        recorded = await self._write(ctx.tenant_id, decision, manifest, store=True)
        return EnforcementOutcome(decision=decision, stage=stage, blocked=False, recorded=recorded)

    async def _stage(self, tenant_id: uuid.UUID) -> EnforcementStage:
        """This tenant's stage, read fresh.

        An unknown tenant reads as the default rather than raising: the caller
        has already been admitted by the tenant middleware, so a missing row here
        means the tenant was deleted mid-request, and refusing on that basis
        would turn a race into an outage.
        """
        async with self._session_factory() as session:
            value = (await session.execute(_READ_STAGE, {"tenant_id": tenant_id})).scalar_one_or_none()
        return EnforcementStage(value) if value is not None else _DEFAULT_STAGE

    async def _write(
        self,
        tenant_id: uuid.UUID,
        decision: AuthorityDecision,
        manifest: IntentManifest,
        *,
        store: bool,
    ) -> bool:
        """Record the refusal: an audit event always, a scan row only in advisory.

        **Two records, and they are not redundant.** The audit event is the
        trail -- a count by action answers "how much would enforcing this tenant
        have broken", which the no-tenant-label rule on metrics means no counter
        can answer. `arc_envelope_advisory_records` is the graduation scan's
        substrate, keyed on the principal, and an audit row could not serve it
        even in principle: `audit_log.target_id` is a NOT NULL UUID and a
        principal is an `(issuer, subject)` pair.

        `store` is False under enforcement. The scan row exists to answer "what
        would this have done", and a refusal that actually refused has already
        answered it.

        One transaction of its own, not the caller's. Both writes are records
        *about* a decision, so joining them to whatever the caller is doing
        would let an unrelated rollback erase the evidence -- and in advisory
        the caller is proceeding regardless, so there is no transaction of
        theirs that ought to carry it.
        """
        event = actions.ARC_ENVELOPE_AUTHORITY_ADVISORY if store else actions.ARC_ENVELOPE_AUTHORITY_REFUSED
        try:
            async with self._session_factory() as session, session.begin():
                if store:
                    await session.execute(
                        _RECORD,
                        {
                            "record_id": uuid.uuid4(),
                            "tenant_id": tenant_id,
                            "issuer": decision.principal.issuer,
                            "subject": decision.principal.subject,
                            "verdict": str(decision.verdict),
                            "binding_id": decision.binding_id,
                            "revision_id": decision.revision_id,
                            "intent_kind": str(manifest.intent_kind),
                            "session_id": manifest.session_id,
                            "decided_at": decision.decided_at,
                            # Null when the manifest carried none, which the
                            # selection engine reads as most restrictive.
                            # Recorded as null rather than as `restricted` so a
                            # reader can tell a declared tier from an absent one
                            # -- the same verdict, and a different fact about
                            # whose omission it was.
                            "data_sensitivity": manifest.data_sensitivity,
                        },
                    )
                await audit_outbox.emit(
                    session,
                    tenant_id=tenant_id,
                    event_type=event,
                    payload={
                        "verdict": str(decision.verdict),
                        "principal_issuer": decision.principal.issuer,
                        "principal_subject": decision.principal.subject,
                        "binding_id": str(decision.binding_id) if decision.binding_id else None,
                        "intent_kind": str(manifest.intent_kind),
                        "session_id": manifest.session_id,
                    },
                )
        except Exception:  # noqa: BLE001 - a record about an act must never block the act
            # Deliberately broad, and deliberately not re-raised. In `advisory`
            # the caller proceeds by definition; letting a bookkeeping write
            # refuse an act that policy allows would make this stage more
            # dangerous than the enforcing one it exists to precede. Under
            # enforcement the act is already refused, and losing the trail must
            # not turn a clean refusal into a 500. The lost row is the correct
            # thing to lose in both cases, which is why this logs at `exception`.
            _log.exception(
                "envelope authority refusal was not recorded",
                extra={"tenant_id": str(tenant_id), "verdict": str(decision.verdict), "event": event},
            )
            return False
        return store


def stage_of(value: str | None) -> EnforcementStage:
    """Parse a stored stage, defaulting rather than raising.

    Shared with the graduation pre-flight so both read a NULL or absent value
    the same way. A CHECK constraint already closes the column's vocabulary, so
    an unparseable value means the constraint and this enum have drifted -- which
    `EnforcementStage(value)` will raise on, loudly, rather than defaulting a
    tenant into the permissive stage on a typo.
    """
    return EnforcementStage(value) if value is not None else _DEFAULT_STAGE


#: One refusal code per verdict, and the reason they are distinct: the caller's
#: remedy differs. `envelope_absent` is somebody else's job; `envelope_excluded`
#: is the agent doing something it should not.
#:
#: Moved down here from the HTTP adapter when the MCP transport needed the same
#: codes. A second copy would have been a second vocabulary, and the transport
#: that got the newer one would say a different thing about the same decision.
REFUSAL_CODES: Final[dict[str, str]] = {
    "no_envelope": "envelope_absent",
    "envelope_suspended": "envelope_suspended",
    "envelope_withdrawn": "envelope_withdrawn",
    "outside_envelope": "envelope_excluded",
}

#: What the caller is told, on either transport. Deliberately not the matched
#: rule, the bound revision, or anything about the matrix: a refusal that
#: explained itself would let a caller map its own envelope by probing.
REFUSAL_MESSAGE: Final = "this principal's autonomy envelope does not authorise this action"


class EnvelopeRefused(Exception):
    """The envelope refused, and the transport decides how to say so.

    A distinct type rather than a transport error, for the reason
    `AdmissionRefused` is one: every caller has to treat it as terminal, and the
    two transports have to shape the same decision into a 403 and a `ToolError`
    without either owning the vocabulary.

    Carries the whole outcome rather than just the code, because a caller that
    wants to record what happened should not have to ask a second time.
    """

    def __init__(self, outcome: EnforcementOutcome) -> None:
        self.outcome = outcome
        self.code = REFUSAL_CODES.get(str(outcome.decision.verdict), "envelope_excluded")
        super().__init__(REFUSAL_MESSAGE)


async def enforce_or_refuse(
    enforcement: AutonomyEnforcementService,
    arc_context: ArcRequestContext,
    manifest: IntentManifest,
) -> EnforcementOutcome:
    """Evaluate the envelope and raise if the stage says to refuse.

    **Transport-neutral on purpose.** This lived in `api/envelope_guard.py`,
    which meant it governed the one HTTP route that called it and no MCP tool at
    all -- so the same act performed through the tool bypassed the envelope
    entirely. Every service here has two transports, and a guard that lives in
    an HTTP adapter is always missing from the second one; `admit_or_refuse` was
    moved for exactly this reason, and the MCP memory tool still carries the
    comment about the period when it scanned nothing.

    In `advisory` this always returns rather than raising. That is the whole of
    the rollout bargain: the decision runs, the would-be refusal is recorded,
    and the caller proceeds.
    """
    outcome = await enforcement.evaluate(arc_context, manifest)
    if outcome.blocked:
        raise EnvelopeRefused(outcome)
    return outcome


__all__ = [
    "REFUSAL_CODES",
    "REFUSAL_MESSAGE",
    "AutonomyEnforcementService",
    "EnforcementOutcome",
    "EnforcementStage",
    "EnvelopeRefused",
    "EnvelopeVerdict",
    "enforce_or_refuse",
    "stage_of",
]
