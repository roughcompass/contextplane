"""The evaluation-run REST surface.

    POST /v1/evaluation/prompt-sets                   → PromptSetResponse
    GET  /v1/evaluation/prompt-sets                   → PromptSetListResponse
    POST /v1/evaluation/prompt-sets/{set_id}/prompts  → PromptResponse
    POST /v1/evaluation/prompt-sets/{set_id}/runs     → RunResponse
    GET  /v1/evaluation/prompt-sets/{set_id}/runs     → RunListResponse
    GET  /v1/evaluation/runs/{run_id}                 → RunResponse
    POST /v1/evaluation/runs/items/{item_id}/verdict  → VerdictResponse

This router adapts and does not decide. Which prompts a run resolves, what a
verdict may say, whether two runs are comparable and what happens to a prompt
that raised are all settled in `context/evaluation/runs.py`, because the MCP
surface answers the same questions and a rule enforced in one adapter is a rule
the other will eventually enforce differently.

**A run is a `POST` that resolves synchronously.** It is bounded — a set holds at
most a hundred prompts — and the alternative, a job id the caller polls, would
add a second lifecycle to something whose whole value is that a person clicked
it and watched. A set large enough to need a queue is a set that has outgrown
this surface, and the bound is where that shows up.

**The verdict route is keyed by run item, not by run.** A run of twenty prompts
where three were wrong is not "bad": it is right seventeen times and wrong three,
and the three are what somebody has to look at. A run-level verdict would flatten
exactly the signal the loop exists to produce.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.evaluation import (
    AddPromptRequest,
    CreatePromptSetRequest,
    PromptResponse,
    PromptSetListResponse,
    PromptSetResponse,
    RecordVerdictRequest,
    RunListResponse,
    RunResponse,
    VerdictResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import CatalogError
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/evaluation", tags=["evaluation"])

# Reading an evaluation is what an auditor is for. Writing one — a set, a run, a
# verdict — is a producer's act, and a run additionally resolves context, which
# the resolver authorizes again underneath on the caller's own identity.
_read_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR])
_write_required = require_roles([ROLE_PRODUCER, ROLE_ADMIN])


@router.post("/prompt-sets", response_model=PromptSetResponse)
async def create_prompt_set(
    body: CreatePromptSetRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> PromptSetResponse:
    """Create an empty named set. Prompts are added one at a time."""
    try:
        created = await container.evaluation_runs.create_set(ctx, name=body.name, description=body.description)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return PromptSetResponse.of(created)


@router.get("/prompt-sets", response_model=PromptSetListResponse)
async def list_prompt_sets(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    page_size: int = 50,
) -> PromptSetListResponse:
    """This tenant's sets, newest first."""
    try:
        found = await container.evaluation_runs.list_sets(ctx, page_size=page_size)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return PromptSetListResponse(items=[PromptSetResponse.of(entry) for entry in found])


@router.post("/prompt-sets/{set_id}/prompts", response_model=PromptResponse)
async def add_prompt(
    set_id: uuid.UUID,
    body: AddPromptRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> PromptResponse:
    """Append one context request to a set.

    The request is validated against the same shape the resolver takes, so a
    prompt that could never resolve is refused here rather than failing on every
    run afterwards.
    """
    try:
        added = await container.evaluation_runs.add_prompt(
            ctx,
            set_id=set_id,
            request=body.request,
            intent_note=body.intent_note,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return PromptResponse.of(added)


@router.post("/prompt-sets/{set_id}/runs", response_model=RunResponse)
async def start_run(
    set_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> RunResponse:
    """Resolve every prompt in the set, once, and keep what came back.

    Every prompt is attempted. One that raises produces an item saying so and the
    run continues — stopping would leave the rest unmeasured and report on a
    subset chosen by whichever prompt happened to fail first.
    """
    try:
        run = await container.evaluation_runs.start_run(ctx, set_id=set_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return RunResponse.of(run)


@router.get("/prompt-sets/{set_id}/runs", response_model=RunListResponse)
async def list_runs(
    set_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
    page_size: int = 20,
) -> RunListResponse:
    """This set's runs, newest first, without their items.

    Headers only, because a comparison starts by choosing two runs and loading
    every item of every run to render that choice would read the whole history
    to answer a question about two rows of it.
    """
    try:
        found = await container.evaluation_runs.runs_of(ctx, set_id=set_id, page_size=page_size)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return RunListResponse(items=[RunResponse.of(run) for run in found])


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> RunResponse:
    """One run, its items in the set's order, and every verdict on them."""
    try:
        run = await container.evaluation_runs.run(ctx, run_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return RunResponse.of(run)


@router.post("/runs/items/{item_id}/verdict", response_model=VerdictResponse)
async def record_verdict(
    item_id: uuid.UUID,
    body: RecordVerdictRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> VerdictResponse:
    """Record what this reviewer thought of one prompt's resolution.

    Attributed to the caller and not to an actor the caller names: a verdict
    somebody could file under another person's name is not evidence about
    anything. A second verdict from the same reviewer replaces the first, because
    somebody who changed their mind has one opinion.
    """
    try:
        recorded = await container.evaluation_runs.record_verdict(
            ctx, item_id=item_id, verdict=body.verdict, note=body.note
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return VerdictResponse.of(recorded)
