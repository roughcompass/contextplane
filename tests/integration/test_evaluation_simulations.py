"""Simulation against a real database: the guards, the two records, the citations.

E24-T3, on ADR 0025 and ADR 0026. What is proved here is what a fake would agree
with whatever the code did:

- **a principal nobody declared an agent cannot be simulated**, and a declared
  human cannot either;
- **the resolution and the generation are two records**, with the simulation
  referencing the receipt rather than embedding it;
- **a citation naming an id that was never served is stored as declared**, which
  is what makes "the model cited something that was not in the envelope" an
  answerable question instead of a failed insert;
- **an assertion citing nothing keeps its row**, because that state is the whole
  finding;
- **usage is reported or explicitly unknown**, never a zero that reads as free.

The provider is a stand-in and the resolver is a stand-in; what is real is the
schema, its constraints, and the service's own arithmetic. The adapters are
proved against transports in `tests/unit/test_simulation_provider.py`, and the
resolver composition in `test_context_resolve_surfaces.py`.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.evaluation.simulation import MAX_PROMPT_CHARS, SimulationService
from contextplane.context.quality import derive_quality
from contextplane.context.schemas.envelope import (
    BLOCK_NAMES,
    BLOCK_WORKSPACE,
    ContextBlockV1,
    ContextEnvelopeV1,
    ContextItemV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import ReceiptItemIdV1, TrustMetadataV1
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.extraction.provider import TokenUsage
from contextplane.extraction.response_factory import JudgeFamilyRefused
from contextplane.extraction.response_provider import Assertion, ResponseResult, SimulationUnavailable
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)


def _envelope() -> ContextEnvelopeV1:
    item = ContextItemV1(
        payload={"goal": "drain the dead-letter queue", "intent_id": str(uuid.uuid4())},
        receipt_item_id=ReceiptItemIdV1(block=BLOCK_WORKSPACE, source="intent_checkpoint", item_key="c1"),
        trust=TrustMetadataV1(
            assertion_kind="annotation",
            attribution="agent-alpha",
            authority="workspace",
            classification="internal",
            freshness=_NOW,
            mutability="immutable",
            source="intent_checkpoint",
            trust="asserted",
        ),
    )
    blocks = tuple(
        ContextBlockV1(
            name=name,
            state="success" if name == BLOCK_WORKSPACE else "empty",
            items=(item,) if name == BLOCK_WORKSPACE else (),
        )
        for name in BLOCK_NAMES
    )
    return ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))


class _Resolver:
    """Answers with one workspace item and a receipt id, recording its arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.receipt_id = uuid.uuid4()

    async def resolve(self, ctx: TenantContext, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Resolved:
            envelope = _envelope()
            receipt_id = self.receipt_id
            instruction_disposition = "declared_known"
            arc_block_note = None
            instruction_block_note = None

        return _Resolved()


class _Provider:
    """A response provider that answers with whatever assertions it was given."""

    provider_id = "anthropic"
    default_model_id = "claude-test"

    def __init__(self, *, assertions: tuple[Assertion, ...], usage: TokenUsage | None = None) -> None:
        self._assertions = assertions
        self._usage = usage or TokenUsage(
            cached_prompt_tokens=5, completion_tokens=20, prompt_tokens=100, source="provider_reported"
        )
        self.requests: list[Any] = []

    async def respond(self, request: Any) -> ResponseResult:
        self.requests.append(request)
        return ResponseResult(
            answer="Drain it through the runbook.",
            assertions=self._assertions,
            duration_ms=42,
            model_id="claude-test",
            usage=self._usage,
        )


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    caller_id, agent_id, human_id, undeclared_id = (uuid.uuid4() for _ in range(4))
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'simulation')"),
            {"slug": f"sim-{tenant_id.hex[:8]}", "t": tenant_id},
        )
        for actor_id, kind, declared in (
            (caller_id, "human", True),
            (agent_id, "agent", True),
            (human_id, "human", True),
            (undeclared_id, "unknown", False),
        ):
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'Principal', :kind, :declared, :declarer, :now)"
                ),
                {
                    "a": actor_id,
                    "declarer": caller_id if declared else None,
                    "declared": _NOW if declared else None,
                    "kind": kind,
                    "now": _NOW,
                    "sub": f"s-{actor_id.hex[:8]}",
                    "t": tenant_id,
                },
            )
    try:
        yield {
            "agent_id": agent_id,
            "ctx": TenantContext(actor_id=caller_id, roles=("producer",), tenant_id=tenant_id),
            "factory": factory,
            "human_id": human_id,
            "tenant_id": tenant_id,
            "undeclared_id": undeclared_id,
        }
    finally:
        await engine.dispose()


def _service(
    world: dict[str, Any],
    *,
    provider: _Provider | None = None,
    resolver: _Resolver | None = None,
    judge_selector: str = "openai",
) -> SimulationService:
    return SimulationService(
        clock=FakeClock(_NOW),
        judge_model_pin="",
        judge_selector=judge_selector,
        max_output_tokens=512,
        model_pin="",
        provider=provider,  # type: ignore[arg-type]
        provider_selector="anthropic",
        resolver=resolver or _Resolver(),  # type: ignore[arg-type]
        session_factory=world["factory"],
    )


def _cited(*ids: str) -> tuple[Assertion, ...]:
    return (Assertion(cited_receipt_item_ids=tuple(ids), text="The queue drains via the runbook."),)


def _served_id() -> str:
    return ReceiptItemIdV1(block=BLOCK_WORKSPACE, source="intent_checkpoint", item_key="c1").value()


# --- the declared-agent guard -------------------------------------------------


@pytest.mark.asyncio
async def test_an_undeclared_principal_cannot_be_simulated(world: dict[str, Any]) -> None:
    """ADR 0019 assumption 2: an agent is declared, never inferred."""
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    with pytest.raises(ValidationError, match="nobody has declared"):
        await service.simulate(
            world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["undeclared_id"]
        )


@pytest.mark.asyncio
async def test_a_declared_human_cannot_be_simulated_either(world: dict[str, Any]) -> None:
    """Putting a machine's answer under a person's name is not what this does."""
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    with pytest.raises(ValidationError, match="'human'"):
        await service.simulate(world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["human_id"])


@pytest.mark.asyncio
async def test_a_principal_from_another_tenant_is_not_found(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    with pytest.raises(NotFoundError):
        await service.simulate(world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=uuid.uuid4())


# --- the two configuration refusals -------------------------------------------


@pytest.mark.asyncio
async def test_a_deployment_with_no_provider_says_which_setting_is_unset(world: dict[str, Any]) -> None:
    service = _service(world, provider=None)
    assert service.is_available is False
    with pytest.raises(SimulationUnavailable, match="SIMULATION_PROVIDER"):
        await service.simulate(world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"])


@pytest.mark.asyncio
async def test_a_same_family_judge_is_refused_before_the_principal_is_read(world: dict[str, Any]) -> None:
    """Ordered deliberately: a configuration error is not the operator's data problem."""
    service = _service(world, provider=_Provider(assertions=()), judge_selector="anthropic")
    with pytest.raises(JudgeFamilyRefused, match="JUDGE_PROVIDER"):
        await service.simulate(
            world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["undeclared_id"]
        )


# --- the two records ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_simulation_references_the_receipt_rather_than_copying_the_envelope(
    world: dict[str, Any],
) -> None:
    resolver = _Resolver()
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())), resolver=resolver)

    simulation = await service.simulate(
        world["ctx"], prompt="how do I drain it?", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )

    assert simulation.receipt_id == resolver.receipt_id
    async with world["factory"]() as session:
        stored = (
            await session.execute(
                text("SELECT receipt_id, answer, provider_id FROM evaluation_simulations WHERE simulation_id = :s"),
                {"s": simulation.simulation_id},
            )
        ).one()
    assert stored.receipt_id == resolver.receipt_id
    assert stored.provider_id == "anthropic"


@pytest.mark.asyncio
async def test_the_resolution_runs_on_the_callers_identity_not_the_simulated_principals(
    world: dict[str, Any],
) -> None:
    """Resolving under the simulated principal would be an escalation wearing a feature."""
    resolver = _Resolver()
    service = _service(world, provider=_Provider(assertions=()), resolver=resolver)

    await service.simulate(world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"])

    assert resolver.calls[0]["query"] == "q"
    assert "simulated_actor_id" not in resolver.calls[0]


# --- citations ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_citation_naming_something_never_served_is_stored_as_declared(world: dict[str, Any]) -> None:
    """A finding, not a failed insert."""
    service = _service(world, provider=_Provider(assertions=_cited("rid-that-was-never-served")))

    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )

    citation = simulation.assertions[0].citations[0]
    assert citation.receipt_item_id == "rid-that-was-never-served"
    assert citation.was_served is False


@pytest.mark.asyncio
async def test_a_citation_that_was_served_says_so(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    assert simulation.assertions[0].citations[0].was_served is True
    assert simulation.uncited_served_ids == ()


@pytest.mark.asyncio
async def test_an_assertion_citing_nothing_keeps_its_row(world: dict[str, Any]) -> None:
    """Dropping it would delete the finding."""
    service = _service(world, provider=_Provider(assertions=_cited()))

    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    read_back = await service.get(world["ctx"], simulation.simulation_id)

    assert len(read_back.assertions) == 1
    assert read_back.assertions[0].citations == ()


@pytest.mark.asyncio
async def test_a_served_item_no_assertion_cited_is_reported(world: dict[str, Any]) -> None:
    """One of E24-T13's observations, computed rather than diagnosed."""
    service = _service(world, provider=_Provider(assertions=_cited()))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    assert simulation.uncited_served_ids == (_served_id(),)


@pytest.mark.asyncio
async def test_a_repeated_citation_is_stored_once(world: dict[str, Any]) -> None:
    """The primary key would otherwise reject the whole write over a model's repetition."""
    served = _served_id()
    service = _service(world, provider=_Provider(assertions=_cited(served, served)))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    assert [c.receipt_item_id for c in simulation.assertions[0].citations] == [served]


# --- usage --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_is_read_back_exactly_with_the_cardinality_that_produced_it(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    read_back = await service.get(world["ctx"], simulation.simulation_id)

    assert read_back.usage.prompt_tokens == 100
    assert read_back.usage.completion_tokens == 20
    assert read_back.usage.source == "provider_reported"
    assert read_back.usage.served_item_count == 1


@pytest.mark.asyncio
async def test_unknown_usage_is_stored_as_unknown_rather_than_as_zero(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=_cited(_served_id()), usage=TokenUsage.unknown()))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    read_back = await service.get(world["ctx"], simulation.simulation_id)

    assert read_back.usage.source == "unknown"
    assert read_back.usage.prompt_tokens is None


# --- reading and bounds -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_simulation_from_another_tenant_is_not_found(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=_cited(_served_id())))
    simulation = await service.simulate(
        world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"]
    )
    elsewhere = TenantContext(actor_id=world["ctx"].actor_id, roles=("producer",), tenant_id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await service.get(elsewhere, simulation.simulation_id)


@pytest.mark.asyncio
async def test_an_empty_prompt_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=()))
    with pytest.raises(ValidationError, match="1 to"):
        await service.simulate(world["ctx"], prompt="   ", resolver_arguments={}, simulated_actor_id=world["agent_id"])


@pytest.mark.asyncio
async def test_a_prompt_past_the_bound_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_Provider(assertions=()))
    with pytest.raises(ValidationError, match="1 to"):
        await service.simulate(
            world["ctx"],
            prompt="x" * (MAX_PROMPT_CHARS + 1),
            resolver_arguments={},
            simulated_actor_id=world["agent_id"],
        )


@pytest.mark.asyncio
async def test_the_model_sees_every_block_including_the_empty_ones(world: dict[str, Any]) -> None:
    """A model told a block is empty may say the material does not exist."""
    provider = _Provider(assertions=())
    service = _service(world, provider=provider)

    await service.simulate(world["ctx"], prompt="q", resolver_arguments={}, simulated_actor_id=world["agent_id"])

    request = provider.requests[0]
    assert [block.name for block in request.blocks] == list(BLOCK_NAMES)
    assert request.instruction_disposition == "declared_known"
