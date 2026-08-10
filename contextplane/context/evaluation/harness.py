"""Running the batch: every scenario, every configuration, no exceptions.

The harness is the part that can quietly ruin a result without ever being
wrong about a single scenario. Three rules do the work, and each exists because
its opposite is the ordinary way an evaluation flatters the system it measures.

**A system error is a failure, never an exclusion.** If the system under test
raises or refuses on a scenario, that scenario scores zero for that
configuration and stays in the batch. Dropping errored runs after the fact is
the most common way a number improves without anything improving, and it is
indistinguishable from the system having got better at the scenarios it did not
crash on.

**An infrastructure error invalidates the whole batch, not the scenario.** A
database that fell over mid-run has produced observations under two different
systems. The batch is rerun whole; the system is deterministic, so that is
cheap, and it is the only treatment that cannot be used to launder a bad
scenario into an "infrastructure" one.

**Every configuration runs on every scenario, unconditionally.** No treatment is
skipped because an earlier one failed. Making the semantic run conditional on
the lexical one passing would foreclose the branch where lexical fails and
semantic succeeds -- an answer the protocol has to be able to return.

Latency is measured as a per-scenario median over repeated runs and compared
median-to-median. A single reading per scenario measures the machine, and a
percentile over a corpus this small rests on one or two observations.
"""

from __future__ import annotations

import dataclasses
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from contextplane.context.assembler import ArmOutcome, assemble
from contextplane.context.evaluation import judge
from contextplane.context.evaluation.protocol import (
    CONFIGURATIONS,
    HUMAN_RISK_SAMPLE_SIZE,
    LATENCY_REPEATS,
    SCENARIO_COUNTS,
    FrozenProtocol,
    freeze,
)
from contextplane.context.evaluation.treatments import workspace_arm_for
from contextplane.context.schemas.envelope import BLOCK_FAILED, BLOCK_WORKSPACE

if TYPE_CHECKING:  # pragma: no cover - typing only
    import datetime
    from collections.abc import Mapping, Sequence

    from contextplane.context.assembler import ContextArm
    from contextplane.context.evaluation.scenarios import Corpus, Scenario
    from contextplane.context.evaluation.treatments import Embedder, WorkspaceSource
    from contextplane.context.schemas.envelope import ContextEnvelopeV1


class InfrastructureError(Exception):
    """The environment failed, not the system under test.

    Raised by a source to say "this observation is not evidence about the
    system". Deliberately a distinct type rather than a flag on a result: the
    caller has to choose which of the two it is at the point where it knows,
    and a boolean would let that choice be made later, by whoever preferred the
    other answer.
    """


class BatchInvalidated(Exception):
    """The batch cannot be scored and must be rerun whole."""


class OtherArms(Protocol):
    """The three arms this evaluation holds fixed.

    Supplied per scenario so a canonical or governance answer can vary with the
    request, and identical across configurations by construction: the harness
    asks for them once per scenario and reuses the same mapping for all three
    runs.
    """

    def __call__(self, scenario: Scenario) -> Mapping[str, ContextArm]:
        """The canonical, governance and claim arms for one scenario."""
        ...


@dataclasses.dataclass(frozen=True)
class ScenarioRun:
    """One scenario under one configuration: what it scored and what it cost."""

    score: judge.ScenarioScore
    #: Per-repeat wall-clock, in milliseconds. Kept whole rather than reduced to
    #: a median here so the aggregate can show the spread that produced it.
    durations_ms: tuple[float, ...]

    @property
    def median_ms(self) -> float:
        """The middle repeat, which is the reading the protocol compares."""
        return statistics.median(self.durations_ms) if self.durations_ms else 0.0


@dataclasses.dataclass(frozen=True)
class ConfigurationResult:
    """Everything observed for one configuration, over the whole corpus."""

    configuration: str
    runs: tuple[ScenarioRun, ...]

    @property
    def mean_recall(self) -> float:
        """The primary metric: mean required-fact recall across scenarios.

        A mean over scenarios rather than over required facts, so a scenario
        with eight required facts does not outvote four scenarios with two.
        """
        if not self.runs:
            return 0.0
        return sum(r.score.recall for r in self.runs) / len(self.runs)

    @property
    def mean_precision(self) -> float:
        """Secondary. An arm can raise this by serving less, so it gates nothing."""
        if not self.runs:
            return 0.0
        return sum(r.score.precision for r in self.runs) / len(self.runs)

    @property
    def median_latency_ms(self) -> float:
        """Median of the per-scenario medians. Repeated-run, as the protocol froze."""
        medians = [r.median_ms for r in self.runs]
        return statistics.median(medians) if medians else 0.0

    @property
    def slowest_scenario_median_ms(self) -> float:
        """The documented tail: descriptive, and it gates nothing."""
        return max((r.median_ms for r in self.runs), default=0.0)

    @property
    def safety_failures(self) -> tuple[judge.ScenarioScore, ...]:
        """Every scenario that served something it should not have. All of them.

        Not a sample. Sampling a risk review is a resourcing decision; sampling
        safety failures is deciding not to look at some of them.
        """
        return tuple(r.score for r in self.runs if r.score.violations)

    @property
    def errored_scenarios(self) -> tuple[str, ...]:
        """Scenarios the system failed on. Still scored, still in the batch."""
        return tuple(r.score.scenario_id for r in self.runs if r.score.errored)


@dataclasses.dataclass(frozen=True)
class BatchResult:
    """One complete batch, and the freeze it was collected under.

    Carries no verdict. Which branch these numbers fall into is a separate,
    human-owned reading, and a `BatchResult` that named one would make the
    reading look like an output of the measurement.
    """

    collected_under: FrozenProtocol
    corpus_digest: str
    results: tuple[ConfigurationResult, ...]
    human_risk_sample: tuple[str, ...]

    def by_configuration(self, configuration: str) -> ConfigurationResult:
        """One configuration's result, or a `KeyError` naming what was asked for."""
        for result in self.results:
            if result.configuration == configuration:
                return result
        raise KeyError(f"the batch has no result for configuration {configuration!r}")


def human_risk_sample(corpus: Corpus, *, size: int = HUMAN_RISK_SAMPLE_SIZE) -> tuple[str, ...]:
    """The scenarios drawn for human risk review.

    Split evenly across the two scenario kinds and spaced evenly through each,
    from scenario ids sorted rather than from corpus order. Deterministic on
    purpose: a random sample would make two runs of an unchanged system produce
    different review sets, and "which scenarios did a human read" is part of the
    evidence rather than an implementation detail of the run.
    """
    per_kind = max(1, size // len(SCENARIO_COUNTS))
    drawn: list[str] = []
    for kind in sorted(SCENARIO_COUNTS):
        ids = sorted(s.scenario_id for s in corpus.by_kind(kind))
        if not ids:
            continue
        stride = max(1, len(ids) // per_kind)
        drawn.extend(ids[::stride][:per_kind])
    return tuple(drawn[:size])


@dataclasses.dataclass
class _ArmFailure:
    """Where an arm's exception is left for the harness to find.

    The assembler catches everything an arm raises, on purpose: one arm must not
    take down the response. That contract is right for a request path and blind
    for this one -- a database that fell over and a workspace that is genuinely
    empty both arrive here as a block with no items, and the whole eligibility
    rule turns on telling them apart. So the arm is wrapped to record what it
    raised on the way past, and the exception still propagates into the
    assembler, which still fails the block exactly as it would have.
    """

    error: BaseException | None = None


def _recording(arm: ContextArm, sink: _ArmFailure) -> ContextArm:
    async def wrapped() -> ArmOutcome:
        try:
            return await arm()
        except BaseException as exc:
            sink.error = exc
            raise

    return wrapped


def _workspace_failed(envelope: ContextEnvelopeV1) -> bool:
    return any(block.name == BLOCK_WORKSPACE and block.state == BLOCK_FAILED for block in envelope.blocks)


async def _resolve_once(
    *,
    scenario: Scenario,
    configuration: str,
    source: WorkspaceSource,
    other_arms: Mapping[str, ContextArm],
    embedder: Embedder | None,
    now: datetime.datetime,
) -> tuple[ContextEnvelopeV1, _ArmFailure]:
    sink = _ArmFailure()
    arms = dict(other_arms)
    arms[BLOCK_WORKSPACE] = _recording(
        workspace_arm_for(configuration, scenario=scenario, source=source, embedder=embedder), sink
    )
    return (await assemble(arms, now=now)).envelope, sink


async def run_scenario(
    *,
    scenario: Scenario,
    configuration: str,
    source: WorkspaceSource,
    other_arms: Mapping[str, ContextArm],
    embedder: Embedder | None,
    now: datetime.datetime,
    repeats: int = LATENCY_REPEATS,
) -> ScenarioRun:
    """Resolve one scenario `repeats` times, score the first, time all of them.

    Scoring the first rather than each is not a shortcut: the system is
    deterministic over a frozen corpus, so the later resolutions exist to
    measure latency and would contribute identical scores. If they ever did not,
    that is a defect in the determinism this whole design rests on, and the
    conformance suite is where it is caught -- not silently averaged away here.
    """
    durations: list[float] = []
    envelope: ContextEnvelopeV1 | None = None
    errored = False

    for attempt in range(max(1, repeats)):
        started = time.perf_counter()
        resolved, failure = await _resolve_once(
            scenario=scenario,
            configuration=configuration,
            source=source,
            other_arms=other_arms,
            embedder=embedder,
            now=now,
        )
        durations.append((time.perf_counter() - started) * 1000.0)

        if isinstance(failure.error, InfrastructureError):
            # Not this scenario's result, and not any scenario's. Straight up.
            raise failure.error
        if failure.error is not None or _workspace_failed(resolved):
            # The arm raised, timed out, or returned something the block
            # contract refuses. All three are the system under test failing to
            # answer, which is a failure for this configuration -- never an
            # exclusion, and never a truthful empty.
            errored = True
            break

        if attempt == 0:
            envelope = resolved

    return ScenarioRun(
        score=judge.score(
            scenario_id=scenario.scenario_id,
            configuration=configuration,
            envelope=None if errored else envelope,
            required_item_keys=scenario.required_item_keys,
            relevant_item_keys=scenario.relevant_item_keys,
            facts=scenario.facts,
            errored=errored,
        ),
        durations_ms=tuple(durations),
    )


async def run_batch(
    *,
    corpus: Corpus,
    source: WorkspaceSource,
    other_arms: OtherArms,
    embedder: Embedder | None,
    now: datetime.datetime,
    configurations: Sequence[str] = CONFIGURATIONS,
    repeats: int = LATENCY_REPEATS,
    judge_source: Path | None = None,
) -> BatchResult:
    """Run every configuration over every scenario, or run nothing.

    The freeze is captured before the first observation and travels with the
    result, so the check that the protocol did not move afterwards has something
    to compare against. An empty configuration list is refused rather than
    returning an empty batch: a run that measured nothing and a run that
    measured everything and found nothing are different results, and only one of
    them is worth reporting.
    """
    if not configurations:
        raise BatchInvalidated("a batch with no configuration measured nothing; it is not a smaller batch")

    collected_under = freeze(judge_source=judge_source)

    results: list[ConfigurationResult] = []
    for configuration in configurations:
        runs: list[ScenarioRun] = []
        for scenario in corpus.scenarios:
            try:
                arms = other_arms(scenario)
            except InfrastructureError as exc:
                raise BatchInvalidated(
                    f"infrastructure failed while preparing {scenario.scenario_id} under {configuration}: {exc}; "
                    "the batch is rerun whole"
                ) from exc
            try:
                runs.append(
                    await run_scenario(
                        scenario=scenario,
                        configuration=configuration,
                        source=source,
                        other_arms=arms,
                        embedder=embedder,
                        now=now,
                        repeats=repeats,
                    )
                )
            except InfrastructureError as exc:
                raise BatchInvalidated(
                    f"infrastructure failed during {scenario.scenario_id} under {configuration}: {exc}; "
                    "the batch is rerun whole"
                ) from exc
        results.append(ConfigurationResult(configuration=configuration, runs=tuple(runs)))

    return BatchResult(
        collected_under=collected_under,
        corpus_digest=corpus.digest,
        results=tuple(results),
        human_risk_sample=human_risk_sample(corpus),
    )


__all__ = [
    "BatchInvalidated",
    "BatchResult",
    "ConfigurationResult",
    "InfrastructureError",
    "OtherArms",
    "ScenarioRun",
    "human_risk_sample",
    "run_batch",
    "run_scenario",
]
