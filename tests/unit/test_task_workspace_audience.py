"""What a task audience resolves to, and what it refuses.

The resolver is the first thing every task read consults, so the cases that
matter here are the ones where an answer would widen the audience: a grant from
a rule this build cannot read, an entitlement answer that has aged out, a role
asked to do something it does not carry, a revocation that arrives while a
grant is still nominally in force.

Roles are checked by membership, never by rank. There is a test for that
specifically, because a linear order is the natural thing to reach for and
`auditor` is where it goes wrong.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

import pytest

from contextplane.workspaces.audience import (
    CAPABILITY_ADMINISTER,
    CAPABILITY_EXTEND,
    CAPABILITY_READ,
    ENTITLEMENT_RESOLVER,
    RESOLVER_EXPLICIT,
    ROLES_THAT_ADMINISTER,
    ROLES_THAT_EXTEND,
    ROLES_THAT_READ,
    AudienceDenied,
    EntitlementEvidence,
    active_grant_for,
    decide,
    materialize_entitlement_grant,
    require,
    revoked_at,
)
from contextplane.workspaces.schemas.task_memory import (
    PARTICIPANT_ROLES,
    ROLE_AUDITOR,
    ROLE_CONTRIBUTOR,
    ROLE_OWNER,
    ROLE_READER,
    ParticipantRole,
    TaskParticipantGrantV1,
)

_TASK = uuid.UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)


def _grant(
    *,
    actor: str = "agent-2",
    role: ParticipantRole = ROLE_CONTRIBUTOR,
    granted_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    resolver: str = RESOLVER_EXPLICIT,
) -> TaskParticipantGrantV1:
    return TaskParticipantGrantV1(
        task_id=_TASK,
        actor_id=actor,
        role=role,
        granted_by="agent-1",
        granted_at=granted_at or (_NOW - _HOUR),
        expires_at=expires_at,
        resolver_version=resolver,
    )


def _evidence(
    *,
    resolver: str = ENTITLEMENT_RESOLVER,
    resolved_at: datetime.datetime | None = None,
    max_age: datetime.timedelta = _HOUR,
) -> EntitlementEvidence:
    return EntitlementEvidence(
        resolver_version=resolver,
        resolved_at=resolved_at or _NOW,
        max_age=max_age,
        source="entitlement-service",
        authority="platform-directory",
    )


# --- capability tables --------------------------------------------------------


def test_every_role_can_read() -> None:
    """Including auditor. Read is the capability every participant has."""
    assert ROLES_THAT_READ == PARTICIPANT_ROLES


def test_auditor_reads_but_cannot_extend_or_administer() -> None:
    """The case a linear role order gets wrong.

    An auditor is broader than a reader in intent and narrower in write access,
    so any ordering that puts it above `reader` hands it a contributor's
    permissions the moment somebody writes a `>=` check.
    """
    assert ROLE_AUDITOR in ROLES_THAT_READ
    assert ROLE_AUDITOR not in ROLES_THAT_EXTEND
    assert ROLE_AUDITOR not in ROLES_THAT_ADMINISTER


def test_only_the_owner_administers() -> None:
    assert ROLES_THAT_ADMINISTER == {ROLE_OWNER}
    assert ROLES_THAT_EXTEND == {ROLE_CONTRIBUTOR, ROLE_OWNER}


@pytest.mark.parametrize("role", sorted(PARTICIPANT_ROLES))
def test_no_role_is_outside_the_capability_tables(role: str) -> None:
    """A role the tables never mention would silently carry nothing, which
    reads as a revoked participant rather than as a missing table entry."""
    assert role in ROLES_THAT_READ | ROLES_THAT_EXTEND | ROLES_THAT_ADMINISTER


# --- resolving an active grant ------------------------------------------------


def test_an_active_grant_resolves() -> None:
    assert active_grant_for([_grant()], actor_id="agent-2", moment=_NOW) is not None


def test_no_grant_resolves_to_nothing() -> None:
    assert active_grant_for([], actor_id="agent-2", moment=_NOW) is None


def test_another_actors_grant_confers_nothing() -> None:
    assert active_grant_for([_grant(actor="agent-9")], actor_id="agent-2", moment=_NOW) is None


def test_a_grant_that_has_not_started_confers_nothing() -> None:
    future = _grant(granted_at=_NOW + _HOUR)
    assert active_grant_for([future], actor_id="agent-2", moment=_NOW) is None


def test_an_expired_grant_confers_nothing() -> None:
    lapsed = _grant(granted_at=_NOW - (2 * _HOUR), expires_at=_NOW - _HOUR)
    assert active_grant_for([lapsed], actor_id="agent-2", moment=_NOW) is None


def test_expiry_is_exclusive_at_the_boundary() -> None:
    """A grant expiring exactly now has stopped applying.

    The boundary has to be decided rather than left to chance: inclusive would
    mean a revocation timestamped `now` still authorizes the request that
    triggered it.
    """
    ending = _grant(granted_at=_NOW - _HOUR, expires_at=_NOW)
    assert active_grant_for([ending], actor_id="agent-2", moment=_NOW) is None


def test_a_grant_from_an_unrecognized_resolver_is_not_evidence() -> None:
    """Fail closed on a rule this build cannot read.

    Honouring it would apply a resolution rule whose text this code has never
    seen, which is indistinguishable from having no rule.
    """
    foreign = _grant(resolver="some-future-resolver/v9")
    assert active_grant_for([foreign], actor_id="agent-2", moment=_NOW) is None


def test_a_naive_moment_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(AudienceDenied, match="naive timestamp"):
        active_grant_for([_grant()], actor_id="agent-2", moment=datetime.datetime(2026, 8, 8, 12, 0))


def test_a_blank_asking_actor_matches_nothing() -> None:
    """A stripped-empty actor is not a participant.

    The other half of this — a stored grant with a blank `actor_id` — cannot
    exist to be matched against: the grant contract refuses one at construction.
    So the only reachable case is the asking side, and it must not resolve to
    the first row it sees.
    """
    assert active_grant_for([_grant()], actor_id="   ", moment=_NOW) is None


# --- decisions and denials ----------------------------------------------------


def test_a_contributor_may_read_and_extend_but_not_administer() -> None:
    grants = [_grant(role=ROLE_CONTRIBUTOR)]
    assert decide(grants, task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_READ, moment=_NOW).allowed
    assert decide(grants, task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_EXTEND, moment=_NOW).allowed
    assert not decide(grants, task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_ADMINISTER, moment=_NOW).allowed


def test_a_reader_may_not_extend() -> None:
    decision = decide(
        [_grant(role=ROLE_READER)], task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_EXTEND, moment=_NOW
    )
    assert not decision.allowed
    assert "does not carry" in decision.reason


def test_a_denial_carries_no_role() -> None:
    """`role` is the honoured grant. A denial that reported one would read as a
    grant the system declined to respect."""
    decision = decide(
        [_grant(role=ROLE_READER)], task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_EXTEND, moment=_NOW
    )
    assert decision.role is None


def test_an_outsider_is_denied_for_absence_not_for_role() -> None:
    decision = decide([_grant()], task_id=_TASK, actor_id="outsider", capability=CAPABILITY_READ, moment=_NOW)
    assert not decision.allowed
    assert decision.reason == "no active participant grant"
    assert decision.resolver_version is None


def test_a_decision_is_recorded_for_denials_too() -> None:
    """The audit record exists either way. A log of successful reads answers
    "who saw this" and not "who tried", and the second is the question asked
    after an incident."""
    decision = decide([], task_id=_TASK, actor_id="outsider", capability=CAPABILITY_READ, moment=_NOW)
    assert decision.task_id == _TASK
    assert decision.actor_id == "outsider"
    assert decision.capability == CAPABILITY_READ
    assert decision.decided_at == _NOW
    assert decision.allowed is False


def test_an_unknown_capability_is_a_programming_error_not_a_denial() -> None:
    """Answering "denied" would let a typo at a call site read as policy."""
    with pytest.raises(ValueError, match="unknown capability"):
        decide([_grant()], task_id=_TASK, actor_id="agent-2", capability="delete", moment=_NOW)


def test_require_raises_without_naming_the_task_or_its_participants() -> None:
    with pytest.raises(AudienceDenied) as caught:
        require([_grant()], task_id=_TASK, actor_id="outsider", capability=CAPABILITY_READ, moment=_NOW)
    message = str(caught.value)
    assert str(_TASK) not in message
    assert "agent-1" not in message
    assert "agent-2" not in message


def test_require_returns_the_decision_when_allowed() -> None:
    decision = require([_grant()], task_id=_TASK, actor_id="agent-2", capability=CAPABILITY_READ, moment=_NOW)
    assert decision.allowed
    assert decision.role == ROLE_CONTRIBUTOR
    assert decision.resolver_version == RESOLVER_EXPLICIT


# --- entitlement-derived grants -----------------------------------------------


def test_fresh_evidence_materializes_a_grant_that_expires_with_it() -> None:
    """One window, not two. A separate expiry policy would drift from the
    evidence, and the longer of the two would win by accident."""
    evidence = _evidence(resolved_at=_NOW, max_age=_HOUR)
    grant = materialize_entitlement_grant(
        task_id=_TASK,
        actor_id="agent-3",
        role=ROLE_READER,
        granted_by="entitlement-service",
        evidence=evidence,
        moment=_NOW,
    )
    assert grant.expires_at == _NOW + _HOUR
    assert grant.resolver_version == ENTITLEMENT_RESOLVER
    assert grant.is_active_at(_NOW)
    assert not grant.is_active_at(_NOW + _HOUR)


def test_stale_evidence_is_refused_rather_than_materialized_expired() -> None:
    """A grant minted from an aged-out answer would already have lapsed. Storing
    it is worse than refusing: the record shows an audience that never applied."""
    stale = _evidence(resolved_at=_NOW - (2 * _HOUR), max_age=_HOUR)
    with pytest.raises(AudienceDenied, match="stale"):
        materialize_entitlement_grant(
            task_id=_TASK,
            actor_id="agent-3",
            role=ROLE_READER,
            granted_by="entitlement-service",
            evidence=stale,
            moment=_NOW,
        )


def test_evidence_from_an_unknown_resolver_is_refused() -> None:
    with pytest.raises(AudienceDenied, match="does not recognize"):
        materialize_entitlement_grant(
            task_id=_TASK,
            actor_id="agent-3",
            role=ROLE_READER,
            granted_by="entitlement-service",
            evidence=_evidence(resolver="entitlement/v99"),
            moment=_NOW,
        )


def test_an_explicit_resolver_cannot_be_passed_off_as_entitlement_evidence() -> None:
    """The two resolvers are not interchangeable: an explicit grant is durable
    authority somebody conferred, and laundering it through this path would give
    it an expiry it was never meant to have."""
    with pytest.raises(AudienceDenied, match="does not recognize"):
        materialize_entitlement_grant(
            task_id=_TASK,
            actor_id="agent-3",
            role=ROLE_READER,
            granted_by="entitlement-service",
            evidence=_evidence(resolver=RESOLVER_EXPLICIT),
            moment=_NOW,
        )


@pytest.mark.parametrize("field", ["resolver_version", "source", "authority"])
def test_evidence_missing_a_required_field_is_refused(field: str) -> None:
    with pytest.raises(AudienceDenied, match=f"needs a {field}"):
        dataclasses.replace(_evidence(), **{field: "  "})


def test_evidence_with_a_non_positive_window_never_applied() -> None:
    with pytest.raises(AudienceDenied, match="non-positive max_age"):
        dataclasses.replace(_evidence(), max_age=datetime.timedelta(0))


def test_evidence_needs_an_aware_resolved_at() -> None:
    with pytest.raises(AudienceDenied, match="timezone-aware"):
        dataclasses.replace(_evidence(), resolved_at=datetime.datetime(2026, 8, 8, 12, 0))


def test_an_unknown_role_is_refused_before_a_grant_exists() -> None:
    with pytest.raises(AudienceDenied, match="unknown participant role"):
        materialize_entitlement_grant(
            task_id=_TASK,
            actor_id="agent-3",
            role="superuser",  # type: ignore[arg-type]
            granted_by="entitlement-service",
            evidence=_evidence(),
            moment=_NOW,
        )


def test_evidence_freshness_is_exclusive_at_its_far_edge() -> None:
    evidence = _evidence(resolved_at=_NOW, max_age=_HOUR)
    assert evidence.is_fresh_at(_NOW)
    assert evidence.is_fresh_at(_NOW + _HOUR - datetime.timedelta(seconds=1))
    assert not evidence.is_fresh_at(_NOW + _HOUR)


def test_evidence_from_the_future_is_not_fresh_yet() -> None:
    """A resolver clock ahead of ours is a reason to distrust the answer, not to
    accept it early."""
    assert not _evidence(resolved_at=_NOW + _HOUR).is_fresh_at(_NOW)


# --- revocation ---------------------------------------------------------------


def test_revoking_ends_the_grant_without_deleting_it() -> None:
    """The row survives so an audit of a past read can still find the grant
    that authorized it."""
    grant = _grant(granted_at=_NOW - _HOUR)
    revoked = revoked_at(grant, moment=_NOW)
    assert revoked.expires_at == _NOW
    assert revoked.granted_at == grant.granted_at
    assert revoked.actor_id == grant.actor_id
    assert not revoked.is_active_at(_NOW)
    assert revoked.is_active_at(_NOW - datetime.timedelta(minutes=30))


def test_revoking_twice_does_not_extend_the_first_revocation() -> None:
    grant = _grant(granted_at=_NOW - (2 * _HOUR), expires_at=_NOW - _HOUR)
    assert revoked_at(grant, moment=_NOW).expires_at == _NOW - _HOUR


def test_revoking_before_the_grant_began_is_refused() -> None:
    """That window never opened, and storing it would misreport the audience the
    task actually had."""
    with pytest.raises(AudienceDenied, match="at or before"):
        revoked_at(_grant(granted_at=_NOW), moment=_NOW - _HOUR)


def test_a_revoked_grant_stops_resolving_immediately() -> None:
    """The end-to-end property revocation exists for."""
    grant = _grant(granted_at=_NOW - _HOUR)
    assert active_grant_for([grant], actor_id="agent-2", moment=_NOW) is not None
    assert active_grant_for([revoked_at(grant, moment=_NOW)], actor_id="agent-2", moment=_NOW) is None
