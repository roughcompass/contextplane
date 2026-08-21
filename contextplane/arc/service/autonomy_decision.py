"""The authority decision: may this principal do the thing it is about to do?

Reads the principal's envelope binding and the applicability rules on the
revision it names, and answers. **Nothing is cached, so nothing needs
invalidating** -- both reads happen on every decision, which is the whole
mechanism by which a suspension takes effect. A status flip on the binding row
is visible to the next decision made by any replica because no replica is
holding a copy, and `contextplane/sharing/grants.py` already states the same rule
for the one grant table that ships.

**The SLO is a bound on operations, not on wall-clock.** A suspended envelope
authorises no operation that begins after the flip commits. That is testable and
tested. "Sub-second" is not: it would be a claim about how long an in-flight
operation may run, which this service does not bound, and the latency histogram
tops out at ten seconds so no number outside that range is even observable.

**Deny by default.** An act is permitted when the envelope in force carries a
rule matching the manifest, and refused otherwise -- including when no rule
matches, which is the ordinary way an act falls outside a narrow envelope. An
envelope that means "this agent may do anything" is expressible, as a
global-scope rule with no selectors, and it says so rather than arising from an
absence.

**Five verdicts, not a boolean.** `no_envelope`, `envelope_suspended`,
`envelope_withdrawn` and `outside_envelope` are different facts about a refusal
and the later stages of this rollout need to tell them apart: the advisory stage
records what *would* have been refused, and the graduation pre-flight refuses to
flip a tenant to enforcing while any principal acted with no envelope at all.
Collapsing them to `False` would make that scan unbuildable.

**A revision withdrawn after binding refuses.** `resolve` reports the bound
revision's lifecycle state precisely so this can be decided here rather than
hidden in the read: a `policy` revision that ARC has revoked or superseded is a
governance document somebody deliberately took out of force, and continuing to
derive authority from it is the failure the revocation was meant to cause.

**The predicate never sees a principal.** `decide` passes the manifest to
`rule_applies` and nothing else; which principal is asking has already been
answered by *which envelope was resolved*. That separation is the point of the
binding being a separate table: a principal smuggled into an applicability
dimension would sit outside `_SCOPE_ORDER`, so precedence would not see it, and
a rule meant to narrow authority for one agent would widen it for every agent
matching the same domain.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.autonomy_envelope import AutonomyEnvelopeService, BoundEnvelope, WorkloadIdentity
from contextplane.arc.service.corpus import rule_from_row
from contextplane.arc.service.selection import rule_applies
from contextplane.arc.types import ApplicabilityRule, ArcRequestContext, ArcVocabularyError, IntentManifest
from contextplane.types import Clock

#: The lifecycle state a bound revision must still be in for its rules to carry
#: authority. Deliberately just `active`: `draft` never passed approval,
#: `superseded` was replaced by a revision this principal is not bound to, and
#: `revoked`/`expired` were taken out of force on purpose.
_IN_FORCE = "active"

_LOAD_RULES = text(
    """
    SELECT rule_id, revision_id, scope, target_tenant_id, entity_ids, domain_ids,
           intent_kinds, action_classes, environments, data_sensitivity_tiers,
           is_mandatory,
           effective_from AS rule_effective_from,
           effective_until AS rule_effective_until
    FROM arc_applicability_rules
    WHERE revision_id = :revision_id
    ORDER BY rule_id
    """
)


class EnvelopeVerdict(enum.StrEnum):
    """Why a principal may or may not act."""

    PERMITTED = "permitted"

    #: Nobody has bound this principal to an envelope. Distinct from every other
    #: refusal because the graduation pre-flight counts exactly these.
    NO_ENVELOPE = "no_envelope"

    #: An envelope is bound and somebody switched it off.
    ENVELOPE_SUSPENDED = "envelope_suspended"

    #: The bound revision is no longer in force -- revoked, superseded, expired
    #: or never activated. The binding still stands; the document behind it does
    #: not.
    ENVELOPE_WITHDRAWN = "envelope_withdrawn"

    #: An envelope is in force and none of its rules cover this act. The
    #: ordinary refusal, and the only one that says the envelope is working.
    OUTSIDE_ENVELOPE = "outside_envelope"


@dataclasses.dataclass(frozen=True)
class AuthorityDecision:
    """One decision, and enough of its reasoning to record or replay."""

    verdict: EnvelopeVerdict
    principal: WorkloadIdentity
    decided_at: datetime.datetime

    #: Present whenever a binding was found, including for a refusal -- an
    #: auditor asking "which envelope refused this" needs it more than an
    #: auditor asking about a permit.
    binding_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None

    #: The rule that permitted the act. Exactly one is recorded even when
    #: several match, because the question this answers is "was there
    #: authority", not "how much". Which one is deterministic: rules are ordered
    #: by `rule_id` and the first match wins.
    matched_rule_id: uuid.UUID | None = None

    @property
    def is_permitted(self) -> bool:
        return self.verdict is EnvelopeVerdict.PERMITTED


def decide(
    *,
    principal: WorkloadIdentity,
    envelope: BoundEnvelope | None,
    rules: tuple[ApplicabilityRule, ...],
    manifest: IntentManifest,
    tenant_id: uuid.UUID,
    as_of: datetime.datetime,
) -> AuthorityDecision:
    """Whether the envelope authorises this manifest. Pure.

    Same inputs in, same verdict out, including a replay months later -- `as_of`
    is a parameter rather than a clock read for the same reason `rule_applies`
    takes one. The impure half is which envelope and which rules get loaded, and
    that lives in the service below.

    **`principal` is a parameter and not read off the envelope**, because the
    verdict that matters most is the one where there is no envelope to read it
    from. An earlier version took it from `envelope.principal` and substituted a
    placeholder for `no_envelope` -- which erased exactly the identity the
    graduation scan counts, on exactly the rows it counts them from.
    """
    common = {"principal": principal, "decided_at": as_of}

    if envelope is None:
        return AuthorityDecision(verdict=EnvelopeVerdict.NO_ENVELOPE, **common)  # type: ignore[arg-type]

    located = {
        **common,
        "binding_id": envelope.binding_id,
        "revision_id": envelope.revision_id,
    }
    if not envelope.is_in_force:
        return AuthorityDecision(verdict=EnvelopeVerdict.ENVELOPE_SUSPENDED, **located)  # type: ignore[arg-type]
    if envelope.revision_lifecycle_state != _IN_FORCE:
        return AuthorityDecision(verdict=EnvelopeVerdict.ENVELOPE_WITHDRAWN, **located)  # type: ignore[arg-type]

    for rule in rules:
        if rule_applies(rule, manifest, tenant_id=tenant_id, as_of=as_of):
            return AuthorityDecision(
                verdict=EnvelopeVerdict.PERMITTED,
                matched_rule_id=rule.rule_id,
                **located,  # type: ignore[arg-type]
            )
    return AuthorityDecision(verdict=EnvelopeVerdict.OUTSIDE_ENVELOPE, **located)  # type: ignore[arg-type]


class AutonomyDecisionService:
    """Answers the authority question, reading both halves fresh every time."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        envelopes: AutonomyEnvelopeService,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._envelopes = envelopes
        self._authorization = authorization
        self._clock = clock

    async def decide(
        self,
        ctx: ArcRequestContext,
        manifest: IntentManifest,
        *,
        principal: WorkloadIdentity | None = None,
        at: datetime.datetime | None = None,
    ) -> AuthorityDecision:
        """Resolve the principal's envelope and evaluate it against the manifest.

        `principal` defaults to the requester, which is the case that matters:
        an agent asking whether it may act. Naming another principal is for an
        operator previewing somebody else's authority, and is gated by the same
        tenant admission as any other ARC read.
        """
        self._authorization.assert_request_tenant(ctx)
        subject = principal or WorkloadIdentity.of_requester(ctx)
        as_of = at or self._clock.now()

        envelope = await self._envelopes.resolve(ctx, subject, at=as_of)
        rules: tuple[ApplicabilityRule, ...] = ()
        if envelope is not None and envelope.is_in_force:
            rules = await self._load_rules(envelope.revision_id)

        return decide(
            principal=subject,
            envelope=envelope,
            rules=rules,
            manifest=manifest,
            tenant_id=ctx.tenant_id,
            as_of=as_of,
        )

    async def _load_rules(self, revision_id: uuid.UUID) -> tuple[ApplicabilityRule, ...]:
        """Every applicability rule on the bound revision, in a stable order.

        A row the rule constructor refuses is skipped rather than raised, and
        skipping is the safe direction here in a way it is not for an obligation:
        a rule that cannot be read cannot be shown to authorise anything, so
        dropping it can only refuse. `_obligation_rule` makes the opposite choice
        for the opposite reason -- an unreadable obligation must still block.
        """
        async with self._session_factory() as session:
            rows = (await session.execute(_LOAD_RULES, {"revision_id": revision_id})).all()

        readable: list[ApplicabilityRule] = []
        for row in rows:
            try:
                readable.append(rule_from_row(row))
            except ArcVocabularyError:
                continue
        return tuple(readable)


__all__ = [
    "AuthorityDecision",
    "AutonomyDecisionService",
    "EnvelopeVerdict",
    "decide",
]
