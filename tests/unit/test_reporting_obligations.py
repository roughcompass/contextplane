"""Every refusal happens before a transaction is opened.

These are the checks that do not need a database, and proving they run *first*
is the point of testing them here rather than only in the integration tier: a
validation that fires after the INSERT is a validation the database was already
asked to do, and the caller gets a constraint violation instead of a sentence.

The session factory used here raises if anybody calls it. A test that passed
while opening a connection would not be testing the ordering at all.
"""

from __future__ import annotations

import datetime
import inspect
import uuid

import pytest

from contextplane.exceptions import ValidationError
from contextplane.service.governance.obligations import (
    CLASSIFIABLE,
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    MATERIALITY_UNCLASSIFIED,
    MATERIALITY_VALUES,
    ReportingObligationService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 22, 9, 0, tzinfo=datetime.UTC)


def _refusing_factory():
    def factory():  # pragma: no cover - the assertion is that this is never called
        raise AssertionError("a refusal must happen before a transaction is opened")

    return factory


def _service() -> ReportingObligationService:
    return ReportingObligationService(_refusing_factory(), clock=FakeClock(_NOW))


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["admin"])


@pytest.mark.asyncio
async def test_a_summary_too_short_to_identify_the_obligation_is_refused() -> None:
    with pytest.raises(ValidationError, match="nobody can identify"):
        await _service().nominate(_ctx(), summary="oops")


@pytest.mark.asyncio
async def test_a_summary_of_whitespace_is_the_same_as_none() -> None:
    with pytest.raises(ValidationError):
        await _service().nominate(_ctx(), summary="              ")


@pytest.mark.asyncio
async def test_classifying_back_to_unclassified_is_refused() -> None:
    """ "I have decided it is undecided" is not a decision.

    Allowing it would let an actor clear a classification while leaving their
    name recorded as having made one.
    """
    with pytest.raises(ValidationError, match="not a conclusion"):
        await _service().classify(
            _ctx(),
            obligation_id=uuid.uuid4(),
            materiality=MATERIALITY_UNCLASSIFIED,
            note="a note long enough to satisfy the bound",
        )


@pytest.mark.asyncio
async def test_an_unknown_materiality_is_refused_by_the_service_not_the_database() -> None:
    """The caller gets a sentence naming the legal values, not a CHECK violation."""
    with pytest.raises(ValidationError, match="materiality must be one of"):
        await _service().classify(
            _ctx(),
            obligation_id=uuid.uuid4(),
            materiality="severe",
            note="a note long enough to satisfy the bound",
        )


@pytest.mark.asyncio
async def test_a_one_word_rationale_is_refused() -> None:
    with pytest.raises(ValidationError, match="same as none"):
        await _service().classify(
            _ctx(),
            obligation_id=uuid.uuid4(),
            materiality=MATERIALITY_MATERIAL,
            note="yes",
        )


def test_unclassified_is_a_value_and_not_a_null() -> None:
    """The state most obligations are in most of the time is named.

    Modelled as `materiality IS NULL`, "nobody has decided" and "the column was
    added later" would be the same value, and the delay gauge would have nothing
    to count.
    """
    assert MATERIALITY_UNCLASSIFIED in MATERIALITY_VALUES
    assert MATERIALITY_UNCLASSIFIED not in CLASSIFIABLE


def test_the_two_conclusions_a_decision_may_reach_are_the_other_two() -> None:
    assert CLASSIFIABLE == {MATERIALITY_NOT_MATERIAL, MATERIALITY_MATERIAL}
    assert set(MATERIALITY_VALUES) == CLASSIFIABLE | {MATERIALITY_UNCLASSIFIED}


def test_nothing_here_classifies_anything_automatically() -> None:
    """The service exposes no path from `unclassified` that does not name an actor.

    Automatic classification needs a ratified threshold set that is external to
    this system. A placeholder threshold presented as a compliance feature is
    worse than an absent one, and this asserts the absence is deliberate rather
    than pending.
    """
    public = {name for name in vars(ReportingObligationService) if not name.startswith("_")}
    assert public == {
        "nominate",
        "classify",
        "get",
        "unclassified_backlog",
        "observe_backlog",
        # E4-T5d. Neither touches `materiality`: they record what the obligation
        # is *about*, which is a different question from how material it is.
        "cite_incident",
        "incidents_for",
    }

    # The property the roster above is a proxy for, asserted directly so a
    # method added to the set cannot smuggle a classification in with it.
    writers = {name for name in public if "materiality" in inspect.getsource(getattr(ReportingObligationService, name))}
    assert writers == {"classify", "nominate", "unclassified_backlog", "observe_backlog"}, (
        f"{sorted(writers - {'classify', 'nominate', 'unclassified_backlog', 'observe_backlog'})} "
        "names materiality. Only an explicit decision moves an obligation out of `unclassified`."
    )


def test_the_per_tenant_read_publishes_no_gauge() -> None:
    """The gauges are deployment-wide and carry no tenant label.

    A per-tenant read that set them would make the series report whichever
    tenant asked most recently -- which is worse than not publishing at all,
    because the number would look live and be wrong. `observe_backlog` is the
    only writer, and it counts everybody.
    """
    source = inspect.getsource(ReportingObligationService.unclassified_backlog)
    assert "UNCLASSIFIED_BACKLOG" not in source
    assert "OLDEST_UNCLASSIFIED_AGE_SECONDS" not in source
    assert "set(" not in source
