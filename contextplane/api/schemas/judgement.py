"""Wire shapes for judged criteria and the human who may overrule the judge.

E24-T5 and E24-T7, on ADR 0026. Projections, not translations. Which criteria
exist, what a verdict may say, and what a reviewer may answer all live in
`context/evaluation/judgement.py` and `extraction/judge_prompt.py`.

**No bare scores.** Every judged criterion carries the judge's reasoning and the
spans it relied on. A verdict a reviewer can only accept or reject is not
reviewable, so `reasoning` and `evidence` are required fields rather than
optional detail — and `evidence` is required on a passing criterion too, because
evidence supplied only on failures teaches a reader that passes are not
checkable.

**`confidence_is_calibrated` is a field, not an inference.** Until a bin fit
exists for the pinned tuple, a judge's self-reported confidence predicts nothing,
and a surface must say so rather than render a number that looks checked. Sending
the flag rather than letting each client decide is what stops one of them getting
it wrong — which is the defect ADR 0019 refused when it declined to infer
`actor_kind`, in the place least able to absorb a confident guess.

**Disagreement is a state on the wire.** `is_disputed` is present so a surface
does not have to re-derive it from the review list, and so the Judgement surface
can filter on it without reading every review.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from contextplane.context.evaluation.judge_calibration import CalibrationState
from contextplane.context.evaluation.judgement import (
    REVIEW_VERDICTS,
    Judgement,
    PanelOutcome,
    ReviewerVerdict,
)
from contextplane.extraction.judge_prompt import JUDGE_VERDICTS, JUDGED_CRITERIA


class RunJudgementRequest(BaseModel):
    """Grade one simulated answer on both model-judged criteria."""

    panel_position: int = Field(
        default=0,
        ge=0,
        description=(
            "Which judge this is. Zero is the single judge an interactive simulation gets; a panel "
            "of three occupies 0, 1 and 2. Re-judging at the same position replaces that verdict, "
            "because a re-run is not a second opinion."
        ),
    )


class RecordJudgementReviewRequest(BaseModel):
    """One reviewer's word on one judged criterion."""

    verdict: str = Field(
        description=(
            f"One of {list(REVIEW_VERDICTS)}. `unsure` is information about the reviewer rather "
            "than a third verdict on the answer, and calibration excludes it — counting it either "
            "way would bias the fit."
        )
    )
    note: str | None = Field(
        default=None,
        description=(
            "Why. Required for anything other than `confirmed`: a disagreement with no reason is "
            "one the next reader has to reach again from scratch."
        ),
    )
    observed_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="The reviewer's own confidence. Follows the claim-adjudication contract.",
    )


class ReviewResponse(BaseModel):
    """One recorded review of one judged criterion."""

    verdict: str
    note: str | None
    observed_confidence: float | None
    reviewed_by: uuid.UUID
    reviewed_at: datetime.datetime

    @classmethod
    def of(cls, review: ReviewerVerdict) -> ReviewResponse:
        """Project one review onto the wire."""
        return cls(
            note=review.note,
            observed_confidence=review.observed_confidence,
            reviewed_at=review.reviewed_at,
            reviewed_by=review.reviewed_by,
            verdict=review.verdict,
        )


class JudgementResponse(BaseModel):
    """One criterion, judged, with the trace that makes the verdict arguable."""

    judgement_id: uuid.UUID
    simulation_id: uuid.UUID
    criterion: str = Field(description=f"One of {list(JUDGED_CRITERIA)}.")
    verdict: str = Field(description=f"One of {list(JUDGE_VERDICTS)}. No partial credit.")
    reasoning: str = Field(
        description=(
            "The judge's step-by-step reasoning, produced before its verdict. Required: a score "
            "with no trace is one a reviewer can only accept or reject."
        )
    )
    evidence: list[str] = Field(
        description="The spans the judge relied on, quoted. Present on a pass as well as a fail."
    )
    confidence: float = Field(
        description=(
            "The judge's own number, on its own scale. Read `confidence_is_calibrated` before "
            "showing it as anything: until a fit exists for this judge's pinned tuple, it predicts "
            "nothing."
        )
    )
    confidence_is_calibrated: bool = Field(
        description=(
            "Whether a bin fit exists for this result's pinned tuple. False means the verdict is "
            "unproven and must be rendered as such — a confident-looking score on the screen whose "
            "job is calibrating trust is a confident label on a guess."
        )
    )
    judge_provider_id: str
    judge_model_id: str
    rubric_version: str
    prompt_template_hash: str = Field(
        description=(
            "A digest over what the judge was asked — template, rubric, tool name and output "
            "schema. Pinned separately from the model id because position, verbosity and format "
            "bias are properties of the template rather than of the model."
        )
    )
    panel_position: int
    is_disputed: bool = Field(
        description="Whether any reviewer overruled the judge. A visible state, never a silent overwrite."
    )
    created_at: datetime.datetime
    reviews: list[ReviewResponse]

    @classmethod
    def of(cls, judgement: Judgement, *, is_calibrated: bool = False) -> JudgementResponse:
        """Project one judged criterion onto the wire.

        `is_calibrated` defaults to false, and the default is the safe direction:
        a caller that forgot to pass it renders an unproven verdict as unproven
        rather than rendering an unchecked one as checked.
        """
        return cls(
            confidence=judgement.confidence,
            confidence_is_calibrated=is_calibrated,
            created_at=judgement.created_at,
            criterion=judgement.criterion,
            evidence=list(judgement.evidence),
            is_disputed=judgement.is_disputed,
            judge_model_id=judgement.judge_model_id,
            judge_provider_id=judgement.judge_provider_id,
            judgement_id=judgement.judgement_id,
            panel_position=judgement.panel_position,
            prompt_template_hash=judgement.prompt_template_hash,
            reasoning=judgement.reasoning,
            reviews=[ReviewResponse.of(review) for review in judgement.reviews],
            rubric_version=judgement.rubric_version,
            simulation_id=judgement.simulation_id,
            verdict=judgement.verdict,
        )


class JudgementListResponse(BaseModel):
    """Every judged criterion of one simulation."""

    items: list[JudgementResponse]


class PanelOutcomeResponse(BaseModel):
    """One criterion as a panel decided it, with the split left visible."""

    criterion: str
    votes: dict[str, int] = Field(
        description="How the panel split, by verdict. A 2-1 is reported as 2-1, never averaged away."
    )
    majority: str | None = Field(
        default=None,
        description=(
            "The verdict more than half the panel reached, or absent on a tie. Absent rather than "
            "tie-broken: a panel that split evenly has not decided, and inventing a winner would "
            "report agreement nobody reached."
        ),
    )
    is_split: bool = Field(
        description=(
            "Whether the panel disagreed at all. The interesting output — a criterion three judges "
            "disagree about is the one most worth a human's time."
        )
    )
    judgements: list[JudgementResponse]

    @classmethod
    def of(
        cls, outcome: PanelOutcome, *, calibrated: frozenset[tuple[str, str, str]] = frozenset()
    ) -> PanelOutcomeResponse:
        """Project one panel outcome onto the wire."""
        return cls(
            criterion=outcome.criterion,
            is_split=outcome.is_split,
            judgements=[
                JudgementResponse.of(entry, is_calibrated=entry.pinned_tuple in calibrated)
                for entry in outcome.judgements
            ],
            majority=outcome.majority,
            votes=outcome.votes,
        )


class PanelResponse(BaseModel):
    """A panel run, one entry per criterion."""

    items: list[PanelOutcomeResponse]


class CalibrationStateResponse(BaseModel):
    """One judge tuple's calibration state, with the numbers behind it.

    The counts travel beside `is_calibrated` because *"why are we still
    unproven"* is the question an evaluator asks next, and an answer of silence
    sends them looking in the wrong place.
    """

    judge_model_id: str
    rubric_version: str
    prompt_template_hash: str
    is_calibrated: bool
    status: str
    version: str
    n_adjudicated: int
    measured_error: float
    fitted_at: datetime.datetime | None = None

    @classmethod
    def of(cls, state: CalibrationState) -> CalibrationStateResponse:
        """Project one calibration state onto the wire."""
        return cls(
            fitted_at=state.fitted_at,
            is_calibrated=state.is_calibrated,
            judge_model_id=state.pinned.judge_model_id,
            measured_error=state.measured_error,
            n_adjudicated=state.n_adjudicated,
            prompt_template_hash=state.pinned.prompt_template_hash,
            rubric_version=state.pinned.rubric_version,
            status=state.status,
            version=state.version,
        )


class CalibrationStateListResponse(BaseModel):
    """Every judge tuple that has ever been fitted, at its most recent attempt."""

    items: list[CalibrationStateResponse]


__all__ = [
    "CalibrationStateListResponse",
    "CalibrationStateResponse",
    "JudgementListResponse",
    "PanelOutcomeResponse",
    "PanelResponse",
    "JudgementResponse",
    "RecordJudgementReviewRequest",
    "ReviewResponse",
    "RunJudgementRequest",
]
