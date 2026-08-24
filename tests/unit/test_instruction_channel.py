"""The instruction channel, over fakes: dispositions, contradiction, items.

E22-T14. What is proved here is the part that has no database in it -- the three
dispositions, what each empty block says, how a contradiction is summarised, and
the trust an item carries. The reads themselves are proved against a real
database in `tests/integration/test_instruction_channel_surfaces.py`, because a
fake row source cannot show that the tenant predicate is in the query.

**The disposition tests are the load-bearing ones.** The decision behind this
channel is emphatic that three states are distinguished and never two, because
collapsing "declared nothing" into "declared something we have never seen" is
what makes partial adoption of the channel invisible -- and a surface built on a
signal that arrives some of the time, reporting as though it arrived always, is
the failure its dissent predicts.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.context import instructions
from contextplane.context.instructions import (
    BLOCK_NOTES,
    DELTA_SOURCE,
    DeclarationOutcome,
    Disposition,
    InstructionChannel,
    InvalidInstructionDeclaration,
    ServedDelta,
    delta_items,
    digest_of,
    validated_digest,
)
from contextplane.context.schemas.envelope import BLOCK_INSTRUCTIONS
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 24, 9, 0, tzinfo=datetime.UTC)
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACTOR = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DIGEST = digest_of("always run the deprecation check")


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=("consumer",))


class _Row:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Session:
    """Answers each statement from a scripted queue, and records what it wrote.

    Keyed on the order the channel issues its reads rather than on SQL text: a
    fake that matched statements by substring would pass while the query it
    matched had lost its tenant predicate, which is the one thing a fake must
    not be able to say is fine.
    """

    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = answers
        self.written: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        text = str(statement).strip().upper()
        if text.startswith("INSERT"):
            self.written.append(dict(params or {}))
            return _Row()
        rows = self._answers.pop(0) if self._answers else []

        class _Result:
            @staticmethod
            def first() -> Any:
                return rows[0] if rows else None

            @staticmethod
            def all() -> list[Any]:
                return rows

        return _Result()

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _Session:
        return self


def _channel(*answers: list[Any]) -> tuple[InstructionChannel, _Session]:
    session = _Session(list(answers))
    return InstructionChannel(lambda: session), session


def _delta(*, contradicts: bool = False, note: str | None = None) -> ServedDelta:
    return ServedDelta(
        authored_at=_NOW,
        body="run the deprecation check on internal interfaces too",
        contradiction_note=note,
        contradicts=contradicts,
        delta_id=uuid.uuid4(),
    )


# --- the digest ---------------------------------------------------------------


def test_the_same_content_digests_the_same_and_different_content_does_not() -> None:
    assert digest_of("a") == digest_of("a")
    assert digest_of("a") != digest_of("b")


def test_a_digest_is_refused_unless_it_is_the_one_spelling_that_joins() -> None:
    """A malformed digest stored on a declaration can never match a submission,
    and is then indistinguishable from an integration that submitted nothing."""
    for bad in ("", "abc", "sha256:" + "F" * 64, "sha256:" + "a" * 63, "a" * 64):
        with pytest.raises(InvalidInstructionDeclaration, match="64 lowercase hex"):
            validated_digest(bad)

    assert validated_digest(_DIGEST) == _DIGEST


# --- the three dispositions ---------------------------------------------------


@pytest.mark.asyncio
async def test_declaring_nothing_is_its_own_disposition_and_reads_nothing() -> None:
    """`not_declared` is a state, not a missing field. A caller that has to infer
    it from an absent value will infer it for `declared_unknown` too, and those
    are exactly the pair whose conflation hides partial adoption."""
    channel, session = _channel()

    outcome = await channel.resolve_declaration(_ctx(), digest=None, limit=10)

    assert outcome.disposition is Disposition.NOT_DECLARED
    assert outcome.digest is None
    assert outcome.deltas == ()
    assert session.written == [], "declaring nothing should not touch the database at all"


@pytest.mark.asyncio
async def test_an_unknown_digest_resolves_rather_than_failing() -> None:
    """Refusing would fail a first-run resolve for a state the service is in
    rather than one the caller caused."""
    channel, _ = _channel([])

    outcome = await channel.resolve_declaration(_ctx(), digest=_DIGEST, limit=10)

    assert outcome.disposition is Disposition.DECLARED_UNKNOWN
    assert outcome.digest == _DIGEST
    assert outcome.deltas == ()


@pytest.mark.asyncio
async def test_a_known_digest_carries_its_deltas() -> None:
    channel, _ = _channel(
        [_Row(present=1)],
        [_Row(authored_at=_NOW, body="b", contradiction_note=None, contradicts=False, delta_id=uuid.uuid4())],
    )

    outcome = await channel.resolve_declaration(_ctx(), digest=_DIGEST, limit=10)

    assert outcome.disposition is Disposition.DECLARED_KNOWN
    assert len(outcome.deltas) == 1


@pytest.mark.asyncio
async def test_a_known_digest_with_no_delta_is_not_the_same_state_as_an_unknown_one() -> None:
    """The two empty blocks a caller can receive after declaring, and the whole
    reason the disposition is carried separately from the block."""
    known, _ = _channel([_Row(present=1)], [])
    unknown, _ = _channel([])

    served = await known.resolve_declaration(_ctx(), digest=_DIGEST, limit=10)
    missing = await unknown.resolve_declaration(_ctx(), digest=_DIGEST, limit=10)

    assert served.deltas == missing.deltas == ()
    assert served.disposition is not missing.disposition
    assert BLOCK_NOTES[served.disposition] != BLOCK_NOTES[missing.disposition]


def test_every_disposition_has_a_note_and_no_two_say_the_same_thing() -> None:
    """Three identical empty blocks with different causes teach a caller to
    ignore all three."""
    assert set(BLOCK_NOTES) == set(Disposition)
    assert len(set(BLOCK_NOTES.values())) == len(Disposition)


@pytest.mark.asyncio
async def test_a_malformed_digest_is_refused_before_anything_is_read() -> None:
    channel, session = _channel([_Row(present=1)])

    with pytest.raises(InvalidInstructionDeclaration):
        await channel.resolve_declaration(_ctx(), digest="not-a-digest", limit=10)

    assert session.written == []


# --- contradiction ------------------------------------------------------------


def test_a_contradiction_is_named_rather_than_counted() -> None:
    """A resolution saying "2 contradictions" gives an evaluator nothing to act
    on; the decision requires the record to say what was contradicted."""
    outcome = DeclarationOutcome(
        deltas=(
            _delta(contradicts=True, note="the declared set skips deprecation review"),
            _delta(),
            _delta(contradicts=True, note="the declared set treats internal interfaces as exempt"),
        ),
        digest=_DIGEST,
        disposition=Disposition.DECLARED_KNOWN,
    )

    note = outcome.contradiction_note()

    assert note is not None
    assert "skips deprecation review" in note
    assert "internal interfaces as exempt" in note
    assert len(outcome.contradictions) == 2


def test_no_contradiction_is_no_note_rather_than_an_empty_string() -> None:
    outcome = DeclarationOutcome(deltas=(_delta(), _delta()), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)

    assert outcome.contradiction_note() is None
    assert outcome.contradictions == ()


# --- the record ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_declaring_nothing_writes_no_row() -> None:
    """The absence is the record. A row per undeclared resolve would put a row on
    every resolution in the product to say nothing happened."""
    channel, session = _channel()

    await channel.record(
        _ctx(),
        now=_NOW,
        outcome=DeclarationOutcome(digest=None, disposition=Disposition.NOT_DECLARED),
        receipt_id=uuid.uuid4(),
    )

    assert session.written == []


@pytest.mark.asyncio
async def test_an_unknown_declaration_is_recorded_as_content_unknown() -> None:
    channel, session = _channel()

    await channel.record(
        _ctx(),
        now=_NOW,
        outcome=DeclarationOutcome(digest=_DIGEST, disposition=Disposition.DECLARED_UNKNOWN),
        receipt_id=None,
    )

    assert len(session.written) == 1
    assert session.written[0]["content_known"] is False
    assert session.written[0]["contradicted"] is False
    assert session.written[0]["receipt_id"] is None, "a resolution with no receipt still declared something"


@pytest.mark.asyncio
async def test_a_served_contradiction_is_recorded_with_what_it_contradicted() -> None:
    channel, session = _channel()

    await channel.record(
        _ctx(),
        now=_NOW,
        outcome=DeclarationOutcome(
            deltas=(_delta(contradicts=True, note="the declared set skips deprecation review"),),
            digest=_DIGEST,
            disposition=Disposition.DECLARED_KNOWN,
        ),
        receipt_id=uuid.uuid4(),
    )

    written = session.written[0]
    assert written["contradicted"] is True
    assert "skips deprecation review" in written["contradiction_note"]


# --- submission ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_submitting_returns_the_digest_of_what_was_submitted() -> None:
    channel, session = _channel()

    digest = await channel.submit(_ctx(), content="  always run the deprecation check  ", now=_NOW)

    assert digest == _DIGEST, "content is stripped before digesting, so whitespace is not a second set"
    assert session.written[0]["digest"] == _DIGEST
    assert session.written[0]["content"] == "always run the deprecation check"


@pytest.mark.asyncio
async def test_an_empty_instruction_set_is_not_a_declaration() -> None:
    channel, session = _channel()

    with pytest.raises(InvalidInstructionDeclaration, match="no content"):
        await channel.submit(_ctx(), content="   ", now=_NOW)

    assert session.written == []


@pytest.mark.asyncio
async def test_an_oversized_instruction_set_is_refused_with_the_bound_named() -> None:
    """Refused here rather than by a constraint violation the caller cannot read."""
    channel, _ = _channel()

    with pytest.raises(InvalidInstructionDeclaration, match=str(instructions.MAX_CONTENT_CHARS)):
        await channel.submit(_ctx(), content="x" * (instructions.MAX_CONTENT_CHARS + 1), now=_NOW)


# --- the items an agent receives ----------------------------------------------


def test_a_delta_item_carries_the_contradiction_flag_in_its_payload() -> None:
    """The agent is the party that has to weigh a correction against what its
    operator told it, and a flag only an evaluator sees days later does not help
    it do that."""
    outcome = DeclarationOutcome(
        deltas=(_delta(contradicts=True, note="the declared set skips deprecation review"),),
        digest=_DIGEST,
        disposition=Disposition.DECLARED_KNOWN,
    )

    (item,) = delta_items(outcome)

    assert item.payload["contradicts"] is True
    assert item.payload["contradiction_note"] == "the declared set skips deprecation review"
    assert item.receipt_item_id.block == BLOCK_INSTRUCTIONS


def test_a_delta_is_asserted_policy_and_never_attested() -> None:
    """Authoring a delta is not an attestation path. Promoting it to one would
    let a correction reach an agent wearing the weight of a governed artifact."""
    outcome = DeclarationOutcome(deltas=(_delta(),), digest=_DIGEST, disposition=Disposition.DECLARED_KNOWN)

    (item,) = delta_items(outcome)

    assert item.trust is not None
    assert item.trust.trust == "asserted"
    assert item.trust.assertion_kind == "policy"
    assert item.trust.mutability == "mutable", "a delta can be withdrawn after this read"
    assert item.trust.source == DELTA_SOURCE


def test_no_deltas_is_no_items() -> None:
    assert delta_items(DeclarationOutcome(digest=None, disposition=Disposition.NOT_DECLARED)) == ()
