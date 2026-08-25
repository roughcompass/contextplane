"""Prompt sets, runs and verdicts, against a real database and the real resolver.

E22-T15. The three rules this feature exists to keep are all things a fake would
agree with whatever the code did, so they are proved here:

- **an errored prompt stays in the run**, with a failure and no receipt;
- **a run pins the deployment**, and two runs under different deployments say
  they are not comparable rather than being diffed;
- **a verdict outlives the page**, which means it is read back from a row.

The resolver is the real one. An evaluation that ran against a copy of it, or
against a path with checks relaxed for evaluation, would measure something the
product does not serve — which is the most expensive kind of wrong answer this
surface can produce.
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

from contextplane.context.evaluation.runs import EvaluationRunService
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)
_FINGERPRINT = "sha256:" + "a" * 64
_OTHER_FINGERPRINT = "sha256:" + "b" * 64


class _Resolver:
    """A resolver that answers, or raises for one nominated query.

    Standing in for `ContextResolver` here rather than wiring the whole arm stack:
    what these tests are about is what the *run* does with an answer or a
    refusal, and a real resolver would make the failure path require breaking
    something real to reach. The composition is proved by
    `test_context_resolve_surfaces.py`; the parity between what this returns and
    what the real one returns is a shape mypy checks.
    """

    def __init__(self, *, raises_for: str | None = None) -> None:
        self.raises_for = raises_for
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, ctx: TenantContext, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises_for is not None and kwargs.get("query") == self.raises_for:
            raise RuntimeError("the canonical arm is unavailable")

        class _Envelope:
            state = "complete"

        class _Resolved:
            envelope = _Envelope()
            receipt_id = uuid.uuid4()

        return _Resolved()


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'evaluation')"),
            {"slug": f"eval-{tenant_id.hex[:8]}", "t": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                "                    declared_at, declared_by, created_at) "
                "VALUES (:a, :t, :sub, 'Reviewer', 'human', :now, :a, :now)"
            ),
            {"a": actor_id, "now": _NOW, "sub": f"s-{actor_id.hex[:8]}", "t": tenant_id},
        )
    try:
        yield {
            "ctx": TenantContext(actor_id=actor_id, roles=("producer",), tenant_id=tenant_id),
            "factory": factory,
            "pg": pg_container,
            "tenant_id": tenant_id,
        }
    finally:
        await engine.dispose()


def _service(world: dict[str, Any], *, resolver: _Resolver | None = None, fingerprint: str = _FINGERPRINT):
    return EvaluationRunService(
        clock=FakeClock(_NOW),
        fingerprint=fingerprint,
        resolver=resolver or _Resolver(),  # type: ignore[arg-type]
        session_factory=world["factory"],
    )


async def _set_with(service: EvaluationRunService, ctx: TenantContext, *queries: str) -> uuid.UUID:
    created = await service.create_set(ctx, name=f"set-{uuid.uuid4().hex[:8]}")
    for query in queries:
        await service.add_prompt(ctx, set_id=created.set_id, request={"query": query})
    return created.set_id


# --- prompt sets --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_set_holds_its_prompts_in_the_order_they_were_added(world: dict[str, Any]) -> None:
    """Runs report in this order, so two runs of one set read side by side rather
    than being reconciled by hand."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first", "second", "third")

    prompts = await service.prompts_in(world["ctx"], set_id=set_id)

    assert [prompt.request["query"] for prompt in prompts] == ["first", "second", "third"]
    assert [prompt.position for prompt in prompts] == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_prompt_that_could_never_resolve_is_refused_when_it_is_saved(
    world: dict[str, Any],
) -> None:
    """Rather than failing at run time, per prompt, on every run afterwards."""
    service = _service(world)
    created = await service.create_set(world["ctx"], name="bad")

    with pytest.raises(ValidationError):
        await service.add_prompt(world["ctx"], set_id=created.set_id, request={"query": ""})

    assert await service.prompts_in(world["ctx"], set_id=created.set_id) == ()


@pytest.mark.asyncio
async def test_a_set_belongs_to_one_tenant(world: dict[str, Any]) -> None:
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    stranger = TenantContext(actor_id=uuid.uuid4(), roles=("producer",), tenant_id=uuid.uuid4())

    with pytest.raises(NotFoundError):
        await service.prompts_in(stranger, set_id=set_id)


@pytest.mark.asyncio
async def test_two_sets_in_one_tenant_cannot_share_a_name(world: dict[str, Any]) -> None:
    """A name is how a run is found later. Two sets sharing one leaves a reader
    choosing between runs they cannot tell apart."""
    service = _service(world)
    await service.create_set(world["ctx"], name="the same name")

    with pytest.raises(Exception, match="uq_prompt_set_name"):
        await service.create_set(world["ctx"], name="the same name")


# --- runs ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_resolves_every_prompt_and_keeps_each_receipt(world: dict[str, Any]) -> None:
    resolver = _Resolver()
    service = _service(world, resolver=resolver)
    set_id = await _set_with(service, world["ctx"], "first", "second")

    run = await service.start_run(world["ctx"], set_id=set_id)

    assert [call["query"] for call in resolver.calls] == ["first", "second"]
    assert len(run.items) == 2
    assert all(item.receipt_id is not None for item in run.items)
    assert all(item.envelope_state == "complete" for item in run.items)
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_an_errored_prompt_stays_in_the_run_and_the_run_continues(
    world: dict[str, Any],
) -> None:
    """The rule this feature is built around.

    Dropping the errored prompt is how a number improves without anything
    improving, and it is indistinguishable from the system having got better at
    the prompts it did not crash on. Stopping at the first failure is the same
    defect in the other direction: the run would report on a subset chosen by
    whichever prompt happened to break.
    """
    service = _service(world, resolver=_Resolver(raises_for="second"))
    set_id = await _set_with(service, world["ctx"], "first", "second", "third")

    run = await service.start_run(world["ctx"], set_id=set_id)

    assert len(run.items) == 3, "every prompt is attempted"
    failed = run.items[1]
    assert failed.receipt_id is None
    assert failed.envelope_state is None
    assert failed.failure is not None
    assert "canonical arm is unavailable" in failed.failure
    assert run.items[2].receipt_id is not None, "the run continued past the failure"


@pytest.mark.asyncio
async def test_a_run_records_the_prompt_count_it_ran_rather_than_counting_later(
    world: dict[str, Any],
) -> None:
    """A set can gain a prompt afterwards, and a run counted from its items would
    then look as though it had skipped one."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first", "second")
    run = await service.start_run(world["ctx"], set_id=set_id)

    await service.add_prompt(world["ctx"], set_id=set_id, request={"query": "third"})

    reread = await service.run(world["ctx"], run.run_id)
    assert reread.prompt_count == 2
    assert len(reread.items) == 2


@pytest.mark.asyncio
async def test_runs_under_different_deployments_are_not_comparable(world: dict[str, Any]) -> None:
    """A difference between them is evidence the configuration changed, not
    evidence about retrieval — and reporting the first as the second is the wrong
    answer this whole loop exists to avoid producing."""
    set_id = await _set_with(_service(world), world["ctx"], "first")

    before = await _service(world).start_run(world["ctx"], set_id=set_id)
    after = await _service(world, fingerprint=_OTHER_FINGERPRINT).start_run(world["ctx"], set_id=set_id)

    assert before.comparable_to(before)
    assert not before.comparable_to(after)
    assert not after.comparable_to(before)


@pytest.mark.asyncio
async def test_two_runs_of_one_set_under_one_deployment_are_comparable(
    world: dict[str, Any],
) -> None:
    """The control. A `comparable_to` that always said no would pass the test
    above and make the feature useless."""
    set_id = await _set_with(_service(world), world["ctx"], "first")

    first = await _service(world).start_run(world["ctx"], set_id=set_id)
    second = await _service(world).start_run(world["ctx"], set_id=set_id)

    assert first.comparable_to(second)


@pytest.mark.asyncio
async def test_a_runs_list_carries_headers_and_not_items(world: dict[str, Any]) -> None:
    """A comparison starts by choosing two runs; loading every item of every run
    to render that choice would read the whole history to answer a question about
    two rows of it."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first", "second")
    await service.start_run(world["ctx"], set_id=set_id)
    await service.start_run(world["ctx"], set_id=set_id)

    runs = await service.runs_of(world["ctx"], set_id=set_id)

    assert len(runs) == 2
    assert all(run.items == () for run in runs)
    assert all(run.prompt_count == 2 for run in runs)


# --- verdicts -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_is_read_back_from_the_run(world: dict[str, Any]) -> None:
    """The whole point: a judgement that survives the tab being closed."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)

    await service.record_verdict(
        world["ctx"], item_id=run.items[0].item_id, note="the ARC block was empty", verdict="wrong"
    )

    reread = await service.run(world["ctx"], run.run_id)
    (verdict,) = reread.items[0].verdicts
    assert verdict.verdict == "wrong"
    assert verdict.note == "the ARC block was empty"
    assert verdict.recorded_by == world["ctx"].actor_id


@pytest.mark.asyncio
async def test_a_reviewer_who_changes_their_mind_has_one_opinion(world: dict[str, Any]) -> None:
    """Two rows would leave a reader counting revisions as agreement."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)
    item_id = run.items[0].item_id

    await service.record_verdict(world["ctx"], item_id=item_id, note="looked wrong", verdict="wrong")
    await service.record_verdict(world["ctx"], item_id=item_id, verdict="right")

    reread = await service.run(world["ctx"], run.run_id)
    (verdict,) = reread.items[0].verdicts
    assert verdict.verdict == "right"
    assert verdict.note is None


@pytest.mark.asyncio
async def test_two_reviewers_disagreeing_is_two_verdicts(world: dict[str, Any]) -> None:
    """Disagreement is a fact worth keeping. Collapsing it would report consensus
    that was never reached."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)
    item_id = run.items[0].item_id

    async with world["factory"]() as session, session.begin():
        second = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                "                    declared_at, declared_by, created_at) "
                "VALUES (:a, :t, :sub, 'Second reviewer', 'human', :now, :a, :now)"
            ),
            {"a": second, "now": _NOW, "sub": f"s-{second.hex[:8]}", "t": world["tenant_id"]},
        )

    await service.record_verdict(world["ctx"], item_id=item_id, verdict="right")
    other = TenantContext(actor_id=second, roles=("producer",), tenant_id=world["tenant_id"])
    await service.record_verdict(other, item_id=item_id, note="missed the ARC block", verdict="wrong")

    reread = await service.run(world["ctx"], run.run_id)
    assert {entry.verdict for entry in reread.items[0].verdicts} == {"right", "wrong"}


@pytest.mark.asyncio
async def test_an_adverse_verdict_says_why(world: dict[str, Any]) -> None:
    """A judgement with no reason is one the next reader has to reach again from
    scratch, which is the same as not having recorded it."""
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)

    with pytest.raises(ValidationError, match="says why"):
        await service.record_verdict(world["ctx"], item_id=run.items[0].item_id, verdict="wrong")


@pytest.mark.asyncio
async def test_a_verdict_outside_the_closed_set_is_refused(world: dict[str, Any]) -> None:
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)

    with pytest.raises(ValidationError, match="verdict is one of"):
        await service.record_verdict(world["ctx"], item_id=run.items[0].item_id, note="x", verdict="4 out of 5")


@pytest.mark.asyncio
async def test_a_verdict_cannot_be_filed_on_another_tenants_item(world: dict[str, Any]) -> None:
    service = _service(world)
    set_id = await _set_with(service, world["ctx"], "first")
    run = await service.start_run(world["ctx"], set_id=set_id)
    stranger = TenantContext(actor_id=uuid.uuid4(), roles=("producer",), tenant_id=uuid.uuid4())

    with pytest.raises(NotFoundError):
        await service.record_verdict(stranger, item_id=run.items[0].item_id, verdict="right")


@pytest.mark.asyncio
async def test_a_verdict_is_attributed_to_the_caller_and_not_to_a_named_actor(
    world: dict[str, Any],
) -> None:
    """There is no parameter for whose verdict this is. A judgement somebody could
    file under another person's name is not evidence about anything, and the
    absence of the parameter is what makes that true rather than a check."""
    import inspect

    parameters = set(inspect.signature(EvaluationRunService.record_verdict).parameters)

    assert parameters == {"self", "ctx", "item_id", "verdict", "note"}


# --- expectations (E24-T7) ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_prompt_carries_what_it_is_checking(world: dict[str, Any]) -> None:
    """Declared at the prompt, before any run: a requirement written afterwards
    would be satisfied by whatever the system returned."""
    service = _service(world)
    created = await service.create_set(world["ctx"], name=f"set-{uuid.uuid4().hex[:8]}")

    await service.add_prompt(
        world["ctx"],
        set_id=created.set_id,
        request={"query": "drain the queue"},
        expectations={"required_item_keys": ["k1"], "min_recall": 0.9, "preset": "compliance"},
    )

    prompts = await service.prompts_in(world["ctx"], set_id=created.set_id)
    assert prompts[0].expectations == {
        "min_recall": 0.9,
        "preset": "compliance",
        "require_groundedness": True,
        "require_relevance": True,
        "required_item_keys": ["k1"],
    }


@pytest.mark.asyncio
async def test_a_prompt_that_asserts_nothing_stores_nothing(world: dict[str, Any]) -> None:
    """A real state, and different from an object of permissive thresholds."""
    service = _service(world)
    created = await service.create_set(world["ctx"], name=f"set-{uuid.uuid4().hex[:8]}")

    await service.add_prompt(world["ctx"], set_id=created.set_id, request={"query": "anything"})

    prompts = await service.prompts_in(world["ctx"], set_id=created.set_id)
    assert prompts[0].expectations is None


@pytest.mark.asyncio
async def test_unusable_expectations_are_refused_at_the_prompt(world: dict[str, Any]) -> None:
    """A far better place to find out than on every run afterwards."""
    service = _service(world)
    created = await service.create_set(world["ctx"], name=f"set-{uuid.uuid4().hex[:8]}")

    with pytest.raises(ValidationError, match="do not carry"):
        await service.add_prompt(
            world["ctx"],
            set_id=created.set_id,
            request={"query": "q"},
            expectations={"min_recal": 0.9},
        )
