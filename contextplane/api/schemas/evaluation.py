"""Wire shapes for evaluation runs.

Projections, not translations: every field below exists on the object it came
from, with the same name and meaning. The rules — which verdicts are legal, what
makes two runs comparable, whether an errored prompt stays in a run — live in
`context/evaluation/runs.py`, because the MCP surface answers the same questions
and restating a rule here would create a second place it can drift.

**The prompt request is carried as an object rather than typed here.** Its shape
is `PromptRequestV1`'s, which the service validates on write, and mirroring it
in this module would be a third definition of one request — the wire model, the
context model, and a copy. A conformance test holds the first two equal; a third
would have nothing holding it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field

from contextplane.context.evaluation.expectations import PRESET_NAMES, PRESETS, Preset
from contextplane.context.evaluation.runs import (
    VERDICTS,
    Prompt,
    PromptSet,
    Run,
    RunItem,
    Verdict,
)


class CreatePromptSetRequest(BaseModel):
    """A named set, created empty."""

    name: str = Field(max_length=200, min_length=1)
    description: str | None = Field(
        default=None,
        description="What this set is for. Read by somebody who did not write it.",
    )


class AddPromptRequest(BaseModel):
    """One context request appended to a set."""

    request: dict[str, Any] = Field(
        description=(
            "A context-resolve request: the same fields `POST /v1/context/resolve` takes. "
            "Validated on write, so a prompt that could never resolve is refused here rather "
            "than failing on every run afterwards."
        )
    )
    intent_note: str | None = Field(
        default=None,
        description="What this prompt is checking. The question a later reader arrives with.",
    )
    expectations: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this prompt asserts about a run, in a form a program can check. Declared here "
            "and not after a run: a scenario whose required facts were written after seeing what "
            "the system returned would be satisfied by whatever the system returned. Omit it "
            "entirely to assert nothing, which is a real state and is rendered as one — an object "
            "of permissive thresholds would be checks that always pass. Seed it from a persona "
            f"preset ({list(PRESET_NAMES)}) and amend from there."
        ),
    )


class RecordVerdictRequest(BaseModel):
    """One reviewer's judgement of one prompt's resolution."""

    verdict: str = Field(description=f"One of {list(VERDICTS)}.")
    note: str | None = Field(
        default=None,
        description=(
            "Why. Required for anything other than `right`: a judgement with no reason is one "
            "the next reader has to reach again from scratch."
        ),
    )


class PromptSetResponse(BaseModel):
    """A set, and how many prompts it holds."""

    set_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime.datetime
    retired_at: datetime.datetime | None
    prompt_count: int

    @classmethod
    def of(cls, entry: PromptSet) -> PromptSetResponse:
        """Project one set onto the wire."""
        return cls(
            created_at=entry.created_at,
            description=entry.description,
            name=entry.name,
            prompt_count=entry.prompt_count,
            retired_at=entry.retired_at,
            set_id=entry.set_id,
        )


class PromptSetListResponse(BaseModel):
    """A page of sets."""

    items: list[PromptSetResponse]


class PromptResponse(BaseModel):
    """One prompt, as stored."""

    prompt_id: uuid.UUID
    position: int
    request: dict[str, Any]
    intent_note: str | None
    expectations: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this prompt asserts, as validated and stored. Absent when it asserts nothing. "
            "The `preset` field inside it, when present, records the persona somebody started "
            "from and is never the source of truth — a preset edited afterwards must not change "
            "what a past prompt asserted."
        ),
    )

    @classmethod
    def of(cls, prompt: Prompt) -> PromptResponse:
        """Project one prompt onto the wire."""
        return cls(
            expectations=prompt.expectations,
            intent_note=prompt.intent_note,
            position=prompt.position,
            prompt_id=prompt.prompt_id,
            request=prompt.request,
        )


class PresetResponse(BaseModel):
    """One seeded persona, and the rubric versions it parameterizes."""

    name: str
    description: str
    envelope_rubric_version: str = Field(
        description=(
            "The deterministic scorer this preset's thresholds are written against. Carried "
            "because a threshold on a criterion that has been redefined is a number describing "
            "something else."
        )
    )
    judge_rubric_version: str
    expectations: dict[str, Any]

    @classmethod
    def of(cls, entry: Preset) -> PresetResponse:
        """Project one persona onto the wire."""
        return cls(
            description=entry.description,
            envelope_rubric_version=entry.envelope_rubric_version,
            expectations=entry.expectations.stored(),
            judge_rubric_version=entry.judge_rubric_version,
            name=entry.name,
        )


class PresetListResponse(BaseModel):
    """The seeded personas, over the same five criteria.

    Each is a parameterization and never an extension: a persona that could add a
    criterion would be a rubric, and two rubrics produce two numbers nobody can
    put side by side.
    """

    items: list[PresetResponse]

    @classmethod
    def seeded(cls) -> PresetListResponse:
        """Every persona this deployment ships."""
        return cls(items=[PresetResponse.of(PRESETS[name]) for name in PRESET_NAMES])


class VerdictResponse(BaseModel):
    """One recorded judgement."""

    verdict: str
    note: str | None
    recorded_by: uuid.UUID
    recorded_at: datetime.datetime

    @classmethod
    def of(cls, entry: Verdict) -> VerdictResponse:
        """Project one verdict onto the wire."""
        return cls(
            note=entry.note,
            recorded_at=entry.recorded_at,
            recorded_by=entry.recorded_by,
            verdict=entry.verdict,
        )


class RunItemResponse(BaseModel):
    """One prompt's resolution within a run."""

    item_id: uuid.UUID
    prompt_id: uuid.UUID
    position: int
    receipt_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The resolution's receipt, or absent alongside `failure`. An errored prompt stays "
            "in the run: dropping it is how a number improves without anything improving."
        ),
    )
    envelope_state: str | None = Field(
        default=None, description="`complete`, `degraded` or `blocked`, or absent alongside `failure`."
    )
    failure: str | None = None
    duration_ms: int
    verdicts: list[VerdictResponse]

    @classmethod
    def of(cls, item: RunItem) -> RunItemResponse:
        """Project one run item onto the wire."""
        return cls(
            duration_ms=item.duration_ms,
            envelope_state=item.envelope_state,
            failure=item.failure,
            item_id=item.item_id,
            position=item.position,
            prompt_id=item.prompt_id,
            receipt_id=item.receipt_id,
            verdicts=[VerdictResponse.of(entry) for entry in item.verdicts],
        )


class RunResponse(BaseModel):
    """One run, with its items when they were asked for."""

    run_id: uuid.UUID
    set_id: uuid.UUID
    resolver_fingerprint: str = Field(
        description=(
            "The deployment that produced this run, as a digest of the facts a resolution "
            "depends on that no request can express. Two runs with different fingerprints are "
            "not comparable — a difference between them is evidence the configuration changed, "
            "not evidence about retrieval."
        )
    )
    prompt_count: int
    started_at: datetime.datetime
    finished_at: datetime.datetime | None = Field(default=None, description="Absent while the run is in flight.")
    items: list[RunItemResponse]

    @classmethod
    def of(cls, run: Run) -> RunResponse:
        """Project one run onto the wire."""
        return cls(
            finished_at=run.finished_at,
            items=[RunItemResponse.of(item) for item in run.items],
            prompt_count=run.prompt_count,
            resolver_fingerprint=run.resolver_fingerprint,
            run_id=run.run_id,
            set_id=run.set_id,
            started_at=run.started_at,
        )


class RunListResponse(BaseModel):
    """A page of run headers."""

    items: list[RunResponse]


__all__ = [
    "AddPromptRequest",
    "CreatePromptSetRequest",
    "PromptResponse",
    "PromptSetListResponse",
    "PromptSetResponse",
    "RecordVerdictRequest",
    "RunItemResponse",
    "RunListResponse",
    "RunResponse",
    "VerdictResponse",
]
