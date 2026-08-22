"""Admin reads and instruction lifecycle for one agent principal.

E20-T9. The human-in-the-loop half of the retraining loop: the MCP surface lets
an agent read its own figures, and this lets an operator read *any* agent's and
change what one does next.

**The asymmetry is the point.** `get_my_accuracy` takes no actor id, so an agent
cannot ask about a colleague. These routes take one in the path, because that is
what an operator needs — and they are gated on the admin role, which is where a
per-actor read belongs now that no floor makes it unconstructible.

That gate is doing more work than it looks like. Before the per-actor aggregate
floor was removed, an actor-level figure could not be built at all; the
authorization here is what replaced that, and it is the whole of the protection
rather than its outer layer. The decision record for the floor removal says so
in its dissent, and this docstring repeats it because a reader of this file is
the one who most needs to know.

**Proposing, activating and rolling back are here and nowhere else.** They
change what an agent does next. There is deliberately no MCP tool for any of
them: an agent that could activate its own instruction would be authoring its
own behaviour change with no human involved, which is a different trust boundary
from adjudicating a claim.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.routers._admin_common import _admin_required
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory import agent_accuracy as accuracy_module
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/agents", tags=["admin: agents"])


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


class _Strict(BaseModel):
    """Closed models: a misspelled field is refused rather than dropped, and a
    response never grows a field nobody noticed changed the contract."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class AccuracyGroupOut(_Strict):
    """One row of an accuracy breakdown."""

    label: str
    n_correct: int
    n_incorrect: int
    n_undecidable: int
    n_decided: int = Field(description="The denominator `rate` is over: correct plus incorrect.")
    n_adjudicated: int = Field(description="Every verdict recorded, including the ones that decided nothing.")
    rate: float | None = Field(
        description=(
            "Correct as a fraction of decided, or null when nothing was decided. "
            "Null is not zero: the first means unknown, the second means wrong every time."
        )
    )


class AccuracyOut(_Strict):
    """One agent's accuracy over one window."""

    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    breakdown: str
    overall: AccuracyGroupOut
    groups: list[AccuracyGroupOut]


class AutonomyOut(_Strict):
    """How many of an agent's sessions ran without a human stepping in."""

    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    n_sessions: int
    n_intervened: int
    n_autonomous: int
    intervention_rate: float | None
    autonomy_rate: float | None = Field(
        description="Null when the agent ran no sessions — an unknown rate rather than a perfect one."
    )


class FailureExampleOut(_Strict):
    claim_id: uuid.UUID
    value: object
    note: str | None


class FailureGroupOut(_Strict):
    claim_category: str
    predicate: str
    incorrect_count: int = Field(description="How often this group appears among the failures.")
    total_count: int = Field(description="How often this group was judged at all.")
    rate: float = Field(
        description=(
            "Incorrect over judged. The figure to act on: a predicate used constantly and "
            "mostly got right will lead on `incorrect_count` by volume alone."
        )
    )
    examples: list[FailureExampleOut]


class FailurePatternsOut(_Strict):
    report_id: uuid.UUID
    author_actor_id: uuid.UUID
    window_start: datetime.datetime
    window_end: datetime.datetime
    n_adjudicated: int
    n_incorrect: int
    n_sessions: int
    n_intervention_sessions: int
    groups: list[FailureGroupOut]


def _group_out(group: accuracy_module.AccuracyGroup) -> AccuracyGroupOut:
    return AccuracyGroupOut(
        label=group.label,
        n_correct=group.n_correct,
        n_incorrect=group.n_incorrect,
        n_undecidable=group.n_undecidable,
        n_decided=group.n_decided,
        n_adjudicated=group.n_adjudicated,
        rate=group.rate,
    )


@router.get("/{actor_id}/accuracy", response_model=AccuracyOut)
async def get_agent_accuracy(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
    window_start: Annotated[datetime.datetime, Query()],
    window_end: Annotated[datetime.datetime, Query()],
    breakdown: Annotated[str, Query()] = accuracy_module.BREAKDOWN_OVERALL,
) -> AccuracyOut:
    """How often this agent's claims were judged correct."""
    try:
        result = await _services(request).agent_accuracy.accuracy_for(
            ctx,
            author_actor_id=actor_id,
            window_start=window_start,
            window_end=window_end,
            breakdown=breakdown,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return AccuracyOut(
        author_actor_id=result.author_actor_id,
        window_start=result.window_start,
        window_end=result.window_end,
        breakdown=result.breakdown,
        overall=_group_out(result.overall),
        groups=[_group_out(group) for group in result.groups],
    )


@router.get("/{actor_id}/autonomy", response_model=AutonomyOut)
async def get_agent_autonomy(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
    window_start: Annotated[datetime.datetime, Query()],
    window_end: Annotated[datetime.datetime, Query()],
) -> AutonomyOut:
    """How often this agent finished a session without being steered."""
    try:
        result = await _services(request).agent_autonomy.autonomy_for(
            ctx, author_actor_id=actor_id, window_start=window_start, window_end=window_end
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return AutonomyOut(
        author_actor_id=result.author_actor_id,
        window_start=result.window_start,
        window_end=result.window_end,
        n_sessions=result.n_sessions,
        n_intervened=result.n_intervened,
        n_autonomous=result.n_autonomous,
        intervention_rate=result.intervention_rate,
        autonomy_rate=result.autonomy_rate,
    )


@router.get("/{actor_id}/failure-patterns", response_model=FailurePatternsOut)
async def get_agent_failure_patterns(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
    window_start: Annotated[datetime.datetime, Query()],
    window_end: Annotated[datetime.datetime, Query()],
) -> FailurePatternsOut:
    """What this agent kept getting wrong, grouped and with examples.

    A `GET` that writes, which is worth flagging rather than hiding: it stores
    the report and returns its id, because an instruction change has to cite a
    stored one. Idempotent in the sense that matters — repeating it over the
    same window produces the same figures — but each call is a new report row,
    which is the record of when somebody looked.
    """
    try:
        report = await _services(request).agent_failure_patterns.build_report(
            ctx, author_actor_id=actor_id, window_start=window_start, window_end=window_end
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return FailurePatternsOut(
        report_id=report.report_id,
        author_actor_id=report.author_actor_id,
        window_start=report.window_start,
        window_end=report.window_end,
        n_adjudicated=report.n_adjudicated,
        n_incorrect=report.n_incorrect,
        n_sessions=report.n_sessions,
        n_intervention_sessions=report.n_intervention_sessions,
        groups=[
            FailureGroupOut(
                claim_category=group.claim_category,
                predicate=group.predicate,
                incorrect_count=group.incorrect_count,
                total_count=group.total_count,
                rate=group.rate,
                examples=[
                    FailureExampleOut(claim_id=example.claim_id, value=example.value, note=example.note)
                    for example in group.examples
                ],
            )
            for group in report.groups
        ],
    )


# ---------------------------------------------------------------------------
# Instruction lifecycle
# ---------------------------------------------------------------------------


class InstructionOut(_Strict):
    instruction_id: uuid.UUID
    author_actor_id: uuid.UUID
    version: int
    content: str
    motivated_by_report_id: uuid.UUID | None
    status: str
    activated_at: datetime.datetime | None
    superseded_at: datetime.datetime | None


class ProposeInstructionRequest(_Strict):
    version: int = Field(ge=1)
    content: str = Field(min_length=1)
    motivated_by_report_id: uuid.UUID = Field(
        description=(
            "The failure-pattern report this version answers. Required: an instruction in "
            "force has to say what evidence justified it, and the database refuses an "
            "active version that cites none."
        )
    )


class ProposedInstructionResponse(_Strict):
    instruction_id: uuid.UUID


class RollbackResponse(_Strict):
    restored_instruction_id: uuid.UUID | None = Field(
        description="Null when there was no predecessor — an agent's first version has nothing behind it."
    )


@router.get("/{actor_id}/instructions", response_model=list[InstructionOut])
async def list_agent_instructions(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
) -> list[InstructionOut]:
    """Every version of this agent's instructions, newest first."""
    history = await _services(request).agent_instructions.history(ctx, author_actor_id=actor_id)
    return [InstructionOut(**vars(instruction)) for instruction in history]


@router.post("/{actor_id}/instructions", response_model=ProposedInstructionResponse, status_code=201)
async def propose_agent_instruction(
    request: Request,
    body: ProposeInstructionRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
) -> ProposedInstructionResponse:
    """Record a candidate version. It does not take effect until activated."""
    try:
        instruction_id = await _services(request).agent_instructions.propose(
            ctx,
            author_actor_id=actor_id,
            version=body.version,
            content=body.content,
            motivated_by_report_id=body.motivated_by_report_id,
        )
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return ProposedInstructionResponse(instruction_id=instruction_id)


@router.post("/{actor_id}/instructions/{instruction_id}:activate", response_model=InstructionOut)
async def activate_agent_instruction(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
    instruction_id: Annotated[uuid.UUID, Path()],
) -> InstructionOut:
    """Put this version in force, superseding whichever held it."""
    services = _services(request)
    try:
        await services.agent_instructions.activate(ctx, instruction_id=instruction_id, now=services.clock.now())
    except (NotFoundError, ConflictError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    active = await services.agent_instructions.active_instruction(ctx, author_actor_id=actor_id)
    if active is None:  # pragma: no cover - activate just succeeded for this actor
        raise map_catalog_error(NotFoundError(f"no active instruction for actor {actor_id} after activation"))
    return InstructionOut(**vars(active))


@router.post("/{actor_id}/instructions:rollback", response_model=RollbackResponse)
async def rollback_agent_instruction(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_id: Annotated[uuid.UUID, Path()],
) -> RollbackResponse:
    """Return to the version that was in force before the current one.

    Returns null rather than erroring when there is no predecessor: "there is
    nothing to roll back to" is a fact the caller acts on, and the incumbent is
    left in force rather than demoted into a gap.
    """
    services = _services(request)
    restored = await services.agent_instructions.rollback(ctx, author_actor_id=actor_id, now=services.clock.now())
    return RollbackResponse(restored_instruction_id=restored)


# No `HttpMethodRouter` aliases here, deliberately. That helper exists to give a
# PATCH/PUT/DELETE route a POST-tunnelled twin, and these three are POST-only
# actions rather than verbs on a resource -- `POST /claims/{claim_id}:link` is
# the shape this codebase already uses for exactly that.
#
# It is also not merely stylistic: registering `activate` through the helper puts
# a plain `POST /{actor_id}/instructions/{instruction_id}` beside the aliased
# `...:activate`, and the plain route matches first with `instruction_id` set to
# "<uuid>:activate". The request 422s on UUID parsing rather than activating
# anything.


__all__ = ["router"]
