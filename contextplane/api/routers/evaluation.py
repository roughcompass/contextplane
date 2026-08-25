"""The evaluation-run REST surface.

    POST /v1/evaluation/prompt-sets                   → PromptSetResponse
    GET  /v1/evaluation/prompt-sets                   → PromptSetListResponse
    POST /v1/evaluation/prompt-sets/{set_id}/prompts  → PromptResponse
    POST /v1/evaluation/prompt-sets/{set_id}/runs     → RunResponse
    GET  /v1/evaluation/prompt-sets/{set_id}/runs     → RunListResponse
    GET  /v1/evaluation/runs/{run_id}                 → RunResponse
    POST /v1/evaluation/runs/items/{item_id}/verdict  → VerdictResponse
    GET  /v1/evaluation/expectation-presets           → PresetListResponse
    GET  /v1/evaluation/simulations/availability      → SimulationAvailabilityResponse
    POST /v1/evaluation/simulations                   → SimulationResponse
    GET  /v1/evaluation/simulations/{simulation_id}   → SimulationResponse
    POST /v1/evaluation/simulations/{simulation_id}/judgements     → JudgementListResponse
    GET  /v1/evaluation/simulations/{simulation_id}/judgements     → JudgementListResponse
    POST /v1/evaluation/judgements/{judgement_id}/review           → ReviewResponse

This router adapts and does not decide. Which prompts a run resolves, what a
verdict may say, whether two runs are comparable and what happens to a prompt
that raised are all settled in `context/evaluation/runs.py`, because the MCP
surface answers the same questions and a rule enforced in one adapter is a rule
the other will eventually enforce differently.

**The simulation guards are in the service for that reason and no other.**
Whether a principal may be simulated (ADR 0019) and whether a candidate and its
judge share a provider family (ADR 0026) are decided in
`context/evaluation/simulation.py`. This router maps their refusals onto status
codes and adds none of its own.

**The resolver still does not generate.** `POST /v1/context/resolve` is untouched
by these three routes; a simulation resolves through it and *then* calls a model,
and the receipt and the simulation stay separately addressable — which is what
keeps "the retrieval was fine and the agent fumbled it" answerable.

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

from fastapi import APIRouter, Depends, HTTPException, status

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.evaluation import (
    AddPromptRequest,
    CreatePromptSetRequest,
    PresetListResponse,
    PromptResponse,
    PromptSetListResponse,
    PromptSetResponse,
    RecordVerdictRequest,
    RunListResponse,
    RunResponse,
    VerdictResponse,
)
from contextplane.api.schemas.judgement import (
    JudgementListResponse,
    JudgementResponse,
    RecordJudgementReviewRequest,
    ReviewResponse,
    RunJudgementRequest,
)
from contextplane.api.schemas.simulation import (
    RunSimulationRequest,
    SimulationAvailabilityResponse,
    SimulationResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import CatalogError
from contextplane.extraction.provider import ProviderError
from contextplane.extraction.response_factory import JudgeFamilyRefused
from contextplane.extraction.response_provider import SimulationUnavailable
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
            expectations=body.expectations,
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


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


@router.get("/simulations/availability", response_model=SimulationAvailabilityResponse)
async def simulation_availability(
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> SimulationAvailabilityResponse:
    """Whether this deployment can simulate, and under which selectors.

    Declared before the route above it in path order deliberately: FastAPI
    matches in declaration order, and `/simulations/{simulation_id}` would
    otherwise swallow `availability` and fail on a UUID parse.

    Carries no credential and no endpoint — only which selectors are in force,
    which is what somebody needs to fix a refusal. A deployment with no provider
    is complete rather than broken: prompt sets, runs, verdicts and the
    deterministic criteria all work, and this says which half is switched off.
    """
    simulation = container.simulation
    settings = container.settings
    return SimulationAvailabilityResponse(
        available=simulation.is_available,
        judge_model=settings.judge_model,
        judge_provider=settings.judge_provider,
        simulation_model=settings.simulation_model,
        simulation_provider=settings.simulation_provider,
    )


@router.post("/simulations", response_model=SimulationResponse)
async def run_simulation(
    body: RunSimulationRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> SimulationResponse:
    """Resolve as a declared agent, then answer from what came back.

    Two records: the resolver writes its receipt and this writes the generation
    beside it, referencing rather than copying. Both remain separately
    addressable, so "the retrieval was fine and the agent fumbled it" stays a
    question somebody can answer.

    The resolution runs on the *caller's* identity. `simulated_actor_id` names
    whose behaviour is being modelled and grants nothing — resolving under the
    simulated principal's entitlements would be a privilege escalation wearing an
    evaluation feature.
    """
    try:
        simulation = await container.simulation.simulate(
            ctx,
            prompt=body.prompt,
            resolver_arguments=_resolver_arguments(body.request),
            run_item_id=body.run_item_id,
            simulated_actor_id=body.simulated_actor_id,
        )
    except JudgeFamilyRefused as exc:
        # 409 rather than 503: the deployment is reachable and configured, and
        # what it is configured as is the problem. A 503 would invite a retry
        # against a configuration that cannot change by being retried.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    except SimulationUnavailable as exc:
        # 501 rather than 503: the capability is not implemented *on this
        # deployment*, which is a permanent answer until somebody configures it,
        # and 503 says "try again shortly" to a caller for whom that is false.
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=exc.reason) from exc
    except ProviderError as exc:
        # The resolution happened and its receipt is written; only the generation
        # failed. 502 says the upstream model is the failing party, which is what
        # a caller needs to know before deciding whether to retry.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.reason) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return SimulationResponse.of(simulation)


@router.get("/simulations/{simulation_id}", response_model=SimulationResponse)
async def get_simulation(
    simulation_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> SimulationResponse:
    """One simulation, its assertions in order, and every citation on them.

    `envelope_state` is empty on this read and that is deliberate: the state
    belongs to the resolution, which owns it on the receipt. A copy stored here
    could not be corrected when the receipt was, so the reader joins instead.
    """
    try:
        simulation = await container.simulation.get(ctx, simulation_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return SimulationResponse.of(simulation)


def _resolver_arguments(request: dict[str, object]) -> dict[str, object]:
    """The saved-prompt shape, minus the query the prompt supplies.

    Validated through `PromptRequestV1` rather than passed through, so a
    misspelled field is refused here instead of resolving a different question
    than the caller asked. `query` is filled with the prompt because that model
    requires one and the simulation's prompt is it.
    """
    from contextplane.context.evaluation.prompt_request import PromptRequestV1  # noqa: PLC0415

    body = {key: value for key, value in request.items() if key != "query"}
    validated = PromptRequestV1.of({**body, "query": "simulation"})
    arguments = validated.resolver_arguments()
    arguments.pop("query")
    return arguments


# ---------------------------------------------------------------------------
# Judged criteria, and the human who may overrule the judge
# ---------------------------------------------------------------------------


@router.post("/simulations/{simulation_id}/judgements", response_model=JudgementListResponse)
async def judge_simulation(
    simulation_id: uuid.UUID,
    body: RunJudgementRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> JudgementListResponse:
    """Grade one simulated answer on groundedness and answer relevance.

    Both criteria in one call, because they are read from the same material and
    two calls would double the cost to produce two verdicts that could disagree
    about what the answer said.

    Refused with `409` when the judge shares a provider family with the candidate
    — a judge from the candidate's own family scores it 10–25 % higher than a
    third party does, and that is a constraint rather than advice.
    """
    try:
        judged = await container.judgement.judge(ctx, simulation_id=simulation_id, panel_position=body.panel_position)
    except JudgeFamilyRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    except SimulationUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=exc.reason) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.reason) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return JudgementListResponse(items=[JudgementResponse.of(entry) for entry in judged])


@router.get("/simulations/{simulation_id}/judgements", response_model=JudgementListResponse)
async def list_judgements(
    simulation_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(_read_required)],
    container: Annotated[Services, Depends(services)],
) -> JudgementListResponse:
    """Every judged criterion of one simulation, with every review on each.

    `confidence_is_calibrated` is false on every row until E24-T6 fits bins for
    the pinned tuple. It is sent rather than left for each client to work out,
    because a client that got it wrong would render an unexamined number with an
    authoritative look — on the screen whose job is calibrating trust.
    """
    try:
        judged = await container.judgement.judgements_of(ctx, simulation_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return JudgementListResponse(items=[JudgementResponse.of(entry) for entry in judged])


@router.post("/judgements/{judgement_id}/review", response_model=ReviewResponse)
async def record_judgement_review(
    judgement_id: uuid.UUID,
    body: RecordJudgementReviewRequest,
    ctx: Annotated[TenantContext, Depends(_write_required)],
    container: Annotated[Services, Depends(services)],
) -> ReviewResponse:
    """Confirm or overrule one judged criterion.

    A second fact beside the judge's, never a correction to it: the pair (what
    the judge said, what the person said) is the only thing calibration can be
    fitted from, and overwriting would destroy it.

    Attributed to the caller and not to an actor the caller names. A second
    review from the same reviewer replaces the first, because somebody who
    changed their mind has one opinion; two reviewers disagreeing stays two rows.
    """
    try:
        recorded = await container.judgement.record_review(
            ctx,
            judgement_id=judgement_id,
            note=body.note,
            observed_confidence=body.observed_confidence,
            verdict=body.verdict,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    return ReviewResponse.of(recorded)


@router.get("/expectation-presets", response_model=PresetListResponse)
async def list_expectation_presets(
    ctx: Annotated[TenantContext, Depends(_read_required)],
) -> PresetListResponse:
    """The seeded personas a prompt's expectations can be started from.

    *"Here is a best practice, but you may amend for a given persona."* Each is a
    parameterization of the same five criteria, never an extension: a persona
    that could add a criterion would be a rubric, and two rubrics produce two
    numbers nobody can put side by side.

    Each carries the rubric versions its thresholds were written against, because
    a threshold on a criterion that has since been redefined is a number
    describing something else.

    No tenant scoping: these are the shapes this deployment ships, not this
    tenant's data. The read gate is still applied, because a caller with no role
    on this deployment has no business enumerating its evaluation vocabulary.
    """
    return PresetListResponse.seeded()
