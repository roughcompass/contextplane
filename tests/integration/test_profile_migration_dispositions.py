"""Every finding needs a decision, and an expired decision is not one.

The failure this file guards is quiet. A migration that inventories most of a
graph, or that carries a grandfather nobody revisited, produces exactly the same
clean report as one that did the work — and the difference only surfaces later, as
rows that were never looked at or an exemption that silently became permanent.

So the tests here are about absence and expiry rather than about the happy path.
A category missing from the inventory must be an error, not a zero. A disposition
missing any of its four parts must not construct. An expired grandfather must
block activation exactly as an undecided finding does — because to an activation
decision they are the same thing, and treating the expired one as softer is how a
temporary exemption becomes permanent.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.profile.migration import (
    COLLISION,
    DISPOSITION_ACTIONS,
    GRANDFATHER,
    INVENTORY_CATEGORIES,
    MIGRATE,
    Disposition,
    Finding,
    IncompleteInventory,
    Inventory,
    MigrationPlan,
    MigrationRefused,
    compare_identities,
    empty_inventory,
)

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _disposition(**overrides: object) -> Disposition:
    fields: dict[str, object] = {
        "action": MIGRATE,
        "owner": "platform-team",
        "reason": "renamed in place",
        "expires_at": _NOW + datetime.timedelta(days=90),
    }
    fields.update(overrides)
    return Disposition(**fields)  # type: ignore[arg-type]


def _finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "kind": COLLISION,
        "subject": "core:capability/payments",
        "detail": "two rows claim this handle",
        "disposition": _disposition(),
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


# --- the inventory is closed ----------------------------------------------------------


def test_a_complete_inventory_accounts_for_every_category() -> None:
    empty_inventory().assert_complete()

    assert set(empty_inventory().counts) == set(INVENTORY_CATEGORIES)


@pytest.mark.parametrize("omitted", INVENTORY_CATEGORIES)
def test_an_inventory_missing_any_category_is_refused(omitted: str) -> None:
    """A category nobody counted is indistinguishable from one that was empty.

    Parametrized over the closed set, so a category added later is covered the
    moment it is defined rather than when somebody remembers to test it.
    """
    counts = dict(empty_inventory().counts)
    del counts[omitted]

    with pytest.raises(IncompleteInventory, match=omitted):
        Inventory(counts=counts).assert_complete()


def test_an_inventory_reporting_an_unknown_category_is_refused() -> None:
    """A count nobody asked for means the reporter and the migration disagree."""
    counts = dict(empty_inventory().counts) | {"vibes": 3}

    with pytest.raises(IncompleteInventory, match="vibes"):
        Inventory(counts=counts).assert_complete()


def test_a_zero_count_is_a_real_answer_not_a_gap() -> None:
    """Otherwise a deployment with nothing to migrate could never produce a valid report."""
    inventory = empty_inventory()

    inventory.assert_complete()
    assert inventory.total == 0


# --- a disposition is four things -----------------------------------------------------


@pytest.mark.parametrize("action", sorted(DISPOSITION_ACTIONS))
def test_each_declared_action_is_accepted(action: str) -> None:
    """The refusals below would all pass for a constructor that rejected everything."""
    assert _disposition(action=action).action == action


def test_an_undeclared_action_is_refused() -> None:
    with pytest.raises(MigrationRefused, match="unknown disposition"):
        _disposition(action="deal_with_it_later")


@pytest.mark.parametrize("field", ["owner", "reason"])
def test_a_disposition_without_an_owner_or_reason_is_refused(field: str) -> None:
    """An owner with no reason is a change nobody can review later."""
    with pytest.raises(MigrationRefused, match=field):
        _disposition(**{field: "   "})


def test_an_unknown_finding_kind_is_refused() -> None:
    with pytest.raises(MigrationRefused, match="unknown finding kind"):
        _finding(kind="something_felt_off")


# --- expiry is enforced ---------------------------------------------------------------


def test_an_expired_disposition_leaves_its_finding_unresolved() -> None:
    """A grandfather that never expires is a remove nobody admitted to."""
    finding = _finding(disposition=_disposition(action=GRANDFATHER, expires_at=_NOW - datetime.timedelta(days=1)))

    assert not finding.is_resolved(_NOW)


def test_an_expired_finding_blocks_activation_exactly_as_an_undecided_one_does() -> None:
    """To an activation decision they are the same thing.

    Asserted as an equality between the two cases rather than separately, because
    the risk is that one is treated as softer than the other.
    """
    expired = MigrationPlan(
        inventory=empty_inventory(),
        findings=[_finding(disposition=_disposition(expires_at=_NOW - datetime.timedelta(days=1)))],
    )
    undecided = MigrationPlan(inventory=empty_inventory(), findings=[_finding(disposition=None)])

    assert expired.may_activate(_NOW) == undecided.may_activate(_NOW) is False
    assert len(expired.unresolved(_NOW)) == len(undecided.unresolved(_NOW)) == 1


def test_a_disposition_expiring_exactly_now_has_expired() -> None:
    """The boundary resolves against the migration, which is the safe direction."""
    assert _disposition(expires_at=_NOW).is_expired(_NOW)


def test_a_live_disposition_resolves_its_finding() -> None:
    assert _finding().is_resolved(_NOW)


# --- activation is blocked by default -------------------------------------------------


def test_a_plan_with_no_findings_may_activate() -> None:
    """Without this, a plan that blocked everything would pass every test above."""
    plan = MigrationPlan(inventory=empty_inventory(), findings=())

    plan.assert_may_activate(_NOW)
    assert plan.may_activate(_NOW)


def test_activation_is_blocked_while_anything_is_unresolved() -> None:
    plan = MigrationPlan(inventory=empty_inventory(), findings=[_finding(disposition=None)])

    with pytest.raises(MigrationRefused, match="block activation"):
        plan.assert_may_activate(_NOW)


def test_activation_is_blocked_by_an_incomplete_inventory_even_with_no_findings() -> None:
    """Findings from an inventory that skipped a category are not reassuring."""
    counts = dict(empty_inventory().counts)
    del counts["closures"]
    plan = MigrationPlan(inventory=Inventory(counts=counts), findings=())

    with pytest.raises(IncompleteInventory):
        plan.assert_may_activate(_NOW)


def test_the_plan_offers_no_way_to_activate_anyway() -> None:
    """A caller able to say "go anyway" will, at the moment the reasoning is weakest.

    Checked on the signature, so adding a parameter is a change to this test.
    """
    import inspect

    parameters = set(inspect.signature(MigrationPlan.assert_may_activate).parameters)

    assert not parameters & {"force", "override", "ignore_findings", "anyway"}


# --- warnings before the cliff --------------------------------------------------------


def test_a_disposition_expiring_soon_is_warned_about() -> None:
    """A grandfather lapsing next week and one lapsing next year need different attention."""
    plan = MigrationPlan(
        inventory=empty_inventory(),
        findings=[_finding(disposition=_disposition(action=GRANDFATHER, expires_at=_NOW + datetime.timedelta(days=7)))],
    )

    warnings = plan.warnings(_NOW)

    assert len(warnings) == 1
    assert "grandfathered until" in warnings[0]


def test_a_distant_expiry_is_not_warned_about() -> None:
    """A warning on everything is a warning on nothing."""
    plan = MigrationPlan(inventory=empty_inventory(), findings=[_finding()])

    assert plan.warnings(_NOW) == ()


# --- identities must survive ----------------------------------------------------------


def test_a_drifted_identity_is_reported() -> None:
    """A subject whose identity moved is one every existing reference now misses."""
    drift = compare_identities({"a": "id-1"}, {"a": "id-2"})

    assert drift == ("a: 'id-1' -> 'id-2'",)


def test_a_lost_subject_is_reported() -> None:
    assert compare_identities({"a": "id-1"}, {}) == ("a: present before, absent after",)


def test_unchanged_identities_report_nothing() -> None:
    assert compare_identities({"a": "id-1"}, {"a": "id-1"}) == ()
