"""Judging a simulated answer, and the human who may overrule the judge.

E24-T5 and E24-T7, on ADR 0026. Two criteria a program cannot compute, scored by
a model that is never the candidate, returning its reasoning and the span it
relied on — never a bare score.

## The judge is not the candidate, and the service enforces it

`assert_families_differ` runs before any model is called, on the same path both
transports reach. ADR 0026 made this a constraint rather than advice because an
advisory note is the shape of guidance followed until the day somebody is in a
hurry and has one key.

## Nothing scores itself

E22-T15's clause survives intact, and this is where it is kept. The judge is a
different model in a different provider family; the deterministic three run with
no model at all; and neither ever asks the system under test whether it was
right. `evidence.py` enforces the same separation for the research campaign and
`EVIDENCE_CARRIES_NO_DECISION` is asserted by the conformance suite.

## The pinned tuple travels on every judged row

`(judge_model_id, rubric_version, prompt_template_hash)`, plus the provider so a
reader can see which family judged. A run keeps the version it ran under, and old
rows are never re-judged when a rubric is edited — `protocol.py`'s discipline,
which holds that a run whose freeze does not match is *invalid, not adjusted*.

## Confidence is recorded and contributes nothing

Stored from the very first run, because a mapping can only ever be fitted from
raw scores paired with judged outcomes. It contributes nothing to any displayed
verdict until E24-T6 has a fit for the tuple; until then the surface renders the
verdict as unproven, per ADR 0026 part 3.

## The judge grades the material the candidate was shown

Not the receipt. `context_receipt_items` records *which* items a resolution
served and deliberately not their content, so a judge asked whether an answer is
grounded in what was served would have nothing to check against — and
re-resolving to recover it would grade a different envelope than the answer came
from. The simulation records what it showed the model, serialized exactly once,
and the judge reads that.

## An override is a second row, and that is what calibration learns from

A human confirming or overruling is a fact beside the judge's, never a correction
to it. Overwriting would destroy the pair `(what the judge said, what the person
said)`, which is the only thing a fit can be built from — and it would erase the
disagreement the score pane renders as a visible state.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.extraction.judge_prompt import (
    JUDGE_RUBRIC_VERSION,
    JUDGED_CRITERIA,
    JudgedItem,
    JudgementRequest,
    prompt_template_hash,
)
from contextplane.extraction.response_factory import assert_families_differ, resolved_model
from contextplane.extraction.response_provider import SimulationUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import RowMapping
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.context.evaluation.simulation import SimulationService
    from contextplane.extraction.judge_prompt import JudgeProvider
    from contextplane.types import Clock, TenantContext

#: What a reviewer may say about a judged criterion. `unsure` is deliberately not
#: a third verdict on the answer -- it is information about the *reviewer*, and
#: calibration excludes it for the reason `calibration.py` excludes an
#: undecidable adjudication: counting it either way would bias the fit.
REVIEW_CONFIRMED: Final = "confirmed"
REVIEW_OVERRULED: Final = "overruled"
REVIEW_UNSURE: Final = "unsure"
REVIEW_VERDICTS: Final[tuple[str, ...]] = (REVIEW_CONFIRMED, REVIEW_OVERRULED, REVIEW_UNSURE)

#: The panel position a single judge occupies. Zero rather than null, so a
#: re-judge replaces rather than accumulating -- see the migration.
SINGLE_JUDGE_POSITION: Final[int] = 0

#: How many output tokens a judging call may spend. Larger than it looks like it
#: needs to be, because the reasoning is the deliverable: a judge truncated
#: mid-argument produces a verdict whose trace stops before the conclusion, which
#: is the one shape a reviewer cannot use.
JUDGE_MAX_OUTPUT_TOKENS: Final[int] = 2048


@dataclasses.dataclass(frozen=True)
class ReviewerVerdict:
    """One person's word on one judged criterion."""

    verdict: str
    note: str | None
    observed_confidence: float | None
    reviewed_by: uuid.UUID
    reviewed_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class Judgement:
    """One criterion, judged, with the trace that makes it arguable."""

    judgement_id: uuid.UUID
    simulation_id: uuid.UUID
    criterion: str
    verdict: str
    reasoning: str
    evidence: tuple[str, ...]
    #: The judge's own number, on its own scale. Uncalibrated until a fit exists
    #: for the pinned tuple, and the surface says so rather than implying
    #: otherwise.
    confidence: float
    judge_provider_id: str
    judge_model_id: str
    rubric_version: str
    prompt_template_hash: str
    panel_position: int
    created_at: datetime.datetime
    reviews: tuple[ReviewerVerdict, ...] = ()

    @property
    def pinned_tuple(self) -> tuple[str, str, str]:
        """What this verdict was produced under, as calibration separates it."""
        return (self.judge_model_id, self.rubric_version, self.prompt_template_hash)

    @property
    def is_disputed(self) -> bool:
        """Whether any reviewer overruled the judge.

        A visible state rather than a silent overwrite: a criterion a person and
        a model disagree about is the one most worth somebody's time, and it
        escalates onto the Judgement surface rather than being resolved here.
        """
        return any(review.verdict == REVIEW_OVERRULED for review in self.reviews)


class JudgementService:
    """Judge a simulated answer, and record what a human said about the judging."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        simulations: SimulationService,
        clock: Clock,
        provider: JudgeProvider | None,
        provider_selector: str,
        model_pin: str,
        candidate_selector: str,
        candidate_model_pin: str,
    ) -> None:
        self._session_factory = session_factory
        self._simulations = simulations
        self._clock = clock
        self._provider = provider
        self._selector = provider_selector
        self._model_pin = model_pin
        self._candidate_selector = candidate_selector
        self._candidate_model_pin = candidate_model_pin

    @property
    def is_available(self) -> bool:
        """Whether this deployment can judge at all."""
        return self._provider is not None

    async def judge(
        self, ctx: TenantContext, *, simulation_id: uuid.UUID, panel_position: int = SINGLE_JUDGE_POSITION
    ) -> tuple[Judgement, ...]:
        """Grade one simulated answer on both model-judged criteria.

        Both criteria in one call rather than two, because they are read from the
        same material and two calls would double the cost to produce two verdicts
        that could disagree about what the answer said.
        """
        provider = self._require_provider()
        assert_families_differ(
            candidate_model=resolved_model(selector=self._candidate_selector, pinned=self._candidate_model_pin),
            candidate_provider=self._candidate_selector,
            judge_model=resolved_model(selector=self._selector, pinned=self._model_pin),
            judge_provider=self._selector,
        )
        simulation = await self._simulations.get(ctx, simulation_id)

        request = JudgementRequest(
            answer=simulation.answer,
            assertions=tuple(
                JudgedItem(
                    cited_receipt_item_ids=tuple(c.receipt_item_id for c in assertion.citations),
                    text=assertion.text,
                    unserved_citations=tuple(c.receipt_item_id for c in assertion.citations if not c.was_served),
                )
                for assertion in simulation.assertions
            ),
            boundary=_new_boundary(),
            max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
            model_id=resolved_model(selector=self._selector, pinned=self._model_pin),
            prompt=simulation.prompt,
            served=tuple((record.receipt_item_id, record.payload_json) for record in simulation.served),
        )
        call = await provider.judge(request)

        now = self._clock.now()
        template_hash = prompt_template_hash()
        judgements = tuple(
            Judgement(
                confidence=criterion.confidence,
                created_at=now,
                criterion=criterion.criterion,
                evidence=criterion.evidence,
                judge_model_id=call.model_id,
                judge_provider_id=provider.provider_id,
                judgement_id=uuid.uuid4(),
                panel_position=panel_position,
                prompt_template_hash=template_hash,
                reasoning=criterion.reasoning,
                rubric_version=JUDGE_RUBRIC_VERSION,
                simulation_id=simulation_id,
                verdict=criterion.verdict,
            )
            for criterion in call.criteria
        )
        await self._store(ctx, judgements)
        return judgements

    async def judgements_of(self, ctx: TenantContext, simulation_id: uuid.UUID) -> tuple[Judgement, ...]:
        """Every judgement of one simulation, with every review on each.

        Ordered by criterion then panel position, so a panel's split on one
        criterion reads as adjacent rows rather than being interleaved with the
        other criterion's.
        """
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT j.judgement_id, j.simulation_id, j.criterion, j.verdict, j.reasoning, "
                            "       j.evidence, j.confidence, j.judge_provider_id, j.judge_model_id, "
                            "       j.rubric_version, j.prompt_template_hash, j.panel_position, j.created_at "
                            "  FROM evaluation_judgements j "
                            " WHERE j.simulation_id = :sid AND j.tenant_id = :tid "
                            " ORDER BY j.criterion, j.panel_position"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )
            review_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT r.judgement_id, r.verdict, r.note, r.observed_confidence, "
                            "       r.reviewed_by, r.reviewed_at "
                            "  FROM evaluation_judgement_reviews r "
                            "  JOIN evaluation_judgements j ON j.judgement_id = r.judgement_id "
                            " WHERE j.simulation_id = :sid AND r.tenant_id = :tid "
                            " ORDER BY r.reviewed_at"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )

        by_judgement: dict[uuid.UUID, list[ReviewerVerdict]] = {}
        for row in review_rows:
            by_judgement.setdefault(row["judgement_id"], []).append(
                ReviewerVerdict(
                    note=row["note"],
                    observed_confidence=None
                    if row["observed_confidence"] is None
                    else float(row["observed_confidence"]),
                    reviewed_at=row["reviewed_at"],
                    reviewed_by=row["reviewed_by"],
                    verdict=row["verdict"],
                )
            )
        return tuple(_judgement(row, tuple(by_judgement.get(row["judgement_id"], ()))) for row in rows)

    async def record_review(
        self,
        ctx: TenantContext,
        *,
        judgement_id: uuid.UUID,
        verdict: str,
        note: str | None = None,
        observed_confidence: float | None = None,
    ) -> ReviewerVerdict:
        """One reviewer's word on one judged criterion.

        Attributed to the caller, never to an actor the caller names: a review
        somebody could file under another person's name is not evidence about
        anything, and this is the table calibration is fitted from.

        Replaces that reviewer's earlier review of the same criterion rather than
        adding a second. Somebody who changed their mind has one opinion; two
        reviewers disagreeing stays two rows, because that is a fact worth
        keeping.
        """
        if verdict not in REVIEW_VERDICTS:
            raise ValidationError(f"a review verdict is one of {list(REVIEW_VERDICTS)}, got {verdict!r}")
        text_note = (note or "").strip() or None
        if verdict != REVIEW_CONFIRMED and text_note is None:
            raise ValidationError(
                f"a {verdict!r} review says why: a disagreement with no reason is one the next "
                "reader has to reach again from scratch"
            )
        if observed_confidence is not None and not 0.0 <= observed_confidence <= 1.0:
            raise ValidationError(f"observed_confidence is 0 to 1 when given, got {observed_confidence!r}")

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            found = (
                await session.execute(
                    text("SELECT 1 FROM evaluation_judgements WHERE judgement_id = :jid AND tenant_id = :tid"),
                    {"jid": judgement_id, "tid": ctx.tenant_id},
                )
            ).first()
            if found is None:
                raise NotFoundError(f"judged criterion {judgement_id} not found")

            await session.execute(
                text(
                    "INSERT INTO evaluation_judgement_reviews "
                    "(judgement_id, tenant_id, verdict, note, observed_confidence, reviewed_by, reviewed_at) "
                    "VALUES (:jid, :tid, :verdict, :note, :confidence, :actor, :now) "
                    "ON CONFLICT (judgement_id, reviewed_by) "
                    "DO UPDATE SET verdict = EXCLUDED.verdict, note = EXCLUDED.note, "
                    "              observed_confidence = EXCLUDED.observed_confidence, "
                    "              reviewed_at = EXCLUDED.reviewed_at"
                ),
                {
                    "actor": ctx.actor_id,
                    "confidence": observed_confidence,
                    "jid": judgement_id,
                    "note": text_note,
                    "now": now,
                    "tid": ctx.tenant_id,
                    "verdict": verdict,
                },
            )
        return ReviewerVerdict(
            note=text_note,
            observed_confidence=observed_confidence,
            reviewed_at=now,
            reviewed_by=ctx.actor_id,
            verdict=verdict,
        )

    # -- helpers -----------------------------------------------------------

    def _require_provider(self) -> JudgeProvider:
        if self._provider is None:
            msg = (
                "judging is switched off: no judge provider is configured. Set JUDGE_PROVIDER and "
                "JUDGE_API_KEY to a family other than the simulator's. The deterministic criteria — "
                "required-fact recall, boundary violations and precision — need no judge and are "
                "unaffected."
            )
            raise SimulationUnavailable(msg)
        return self._provider

    async def _store(self, ctx: TenantContext, judgements: tuple[Judgement, ...]) -> None:
        async with self._session_factory() as session, session.begin():
            for judgement in judgements:
                await session.execute(
                    text(
                        "INSERT INTO evaluation_judgements "
                        "(judgement_id, simulation_id, tenant_id, criterion, verdict, reasoning, evidence, "
                        " confidence, judge_model_id, judge_provider_id, rubric_version, "
                        " prompt_template_hash, panel_position, created_at) "
                        "VALUES (:jid, :sid, :tid, :criterion, :verdict, :reasoning, CAST(:evidence AS JSONB), "
                        "        :confidence, :model, :provider, :rubric, :template, :position, :now) "
                        "ON CONFLICT (simulation_id, criterion, panel_position) DO UPDATE SET "
                        "  judgement_id = EXCLUDED.judgement_id, verdict = EXCLUDED.verdict, "
                        "  reasoning = EXCLUDED.reasoning, evidence = EXCLUDED.evidence, "
                        "  confidence = EXCLUDED.confidence, judge_model_id = EXCLUDED.judge_model_id, "
                        "  judge_provider_id = EXCLUDED.judge_provider_id, "
                        "  rubric_version = EXCLUDED.rubric_version, "
                        "  prompt_template_hash = EXCLUDED.prompt_template_hash, "
                        "  created_at = EXCLUDED.created_at"
                    ),
                    {
                        "confidence": round(judgement.confidence, 3),
                        "criterion": judgement.criterion,
                        "evidence": json.dumps(list(judgement.evidence)),
                        "jid": judgement.judgement_id,
                        "model": judgement.judge_model_id,
                        "now": judgement.created_at,
                        "position": judgement.panel_position,
                        "provider": judgement.judge_provider_id,
                        "reasoning": judgement.reasoning,
                        "rubric": judgement.rubric_version,
                        "sid": judgement.simulation_id,
                        "template": judgement.prompt_template_hash,
                        "tid": ctx.tenant_id,
                        "verdict": judgement.verdict,
                    },
                )


def _judgement(row: RowMapping, reviews: tuple[ReviewerVerdict, ...]) -> Judgement:
    evidence = row["evidence"] if isinstance(row["evidence"], list) else json.loads(row["evidence"])
    return Judgement(
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
        criterion=row["criterion"],
        evidence=tuple(str(span) for span in evidence),
        judge_model_id=row["judge_model_id"],
        judge_provider_id=row["judge_provider_id"],
        judgement_id=row["judgement_id"],
        panel_position=int(row["panel_position"]),
        prompt_template_hash=row["prompt_template_hash"],
        reasoning=row["reasoning"],
        reviews=reviews,
        rubric_version=row["rubric_version"],
        simulation_id=row["simulation_id"],
        verdict=row["verdict"],
    )


def _new_boundary() -> str:
    from contextplane.extraction.containment import new_boundary  # noqa: PLC0415

    return new_boundary()


__all__ = [
    "JUDGED_CRITERIA",
    "JUDGE_MAX_OUTPUT_TOKENS",
    "REVIEW_CONFIRMED",
    "REVIEW_OVERRULED",
    "REVIEW_UNSURE",
    "REVIEW_VERDICTS",
    "SINGLE_JUDGE_POSITION",
    "Judgement",
    "JudgementService",
    "ReviewerVerdict",
]
