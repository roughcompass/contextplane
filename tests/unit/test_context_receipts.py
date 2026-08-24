"""The parts of receipt writing that hold without a database.

The request digest and the trust rule are both things a wrong implementation
gets away with for a long time: a digest that varies with key order makes every
comparison a false negative, and a canonical item carrying trust reads as more
carefully labelled rather than as wrong. Neither shows up as a failure anywhere
downstream, so both are asserted here.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.context.assembler import (
    ArmOutcome,
    Exclusion,
    SelectionEvidence,
    canonical_item,
    contextual_item,
)
from contextplane.context.quality import derive_quality
from contextplane.context.receipts import (
    ContextReceiptService,
    canonical_items_carry_no_trust,
    request_digest,
)
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_INSTRUCTIONS,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ContextBlockV1,
    ContextEnvelopeV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import TrustMetadataV1
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)


class _Clock:
    def now(self) -> datetime.datetime:
        return _NOW


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=str(uuid.uuid4()), roles=["member"])


def _trust() -> TrustMetadataV1:
    return TrustMetadataV1(
        trust="observed",
        source="probe",
        assertion_kind="annotation",
        authority="agent-a",
        freshness=_NOW,
        mutability="mutable",
        attribution="agent-a",
        classification="internal",
    )


def _envelope() -> ContextEnvelopeV1:
    blocks = (
        ContextBlockV1(
            name=BLOCK_CANONICAL,
            state=BLOCK_SUCCESS,
            items=(canonical_item(source="catalog", item_key="cap-1", payload={"name": "cap-1"}),),
        ),
        ContextBlockV1(
            name=BLOCK_ARC,
            state=BLOCK_SUCCESS,
            items=(contextual_item(block=BLOCK_ARC, source="arc", item_key="a1", payload={"k": 1}, trust=_trust()),),
        ),
        ContextBlockV1(
            name=BLOCK_OBSERVED_CLAIMS,
            state=BLOCK_SUCCESS,
            items=(
                contextual_item(
                    block=BLOCK_OBSERVED_CLAIMS, source="memory", item_key="c1", payload={"k": 2}, trust=_trust()
                ),
            ),
        ),
        ContextBlockV1(
            name=BLOCK_WORKSPACE,
            state=BLOCK_SUCCESS,
            items=(
                contextual_item(
                    block=BLOCK_WORKSPACE, source="intent_checkpoint", item_key="cp1", payload={"k": 3}, trust=_trust()
                ),
            ),
        ),
        ContextBlockV1(
            name=BLOCK_INSTRUCTIONS,
            state=BLOCK_SUCCESS,
            items=(
                contextual_item(
                    block=BLOCK_INSTRUCTIONS,
                    source="contextplane.instruction_delta",
                    item_key="d1",
                    payload={"k": 4},
                    trust=_trust(),
                ),
            ),
        ),
    )
    return ContextEnvelopeV1(blocks=blocks, quality=derive_quality(blocks), state=derive_envelope_state(blocks))


# --- The request digest -------------------------------------------------------


def test_the_same_request_digests_the_same_however_it_was_built() -> None:
    """Two resolutions are only comparable if the thing they answered is
    comparable. A digest that varied with key order would make every comparison
    a false negative, which reads as "nothing matched" rather than as a bug."""
    first = request_digest({"subject": "cap-1", "depth": 2})
    second = request_digest({"depth": 2, "subject": "cap-1"})

    assert first == second


def test_a_different_request_digests_differently() -> None:
    """The other half. Without it the digest could be a constant."""
    assert request_digest({"subject": "cap-1"}) != request_digest({"subject": "cap-2"})


def test_the_digest_survives_values_json_cannot_encode() -> None:
    """A request carrying a UUID or a datetime must still digest rather than
    raising -- the alternative is a receipt that silently records no request."""
    digest = request_digest({"at": _NOW, "depth": 1})

    assert len(digest) == 64


# --- The trust rule -----------------------------------------------------------


def test_canonical_items_carry_no_trust() -> None:
    """Attaching one invites the question of whether another authority could
    have supplied the registry's own answer."""
    assert canonical_items_carry_no_trust(_envelope())


def test_every_non_canonical_item_carries_trust() -> None:
    """The rule the receipt's own CHECK enforces, stated where a reader of the
    writer can see it."""
    envelope = _envelope()
    for block in envelope.blocks:
        if block.name == BLOCK_CANONICAL:
            continue
        for item in block.items:
            assert item.trust is not None, f"{block.name} item {item.receipt_item_id.value()} has no trust"


# --- What a receipt has to carry ----------------------------------------------


def test_selection_evidence_keeps_what_the_envelope_throws_away() -> None:
    """The envelope carries what survived. If the evidence does not carry what
    was dropped, nothing later can reconstruct it -- which is the whole reason
    exclusions are persisted rather than derived."""
    evidence = SelectionEvidence(
        block=BLOCK_WORKSPACE,
        state="degraded",
        considered=9,
        returned=1,
        exclusions=(Exclusion(item_key="task-9", reason="no active participant grant"),),
        truncated_by_cap=True,
        truncated_by_arm=False,
        fresh_as_of=_NOW,
        stale=False,
        duration_ms=12,
    )
    envelope = _envelope()

    # Nothing in the envelope names task-9 or says nine were considered.
    rendered = str([(block.name, block.state, len(block.items)) for block in envelope.blocks])
    assert "task-9" not in rendered
    assert evidence.exclusions[0].item_key == "task-9"
    assert (evidence.considered, evidence.returned) == (9, 1)


def test_an_arm_that_returned_nothing_still_reports_what_it_considered() -> None:
    """ "Nothing came back" and "nothing was there" are different answers, and
    only the counts distinguish them."""
    outcome = ArmOutcome(items=(), exclusions=(Exclusion(item_key="t", reason="withheld"),))

    assert outcome.items == ()
    assert outcome.exclusions[0].reason == "withheld"


def test_the_blocks_are_the_ones_the_receipt_will_store() -> None:
    """The receipt writes one arm row per block. If the envelope ever carried a
    different set, the receipt would silently record fewer arms than ran."""
    assert tuple(block.name for block in _envelope().blocks) == BLOCK_NAMES


# --- reading a receipt's arms back --------------------------------------------


class _CapturingSession:
    """Records the statement it was asked to execute, and returns nothing.

    The rows are not the interesting part -- a fake that echoed rows back would
    assert only that this module can return what it was handed. What can
    silently regress is the *shape of the query*, so that is what is captured.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)

        class _Result:
            @staticmethod
            def scalars() -> Any:
                class _Scalars:
                    @staticmethod
                    def all() -> list[Any]:
                        return []

                return _Scalars()

            @staticmethod
            def scalar_one_or_none() -> Any:
                """No receipt row, which the servability check reads as nothing
                to refuse. The row-level reads answer an absent receipt with an
                empty tuple, exactly as they did before that check existed, and
                the transports answer 404 from `get`."""
                return None

        return _Result()

    async def __aenter__(self) -> _CapturingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _compiled(statement: Any) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": False}))


def _statement_touching(session: Any, table: str) -> str:
    """The emitted statement that reads *table*, whichever order it was issued in.

    Not `statements[0]`: these reads now load the receipt header first, to refuse
    one that may not be shown. Indexing by position would make every future read
    added ahead of this one look like a missing join, which is the assertion
    below, and it would fail for a reason that has nothing to do with tenancy.
    """
    for statement in session.statements:
        sql = _compiled(statement)
        if table in sql:
            return sql
    raise AssertionError(f"no statement read {table}")


@pytest.mark.asyncio
async def test_reading_a_receipts_arms_scopes_the_read_to_the_tenant() -> None:
    """The arms table carries no tenant of its own.

    Read by `receipt_id` alone it would hand another tenant's resolution shape
    to anyone who guessed an id, so the tenant predicate has to arrive through a
    join back to the receipt. Asserted on the emitted SQL rather than on returned
    rows, because a fake cannot tell you the join is missing -- it can only tell
    you what it was told to return.
    """
    session = _CapturingSession()
    service = ContextReceiptService(session_factory=lambda: session, clock=_Clock())  # type: ignore[arg-type]

    await service.arms_for(_ctx(), receipt_id=uuid.uuid4())

    sql = _statement_touching(session, "context_receipt_arms")
    assert "context_receipt_arms" in sql
    assert "JOIN context_receipts" in sql
    assert "context_receipts.tenant_id" in sql


@pytest.mark.asyncio
async def test_reading_a_receipts_arms_orders_them_so_two_reads_agree() -> None:
    """A caller digesting these -- and a handoff does -- needs a stable sequence.

    Unordered, the same receipt would digest differently depending on how the
    rows came back, and the handoff that compares those digests would refuse a
    handoff nothing was wrong with.
    """
    session = _CapturingSession()
    service = ContextReceiptService(session_factory=lambda: session, clock=_Clock())  # type: ignore[arg-type]

    await service.arms_for(_ctx(), receipt_id=uuid.uuid4())

    assert "ORDER BY context_receipt_arms.block" in _statement_touching(session, "context_receipt_arms")
