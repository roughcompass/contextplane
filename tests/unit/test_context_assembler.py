"""The four-block envelope, and every way an arm can fail to fill one.

Most of these are failure paths on purpose. The success path is exercised by
everything downstream and by every integration test; the paths that decide
whether a broken arm reads as an empty one are exercised nowhere else, and they
are the ones that turn a partial answer into a confidently wrong one.

The arms are planted rather than queried. That is the point of injecting them:
a timeout, a raised exception, a malformed item and a withheld row are all
one-line fixtures here and are nearly impossible to arrange against a database.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from contextplane.context.assembler import (
    ArmOutcome,
    Exclusion,
    assemble,
    canonical_item,
    contextual_item,
    ordered_items,
)
from contextplane.context.quality import derive_quality
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_DEGRADED,
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ENVELOPE_BLOCKED,
    ENVELOPE_COMPLETE,
    ENVELOPE_DEGRADED,
    ContextBlockV1,
    ContextItemV1,
)
from contextplane.context.schemas.trust import InvalidContextItem, TrustMetadataV1

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)


def _trust(*, freshness: datetime.datetime | None = None) -> TrustMetadataV1:
    return TrustMetadataV1(
        trust="observed",
        source="probe",
        assertion_kind="annotation",
        authority="agent-a",
        freshness=freshness,
        mutability="mutable",
        attribution="agent-a",
        classification="internal",
    )


def _canonical(key: str = "cap-1") -> ContextItemV1:
    return canonical_item(source="catalog", item_key=key, payload={"name": key})


def _contextual(block: str, key: str) -> ContextItemV1:
    return contextual_item(block=block, source="probe", item_key=key, payload={"k": key}, trust=_trust())


def _arm(outcome: ArmOutcome):
    async def _run() -> ArmOutcome:
        return outcome

    return _run


def _all_arms(**overrides):
    """Four healthy arms, with named ones replaced."""
    arms = {
        BLOCK_CANONICAL: _arm(ArmOutcome(items=(_canonical(),))),
        BLOCK_ARC: _arm(ArmOutcome(items=(_contextual(BLOCK_ARC, "arc-1"),))),
        BLOCK_OBSERVED_CLAIMS: _arm(ArmOutcome(items=(_contextual(BLOCK_OBSERVED_CLAIMS, "claim-1"),))),
        BLOCK_WORKSPACE: _arm(ArmOutcome(items=(_contextual(BLOCK_WORKSPACE, "cp-1"),))),
    }
    arms.update(overrides)
    return arms


# --- The shape of the answer --------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_blocks_are_present_in_the_fixed_order() -> None:
    """A caller that has to check whether a block exists gets that check wrong
    once, and the failure looks like missing data rather than a missing check."""
    result = await assemble(_all_arms(), now=_NOW)

    assert tuple(block.name for block in result.envelope.blocks) == BLOCK_NAMES


@pytest.mark.asyncio
async def test_a_healthy_resolution_is_complete_and_cacheable() -> None:
    result = await assemble(_all_arms(), now=_NOW)

    assert result.envelope.state == ENVELOPE_COMPLETE
    assert result.envelope.quality.cacheable
    assert result.envelope.quality.degraded_blocks == ()


@pytest.mark.asyncio
async def test_blocks_are_reported_in_order_regardless_of_which_arm_finished_first() -> None:
    """Ordering by completion would make two identical resolutions differ by
    timing alone, which is the kind of difference that makes a receipt look like
    it recorded something it did not."""

    async def _slow() -> ArmOutcome:
        await asyncio.sleep(0.02)
        return ArmOutcome(items=(_canonical(),))

    result = await assemble(_all_arms(**{BLOCK_CANONICAL: _slow}), now=_NOW)

    assert tuple(block.name for block in result.envelope.blocks) == BLOCK_NAMES
    assert result.envelope.block(BLOCK_CANONICAL).state == BLOCK_SUCCESS


# --- Empty is not failure -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_arm_with_nothing_to_say_is_empty_not_failed() -> None:
    """A subject with no workspace notes is a complete answer. Collapsing empty
    into failed would make every quiet arm look broken."""
    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(ArmOutcome())}), now=_NOW)

    assert result.envelope.block(BLOCK_WORKSPACE).state == BLOCK_EMPTY
    assert result.envelope.state == ENVELOPE_COMPLETE


@pytest.mark.asyncio
async def test_an_empty_arm_does_not_degrade_the_answer() -> None:
    result = await assemble(_all_arms(**{BLOCK_ARC: _arm(ArmOutcome())}), now=_NOW)

    assert result.envelope.quality.degraded_blocks == ()
    assert result.envelope.quality.cacheable


# --- Canonical failure blocks --------------------------------------------------


@pytest.mark.asyncio
async def test_a_canonical_failure_blocks_the_whole_response() -> None:
    """Serving the surrounding context without the thing it surrounds is not a
    partial answer, it is a misleading one."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("catalog is down")

    result = await assemble(_all_arms(**{BLOCK_CANONICAL: _broken}), now=_NOW)

    assert result.envelope.block(BLOCK_CANONICAL).state == BLOCK_FAILED
    assert result.envelope.state == ENVELOPE_BLOCKED


@pytest.mark.asyncio
async def test_a_blocked_response_still_carries_the_other_arms_states() -> None:
    """Blocked is a verdict on the answer, not an excuse to stop reporting. An
    operator needs to know whether the other three were also failing."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("catalog is down")

    result = await assemble(_all_arms(**{BLOCK_CANONICAL: _broken}), now=_NOW)

    assert result.envelope.block(BLOCK_ARC).state == BLOCK_SUCCESS
    assert result.envelope.block(BLOCK_WORKSPACE).state == BLOCK_SUCCESS


@pytest.mark.asyncio
async def test_a_degraded_canonical_arm_degrades_rather_than_blocks() -> None:
    """It answered, incompletely. Nothing downstream may treat it as whole, but
    refusing the response outright would throw away a real partial answer."""
    outcome = ArmOutcome(items=(_canonical(),), degraded_reason="secondary index unavailable")
    result = await assemble(_all_arms(**{BLOCK_CANONICAL: _arm(outcome)}), now=_NOW)

    assert result.envelope.block(BLOCK_CANONICAL).state == BLOCK_DEGRADED
    assert result.envelope.state == ENVELOPE_DEGRADED


# --- One arm cannot take down the response ------------------------------------


@pytest.mark.parametrize("block", [BLOCK_ARC, BLOCK_OBSERVED_CLAIMS, BLOCK_WORKSPACE])
@pytest.mark.asyncio
async def test_a_non_canonical_failure_degrades_but_does_not_block(block: str) -> None:
    async def _broken() -> ArmOutcome:
        raise RuntimeError("arm is down")

    result = await assemble(_all_arms(**{block: _broken}), now=_NOW)

    assert result.envelope.block(block).state == BLOCK_FAILED
    assert result.envelope.state == ENVELOPE_DEGRADED


@pytest.mark.asyncio
async def test_a_failed_arm_names_the_exception_type_it_hit() -> None:
    """ "The arm is broken" and "the arm is slow" send an operator to different
    places, so the reason has to distinguish them."""

    async def _broken() -> ArmOutcome:
        raise ValueError("bad row")

    result = await assemble(_all_arms(**{BLOCK_ARC: _broken}), now=_NOW)

    assert "ValueError" in (result.envelope.block(BLOCK_ARC).reason or "")


@pytest.mark.asyncio
async def test_a_slow_arm_times_out_without_delaying_the_others() -> None:
    """A per-arm timeout, so one arm's latency does not decide whether the other
    three are ever reported."""

    async def _hangs() -> ArmOutcome:
        await asyncio.sleep(10)
        return ArmOutcome()

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _hangs}), now=_NOW, arm_timeout_s=0.01)

    workspace = result.envelope.block(BLOCK_WORKSPACE)
    assert workspace.state == BLOCK_FAILED
    assert "within" in (workspace.reason or ""), "a timeout must read as a timeout, not as a generic failure"
    assert result.envelope.block(BLOCK_ARC).state == BLOCK_SUCCESS


@pytest.mark.asyncio
async def test_a_failed_arm_carries_no_items() -> None:
    """Partial output from a failed arm is the shape that gets read as complete."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("down")

    result = await assemble(_all_arms(**{BLOCK_ARC: _broken}), now=_NOW)

    assert result.envelope.block(BLOCK_ARC).items == ()


@pytest.mark.asyncio
async def test_an_arm_returning_an_item_with_no_trust_fails_that_arm_only() -> None:
    """The trust rule is enforced by the block contract, so a malformed item
    would otherwise raise out of assembly and take the other three arms with it."""
    untrusted = ContextItemV1(
        receipt_item_id=_contextual(BLOCK_ARC, "x").receipt_item_id,
        payload={},
        trust=None,
    )
    result = await assemble(_all_arms(**{BLOCK_ARC: _arm(ArmOutcome(items=(untrusted,)))}), now=_NOW)

    assert result.envelope.block(BLOCK_ARC).state == BLOCK_FAILED
    assert "unusable" in (result.envelope.block(BLOCK_ARC).reason or "")
    assert result.envelope.block(BLOCK_WORKSPACE).state == BLOCK_SUCCESS


@pytest.mark.asyncio
async def test_a_missing_arm_is_a_failed_block_not_an_absent_one() -> None:
    arms = _all_arms()
    del arms[BLOCK_OBSERVED_CLAIMS]

    result = await assemble(arms, now=_NOW)

    assert result.envelope.block(BLOCK_OBSERVED_CLAIMS).state == BLOCK_FAILED
    assert tuple(block.name for block in result.envelope.blocks) == BLOCK_NAMES


# --- Bounds -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_item_cap_truncates_and_says_so() -> None:
    """An arm that ignored its own bound must not decide how large every
    response gets."""
    items = tuple(_contextual(BLOCK_WORKSPACE, f"cp-{i}") for i in range(10))
    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(ArmOutcome(items=items))}), now=_NOW, item_cap=3)

    workspace = result.envelope.block(BLOCK_WORKSPACE)
    assert len(workspace.items) == 3
    assert workspace.state == BLOCK_DEGRADED
    assert "truncated to 3 of 10" in (workspace.reason or "")


@pytest.mark.asyncio
async def test_truncation_is_recorded_as_evidence_with_both_counts() -> None:
    """The envelope carries what survived, so what was dropped exists only here."""
    items = tuple(_contextual(BLOCK_WORKSPACE, f"cp-{i}") for i in range(10))
    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(ArmOutcome(items=items))}), now=_NOW, item_cap=4)

    evidence = next(e for e in result.evidence if e.block == BLOCK_WORKSPACE)
    assert (evidence.considered, evidence.returned) == (10, 4)
    assert evidence.truncated_by_cap


@pytest.mark.asyncio
async def test_an_arm_that_hit_its_own_limit_is_reported_separately_from_the_cap() -> None:
    """Two different truncations: the arm stopped early, or the assembler cut it.
    An operator tuning one needs to know which happened."""
    result = await assemble(
        _all_arms(**{BLOCK_ARC: _arm(ArmOutcome(items=(_contextual(BLOCK_ARC, "a"),), truncated=True))}),
        now=_NOW,
    )

    evidence = next(e for e in result.evidence if e.block == BLOCK_ARC)
    assert evidence.truncated_by_arm
    assert not evidence.truncated_by_cap


@pytest.mark.asyncio
async def test_a_zero_item_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await assemble(_all_arms(), now=_NOW, item_cap=0)


# --- Staleness ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_older_than_the_freshness_bound_degrades_the_arm() -> None:
    old = _NOW - datetime.timedelta(hours=3)
    outcome = ArmOutcome(items=(_contextual(BLOCK_WORKSPACE, "cp-1"),), fresh_as_of=old)

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(outcome)}), now=_NOW, max_age_s=60)

    workspace = result.envelope.block(BLOCK_WORKSPACE)
    assert workspace.state == BLOCK_DEGRADED
    assert "older than the freshness bound" in (workspace.reason or "")


@pytest.mark.asyncio
async def test_fresh_data_inside_the_bound_does_not_degrade() -> None:
    recent = _NOW - datetime.timedelta(seconds=5)
    outcome = ArmOutcome(items=(_contextual(BLOCK_WORKSPACE, "cp-1"),), fresh_as_of=recent)

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(outcome)}), now=_NOW, max_age_s=60)

    assert result.envelope.block(BLOCK_WORKSPACE).state == BLOCK_SUCCESS


@pytest.mark.asyncio
async def test_an_arm_that_does_not_track_freshness_is_not_reported_as_fresh() -> None:
    """`None` means the arm cannot say, which is not the same as "current" -- and
    treating it as current is how a stale arm passes a freshness bound."""
    outcome = ArmOutcome(items=(_contextual(BLOCK_WORKSPACE, "cp-1"),), fresh_as_of=None)

    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(outcome)}), now=_NOW, max_age_s=60)

    evidence = next(e for e in result.evidence if e.block == BLOCK_WORKSPACE)
    assert evidence.fresh_as_of is None
    assert not evidence.stale


# --- Exclusions ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_withheld_item_degrades_the_arm_rather_than_passing_silently() -> None:
    """ "There was nothing" and "there was something you may not see" send a
    reader to different places, and only the second says to go and ask."""
    outcome = ArmOutcome(
        items=(_contextual(BLOCK_WORKSPACE, "cp-1"),),
        exclusions=(Exclusion(item_key="task-9", reason="no active participant grant"),),
    )
    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(outcome)}), now=_NOW)

    workspace = result.envelope.block(BLOCK_WORKSPACE)
    assert workspace.state == BLOCK_DEGRADED
    assert "withheld" in (workspace.reason or "")


@pytest.mark.asyncio
async def test_the_exclusion_reason_survives_into_the_evidence() -> None:
    """The receipt is written from this. A count of withheld items with no
    reason cannot tell an operator whether to grant access or fix a bug."""
    outcome = ArmOutcome(
        exclusions=(Exclusion(item_key="task-9", reason="no active participant grant"),),
    )
    result = await assemble(_all_arms(**{BLOCK_WORKSPACE: _arm(outcome)}), now=_NOW)

    evidence = next(e for e in result.evidence if e.block == BLOCK_WORKSPACE)
    assert evidence.exclusions[0].reason == "no active participant grant"


# --- Quality ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_degraded_answer_is_never_cacheable() -> None:
    """Caching it would outlive the failure that caused it."""

    async def _broken() -> ArmOutcome:
        raise RuntimeError("down")

    result = await assemble(_all_arms(**{BLOCK_ARC: _broken}), now=_NOW)

    assert not result.envelope.quality.cacheable


@pytest.mark.asyncio
async def test_quality_names_every_degraded_arm_with_its_own_reason() -> None:
    async def _broken() -> ArmOutcome:
        raise RuntimeError("down")

    result = await assemble(
        _all_arms(
            **{
                BLOCK_ARC: _broken,
                BLOCK_WORKSPACE: _arm(
                    ArmOutcome(items=(_contextual(BLOCK_WORKSPACE, "c"),), degraded_reason="partial")
                ),
            }
        ),
        now=_NOW,
    )

    quality = result.envelope.quality
    assert set(quality.degraded_blocks) == {BLOCK_ARC, BLOCK_WORKSPACE}
    assert len(quality.reasons) == 2
    assert all(reason.strip() for reason in quality.reasons)


def test_quality_orders_degraded_blocks_by_the_fixed_block_order() -> None:
    """Two resolutions that degraded the same way must read identically;
    ordering by arrival would make them differ by timing alone."""
    blocks = (
        ContextBlockV1(name=BLOCK_CANONICAL, state=BLOCK_SUCCESS, items=(_canonical(),)),
        ContextBlockV1(name=BLOCK_ARC, state=BLOCK_FAILED, reason="a"),
        ContextBlockV1(
            name=BLOCK_OBSERVED_CLAIMS,
            state=BLOCK_SUCCESS,
            items=(_contextual(BLOCK_OBSERVED_CLAIMS, "1"),),
        ),
        ContextBlockV1(name=BLOCK_WORKSPACE, state=BLOCK_DEGRADED, reason="b"),
    )
    quality = derive_quality(blocks)

    assert quality.degraded_blocks == (BLOCK_ARC, BLOCK_WORKSPACE)
    assert quality.reasons == ("a", "b")


# --- Determinism --------------------------------------------------------------


def test_item_order_is_a_property_of_the_items_not_of_the_query() -> None:
    """Two resolutions over unchanged data produce the same order, which is what
    makes a receipt checkable rather than decorative."""
    items = [_contextual(BLOCK_WORKSPACE, key) for key in ("c", "a", "b")]

    assert ordered_items(items) == ordered_items(list(reversed(items)))


@pytest.mark.asyncio
async def test_the_same_inputs_produce_the_same_receipt_item_ids() -> None:
    first = await assemble(_all_arms(), now=_NOW)
    second = await assemble(_all_arms(), now=_NOW)

    def _ids(result: object) -> list[str]:
        return [
            item.receipt_item_id.value()
            for block in result.envelope.blocks  # type: ignore[attr-defined]
            for item in block.items
        ]

    assert _ids(first) == _ids(second)


# --- The trust rule, at the boundary ------------------------------------------


def test_a_canonical_item_carries_no_trust_metadata() -> None:
    """Attaching one invites the question of whether another authority could
    have supplied the registry's own answer."""
    assert _canonical().trust is None


def test_a_non_canonical_item_without_trust_is_refused_at_construction() -> None:
    with pytest.raises(InvalidContextItem, match="no trust metadata"):
        ContextBlockV1(
            name=BLOCK_WORKSPACE,
            state=BLOCK_SUCCESS,
            items=(ContextItemV1(receipt_item_id=_contextual(BLOCK_WORKSPACE, "x").receipt_item_id, payload={}),),
        )


def test_contextual_item_refuses_a_non_trust_object() -> None:
    """The type is the check. A dict shaped like trust metadata would satisfy
    every attribute read and carry none of the validation."""
    with pytest.raises(TypeError, match="TrustMetadataV1"):
        contextual_item(block=BLOCK_ARC, source="s", item_key="k", payload={}, trust={"trust": "observed"})
