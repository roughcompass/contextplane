"""Wire shapes for agent simulation.

E24-T3, on ADR 0025. Projections, not translations: every field below exists on
the object it came from, with the same name and meaning. Which principals may be
simulated, which provider pairs are refused, and what a citation means all live in
`context/evaluation/simulation.py`, because a rule restated here would be a second
place it can drift and the MCP surface would eventually enforce a different one.

**The resolution and the generation are two objects on the wire, as they are two
rows in storage.** `receipt_id` points at the resolver's record; nothing here
copies the envelope. That is ADR 0025's decision 2 carried through to the
response body, so a client can read one without the other and neither becomes the
place the truth lives.

**`served_item_count` travels beside the token counts, deliberately.** A token
figure with no cardinality beside it says nothing about what to do, and `limit`
is the only lever the product actually offers when a run comes back too large.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, Field

from contextplane.context.evaluation.simulation import (
    MAX_PROMPT_CHARS,
    CitedItem,
    SimulatedAssertion,
    Simulation,
    TokenReport,
)


class RunSimulationRequest(BaseModel):
    """Resolve as a declared agent, then answer."""

    simulated_actor_id: uuid.UUID = Field(
        description=(
            "The principal whose behaviour is being modelled. Must be declared `agent` per ADR "
            "0019 — an agent is declared, never inferred, so an undeclared principal is refused "
            "rather than defaulted. This names whose behaviour is simulated; it does not grant "
            "that principal's authority, and the resolution still runs on the caller's own."
        )
    )
    prompt: str = Field(
        max_length=MAX_PROMPT_CHARS,
        min_length=1,
        description="What to ask. Answered from the resolved envelope and nothing else.",
    )
    request: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The context-resolve request this simulation resolves under: the same fields "
            "`POST /v1/context/resolve` takes, minus `query`, which the prompt supplies. "
            "Omit it entirely for a bare resolution."
        ),
    )
    run_item_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The evaluation run item this simulation answers, when it is part of a run. Absent "
            "for an interactive one."
        ),
    )


class AssertionCitationResponse(BaseModel):
    """One receipt item id an assertion rested on.

    **Named for what it cites, not just `CitationResponse`, and the reason is a
    collision that renamed somebody else's schema.** `api/routers/memory.py`
    already publishes a `CitationResponse`; a second class under that name makes
    FastAPI qualify *both* by module path, so the pre-existing schema silently
    became `contextplane__api__routers__memory__CitationResponse` in the
    contract — a breaking rename of a published name, caused by an unrelated
    addition. The generated-client build in `contextplane-ui` is what caught it.
    """

    receipt_item_id: str
    was_served: bool = Field(
        description=(
            "Whether the cited id was actually in the envelope. False is a finding rather than a "
            "bug: a model citing something that was not served is exactly what a groundedness "
            "check is for, so the citation is stored as declared."
        )
    )

    @classmethod
    def of(cls, citation: CitedItem) -> AssertionCitationResponse:
        """Project one citation onto the wire."""
        return cls(receipt_item_id=citation.receipt_item_id, was_served=citation.was_served)


class AssertionResponse(BaseModel):
    """One claim the answer made, and what it rested on."""

    position: int
    text: str
    citations: list[AssertionCitationResponse] = Field(
        description=(
            "Empty when the assertion rested on nothing served. That is a real state, not a "
            "missing one: it is either a fact the graph does not hold or a groundedness failure, "
            "and the improvement surface offers both readings rather than choosing."
        )
    )

    @classmethod
    def of(cls, assertion: SimulatedAssertion) -> AssertionResponse:
        """Project one assertion onto the wire."""
        return cls(
            citations=[AssertionCitationResponse.of(citation) for citation in assertion.citations],
            position=assertion.position,
            text=assertion.text,
        )


class UsageResponse(BaseModel):
    """What the call cost, as reported or as explicitly unknown."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    source: str = Field(
        description=(
            "`provider_reported`, `estimated` or `unknown`. Absent counts mean nobody could "
            "report — never that the call was free, which is the reading that makes a spend "
            "figure lie."
        )
    )
    served_item_count: int = Field(
        description=(
            "How many items the envelope carried. Paired with the token figure because `limit` "
            "is the only lever the product offers when a run comes back too large."
        )
    )

    @classmethod
    def of(cls, usage: TokenReport) -> UsageResponse:
        """Project one usage record onto the wire."""
        return cls(
            cached_prompt_tokens=usage.cached_prompt_tokens,
            completion_tokens=usage.completion_tokens,
            prompt_tokens=usage.prompt_tokens,
            served_item_count=usage.served_item_count,
            source=usage.source,
        )


class SimulationResponse(BaseModel):
    """One resolution and the answer generated from it, kept apart."""

    simulation_id: uuid.UUID
    receipt_id: uuid.UUID = Field(
        description=(
            "The resolution's own receipt, referenced rather than embedded. Read it to see what "
            "was served; this body says what was made of it."
        )
    )
    simulated_actor_id: uuid.UUID
    prompt: str
    answer: str
    provider_id: str
    model_id: str
    instruction_disposition: str = Field(
        description=(
            "`not_declared`, `declared_unknown` or `declared_known`. Three states, never two: an "
            "agent that declared no instructions and one that declared an empty set are different "
            "experiments, and a score conflating them would be scoring both under one number."
        )
    )
    envelope_state: str = Field(description="`complete`, `degraded` or `blocked`, as the resolution reported it.")
    usage: UsageResponse
    assertions: list[AssertionResponse]
    uncited_served_ids: list[str] = Field(
        description=(
            "Served items no assertion cited. An observation, not a diagnosis: it means the scope "
            "was too wide, or the agent ignored them, and the improvement surface names both."
        )
    )
    duration_ms: int | None = None
    created_at: datetime.datetime
    run_item_id: uuid.UUID | None = None

    @classmethod
    def of(cls, simulation: Simulation) -> SimulationResponse:
        """Project one simulation onto the wire."""
        return cls(
            answer=simulation.answer,
            assertions=[AssertionResponse.of(entry) for entry in simulation.assertions],
            created_at=simulation.created_at,
            duration_ms=simulation.duration_ms,
            envelope_state=simulation.envelope_state,
            instruction_disposition=simulation.instruction_disposition,
            model_id=simulation.model_id,
            prompt=simulation.prompt,
            provider_id=simulation.provider_id,
            receipt_id=simulation.receipt_id,
            run_item_id=simulation.run_item_id,
            simulated_actor_id=simulation.simulated_actor_id,
            simulation_id=simulation.simulation_id,
            uncited_served_ids=list(simulation.uncited_served_ids),
            usage=UsageResponse.of(simulation.usage),
        )


class SimulationAvailabilityResponse(BaseModel):
    """Whether this deployment can simulate, and under what.

    A read a surface calls before offering the action, so an operator sees a
    switched-off feature rather than a button that always fails. It carries no
    credential and no endpoint — only which selectors are in force, which is what
    somebody needs to know to fix a refusal.
    """

    available: bool
    simulation_provider: str
    simulation_model: str = Field(description="Empty when the selected adapter's own default is in force.")
    judge_provider: str = Field(
        description=(
            "`noop` when no judge is configured. Never the same family as `simulation_provider` — "
            "the service refuses that pair rather than warning about it."
        )
    )
    judge_model: str


__all__ = [
    "AssertionCitationResponse",
    "AssertionResponse",
    "RunSimulationRequest",
    "SimulationAvailabilityResponse",
    "SimulationResponse",
    "UsageResponse",
]
