"""Assemble the five-block context envelope from five independent arms.

One deterministic contract that cannot flatten authority or hide an arm
failure. Everything here exists to protect one of those two properties.

**Authority is not flattened.** The five arms stay five blocks. Nothing merges
them, re-ranks across them, or promotes a workspace note next to a canonical
answer because it scored well. A single ranked list would be more convenient to
consume and would destroy the only signal telling a reader which claims the
registry stands behind.

**A failure is never silent.** Every arm reports success, empty, degraded or
failed, and empty is not failure. An arm that could not answer must not read as
an arm with nothing to say -- that reading is what makes an agent proceed
confidently on an incomplete picture. A canonical failure blocks the whole
response, because the surrounding context without the thing it surrounds is
misleading rather than partial.

**The arms are injected, not imported.** Each arm is an async callable this
module composes. That is what lets one arm fail, time out, or return garbage in
a unit test without a database, and it is the only way to prove the failure
paths -- which are the paths that matter and the ones an integration test
exercises least. `queries.py` holds the real reads.

**Every arm is bounded.** A per-arm item cap and a per-arm timeout, both applied
here rather than trusted to each arm. An arm that ignores its own bound would
otherwise decide how large every response gets, and a slow arm would decide how
long every caller waits.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
from typing import TYPE_CHECKING, Protocol

from contextplane.context.quality import derive_quality
from contextplane.context.schemas.envelope import (
    BLOCK_CANONICAL,
    BLOCK_DEGRADED,
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_SUCCESS,
    ContextBlockV1,
    ContextEnvelopeV1,
    ContextItemV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import ReceiptItemIdV1, TrustMetadataV1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

#: Most items one arm may contribute. Applied by the assembler, so an arm that
#: forgot its own limit cannot decide how large every response gets.
DEFAULT_ITEM_CAP = 50

#: How long one arm gets. Applied per arm rather than to the whole assembly so a
#: single slow arm degrades itself instead of the response -- the alternative is
#: one arm's latency deciding whether the other three are ever reported.
DEFAULT_ARM_TIMEOUT_S = 2.0


@dataclasses.dataclass(frozen=True)
class Exclusion:
    """One item an arm found and deliberately did not return.

    Recorded rather than dropped. An item withheld for authorization or
    classification is the single most important thing a receipt can show: it is
    the difference between "there was nothing" and "there was something you may
    not see", and only the second one tells a reader to go and ask someone.
    """

    item_key: str
    reason: str


@dataclasses.dataclass(frozen=True)
class ArmOutcome:
    """What one arm returned, before the assembler decides what to call it.

    The arm reports facts about its own read -- what it found, what it withheld,
    whether it hit its own limit, how fresh its data is. It does not report a
    block state: the mapping from those facts to success/empty/degraded/failed is
    one decision, made once, below.
    """

    items: tuple[ContextItemV1, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    #: The arm stopped early against its own limit. Distinct from the
    #: assembler's cap, which is applied here and recorded separately.
    truncated: bool = False
    #: When this arm's data was last known good. `None` means the arm does not
    #: track staleness, which is not the same as "fresh" and is not reported as
    #: such.
    fresh_as_of: datetime.datetime | None = None
    #: Set when the arm answered but incompletely -- a partial read, a fallback
    #: path. Degrades the arm without failing it.
    degraded_reason: str | None = None


class ContextArm(Protocol):
    """One arm of the answer."""

    async def __call__(self) -> ArmOutcome:
        """Read this arm's slice of the answer, reporting facts and not a verdict."""
        ...


@dataclasses.dataclass(frozen=True)
class SelectionEvidence:
    """Why one arm's contents are what they are, kept for the receipt.

    Persisted by slice C rather than here, but produced here, because this is
    the only place that still knows what was dropped and why. Reconstructing it
    later from the envelope alone is impossible by design: the envelope carries
    what survived.
    """

    block: str
    state: str
    considered: int
    returned: int
    exclusions: tuple[Exclusion, ...]
    truncated_by_cap: bool
    truncated_by_arm: bool
    fresh_as_of: datetime.datetime | None
    stale: bool
    duration_ms: int
    #: The arm ran out of time, as opposed to raising. Only ever true alongside
    #: a failed state. Carried as a field because "slow" and "broken" send an
    #: operator to different places, and a reader that has to recover that from
    #: the reason text loses it the first time the text is reworded.
    timed_out: bool = False


@dataclasses.dataclass(frozen=True)
class AssemblyResult:
    """The envelope, plus the evidence a receipt is written from."""

    envelope: ContextEnvelopeV1
    evidence: tuple[SelectionEvidence, ...]


def _staleness_cutoff(now: datetime.datetime, max_age_s: float | None) -> datetime.datetime | None:
    if max_age_s is None:
        return None
    return now - datetime.timedelta(seconds=max_age_s)


async def _run_arm(arm: ContextArm, *, timeout_s: float) -> tuple[ArmOutcome | None, str | None, bool, int]:
    """Run one arm under its own timeout.

    Returns the outcome, a failure reason, whether the failure was a timeout,
    and how long it took. A timeout and a raised exception are both failures and
    are reported differently, because "the arm is slow" and "the arm is broken"
    send an operator to different places.

    The timeout travels as its own boolean rather than only inside the reason
    text. For a while it did not, and the two were distinguishable only by
    matching English in a string written for a human -- so anything downstream
    wanting to count timeouts had to either reword-match or give up the
    distinction this docstring says matters.

    The exception is caught broadly on purpose. This module's contract is that
    one arm cannot take down the response, and narrowing the catch to the
    exceptions known today would mean the next unforeseen one does exactly that.
    """
    started = asyncio.get_running_loop().time()

    def _elapsed_ms() -> int:
        return int((asyncio.get_running_loop().time() - started) * 1000)

    try:
        outcome = await asyncio.wait_for(arm(), timeout=timeout_s)
    except TimeoutError:
        return None, f"the arm did not answer within {timeout_s:g}s", True, _elapsed_ms()
    except Exception as exc:  # noqa: BLE001 - one arm must not take down the response
        return None, f"the arm raised {type(exc).__name__}", False, _elapsed_ms()
    return outcome, None, False, _elapsed_ms()


def _block_from_outcome(
    name: str,
    outcome: ArmOutcome,
    *,
    item_cap: int,
    stale_cutoff: datetime.datetime | None,
) -> tuple[ContextBlockV1, SelectionEvidence, bool]:
    """Turn one arm's facts into one block, and say what it cost.

    The single place success/empty/degraded/failed is decided, so the five arms
    cannot drift on what "degraded" means.
    """
    considered = len(outcome.items)
    kept = outcome.items[:item_cap]
    truncated_by_cap = considered > item_cap

    fresh_as_of = outcome.fresh_as_of
    stale = bool(stale_cutoff and fresh_as_of and fresh_as_of < stale_cutoff)

    # Every reason the arm is less than whole, in a fixed order so two identical
    # degradations read identically.
    reasons: list[str] = []
    if outcome.degraded_reason:
        reasons.append(outcome.degraded_reason)
    if outcome.truncated:
        reasons.append("the arm stopped at its own limit, so this is a partial read")
    if truncated_by_cap:
        reasons.append(f"truncated to {item_cap} of {considered} item(s)")
    if stale and fresh_as_of is not None:
        reasons.append(f"data is older than the freshness bound (as of {fresh_as_of.isoformat()})")
    if outcome.exclusions:
        # Withheld items degrade the arm rather than passing silently. A reader
        # who cannot tell "nothing" from "something withheld" cannot know to ask.
        reasons.append(f"{len(outcome.exclusions)} item(s) withheld")

    if reasons:
        state = BLOCK_DEGRADED
    elif kept:
        state = BLOCK_SUCCESS
    else:
        state = BLOCK_EMPTY

    block = ContextBlockV1(
        name=name,
        state=state,
        items=tuple(kept),
        reason="; ".join(reasons) if reasons else None,
    )
    evidence = SelectionEvidence(
        block=name,
        state=state,
        considered=considered,
        returned=len(kept),
        exclusions=outcome.exclusions,
        truncated_by_cap=truncated_by_cap,
        truncated_by_arm=outcome.truncated,
        fresh_as_of=outcome.fresh_as_of,
        stale=stale,
        duration_ms=0,
    )
    return block, evidence, stale


def _failed_block(
    name: str, reason: str, *, duration_ms: int, timed_out: bool = False
) -> tuple[ContextBlockV1, SelectionEvidence]:
    """A block for an arm that could not answer.

    Carries no items by construction. Partial output from a failed arm is the
    shape that gets read as complete, which is the reading this whole contract
    exists to prevent.
    """
    block = ContextBlockV1(name=name, state=BLOCK_FAILED, items=(), reason=reason)
    evidence = SelectionEvidence(
        block=name,
        state=BLOCK_FAILED,
        considered=0,
        returned=0,
        exclusions=(),
        truncated_by_cap=False,
        truncated_by_arm=False,
        fresh_as_of=None,
        stale=False,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
    return block, evidence


async def assemble(
    arms: Mapping[str, ContextArm],
    *,
    now: datetime.datetime,
    item_cap: int = DEFAULT_ITEM_CAP,
    arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S,
    max_age_s: float | None = None,
) -> AssemblyResult:
    """Resolve one context envelope.

    Every one of the five arms is asked, always, and every one appears in the
    result. An arm missing from `arms` is a failed arm rather than an absent
    block: a caller that has to check whether a block exists will get that check
    wrong once, and the failure looks like missing data rather than a missing
    check.

    The arms run concurrently because they are independent and a caller waits
    for the slowest either way. They are *reported* in the fixed block order
    regardless of which finished first -- ordering by completion would make two
    identical resolutions differ by timing alone.
    """
    if item_cap < 1:
        raise ValueError(f"item_cap must be at least 1, got {item_cap}; an arm capped at zero cannot answer")

    stale_cutoff = _staleness_cutoff(now, max_age_s)

    async def _one(name: str) -> tuple[ContextBlockV1, SelectionEvidence]:
        arm = arms.get(name)
        if arm is None:
            return _failed_block(name, "no arm was configured for this block", duration_ms=0)

        outcome, failure, timed_out, duration_ms = await _run_arm(arm, timeout_s=arm_timeout_s)
        if outcome is None:
            return _failed_block(
                name, failure or "the arm did not answer", duration_ms=duration_ms, timed_out=timed_out
            )

        try:
            block, evidence, _stale = _block_from_outcome(name, outcome, item_cap=item_cap, stale_cutoff=stale_cutoff)
        except Exception as exc:  # noqa: BLE001 - a malformed arm result is that arm's failure
            # An arm that returned something the block contract refuses -- an
            # item with no trust metadata, most likely -- has failed, and saying
            # so is better than propagating an exception that takes the other
            # three arms with it.
            return _failed_block(name, f"the arm returned an unusable result: {exc}", duration_ms=duration_ms)

        return block, dataclasses.replace(evidence, duration_ms=duration_ms)

    gathered = await asyncio.gather(*(_one(name) for name in BLOCK_NAMES))

    blocks = tuple(block for block, _ in gathered)
    evidence = tuple(item for _, item in gathered)

    envelope = ContextEnvelopeV1(
        blocks=blocks,
        quality=derive_quality(blocks),
        state=derive_envelope_state(blocks),
    )
    return AssemblyResult(envelope=envelope, evidence=evidence)


def canonical_item(*, source: str, item_key: str, payload: dict[str, object]) -> ContextItemV1:
    """A canonical item, which carries no trust metadata by contract."""
    return ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=BLOCK_CANONICAL, source=source, item_key=item_key),
        payload=payload,
        trust=None,
    )


def contextual_item(
    *,
    block: str,
    source: str,
    item_key: str,
    payload: dict[str, object],
    trust: object,
) -> ContextItemV1:
    """An item for any block other than canonical, which must carry trust."""
    if not isinstance(trust, TrustMetadataV1):
        raise TypeError(f"a {block} item needs TrustMetadataV1, got {type(trust).__name__}")
    return ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=block, source=source, item_key=item_key),
        payload=payload,
        trust=trust,
    )


def ordered_items(items: Sequence[ContextItemV1]) -> tuple[ContextItemV1, ...]:
    """Items in the order the read produced them. ADR 0028.

    This sorted by `receipt_item_id.value()` -- a SHA-256 digest of the block,
    source and item identity -- for a stated reason:

        Sorted by receipt item id ... so the order is a property of what the
        items *are* rather than of the query plan that found them. Two
        resolutions over unchanged data produce the same order, which is what
        makes a receipt checkable.

    The property is real. What it cost was not stated, and is easiest to see
    measured. Asking the development catalog *"which components depend on the
    salt theme provider"*, the retriever ranked `salt-design-system` first at
    0.3222 and `salt-avatar` fourth at 0.1000; the block presented `salt-avatar`
    first and the design system third, in ascending hexadecimal digest order.
    **The ranking was computed and then discarded**, on every block, on every
    resolution.

    Worse than presentation: `_block_from_outcome` caps with
    `outcome.items[:item_cap]` *after* this call, so a block over the cap dropped
    items by hash. The best match could be discarded while a worse one was kept.

    Determinism did not need a digest, only a total order, and every read feeding
    a block already has one -- entity search's was measured directly across three
    freshly seeded databases while fixing the natural-language defect. So the
    guarantee moves from this function to the arms, where it is asserted by a
    test rather than made true by construction and therefore untestable.

    Kept as a function, and every arm keeps calling it, because one point where a
    block's order is decided is what makes that decision enforceable. A read added
    without a total `ORDER BY` now produces a block whose order varies between
    identical requests, which is the risk ADR 0028 accepts and names.

    The receipt is unaffected: `context_receipt_items` has no position column, so
    it records *which* items were served and never in what order.
    """
    return tuple(items)


__all__ = [
    "DEFAULT_ARM_TIMEOUT_S",
    "DEFAULT_ITEM_CAP",
    "ArmOutcome",
    "AssemblyResult",
    "ContextArm",
    "Exclusion",
    "SelectionEvidence",
    "assemble",
    "canonical_item",
    "contextual_item",
    "ordered_items",
]
