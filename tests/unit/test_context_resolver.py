"""What one resolution records about the instruction set the caller declared.

E22-T14. The resolver is where the three dispositions become something a caller
and an evaluator can both read, and where the one rule that keeps the record
honest lives: **the declaration is recorded against what the caller actually
received, not against what the read found.** An instruction arm that failed a
floor served nothing, so nothing was contradicted -- and a record claiming a
contradiction reached an agent that never saw one is worse than no record,
because it is the record an evaluator would act on.

Real arms over fake sources, the real assembler, and a fake receipt writer. The
instruction arm is the real one, so the floor it passes through is the real
guard rather than a stand-in that agrees with it.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

import pytest

from contextplane.context.arms import ContextArms
from contextplane.context.instructions import (
    BLOCK_NOTES,
    DeclarationOutcome,
    Disposition,
    ServedDelta,
)
from contextplane.context.resolve import ContextResolver
from contextplane.context.schemas.envelope import (
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_INSTRUCTIONS,
    BLOCK_SUCCESS,
)
from contextplane.types import TenantContext
from contextplane.workspaces import recall as workspace_recall

_NOW = datetime.datetime(2026, 8, 24, 9, 0, tzinfo=datetime.UTC)
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACTOR = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DIGEST = "sha256:" + "a" * 64
_RECEIPT = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=("consumer",))


class _Session:
    """Answers "nothing is overdue" and nothing else."""

    overdue = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        overdue = self.overdue

        class _Result:
            @staticmethod
            def scalar_one() -> int:
                return overdue

        return _Result()

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _OverdueSession(_Session):
    overdue = 3


class _Empty:
    """Every arm's source, answering with nothing rather than failing.

    The other four blocks are not this test's subject; what matters is that they
    resolve to a legal state so the envelope is well-formed and the instruction
    block is the only thing varying.
    """

    async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def query(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        return ()

    async def get_receipt(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return {}

    def reference_arm(self, **kwargs: Any) -> Any:
        return self._nothing

    def lexical_arm(self, **kwargs: Any) -> Any:
        return self._nothing

    @staticmethod
    async def _nothing() -> Any:
        from contextplane.context.assembler import ArmOutcome

        return ArmOutcome()


class _Channel:
    """Returns a scripted declaration and records what the resolver wrote back."""

    def __init__(self, outcome: DeclarationOutcome) -> None:
        self._outcome = outcome
        self.recorded: list[DeclarationOutcome] = []

    async def resolve_declaration(self, ctx: Any, *, digest: str | None, limit: int) -> DeclarationOutcome:
        return self._outcome

    async def record(self, ctx: Any, *, outcome: DeclarationOutcome, receipt_id: uuid.UUID | None, now: Any) -> None:
        self.recorded.append(outcome)


class _Receipts:
    async def record(self, ctx: Any, **kwargs: Any) -> uuid.UUID:
        self.request = kwargs.get("request")
        return _RECEIPT


def _delta(*, contradicts: bool = False) -> ServedDelta:
    return ServedDelta(
        authored_at=_NOW,
        body="run the deprecation check on internal interfaces too",
        contradiction_note="the declared set treats internal interfaces as exempt" if contradicts else None,
        contradicts=contradicts,
        delta_id=uuid.uuid4(),
    )


def _resolver(outcome: DeclarationOutcome, *, session: type[_Session] = _Session) -> tuple[Any, _Channel, _Receipts]:
    channel = _Channel(outcome)
    receipts = _Receipts()
    empty = _Empty()
    arms = ContextArms(
        arc_receipts=empty,  # type: ignore[arg-type]
        claims=empty,  # type: ignore[arg-type]
        instructions=channel,  # type: ignore[arg-type]
        recall=empty,  # type: ignore[arg-type]
        retrieval=empty,  # type: ignore[arg-type]
        session_factory=session,  # type: ignore[arg-type]
    )
    resolver = ContextResolver(arms=arms, instruction_channel=channel, receipts=receipts)  # type: ignore[arg-type]
    return resolver, channel, receipts


async def _resolve(outcome: DeclarationOutcome, **kwargs: Any) -> tuple[Any, _Channel, _Receipts]:
    resolver, channel, receipts = _resolver(outcome, **kwargs)
    resolved = await resolver.resolve(_ctx(), query="payments", moment=_NOW, instruction_digest=outcome.digest)
    return resolved, channel, receipts


# --- the disposition reaches the caller ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [Disposition.NOT_DECLARED, Disposition.DECLARED_UNKNOWN, Disposition.DECLARED_KNOWN],
)
async def test_every_disposition_reaches_the_caller_with_the_note_that_explains_it(
    disposition: Disposition,
) -> None:
    """All three empty blocks, each saying which empty it is. Only one of them is
    a state the caller can leave by doing something."""
    digest = None if disposition is Disposition.NOT_DECLARED else _DIGEST
    resolved, _, _ = await _resolve(DeclarationOutcome(digest=digest, disposition=disposition))

    assert resolved.instruction_disposition is disposition
    assert resolved.envelope.block(BLOCK_INSTRUCTIONS).state == BLOCK_EMPTY
    assert resolved.instruction_block_note == BLOCK_NOTES[disposition]


@pytest.mark.asyncio
async def test_a_served_delta_carries_no_note_because_there_is_no_emptiness_to_explain() -> None:
    """A note attached regardless would train callers to ignore it."""
    resolved, _, _ = await _resolve(
        DeclarationOutcome(deltas=(_delta(),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)
    )

    assert resolved.envelope.block(BLOCK_INSTRUCTIONS).state == BLOCK_SUCCESS
    assert resolved.instruction_block_note is None


@pytest.mark.asyncio
async def test_the_receipt_records_the_digest_and_never_the_content() -> None:
    """Copying the content into every receipt would put the caller's instruction
    set on every resolution in the product -- the second copy this channel was
    designed to avoid."""
    _, _, receipts = await _resolve(
        DeclarationOutcome(deltas=(_delta(),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)
    )

    assert receipts.request["instruction_digest"] == _DIGEST
    assert not any("content" in key for key in receipts.request)


@pytest.mark.asyncio
async def test_declaring_nothing_puts_nothing_in_the_receipt() -> None:
    _, _, receipts = await _resolve(DeclarationOutcome(digest=None, disposition=Disposition.NOT_DECLARED))

    assert "instruction_digest" not in receipts.request


# --- the floor, and what it does to the record --------------------------------


@pytest.mark.asyncio
async def test_the_instruction_block_obeys_the_same_floor_as_the_rest_of_the_envelope() -> None:
    """An unsuppressible block is a channel around every floor the product has.

    A delta is the most behaviour-changing thing the envelope can carry, so if it
    could reach an agent when the other arms were refusing to serve, the
    instruction channel would be the way to say to an agent what the governed
    channels refuse to say.
    """
    resolved, _, _ = await _resolve(
        DeclarationOutcome(deltas=(_delta(),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN),
        session=_OverdueSession,
    )

    block = resolved.envelope.block(BLOCK_INSTRUCTIONS)
    assert block.state == BLOCK_FAILED
    assert block.items == ()
    assert "OverdueDerivativeRefusal" in (block.reason or ""), (
        "the same refusal the claims and workspace arms raise, so a caller that catches one "
        "is protected on all three"
    )


@pytest.mark.asyncio
async def test_a_contradiction_the_floor_withheld_is_not_recorded_as_served() -> None:
    """The rule that keeps the record honest. An evaluator reading "a
    contradiction was served" would act on it; the agent never saw one."""
    _, channel, _ = await _resolve(
        DeclarationOutcome(deltas=(_delta(contradicts=True),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN),
        session=_OverdueSession,
    )

    (recorded,) = channel.recorded
    assert recorded.deltas == ()
    assert recorded.contradiction_note() is None
    assert recorded.disposition is Disposition.DECLARED_KNOWN, "what was declared is still what was declared"


@pytest.mark.asyncio
async def test_a_contradiction_that_reached_the_agent_is_recorded_as_served() -> None:
    _, channel, _ = await _resolve(
        DeclarationOutcome(deltas=(_delta(contradicts=True),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)
    )

    (recorded,) = channel.recorded
    assert recorded.contradiction_note() is not None


@pytest.mark.asyncio
async def test_the_declaration_is_recorded_even_when_nothing_was_declared() -> None:
    """The channel decides whether that writes a row; the resolver always tells
    it. A resolver that skipped the call for one disposition would make the
    decision in two places."""
    _, channel, _ = await _resolve(DeclarationOutcome(digest=None, disposition=Disposition.NOT_DECLARED))

    assert [outcome.disposition for outcome in channel.recorded] == [Disposition.NOT_DECLARED]


# --- the guard is the real one ------------------------------------------------


def test_the_overdue_refusal_is_the_one_the_other_arms_raise() -> None:
    """A refusal type of this arm's own would be caught by a caller protected on
    whichever arm it happened to name."""
    assert issubclass(workspace_recall.OverdueDerivativeRefusal, Exception)


@pytest.mark.asyncio
async def test_a_declaration_the_resolver_did_not_change_is_passed_through_unchanged() -> None:
    """`dataclasses.replace` on the served path would be a second object with the
    same fields; the identity check catches a copy nobody needed."""
    outcome = DeclarationOutcome(deltas=(_delta(),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)
    _, channel, _ = await _resolve(outcome)

    assert channel.recorded[0] is outcome


def test_the_resolved_context_has_no_default_for_the_disposition() -> None:
    """Always set, including `NOT_DECLARED`. A default would let a construction
    site omit it and record the most common state by accident."""
    fields = {field.name: field for field in dataclasses.fields(_resolved_context_type())}

    assert fields["instruction_disposition"].default is dataclasses.MISSING


def _resolved_context_type() -> type:
    from contextplane.context.resolve import ResolvedContext

    return ResolvedContext
