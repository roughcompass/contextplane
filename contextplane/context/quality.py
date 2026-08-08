"""How good an answer is, derived from the arms rather than asserted.

Separate module from the assembler because this is the part a caller reads to
decide what to do, and it has exactly one rule that matters: it must never
describe the response as better than the blocks say it was. The envelope
contract enforces the agreement, so a mismatch here is caught at construction
rather than shipped -- but the derivation still lives in one place so there is
only one thing to get right.
"""

from __future__ import annotations

from contextplane.context.schemas.envelope import (
    BLOCK_DEGRADED,
    BLOCK_FAILED,
    BLOCK_NAMES,
    ContextBlockV1,
)
from contextplane.context.schemas.trust import QualityStateV1


def derive_quality(blocks: tuple[ContextBlockV1, ...]) -> QualityStateV1:
    """Quality for one assembled set of arms.

    Degraded and failed are both reported here, and neither is filtered out for
    being "expected". An arm that always fails in a given deployment is exactly
    the one whose failure stops being mentioned, and then stops being fixed.

    Order follows `BLOCK_NAMES` rather than the order failures happened to
    arrive in. Two resolutions of the same subject that degraded the same way
    should read identically; ordering by arrival would make them differ by
    timing alone, which is the kind of difference that makes a receipt look like
    it recorded something it did not.
    """
    by_name = {block.name: block for block in blocks}

    degraded: list[str] = []
    reasons: list[str] = []
    for name in BLOCK_NAMES:
        block = by_name.get(name)
        if block is None or block.state not in (BLOCK_DEGRADED, BLOCK_FAILED):
            continue
        degraded.append(name)
        # The block contract already refuses a degraded or failed block with no
        # reason, so this cannot be empty -- but reading it through a default
        # would quietly paper over that if it ever changed.
        reasons.append(block.reason or f"the {name} arm did not fully answer")

    # A degraded answer is not cacheable: caching it would outlive the failure
    # that caused it, and the next reader would get a stale picture with no sign
    # that anything went wrong. The envelope refuses the combination anyway;
    # deriving it correctly here means the refusal never fires in practice.
    return QualityStateV1(
        degraded_blocks=tuple(degraded),
        reasons=tuple(reasons),
        cacheable=not degraded,
    )


__all__ = ["derive_quality"]
