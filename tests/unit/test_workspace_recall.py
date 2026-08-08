"""What workspace recall returns, and how it labels it.

The authorization half needs a real database and lives in the integration
suite. What is testable in isolation is everything the arm decides *about*
rows it already has: how a checkpoint's classification is derived from the
references it cites, what trust metadata it carries, and how a bound is
translated into the truncation fact the assembler reads.

The classification cases are the ones worth reading twice. A checkpoint has no
classification column of its own, so the label is derived — and every way of
deriving it wrong publishes something.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

import pytest

from contextplane.workspaces.recall import (
    CLASSIFICATION_FLOOR,
    DEFAULT_LIMIT,
    MAX_RESULTS,
    WorkspaceRecall,
    _bounded,
    _item,
    _trust_for,
    classification_for,
)

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)


@dataclasses.dataclass
class _Row:
    """A checkpoint row, only the columns recall reads.

    A stand-in rather than the ORM class: constructing a mapped object outside a
    session drags in a registry this test has no use for, and every attribute
    below is one the module actually touches — so a field recall starts reading
    that is missing here fails loudly instead of silently defaulting.
    """

    checkpoint_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    task_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    sequence: int = 1
    goal: str = "ship the recall arm"
    decisions: list[Any] = dataclasses.field(default_factory=list)
    assumptions: list[Any] = dataclasses.field(default_factory=list)
    evidence: list[Any] = dataclasses.field(default_factory=list)
    completed_checks: list[Any] = dataclasses.field(default_factory=list)
    open_questions: list[Any] = dataclasses.field(default_factory=list)
    next_action: str | None = "keep going"
    author: str = "agent-member"
    recorded_at: datetime.datetime = _NOW
    digest: str = "deadbeef"


def _reference(classification: str) -> dict[str, Any]:
    return {
        "source_system": "github",
        "source_namespace": "acme/platform",
        "kind": "commit",
        "external_id": "abc123",
        "classification": classification,
    }


# --- classification derivation -------------------------------------------------


def test_a_checkpoint_citing_nothing_gets_the_floor() -> None:
    """Not `public`. Nobody classified this content, and defaulting to the least
    restrictive label would publish agent-authored task material by omission."""
    assert classification_for([]) == CLASSIFICATION_FLOOR
    assert CLASSIFICATION_FLOOR == "internal"


@pytest.mark.parametrize("label", ["internal", "confidential", "restricted"])
def test_a_single_reference_raises_the_label_to_its_own(label: str) -> None:
    assert classification_for([_reference(label)]) == label


def test_a_public_reference_does_not_lower_the_floor() -> None:
    """Citing something public does not make the checkpoint public: the
    checkpoint is its author's own words about the work, not the reference."""
    assert classification_for([_reference("public")]) == CLASSIFICATION_FLOOR


def test_the_most_restrictive_reference_wins() -> None:
    """Not first, not last. Order-dependence would make the label depend on the
    order the writer happened to cite things in."""
    mixed = [_reference("public"), _reference("restricted"), _reference("internal")]
    assert classification_for(mixed) == "restricted"
    assert classification_for(list(reversed(mixed))) == "restricted"


def test_an_unknown_label_is_treated_as_the_most_restrictive_thing_it_could_be() -> None:
    """Failing closed on a label this build cannot read. Guessing downward is
    the guess that discloses."""
    assert classification_for([_reference("super-secret")]) == "restricted"


def test_a_missing_label_is_also_treated_as_restricted() -> None:
    assert classification_for([{"source_system": "github"}]) == "restricted"


def test_a_non_mapping_entry_is_skipped_rather_than_crashing_the_arm() -> None:
    """Evidence is JSONB, so a malformed row is possible. One bad entry must not
    take out a whole recall — but it must not silently lower the label either,
    which is why the floor still applies."""
    assert classification_for(["not-a-reference", _reference("internal")]) == "internal"


# --- trust metadata ------------------------------------------------------------


def test_a_checkpoint_is_asserted_and_immutable() -> None:
    """`asserted`, not `observed`: the system has the agent's word for its own
    work and no independent observation of it. `immutable` because a checkpoint
    cannot be rewritten — which is also why the mutable head is not recalled."""
    trust = _trust_for(_Row())
    assert trust.trust == "asserted"
    assert trust.mutability == "immutable"
    assert trust.assertion_kind == "annotation"


def test_freshness_is_the_moment_the_checkpoint_was_recorded() -> None:
    trust = _trust_for(_Row(recorded_at=_NOW))
    assert trust.freshness == _NOW


def test_the_author_is_attribution_and_the_task_is_authority() -> None:
    """Two different questions: who wrote it, and what boundary vouches for it.
    Collapsing them would let a reader mistake an author for an authority."""
    row = _Row(author="agent-7")
    trust = _trust_for(row)
    assert trust.attribution == "agent-7"
    assert trust.authority == f"task:{row.task_id}"


def test_the_derived_classification_reaches_the_trust_metadata() -> None:
    trust = _trust_for(_Row(evidence=[_reference("confidential")]))
    assert trust.classification == "confidential"


# --- items ---------------------------------------------------------------------


def test_an_item_lands_in_the_workspace_block_keyed_by_checkpoint() -> None:
    row = _Row()
    item = _item(row)
    assert item.receipt_item_id.block == "workspace"
    assert item.receipt_item_id.item_key == str(row.checkpoint_id)
    assert item.trust is not None


def test_the_payload_keeps_the_structured_fields_apart() -> None:
    """Resume treats an open question and a completed check differently, so
    flattening them into prose would lose the distinction the writer made."""
    row = _Row(open_questions=["does the cap hold"], completed_checks=["migration applied"])
    payload = _item(row).payload
    assert payload["open_questions"] == ["does the cap hold"]
    assert payload["completed_checks"] == ["migration applied"]
    assert payload["digest"] == row.digest
    assert payload["task_id"] == str(row.task_id)


# --- bounds --------------------------------------------------------------------


def test_no_limit_means_the_default_not_unbounded() -> None:
    assert _bounded(None) == DEFAULT_LIMIT


def test_a_caller_cannot_ask_for_more_than_the_ceiling() -> None:
    """The arm's own ceiling, independent of the assembler's cap. One caller
    must not get to decide how much work every other request queues behind."""
    assert _bounded(MAX_RESULTS * 10) == MAX_RESULTS


def test_a_smaller_request_is_honoured() -> None:
    assert _bounded(5) == 5


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_limit_is_refused(bad: int) -> None:
    """Zero is not "no limit" and not "empty page" — it is a caller that has not
    decided, and answering either way would be picking for them."""
    with pytest.raises(ValueError, match="at least 1"):
        _bounded(bad)


# --- truncation and outcome mapping -------------------------------------------


@pytest.mark.asyncio
async def test_hitting_the_bound_is_reported_as_truncated() -> None:
    """The arm reads one more row than it will return, so landing exactly on the
    bound is distinguishable from being cut short by it."""
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    read = recall._cut(tuple(_Row() for _ in range(4)), 3)
    outcome = await recall._as_outcome(read, moment=_NOW)
    assert outcome.truncated is True
    assert len(outcome.items) == 3


@pytest.mark.asyncio
async def test_landing_exactly_on_the_bound_is_not_truncated() -> None:
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    read = recall._cut(tuple(_Row() for _ in range(3)), 3)
    outcome = await recall._as_outcome(read, moment=_NOW)
    assert outcome.truncated is False
    assert len(outcome.items) == 3


@pytest.mark.asyncio
async def test_the_arm_reports_freshness_rather_than_claiming_not_to_track_it() -> None:
    """`None` would mean "this arm does not track staleness", which is a
    different and untrue statement about a live read."""
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    outcome = await recall._as_outcome(recall._cut((_Row(),), 3), moment=_NOW)
    assert outcome.fresh_as_of == _NOW


@pytest.mark.asyncio
async def test_restricted_content_is_withheld_as_an_exclusion_not_dropped() -> None:
    """This is material inside the caller's own task, so saying it exists is not
    a disclosure — and "there was something you may not see" is the only answer
    that tells a reader to go and ask someone."""
    row = _Row(evidence=[_reference("restricted")])
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    outcome = await recall._as_outcome(recall._cut((row,), 3), moment=_NOW)
    assert outcome.items == ()
    assert [e.item_key for e in outcome.exclusions] == [str(row.checkpoint_id)]
    assert "classification" in outcome.exclusions[0].reason


@pytest.mark.asyncio
async def test_an_arm_reports_no_block_state_of_its_own() -> None:
    """Success, empty, degraded and failed are one decision made in the
    assembler. An arm that also decided would give the envelope two answers."""
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    outcome = await recall._as_outcome(recall._cut((), 3), moment=_NOW)
    assert outcome.items == ()
    assert outcome.degraded_reason is None
    assert not hasattr(outcome, "state")


@pytest.mark.asyncio
async def test_items_come_back_in_a_deterministic_order() -> None:
    """Ordered by receipt item id, so two identical requests produce identical
    envelopes regardless of how the rows arrived from the database."""
    rows = tuple(_Row() for _ in range(5))
    recall = WorkspaceRecall(session_factory=None)  # type: ignore[arg-type]
    first = await recall._as_outcome(recall._cut(rows, 5), moment=_NOW)
    second = await recall._as_outcome(recall._cut(tuple(reversed(rows)), 5), moment=_NOW)
    assert [i.receipt_item_id for i in first.items] == [i.receipt_item_id for i in second.items]
