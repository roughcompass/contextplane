"""The four blocks a context resolution returns, and what their states mean.

Exactly four blocks, always present: canonical, ARC, observed claims, workspace.
Present even when empty, because a caller that has to branch on whether a key
exists will get that branch wrong once, and the failure looks like missing data
rather than a missing check.

**The four blocks are not peers.** Canonical is the registry's own answer; the
other three are context around it. That asymmetry is the whole reason a canonical
failure blocks the response while any other arm failing merely degrades it:
serving the surrounding context without the thing it surrounds is not a partial
answer, it is a misleading one.

**Empty and failed are different, and the distinction is load-bearing.** Empty
means the arm was asked and truthfully has nothing. Failed means the arm could
not answer. Collapsing them lets a broken integration read as "no workspace notes
exist", which is the reading that makes an agent proceed confidently on an
incomplete picture.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from contextplane.context.schemas.trust import (
    InvalidContextItem,
    QualityStateV1,
    ReceiptItemIdV1,
    TrustMetadataV1,
)

# One arm's outcome.
BlockState = Literal["success", "empty", "degraded", "failed"]

BLOCK_SUCCESS: BlockState = "success"
BLOCK_EMPTY: BlockState = "empty"
BLOCK_DEGRADED: BlockState = "degraded"
BLOCK_FAILED: BlockState = "failed"

BLOCK_STATES: frozenset[str] = frozenset({BLOCK_SUCCESS, BLOCK_EMPTY, BLOCK_DEGRADED, BLOCK_FAILED})

# The response's outcome, derived from the arms rather than set by hand.
EnvelopeState = Literal["complete", "degraded", "blocked"]

ENVELOPE_COMPLETE: EnvelopeState = "complete"
ENVELOPE_DEGRADED: EnvelopeState = "degraded"
ENVELOPE_BLOCKED: EnvelopeState = "blocked"

ENVELOPE_STATES: frozenset[str] = frozenset({ENVELOPE_COMPLETE, ENVELOPE_DEGRADED, ENVELOPE_BLOCKED})

BLOCK_CANONICAL = "canonical"
BLOCK_ARC = "arc"
BLOCK_OBSERVED_CLAIMS = "observed_claims"
BLOCK_WORKSPACE = "workspace"

# Order is fixed and meaningful: it is the order a reader should weigh them, and
# pinning it here stops each caller inventing its own.
BLOCK_NAMES: tuple[str, ...] = (BLOCK_CANONICAL, BLOCK_ARC, BLOCK_OBSERVED_CLAIMS, BLOCK_WORKSPACE)

# Every block except canonical carries trust metadata per item.
NON_CANONICAL_BLOCKS: frozenset[str] = frozenset({BLOCK_ARC, BLOCK_OBSERVED_CLAIMS, BLOCK_WORKSPACE})


@dataclasses.dataclass(frozen=True)
class ContextItemV1:
    """One piece of context, and everything needed to weigh it.

    `trust` is `None` only for canonical items. Assembly enforces that, not the
    item: an item does not know which block it landed in, and asking it to would
    mean trusting the caller to tell it the truth.
    """

    receipt_item_id: ReceiptItemIdV1
    payload: dict[str, object]
    trust: TrustMetadataV1 | None = None


@dataclasses.dataclass(frozen=True)
class ContextBlockV1:
    """One arm of the answer.

    The state is asserted by whoever assembled the arm rather than inferred from
    whether `items` is empty, because the two disagree in exactly the case that
    matters: an arm that failed has no items either.
    """

    name: str
    state: BlockState
    items: tuple[ContextItemV1, ...] = ()
    # Present when the arm did not fully succeed. Required then -- a degraded arm
    # with no reason is a dead end for whoever has to explain the response.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.name not in BLOCK_NAMES:
            raise InvalidContextItem(f"unknown block {self.name!r}; the four blocks are {list(BLOCK_NAMES)}")
        if self.state not in BLOCK_STATES:
            raise InvalidContextItem(f"unknown block state {self.state!r}; legal values are {sorted(BLOCK_STATES)}")

        if self.state in (BLOCK_DEGRADED, BLOCK_FAILED) and not (self.reason or "").strip():
            raise InvalidContextItem(f"the {self.name} block is {self.state} and must say why")

        # `empty` and `success` are distinguished by whether anything came back,
        # so a mismatch here means the assembler mislabelled its own result.
        if self.state == BLOCK_EMPTY and self.items:
            raise InvalidContextItem(f"the {self.name} block is empty but carries {len(self.items)} item(s)")
        if self.state == BLOCK_SUCCESS and not self.items:
            raise InvalidContextItem(
                f"the {self.name} block claims success with no items; an arm with nothing to say is empty, "
                "and the difference is what tells a reader whether to go looking elsewhere"
            )
        if self.state == BLOCK_FAILED and self.items:
            raise InvalidContextItem(
                f"the {self.name} block failed but carries {len(self.items)} item(s); partial output from a "
                "failed arm is the shape that gets read as complete"
            )

        # The rule the whole trust contract rests on: outside canonical, an item
        # without complete trust metadata is invalid, not merely untrusted.
        if self.name in NON_CANONICAL_BLOCKS:
            for item in self.items:
                if item.trust is None:
                    raise InvalidContextItem(
                        f"item {item.receipt_item_id.value()} in the {self.name} block has no trust metadata; "
                        "outside canonical that is invalid, because a reader cannot weigh what does not say "
                        "where it came from"
                    )
        else:
            for item in self.items:
                if item.trust is not None:
                    raise InvalidContextItem(
                        "canonical items carry no trust metadata; attaching one invites the question of "
                        "whether another authority could have supplied the registry's own answer"
                    )


def derive_envelope_state(blocks: tuple[ContextBlockV1, ...]) -> EnvelopeState:
    """The response's state, computed from the arms. Never set by hand.

    Computed rather than passed so the canonical-blocks rule cannot be forgotten
    at one call site. There is exactly one place this decision is made.
    """
    by_name = {block.name: block for block in blocks}
    canonical = by_name[BLOCK_CANONICAL]

    # A canonical arm that could not answer blocks the response outright. The
    # surrounding context without the thing it surrounds is misleading, not
    # partial -- an agent would read the ARC and workspace blocks as the whole
    # picture.
    if canonical.state == BLOCK_FAILED:
        return ENVELOPE_BLOCKED

    # A canonical arm that answered incompletely is not a block, but nothing
    # downstream may treat the answer as whole.
    if canonical.state == BLOCK_DEGRADED:
        return ENVELOPE_DEGRADED

    if any(by_name[name].state in (BLOCK_DEGRADED, BLOCK_FAILED) for name in NON_CANONICAL_BLOCKS):
        return ENVELOPE_DEGRADED

    # Every arm either answered or truthfully had nothing. Empty is not degraded:
    # a subject with no workspace notes is a complete answer.
    return ENVELOPE_COMPLETE


@dataclasses.dataclass(frozen=True)
class ContextEnvelopeV1:
    """What one resolution returns: four blocks, quality, and a receipt."""

    blocks: tuple[ContextBlockV1, ...]
    quality: QualityStateV1
    state: EnvelopeState

    def __post_init__(self) -> None:
        names = tuple(block.name for block in self.blocks)
        if names != BLOCK_NAMES:
            raise InvalidContextItem(
                f"an envelope carries exactly the four blocks in order {list(BLOCK_NAMES)}, got {list(names)}; "
                "a caller that has to check whether a block exists will get that check wrong once"
            )

        derived = derive_envelope_state(self.blocks)
        if self.state != derived:
            raise InvalidContextItem(
                f"envelope state {self.state!r} disagrees with its blocks, which derive {derived!r}; "
                "the state is computed, never asserted"
            )

        # Quality must name the arms that actually degraded, or the caller's
        # explanation of the response contradicts the response.
        actually_degraded = {block.name for block in self.blocks if block.state in (BLOCK_DEGRADED, BLOCK_FAILED)}
        claimed = set(self.quality.degraded_blocks)
        if claimed != actually_degraded:
            raise InvalidContextItem(
                f"quality names {sorted(claimed)} as degraded but the blocks say {sorted(actually_degraded)}"
            )

    def block(self, name: str) -> ContextBlockV1:
        """The named arm. Raises rather than returning None, because a caller
        reaching for a block it did not expect is a bug, not an empty result."""
        for candidate in self.blocks:
            if candidate.name == name:
                return candidate
        raise InvalidContextItem(f"unknown block {name!r}")


__all__ = [
    "BLOCK_ARC",
    "BLOCK_CANONICAL",
    "BLOCK_DEGRADED",
    "BLOCK_EMPTY",
    "BLOCK_FAILED",
    "BLOCK_NAMES",
    "BLOCK_OBSERVED_CLAIMS",
    "BLOCK_STATES",
    "BLOCK_SUCCESS",
    "BLOCK_WORKSPACE",
    "ENVELOPE_BLOCKED",
    "ENVELOPE_COMPLETE",
    "ENVELOPE_DEGRADED",
    "ENVELOPE_STATES",
    "NON_CANONICAL_BLOCKS",
    "BlockState",
    "ContextBlockV1",
    "ContextEnvelopeV1",
    "ContextItemV1",
    "EnvelopeState",
    "derive_envelope_state",
]
