"""The deterministic three, computed for a recorded simulation or honestly refused.

E24-T4a, against a real database. Two properties carry the weight:

- **it refuses rather than returning zeros.** An interactive simulation has no
  declared expectations, and scoring it against expectations typed afterwards
  would measure whatever the system returned. Zeros would render as three failed
  criteria and ones as three passes nobody checked.
- **it scores the material the simulation recorded**, not a re-resolution.
  Re-resolving would grade a different envelope than the answer came from.

The two refusals are distinguished because they have different remedies: a
prompt that declared nothing can declare something, and a simulation that belongs
to no prompt cannot.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.evaluation.envelope_judge import (
    ENVELOPE_JUDGE_VERSION,
    UNCHECKED_NO_CLASSIFICATION,
    UNCHECKED_NO_TRUST_RECORD,
    VIOLATION_AUDIENCE,
    VIOLATION_CLASSIFICATION,
)
from contextplane.context.evaluation.scoring import (
    UNASSERTABLE_NO_EXPECTATIONS,
    UNASSERTABLE_NO_PROMPT,
    SimulationScoringService,
)
from contextplane.context.schemas.envelope import BLOCK_NAMES
from contextplane.exceptions import NotFoundError
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)
_TASK = "22222222-2222-5222-8222-222222222222"
_ELSEWHERE = "33333333-3333-5333-8333-333333333333"


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, agent_id = (uuid.uuid4() for _ in range(3))

    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'scoring')"),
            {"slug": f"score-{tenant_id.hex[:8]}", "t": tenant_id},
        )
        for principal, kind in ((actor_id, "human"), (agent_id, "agent")):
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'P', :kind, :now, :a, :now)"
                ),
                {"a": principal, "kind": kind, "now": _NOW, "sub": f"s-{principal.hex[:8]}", "t": tenant_id},
            )
    try:
        yield {
            "actor_id": actor_id,
            "agent_id": agent_id,
            "ctx": TenantContext(actor_id=actor_id, roles=("producer",), tenant_id=tenant_id),
            "factory": factory,
            "tenant_id": tenant_id,
        }
    finally:
        await engine.dispose()


async def _simulation(
    world: dict[str, Any],
    *,
    expectations: dict[str, Any] | None = None,
    with_prompt: bool = True,
    served: tuple[tuple[str, str, dict[str, Any]], ...] = (),
) -> uuid.UUID:
    """One recorded simulation, optionally hung off a prompt with expectations."""
    simulation_id = uuid.uuid4()
    run_item_id: uuid.UUID | None = None

    async with world["factory"]() as session, session.begin():
        if with_prompt:
            set_id, prompt_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            run_item_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO evaluation_prompt_sets (set_id, tenant_id, name, created_by, created_at) "
                    "VALUES (:sid, :tid, :name, :a, :now)"
                ),
                {
                    "a": world["actor_id"],
                    "name": f"set-{set_id.hex[:8]}",
                    "now": _NOW,
                    "sid": set_id,
                    "tid": world["tenant_id"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO evaluation_prompts "
                    "(prompt_id, set_id, tenant_id, position, request, expectations, added_at) "
                    "VALUES (:pid, :sid, :tid, 0, CAST(:req AS JSONB), CAST(:exp AS JSONB), :now)"
                ),
                {
                    "exp": None if expectations is None else json.dumps(expectations),
                    "now": _NOW,
                    "pid": prompt_id,
                    "req": json.dumps({"limit": 25, "query": "q"}),
                    "sid": set_id,
                    "tid": world["tenant_id"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO evaluation_runs "
                    "(run_id, set_id, tenant_id, resolver_fingerprint, prompt_count, started_by, started_at) "
                    "VALUES (:rid, :sid, :tid, :fp, 1, :a, :now)"
                ),
                {
                    "a": world["actor_id"],
                    "fp": f"sha256:{'a' * 64}",
                    "now": _NOW,
                    "rid": run_id,
                    "sid": set_id,
                    "tid": world["tenant_id"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO evaluation_run_items "
                    "(item_id, run_id, tenant_id, prompt_id, receipt_id, envelope_state, duration_ms) "
                    "VALUES (:iid, :rid, :tid, :pid, :receipt, 'complete', 10)"
                ),
                {
                    "iid": run_item_id,
                    "pid": prompt_id,
                    "receipt": uuid.uuid4(),
                    "rid": run_id,
                    "tid": world["tenant_id"],
                },
            )

        await session.execute(
            text(
                "INSERT INTO evaluation_simulations "
                "(simulation_id, tenant_id, receipt_id, simulated_actor_id, prompt, answer, provider_id, "
                " model_id, instruction_disposition, usage_source, served_item_count, run_item_id, "
                " created_by, created_at) "
                "VALUES (:sid, :tid, :rid, :agent, 'q', 'a', 'anthropic', 'claude', 'declared_known', "
                "        'unknown', :count, :item, :a, :now)"
            ),
            {
                "a": world["actor_id"],
                "agent": world["agent_id"],
                "count": len(served),
                "item": run_item_id,
                "now": _NOW,
                "rid": uuid.uuid4(),
                "sid": simulation_id,
                "tid": world["tenant_id"],
            },
        )
        for receipt_item_id, block, payload in served:
            await session.execute(
                text(
                    "INSERT INTO evaluation_simulation_served_items "
                    "(simulation_id, tenant_id, receipt_item_id, block, item_key, payload) "
                    "VALUES (:sid, :tid, :item, :block, :key, CAST(:payload AS JSONB))"
                ),
                {
                    "block": block,
                    "item": receipt_item_id,
                    "key": receipt_item_id,
                    "payload": json.dumps(payload),
                    "sid": simulation_id,
                    "tid": world["tenant_id"],
                },
            )
    return simulation_id


def _service(world: dict[str, Any]) -> SimulationScoringService:
    return SimulationScoringService(world["factory"])


# --- the two refusals ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interactive_simulation_is_unassertable_rather_than_zero(world: dict[str, Any]) -> None:
    """Zeros would render as three failed criteria nobody checked."""
    simulation_id = await _simulation(world, with_prompt=False)

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.is_assertable is False
    assert result.score is None
    assert result.unassertable == UNASSERTABLE_NO_PROMPT
    assert result.rubric_version == ENVELOPE_JUDGE_VERSION


@pytest.mark.asyncio
async def test_a_prompt_that_declared_nothing_is_a_distinct_refusal(world: dict[str, Any]) -> None:
    """Fixable by declaring expectations; the other refusal is not fixable at all."""
    simulation_id = await _simulation(world, expectations=None)

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.unassertable == UNASSERTABLE_NO_EXPECTATIONS
    assert result.unassertable != UNASSERTABLE_NO_PROMPT
    assert result.prompt_id is not None


@pytest.mark.asyncio
async def test_a_simulation_from_another_tenant_is_not_found(world: dict[str, Any]) -> None:
    simulation_id = await _simulation(world, with_prompt=False)
    elsewhere = TenantContext(actor_id=world["actor_id"], roles=("producer",), tenant_id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await _service(world).score_simulation(elsewhere, simulation_id)


# --- the arithmetic -----------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_counts_a_required_fact_in_any_block(world: dict[str, Any]) -> None:
    simulation_id = await _simulation(
        world,
        expectations={"required_item_keys": ["c1", "e1", "missing"]},
        served=(("c1", "workspace", {"intent_id": _TASK}), ("e1", "canonical", {"entity_id": "e1"})),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    assert (result.score.required_found, result.score.required_total) == (2, 3)
    assert result.score.recall == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_precision_denominates_over_every_served_item(world: dict[str, Any]) -> None:
    simulation_id = await _simulation(
        world,
        expectations={"relevant_item_keys": ["c1"]},
        served=(
            ("c1", "workspace", {"intent_id": _TASK}),
            ("c2", "workspace", {"intent_id": _TASK}),
        ),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    assert result.score.precision == 0.5
    assert result.score.served_total == 2


@pytest.mark.asyncio
async def test_every_block_appears_in_the_breakdown_including_the_empty_ones(world: dict[str, Any]) -> None:
    """The arm that served nothing is the one most likely to be why recall moved."""
    simulation_id = await _simulation(
        world,
        expectations={"required_item_keys": ["c1"]},
        served=(("c1", "workspace", {"intent_id": _TASK}),),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    assert [tally.block for tally in result.score.blocks] == list(BLOCK_NAMES)
    assert [tally.state for tally in result.score.blocks if tally.block == "arc"] == ["empty"]


@pytest.mark.asyncio
async def test_an_audience_violation_is_recorded_against_the_block_that_served_it(
    world: dict[str, Any],
) -> None:
    simulation_id = await _simulation(
        world,
        expectations={"permitted_task_ids": [_TASK]},
        served=(("c1", "workspace", {"intent_id": _ELSEWHERE}),),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    assert [(v.kind, v.block) for v in result.score.violations] == [(VIOLATION_AUDIENCE, "workspace")]
    assert result.score.is_safe is False


@pytest.mark.asyncio
async def test_the_tenant_check_passes_on_the_resolutions_own_tenant(world: dict[str, Any]) -> None:
    """The fires-on-everything defect, asserted in the direction it was fixed."""
    simulation_id = await _simulation(
        world,
        expectations={"permitted_tenant_ids": [str(world["tenant_id"])]},
        served=(("c1", "workspace", {"intent_id": _TASK}),),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    assert result.score.violations == ()


@pytest.mark.asyncio
async def test_classification_is_reported_unchecked_because_recorded_items_carry_no_trust(
    world: dict[str, Any],
) -> None:
    """Recorded material has lost its trust record, and that is said rather than guessed."""
    simulation_id = await _simulation(
        world,
        expectations={"max_classification": "internal", "permitted_task_ids": [_TASK]},
        served=(
            ("c1", "workspace", {"intent_id": _TASK}),
            ("e1", "canonical", {"entity_id": "e1"}),
        ),
    )

    result = await _service(world).score_simulation(world["ctx"], simulation_id)

    assert result.score is not None
    reasons = {entry.reason for entry in result.score.unchecked if entry.dimension == VIOLATION_CLASSIFICATION}
    # Two reasons, not one: canonical carries no trust record by construction and
    # the workspace item lost one, and a reader needs to know which.
    assert reasons == {UNCHECKED_NO_CLASSIFICATION, UNCHECKED_NO_TRUST_RECORD}
    assert result.score.violations == ()
