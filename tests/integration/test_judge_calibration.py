"""A judge's confidence becomes a number that predicts, or says it does not yet.

E24-T6, on ADR 0026 part 3, against a real database. The properties asserted here
are the ones that would still "work" if they were quietly removed:

- **no fit means `None`, never identity.** Identity asserts that a judge
  reporting 0.9 is right nine times in ten, which nobody has checked.
- **below the evidence bound nothing is stored**, because a row that cannot be
  evaluated against the target is not a fit.
- **a fit that misses its bound is stored and never selected**, because deleting
  it would leave "why are we still uncalibrated" with no answer.
- **`unsure` reviews are excluded**, because they are information about the
  reviewer and counting them either way would bias the fit.
- **a changed rubric or judge matches no row**, so scoring reverts to
  uncalibrated with nobody having to act.
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

from contextplane.context.evaluation.judge_calibration import (
    MIN_ADJUDICATED_FOR_MAPPING,
    UNCALIBRATED,
    JudgeCalibrationService,
    PinnedTuple,
)
from contextplane.extraction.judge_prompt import CRITERION_GROUNDEDNESS, CRITERION_RELEVANCE
from contextplane.service.memory.calibration import MAX_CALIBRATION_ERROR, STATUS_ACTIVE, STATUS_FAILED
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)
_RUBRIC = "agent-response-judge v1.0.0"


def _tuple(model: str | None = None, *, template: str | None = None, rubric: str | None = None) -> PinnedTuple:
    """A pinned tuple unique to one test unless it is deliberately reused.

    `evaluation_judge_calibration` has no tenant column and is read
    deployment-wide, which is correct — what is being calibrated is a property of
    the model rather than of a tenant. That makes a fixed judge id shared state
    between tests, so each test mints its own.
    """
    return PinnedTuple(
        judge_model_id=model or f"judge-{uuid.uuid4().hex[:12]}",
        prompt_template_hash=template or ("a" * 64),
        rubric_version=rubric or _RUBRIC,
    )


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, agent_id = (uuid.uuid4() for _ in range(3))
    simulation_id = uuid.uuid4()

    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'calib')"),
            {"slug": f"cal-{tenant_id.hex[:8]}", "t": tenant_id},
        )
        for principal, kind in ((actor_id, "human"), (agent_id, "agent")):
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'Principal', :kind, :now, :a, :now)"
                ),
                {"a": principal, "kind": kind, "now": _NOW, "sub": f"s-{principal.hex[:8]}", "t": tenant_id},
            )
    try:
        yield {
            "actor_id": actor_id,
            "agent_id": agent_id,
            "ctx": TenantContext(actor_id=actor_id, roles=("producer",), tenant_id=tenant_id),
            "factory": factory,
            "pinned": _tuple(),
            "simulation_id": simulation_id,
            "tenant_id": tenant_id,
        }
    finally:
        await engine.dispose()


async def _seed(
    world: dict[str, Any],
    *,
    confidence: float,
    review: str | None,
    count: int,
    pinned: PinnedTuple | None = None,
) -> None:
    """`count` judged criteria at one confidence, each with the given review.

    One simulation per seeded criterion, because a judged row is unique per
    `(simulation, criterion, panel_position)` — which is the constraint that
    makes a re-judge replace rather than accumulate, and which two batches
    sharing a simulation would collide on.
    """
    key = pinned or world["pinned"]
    async with world["factory"]() as session, session.begin():
        for index in range(count):
            judgement_id = uuid.uuid4()
            reviewer = uuid.uuid4()
            simulation_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO evaluation_simulations "
                    "(simulation_id, tenant_id, receipt_id, simulated_actor_id, prompt, answer, "
                    " provider_id, model_id, instruction_disposition, usage_source, served_item_count, "
                    " created_by, created_at) "
                    "VALUES (:sid, :tid, :rid, :agent, 'q', 'a', 'anthropic', 'claude', "
                    "        'declared_known', 'unknown', 0, :a, :now)"
                ),
                {
                    "a": world["actor_id"],
                    "agent": world["agent_id"],
                    "now": _NOW,
                    "rid": uuid.uuid4(),
                    "sid": simulation_id,
                    "tid": world["tenant_id"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'R', 'human', :now, :a, :now)"
                ),
                {"a": reviewer, "now": _NOW, "sub": f"r-{reviewer.hex[:10]}", "t": world["tenant_id"]},
            )
            await session.execute(
                text(
                    "INSERT INTO evaluation_judgements "
                    "(judgement_id, simulation_id, tenant_id, criterion, verdict, reasoning, evidence, "
                    " confidence, judge_model_id, judge_provider_id, rubric_version, prompt_template_hash, "
                    " panel_position, created_at) "
                    "VALUES (:jid, :sid, :tid, :criterion, 'pass', 'because', CAST(:ev AS JSONB), "
                    "        :confidence, :model, 'openai', :rubric, :template, :position, :now)"
                ),
                {
                    "confidence": confidence,
                    "criterion": CRITERION_GROUNDEDNESS if index % 2 == 0 else CRITERION_RELEVANCE,
                    "ev": json.dumps(["a span"]),
                    "jid": judgement_id,
                    "model": key.judge_model_id,
                    "now": _NOW,
                    "position": 0,
                    "rubric": key.rubric_version,
                    "sid": simulation_id,
                    "template": key.prompt_template_hash,
                    "tid": world["tenant_id"],
                },
            )
            if review is not None:
                await session.execute(
                    text(
                        "INSERT INTO evaluation_judgement_reviews "
                        "(judgement_id, tenant_id, verdict, note, reviewed_by, reviewed_at) "
                        "VALUES (:jid, :tid, :verdict, :note, :by, :now)"
                    ),
                    {
                        "by": reviewer,
                        "jid": judgement_id,
                        "note": None if review == "confirmed" else "because",
                        "now": _NOW,
                        "tid": world["tenant_id"],
                        "verdict": review,
                    },
                )


def _service(world: dict[str, Any]) -> JudgeCalibrationService:
    return JudgeCalibrationService(world["factory"], clock=FakeClock(_NOW))


# --- no fit means none --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unfitted_tuple_has_no_mapping_rather_than_an_identity_one(world: dict[str, Any]) -> None:
    """Identity asserts something nobody has checked."""
    assert await _service(world).active_fit(world["pinned"]) is None
    mine = (world["pinned"].judge_model_id, world["pinned"].rubric_version, world["pinned"].prompt_template_hash)
    assert mine not in await _service(world).calibrated_tuples()


@pytest.mark.asyncio
async def test_below_the_evidence_bound_nothing_is_stored(world: dict[str, Any]) -> None:
    """A row that cannot be evaluated against the target is not a fit."""
    await _seed(world, confidence=0.9, count=10, review="confirmed")
    state = await _service(world).refit(world["pinned"])

    assert state.is_calibrated is False
    assert state.version == UNCALIBRATED
    assert state.n_adjudicated == 10
    assert not [s for s in await _service(world).states(world["ctx"]) if s.pinned == world["pinned"]]


# --- fitting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_well_calibrated_judge_gets_an_active_fit(world: dict[str, Any]) -> None:
    """Confidence 0.9 and confirmed 0.9 of the time is what a fit is for."""
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    state = await _service(world).refit(world["pinned"])

    assert state.is_calibrated is True
    assert state.status == STATUS_ACTIVE
    assert state.measured_error <= MAX_CALIBRATION_ERROR
    assert await _service(world).active_fit(world["pinned"]) is not None
    assert world["pinned"].judge_model_id in state.version


@pytest.mark.asyncio
async def test_a_fit_that_misses_its_bound_is_stored_and_never_selected(world: dict[str, Any]) -> None:
    """A mapping worse than the bound carries a version string that reads as calibrated.

    The pattern that misses it is a judge whose confidence is *spread* and whose
    correctness does not track it: ten bins of twenty, alternating all-confirmed
    and all-overruled. A two-bin disagreement cannot miss the bound and that is
    correct rather than a gap — the error is weighted by how many observations
    landed in each bin, so one deviant bin among two large ones is a small effect
    and a judge that is right at high confidence and wrong at low confidence is
    exactly what calibration is for.
    """
    for index in range(10):
        confidence = index / 10 + 0.05
        await _seed(world, confidence=confidence, count=20, review="confirmed" if index % 2 == 0 else "overruled")

    state = await _service(world).refit(world["pinned"])

    assert state.measured_error > MAX_CALIBRATION_ERROR
    assert state.status == STATUS_FAILED
    assert state.is_calibrated is False
    assert await _service(world).active_fit(world["pinned"]) is None
    # Stored, so "why are we still uncalibrated" has an answer.
    mine = [s for s in await _service(world).states(world["ctx"]) if s.pinned == world["pinned"]]
    assert [entry.status for entry in mine] == [STATUS_FAILED]


@pytest.mark.asyncio
async def test_an_unsure_review_is_excluded_from_the_fit(world: dict[str, Any]) -> None:
    """It says something about the reviewer; counting it either way would bias the fit."""
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="unsure")
    observations = await _service(world).load_observations(world["pinned"])
    assert observations == []


@pytest.mark.asyncio
async def test_an_unreviewed_judgement_contributes_nothing(world: dict[str, Any]) -> None:
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review=None)
    assert await _service(world).load_observations(world["pinned"]) == []


@pytest.mark.asyncio
async def test_an_overruled_review_is_counted_as_the_judge_being_wrong(world: dict[str, Any]) -> None:
    await _seed(world, confidence=0.9, count=4, review="overruled")
    observations = await _service(world).load_observations(world["pinned"])
    assert len(observations) == 4
    assert all(not entry.was_correct for entry in observations)


# --- the separation key -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_rubric_matches_no_fit_and_reverts_to_uncalibrated(world: dict[str, Any]) -> None:
    """A rubric edit is a new population, for the reason a scorer change is one."""
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    await _service(world).refit(world["pinned"])

    edited = _tuple(world["pinned"].judge_model_id, rubric="agent-response-judge v1.1.0")
    assert await _service(world).active_fit(edited) is None


@pytest.mark.asyncio
async def test_a_changed_judge_model_matches_no_fit(world: dict[str, Any]) -> None:
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    await _service(world).refit(world["pinned"])

    swapped = _tuple()
    assert await _service(world).active_fit(swapped) is None


@pytest.mark.asyncio
async def test_a_changed_prompt_template_matches_no_fit(world: dict[str, Any]) -> None:
    """Position, verbosity and format bias live in the template."""
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    await _service(world).refit(world["pinned"])

    edited = _tuple(world["pinned"].judge_model_id, template="b" * 64)
    assert await _service(world).active_fit(edited) is None


@pytest.mark.asyncio
async def test_observations_from_another_tuple_do_not_pool(world: dict[str, Any]) -> None:
    other = _tuple()
    await _seed(world, confidence=0.9, count=6, review="confirmed")
    await _seed(world, confidence=0.1, count=6, pinned=other, review="overruled")

    assert len(await _service(world).load_observations(world["pinned"])) == 6
    assert len(await _service(world).load_observations(other)) == 6


# --- superseding --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_active_fit_supersedes_rather_than_deletes(world: dict[str, Any]) -> None:
    """A verdict scored under the old one names it, and that name has to keep resolving."""
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    first = await _service(world).refit(world["pinned"])
    await _seed(world, confidence=0.9, count=20, review="confirmed")
    second = await _service(world).refit(world["pinned"])

    assert first.version != second.version
    async with world["factory"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT version, status FROM evaluation_judge_calibration "
                    " WHERE judge_model_id = :model ORDER BY n_adjudicated"
                ),
                {"model": world["pinned"].judge_model_id},
            )
        ).all()
    assert [row.status for row in rows] == ["superseded", "active"]


@pytest.mark.asyncio
async def test_only_the_most_recent_attempt_per_tuple_is_reported(world: dict[str, Any]) -> None:
    await _seed(world, confidence=0.9, count=MIN_ADJUDICATED_FOR_MAPPING, review="confirmed")
    await _service(world).refit(world["pinned"])
    await _seed(world, confidence=0.9, count=20, review="confirmed")
    await _service(world).refit(world["pinned"])

    states = [s for s in await _service(world).states(world["ctx"]) if s.pinned == world["pinned"]]
    assert len(states) == 1
    assert states[0].is_calibrated is True
