"""The PII vocabularies the contract publishes are the ones the service enforces.

E10-T6. Two closed sets govern personal-data policy — the field types a scan
runs on, and the three policies a match resolves to — and until this task both
were published as bare `string`.

**Why that mattered enough to be its own task.** A client cannot offer a correct
picker against an open string, so the dashboard duplicated the vocabulary: nine
field-type literals written into a component. The alternative there was a
free-text box, which lets an operator save a policy that stores, lists, and
**silently governs nothing** — resolution matches the field type as an exact
string, so one wrong character produces a control that was never in force.

**What this file pins.** Publishing the vocabularies means writing them a second
time, as `Literal` members, because a type cannot be built from a runtime
frozenset. That is a duplication in the same shape as the one being removed —
so it is only safe while something holds the two in agreement. This is that
something.

`PROHIBITED_CLASSES` is the precedent: it is read off the shipped detectors
rather than restated, because a hand-written second list disagrees first *in the
direction that silently admits*. Where a second list is unavoidable, it gets a
gate.
"""

from __future__ import annotations

import typing

from contextplane.api.routers.admin_pii import (
    _VALID_FIELD_TYPES,
    _VALID_POLICIES,
    FieldTypeValue,
    PolicyValue,
)
from contextplane.context.admission import PILOT_FIELD_TYPES
from contextplane.security.pii_scanner import POLICY_VALUES


def test_the_published_field_types_are_the_pilot_set() -> None:
    """The `Literal` the contract carries and the set the scanner resolves
    against must name the same values.

    A field type in one and not the other is the whole defect: published but
    unenforced means a client offers a policy that governs nothing, and enforced
    but unpublished means a client cannot offer a policy that would have worked.
    """
    published = set(typing.get_args(FieldTypeValue))
    assert published == set(PILOT_FIELD_TYPES), (
        "the contract's field-type vocabulary and `PILOT_FIELD_TYPES` have diverged; "
        f"only in the contract: {sorted(published - set(PILOT_FIELD_TYPES))}, "
        f"only in the service: {sorted(set(PILOT_FIELD_TYPES) - published)}"
    )


def test_the_published_policies_are_the_scanners_own() -> None:
    """`_POLICY_SEVERITY` in the scanner is the authority, because it also
    carries the ordering the maximum is taken over."""
    assert set(typing.get_args(PolicyValue)) == set(POLICY_VALUES)


def test_the_router_validates_against_the_owning_modules_not_its_own_copies() -> None:
    """The regression this task exists for.

    `_VALID_POLICIES` used to be a hand-written frozenset in the router, beside
    the scanner's — and the scanner's is the one that decides. Two lists, and
    the one the route checked was not the one that governs.
    """
    assert _VALID_POLICIES == frozenset(POLICY_VALUES)
    assert _VALID_FIELD_TYPES == PILOT_FIELD_TYPES


def test_the_policy_order_is_least_severe_first() -> None:
    """A caller offering these as a choice should present them in the order that
    decides, since the scanner takes the maximum across matches."""
    assert POLICY_VALUES == ("advisory", "warn", "block")
