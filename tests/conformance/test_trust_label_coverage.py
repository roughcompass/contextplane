"""Trust-label coverage is 100%, the inventory is complete, and arms report five states.

Three contracts meet here, and they fail in different ways.

**The inventory is generated, so it cannot go stale.** A surface added tomorrow
is inventoried the moment it exists. The test that matters is not that the
current count is right -- it is that an unregistered surface fails the gate, so
this file plants one and checks the gate notices.

**Coverage counts what a caller received, not what assembly intended.** Assembly
already refuses a non-canonical item with no trust metadata, so coverage over
assembled output is 100% by construction. That is the point: this measures
whether the invariant was in the path, and it is the check that survives a
future surface that builds its own response.

**All five arm states are reachable.** Four were, for a while, and the fifth --
timed out -- existed only as English inside a reason string. A vocabulary entry
nothing can produce reads as implemented and is not, so each of the five is
produced here from a real assembly rather than by constructing the evidence by
hand.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
from pathlib import Path

import pytest

from contextplane.context.assembler import (
    ArmOutcome,
    Exclusion,
    SelectionEvidence,
    assemble,
    canonical_item,
    contextual_item,
)
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
    ContextItemV1,
)
from contextplane.context.schemas.trust import (
    TRUST_OBSERVED,
    ReceiptItemIdV1,
    TrustMetadataV1,
)
from contextplane.observability.trust_coverage import (
    ARM_EXCLUDED,
    ARM_FAILED,
    ARM_STALE,
    ARM_TIMED_OUT,
    ARM_TRUNCATED,
    REPORTABLE_ARM_STATES,
    measure_envelope,
    report_arms,
)
from scripts import check_surface_inventory

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _trust(*, freshness: datetime.datetime | None = None) -> TrustMetadataV1:
    return TrustMetadataV1(
        trust=TRUST_OBSERVED,
        source="test-source",
        assertion_kind="fact",
        authority="test-authority",
        freshness=freshness,
        mutability="mutable",
        attribution=None,
        classification="internal",
    )


def _item(block: str, key: str, *, trust: TrustMetadataV1 | None) -> ContextItemV1:
    """One item, labelled or not.

    `contextual_item` refuses a non-canonical item without trust -- which is the
    invariant under test -- so the unlabelled case is constructed directly. That
    is the only way to produce the shape a surface bypassing assembly would
    emit, and a coverage check that cannot be shown a gap proves nothing.
    """
    if trust is None:
        return ContextItemV1(
            receipt_item_id=ReceiptItemIdV1(block=block, source="probe", item_key=key),
            payload={"key": key},
            trust=None,
        )
    return contextual_item(block=block, source="probe", item_key=key, payload={"key": key}, trust=trust)


def _canonical(key: str = "cap-1") -> ContextItemV1:
    return canonical_item(source="catalog", item_key=key, payload={"name": key})


def _unlabelled(block: str, key: str) -> ContextItemV1:
    """An item with no trust metadata. No block will accept one; see the backstop test."""
    return ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=block, source="probe", item_key=key),
        payload={"key": key},
        trust=None,
    )


@dataclasses.dataclass(frozen=True)
class _StubBlock:
    """The shape `measure_envelope` reads, without the schema's refusal.

    Not a mock of the block contract -- a stand-in for a hypothetical future
    surface that builds a response without going through `ContextBlockV1`, which
    is the only way the coverage counter can ever see a gap.
    """

    name: str
    state: str
    items: tuple[ContextItemV1, ...]


def _arm(outcome: ArmOutcome):
    async def _run() -> ArmOutcome:
        return outcome

    return _run


def _all_arms(**overrides):
    arms = {
        BLOCK_CANONICAL: _arm(ArmOutcome(items=(_canonical(),))),
        BLOCK_ARC: _arm(ArmOutcome(items=(_item(BLOCK_ARC, "arc-1", trust=_trust()),))),
        BLOCK_OBSERVED_CLAIMS: _arm(ArmOutcome(items=(_item(BLOCK_OBSERVED_CLAIMS, "claim-1", trust=_trust()),))),
        BLOCK_WORKSPACE: _arm(ArmOutcome(items=(_item(BLOCK_WORKSPACE, "ws-1", trust=_trust()),))),
    }
    arms.update(overrides)
    return arms


def _evidence(block: str, **overrides) -> SelectionEvidence:
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
        "duration_ms": 1,
        "timed_out": False,
    }
    base.update(overrides)
    return SelectionEvidence(**base)  # type: ignore[arg-type]


# --- The generated inventory --------------------------------------------------


def test_every_surface_is_inventoried_or_reasoned_away() -> None:
    """The release gate itself. Fails naming any module in a surface family that
    is neither inventoried nor excluded with a reason."""
    assert check_surface_inventory.unregistered(_REPO_ROOT) == []
    assert check_surface_inventory.stale_exclusions(_REPO_ROOT) == []


def test_the_inventory_covers_both_kinds_of_surface() -> None:
    """An inventory of writes alone would miss the whole recall path, which is
    where labels are attached and therefore where they can go missing."""
    inventory = check_surface_inventory.build_inventory(_REPO_ROOT)
    kinds = {surface.kind for surface in inventory}

    assert kinds == {check_surface_inventory.RECALL, check_surface_inventory.PILOT_WRITE}
    assert len(inventory) > 50, "the families walk real directories; a near-empty result means a bad root"


def test_an_unregistered_surface_fails_the_gate(tmp_path: Path) -> None:
    """The test that makes the two above worth having.

    A gate that passes is only meaningful if it can fail, so this plants a
    module inside a narrowed family and checks the gate names it.
    """
    narrowed = next(family for family in check_surface_inventory.FAMILIES if family.members)
    planted = tmp_path / narrowed.root
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("")
    for member in narrowed.members:
        (planted / member).write_text("")
    (planted / "brand_new_surface.py").write_text("")

    findings = check_surface_inventory.unregistered(tmp_path)

    assert any("brand_new_surface.py" in finding for finding in findings)


def test_a_stale_exclusion_fails_the_gate(tmp_path: Path) -> None:
    """An exclusion list that only ever grows rots into a set of claims nobody
    re-reads. Every entry is re-checked against the tree."""
    assert check_surface_inventory.stale_exclusions(
        tmp_path
    ), "against an empty tree every exclusion names a missing file, so the check must report them"


# --- Coverage -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_assembled_response_is_fully_labelled() -> None:
    """100% is the steady state, not an aspiration: assembly refuses an
    unlabelled non-canonical item, so anything that went through it is whole."""
    result = await assemble(_all_arms(), now=_NOW)

    coverage = measure_envelope(result.envelope)

    assert coverage.is_complete()
    assert coverage.ratio == 1.0
    assert coverage.eligible == 3, "three non-canonical items; the canonical one is not eligible"


@pytest.mark.asyncio
async def test_canonical_items_are_excluded_from_the_denominator() -> None:
    """Not counted as covered and not counted as uncovered.

    Counting them as covered would pad the ratio with items that were never in
    question. Counting them as uncovered would put 100% permanently out of
    reach, and an alert that is always on is the same as no alert.
    """
    canonical_only = {
        BLOCK_CANONICAL: _arm(ArmOutcome(items=(_canonical("a"), _canonical("b")))),
        BLOCK_ARC: _arm(ArmOutcome()),
        BLOCK_OBSERVED_CLAIMS: _arm(ArmOutcome()),
        BLOCK_WORKSPACE: _arm(ArmOutcome()),
    }
    result = await assemble(canonical_only, now=_NOW)

    coverage = measure_envelope(result.envelope)

    assert coverage.eligible == 0
    assert coverage.ratio == 1.0, "a resolution with nothing to label is whole, not 0% covered"
    assert coverage.is_complete()


def test_an_unlabelled_item_cannot_be_put_in_a_block_at_all() -> None:
    """Where 100% coverage is actually enforced, which is not here.

    The block schema refuses a non-canonical item with no trust metadata, so an
    unlabelled item never reaches an envelope and never reaches a caller. That
    makes coverage over any real envelope 1.0 by construction -- which is the
    contract being met, not the measurement being useless: it is what the
    measurement is asserting.
    """
    from contextplane.context.schemas.envelope import ContextBlockV1
    from contextplane.context.schemas.trust import InvalidContextItem

    with pytest.raises(InvalidContextItem, match="no trust metadata"):
        ContextBlockV1(
            name=BLOCK_WORKSPACE,
            state="success",
            items=(_item(BLOCK_WORKSPACE, "ws-leak", trust=None),),
        )


@pytest.mark.asyncio
async def test_an_arm_offering_an_unlabelled_item_fails_rather_than_returning_it() -> None:
    """The runtime path a labelling gap actually takes.

    The arm's whole block fails. That is deliberately not a partial success:
    returning the arm's other items while dropping the unlabelled one would hand
    back a block that reads as complete and is not.
    """
    arms = _all_arms(
        **{
            BLOCK_WORKSPACE: _arm(
                ArmOutcome(
                    items=(
                        _item(BLOCK_WORKSPACE, "ws-good", trust=_trust()),
                        _item(BLOCK_WORKSPACE, "ws-leak", trust=None),
                    )
                )
            )
        }
    )

    result = await assemble(arms, now=_NOW)

    workspace = result.envelope.block(BLOCK_WORKSPACE)
    assert workspace.state == BLOCK_FAILED
    assert workspace.items == (), "a failed arm carries nothing, including the items that were fine"
    assert measure_envelope(
        result.envelope
    ).is_complete(), "no unlabelled item reached the caller, which is the property coverage reports"


def test_the_coverage_counter_reports_a_gap_when_shown_one() -> None:
    """A backstop, exercised against a stand-in rather than a real envelope.

    The schema makes a gap unconstructible today, so there is no real envelope
    that produces one. This keeps the counting branch live: if a future recall
    surface assembles a response some other way, the branch it lands in has been
    executed at least once and is known to report the block and the id.
    """

    class _Stub:
        blocks = (
            _StubBlock(BLOCK_CANONICAL, "success", (_canonical(),)),
            _StubBlock(BLOCK_WORKSPACE, "success", (_unlabelled(BLOCK_WORKSPACE, "ws-leak"),)),
        )

    coverage = measure_envelope(_Stub())  # type: ignore[arg-type]

    assert not coverage.is_complete()
    assert coverage.eligible == 1, "the canonical item is not eligible, so it cannot dilute the gap"
    assert coverage.ratio == 0.0
    assert coverage.unlabelled[0].block == BLOCK_WORKSPACE
    assert not hasattr(
        coverage.unlabelled[0], "payload"
    ), "the report carries the id and the block; the payload is the content whose handling class is unknown"


@pytest.mark.asyncio
async def test_a_failed_block_is_not_counted_as_a_labelling_gap() -> None:
    """A failed arm carries no items by construction. Counting it here would
    make an outage look like a provenance problem and send whoever is on call to
    the wrong dashboard."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("down")

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _broken}), now=_NOW)

    coverage = measure_envelope(result.envelope)

    assert coverage.is_complete()
    assert coverage.eligible == 2, "the failed arm contributes nothing to either side of the ratio"


# --- The five arm states ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timed_out_arm_is_reported_as_timed_out_not_merely_failed() -> None:
    """The state that did not exist. "The arm is slow" and "the arm is broken"
    send an operator to different places."""

    async def _hangs() -> ArmOutcome:
        await asyncio.sleep(10)
        return ArmOutcome()

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _hangs}), now=_NOW, arm_timeout_s=0.01)

    reports = {report.block: report for report in report_arms(result.evidence)}

    assert ARM_TIMED_OUT in reports[BLOCK_WORKSPACE].states
    assert ARM_FAILED not in reports[BLOCK_WORKSPACE].states, "timed out is the narrower answer, so it replaces it"


@pytest.mark.asyncio
async def test_a_broken_arm_is_reported_as_failed_not_timed_out() -> None:
    """The pair that makes the previous assertion mean something."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("down")

    result = await assemble(_all_arms(**{BLOCK_ARC: _broken}), now=_NOW)

    reports = {report.block: report for report in report_arms(result.evidence)}

    assert ARM_FAILED in reports[BLOCK_ARC].states
    assert ARM_TIMED_OUT not in reports[BLOCK_ARC].states


@pytest.mark.asyncio
async def test_a_stale_arm_is_reported_as_stale() -> None:
    old = _NOW - datetime.timedelta(hours=2)
    arms = _all_arms(
        **{
            BLOCK_WORKSPACE: _arm(
                ArmOutcome(
                    items=(_item(BLOCK_WORKSPACE, "ws-1", trust=_trust(freshness=old)),),
                    fresh_as_of=old,
                )
            )
        }
    )

    result = await assemble(arms, now=_NOW, max_age_s=60)

    reports = {report.block: report for report in report_arms(result.evidence)}
    assert ARM_STALE in reports[BLOCK_WORKSPACE].states


@pytest.mark.asyncio
async def test_an_arm_that_withheld_something_is_reported_as_excluded() -> None:
    """The difference between "there was nothing" and "there was something you
    may not see"; only the second tells a reader to go and ask somebody."""
    arms = _all_arms(
        **{
            BLOCK_WORKSPACE: _arm(
                ArmOutcome(
                    items=(_item(BLOCK_WORKSPACE, "ws-1", trust=_trust()),),
                    exclusions=(Exclusion(item_key="ws-2", reason="no active grant"),),
                )
            )
        }
    )

    result = await assemble(arms, now=_NOW)

    reports = {report.block: report for report in report_arms(result.evidence)}
    assert ARM_EXCLUDED in reports[BLOCK_WORKSPACE].states
    assert reports[BLOCK_WORKSPACE].excluded == 1


@pytest.mark.asyncio
async def test_a_truncated_arm_is_reported_as_truncated() -> None:
    many = tuple(_item(BLOCK_WORKSPACE, f"ws-{n}", trust=_trust()) for n in range(5))
    arms = _all_arms(**{BLOCK_WORKSPACE: _arm(ArmOutcome(items=many))})

    result = await assemble(arms, now=_NOW, item_cap=2)

    reports = {report.block: report for report in report_arms(result.evidence)}
    assert ARM_TRUNCATED in reports[BLOCK_WORKSPACE].states
    assert reports[BLOCK_WORKSPACE].returned == 2
    assert reports[BLOCK_WORKSPACE].considered == 5


def test_all_five_reportable_states_are_produced_by_this_suite() -> None:
    """The completeness check on the four tests above.

    A state in the vocabulary that nothing can produce reads as implemented and
    is not. This asserts the set the contract names, so adding a sixth state
    without a test that reaches it fails here.
    """
    assert REPORTABLE_ARM_STATES == {ARM_STALE, ARM_EXCLUDED, ARM_TRUNCATED, ARM_TIMED_OUT, ARM_FAILED}


def test_an_arm_can_be_reported_in_more_than_one_state_at_once() -> None:
    """States are not mutually exclusive. Collapsing an arm that is both stale
    and truncated to one label would hide whichever the collapsing rule ranked
    lower, and which one that is would be nobody's deliberate decision."""
    both = _evidence(
        BLOCK_WORKSPACE,
        state="degraded",
        stale=True,
        truncated_by_cap=True,
        exclusions=(Exclusion(item_key="k", reason="withheld"),),
    )

    report = report_arms([both])[0]

    assert {ARM_STALE, ARM_TRUNCATED, ARM_EXCLUDED} <= report.states
    assert not report.is_whole()


def test_a_whole_arm_reports_no_states() -> None:
    """An arm that answered completely must not appear in an operator's filter
    for degradation."""
    report = report_arms([_evidence(BLOCK_ARC)])[0]

    assert report.states == frozenset()
    assert report.is_whole()


def test_every_block_name_can_be_reported() -> None:
    """report_arms is driven by the evidence, so a new block gets reporting for
    free -- this pins that rather than leaving it to be rediscovered."""
    reports = report_arms([_evidence(name) for name in sorted(BLOCK_NAMES)])

    assert {report.block for report in reports} == set(BLOCK_NAMES)


@pytest.mark.asyncio
async def test_a_failed_state_is_only_ever_reported_for_a_failed_block() -> None:
    """`timed_out` narrows a failure; it must never appear on an arm that
    answered."""
    result = await assemble(_all_arms(), now=_NOW)

    for arm, report in zip(result.evidence, report_arms(result.evidence), strict=True):
        assert arm.state != BLOCK_FAILED
        assert not ({ARM_FAILED, ARM_TIMED_OUT} & report.states)
