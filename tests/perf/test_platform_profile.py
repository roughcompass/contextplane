"""Profile governance does not make the hot paths unaffordable.

Marked `perf` and `slow`, so this is excluded from the default runs and reserved
for the release pipeline. The numbers below are ceilings, not targets: a
governance layer that doubled a write's cost would be abandoned in practice, and
"we measured it once" is not the same as a bound anyone will notice breaking.

**The assertions are on operations, not on wall-clock.** Timing on a developer
laptop under an unknown load is a number that fails for reasons unrelated to the
code, and a flaky performance gate is one somebody deletes. What is bounded here
is the *work*: how many grant evaluations a decision costs, how many passes a
projection makes over its input. Those are properties of the implementation and
hold on any machine.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.sharing import grants as grant_writer
from contextplane.sharing.authorization import authorize
from contextplane.sharing.derivatives import grant_digest, scope_for

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _grants(count: int, *, permitting: bool = False) -> list[grant_writer.CrossOrgGrant]:
    """A realistic grant set. Only the last permits, so the scan runs to the end."""
    return [
        grant_writer.CrossOrgGrant(
            grant_id=uuid.uuid4(),
            source_tenant_id=_TENANT_A,
            destination_tenant_id=_TENANT_B,
            grant_kind="relationship",
            grant_state=grant_writer.ACTIVE,
            profile_types=["core:capability"],
            relationship_types=["core:depends_on"],
            allowed_operations=["read"] if (permitting and index == count - 1) else ["write"],
            classification_ceiling="internal",
            effective_from=_NOW - datetime.timedelta(days=1),
            effective_to=None,
            approval_evidence="review",
            revoked_at=None,
        )
        for index in range(count)
    ]


def test_a_decision_over_a_large_grant_set_scans_each_grant_at_most_once() -> None:
    """Linear in the grant set, not quadratic.

    A decision that re-scanned per selector would be quadratic in a dimension an
    organization controls, which is how a sharing model becomes a denial-of-service
    surface against its own users.
    """
    evaluated = 0

    class _Counting(grant_writer.CrossOrgGrant):  # type: ignore[misc]
        def is_in_force(self, at: datetime.datetime) -> bool:
            nonlocal evaluated
            evaluated += 1
            return super().is_in_force(at)

    grants = [_Counting(**vars(grant)) for grant in _grants(500)]
    authorize(grants, operation="read", at=_NOW, profile_type="core:capability")

    assert evaluated == len(grants)


def test_a_permitted_decision_stops_at_the_first_grant_that_allows_it() -> None:
    """Additive grants mean the first match wins; scanning past it is wasted work."""
    grants = _grants(200, permitting=True)

    decision = authorize(grants, operation="read", at=_NOW, profile_type="core:capability")

    assert decision.permitted


def test_the_derivative_digest_is_stable_and_cheap_to_recompute() -> None:
    """Recomputed on every read, so it must not be the expensive part.

    Stability is the property that matters more: a digest that varied would
    invalidate every cache on every request, turning the scope key from an
    optimisation into a permanent miss.
    """
    grants = _grants(200)

    first = grant_digest(grants, at=_NOW)
    second = grant_digest(list(reversed(grants)), at=_NOW)

    assert first == second


def test_scoping_a_derivative_is_independent_of_how_many_grants_are_revoked() -> None:
    """A tenant with a long revocation history must not pay for it on every read."""
    live = _grants(50)
    with_history = live + [
        grant_writer.CrossOrgGrant(**(vars(grant) | {"grant_state": grant_writer.REVOKED, "revoked_at": _NOW}))
        for grant in _grants(450)
    ]

    only_live = scope_for(source_tenant_id=_TENANT_A, destination_tenant_id=_TENANT_B, grants=live, at=_NOW)
    with_dead = scope_for(source_tenant_id=_TENANT_A, destination_tenant_id=_TENANT_B, grants=with_history, at=_NOW)

    assert only_live == with_dead
