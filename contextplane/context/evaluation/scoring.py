"""The deterministic three, computed for one simulation, or an honest refusal.

E24-T4a. `envelope_judge.py` scores five blocks and nothing called it — the
scorer was complete, tested, wired into `protocol.freeze()`, and reachable by no
transport. That is the third instance of one failure this workspace has recorded
(`requires_validated` with no caller outside its tests, `resolve_weights` with no
production caller), and *"twice in one audit is enough to make it a thing to
check for rather than a thing to notice"* — so this is the caller.

## What it computes, and what it needs to

Required-fact recall, boundary violations and precision, over the material the
simulation recorded showing the model. **No judge, no model, nothing that can
drift** — which is the property that keeps a failure of these three attributable
to what was *served* rather than to what graded it.

## Why it can refuse, and why the refusal is the honest answer

The scorer asks the *scenario*, never the system under test. That means it needs
expectations declared **before the run**, per `scenarios.py`:

    a scenario whose required facts were written after seeing what the system
    returned would be satisfied by whatever the system returned

An interactive simulation carries no prompt and therefore no declared
expectations. It could be scored against expectations typed afterwards, and that
number would be worthless in a specific way: it would measure whatever the system
returned. So the answer is `unassertable`, with the reason — not zeros, which a
surface would render as three failed criteria, and not ones, which would render
as three passes nobody checked.

**A prompt that declared nothing is a second, distinct refusal.** An evaluator
exploring has not yet decided what good looks like, and that is a legal state; it
is not the same as a simulation nobody could have declared expectations for, and
the two carry different remedies.

## The tenant is the resolution's

Passed to the scorer rather than read off an item, because no arm writes a tenant
into a payload — see `envelope_judge`'s own docstring for what that mistake looked
like in the shipped scorer.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from sqlalchemy import RowMapping, text

from contextplane.context.evaluation.envelope_judge import (
    ENVELOPE_JUDGE_VERSION,
    BlockTally,
    EnvelopeScore,
    SafetyViolation,
    UncheckedDimension,
    score,
)
from contextplane.context.evaluation.expectations import ExpectationsV1
from contextplane.context.schemas.envelope import BLOCK_NAMES
from contextplane.context.schemas.trust import ReceiptItemIdV1
from contextplane.exceptions import NotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import TenantContext

#: Why a deterministic score could not be computed. Two reasons, not one, and
#: they have different remedies: the first is fixable by declaring expectations
#: on the prompt, the second is not fixable at all for this simulation.
UNASSERTABLE_NO_PROMPT: Final = (
    "this simulation was run interactively and belongs to no prompt, so nothing was declared in "
    "advance to score it against. A scenario whose required facts were written after seeing what "
    "the system returned would be satisfied by whatever the system returned, so the criteria are "
    "reported as unassertable rather than computed against expectations typed afterwards."
)
UNASSERTABLE_NO_EXPECTATIONS: Final = (
    "the prompt this simulation ran declares no expectations, so there is nothing for the "
    "deterministic criteria to check. Declare them on the prompt — before the next run — and they "
    "become checkable."
)


@dataclasses.dataclass(frozen=True)
class DeterministicScore:
    """The three criteria a program computes, or the reason it could not.

    `score` and `unassertable` are exclusive, and a reader branches on which is
    present. That is deliberately not a nullable score with a default: zeros
    would render as three failed criteria and ones as three passes nobody
    checked, and both are worse than the sentence that says why there is no
    number.
    """

    rubric_version: str
    score: EnvelopeScore | None = None
    unassertable: str | None = None
    #: Which prompt's expectations were used, when any were.
    prompt_id: uuid.UUID | None = None

    @property
    def is_assertable(self) -> bool:
        """Whether there is a number at all."""
        return self.score is not None


class SimulationScoringService:
    """Scores a recorded simulation against expectations declared before it ran."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def score_simulation(self, ctx: TenantContext, simulation_id: uuid.UUID) -> DeterministicScore:
        """The deterministic three for one simulation, or why they cannot be computed.

        Reads the material the simulation recorded rather than re-resolving.
        Re-resolving would score a *different* envelope than the answer came
        from, which is the same reason the judge reads the recorded material.
        """
        async with self._session_factory() as session:
            header = (
                (
                    await session.execute(
                        text(
                            "SELECT s.simulation_id, s.run_item_id, p.prompt_id, p.expectations "
                            "  FROM evaluation_simulations s "
                            "  LEFT JOIN evaluation_run_items i ON i.item_id = s.run_item_id "
                            "  LEFT JOIN evaluation_prompts p ON p.prompt_id = i.prompt_id "
                            " WHERE s.simulation_id = :sid AND s.tenant_id = :tid"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .first()
            )
            if header is None:
                raise NotFoundError(f"simulation {simulation_id} not found")

            if header["prompt_id"] is None:
                return DeterministicScore(rubric_version=ENVELOPE_JUDGE_VERSION, unassertable=UNASSERTABLE_NO_PROMPT)
            if header["expectations"] is None:
                return DeterministicScore(
                    prompt_id=header["prompt_id"],
                    rubric_version=ENVELOPE_JUDGE_VERSION,
                    unassertable=UNASSERTABLE_NO_EXPECTATIONS,
                )

            served = (
                (
                    await session.execute(
                        text(
                            "SELECT receipt_item_id, block, item_key, payload "
                            "  FROM evaluation_simulation_served_items "
                            " WHERE simulation_id = :sid AND tenant_id = :tid "
                            " ORDER BY receipt_item_id"
                        ),
                        {"sid": simulation_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )

        declared = ExpectationsV1.of(dict(header["expectations"]))
        envelope = _RecordedEnvelope(blocks=_blocks_from(served))
        return DeterministicScore(
            prompt_id=header["prompt_id"],
            rubric_version=ENVELOPE_JUDGE_VERSION,
            score=score(
                envelope=envelope,  # type: ignore[arg-type]
                facts=declared.authorization_facts(),
                relevant_item_keys=declared.relevant_item_keys,
                required_item_keys=declared.required_item_keys,
                served_tenant_id=str(ctx.tenant_id),
            ),
        )


@dataclasses.dataclass(frozen=True)
class _RecordedItem:
    """One recorded item, as the scorer reads it.

    `trust` is always `None`, and that is a fact about storage rather than a
    shortcut: the served record keeps the payload the model was shown, and the
    *receipt* is the record that owns the trust metadata. A copy here would be a
    second place a classification lives and the one that could not be corrected.

    `envelope_judge` reports the classification dimension as unchecked for an
    item with no trust record, under a reason distinct from canonical's
    structural absence — which is the honest outcome and the reason that
    distinction exists.
    """

    receipt_item_id: ReceiptItemIdV1
    payload: dict[str, object]
    trust: None = None


@dataclasses.dataclass(frozen=True)
class _RecordedBlock:
    """One block of recorded material, as the scorer reads it.

    **Deliberately not a `ContextBlockV1`.** That contract refuses a
    non-canonical item with no trust record — correctly, because a *served*
    envelope must never carry one. Recorded material does carry them, because the
    trust metadata lives on the receipt, so constructing the validated type here
    would mean either inventing a trust record or failing on every replay. The
    scorer reads `name`, `state`, `items` and `reason`, and this is exactly that
    surface.
    """

    name: str
    state: str
    items: tuple[_RecordedItem, ...]
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class _RecordedEnvelope:
    """The recorded material, shaped as the scorer reads it.

    The scorer reads `.blocks` and nothing else, so this is not a reconstructed
    `ContextEnvelopeV1` — building one would mean re-deriving an envelope state
    and a quality record from stored items, which is inventing two facts to
    satisfy a type that neither field is read through.
    """

    blocks: tuple[_RecordedBlock, ...]


def _blocks_from(rows: Sequence[RowMapping]) -> tuple[_RecordedBlock, ...]:
    """The stored served items, grouped back into blocks in contract order.

    A block with no stored items is reported as `empty` rather than omitted. The
    scorer's per-block tally reports every block including the empty ones,
    because the arm that served nothing is the one most likely to be the reason a
    recall figure moved — and a block missing from the input would be missing from
    the breakdown.

    **Trust metadata is not reconstructed, and the consequence is stated rather
    than hidden.** The served record keeps the payload, not the trust record, so
    the classification dimension is unavailable here and `envelope_judge` records
    it as unchecked — which is exactly the honest outcome its `UncheckedDimension`
    exists for. Storing a trust copy would be a second place a classification
    lives, and the receipt is the record that owns it.
    """
    by_block: dict[str, list[_RecordedItem]] = {name: [] for name in BLOCK_NAMES}
    for row in rows:
        block = str(row["block"])
        if block not in by_block:
            continue
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        by_block[block].append(
            _RecordedItem(
                payload=dict(payload),
                receipt_item_id=ReceiptItemIdV1(block=block, source="recorded", item_key=str(row["item_key"])),
            )
        )
    return tuple(
        _RecordedBlock(
            items=tuple(items),
            name=name,
            state="success" if items else "empty",
        )
        for name, items in ((name, by_block[name]) for name in BLOCK_NAMES)
    )


__all__ = [
    "UNASSERTABLE_NO_EXPECTATIONS",
    "UNASSERTABLE_NO_PROMPT",
    "BlockTally",
    "DeterministicScore",
    "SafetyViolation",
    "SimulationScoringService",
    "UncheckedDimension",
]
