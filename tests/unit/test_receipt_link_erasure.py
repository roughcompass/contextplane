"""Reducing a receipt instead of deleting it, and finding one to reduce.

A receipt is evidence that a resolution happened, and things downstream depend on
it existing — most concretely the feedback rows that cite it, whose foreign keys
deliberately do not cascade. So an erasure that reached receipts by deleting them
would fail on exactly the receipts that most need erasing: the ones somebody
reported on. Everything here follows from that: the handler reduces on all three
operations, the participant minimizes rather than removes, and the registrar
records enough links for an erasure to find the receipt in the first place.

The other half is idempotence. Propagation retries; a receipt already reduced must
come back as zero-touched success rather than as work redone or as an error. That
is why the minimization skips its own marker and why these tests run twice.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.context import derivative_handlers as handlers
from contextplane.retention import derivatives, policies, tombstones
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_TENANT = uuid.UUID("66666666-6666-6666-6666-666666666666")
_ACTOR = uuid.UUID("77777777-7777-7777-7777-777777777777")
_RECEIPT = uuid.UUID("88888888-8888-8888-8888-888888888888")
_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_SALT = b"a-tenant-salt-of-adequate-length"


class _Salts:
    """A resolver that answers, so these tests are about the reduction and not the key."""

    def salt_for(self, tenant_id: uuid.UUID) -> bytes:
        return _SALT


class _AsyncCM:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Answers the two reads this module makes and records every write.

    Keyed on the table each statement names rather than on the whole SQL: what these
    tests assert is which rows were reduced and what replaced them, and pinning the
    statement text would make every reformat a test failure.
    """

    def __init__(self, *, items: list[SimpleNamespace] | None = None, receipts: list[uuid.UUID] | None = None) -> None:
        self.items = items if items is not None else []
        self.receipts = receipts if receipts is not None else []
        self.feedback_count = 0
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.deleted_bindings = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))
        if "FROM context_receipt_items" in sql:
            # Only the keys that still say something, which is what the SQL's own
            # NOT LIKE does against a real database.
            live = [row for row in self.items if not tombstones.is_erased_key(str(row.item_key))]
            return SimpleNamespace(all=lambda: live)
        if "FROM context_receipts" in sql:
            return SimpleNamespace(all=lambda: [(receipt_id,) for receipt_id in self.receipts])
        if "FROM context_feedback" in sql:
            return SimpleNamespace(scalar=lambda: self.feedback_count)
        if sql.startswith("UPDATE context_receipt_items"):
            for row in self.items:
                if row.item_row_id == params["row"]:  # type: ignore[index]
                    row.item_key = params["marker"]  # type: ignore[index]
            return SimpleNamespace(rowcount=1)
        if sql.startswith("DELETE FROM context_reference_bindings"):
            deleted, self.deleted_bindings = self.deleted_bindings, 0
            return SimpleNamespace(rowcount=deleted)
        return SimpleNamespace(rowcount=0)

    async def commit(self) -> None:
        self.commits += 1

    def statements_touching(self, table: str) -> list[dict[str, Any]]:
        return [params for sql, params in self.executed if table in sql]


def _item(key: str) -> SimpleNamespace:
    return SimpleNamespace(item_row_id=uuid.uuid4(), item_key=key)


def _registration(receipt_id: uuid.UUID = _RECEIPT) -> derivatives.Registration:
    return derivatives.Registration(
        derivative_id=uuid.uuid4(),
        tenant_id=_TENANT,
        derivative_kind=derivatives.KIND_RECEIPT_LINK,
        storage_locator=handlers.locator_for(receipt_id),
        audience_partition="tenant",
        classification="internal",
        expires_at=_NOW,
        blocking=True,
    )


def _factory(session: _FakeSession) -> MagicMock:
    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    return factory


# --- the locator ---------------------------------------------------------------


def test_a_locator_round_trips_through_the_pair_that_writes_and_reads_it() -> None:
    """One spelling, two callers. A locator the handler cannot read is a derivative
    that stays registered and never gets reduced."""
    assert handlers.receipt_from_locator(handlers.locator_for(_RECEIPT)) == _RECEIPT


@pytest.mark.parametrize("locator", ["", "receipt:", "receipt:not-a-uuid", "vector:1234", str(_RECEIPT)])
def test_a_locator_this_handler_cannot_read_is_refused_rather_than_guessed(locator: str) -> None:
    """Guessing would reduce the wrong receipt, or none, while reporting success."""
    with pytest.raises(derivatives.UnhandledDerivativeKind):
        handlers.receipt_from_locator(locator)


# --- the handler ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_item_key_is_replaced_by_a_keyed_marker() -> None:
    """The item key names what somebody was reading about, so it is the field that
    goes. Keyed rather than blanked: the marker has to be the same on a retry
    without being a lookup table anyone can probe with candidate keys."""
    session = _FakeSession(items=[_item("catalog:svc-checkout"), _item("workspace:notes")])

    touched = await handlers.ReceiptLinkHandler(_Salts()).apply(
        session,  # type: ignore[arg-type]
        _registration(),
        derivatives.OPERATION_REDACT,
    )

    assert touched == 2
    for row in session.items:
        assert tombstones.is_erased_key(str(row.item_key))
    # Keyed in the original, not a constant: two different keys must not collapse
    # to one marker, or the receipt would read as having returned the same thing twice.
    assert len({row.item_key for row in session.items}) == 2


@pytest.mark.asyncio
async def test_the_marker_is_what_the_shared_keying_function_produces() -> None:
    """Restating the HMAC here would give the erasure a second definition of an
    erased key, and `is_erased_key` would stop recognising one of them."""
    session = _FakeSession(items=[_item("catalog:svc-checkout")])

    await handlers.ReceiptLinkHandler(_Salts()).apply(
        session,  # type: ignore[arg-type]
        _registration(),
        derivatives.OPERATION_REDACT,
    )

    assert session.items[0].item_key == tombstones.erased_item_key(_SALT, "catalog:svc-checkout")


@pytest.mark.asyncio
async def test_reducing_an_already_reduced_receipt_touches_nothing() -> None:
    """A retried propagation item is the normal recovery path. Re-keying its own
    marker would produce a different marker on every run, which is what would make
    the retry visible in the data instead of free."""
    handler = handlers.ReceiptLinkHandler(_Salts())
    session = _FakeSession(items=[_item("catalog:svc-checkout")])

    first = await handler.apply(session, _registration(), derivatives.OPERATION_REDACT)  # type: ignore[arg-type]
    marker = session.items[0].item_key
    second = await handler.apply(session, _registration(), derivatives.OPERATION_REDACT)  # type: ignore[arg-type]

    assert (first, second) == (1, 0)
    assert session.items[0].item_key == marker


@pytest.mark.asyncio
async def test_zero_touched_is_a_success_rather_than_a_failure_to_find_work() -> None:
    """Reporting it as an error would turn recovery into a compliance incident and
    make the queue's failed count mean nothing."""
    touched = await handlers.ReceiptLinkHandler(_Salts()).apply(
        _FakeSession(),  # type: ignore[arg-type]
        _registration(),
        derivatives.OPERATION_DELETE,
    )

    assert touched == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(derivatives.OPERATIONS))
async def test_delete_reduces_like_every_other_operation(operation: str) -> None:
    """Implemented literally, `delete` would fail on exactly the receipts that most
    need erasing — the ones with feedback, whose foreign keys refuse the removal."""
    session = _FakeSession(items=[_item("catalog:svc-checkout")])

    touched = await handlers.ReceiptLinkHandler(_Salts()).apply(
        session,  # type: ignore[arg-type]
        _registration(),
        operation,
    )

    assert touched == 1
    assert tombstones.is_erased_key(str(session.items[0].item_key))


@pytest.mark.asyncio
async def test_an_operation_the_schema_does_not_store_is_refused() -> None:
    with pytest.raises(derivatives.UnhandledDerivativeKind):
        await handlers.ReceiptLinkHandler(_Salts()).apply(
            _FakeSession(),  # type: ignore[arg-type]
            _registration(),
            "shred",
        )


@pytest.mark.asyncio
async def test_the_bindings_that_say_this_receipt_cited_a_reference_are_deleted() -> None:
    """The reference survives — another subject may still cite it. The fact that
    this receipt did is the link being erased, and nothing cascades it."""
    session = _FakeSession()
    session.deleted_bindings = 3

    touched = await handlers.ReceiptLinkHandler(_Salts()).apply(
        session,  # type: ignore[arg-type]
        _registration(),
        derivatives.OPERATION_REDACT,
    )

    assert touched == 3
    assert session.statements_touching("DELETE FROM context_reference_bindings")


def test_the_handler_claims_the_kind_the_registry_will_look_it_up_by() -> None:
    """A handler registered under the wrong kind covers nothing and leaves the kind
    it should have covered in the release gate's unhandled set."""
    assert handlers.ReceiptLinkHandler(_Salts()).kind == derivatives.KIND_RECEIPT_LINK
    assert handlers.ReceiptLinkHandler(_Salts()).version == handlers.HANDLER_VERSION


# --- the participant -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_participant_minimizes_every_receipt_the_actor_requested() -> None:
    session = _FakeSession(receipts=[_RECEIPT], items=[_item("catalog:svc-checkout")])
    participant = handlers.ReceiptErasure(_factory(session), _Salts(), clock=FakeClock(_NOW))

    counts = await participant.erase_actor(tenant_context(tenant_id=_TENANT, actor_id=_ACTOR), _ACTOR)

    assert counts["receipts"] == 1
    assert counts["artefacts"] == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_receipt_with_feedback_is_minimized_and_reported_as_retained() -> None:
    """This is the block being resolved. The feedback foreign key refuses a delete,
    on purpose — a person's report has standing independent of what it cites — so
    the erasure succeeds by reducing the receipt and leaving the row for the report
    to keep pointing at."""
    session = _FakeSession(receipts=[_RECEIPT], items=[_item("catalog:svc-checkout")])
    session.feedback_count = 2
    participant = handlers.ReceiptErasure(_factory(session), _Salts(), clock=FakeClock(_NOW))

    counts = await participant.erase_actor(tenant_context(tenant_id=_TENANT, actor_id=_ACTOR), _ACTOR)

    assert counts["receipts"] == 1
    assert counts["receipts_with_feedback"] == 1
    assert tombstones.is_erased_key(str(session.items[0].item_key))


@pytest.mark.asyncio
async def test_the_actor_is_matched_as_text_because_that_is_how_a_receipt_records_them() -> None:
    """`requested_by` is TEXT, not a foreign key into actors: a uuid bound against
    it is an error, not a near miss."""
    session = _FakeSession(receipts=[])
    participant = handlers.ReceiptErasure(_factory(session), _Salts(), clock=FakeClock(_NOW))

    await participant.erase_actor(tenant_context(tenant_id=_TENANT, actor_id=_ACTOR), _ACTOR)

    (params,) = session.statements_touching("FROM context_receipts")
    assert params["actor"] == str(_ACTOR)


@pytest.mark.asyncio
async def test_an_actor_with_no_receipts_reports_zeros_rather_than_nothing() -> None:
    """Zero is a real answer; omitting it makes "nothing to do" indistinguishable
    from "this subsystem was never asked"."""
    session = _FakeSession(receipts=[])
    participant = handlers.ReceiptErasure(_factory(session), _Salts(), clock=FakeClock(_NOW))

    counts = await participant.erase_actor(tenant_context(tenant_id=_TENANT, actor_id=_ACTOR), _ACTOR)

    assert counts == {"receipts": 0, "receipts_with_feedback": 0, "artefacts": 0}


def test_the_participant_names_the_subsystem_it_reports_under() -> None:
    assert handlers.ReceiptErasure.subsystem == handlers.SUBSYSTEM


# --- the registrar -------------------------------------------------------------


def _envelope_item(source: str, item_key: str) -> SimpleNamespace:
    return SimpleNamespace(receipt_item_id=SimpleNamespace(source=source, item_key=item_key))


def _envelope(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(blocks=[SimpleNamespace(items=list(items))])


def test_the_source_names_are_the_ones_the_arms_actually_stamp() -> None:
    """Restated in the handler module because both live as module-private constants
    elsewhere. Pinned here because a restatement that drifted would map nothing —
    a registration with no links, silently, on every receipt."""
    from contextplane.context import arms, queries

    assert queries._WORKSPACE_SOURCE in handlers._SOURCE_RECORD_CLASSES
    assert arms._CLAIMS_SOURCE in handlers._SOURCE_RECORD_CLASSES
    assert handlers._SOURCE_RECORD_CLASSES[queries._WORKSPACE_SOURCE] == policies.RECORD_TASK_CHECKPOINT
    assert handlers._SOURCE_RECORD_CLASSES[arms._CLAIMS_SOURCE] == policies.RECORD_MEMORY_CLAIM


def test_every_erasable_record_the_receipt_quoted_becomes_a_link() -> None:
    """All of them, not the one that triggered the write: a derivative registered
    with a single source inherits that source's expiry and outlives the others."""
    checkpoint, claim = uuid.uuid4(), uuid.uuid4()
    refs = handlers.source_refs_for(
        _envelope(
            _envelope_item("task_checkpoint", str(checkpoint)),
            _envelope_item("living-memory", str(claim)),
        ),
        now=_NOW,
    )

    assert {(ref.record_class, ref.source_id) for ref in refs} == {
        (policies.RECORD_TASK_CHECKPOINT, checkpoint),
        (policies.RECORD_MEMORY_CLAIM, claim),
    }


def test_registry_owned_and_governance_material_is_not_linked() -> None:
    """A catalog entity and a resolved directive are not records a person authored,
    so neither carries anybody's content into the receipt."""
    refs = handlers.source_refs_for(
        _envelope(
            _envelope_item("catalog", str(uuid.uuid4())),
            _envelope_item("arc-receipt", str(uuid.uuid4())),
        ),
        now=_NOW,
    )

    assert refs == []


def test_the_same_record_returned_twice_is_linked_once() -> None:
    """The link table is unique per (derivative, class, id); a duplicate would abort
    the transaction the receipt is being written in."""
    claim = uuid.uuid4()
    refs = handlers.source_refs_for(
        _envelope(_envelope_item("living-memory", str(claim)), _envelope_item("living-memory", str(claim))),
        now=_NOW,
    )

    assert len(refs) == 1


def test_an_item_key_that_is_not_a_record_id_is_skipped_not_raised() -> None:
    """A lost registration costs a derivative nobody propagates. Raising here would
    cost the receipt itself, which is the evidence that the resolution happened."""
    claim = uuid.uuid4()
    refs = handlers.source_refs_for(
        _envelope(_envelope_item("living-memory", "not-a-uuid"), _envelope_item("living-memory", str(claim))),
        now=_NOW,
    )

    assert [ref.source_id for ref in refs] == [claim]


@pytest.mark.asyncio
async def test_a_receipt_that_quoted_nothing_erasable_registers_nothing() -> None:
    """A registration with no links has no source to expire with, and the schema
    refuses one with no expiry at all rather than storing a guessed horizon."""
    session = _FakeSession()

    registered = await handlers.register_receipt_links(
        session,  # type: ignore[arg-type]
        tenant_id=_TENANT,
        receipt_id=_RECEIPT,
        sources=[],
        now=_NOW,
    )

    assert registered is None
    assert session.executed == []
