"""Who may see a task, and on what evidence.

Every read path on task memory — content, count, list, lexical lookup — asks
this module first. That ordering is the point: a path that fetches rows and then
filters them has already decided the task exists, and the shape of the answer
leaks that even when the rows do not.

**Explicit grants are the durable authority.** An entitlement system may say an
actor belongs on a task, but that answer is a fact about a moment, not a
standing permission. So it is materialized into a grant that records which
resolver decided it, what evidence it saw, and when the answer stops counting.
Reads then consult stored grants only — nothing here calls an entitlement
service, because a read path that depends on a live external answer fails
whenever that service does, and failing a read closed on someone who is
genuinely a participant is its own outage.

**Uncertainty denies.** A grant from a resolver this build does not recognize, a
grant whose evidence has aged past its own window, a grant with no evidence at
all: each denies. The alternative — treating an unreadable grant as absent-but-
harmless, or as present-because-it-was-once-valid — picks a side on a question
nobody answered. Denying is the only outcome that cannot silently widen an
audience.

**A tenant is not an audience.** Sharing a tenant with a task confers nothing
here. That is deliberate and it is the difference between this and ordinary
workspace visibility: a task is a bounded collaboration between named actors,
and "everyone in the tenant" is the boundary being replaced.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING

from contextplane.exceptions import RegistryError
from contextplane.workspaces.schemas.task_memory import (
    PARTICIPANT_ROLES,
    ROLE_AUDITOR,
    ROLE_CONTRIBUTOR,
    ROLE_OWNER,
    ROLE_READER,
    ParticipantRole,
    TaskParticipantGrantV1,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

# Which roles may do what, as explicit sets rather than an ordering. The role
# vocabulary is deliberately not comparable -- an `auditor` reads everything and
# writes nothing, which no linear order places correctly -- so a `>= contributor`
# test is how `auditor` silently acquires write access the day somebody reorders
# the list. Membership is checked, never rank.
ROLES_THAT_READ: frozenset[str] = frozenset({ROLE_READER, ROLE_CONTRIBUTOR, ROLE_OWNER, ROLE_AUDITOR})
ROLES_THAT_EXTEND: frozenset[str] = frozenset({ROLE_CONTRIBUTOR, ROLE_OWNER})
ROLES_THAT_ADMINISTER: frozenset[str] = frozenset({ROLE_OWNER})

# Named so a caller asks for a capability instead of naming roles at the call
# site. A call site that lists roles itself is a second copy of this table.
CAPABILITY_READ = "read"
CAPABILITY_EXTEND = "extend"
CAPABILITY_ADMINISTER = "administer"

_CAPABILITIES: dict[str, frozenset[str]] = {
    CAPABILITY_READ: ROLES_THAT_READ,
    CAPABILITY_EXTEND: ROLES_THAT_EXTEND,
    CAPABILITY_ADMINISTER: ROLES_THAT_ADMINISTER,
}

# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

#: A grant somebody conferred directly. Durable: it does not stop counting
#: because the entitlement resolver changed, since no resolver produced it.
RESOLVER_EXPLICIT = "explicit/v1"

#: The entitlement resolver this build understands. A grant naming any other
#: entitlement resolver is not evidence here -- see `_resolver_is_recognized`.
ENTITLEMENT_RESOLVER = "entitlement/v1"

RECOGNIZED_RESOLVERS: frozenset[str] = frozenset({RESOLVER_EXPLICIT, ENTITLEMENT_RESOLVER})


class AudienceDenied(RegistryError):
    """The actor is not an authorized participant for the requested capability.

    Routers map this to HTTP 403 and must not vary the response by reason. The
    reason strings below are for the audit record and the server log; handing
    them to the caller would turn one denial into a probe that distinguishes
    "no such task", "not a participant", and "grant expired" -- three answers
    that together enumerate the tenant's tasks.
    """


@dataclasses.dataclass(frozen=True)
class EntitlementEvidence:
    """What an entitlement resolver saw, and for how long it counts.

    `max_age` is carried with the evidence rather than read from configuration
    at check time: an answer resolved under a one-hour window does not become a
    one-day answer because somebody widened a setting afterwards.
    """

    resolver_version: str
    resolved_at: datetime.datetime
    max_age: datetime.timedelta
    # The system that answered, and the authority it answered for. Kept
    # separate for the same reason the trust contract separates them: a relay
    # that forwards an answer is not the body that stands behind it.
    source: str
    authority: str

    def __post_init__(self) -> None:
        for name, value in (
            ("resolver_version", self.resolver_version),
            ("source", self.source),
            ("authority", self.authority),
        ):
            if not value.strip():
                raise AudienceDenied(f"entitlement evidence needs a {name}")
        if self.resolved_at.tzinfo is None:
            raise AudienceDenied("entitlement evidence needs a timezone-aware resolved_at")
        if self.max_age <= datetime.timedelta(0):
            raise AudienceDenied(
                "entitlement evidence with a non-positive max_age was never valid; "
                "a zero window is indistinguishable from a missing one"
            )

    def is_fresh_at(self, moment: datetime.datetime) -> bool:
        """Whether this evidence still counts at *moment*."""
        if moment.tzinfo is None:
            raise AudienceDenied("cannot age entitlement evidence against a naive timestamp")
        return self.resolved_at <= moment < self.resolved_at + self.max_age


@dataclasses.dataclass(frozen=True)
class AudienceDecision:
    """One authorization answer, in the shape the audit log stores.

    Recorded for denials as well as grants. A log that only records successful
    access answers "who read this" and not "who tried", and the second question
    is the one asked after an incident.
    """

    task_id: uuid.UUID
    actor_id: str
    capability: str
    allowed: bool
    # Present only when allowed; a denial deliberately carries no role, because
    # "denied as reader" would imply a grant that was not honoured.
    role: ParticipantRole | None
    reason: str
    resolver_version: str | None
    decided_at: datetime.datetime


def _resolver_is_recognized(resolver_version: str) -> bool:
    return resolver_version.strip() in RECOGNIZED_RESOLVERS


def active_grant_for(
    grants: Iterable[TaskParticipantGrantV1],
    *,
    actor_id: str,
    moment: datetime.datetime,
) -> TaskParticipantGrantV1 | None:
    """The one grant that confers something on *actor_id* at *moment*.

    Returns `None` rather than raising: absence is a normal answer, and the
    caller decides whether absence is a denial (a read) or a precondition (an
    owner about to issue a grant).

    A grant naming an unrecognized resolver is skipped as though it were not
    there. It is not evidence in this build, and treating it as evidence would
    honour a rule this code cannot see.
    """
    if moment.tzinfo is None:
        raise AudienceDenied("cannot resolve a task audience against a naive timestamp")
    wanted = actor_id.strip()
    if not wanted:
        return None
    for grant in grants:
        if grant.actor_id.strip() != wanted:
            continue
        if not _resolver_is_recognized(grant.resolver_version):
            continue
        if grant.is_active_at(moment):
            return grant
    return None


def decide(
    grants: Iterable[TaskParticipantGrantV1],
    *,
    task_id: uuid.UUID,
    actor_id: str,
    capability: str,
    moment: datetime.datetime,
) -> AudienceDecision:
    """Resolve *capability* for *actor_id* on *task_id*, and say why.

    Returns the decision rather than raising, so a caller that needs to record
    a denial has the record before the exception exists. `require` is the
    raising wrapper around this.
    """
    if capability not in _CAPABILITIES:
        # Not a denial: a caller asking for a capability that does not exist is
        # a programming error, and answering "denied" would let a typo read as
        # a policy outcome.
        raise ValueError(f"unknown capability {capability!r}; legal values are {sorted(_CAPABILITIES)}")

    grant = active_grant_for(grants, actor_id=actor_id, moment=moment)
    if grant is None:
        return AudienceDecision(
            task_id=task_id,
            actor_id=actor_id,
            capability=capability,
            allowed=False,
            role=None,
            reason="no active participant grant",
            resolver_version=None,
            decided_at=moment,
        )

    permitted = _CAPABILITIES[capability]
    if grant.role not in permitted:
        return AudienceDecision(
            task_id=task_id,
            actor_id=actor_id,
            capability=capability,
            allowed=False,
            role=None,
            reason=f"role {grant.role!r} does not carry {capability!r}",
            resolver_version=grant.resolver_version,
            decided_at=moment,
        )

    return AudienceDecision(
        task_id=task_id,
        actor_id=actor_id,
        capability=capability,
        allowed=True,
        role=grant.role,
        reason=f"active {grant.role!r} grant",
        resolver_version=grant.resolver_version,
        decided_at=moment,
    )


def require(
    grants: Iterable[TaskParticipantGrantV1],
    *,
    task_id: uuid.UUID,
    actor_id: str,
    capability: str,
    moment: datetime.datetime,
) -> AudienceDecision:
    """`decide`, raising `AudienceDenied` when the answer is no.

    The raised message names the capability and nothing about the task's
    existence or its other participants. Routers turn it into a bare 403.
    """
    decision = decide(
        grants,
        task_id=task_id,
        actor_id=actor_id,
        capability=capability,
        moment=moment,
    )
    if not decision.allowed:
        raise AudienceDenied(f"actor is not authorized to {capability} this task")
    return decision


def materialize_entitlement_grant(
    *,
    task_id: uuid.UUID,
    actor_id: str,
    role: ParticipantRole,
    granted_by: str,
    evidence: EntitlementEvidence,
    moment: datetime.datetime,
) -> TaskParticipantGrantV1:
    """Turn a fresh entitlement answer into a stored grant that expires.

    The grant's `expires_at` is the moment the evidence stops counting, not a
    separate policy number. Two windows would drift, and the longer one would
    win by accident -- which is how an entitlement answer becomes permanent.

    Fails closed on stale evidence rather than materializing a grant that is
    already expired. Such a grant would be harmless to read and misleading to
    audit: the record would show an audience that never applied.
    """
    if role not in PARTICIPANT_ROLES:
        raise AudienceDenied(f"unknown participant role {role!r}; legal values are {sorted(PARTICIPANT_ROLES)}")
    if evidence.resolver_version.strip() != ENTITLEMENT_RESOLVER:
        raise AudienceDenied(
            "entitlement evidence names a resolver this build does not recognize; "
            "an answer produced under an unknown rule is not evidence for this one"
        )
    if not evidence.is_fresh_at(moment):
        raise AudienceDenied(
            "entitlement evidence is stale; re-resolve before granting participation, "
            "because a grant minted from an expired answer would already have lapsed"
        )

    return TaskParticipantGrantV1(
        task_id=task_id,
        actor_id=actor_id,
        role=role,
        granted_by=granted_by,
        granted_at=moment,
        expires_at=evidence.resolved_at + evidence.max_age,
        resolver_version=ENTITLEMENT_RESOLVER,
    )


def revoked_at(grant: TaskParticipantGrantV1, *, moment: datetime.datetime) -> TaskParticipantGrantV1:
    """The same grant, ended at *moment*.

    Revocation is temporal, not a delete: the row stays and stops applying. A
    deleted grant erases the fact that the actor ever had access, which is
    exactly the fact an audit of a past read needs.

    A grant already ended earlier is returned unchanged, so revoking twice does
    not extend the first revocation.
    """
    if moment.tzinfo is None:
        raise AudienceDenied("cannot revoke a grant at a naive timestamp")
    if moment <= grant.granted_at:
        # Ending a grant at or before it began would store a window that never
        # opened, which the grant contract refuses outright. Callers revoking a
        # future-dated grant should delete the row they just wrote instead.
        raise AudienceDenied(
            "cannot revoke a grant at or before the moment it was granted; "
            "that window never applied and storing it would misreport the audience"
        )
    if grant.expires_at is not None and grant.expires_at <= moment:
        return grant
    return dataclasses.replace(grant, expires_at=moment)


__all__ = [
    "CAPABILITY_ADMINISTER",
    "CAPABILITY_EXTEND",
    "CAPABILITY_READ",
    "ENTITLEMENT_RESOLVER",
    "RECOGNIZED_RESOLVERS",
    "RESOLVER_EXPLICIT",
    "ROLES_THAT_ADMINISTER",
    "ROLES_THAT_EXTEND",
    "ROLES_THAT_READ",
    "AudienceDecision",
    "AudienceDenied",
    "EntitlementEvidence",
    "active_grant_for",
    "decide",
    "materialize_entitlement_grant",
    "require",
    "revoked_at",
]
