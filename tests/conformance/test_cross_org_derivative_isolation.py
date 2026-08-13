"""Hidden data contributes no rank, count, closure, fallback or timing class.

A derivative is where cross-organization data leaks without ever being returned.
A record nobody may read still changes a rank, still increments a count, still
shortens a traversal — and each of those is observable by a caller who never sees
the record itself. Testing that the record is absent from the *results* is
therefore not enough; what has to hold is that it made no difference to anything
derived.

The property is enforced by the scope key rather than by a filter applied
afterwards, and that distinction is what these tests are about. A filter runs
after the derivative was built from everything, so the rank it returns already
reflects rows the caller cannot see. A scope key means the derivative for this
caller was never built from them, and a mismatch is unservable rather than
filterable.

**Revocation is immediate because the key changes, not because a purge finished.**
That is asserted directly: the scope after a revocation differs from the scope
before it, so the old artefact is unreachable at once and the rebuild is a
space-reclamation concern rather than a correctness one.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.sharing import grants as grant_writer
from contextplane.sharing.derivatives import (
    DERIVATION_VERSION,
    DerivativeScope,
    StaleDerivative,
    assert_fresh,
    grant_digest,
    is_fresh,
    scope_for,
)

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)
_SOURCE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_DESTINATION = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _grant(**overrides: object) -> grant_writer.CrossOrgGrant:
    fields: dict[str, object] = {
        "grant_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "source_tenant_id": _SOURCE,
        "destination_tenant_id": _DESTINATION,
        "grant_kind": "relationship",
        "grant_state": grant_writer.ACTIVE,
        "profile_types": ["core:capability"],
        "relationship_types": ["core:depends_on"],
        "allowed_operations": ["read"],
        "classification_ceiling": "internal",
        "effective_from": _NOW - datetime.timedelta(days=1),
        "effective_to": None,
        "approval_evidence": "review-1",
        "revoked_at": None,
    }
    fields.update(overrides)
    return grant_writer.CrossOrgGrant(**fields)  # type: ignore[arg-type]


def _scope(*grants: grant_writer.CrossOrgGrant, at: datetime.datetime = _NOW) -> DerivativeScope:
    return scope_for(source_tenant_id=_SOURCE, destination_tenant_id=_DESTINATION, grants=list(grants), at=at)


# --- the scope is a function of the grants, not of the tenants ------------------------


def test_a_derivative_is_scoped_to_both_tenants_and_the_grant_set() -> None:
    """Scoped to a tenant alone, a cache would serve one caller's grants to another."""
    scope = _scope(_grant())

    assert scope.source_tenant_id == _SOURCE
    assert scope.destination_tenant_id == _DESTINATION
    assert scope.grant_digest
    assert scope.derivation_version == DERIVATION_VERSION


def test_the_key_does_not_collide_across_a_reversed_pair() -> None:
    """`A shares to B` and `B shares to A` are different grants and different slots."""
    forward = DerivativeScope(_SOURCE, _DESTINATION, "digest")
    reverse = DerivativeScope(_DESTINATION, _SOURCE, "digest")

    assert forward.cache_key != reverse.cache_key


def test_two_callers_under_the_same_grants_share_one_scope() -> None:
    """Otherwise every caller rebuilds, and the cache stops being one."""
    assert _scope(_grant()) == _scope(_grant())


def test_the_digest_is_independent_of_the_order_grants_were_returned_in() -> None:
    """A digest that depended on query order would invalidate at random."""
    first = _grant(grant_id=uuid.UUID("44444444-4444-4444-4444-444444444444"))
    second = _grant(grant_id=uuid.UUID("55555555-5555-5555-5555-555555555555"))

    assert grant_digest([first, second], at=_NOW) == grant_digest([second, first], at=_NOW)


# --- a narrowing changes the scope, so nothing built before it survives ----------------


def test_narrowing_the_operations_changes_the_scope() -> None:
    """The grant keeps its id; a digest over ids alone would let the old artefact survive."""
    before = _scope(_grant(allowed_operations=["read", "write"]))
    after = _scope(_grant(allowed_operations=["read"]))

    assert before != after
    assert not is_fresh(before, after)


def test_narrowing_the_profile_types_changes_the_scope() -> None:
    before = _scope(_grant(profile_types=["core:capability", "core:dataset"]))
    after = _scope(_grant(profile_types=["core:capability"]))

    assert before != after


def test_lowering_the_classification_ceiling_changes_the_scope() -> None:
    before = _scope(_grant(classification_ceiling="restricted"))
    after = _scope(_grant(classification_ceiling="internal"))

    assert before != after


def test_adding_a_grant_changes_the_scope() -> None:
    """A widening must invalidate too: the old artefact is missing newly-shared rows."""
    one = _scope(_grant())
    two = _scope(_grant(), _grant(grant_id=uuid.UUID("66666666-6666-6666-6666-666666666666")))

    assert one != two


# --- revocation is immediate ----------------------------------------------------------


def test_revocation_changes_the_scope_at_once() -> None:
    """Unreachable the moment the grant is revoked, not when a purge completes."""
    before = _scope(_grant())
    after = _scope(_grant(grant_state=grant_writer.REVOKED, revoked_at=_NOW))

    assert before != after


def test_a_revoked_grant_contributes_nothing_to_the_digest() -> None:
    """The scope after revoking the only grant equals the scope with no grants at all.

    This is the strong form: not merely "different", but *identical to having
    nothing shared*, so no residue of the revoked grant can influence what is
    built.
    """
    revoked = _scope(_grant(grant_state=grant_writer.REVOKED, revoked_at=_NOW))
    none_at_all = _scope()

    assert revoked == none_at_all


def test_a_grant_outside_its_window_contributes_nothing() -> None:
    expired = _scope(_grant(effective_to=_NOW - datetime.timedelta(hours=1)))

    assert expired == _scope()


def test_a_proposed_grant_contributes_nothing() -> None:
    """A proposal shapes no derivative; otherwise proposing would leak by ranking."""
    proposed = _scope(_grant(grant_state=grant_writer.PROPOSED))

    assert proposed == _scope()


# --- stale reads fail closed ----------------------------------------------------------


def test_a_stale_derivative_is_refused_rather_than_served() -> None:
    """Serving it hands back a result built under permissions somebody withdrew."""
    built_under = _scope(_grant())
    now_in_force = _scope(_grant(grant_state=grant_writer.REVOKED, revoked_at=_NOW))

    with pytest.raises(StaleDerivative):
        assert_fresh(built_under, now_in_force)


def test_a_stale_read_is_not_silently_recomputed() -> None:
    """Recomputing inline puts the revocation's cost on whichever request noticed.

    Asserted on the refusal's own words rather than by timing, because the
    property is a design commitment: the rebuild is scheduled, and the read does
    not wait for it or do it.
    """
    with pytest.raises(StaleDerivative, match="not recomputed inline"):
        assert_fresh(_scope(_grant()), _scope())


def test_a_fresh_derivative_is_served() -> None:
    """The refusals above would all pass for a function that refused everything."""
    scope = _scope(_grant())

    assert_fresh(scope, scope)
    assert is_fresh(scope, scope)


# --- the derivation version -----------------------------------------------------------


def test_a_derivation_version_change_invalidates_every_scope() -> None:
    """Two artefacts built by different algorithms are not interchangeable.

    Without the version in the key, a deploy that changed how a closure is
    computed would serve the new shape out of the old slot.
    """
    current = DerivativeScope(_SOURCE, _DESTINATION, "digest")
    older = DerivativeScope(_SOURCE, _DESTINATION, "digest", derivation_version="derivative-scope-0")

    assert current != older
    assert current.cache_key != older.cache_key


def test_the_empty_grant_set_has_a_scope_of_its_own() -> None:
    """ "Nothing is shared" is a real scope a derivative can be built for.

    Giving it a digest rather than a sentinel means the key never has to be read
    as three-valued, and a caller with no grants gets a cache rather than a
    permanent miss.
    """
    empty = _scope()

    assert empty.grant_digest
    assert empty.cache_key
