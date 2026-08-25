"""Wire shapes for the deterministic three.

E24-T4a. Projections, not translations. What the criteria mean, and when they
cannot be computed, live in `context/evaluation/scoring.py` and
`context/evaluation/envelope_judge.py`.

**`unassertable` and `score` are exclusive on the wire as they are in the
service.** A reader branches on which is present, and there is deliberately no
zero-filled score with a flag beside it: zeros render as three failed criteria
and ones as three passes nobody checked, and both are worse than the sentence
that says why there is no number.

**An unchecked dimension is on the wire.** A boundary check that could not run is
neither a pass nor a failure, and a surface that showed only violations would
render an absent check as a clean one — which is the shape of every defence that
turns out to have been unreachable.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from contextplane.context.evaluation.envelope_judge import VIOLATION_KINDS
from contextplane.context.evaluation.scoring import DeterministicScore


class BlockTallyResponse(BaseModel):
    """One block's contribution, so a total stays attributable."""

    block: str
    state: str
    served: int
    relevant: int
    required_found: int


class ViolationResponse(BaseModel):
    """One served item that should not have been served, and which rule it broke."""

    item_key: str
    block: str = Field(description="Which arm served it. 'Something leaked' without the arm is unactionable.")
    kind: str = Field(description=f"One of {list(VIOLATION_KINDS)}.")
    detail: str


class UncheckedResponse(BaseModel):
    """One boundary this item could not be checked against, and why.

    Not a violation and not a pass. A surface that reported neither would let an
    absent check read as a clean one.
    """

    item_key: str
    block: str
    dimension: str
    reason: str


class DeterministicScoreResponse(BaseModel):
    """The three criteria a program computes, or the reason it could not."""

    rubric_version: str
    prompt_id: uuid.UUID | None = Field(
        default=None, description="Which prompt's declared expectations were used, when any were."
    )
    unassertable: str | None = Field(
        default=None,
        description=(
            "Present instead of a score when nothing was declared in advance to check. A scenario "
            "whose required facts were written after seeing what the system returned would be "
            "satisfied by whatever the system returned."
        ),
    )
    recall: float | None = None
    precision: float | None = None
    required_total: int | None = None
    required_found: int | None = None
    served_total: int | None = None
    is_safe: bool | None = Field(
        default=None,
        description=(
            "Whether the resolution served nothing it should not have. One violation fails the "
            "case whatever the other numbers say."
        ),
    )
    violations: list[ViolationResponse] = []
    unchecked: list[UncheckedResponse] = []
    blocks: list[BlockTallyResponse] = []

    @classmethod
    def of(cls, result: DeterministicScore) -> DeterministicScoreResponse:
        """Project one deterministic score onto the wire."""
        if result.score is None:
            return cls(
                prompt_id=result.prompt_id,
                rubric_version=result.rubric_version,
                unassertable=result.unassertable,
            )
        score = result.score
        return cls(
            blocks=[
                BlockTallyResponse(
                    block=tally.block,
                    relevant=tally.relevant,
                    required_found=tally.required_found,
                    served=tally.served,
                    state=tally.state,
                )
                for tally in score.blocks
            ],
            is_safe=score.is_safe,
            precision=score.precision,
            prompt_id=result.prompt_id,
            recall=score.recall,
            required_found=score.required_found,
            required_total=score.required_total,
            rubric_version=result.rubric_version,
            served_total=score.served_total,
            unchecked=[
                UncheckedResponse(
                    block=entry.block, dimension=entry.dimension, item_key=entry.item_key, reason=entry.reason
                )
                for entry in score.unchecked
            ],
            violations=[
                ViolationResponse(block=entry.block, detail=entry.detail, item_key=entry.item_key, kind=entry.kind)
                for entry in score.violations
            ],
        )


__all__ = [
    "BlockTallyResponse",
    "DeterministicScoreResponse",
    "UncheckedResponse",
    "ViolationResponse",
]
