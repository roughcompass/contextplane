"""What resume refuses, and what it cannot be asked for.

The database-backed behaviour is proved next door against real Postgres. What
is here is the part that must hold before any query runs: the bounds a caller
cannot escape, and the transcript there is no way to request.

The transcript tests look trivial and are the most important ones in the file.
"Never return a transcript" is the kind of guarantee that survives review and
then dies to a helpful parameter added eighteen months later, so it is asserted
structurally -- against the shape of the request and the shape of the result,
not against one code path's behaviour.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from contextplane.context.resume import (
    DEFAULT_CHECKPOINT_BOUND,
    DEFAULT_FEEDBACK_BOUND,
    DEFAULT_LEARNING_BOUND,
    DEFAULT_RECEIPT_BOUND,
    DEFAULT_REFERENCE_BOUND,
    ResumeRequest,
    ResumeState,
)

_REF = ("github", "acme/app", "pull_request", "42")


# --- The request a caller can make --------------------------------------------


def test_a_resume_needs_something_to_resume_from() -> None:
    """Resuming everything a tenant has ever done is not a resume, and an empty
    reference list is the request that would ask for exactly that."""
    with pytest.raises(ValueError, match="at least one external reference"):
        ResumeRequest(references=())


@pytest.mark.parametrize(
    "field",
    ["checkpoint_bound", "receipt_bound", "reference_bound", "feedback_bound", "learning_bound"],
)
def test_a_bound_of_zero_is_refused(field: str) -> None:
    """A bound of zero returns nothing, which reads as "there was nothing" --
    the one answer resume must never give by accident."""
    with pytest.raises(ValueError, match="at least 1"):
        ResumeRequest(references=(_REF,), **{field: 0})


def test_every_arm_is_bounded_by_default() -> None:
    """A caller that passes no bounds still gets bounded. Unbounded-by-default
    would mean the task's age decides how much of a context window resume eats,
    and nobody would notice until a long-running task hit one."""
    request = ResumeRequest(references=(_REF,))

    assert request.checkpoint_bound == DEFAULT_CHECKPOINT_BOUND
    assert request.receipt_bound == DEFAULT_RECEIPT_BOUND
    assert request.reference_bound == DEFAULT_REFERENCE_BOUND
    assert request.feedback_bound == DEFAULT_FEEDBACK_BOUND
    assert request.learning_bound == DEFAULT_LEARNING_BOUND
    assert all(
        bound >= 1
        for bound in (
            request.checkpoint_bound,
            request.receipt_bound,
            request.reference_bound,
            request.feedback_bound,
            request.learning_bound,
        )
    )


def test_a_caller_may_ask_for_less_but_the_field_still_holds() -> None:
    request = ResumeRequest(references=(_REF,), checkpoint_bound=1)

    assert request.checkpoint_bound == 1


# --- The transcript that cannot be asked for ----------------------------------


def test_the_request_has_no_way_to_ask_for_a_transcript() -> None:
    """Structural, not behavioural. A flag that could be set to true is a flag
    somebody sets to true, so the guarantee is that no such field exists."""
    fields = {field.name for field in dataclasses.fields(ResumeRequest)}

    assert fields == {
        "references",
        "checkpoint_bound",
        "receipt_bound",
        "reference_bound",
        "feedback_bound",
        "learning_bound",
    }
    assert not any("transcript" in name or "messages" in name or "raw" in name for name in fields)


def test_the_result_has_nowhere_to_put_a_transcript() -> None:
    """The other half. A request that cannot ask and a result that could still
    carry one would leave the guarantee resting on every future caller."""
    fields = {field.name for field in dataclasses.fields(ResumeState)}

    assert not any("transcript" in name or "messages" in name or "body" in name for name in fields)


def test_the_result_carries_conclusions_rather_than_exchanges() -> None:
    """What resume returns instead: the head summary, the open questions and the
    next action -- what an agent concluded, not everything it said."""
    fields = {field.name for field in dataclasses.fields(ResumeState)}

    assert {"head_summary", "open_questions", "next_action"} <= fields


# --- Saying when the answer is partial ----------------------------------------


def test_truncation_is_named_per_arm() -> None:
    """A resume that quietly returned five of forty checkpoints would read as
    the whole story, and the caller would carry on from a middle it believed
    was the start."""
    state = ResumeState(
        intent_id=None,
        head_checkpoint_id=None,
        head_sequence=None,
        head_summary=None,
        checkpoints=(),
        receipts=(),
        references=(),
        open_questions=(),
        next_action=None,
        truncated=("checkpoints",),
    )

    assert state.truncated == ("checkpoints",)


def test_nothing_found_is_distinguishable_from_a_task_with_no_history() -> None:
    """ "Start fresh" and "this task exists and is empty" are different
    instructions, and a caller that cannot tell them apart will guess wrong."""
    nothing = ResumeState(
        intent_id=None,
        head_checkpoint_id=None,
        head_sequence=None,
        head_summary=None,
        checkpoints=(),
        receipts=(),
        references=(),
        open_questions=(),
        next_action=None,
        truncated=(),
    )
    empty_task = dataclasses.replace(nothing, intent_id=uuid.uuid4())

    assert nothing.is_empty()
    assert not empty_task.is_empty()
