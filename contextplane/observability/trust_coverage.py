"""Trust-label coverage: how much of what we returned says where it came from.

Coverage is not a quality score and not an average of trust levels. It answers
one question -- of the items a resolution handed to an agent, how many carry an
account of their own provenance? An item without one is not "less trustworthy";
it is unattributable, and an agent cannot discount weight it cannot see.

**Canonical items are excluded from the denominator, not counted as covered.**
The canonical block carries no trust metadata by contract: it is the registry's
own answer, and attaching an attribution would invite the question of whether
some other authority could have supplied it. Counting canonical items as covered
would pad the ratio with items that were never in question. Counting them as
uncovered would put 100% permanently out of reach, and a target that can never
be met is indistinguishable from no target -- the alert would be on from the
first request and every operator would learn to ignore it.

**Why measure at all, when the schema already refuses unlabelled items.**
`TrustMetadataV1` will not construct incomplete, and `ContextBlockV1` refuses a
non-canonical item that carries none -- failing that arm's whole block rather
than dropping the item, because a block returning its other items would read as
complete when it is not. So coverage over anything assembled through those two
types is 100% by construction, and this module asserts that rather than
discovering it.

That is the measurement's job, not a reason to skip it. It is not re-checking
the invariant; it is checking that the invariant was *in the path*. A future
recall surface that builds a response without going through `ContextBlockV1`
produces unlabelled output that every existing test still passes. This is the
thing that would notice, and the counting branch is kept exercised for exactly
that day.

**Coverage is reported, not enforced, and the distinction is deliberate.**
Refusing a response because one item lost its label would turn a provenance gap
into an outage. The response goes out, the gap is counted, and an operator is
told. Admission enforcement is a different mechanism with a different owner.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import structlog
from prometheus_client import Counter, Gauge

from contextplane.context.schemas.envelope import BLOCK_CANONICAL, BLOCK_FAILED

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from contextplane.context.assembler import SelectionEvidence
    from contextplane.context.schemas.envelope import ContextEnvelopeV1

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

#: Items that went out carrying a complete trust label, and items that did not.
#: A counter pair rather than a ratio gauge alone, because a ratio cannot
#: distinguish "no gaps" from "no traffic" once it is scraped and averaged.
TRUST_LABELLED_ITEMS_TOTAL = Counter(
    "contextplane_context_trust_labelled_items_total",
    "Non-canonical context items returned, split by whether they carried a trust label.",
    ["block", "labelled"],
)

#: The most recent resolution's coverage. Ratio in [0, 1]; 1 is the only
#: acceptable steady state, so alert on `< 1` rather than on a threshold that
#: would need choosing.
TRUST_LABEL_COVERAGE = Gauge(
    "contextplane_context_trust_label_coverage",
    "Fraction of eligible context items in the last resolution that carried a trust label.",
)

#: Resolutions that returned at least one unlabelled item. The alerting signal:
#: any increase is a provenance gap that reached a caller.
TRUST_LABEL_GAPS_TOTAL = Counter(
    "contextplane_context_trust_label_gaps_total",
    "Resolutions that returned at least one eligible item with no trust label.",
)

#: Arms reported in each of the states retrieval must distinguish. One counter
#: with a state label rather than five counters, so a dashboard can show them
#: together and a new state does not need a new metric.
ARM_STATES_TOTAL = Counter(
    "contextplane_context_arm_states_total",
    "Arm outcomes reported by retrieval, by block and reported state.",
    ["block", "state"],
)


# ---------------------------------------------------------------------------
# The states retrieval must report
# ---------------------------------------------------------------------------

#: An arm whose data is older than the freshness bound the request asked for.
ARM_STALE = "stale"
#: An arm that withheld something it found.
ARM_EXCLUDED = "excluded"
#: An arm that stopped short, at its own limit or the assembler's cap.
ARM_TRUNCATED = "truncated"
#: An arm that did not answer in time.
ARM_TIMED_OUT = "timed_out"
#: An arm that could not answer at all.
ARM_FAILED = "failed"
#: An arm that answered whole. Not one of the five reportable degradations, but
#: named so the vocabulary is closed and a caller can switch on it exhaustively.
ARM_OK = "ok"

#: The five states the reliability contract requires retrieval to distinguish.
#: `ARM_OK` is deliberately not in here: this is the set that means "something
#: was less than whole", and an operator filtering on it wants exactly these.
REPORTABLE_ARM_STATES: frozenset[str] = frozenset({ARM_STALE, ARM_EXCLUDED, ARM_TRUNCATED, ARM_TIMED_OUT, ARM_FAILED})


@dataclasses.dataclass(frozen=True)
class UnlabelledItem:
    """One item that reached a caller with no account of where it came from.

    Carries the block and the item id rather than the payload: the payload is
    the thing whose provenance is unknown, and copying it into a log line is how
    unclassified content ends up somewhere with a different retention policy.
    """

    block: str
    receipt_item_id: str


@dataclasses.dataclass(frozen=True)
class TrustCoverage:
    """What one resolution's labelling looked like.

    `eligible` excludes canonical items. `eligible == 0` is a complete
    resolution, not a degenerate one -- a request answered entirely from the
    canonical block has nothing to label, and reporting it as 0% would be false.
    """

    eligible: int
    labelled: int
    unlabelled: tuple[UnlabelledItem, ...]

    @property
    def ratio(self) -> float:
        """Fraction labelled, where a resolution with nothing to label is whole."""
        if self.eligible == 0:
            return 1.0
        return self.labelled / self.eligible

    def is_complete(self) -> bool:
        """True when every eligible item carried a label."""
        return not self.unlabelled


@dataclasses.dataclass(frozen=True)
class ArmReport:
    """What one arm did, in the vocabulary an operator filters on.

    `states` is a set rather than a single value because the states are not
    mutually exclusive: an arm can be stale *and* truncated, and collapsing that
    to one label would hide whichever the collapsing rule ranked lower.
    """

    block: str
    states: frozenset[str]
    considered: int
    returned: int
    excluded: int
    duration_ms: int

    def is_whole(self) -> bool:
        """True when nothing about this arm needs reporting."""
        return not self.states


def measure_envelope(envelope: ContextEnvelopeV1) -> TrustCoverage:
    """Count labelled and unlabelled items across every block of one envelope.

    Failed blocks contribute nothing because they carry no items by
    construction; they are a retrieval failure, not a labelling failure, and
    counting them here would make an outage look like a provenance problem.
    """
    eligible = 0
    labelled = 0
    unlabelled: list[UnlabelledItem] = []

    for block in envelope.blocks:
        if block.name == BLOCK_CANONICAL or block.state == BLOCK_FAILED:
            continue
        for item in block.items:
            eligible += 1
            if item.trust is not None:
                labelled += 1
            else:
                unlabelled.append(UnlabelledItem(block=block.name, receipt_item_id=str(item.receipt_item_id)))

    return TrustCoverage(eligible=eligible, labelled=labelled, unlabelled=tuple(unlabelled))


def report_arms(evidence: Sequence[SelectionEvidence]) -> tuple[ArmReport, ...]:
    """Translate assembly's evidence into the reportable arm states.

    The evidence records facts -- stale, truncated by whom, how many withheld.
    This is where those facts become the words the reliability contract uses, in
    one place, so a dashboard and an alert cannot disagree about what "degraded"
    covered.
    """
    reports: list[ArmReport] = []
    for arm in evidence:
        states: set[str] = set()
        if arm.state == BLOCK_FAILED:
            states.add(ARM_TIMED_OUT if _is_timeout(arm) else ARM_FAILED)
        if arm.stale:
            states.add(ARM_STALE)
        if arm.truncated_by_arm or arm.truncated_by_cap:
            states.add(ARM_TRUNCATED)
        if arm.exclusions:
            states.add(ARM_EXCLUDED)
        reports.append(
            ArmReport(
                block=arm.block,
                states=frozenset(states),
                considered=arm.considered,
                returned=arm.returned,
                excluded=len(arm.exclusions),
                duration_ms=arm.duration_ms,
            )
        )
    return tuple(reports)


def _is_timeout(arm: SelectionEvidence) -> bool:
    """Whether a failed arm ran out of time rather than breaking.

    Reads the structured flag rather than inferring the state from the failure
    reason. That text is written for a human, and recovering a machine state by
    matching English means the day somebody rewords the message, every timeout
    silently becomes an ordinary failure and no test notices.
    """
    return arm.timed_out


def observe(coverage: TrustCoverage, arms: Sequence[ArmReport] = ()) -> None:
    """Publish one resolution's coverage and arm states, and alert on a gap.

    Called for its side effects on the metric registry and the log. Separated
    from `measure_envelope` so a caller can measure without emitting -- a test
    asserting on coverage should not move a process-wide counter.
    """
    for arm in arms:
        for state in arm.states:
            ARM_STATES_TOTAL.labels(block=arm.block, state=state).inc()

    TRUST_LABEL_COVERAGE.set(coverage.ratio)
    if coverage.labelled:
        _inc_labelled(coverage)
    if coverage.is_complete():
        return

    TRUST_LABEL_GAPS_TOTAL.inc()
    for item in coverage.unlabelled:
        TRUST_LABELLED_ITEMS_TOTAL.labels(block=item.block, labelled="false").inc()

    # Warning rather than error: the response was served and the caller is not
    # stuck. It is an integrity gap to chase, not an incident in progress.
    _log.warning(
        "context.trust_label_gap",
        eligible=coverage.eligible,
        labelled=coverage.labelled,
        coverage=round(coverage.ratio, 4),
        blocks=sorted({item.block for item in coverage.unlabelled}),
        item_ids=[item.receipt_item_id for item in coverage.unlabelled],
    )


def _inc_labelled(coverage: TrustCoverage) -> None:
    """Count the labelled items, per block.

    The envelope is gone by this point, so the per-block split of the labelled
    majority is not recoverable; the aggregate is attributed to the blocks that
    were eligible. Unlabelled items are counted individually above, with their
    real block, because those are the ones anybody will go looking for.
    """
    TRUST_LABELLED_ITEMS_TOTAL.labels(block="_all", labelled="true").inc(coverage.labelled)


__all__ = [
    "ARM_EXCLUDED",
    "ARM_FAILED",
    "ARM_OK",
    "ARM_STALE",
    "ARM_TIMED_OUT",
    "ARM_TRUNCATED",
    "REPORTABLE_ARM_STATES",
    "ArmReport",
    "TrustCoverage",
    "UnlabelledItem",
    "measure_envelope",
    "observe",
    "report_arms",
]
