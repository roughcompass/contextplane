"""The three carrier handlers: what they delete, and what they refuse to.

These handlers are the only ones whose *refusals* are the delivered behaviour
rather than an error path, so the refusals get the same attention here as the
deletions. Two of the three refuse everything today — there is no export store
and no erasable log projection — and the way that goes wrong is not a crash but
a handler that returns zero, which the propagation drain marks done. A test
suite that only checked the happy path would pass against exactly that mistake.

No database. A handler's whole job is to choose a statement from a closed map
and bind two parameters to it, and a fake session that records what was executed
proves both the choice and the binding. What the statements do to real rows is
the integration tier's question.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.retention import derivatives
from contextplane.service.operations import derivative_handlers as handlers

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: Every outbox table a propagation is allowed to delete from, paired with the
#: primary key its statement must use. Written out rather than derived from the
#: handler's own map: a test that read the map would pass whatever the map said,
#: including a statement pointed at the wrong column.
_OUTBOX_TABLES: tuple[tuple[str, str], ...] = (
    ("embedding_outbox", "outbox_id"),
    ("embedding_outbox_failed", "failed_id"),
    ("memory_extraction_outbox", "outbox_id"),
    ("memory_extraction_outbox_failed", "failed_id"),
    ("closure_outbox", "outbox_id"),
)


class _FakeSession:
    """Records statements and answers with a declared rowcount."""

    def __init__(self, rowcount: int = 1) -> None:
        self._rowcount = rowcount
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append((" ".join(str(statement).split()), params or {}))
        return _FakeResult(self._rowcount)


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _registration(
    *,
    kind: str,
    locator: str,
    tenant_id: uuid.UUID = _TENANT,
) -> derivatives.Registration:
    return derivatives.Registration(
        derivative_id=uuid.uuid4(),
        tenant_id=tenant_id,
        derivative_kind=kind,
        storage_locator=locator,
        audience_partition="tenant-internal",
        classification="internal",
        expires_at=_NOW + datetime.timedelta(days=1),
        blocking=False,
    )


# --- the locator format ----------------------------------------------------


def test_the_builder_and_the_parser_agree_on_the_locator_format() -> None:
    """A registrar using the exported builder produces something the handler reads."""
    row_id = uuid.uuid4()
    locator = handlers.carrier_locator(derivatives.KIND_OUTBOX, "closure_outbox", row_id)

    assert locator == f"outbox://closure_outbox/{row_id}"
    assert handlers._parse_locator(derivatives.KIND_OUTBOX, locator) == ("closure_outbox", row_id)


@pytest.mark.parametrize(
    "locator",
    [
        "closure_outbox/00000000-0000-0000-0000-000000000000",  # no scheme
        "vector://closure_outbox/00000000-0000-0000-0000-000000000000",  # another kind's scheme
        "outbox://closure_outbox",  # no row
        "outbox://closure_outbox/not-a-uuid-at-all-really-nope-xx",  # right length, not a uuid
        "outbox://closure_outbox; DROP TABLE tenants/00000000-0000-0000-0000-000000000000",
        "outbox:///00000000-0000-0000-0000-000000000000",  # no table
    ],
)
async def test_a_locator_that_is_not_the_agreed_shape_is_refused_before_any_statement_runs(locator: str) -> None:
    """A malformed locator never reaches the database, however it is malformed."""
    session = _FakeSession()
    registration = _registration(kind=derivatives.KIND_OUTBOX, locator=locator)

    with pytest.raises(handlers.UnresolvableLocator):
        await handlers.OutboxHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert session.executed == []


# --- outbox ----------------------------------------------------------------


@pytest.mark.parametrize(("table", "primary_key"), _OUTBOX_TABLES)
async def test_the_outbox_handler_deletes_the_named_row_from_each_product_outbox(table: str, primary_key: str) -> None:
    """Each of the five outboxes is deletable, by its own primary key."""
    row_id = uuid.uuid4()
    session = _FakeSession(rowcount=1)
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, table, row_id),
    )

    touched = await handlers.OutboxHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert touched == 1
    ((sql, params),) = session.executed
    assert sql == f"DELETE FROM {table} WHERE {primary_key} = :row_id AND tenant_id = :tenant_id"
    assert params == {"row_id": row_id, "tenant_id": _TENANT}


async def test_the_delete_is_scoped_to_the_registrations_own_tenant() -> None:
    """The tenant clause is bound from the registration, not from the locator.

    A locator is a uuid somebody else wrote down. Without the tenant clause it
    would be sufficient on its own to delete a row belonging to another tenant.
    """
    row_id = uuid.uuid4()
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, "embedding_outbox", row_id),
        tenant_id=_OTHER_TENANT,
    )

    await handlers.OutboxHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    _, params = session.executed[0]
    assert params["tenant_id"] == _OTHER_TENANT


@pytest.mark.parametrize("operation", derivatives.OPERATIONS)
async def test_every_operation_deletes_the_queued_copy(operation: str) -> None:
    """Rebuild and redact delete too: a queued payload has no partial form worth keeping."""
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, "embedding_outbox", uuid.uuid4()),
    )

    await handlers.OutboxHandler().apply(session, registration, operation)

    sql, _ = session.executed[0]
    assert sql.startswith("DELETE FROM embedding_outbox")


async def test_an_operation_outside_the_vocabulary_is_refused() -> None:
    """A caller that invented an operation hears about it rather than getting a delete."""
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, "embedding_outbox", uuid.uuid4()),
    )

    with pytest.raises(handlers.UnsupportedPropagationOperation):
        await handlers.OutboxHandler().apply(session, registration, "archive")

    assert session.executed == []


async def test_a_row_that_is_already_gone_is_a_success_not_a_refusal() -> None:
    """Zero touched on a resolvable locator is what a retry of a partial apply looks like."""
    session = _FakeSession(rowcount=0)
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, "closure_outbox", uuid.uuid4()),
    )

    touched = await handlers.OutboxHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert touched == 0


@pytest.mark.parametrize(
    "table",
    [
        "arc_audit_outbox",  # audit-bearing, exempt from erasure
        "derivative_work_outbox",  # the propagation queue itself
        "tenants",  # a table that is not a queue at all
        "embedding_outbox_archive",  # a plausible name that does not exist
    ],
)
async def test_an_outbox_the_map_does_not_name_is_refused_and_nothing_is_executed(table: str) -> None:
    """The closed map is the whole authorization: a table not in it is not deletable."""
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_OUTBOX,
        locator=handlers.carrier_locator(derivatives.KIND_OUTBOX, table, uuid.uuid4()),
    )

    with pytest.raises(handlers.UnresolvableLocator) as raised:
        await handlers.OutboxHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert table in str(raised.value)
    assert session.executed == []


def test_no_statement_is_assembled_from_locator_content() -> None:
    """Every statement is a literal written in the module, naming its own table.

    The property this pins is that the table a locator names selects a statement
    and never contributes text to one — so a locator carrying SQL has nowhere to
    land even if some future edit forgets why the map is closed.
    """
    for table, statement in handlers._OUTBOX_DELETES.items():
        assert statement.startswith(f"DELETE FROM {table} WHERE ")
        assert ":row_id" in statement
        assert ":tenant_id" in statement
        assert "{" not in statement and "%" not in statement


def test_the_outbox_map_covers_exactly_the_product_outboxes() -> None:
    """Adding an outbox to this product is a visible decision, made here."""
    assert set(handlers._OUTBOX_DELETES) == {table for table, _ in _OUTBOX_TABLES}


# --- log projections -------------------------------------------------------


@pytest.mark.parametrize("table", ["audit_log", "pii_detection_log"])
async def test_an_erasure_exempt_log_is_refused_rather_than_deleted(table: str) -> None:
    """Deleting an exempt class to drain the queue would violate the retention approval.

    The refusal is a distinct exception from an unresolvable locator because the
    remedy is the opposite one: the registration is the thing to remove, not the
    missing handler to write.
    """
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_LOG_PROJECTION,
        locator=handlers.carrier_locator(derivatives.KIND_LOG_PROJECTION, table, uuid.uuid4()),
    )

    with pytest.raises(handlers.ErasureExemptRecordClass) as raised:
        await handlers.LogProjectionHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert table in str(raised.value)
    assert session.executed == []


async def test_an_exempt_log_is_refused_under_every_operation() -> None:
    """A redact request does not become a licence to delete an exempt record."""
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_LOG_PROJECTION,
        locator=handlers.carrier_locator(derivatives.KIND_LOG_PROJECTION, "audit_log", uuid.uuid4()),
    )

    for operation in derivatives.OPERATIONS:
        with pytest.raises(handlers.ErasureExemptRecordClass):
            await handlers.LogProjectionHandler().apply(session, registration, operation)

    assert session.executed == []


async def test_an_unregistered_log_projection_is_refused_rather_than_reported_clean() -> None:
    """No projection is erasable today, and the handler says so instead of returning zero."""
    session = _FakeSession()
    registration = _registration(
        kind=derivatives.KIND_LOG_PROJECTION,
        locator=handlers.carrier_locator(derivatives.KIND_LOG_PROJECTION, "audit_log_by_actor", uuid.uuid4()),
    )

    with pytest.raises(handlers.UnresolvableLocator):
        await handlers.LogProjectionHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert session.executed == []


def test_the_two_exempt_log_classes_each_carry_their_reason() -> None:
    """A refusal without a reason reads as an unimplemented case."""
    assert set(handlers._ERASURE_EXEMPT_LOGS) == {"audit_log", "pii_detection_log"}
    assert all(reason.strip() for reason in handlers._ERASURE_EXEMPT_LOGS.values())


# --- exports ---------------------------------------------------------------


@pytest.mark.parametrize(
    "locator",
    [
        "export://s3/bucket/key.csv",
        "export://exports/00000000-0000-0000-0000-000000000000",
        "",
        "anything at all",
    ],
)
async def test_the_export_handler_refuses_every_locator(locator: str) -> None:
    """There is no export store, so no locator resolves and none is quietly accepted."""
    session = _FakeSession()
    registration = _registration(kind=derivatives.KIND_EXPORT, locator=locator)

    with pytest.raises(handlers.UnresolvableLocator):
        await handlers.ExportHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert session.executed == []


async def test_the_export_refusal_names_the_absent_store_rather_than_a_missing_handler() -> None:
    """An operator reading the exhausted-retry log must be able to tell the two apart."""
    session = _FakeSession()
    registration = _registration(kind=derivatives.KIND_EXPORT, locator="export://exports/report.csv")

    with pytest.raises(handlers.UnresolvableLocator) as raised:
        await handlers.ExportHandler().apply(session, registration, derivatives.OPERATION_DELETE)

    assert "no export store" in str(raised.value)


# --- what this module offers the wiring ------------------------------------


def test_the_three_handlers_cover_the_three_carrier_kinds() -> None:
    """Between them, the kinds no product domain owns, and nothing else."""
    assert {handler.kind for handler in handlers.CARRIER_HANDLERS} == {
        derivatives.KIND_OUTBOX,
        derivatives.KIND_LOG_PROJECTION,
        derivatives.KIND_EXPORT,
    }


def test_every_carrier_handler_registers_into_the_propagation_registry() -> None:
    """The registry refuses an unknown kind and a duplicate, so this proves both."""
    registry = derivatives.HandlerRegistry()

    for handler in handlers.CARRIER_HANDLERS:
        registry.register(handler)

    assert registry.kinds == (
        derivatives.KIND_OUTBOX,
        derivatives.KIND_LOG_PROJECTION,
        derivatives.KIND_EXPORT,
    )


def test_every_carrier_handler_declares_a_version() -> None:
    """The version is recorded on each registration, so an empty one is unusable."""
    assert all(handler.version.strip() for handler in handlers.CARRIER_HANDLERS)
