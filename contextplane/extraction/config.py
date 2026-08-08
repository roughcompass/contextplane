"""Resolving what a strategy actually does for a given tenant.

A strategy has code defaults; a tenant may overlay some of them. This module is
where the two combine, and where the line between them is enforced.

**Overridable:** enablement, confidence floor, prompt, model. All four change how
well claims are found.

**Not overridable, and not present here at all:** the output schema, the permitted
predicate set, the namespace template. Those decide what a claim is allowed to
*mean*. A tenant that could widen its own predicate set would be redefining the
shared vocabulary from inside a configuration field, and the whole point of a
deployment-wide ontology is that a predicate means the same thing everywhere.

**A missing row means defaults, not disabled.** Absence is the common case. A
deployment that needed a row per tenant per strategy before extraction did
anything would look broken on every new tenant, and somebody would "fix" it by
inserting rows with whatever values were handy.

**Conformance is judged per strategy, and a bad prompt is reported rather than
retried.** A strategy whose output is mostly refused is a defective prompt: no
number of retries fixes it, and retrying costs real money per attempt. The
judgement needs a minimum sample, because two refusals out of two is not evidence
of anything.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid

from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.extraction.strategies import STRATEGIES, Strategy
from contextplane.types import Clock, TenantContext

_log = logging.getLogger(__name__)

# The share of candidates that must conform for a strategy to be considered
# healthy. Below it, the strategy is reported as defective.
CONFORMANCE_TARGET = 0.95

# Below this many candidates, no judgement is made. Two refusals out of two is
# not evidence that a prompt is broken, and reporting it as such would mean every
# new strategy is defective on its first quiet hour.
MIN_CONFORMANCE_SAMPLE = 20

_DEFECTIVE = Counter(
    "registry_extraction_strategy_defective_total",
    "Times a strategy was reported as defective for sustained non-conformance.",
    ["strategy"],
)

_CONFORMANCE_GAUGE = Gauge(
    "registry_extraction_strategy_conformance_ratio",
    "Most recent measured conformance ratio, per strategy.",
    ["strategy"],
)


@dataclasses.dataclass(frozen=True)
class ResolvedStrategy:
    """A strategy as it runs for one tenant: defaults plus that tenant's overlay."""

    strategy: Strategy
    is_enabled: bool
    confidence_floor: float
    # True when this tenant supplied its own prompt. Surfaced because a strategy
    # producing poor output under an override is a different problem from one
    # producing poor output under the shipped prompt, and the fix is different.
    prompt_is_overridden: bool
    model_is_overridden: bool

    def namespace_for(self, *, tenant_id: uuid.UUID, actor_id: uuid.UUID, session_id: str) -> str:
        return self.strategy.namespace_for(tenant_id=str(tenant_id), actor_id=str(actor_id), session_id=session_id)


@dataclasses.dataclass(frozen=True)
class StrategyConfig:
    """One stored configuration row, as an operator sees it."""

    strategy_id: str
    is_enabled: bool
    confidence_floor: float
    prompt_override: str | None
    model_override: str | None
    updated_at: datetime.datetime


class StrategyConfigService:
    """Reads and writes per-tenant strategy configuration."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def resolve(self, tenant_id: uuid.UUID) -> tuple[ResolvedStrategy, ...]:
        """Every strategy as it runs for this tenant, enabled or not.

        Returns disabled strategies too. A caller that filtered them out here
        could not tell "disabled" from "does not exist", and the operator surface
        needs to show both.
        """
        stored = {row.strategy_id: row for row in await self._rows(tenant_id)}
        resolved: list[ResolvedStrategy] = []

        for strategy in STRATEGIES.values():
            row = stored.get(strategy.strategy_id)
            if row is None:
                resolved.append(
                    ResolvedStrategy(
                        strategy=strategy,
                        is_enabled=True,
                        confidence_floor=strategy.default_confidence_floor,
                        prompt_is_overridden=False,
                        model_is_overridden=False,
                    )
                )
                continue

            resolved.append(
                ResolvedStrategy(
                    strategy=strategy.with_overrides(
                        system_prompt=row.prompt_override,
                        model_id=row.model_override,
                    ),
                    is_enabled=row.is_enabled,
                    confidence_floor=float(row.confidence_floor),
                    prompt_is_overridden=row.prompt_override is not None,
                    model_is_overridden=row.model_override is not None,
                )
            )
        return tuple(resolved)

    async def resolve_one(self, tenant_id: uuid.UUID, strategy_id: str) -> ResolvedStrategy:
        for resolved in await self.resolve(tenant_id):
            if resolved.strategy.strategy_id == strategy_id:
                return resolved
        msg = f"unknown strategy {strategy_id!r}; expected one of {sorted(STRATEGIES)}"
        raise NotFoundError(msg)

    async def upsert(
        self,
        ctx: TenantContext,
        *,
        strategy_id: str,
        is_enabled: bool | None = None,
        confidence_floor: float | None = None,
        prompt_override: str | None = None,
        model_override: str | None = None,
        clear_prompt_override: bool = False,
        clear_model_override: bool = False,
    ) -> StrategyConfig:
        """Set this tenant's overlay for one strategy.

        `clear_*` flags exist because `None` already means "leave unchanged". A
        single nullable field cannot express both "do not touch it" and "remove
        the override", and conflating them means an operator who omits a field
        silently reverts to the shipped prompt.
        """
        if strategy_id not in STRATEGIES:
            msg = f"unknown strategy {strategy_id!r}; expected one of {sorted(STRATEGIES)}"
            raise NotFoundError(msg)
        if confidence_floor is not None and not 0.0 <= confidence_floor <= 1.0:
            msg = f"confidence floor must be between 0 and 1, got {confidence_floor}"
            raise ValidationError(msg)
        if prompt_override is not None and not prompt_override.strip():
            msg = (
                "an empty prompt override is not an override; it would leave the model with no "
                "instructions while extraction kept running. Clear it instead."
            )
            raise ValidationError(msg)
        if prompt_override is not None and clear_prompt_override:
            msg = "cannot both set and clear the prompt override"
            raise ValidationError(msg)
        if model_override is not None and clear_model_override:
            msg = "cannot both set and clear the model override"
            raise ValidationError(msg)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            existing = (
                await session.execute(
                    text(
                        "SELECT is_enabled, confidence_floor, prompt_override, model_override "
                        "FROM memory_strategy_config "
                        "WHERE tenant_id = :tid AND strategy_id = :sid FOR UPDATE"
                    ),
                    {"tid": ctx.tenant_id, "sid": strategy_id},
                )
            ).one_or_none()

            default_floor = STRATEGIES[strategy_id].default_confidence_floor
            merged_enabled = is_enabled if is_enabled is not None else (existing.is_enabled if existing else True)
            merged_floor = (
                confidence_floor
                if confidence_floor is not None
                else (float(existing.confidence_floor) if existing else default_floor)
            )
            merged_prompt = _merge_override(
                new=prompt_override,
                clear=clear_prompt_override,
                current=existing.prompt_override if existing else None,
            )
            merged_model = _merge_override(
                new=model_override,
                clear=clear_model_override,
                current=existing.model_override if existing else None,
            )

            await session.execute(
                text(
                    "INSERT INTO memory_strategy_config "
                    "  (tenant_id, strategy_id, is_enabled, confidence_floor, prompt_override, "
                    "   model_override, updated_at, updated_by) "
                    "VALUES (:tid, :sid, :enabled, CAST(:floor AS NUMERIC), :prompt, :model, "
                    "        CAST(:now AS TIMESTAMPTZ), :actor) "
                    "ON CONFLICT (tenant_id, strategy_id) DO UPDATE "
                    "SET is_enabled = EXCLUDED.is_enabled, "
                    "    confidence_floor = EXCLUDED.confidence_floor, "
                    "    prompt_override = EXCLUDED.prompt_override, "
                    "    model_override = EXCLUDED.model_override, "
                    "    updated_at = EXCLUDED.updated_at, "
                    "    updated_by = EXCLUDED.updated_by"
                ),
                {
                    "tid": ctx.tenant_id,
                    "sid": strategy_id,
                    "enabled": merged_enabled,
                    "floor": merged_floor,
                    "prompt": merged_prompt,
                    "model": merged_model,
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )

        return StrategyConfig(
            strategy_id=strategy_id,
            is_enabled=merged_enabled,
            confidence_floor=merged_floor,
            prompt_override=merged_prompt,
            model_override=merged_model,
            updated_at=now,
        )

    async def _rows(self, tenant_id: uuid.UUID) -> list[_Row]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT strategy_id, is_enabled, confidence_floor, prompt_override, "
                    "       model_override, updated_at "
                    "FROM memory_strategy_config WHERE tenant_id = :tid ORDER BY strategy_id"
                ),
                {"tid": tenant_id},
            )
            return [
                _Row(
                    strategy_id=r.strategy_id,
                    is_enabled=r.is_enabled,
                    confidence_floor=r.confidence_floor,
                    prompt_override=r.prompt_override,
                    model_override=r.model_override,
                    updated_at=r.updated_at,
                )
                for r in result.all()
            ]


def _merge_override(*, new: str | None, clear: bool, current: str | None) -> str | None:
    """`None` means leave alone; `clear` means remove. They are different asks."""
    if clear:
        return None
    if new is not None:
        return new
    return current


@dataclasses.dataclass(frozen=True)
class ConformanceVerdict:
    """Whether a strategy's output is conforming well enough to keep spending on."""

    strategy_id: str
    candidates: int
    staged: int
    ratio: float
    is_defective: bool
    reason: str

    @property
    def sample_is_sufficient(self) -> bool:
        return self.candidates >= MIN_CONFORMANCE_SAMPLE


def judge_conformance(
    strategy_id: str,
    *,
    candidates: int,
    staged: int,
    target: float = CONFORMANCE_TARGET,
    minimum_sample: int = MIN_CONFORMANCE_SAMPLE,
) -> ConformanceVerdict:
    """Report a defective prompt rather than retrying it.

    Retrying non-conformance is the failure mode this exists to prevent: the
    output is wrong in the same way every time, each attempt costs a real call,
    and the queue looks busy while nothing is produced. A prompt is fixed by a
    person, so the correct response is to say so.

    An insufficient sample is never defective. Two refusals out of two is not
    evidence, and judging it would make every strategy defective on its first
    quiet hour -- after which nobody would believe the signal.
    """
    ratio = 1.0 if candidates == 0 else staged / candidates
    _CONFORMANCE_GAUGE.labels(strategy=strategy_id).set(ratio)

    if candidates < minimum_sample:
        return ConformanceVerdict(
            strategy_id=strategy_id,
            candidates=candidates,
            staged=staged,
            ratio=ratio,
            is_defective=False,
            reason=(f"sample too small to judge ({candidates} < {minimum_sample} candidates); " "no verdict"),
        )

    if ratio >= target:
        return ConformanceVerdict(
            strategy_id=strategy_id,
            candidates=candidates,
            staged=staged,
            ratio=ratio,
            is_defective=False,
            reason=f"conformance {ratio:.2%} meets the {target:.0%} target",
        )

    _DEFECTIVE.labels(strategy=strategy_id).inc()
    reason = (
        f"conformance {ratio:.2%} is below the {target:.0%} target over {candidates} "
        f"candidates. This is a defective prompt, not a transient failure: the output is "
        f"wrong the same way every time, so retrying costs a call per attempt and produces "
        f"nothing. Review the strategy's prompt or its predicate set."
    )
    _log.error("extraction.strategy_defective strategy=%s %s", strategy_id, reason)
    return ConformanceVerdict(
        strategy_id=strategy_id,
        candidates=candidates,
        staged=staged,
        ratio=ratio,
        is_defective=True,
        reason=reason,
    )


@dataclasses.dataclass(frozen=True)
class _Row:
    strategy_id: str
    is_enabled: bool
    confidence_floor: float
    prompt_override: str | None
    model_override: str | None
    updated_at: datetime.datetime


__all__ = [
    "CONFORMANCE_TARGET",
    "MIN_CONFORMANCE_SAMPLE",
    "ConformanceVerdict",
    "ResolvedStrategy",
    "StrategyConfig",
    "StrategyConfigService",
    "judge_conformance",
]
