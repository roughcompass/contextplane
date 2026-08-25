"""Judging a simulated answer, and the human who may overrule the judge.

E24-T5 and E24-T7, against a real database. What is proved here is what a fake
would agree with whatever the code did:

- **the pinned tuple travels on every judged row**, all three values;
- **confidence is stored and contributes nothing**, so a fit can be built later
  from a deployment that has been recording since its first run;
- **a review is a second row, not an update**, because the pair `(what the judge
  said, what the person said)` is the only thing calibration can be fitted from;
- **a re-judge at the same panel position replaces**, and a second panel member
  does not, which is what makes a 2–1 split recordable as one;
- **disagreement is a visible state**, never a silent overwrite;
- **the judge grades the material the candidate was shown**, read from the
  simulation record rather than re-resolved.
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

from contextplane.context.evaluation.judgement import (
    REVIEW_CONFIRMED,
    REVIEW_OVERRULED,
    REVIEW_UNSURE,
    JudgementService,
)
from contextplane.context.evaluation.simulation import (
    CitedItem,
    ServedRecord,
    SimulatedAssertion,
    Simulation,
    TokenReport,
)
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.extraction.judge_prompt import (
    CRITERION_GROUNDEDNESS,
    CRITERION_RELEVANCE,
    JUDGE_RUBRIC_VERSION,
    JUDGED_CRITERIA,
    CriterionJudgement,
    JudgementCall,
    prompt_template_hash,
)
from contextplane.extraction.provider import TokenUsage
from contextplane.extraction.response_factory import JudgeFamilyRefused
from contextplane.extraction.response_provider import SimulationUnavailable
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 25, 12, 0, tzinfo=datetime.UTC)


class _JudgeProvider:
    """A judge that grades however the test told it to, recording its requests."""

    provider_id = "openai"
    default_model_id = "gpt-judge"

    def __init__(self, *, verdicts: dict[str, str] | None = None, confidence: float = 0.7) -> None:
        self._verdicts = verdicts or dict.fromkeys(JUDGED_CRITERIA, "pass")
        self._confidence = confidence
        self.requests: list[Any] = []

    async def judge(self, request: Any) -> JudgementCall:
        self.requests.append(request)
        return JudgementCall(
            criteria=tuple(
                CriterionJudgement(
                    confidence=self._confidence,
                    criterion=name,
                    evidence=(f"a span for {name}",),
                    reasoning=f"step by step about {name}",
                    verdict=self._verdicts[name],
                )
                for name in JUDGED_CRITERIA
            ),
            duration_ms=11,
            model_id="gpt-judge-2026",
            usage=TokenUsage(
                cached_prompt_tokens=0, completion_tokens=30, prompt_tokens=200, source="provider_reported"
            ),
        )


class _Simulations:
    """Returns one prepared simulation, standing in for the service that stores them."""

    def __init__(self, simulation: Simulation) -> None:
        self._simulation = simulation

    async def get(self, ctx: TenantContext, simulation_id: uuid.UUID) -> Simulation:
        if simulation_id != self._simulation.simulation_id:
            raise NotFoundError(f"simulation {simulation_id} not found")
        return self._simulation


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, other_id, agent_id = (uuid.uuid4() for _ in range(4))
    simulation_id, receipt_id = uuid.uuid4(), uuid.uuid4()

    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'judging')"),
            {"slug": f"judge-{tenant_id.hex[:8]}", "t": tenant_id},
        )
        for principal, kind in ((actor_id, "human"), (other_id, "human"), (agent_id, "agent")):
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind, "
                    "                    declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, :sub, 'Principal', :kind, :now, :a, :now)"
                ),
                {"a": principal, "kind": kind, "now": _NOW, "sub": f"s-{principal.hex[:8]}", "t": tenant_id},
            )
        await session.execute(
            text(
                "INSERT INTO evaluation_simulations "
                "(simulation_id, tenant_id, receipt_id, simulated_actor_id, prompt, answer, provider_id, "
                " model_id, instruction_disposition, usage_source, served_item_count, created_by, created_at) "
                "VALUES (:sid, :tid, :rid, :agent, 'how do I drain it?', 'Through the runbook.', "
                "        'anthropic', 'claude-test', 'declared_known', 'unknown', 1, :a, :now)"
            ),
            {"a": actor_id, "agent": agent_id, "now": _NOW, "rid": receipt_id, "sid": simulation_id, "tid": tenant_id},
        )

    simulation = Simulation(
        answer="Through the runbook.",
        assertions=(
            SimulatedAssertion(
                citations=(CitedItem(receipt_item_id="rid-1", was_served=True),),
                position=0,
                text="The runbook drains the queue.",
            ),
            SimulatedAssertion(
                citations=(CitedItem(receipt_item_id="ghost", was_served=False),),
                position=1,
                text="It takes four minutes.",
            ),
        ),
        created_at=_NOW,
        duration_ms=42,
        envelope_state="complete",
        instruction_disposition="declared_known",
        model_id="claude-test",
        prompt="how do I drain it?",
        provider_id="anthropic",
        receipt_id=receipt_id,
        served=(
            ServedRecord(
                block="workspace",
                item_key="c1",
                payload_json='{"goal": "drain the dead-letter queue"}',
                receipt_item_id="rid-1",
            ),
        ),
        simulated_actor_id=agent_id,
        simulation_id=simulation_id,
        usage=TokenReport(
            cached_prompt_tokens=None,
            completion_tokens=None,
            prompt_tokens=None,
            served_item_count=1,
            source="unknown",
        ),
    )

    try:
        yield {
            "ctx": TenantContext(actor_id=actor_id, roles=("producer",), tenant_id=tenant_id),
            "factory": factory,
            "other_ctx": TenantContext(actor_id=other_id, roles=("producer",), tenant_id=tenant_id),
            "simulation": simulation,
            "simulation_id": simulation_id,
            "tenant_id": tenant_id,
        }
    finally:
        await engine.dispose()


def _service(
    world: dict[str, Any],
    *,
    provider: _JudgeProvider | None = None,
    judge_selector: str = "openai",
    candidate_selector: str = "anthropic",
) -> JudgementService:
    return JudgementService(
        candidate_model_pin="",
        candidate_selector=candidate_selector,
        clock=FakeClock(_NOW),
        model_pin="",
        provider=provider,  # type: ignore[arg-type]
        provider_selector=judge_selector,
        session_factory=world["factory"],
        simulations=_Simulations(world["simulation"]),  # type: ignore[arg-type]
    )


# --- the pinned tuple ---------------------------------------------------------


@pytest.mark.asyncio
async def test_every_judged_row_carries_all_three_pinned_values(world: dict[str, Any]) -> None:
    judged = await _service(world, provider=_JudgeProvider()).judge(world["ctx"], simulation_id=world["simulation_id"])

    assert len(judged) == 2
    for entry in judged:
        assert entry.judge_model_id == "gpt-judge-2026"
        assert entry.rubric_version == JUDGE_RUBRIC_VERSION
        assert entry.prompt_template_hash == prompt_template_hash()
        assert entry.pinned_tuple == (entry.judge_model_id, entry.rubric_version, entry.prompt_template_hash)


@pytest.mark.asyncio
async def test_both_criteria_are_judged_in_rubric_order(world: dict[str, Any]) -> None:
    judged = await _service(world, provider=_JudgeProvider()).judge(world["ctx"], simulation_id=world["simulation_id"])
    assert [entry.criterion for entry in judged] == list(JUDGED_CRITERIA)


@pytest.mark.asyncio
async def test_reasoning_and_evidence_are_read_back(world: dict[str, Any]) -> None:
    """A verdict a reviewer can only accept or reject is not reviewable."""
    service = _service(world, provider=_JudgeProvider())
    await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    grounded = next(entry for entry in read_back if entry.criterion == CRITERION_GROUNDEDNESS)
    assert grounded.reasoning == f"step by step about {CRITERION_GROUNDEDNESS}"
    assert grounded.evidence == (f"a span for {CRITERION_GROUNDEDNESS}",)


@pytest.mark.asyncio
async def test_confidence_is_stored_from_the_first_run(world: dict[str, Any]) -> None:
    """A deployment that discards raw scores can never stop being uncalibrated."""
    service = _service(world, provider=_JudgeProvider(confidence=0.62))
    await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    assert {entry.confidence for entry in read_back} == {0.62}


# --- what the judge is shown --------------------------------------------------


@pytest.mark.asyncio
async def test_the_judge_grades_the_material_the_candidate_was_shown(world: dict[str, Any]) -> None:
    """Read from the simulation record: the receipt does not carry content."""
    provider = _JudgeProvider()
    await _service(world, provider=provider).judge(world["ctx"], simulation_id=world["simulation_id"])

    request = provider.requests[0]
    assert request.served == (("rid-1", '{"goal": "drain the dead-letter queue"}'),)
    assert request.answer == "Through the runbook."


@pytest.mark.asyncio
async def test_the_judge_is_told_which_citations_were_never_served(world: dict[str, Any]) -> None:
    """Its mistake about which were real must not become the finding."""
    provider = _JudgeProvider()
    await _service(world, provider=provider).judge(world["ctx"], simulation_id=world["simulation_id"])

    request = provider.requests[0]
    assert request.assertions[0].unserved_citations == ()
    assert request.assertions[1].unserved_citations == ("ghost",)


# --- the refusals -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_same_family_judge_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider(), judge_selector="anthropic")
    with pytest.raises(JudgeFamilyRefused, match="JUDGE_PROVIDER"):
        await service.judge(world["ctx"], simulation_id=world["simulation_id"])


@pytest.mark.asyncio
async def test_no_judge_configured_says_which_setting_is_unset(world: dict[str, Any]) -> None:
    service = _service(world, provider=None)
    assert service.is_available is False
    with pytest.raises(SimulationUnavailable, match="JUDGE_PROVIDER"):
        await service.judge(world["ctx"], simulation_id=world["simulation_id"])


# --- panels and re-judging ----------------------------------------------------


@pytest.mark.asyncio
async def test_re_judging_at_one_position_replaces_rather_than_accumulating(world: dict[str, Any]) -> None:
    """A re-run is not a second opinion."""
    service = _service(world, provider=_JudgeProvider())
    await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    service = _service(world, provider=_JudgeProvider(verdicts=dict.fromkeys(JUDGED_CRITERIA, "fail")))
    await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    assert len(read_back) == 2
    assert {entry.verdict for entry in read_back} == {"fail"}


@pytest.mark.asyncio
async def test_a_second_panel_member_is_a_second_row(world: dict[str, Any]) -> None:
    """A 2-1 split has to be recordable as one."""
    service = _service(world, provider=_JudgeProvider())
    await service.judge(world["ctx"], simulation_id=world["simulation_id"], panel_position=0)

    dissenter = _service(
        world, provider=_JudgeProvider(verdicts={CRITERION_GROUNDEDNESS: "fail", CRITERION_RELEVANCE: "pass"})
    )
    await dissenter.judge(world["ctx"], simulation_id=world["simulation_id"], panel_position=1)

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    grounded = [entry for entry in read_back if entry.criterion == CRITERION_GROUNDEDNESS]
    assert [entry.panel_position for entry in grounded] == [0, 1]
    assert {entry.verdict for entry in grounded} == {"pass", "fail"}


# --- human review -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_review_is_a_second_row_and_the_judges_verdict_survives(world: dict[str, Any]) -> None:
    """The pair is the only thing calibration can be fitted from."""
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])
    target = judged[0]

    await service.record_review(
        world["ctx"],
        judgement_id=target.judgement_id,
        note="the cited item says nothing of the kind",
        verdict=REVIEW_OVERRULED,
    )

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    reviewed = next(entry for entry in read_back if entry.judgement_id == target.judgement_id)
    assert reviewed.verdict == target.verdict
    assert [review.verdict for review in reviewed.reviews] == [REVIEW_OVERRULED]
    assert reviewed.is_disputed is True


@pytest.mark.asyncio
async def test_a_confirmation_is_not_a_dispute(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    await service.record_review(world["ctx"], judgement_id=judged[0].judgement_id, verdict=REVIEW_CONFIRMED)

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    assert next(e for e in read_back if e.judgement_id == judged[0].judgement_id).is_disputed is False


@pytest.mark.asyncio
async def test_a_reviewer_who_changed_their_mind_has_one_opinion(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    await service.record_review(
        world["ctx"], judgement_id=judged[0].judgement_id, note="first thought", verdict=REVIEW_OVERRULED
    )
    await service.record_review(world["ctx"], judgement_id=judged[0].judgement_id, verdict=REVIEW_CONFIRMED)

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    reviews = next(e for e in read_back if e.judgement_id == judged[0].judgement_id).reviews
    assert [review.verdict for review in reviews] == [REVIEW_CONFIRMED]


@pytest.mark.asyncio
async def test_two_reviewers_disagreeing_stays_two_rows(world: dict[str, Any]) -> None:
    """Disagreement between people is a fact worth keeping."""
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    await service.record_review(world["ctx"], judgement_id=judged[0].judgement_id, verdict=REVIEW_CONFIRMED)
    await service.record_review(
        world["other_ctx"], judgement_id=judged[0].judgement_id, note="I read it differently", verdict=REVIEW_OVERRULED
    )

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    reviews = next(e for e in read_back if e.judgement_id == judged[0].judgement_id).reviews
    assert sorted(review.verdict for review in reviews) == [REVIEW_CONFIRMED, REVIEW_OVERRULED]


@pytest.mark.asyncio
async def test_a_disagreement_with_no_reason_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])
    with pytest.raises(ValidationError, match="says why"):
        await service.record_review(world["ctx"], judgement_id=judged[0].judgement_id, verdict=REVIEW_OVERRULED)


@pytest.mark.asyncio
async def test_unsure_needs_a_reason_too_and_is_a_fact_about_the_reviewer(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    await service.record_review(
        world["ctx"], judgement_id=judged[0].judgement_id, note="I cannot tell from the evidence", verdict=REVIEW_UNSURE
    )

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    reviewed = next(e for e in read_back if e.judgement_id == judged[0].judgement_id)
    assert [r.verdict for r in reviewed.reviews] == [REVIEW_UNSURE]
    assert reviewed.is_disputed is False


@pytest.mark.asyncio
async def test_an_unknown_review_verdict_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])
    with pytest.raises(ValidationError, match="review verdict"):
        await service.record_review(world["ctx"], judgement_id=judged[0].judgement_id, verdict="maybe", note="x")


@pytest.mark.asyncio
async def test_an_observed_confidence_outside_the_range_is_refused(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])
    with pytest.raises(ValidationError, match="0 to 1"):
        await service.record_review(
            world["ctx"], judgement_id=judged[0].judgement_id, observed_confidence=1.4, verdict=REVIEW_CONFIRMED
        )


@pytest.mark.asyncio
async def test_reviewing_a_judgement_from_another_tenant_is_not_found(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])
    elsewhere = TenantContext(actor_id=world["ctx"].actor_id, roles=("producer",), tenant_id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await service.record_review(elsewhere, judgement_id=judged[0].judgement_id, verdict=REVIEW_CONFIRMED)


@pytest.mark.asyncio
async def test_the_observed_confidence_is_read_back(world: dict[str, Any]) -> None:
    service = _service(world, provider=_JudgeProvider())
    judged = await service.judge(world["ctx"], simulation_id=world["simulation_id"])

    await service.record_review(
        world["ctx"], judgement_id=judged[0].judgement_id, observed_confidence=0.25, verdict=REVIEW_CONFIRMED
    )

    read_back = await service.judgements_of(world["ctx"], world["simulation_id"])
    reviews = next(e for e in read_back if e.judgement_id == judged[0].judgement_id).reviews
    assert reviews[0].observed_confidence == 0.25
