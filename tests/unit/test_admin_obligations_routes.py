"""The wire vocabulary and the service vocabulary do not drift apart.

`ClassifyObligationRequest.materiality` is a `Literal`, which cannot be
enumerated at runtime, so nothing stops it and the service's `CLASSIFIABLE` from
disagreeing. They disagree silently in the worst direction: a value the service
would accept but the wire refuses looks like a client bug, and a value the wire
accepts but the service refuses reaches a caller as a 400 naming a field they
were offered.
"""

from __future__ import annotations

import typing

from contextplane.api.routers.admin_obligations import (
    CLASSIFIABLE_ON_THE_WIRE,
    ClassifyObligationRequest,
    NominateObligationRequest,
)
from contextplane.service.governance import obligations
from contextplane.service.governance.obligations import CLASSIFIABLE, MATERIALITY_UNCLASSIFIED


def _literal_values() -> frozenset[str]:
    annotation = ClassifyObligationRequest.model_fields["materiality"].annotation
    return frozenset(typing.get_args(annotation))


def test_the_wire_offers_exactly_what_the_service_will_accept() -> None:
    assert _literal_values() == CLASSIFIABLE
    assert CLASSIFIABLE_ON_THE_WIRE == CLASSIFIABLE


def test_the_wire_does_not_offer_unclassified_as_a_conclusion() -> None:
    """A caller cannot clear a classification while leaving their name on it."""
    assert MATERIALITY_UNCLASSIFIED not in _literal_values()


def _bounds(model: type, field: str) -> tuple[int | None, int | None]:
    low = high = None
    for constraint in model.model_fields[field].metadata:
        low = getattr(constraint, "min_length", None) or low
        high = getattr(constraint, "max_length", None) or high
    return low, high


def test_the_wire_states_the_same_bounds_the_service_enforces() -> None:
    """A caller learns the limit from the schema rather than from a refusal.

    Both halves are deliberate and neither is redundant: the wire bound produces
    a 422 naming the field, and the service bound holds for any caller that does
    not go through this model. What would be a defect is the two disagreeing,
    which is what this asserts.
    """
    assert _bounds(NominateObligationRequest, "summary") == (obligations._MIN_SUMMARY, obligations._MAX_SUMMARY)
    assert _bounds(ClassifyObligationRequest, "note") == (obligations._MIN_NOTE, obligations._MAX_NOTE)
