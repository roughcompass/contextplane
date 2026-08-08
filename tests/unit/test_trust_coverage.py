"""The counting behind trust-label coverage and arm reporting.

Pure logic, no database and no assembly: this file feeds the module the shapes
it will meet and checks the arithmetic and the vocabulary. The conformance suite
proves the same module against real assembled output, which is a different
question -- that one asks whether the invariant is in the path, this one asks
whether the counter can count.

The two divide on purpose. Coverage arithmetic has boundary cases that a real
envelope cannot produce (nothing eligible, everything unlabelled), and a test
that can only reach them through a full assembly is a test that will be deleted
the first time assembly changes.
"""

from __future__ import annotations

import dataclasses
import datetime

import pytest
from prometheus_client import REGISTRY

from contextplane.context.assembler import Exclusion, SelectionEvidence
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_FAILED,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
    ContextItemV1,
)
from contextplane.context.schemas.trust import ReceiptItemIdV1, TrustMetadataV1
from contextplane.observability.trust_coverage import (
    ARM_EXCLUDED,
    ARM_FAILED,
    ARM_OK,
    ARM_STALE,
    ARM_TIMED_OUT,
    ARM_TRUNCATED,
    REPORTABLE_ARM_STATES,
    ArmReport,
    TrustCoverage,
    UnlabelledItem,
    measure_envelope,
    observe,
    report_arms,
)

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)


def _trust() -> TrustMetadataV1:
    return TrustMetadataV1(
        trust="observed",
        source="probe",
        assertion_kind="fact",
        authority="agent-a",
        freshness=None,
        mutability="mutable",
        attribution=None,
        classification="internal",
    )


def _item(block: str, key: str, *, labelled: bool) -> ContextItemV1:
    return ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=block, source="probe", item_key=key),
        payload={"key": key},
        trust=_trust() if labelled else None,
    )


@dataclasses.dataclass(frozen=True)
class _Block:
    """The shape `measure_envelope` reads.

    A stand-in rather than a `ContextBlockV1`, because the real block refuses an
    unlabelled non-canonical item -- so the gap this module exists to count
    cannot be built out of the real type. Using the real one here would mean
    only ever testing the case that cannot fail.
    """

    name: str
    state: str
    items: tuple[ContextItemV1, ...] = ()


@dataclasses.dataclass(frozen=True)
class _Envelope:
    blocks: tuple[_Block, ...]


def _evidence(block: str, **overrides: object) -> SelectionEvidence:
    base: dict[str, object] = {
        "block": block,
        "state": "success",
        "considered": 1,
        "returned": 1,
        "exclusions": (),
        "truncated_by_cap": False,
        "truncated_by_arm": False,
        "fresh_as_of": None,
        "stale": False,
        "duration_ms": 3,
        "timed_out": False,
    }
    base.update(overrides)
    return SelectionEvidence(**base)  # type: ignore[arg-type]


def _measure(*blocks: _Block) -> TrustCoverage:
    return measure_envelope(_Envelope(blocks=blocks))  # type: ignore[arg-type]


# --- The ratio ----------------------------------------------------------------


def test_a_fully_labelled_resolution_is_whole() -> None:
    coverage = _measure(
        _Block(BLOCK_ARC, "success", (_item(BLOCK_ARC, "a", labelled=True),)),
        _Block(BLOCK_WORKSPACE, "success", (_item(BLOCK_WORKSPACE, "w", labelled=True),)),
    )

    assert coverage.eligible == 2
    assert coverage.labelled == 2
    assert coverage.ratio == 1.0
    assert coverage.is_complete()


def test_nothing_eligible_is_whole_rather_than_zero_percent() -> None:
    """A request answered entirely from the canonical block has nothing to
    label. Reporting that as 0% covered would be false, and would put the alert
    on for a resolution that did nothing wrong."""
    coverage = _measure(
        _Block(BLOCK_CANONICAL, "success", (_item(BLOCK_CANONICAL, "c", labelled=False),)),
        _Block(BLOCK_ARC, "empty"),
    )

    assert coverage.eligible == 0
    assert coverage.ratio == 1.0
    assert coverage.is_complete()


def test_canonical_items_never_enter_either_side_of_the_ratio() -> None:
    """Not counted as covered, not counted as uncovered. Counting them as
    covered would pad the number with items that were never in question."""
    coverage = _measure(
        _Block(BLOCK_CANONICAL, "success", tuple(_item(BLOCK_CANONICAL, f"c{n}", labelled=False) for n in range(9))),
        _Block(BLOCK_ARC, "success", (_item(BLOCK_ARC, "a", labelled=True),)),
    )

    assert coverage.eligible == 1, "nine canonical items must not dilute one eligible item"
    assert coverage.ratio == 1.0


def test_a_partial_gap_is_reported_as_a_fraction() -> None:
    coverage = _measure(
        _Block(
            BLOCK_WORKSPACE,
            "success",
            (
                _item(BLOCK_WORKSPACE, "w1", labelled=True),
                _item(BLOCK_WORKSPACE, "w2", labelled=False),
                _item(BLOCK_WORKSPACE, "w3", labelled=True),
                _item(BLOCK_WORKSPACE, "w4", labelled=True),
            ),
        ),
    )

    assert coverage.eligible == 4
    assert coverage.labelled == 3
    assert coverage.ratio == 0.75
    assert not coverage.is_complete()


def test_a_gap_names_its_block_and_id_and_carries_no_payload() -> None:
    """The id and block are enough to find the item. The payload is the content
    whose handling class is unknown, and copying it into a log is how
    unclassified material lands somewhere with a different retention policy."""
    leaked = _item(BLOCK_WORKSPACE, "w-leak", labelled=False)

    coverage = _measure(_Block(BLOCK_WORKSPACE, "success", (leaked,)))

    assert coverage.unlabelled == (UnlabelledItem(block=BLOCK_WORKSPACE, receipt_item_id=str(leaked.receipt_item_id)),)
    assert not hasattr(coverage.unlabelled[0], "payload")


def test_a_failed_block_contributes_to_neither_side() -> None:
    """A failed arm carries no items by construction. Counting it as a
    labelling gap would make an outage look like a provenance problem."""
    coverage = _measure(
        _Block(BLOCK_ARC, BLOCK_FAILED, (_item(BLOCK_ARC, "a", labelled=False),)),
        _Block(BLOCK_WORKSPACE, "success", (_item(BLOCK_WORKSPACE, "w", labelled=True),)),
    )

    assert coverage.eligible == 1
    assert coverage.is_complete()


def test_an_empty_envelope_is_whole() -> None:
    assert _measure().is_complete()
    assert _measure().ratio == 1.0


# --- Arm states ---------------------------------------------------------------


def test_a_whole_arm_reports_nothing() -> None:
    report = report_arms([_evidence(BLOCK_ARC)])[0]

    assert report.states == frozenset()
    assert report.is_whole()
    assert report.excluded == 0
    assert report.duration_ms == 3


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"stale": True}, ARM_STALE),
        ({"truncated_by_cap": True}, ARM_TRUNCATED),
        ({"truncated_by_arm": True}, ARM_TRUNCATED),
        ({"exclusions": (Exclusion(item_key="k", reason="withheld"),)}, ARM_EXCLUDED),
        ({"state": BLOCK_FAILED}, ARM_FAILED),
        ({"state": BLOCK_FAILED, "timed_out": True}, ARM_TIMED_OUT),
    ],
)
def test_each_fact_maps_to_its_reported_state(overrides: dict[str, object], expected: str) -> None:
    report = report_arms([_evidence(BLOCK_WORKSPACE, **overrides)])[0]

    assert expected in report.states


def test_both_kinds_of_truncation_report_the_same_state() -> None:
    """An arm stopping at its own limit and the assembler capping it are
    different causes with the same consequence for a reader: there was more."""
    by_arm = report_arms([_evidence(BLOCK_WORKSPACE, truncated_by_arm=True)])[0]
    by_cap = report_arms([_evidence(BLOCK_WORKSPACE, truncated_by_cap=True)])[0]

    assert by_arm.states == by_cap.states == frozenset({ARM_TRUNCATED})


def test_a_timeout_replaces_the_generic_failure_rather_than_joining_it() -> None:
    """Reporting both would put one arm in two buckets on a dashboard that
    counts failures, and double it."""
    report = report_arms([_evidence(BLOCK_ARC, state=BLOCK_FAILED, timed_out=True)])[0]

    assert report.states == frozenset({ARM_TIMED_OUT})
    assert ARM_FAILED not in report.states


def test_states_combine_rather_than_collapse() -> None:
    """Stale and truncated are both true of the same arm, and a reader needs
    both: one says the data is old, the other says there is more of it."""
    report = report_arms(
        [
            _evidence(
                BLOCK_WORKSPACE,
                stale=True,
                truncated_by_cap=True,
                exclusions=(Exclusion(item_key="k", reason="withheld"),),
            )
        ]
    )[0]

    assert report.states == frozenset({ARM_STALE, ARM_TRUNCATED, ARM_EXCLUDED})
    assert not report.is_whole()


def test_the_counts_travel_with_the_report() -> None:
    """ "Truncated" without the numbers tells a reader something was dropped and
    not how much, which is not actionable."""
    report = report_arms(
        [
            _evidence(
                BLOCK_OBSERVED_CLAIMS,
                considered=40,
                returned=5,
                truncated_by_cap=True,
                exclusions=(Exclusion(item_key="a", reason="r"), Exclusion(item_key="b", reason="r")),
            )
        ]
    )[0]

    assert (report.considered, report.returned, report.excluded) == (40, 5, 2)


def test_every_arm_gets_a_report_in_order() -> None:
    reports = report_arms([_evidence(BLOCK_CANONICAL), _evidence(BLOCK_ARC), _evidence(BLOCK_WORKSPACE)])

    assert [report.block for report in reports] == [BLOCK_CANONICAL, BLOCK_ARC, BLOCK_WORKSPACE]


def test_no_evidence_reports_nothing() -> None:
    assert report_arms([]) == ()


def test_the_reportable_set_is_the_five_degradations_and_excludes_ok() -> None:
    """`ARM_OK` names the whole case so a caller can switch exhaustively, but an
    operator filtering for "something was less than whole" must not get every
    healthy arm back."""
    assert REPORTABLE_ARM_STATES == {ARM_STALE, ARM_EXCLUDED, ARM_TRUNCATED, ARM_TIMED_OUT, ARM_FAILED}
    assert ARM_OK not in REPORTABLE_ARM_STATES


# --- Publishing ---------------------------------------------------------------


def _gauge() -> float | None:
    return REGISTRY.get_sample_value("contextplane_context_trust_label_coverage")


def _gaps() -> float:
    return REGISTRY.get_sample_value("contextplane_context_trust_label_gaps_total") or 0.0


def _arm_state_count(block: str, state: str) -> float:
    return REGISTRY.get_sample_value("contextplane_context_arm_states_total", {"block": block, "state": state}) or 0.0


def test_a_whole_resolution_publishes_full_coverage_and_raises_no_gap() -> None:
    before = _gaps()

    observe(_measure(_Block(BLOCK_ARC, "success", (_item(BLOCK_ARC, "a", labelled=True),))))

    assert _gauge() == 1.0
    assert _gaps() == before, "a whole resolution must not move the alerting counter"


def test_a_gap_moves_the_alerting_counter_and_the_gauge() -> None:
    before = _gaps()

    observe(_measure(_Block(BLOCK_WORKSPACE, "success", (_item(BLOCK_WORKSPACE, "w", labelled=False),))))

    assert _gauge() == 0.0
    assert _gaps() == before + 1


def test_arm_states_are_counted_per_block_and_state() -> None:
    before = _arm_state_count(BLOCK_OBSERVED_CLAIMS, ARM_STALE)

    observe(
        _measure(),
        report_arms([_evidence(BLOCK_OBSERVED_CLAIMS, stale=True, truncated_by_arm=True)]),
    )

    assert _arm_state_count(BLOCK_OBSERVED_CLAIMS, ARM_STALE) == before + 1
    assert _arm_state_count(BLOCK_OBSERVED_CLAIMS, ARM_TRUNCATED) >= 1


def test_a_whole_arm_publishes_no_state_at_all() -> None:
    """An arm that answered must not appear under any state, including a
    zero-valued one -- an operator scanning the metric should see only arms that
    need attention."""
    before = {state: _arm_state_count(BLOCK_CANONICAL, state) for state in REPORTABLE_ARM_STATES}

    observe(_measure(), report_arms([_evidence(BLOCK_CANONICAL)]))

    assert all(_arm_state_count(BLOCK_CANONICAL, state) == value for state, value in before.items())


def test_observing_nothing_is_safe() -> None:
    """Called on every resolution, including ones with no arms to report."""
    observe(_measure())

    assert _gauge() == 1.0


def test_a_report_can_be_built_directly_for_a_caller_that_has_no_evidence() -> None:
    """`ArmReport` is part of the module's surface, not only an internal shape."""
    report = ArmReport(
        block=BLOCK_ARC, states=frozenset({ARM_FAILED}), considered=0, returned=0, excluded=0, duration_ms=9
    )

    assert not report.is_whole()
    assert report.duration_ms == 9
