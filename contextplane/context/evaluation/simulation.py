"""Resolve as a declared agent, then answer — two records, one operation.

E24-T3, on ADR 0025. The resolver does not generate; this composes a resolution
with a model call and receipts the two halves separately, so *"the retrieval was
fine and the agent fumbled it"* stays an answerable question.

## The guards are here and not in a router

Every service has two transports; a check on a route is a check the MCP tool does
not have. Both of E24's refusals live in `simulate()`:

**A principal nobody declared as an agent cannot be simulated.** ADR 0019
established that an undeclared principal is `unknown` and never `human`, because
nothing can infer the kind from the transport — a person in an IDE and an
unattended agent arrive over the identical MCP path. Simulating a principal
nobody has declared would be the product asserting exactly the thing ADR 0019
refused to assert, at the moment it is least checkable. The refusal names the
principal and the declaration route.

**A judge from the candidate's own provider family is refused, not warned about.**
ADR 0026, and the check runs on the simulation path rather than at startup
because a deployment may configure the judge after the simulator. The refusal
names both models.

## What the model is given, and what it is not

The five-block envelope, delimited as data with a per-request unguessable
boundary, exactly as session bodies are for extraction. Every block appears
including the empty and failed ones, each carrying its own state — an agent told
a block is `empty` may say the material does not exist, and an agent told it
`failed` must not.

**The declared instruction set is not reproduced.** Contextplane is not its store
of record, per ADR 0020, and a copy in the prompt would be the second copy that
ADR refused to hold. What is passed is the delta — the product's own correction,
which it authored and can be held to — plus which of the three dispositions the
resolution ran under.

## What is recorded

A simulation row referencing the resolution's receipt, its assertions in order,
and each assertion's citations with whether the cited id was actually served.
`was_served` is computed at write time by the one component holding both sides;
recomputing it later would mean re-reading a receipt to answer a question the
write already knew.

**A citation naming an id that was never served is stored as declared.** That is
not an insert to reject: "the model cited something that was not in the envelope"
is a finding, and a foreign key would turn it into a failed write with no record
of what the model said.

## Token usage is reported exactly, or explicitly unknown

The provider contract forbids guessing and `UsageSource` keeps the three cases
apart. The served item count travels beside the token figure because `limit` is
the only lever the product offers when a run comes back too large, and a token
count with no cardinality beside it says nothing about what to do.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.extraction.provider import ProviderError
from contextplane.extraction.response_factory import assert_families_differ, resolved_model
from contextplane.extraction.response_provider import (
    ResponseRequest,
    ServedBlockView,
    ServedItemView,
    SimulationUnavailable,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.resolve import ContextResolver, ResolvedContext
    from contextplane.context.schemas.envelope import ContextEnvelopeV1
    from contextplane.extraction.response_provider import ResponseProvider
    from contextplane.types import Clock, TenantContext

#: The longest prompt a simulation accepts, matching the column's own check. A
#: bound rather than a target: a prompt long enough to fill the context window on
#: its own leaves no room for the envelope, which is the material the whole
#: operation exists to answer from.
MAX_PROMPT_CHARS: Final[int] = 20_000

#: The declaration that makes a principal simulatable. `human` is a declaration
#: too, and it is refused here for the same reason `unknown` is: simulating a
#: person is not a thing this operation does, and letting it through would put a
#: machine's answer under a human's name in the record.
SIMULATABLE_KIND: Final = "agent"


@dataclasses.dataclass(frozen=True)
class CitedItem:
    """One receipt item id an assertion rested on, and whether it was served."""

    receipt_item_id: str
    was_served: bool


@dataclasses.dataclass(frozen=True)
class SimulatedAssertion:
    """One claim the answer made, in the order the model made it."""

    position: int
    text: str
    citations: tuple[CitedItem, ...]


@dataclasses.dataclass(frozen=True)
class TokenReport:
    """What the call cost, as reported or as explicitly unknown.

    Never estimated. `source` is what keeps a spend total from silently mixing a
    measured figure with a computed one.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    source: str
    #: The cardinality that produced the figure. Paired with it deliberately.
    served_item_count: int


@dataclasses.dataclass(frozen=True)
class Simulation:
    """One resolution and the answer generated from it, kept apart.

    `receipt_id` references the resolution's own record rather than embedding it.
    A reader can ask what was served and what was said about it independently,
    which is the whole diagnostic value of the split.
    """

    simulation_id: uuid.UUID
    receipt_id: uuid.UUID
    simulated_actor_id: uuid.UUID
    prompt: str
    answer: str
    provider_id: str
    model_id: str
    instruction_disposition: str
    envelope_state: str
    usage: TokenReport
    assertions: tuple[SimulatedAssertion, ...]
    duration_ms: int | None
    created_at: datetime.datetime
    run_item_id: uuid.UUID | None = None

    @property
    def uncited_served_ids(self) -> tuple[str, ...]:
        """Served items no assertion rested on — one of E24-T13's observations.

        Computed from what this object carries rather than stored, because it is
        a view over two facts the row already holds and a stored copy would be a
        third place the same answer lives.
        """
        cited = {citation.receipt_item_id for assertion in self.assertions for citation in assertion.citations}
        return tuple(sorted(self.served_receipt_item_ids - cited))

    #: Every receipt item id the envelope actually served. Set by the service at
    #: construction; a simulation read back from storage carries the ids its
    #: citations name plus whatever the receipt says, which is the reader's join.
    served_receipt_item_ids: frozenset[str] = frozenset()


class SimulationService:
    """Resolve as a declared agent, generate, and record both halves."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        resolver: ContextResolver,
        clock: Clock,
        provider: ResponseProvider | None,
        provider_selector: str,
        model_pin: str,
        max_output_tokens: int,
        judge_selector: str,
        judge_model_pin: str,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver
        self._clock = clock
        self._provider = provider
        self._selector = provider_selector
        self._model_pin = model_pin
        self._max_output_tokens = max_output_tokens
        self._judge_selector = judge_selector
        self._judge_model_pin = judge_model_pin

    @property
    def is_available(self) -> bool:
        """Whether this deployment can simulate at all.

        A read a surface calls before offering the action, so an operator sees a
        switched-off feature rather than a button that always fails.
        """
        return self._provider is not None

    async def simulate(
        self,
        ctx: TenantContext,
        *,
        simulated_actor_id: uuid.UUID,
        prompt: str,
        resolver_arguments: dict[str, Any],
        run_item_id: uuid.UUID | None = None,
    ) -> Simulation:
        """One prompt, resolved as a declared agent and then answered.

        The resolution runs on the caller's own identity, through the resolver
        every other caller uses — not a copy of it and not with checks relaxed
        for evaluation. An evaluation that ran against a laxer path would measure
        something the product does not serve, which is the most expensive kind of
        wrong answer available here.

        `simulated_actor_id` says whose behaviour is being modelled; it does not
        grant that principal's authority. Resolving under the simulated
        principal's entitlements would let anyone read anything by naming an
        agent, which is a privilege escalation wearing an evaluation feature.
        """
        question = prompt.strip()
        if not (1 <= len(question) <= MAX_PROMPT_CHARS):
            raise ValidationError(f"a simulation prompt is 1 to {MAX_PROMPT_CHARS} characters")

        provider = self._require_provider()
        assert_families_differ(
            candidate_model=resolved_model(selector=self._selector, pinned=self._model_pin),
            candidate_provider=self._selector,
            judge_model=resolved_model(selector=self._judge_selector, pinned=self._judge_model_pin),
            judge_provider=self._judge_selector,
        )
        await self._require_declared_agent(ctx, simulated_actor_id)

        resolved = await self._resolver.resolve(ctx, moment=self._clock.now(), query=question, **resolver_arguments)
        request = _response_request(
            resolved=resolved,
            max_output_tokens=self._max_output_tokens,
            model_id=resolved_model(selector=self._selector, pinned=self._model_pin),
            prompt=question,
            requested_at=self._clock.now(),
        )
        try:
            answer = await provider.respond(request)
        except ProviderError:
            # Re-raised unchanged. The resolution happened and its receipt is
            # written, so the record is complete even though the response to the
            # caller is not — which is ADR 0025's dissent, and the reason the
            # receipt write is not conditional on the generation succeeding.
            raise

        served = _served_ids(resolved.envelope)
        assertions = tuple(
            SimulatedAssertion(
                citations=tuple(
                    CitedItem(receipt_item_id=cited, was_served=cited in served)
                    for cited in dict.fromkeys(assertion.cited_receipt_item_ids)
                ),
                position=position,
                text=assertion.text,
            )
            for position, assertion in enumerate(answer.assertions)
        )
        usage = TokenReport(
            cached_prompt_tokens=answer.usage.cached_prompt_tokens,
            completion_tokens=answer.usage.completion_tokens,
            prompt_tokens=answer.usage.prompt_tokens,
            served_item_count=len(served),
            source=answer.usage.source,
        )
        simulation = Simulation(
            answer=answer.answer,
            assertions=assertions,
            created_at=self._clock.now(),
            duration_ms=answer.duration_ms,
            envelope_state=resolved.envelope.state,
            instruction_disposition=str(resolved.instruction_disposition),
            model_id=answer.model_id,
            prompt=question,
            provider_id=provider.provider_id,
            receipt_id=resolved.receipt_id,
            run_item_id=run_item_id,
            served_receipt_item_ids=served,
            simulated_actor_id=simulated_actor_id,
            simulation_id=uuid.uuid4(),
            usage=usage,
        )
        await self._store(ctx, simulation)
        return simulation

    # -- reading -----------------------------------------------------------

    async def get(self, ctx: TenantContext, simulation_id: uuid.UUID) -> Simulation:
        """One simulation, its assertions in order, and every citation on them."""
        async with self._session_factory() as session:
            header = (
                (
                    await session.execute(
                        text(
                            "SELECT simulation_id, receipt_id, simulated_actor_id, prompt, answer, "
                            "       provider_id, model_id, instruction_disposition, prompt_tokens, "
                            "       completion_tokens, cached_prompt_tokens, usage_source, "
                            "       served_item_count, duration_ms, created_at, run_item_id "
                            "  FROM evaluation_simulations "
                            " WHERE simulation_id = :sid AND tenant_id = :tid"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .first()
            )
            if header is None:
                raise NotFoundError(f"simulation {simulation_id} not found")

            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT a.assertion_id, a.position, a.text, c.receipt_item_id, c.was_served "
                            "  FROM evaluation_simulation_assertions a "
                            "  LEFT JOIN evaluation_simulation_citations c ON c.assertion_id = a.assertion_id "
                            " WHERE a.simulation_id = :sid AND a.tenant_id = :tid "
                            " ORDER BY a.position, c.receipt_item_id"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )

        by_position: dict[int, tuple[str, list[CitedItem]]] = {}
        for row in rows:
            position = int(row["position"])
            entry = by_position.setdefault(position, (row["text"], []))
            if row["receipt_item_id"] is not None:
                entry[1].append(
                    CitedItem(receipt_item_id=str(row["receipt_item_id"]), was_served=bool(row["was_served"]))
                )

        return Simulation(
            answer=header["answer"],
            assertions=tuple(
                SimulatedAssertion(citations=tuple(citations), position=position, text=body)
                for position, (body, citations) in sorted(by_position.items())
            ),
            created_at=header["created_at"],
            duration_ms=header["duration_ms"],
            # Not stored: the envelope's state lives on the receipt, which is the
            # record that owns it. Reported as empty here rather than duplicated,
            # so a reader joins rather than trusting a copy that cannot be
            # corrected when the receipt is.
            envelope_state="",
            instruction_disposition=header["instruction_disposition"],
            model_id=header["model_id"],
            prompt=header["prompt"],
            provider_id=header["provider_id"],
            receipt_id=header["receipt_id"],
            run_item_id=header["run_item_id"],
            simulated_actor_id=header["simulated_actor_id"],
            simulation_id=header["simulation_id"],
            usage=TokenReport(
                cached_prompt_tokens=header["cached_prompt_tokens"],
                completion_tokens=header["completion_tokens"],
                prompt_tokens=header["prompt_tokens"],
                served_item_count=int(header["served_item_count"]),
                source=header["usage_source"],
            ),
        )

    # -- guards ------------------------------------------------------------

    def _require_provider(self) -> ResponseProvider:
        if self._provider is None:
            msg = (
                "simulation is switched off: no response provider is configured. Set "
                "SIMULATION_PROVIDER and SIMULATION_API_KEY to enable it. Prompt sets, runs, "
                "verdicts and the deterministic criteria work without it."
            )
            raise SimulationUnavailable(msg)
        return self._provider

    async def _require_declared_agent(self, ctx: TenantContext, actor_id: uuid.UUID) -> None:
        """Refuse to simulate a principal nobody declared an agent.

        Read here rather than through `ActorDirectoryService` because that
        service sits in `service/governance` and this one in `context` — a
        dependency this direction would invert the layering. The predicate is
        one row and the tenant scoping is on it, which is the property that
        matters.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT actor_kind, declared_at FROM actors WHERE actor_id = :actor AND tenant_id = :tid"),
                    {"actor": actor_id, "tid": ctx.tenant_id},
                )
            ).first()
        if row is None:
            raise NotFoundError(f"no principal {actor_id} in this tenant")
        if row.declared_at is None or row.actor_kind != SIMULATABLE_KIND:
            declared = "nobody has declared what it is" if row.declared_at is None else f"it is a {row.actor_kind!r}"
            msg = (
                f"principal {actor_id} cannot be simulated: {declared}. An agent is declared, never "
                "inferred — a person in an IDE and an unattended agent arrive over the same "
                "transport — so declare it through POST /v1/admin/actors/{actor_id}/declare with "
                "actor_kind='agent' first."
            )
            raise ValidationError(msg)

    # -- writing -----------------------------------------------------------

    async def _store(self, ctx: TenantContext, simulation: Simulation) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO evaluation_simulations "
                    "(simulation_id, tenant_id, receipt_id, simulated_actor_id, prompt, run_item_id, "
                    " answer, provider_id, model_id, instruction_disposition, prompt_tokens, "
                    " completion_tokens, cached_prompt_tokens, usage_source, served_item_count, "
                    " duration_ms, created_by, created_at) "
                    "VALUES (:sid, :tid, :receipt, :actor, :prompt, :run_item, :answer, :provider, "
                    "        :model, :disposition, :prompt_tokens, :completion_tokens, :cached, "
                    "        :usage_source, :served, :duration, :by, :now)"
                ),
                {
                    "actor": simulation.simulated_actor_id,
                    "answer": simulation.answer,
                    "by": ctx.actor_id,
                    "cached": simulation.usage.cached_prompt_tokens,
                    "completion_tokens": simulation.usage.completion_tokens,
                    "disposition": simulation.instruction_disposition,
                    "duration": simulation.duration_ms,
                    "model": simulation.model_id,
                    "now": simulation.created_at,
                    "prompt": simulation.prompt,
                    "prompt_tokens": simulation.usage.prompt_tokens,
                    "provider": simulation.provider_id,
                    "receipt": simulation.receipt_id,
                    "run_item": simulation.run_item_id,
                    "served": simulation.usage.served_item_count,
                    "sid": simulation.simulation_id,
                    "tid": ctx.tenant_id,
                    "usage_source": simulation.usage.source,
                },
            )
            for assertion in simulation.assertions:
                assertion_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO evaluation_simulation_assertions "
                        "(assertion_id, simulation_id, tenant_id, position, text) "
                        "VALUES (:aid, :sid, :tid, :position, :text)"
                    ),
                    {
                        "aid": assertion_id,
                        "position": assertion.position,
                        "sid": simulation.simulation_id,
                        "text": assertion.text,
                        "tid": ctx.tenant_id,
                    },
                )
                for citation in assertion.citations:
                    await session.execute(
                        text(
                            "INSERT INTO evaluation_simulation_citations "
                            "(assertion_id, tenant_id, receipt_item_id, was_served) "
                            "VALUES (:aid, :tid, :item, :served)"
                        ),
                        {
                            "aid": assertion_id,
                            "item": citation.receipt_item_id,
                            "served": citation.was_served,
                            "tid": ctx.tenant_id,
                        },
                    )


def _served_ids(envelope: ContextEnvelopeV1) -> frozenset[str]:
    """Every receipt item id this envelope served, across all five blocks."""
    return frozenset(item.receipt_item_id.value() for block in envelope.blocks for item in block.items)


def _response_request(
    *,
    resolved: ResolvedContext,
    max_output_tokens: int,
    model_id: str,
    prompt: str,
    requested_at: datetime.datetime,
) -> ResponseRequest:
    """The envelope, as the model sees it.

    Every block travels, including empty and failed ones, each with its own
    state. The instruction deltas travel twice — once as block items, because
    that is what was served, and once as instructions, because that is what they
    are. Passing them only as items would put a correction addressed to the
    caller in the same position as material about the subject.
    """
    from contextplane.context.schemas.envelope import BLOCK_INSTRUCTIONS  # noqa: PLC0415

    blocks = tuple(
        ServedBlockView(
            items=tuple(
                ServedItemView(
                    block=block.name,
                    item_key=item.receipt_item_id.item_key,
                    payload_json=json.dumps(item.payload, default=str, sort_keys=True),
                    receipt_item_id=item.receipt_item_id.value(),
                )
                for item in block.items
            ),
            name=block.name,
            reason=block.reason,
            state=block.state,
        )
        for block in resolved.envelope.blocks
    )
    deltas = tuple(
        str(item.payload.get("body", ""))
        for block in resolved.envelope.blocks
        if block.name == BLOCK_INSTRUCTIONS
        for item in block.items
    )
    return ResponseRequest(
        blocks=blocks,
        instruction_disposition=str(resolved.instruction_disposition),
        instructions=deltas,
        max_output_tokens=max_output_tokens,
        model_id=model_id,
        prompt=prompt,
        requested_at=requested_at,
    )


__all__ = [
    "MAX_PROMPT_CHARS",
    "SIMULATABLE_KIND",
    "CitedItem",
    "Simulation",
    "SimulatedAssertion",
    "SimulationService",
    "TokenReport",
]
