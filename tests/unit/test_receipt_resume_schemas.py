"""The wire shapes for receipt lookup and resume.

Most of this file is about one field. `ResumeResponse.status` exists because
resumed, empty and ambiguous are three instructions a caller acts on
differently -- carry on, start fresh, disambiguate -- and the shape of the
response alone cannot tell them apart: all three can come back with an empty
checkpoint list. A caller that infers "nothing to resume" from emptiness will,
in the ambiguous case, start work that already exists somewhere.

So the tests below pin the mapping from service state to status, and pin that
both transports compute it through the same helper rather than each deciding
for itself.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from pydantic import ValidationError

from contextplane.api.routers.receipts import resume_status
from contextplane.api.schemas.receipts import (
    ExclusionResponse,
    ReceiptResponse,
    ResumeFeedbackResponse,
    ResumeRequestBody,
    ResumeResponse,
)
from contextplane.context.resume import ResumeRequest, ResumeState

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _state(**overrides: object) -> ResumeState:
    base: dict[str, object] = {
        "intent_id": None,
        "head_checkpoint_id": None,
        "head_sequence": None,
        "head_summary": None,
        "checkpoints": (),
        "receipts": (),
        "references": (),
        "open_questions": (),
        "next_action": None,
        "truncated": (),
        "ambiguous_intent_ids": (),
    }
    base.update(overrides)
    return ResumeState(**base)  # type: ignore[arg-type]


# --- The three answers --------------------------------------------------------


def test_a_resume_with_a_head_reports_resumed() -> None:
    task = uuid.uuid4()
    state = _state(intent_id=task, head_checkpoint_id=uuid.uuid4(), head_sequence=3, head_summary="did the thing")

    assert resume_status(state) == "resumed"


def test_a_resume_that_found_nothing_reports_empty() -> None:
    """Distinct from ambiguous, and that distinction is the point: this one
    means "start fresh", and acting on it is correct."""
    assert resume_status(_state()) == "empty"


def test_references_naming_more_than_one_task_report_ambiguous() -> None:
    """The case that would otherwise read as `empty`.

    Two tasks cite the same pull request; a resume cannot choose between them,
    and must not silently answer "nothing here" -- that is the answer that
    sends an agent off to redo work that exists.
    """
    both = (uuid.uuid4(), uuid.uuid4())
    state = _state(ambiguous_intent_ids=both)

    assert resume_status(state) == "ambiguous"
    assert state.is_ambiguous()


def test_ambiguous_and_empty_are_mutually_exclusive() -> None:
    """An ambiguous resume has no head and no checkpoints, so it would answer
    to a naive emptiness test. `is_empty` excludes ambiguity at the source
    rather than leaving the two predicates to be checked in the right order,
    which means no caller can be told both "start fresh" and "disambiguate".
    """
    ambiguous = _state(ambiguous_intent_ids=(uuid.uuid4(), uuid.uuid4()))
    nothing = _state()

    assert ambiguous.is_ambiguous() and not ambiguous.is_empty()
    assert nothing.is_empty() and not nothing.is_ambiguous()
    assert resume_status(ambiguous) == "ambiguous"
    assert resume_status(nothing) == "empty"


def test_the_ambiguous_task_ids_reach_the_wire() -> None:
    """Reporting "ambiguous" without saying between what leaves the caller with
    no next move."""
    both = (uuid.uuid4(), uuid.uuid4())
    state = _state(ambiguous_intent_ids=both)

    body = ResumeResponse(
        status=resume_status(state),
        intent_id=None,
        head_checkpoint_id=None,
        head_sequence=None,
        head_summary=None,
        checkpoints=[],
        receipts=[],
        references=[],
        open_questions=[],
        next_action=None,
        feedback=[],
        learning=[],
        truncated=[],
        ambiguous_intent_ids=list(state.ambiguous_intent_ids),
    )

    assert body.status == "ambiguous"
    assert set(body.ambiguous_intent_ids) == set(both)


def test_the_ordinary_response_carries_no_ambiguity() -> None:
    body = ResumeResponse(
        status="resumed",
        intent_id=uuid.uuid4(),
        head_checkpoint_id=uuid.uuid4(),
        head_sequence=1,
        head_summary="s",
        checkpoints=[],
        receipts=[],
        references=[],
        open_questions=["what now"],
        next_action="ship it",
        feedback=[],
        learning=[],
        truncated=["checkpoints"],
    )

    assert body.ambiguous_intent_ids == []


# --- The request --------------------------------------------------------------


def test_a_resume_request_needs_at_least_one_reference() -> None:
    """Resuming from nothing would mean resuming everything the tenant has
    ever done, which is not a resume."""
    with pytest.raises(ValidationError):
        ResumeRequestBody(references=[])


def test_a_reference_must_be_a_four_tuple() -> None:
    with pytest.raises(ValidationError):
        ResumeRequestBody(references=[("github", "acme/app", "commit")])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "field",
    ["checkpoint_bound", "receipt_bound", "reference_bound", "feedback_bound", "learning_bound"],
)
def test_a_bound_of_zero_is_refused_on_the_wire(field: str) -> None:
    """A bound of zero returns nothing while looking like a successful resume.
    The service refuses it; the wire has to refuse it too, or the 422 becomes a
    500 raised out of a frozen dataclass."""
    with pytest.raises(ValidationError):
        ResumeRequestBody(references=[("github", "acme/app", "commit", "abc")], **{field: 0})


def test_the_wire_bounds_match_the_service_bounds() -> None:
    """Both refuse zero. Written as one assertion so that relaxing either side
    alone fails here rather than in production."""
    with pytest.raises(ValueError, match="at least 1"):
        ResumeRequest(references=(("github", "acme/app", "commit", "abc"),), checkpoint_bound=0)


def test_omitted_bounds_are_left_to_the_service() -> None:
    """`None` on the wire means "unspecified", not "zero" -- the router drops
    them so the service's own defaults apply, which is where the real numbers
    are documented."""
    body = ResumeRequestBody(references=[("github", "acme/app", "commit", "abc")])

    assert body.checkpoint_bound is None
    assert body.receipt_bound is None
    assert body.reference_bound is None
    assert body.feedback_bound is None
    assert body.learning_bound is None


def test_resume_feedback_cannot_carry_reporter_or_note_data() -> None:
    """Resume needs the verdict, not who said it or their free text."""
    fields = set(ResumeFeedbackResponse.model_fields)

    assert {"consumed", "rating"} <= fields
    assert not {"note", "reporter_id", "reporter_type"} & fields


# --- Reads --------------------------------------------------------------------


def test_a_receipt_without_a_task_still_serialises() -> None:
    """Resolutions are not all made against a task, and the read surface must
    not require one."""
    body = ReceiptResponse(
        receipt_id=uuid.uuid4(),
        intent_id=None,
        state="resolved",
        cacheable=True,
        resolved_at=_NOW,
        requested_by="agent-a",
        request_digest=None,
    )

    assert body.intent_id is None
    assert body.request_digest is None


def test_an_exclusion_carries_its_reason() -> None:
    """An exclusion without a reason is a row that says something was withheld
    and refuses to say why, which is worse than not recording it."""
    body = ExclusionResponse(block="canonical", item_key="cap:alpha", reason="below trust floor")

    assert body.reason == "below trust floor"
