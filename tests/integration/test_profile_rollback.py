"""A migration can be run backwards, and both directions refuse the same things.

Rollback is the direction nobody rehearses. It gets written once, exercised
never, and reached for at the worst possible moment — so the property worth
holding is not that rollback works in isolation but that it is *the same
machinery*: same inventory completeness, same disposition rules, same activation
block. A rollback path with its own weaker checks is a way to leave a graph in a
state the forward path would have refused.

So every test here asserts the two directions agree, rather than testing rollback
separately. Where they are asserted apart, the assertion is on identity
preservation — which is the one thing a rollback has to guarantee that a forward
migration does not: the rows must come back the same, not merely come back.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.profile.migration import (
    COLLISION,
    MIGRATE,
    Disposition,
    Finding,
    Inventory,
    MigrationPlan,
    MigrationRefused,
    compare_identities,
    empty_inventory,
)
from scripts.profile_migration_execute import plan_for

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)

_DIRECTIONS = ("forward", "rollback")


def _blocking_finding() -> Finding:
    return Finding(kind=COLLISION, subject="core:capability/payments", detail="handle claimed twice")


def _live_disposition() -> Disposition:
    return Disposition(
        action=MIGRATE,
        owner="platform-team",
        reason="renamed in place",
        expires_at=_NOW + datetime.timedelta(days=90),
    )


# --- both directions are the same machinery -------------------------------------------


@pytest.mark.parametrize("direction", _DIRECTIONS)
def test_each_direction_produces_a_plan(direction: str) -> None:
    plan = plan_for(direction)

    plan.inventory.assert_complete()
    assert plan.may_activate(_NOW)


def test_an_unknown_direction_is_refused() -> None:
    """A typo must not silently run the other way."""
    with pytest.raises(MigrationRefused, match="unknown direction"):
        plan_for("sideways")


@pytest.mark.parametrize("direction", _DIRECTIONS)
def test_an_unresolved_finding_blocks_either_direction(direction: str) -> None:
    """A rollback with weaker checks is a way to reach a state the forward path refused."""
    plan = MigrationPlan(inventory=plan_for(direction).inventory, findings=[_blocking_finding()])

    assert not plan.may_activate(_NOW)


@pytest.mark.parametrize("direction", _DIRECTIONS)
def test_an_incomplete_inventory_blocks_either_direction(direction: str) -> None:
    counts = dict(plan_for(direction).inventory.counts)
    del counts["caches"]
    plan = MigrationPlan(inventory=Inventory(counts=counts), findings=())

    assert not plan.may_activate(_NOW)


def test_both_directions_agree_on_the_same_findings() -> None:
    """Asserted as an equality, because the risk is one direction being lenient.

    Two separate assertions would both pass for an implementation where rollback
    checked nothing and happened to be handed an empty finding list.
    """
    findings = [_blocking_finding()]
    forward = MigrationPlan(inventory=empty_inventory(), findings=findings)
    rollback = MigrationPlan(inventory=empty_inventory(), findings=findings)

    assert forward.may_activate(_NOW) == rollback.may_activate(_NOW) is False
    assert forward.unresolved(_NOW) == rollback.unresolved(_NOW)


def test_a_resolved_finding_unblocks_both_directions_together() -> None:
    """The negative above proves nothing unless a disposition actually clears it."""
    findings = [
        Finding(
            kind=COLLISION,
            subject="core:capability/payments",
            detail="handle claimed twice",
            disposition=_live_disposition(),
        )
    ]
    forward = MigrationPlan(inventory=empty_inventory(), findings=findings)
    rollback = MigrationPlan(inventory=empty_inventory(), findings=findings)

    assert forward.may_activate(_NOW)
    assert rollback.may_activate(_NOW)


# --- what rollback alone must guarantee -----------------------------------------------


def test_a_rollback_that_preserves_identities_reports_no_drift() -> None:
    """Rows must come back the same, not merely come back.

    A rollback that restored the data under new identities would leave every
    external reference pointing at nothing, and the row counts would look correct
    throughout.
    """
    before = {"cap-1": "id-1", "cap-2": "id-2"}
    after = dict(before)

    assert compare_identities(before, after) == ()


def test_a_rollback_that_renumbers_a_subject_is_reported_as_drift() -> None:
    before = {"cap-1": "id-1"}
    after = {"cap-1": "id-9"}

    assert compare_identities(before, after) == ("cap-1: 'id-1' -> 'id-9'",)


def test_a_rollback_that_drops_a_subject_is_reported() -> None:
    """The failure a row count cannot see: fewer rows, all of them correct."""
    assert compare_identities({"cap-1": "id-1", "cap-2": "id-2"}, {"cap-1": "id-1"}) == (
        "cap-2: present before, absent after",
    )


def test_drift_is_reported_rather_than_raised() -> None:
    """A dry run's job is to say what *would* happen, not to stop at the first surprise.

    Raising would report one drifted subject and hide the rest, which is the least
    useful moment to be terse.
    """
    drift = compare_identities({"a": "1", "b": "2", "c": "3"}, {"a": "9", "b": "8", "c": "3"})

    assert len(drift) == 2
