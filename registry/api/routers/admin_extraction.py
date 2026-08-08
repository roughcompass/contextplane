"""Operator surface for extraction strategies: enable, tune, override, inspect.

Shaped like the per-tenant PII configuration surface, and admin-gated the same
way. A tenant administrator decides which strategies run against their sessions
and what prompt they run with — nobody else's tenant is visible or reachable from
here.

**What this surface can change, and what it deliberately cannot.** Enablement,
confidence floor, prompt, and model are editable. The output schema, the permitted
predicate set, and the namespace template are not exposed at all. Those decide
what a claim is allowed to *mean*, and a tenant that could widen its own predicate
set would be redefining the shared vocabulary through a config field — which is
exactly what a deployment-wide ontology exists to prevent. The distinction is
worth stating on the surface itself, because "why can't I add a predicate here"
is the obvious question and the answer is a design decision rather than an
oversight.

**Reads show the effective configuration, not just the stored row.** A missing row
means defaults, so returning only stored rows would show nothing on a tenant that
has never configured anything — and an operator would reasonably conclude
extraction was off.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.routers._admin_common import _admin_required
from registry.extraction.config import (
    CONFORMANCE_TARGET,
    MIN_CONFORMANCE_SAMPLE,
    StrategyConfigService,
)
from registry.extraction.factory import default_model_for
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin")


class StrategyView(BaseModel):
    """A strategy as it will actually run for this tenant."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    is_enabled: bool
    confidence_floor: float
    prompt_is_overridden: bool
    model_is_overridden: bool
    model_id: str
    namespace_template: str
    # Read-only, and returned precisely so an operator can see the boundary they
    # are working inside rather than discovering it through a rejected claim.
    permitted_predicates: list[str]


class StrategyUpdate(BaseModel):
    """A change to one strategy's configuration.

    Every field is optional and `None` means "leave it alone". Clearing an
    override therefore needs its own flag: a single nullable field cannot express
    both "do not touch this" and "remove it", and conflating them means an
    operator who omits the prompt silently reverts to the shipped one.
    """

    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None
    confidence_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    prompt_override: str | None = Field(default=None, min_length=1, max_length=20_000)
    model_override: str | None = Field(default=None, min_length=1, max_length=200)
    clear_prompt_override: bool = False
    clear_model_override: bool = False


class StrategyConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    is_enabled: bool
    confidence_floor: float
    prompt_override: str | None
    model_override: str | None


class ConformancePolicyView(BaseModel):
    """The rule by which a strategy is judged defective."""

    model_config = ConfigDict(extra="forbid")

    target_ratio: float
    minimum_sample: int
    explanation: str


def _service(request: Request) -> StrategyConfigService:
    return StrategyConfigService(request.app.state.session_factory, clock=request.app.state.clock)


@router.get(
    "/extraction-strategies",
    response_model=list[StrategyView],
    tags=["admin: extraction"],
)
async def list_extraction_strategies(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[StrategyView]:
    """Every strategy as it will run for this tenant, enabled or not.

    Disabled strategies are included: a caller that could not see them would be
    unable to distinguish "switched off" from "does not exist in this build".
    """
    resolved = await _service(request).resolve(ctx.tenant_id)
    # The strategy table pins no model, so an unoverridden strategy resolves to
    # whatever the selected provider declares. Resolved here rather than
    # reported as null: the system knows which id will be sent, and answering
    # "unknown" would push the same lookup onto every caller of this endpoint.
    fallback = default_model_for(request.app.state.settings.extraction_provider)
    return [
        StrategyView(
            strategy_id=r.strategy.strategy_id,
            is_enabled=r.is_enabled,
            confidence_floor=r.confidence_floor,
            prompt_is_overridden=r.prompt_is_overridden,
            model_is_overridden=r.model_is_overridden,
            model_id=r.strategy.default_model_id or fallback,
            namespace_template=r.strategy.namespace_template,
            permitted_predicates=list(r.strategy.permitted_predicates),
        )
        for r in resolved
    ]


async def update_extraction_strategy(
    strategy_id: str,
    body: StrategyUpdate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> StrategyConfigView:
    """Enable, disable, or tune one strategy for this tenant.

    An override changes how well claims are found, never what they mean: the
    schema, the predicate set, and the namespace are not editable here.
    """
    config = await _service(request).upsert(
        ctx,
        strategy_id=strategy_id,
        is_enabled=body.is_enabled,
        confidence_floor=body.confidence_floor,
        prompt_override=body.prompt_override,
        model_override=body.model_override,
        clear_prompt_override=body.clear_prompt_override,
        clear_model_override=body.clear_model_override,
    )
    return StrategyConfigView(
        strategy_id=config.strategy_id,
        is_enabled=config.is_enabled,
        confidence_floor=config.confidence_floor,
        prompt_override=config.prompt_override,
        model_override=config.model_override,
    )


@router.get(
    "/extraction-strategies/conformance-policy",
    response_model=ConformancePolicyView,
    tags=["admin: extraction"],
)
async def get_conformance_policy() -> ConformancePolicyView:
    """The threshold at which a strategy is reported as a defective prompt.

    Exposed because the number decides when an operator gets told their prompt is
    broken, and a threshold nobody can look up is one nobody trusts.
    """
    return ConformancePolicyView(
        target_ratio=CONFORMANCE_TARGET,
        minimum_sample=MIN_CONFORMANCE_SAMPLE,
        explanation=(
            f"A strategy whose candidates conform at below {CONFORMANCE_TARGET:.0%}, measured "
            f"over at least {MIN_CONFORMANCE_SAMPLE} candidates, is reported as a defective "
            "prompt rather than retried. Retrying non-conformance costs a provider call per "
            "attempt and produces the same wrong output, so a person has to change the prompt. "
            "Below the minimum sample no verdict is issued, because a handful of refusals is "
            "not evidence that anything is wrong."
        ),
    )


# ---------------------------------------------------------------------------
# Mutation router (PATCH via HttpMethodRouter)
#
# Registered through the method-router factory rather than as a bare @router.patch
# so the deployment-wide HTTP-methods mode applies. A directly-declared PATCH
# would be unreachable in post-only mode -- and the drift would only surface as a
# conformance failure, not as anything an operator could see.
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/admin")
_mode, _sep = get_mode_settings()
_mutation_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mutation_mr.add_mutation_route(
    path="/extraction-strategies/{strategy_id}",
    action="update",
    handler=update_extraction_strategy,
    verb="PATCH",
    response_model=StrategyConfigView,
    tags=["admin: extraction"],
)

mutation_router = _mutation_base

__all__ = ["mutation_router", "router"]
