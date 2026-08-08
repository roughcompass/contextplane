"""Resume against real data: the bounds hold, and the answer is stable.

Determinism is the property this phase exists to establish, and it is not
provable in a unit test: it is a claim about what two database reads return, in
what order, over rows a previous run inserted.

The stability test is the one that matters most. An agent resuming twice with no
work in between must get the same answer both times -- otherwise a caller
diffing two resumes to see what moved sees churn no work caused, and stops
trusting the diff. A later checkpoint must change the answer, because that is
the only thing that should.
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

from contextplane.context.resume import ContextResumeService, ResumeRequest
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_REF = ("github", "acme/app", "pull_request", "42")

type _Wired = dict[str, Any]


@pytest_asyncio.fixture
async def wired(pg_container: str) -> AsyncIterator[_Wired]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
        task_id, reference_id = uuid.uuid4(), uuid.uuid4()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": tenant_id, "s": f"rz-{tenant_id.hex[:8]}", "now": _NOW},
            )
            # The actor participates, so the head read is authorized. Resume
            # reads through the same predicate every other task read uses.
            await session.execute(
                text(
                    "INSERT INTO task_participant_grants "
                    "(tenant_id, task_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:t, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"t": tenant_id, "task": task_id, "actor": str(actor_id), "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO context_external_references "
                    "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                    " classification, external_authority, collision_key) "
                    "VALUES (:rid, :t, :sys, :ns, :kind, :eid, 'internal', 'github', :ckey)"
                ),
                {
                    "rid": reference_id,
                    "t": tenant_id,
                    "sys": _REF[0],
                    "ns": _REF[1],
                    "kind": _REF[2],
                    "eid": _REF[3],
                    "ckey": "|".join(_REF),
                },
            )

        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "task_id": task_id,
            "reference_id": reference_id,
            "service": ContextResumeService(session_factory=factory, clock=FakeClock(_NOW)),
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        }
    finally:
        await engine.dispose()


async def _checkpoint(wired: _Wired, *, sequence: int, goal: str, next_action: str | None = None) -> uuid.UUID:
    """Append one checkpoint, keeping the chain intact.

    The predecessor is threaded rather than left NULL: the chain constraint
    refuses a checkpoint past the first that names no parent, which is what
    stops a step being written into the middle of somebody else's history.
    """
    checkpoint_id = uuid.uuid4()
    predecessor = wired.get("last_checkpoint_id")
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO task_checkpoints "
                "(checkpoint_id, tenant_id, task_id, sequence, predecessor_id, goal, decisions, assumptions, "
                " evidence, completed_checks, open_questions, next_action, author, recorded_at, retention_policy, "
                " digest) "
                "VALUES (:cid, :t, :task, :seq, :pred, :goal, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                " :oq, :next, 'agent-a', :at, 'standard', :digest)"
            ),
            {
                "cid": checkpoint_id,
                "t": wired["tenant_id"],
                "task": wired["task_id"],
                "seq": sequence,
                "pred": predecessor,
                "goal": goal,
                "oq": f'["q{sequence}"]',
                "next": next_action,
                "at": _NOW + datetime.timedelta(minutes=sequence),
                "digest": f"digest-{sequence}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_heads (tenant_id, task_id, head_checkpoint_id, head_sequence, summary, updated_at) "
                "VALUES (:t, :task, :cid, :seq, :summary, :at) "
                "ON CONFLICT (tenant_id, task_id) DO UPDATE SET "
                "  head_checkpoint_id = EXCLUDED.head_checkpoint_id, "
                "  head_sequence = EXCLUDED.head_sequence, "
                "  summary = EXCLUDED.summary, "
                "  updated_at = EXCLUDED.updated_at"
            ),
            {
                "t": wired["tenant_id"],
                "task": wired["task_id"],
                "cid": checkpoint_id,
                "seq": sequence,
                "summary": goal,
                "at": _NOW + datetime.timedelta(minutes=sequence),
            },
        )
        # A reference is evidence a checkpoint cited, so the binding hangs off
        # the checkpoint. Resume reaches the task through it -- the junction has
        # no `task` subject type, and that is the shape the schema intends.
        await session.execute(
            text(
                "INSERT INTO context_reference_bindings "
                "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                "VALUES (:bid, :t, :rid, 'task_checkpoint', :cid, :now)"
            ),
            {
                "bid": uuid.uuid4(),
                "t": wired["tenant_id"],
                "rid": wired["reference_id"],
                "cid": checkpoint_id,
                "now": _NOW,
            },
        )
    wired["last_checkpoint_id"] = checkpoint_id
    return checkpoint_id


def _request(**overrides: Any) -> ResumeRequest:
    return ResumeRequest(references=(_REF,), **overrides)


# --- Determinism ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_resumes_with_an_unchanged_head_are_identical(wired: _Wired) -> None:
    """The property the phase exists to establish. A caller diffing two resumes
    to see what moved must see only what moved."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second", next_action="carry on")

    first = await wired["service"].resume(wired["ctx"], _request())
    second = await wired["service"].resume(wired["ctx"], _request())

    assert first.head_checkpoint_id == second.head_checkpoint_id
    assert [c.checkpoint_id for c in first.checkpoints] == [c.checkpoint_id for c in second.checkpoints]
    assert first.open_questions == second.open_questions
    assert first.next_action == second.next_action


@pytest.mark.asyncio
async def test_checkpoints_come_back_oldest_first(wired: _Wired) -> None:
    """Read newest-first so the bound keeps the recent end, then reversed so a
    reader gets them in the order they happened."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second")
    await _checkpoint(wired, sequence=3, goal="third")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert [c.sequence for c in state.checkpoints] == [1, 2, 3]


# --- Stability after later work ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_checkpoint_moves_the_head_and_the_answer(wired: _Wired) -> None:
    """The only thing that should change the answer. Without this the
    determinism test above could be passing because resume returns nothing."""
    await _checkpoint(wired, sequence=1, goal="first", next_action="do the first thing")
    before = await wired["service"].resume(wired["ctx"], _request())

    await _checkpoint(wired, sequence=2, goal="second", next_action="do the second thing")
    after = await wired["service"].resume(wired["ctx"], _request())

    assert before.head_sequence == 1
    assert after.head_sequence == 2
    assert before.next_action == "do the first thing"
    assert after.next_action == "do the second thing"


@pytest.mark.asyncio
async def test_open_questions_come_from_the_newest_checkpoint_not_the_union(wired: _Wired) -> None:
    """A question closed three checkpoints ago is not open. Unioning the window
    would resurrect it, and a resumed agent would go and answer it again."""
    await _checkpoint(wired, sequence=1, goal="first")
    await _checkpoint(wired, sequence=2, goal="second")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert state.open_questions == ("q2",)


# --- Bounds ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_checkpoint_bound_keeps_the_recent_end(wired: _Wired) -> None:
    """Bounding from the old end would return the beginning of a long task and
    call it resume."""
    for sequence in range(1, 8):
        await _checkpoint(wired, sequence=sequence, goal=f"step {sequence}")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=3))

    assert [c.sequence for c in state.checkpoints] == [5, 6, 7]


@pytest.mark.asyncio
async def test_hitting_a_bound_is_reported_rather_than_silent(wired: _Wired) -> None:
    """A resume that quietly returned three of seven would read as the whole
    story, and the caller would carry on from a middle it believed was the
    start."""
    for sequence in range(1, 8):
        await _checkpoint(wired, sequence=sequence, goal=f"step {sequence}")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=3))

    assert "checkpoints" in state.truncated


@pytest.mark.asyncio
async def test_a_resume_inside_its_bounds_reports_no_truncation(wired: _Wired) -> None:
    """The other half. Without it `truncated` could be set unconditionally."""
    await _checkpoint(wired, sequence=1, goal="only one")

    state = await wired["service"].resume(wired["ctx"], _request(checkpoint_bound=5))

    assert state.truncated == ()


# --- Authorization -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_participant_gets_no_checkpoints(wired: _Wired) -> None:
    """Resume reads the head through the same audience predicate as every other
    task read, so a stranger holding the pull-request id learns nothing from it."""
    await _checkpoint(wired, sequence=1, goal="private work")
    stranger = TenantContext(tenant_id=wired["tenant_id"], actor_id=uuid.uuid4(), roles=["producer"])

    state = await wired["service"].resume(stranger, _request())

    assert state.checkpoints == ()
    assert state.head_checkpoint_id is None


@pytest.mark.asyncio
async def test_another_tenant_sees_nothing_at_all(wired: _Wired) -> None:
    """The reference itself is tenant-scoped, so a foreign caller does not even
    resolve the work -- it gets the empty answer, not a filtered one."""
    outsider = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=["producer"])

    state = await wired["service"].resume(outsider, _request())

    assert state.is_empty()


@pytest.mark.asyncio
async def test_an_unknown_reference_resumes_empty_rather_than_failing(wired: _Wired) -> None:
    """ "Start fresh" is a legitimate answer and must not arrive as an error --
    a pipeline resuming a run that has no history yet is the common case."""
    state = await wired["service"].resume(
        wired["ctx"], ResumeRequest(references=(("github", "acme/app", "pull_request", "99999"),))
    )

    assert state.is_empty()
    assert state.checkpoints == ()


# --- Never a transcript --------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_returns_conclusions_and_never_an_exchange(wired: _Wired) -> None:
    """The checkpoint chain exists so an agent records what it concluded rather
    than everything it said. Handing back the raw exchange would make the
    summary decorative."""
    await _checkpoint(wired, sequence=1, goal="decided to use the kit", next_action="wire it up")

    state = await wired["service"].resume(wired["ctx"], _request())

    assert state.head_summary == "decided to use the kit"
    assert state.next_action == "wire it up"
    assert not hasattr(state, "transcript")
    assert not hasattr(state, "messages")
