"""How the derivative participant asks each table "what did this actor write".

Five tables, five different answers, and the differences are not stylistic. Four
store the author's id as *text*, because the author of a checkpoint or a signal
need not be a row in `actors` at all; one stores a real `uuid` foreign key. Two of
the text tables also record what kind of author they name, and only two of those
kinds are people. One scopes by `author_tenant_id` rather than `tenant_id`,
because a claim carries two tenants.

Getting any of that wrong produces one of two failures. The loud one is a bound
parameter Postgres refuses. The quiet one is a comparison that runs fine and
matches nothing — an erasure that reports success having scheduled no work, which
is the failure this whole subsystem exists to prevent. So these tests pin the
column names against the mapped models rather than restating them, and pin the
parameter shape per class.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.context import derivatives as context_derivatives
from contextplane.retention import policies
from contextplane.types import TenantContext
from tests.helpers.context import tenant_context

_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_CALLER = uuid.UUID("33333333-3333-3333-3333-333333333333")
_TARGET = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TOMBSTONE = uuid.UUID("55555555-5555-5555-5555-555555555555")


class _AsyncCM:
    """The `async with session_factory() as session` shape, and nothing more."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Records every statement and its parameters; answers reads with nothing found.

    The source queries return no rows on purpose. What these tests are checking is
    how the questions are *asked* — which column, which parameter shape — and a
    fake that also invented rows would let a query with the wrong parameter type
    look like it worked.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))
        if "FROM source_tombstones" in sql:
            return SimpleNamespace(scalar_one=lambda: _TOMBSTONE)
        return SimpleNamespace(all=lambda: [], scalar_one=lambda: _TOMBSTONE)

    async def commit(self) -> None:
        self.commits += 1

    def source_query(self, table: str) -> tuple[str, dict[str, Any]]:
        """The one statement that read `table`, with what it bound."""
        matches = [entry for entry in self.executed if f"FROM {table} " in f"{entry[0]} "]
        assert len(matches) == 1, f"expected exactly one read of {table}, got {len(matches)}"
        return matches[0]

    def tombstone_insert(self) -> dict[str, Any]:
        return next(params for sql, params in self.executed if sql.startswith("INSERT INTO source_tombstones"))


class _Salts:
    def salt_for(self, tenant_id: uuid.UUID) -> bytes:
        return b"salt-for-" + tenant_id.bytes


def _ctx() -> TenantContext:
    return tenant_context(tenant_id=_TENANT, actor_id=_CALLER, roles=["admin"])


async def _run() -> _FakeSession:
    session = _FakeSession()
    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    participant = context_derivatives.ContextDerivativeErasure(factory, _Salts())
    await participant.erase_actor(_ctx(), _TARGET)
    return session


# --- which tables, and which of their columns ---------------------------------

#: The table each record class lives in, and the column that names its author.
#: Checked against the mapped model below rather than trusted: an earlier version
#: of the participant guessed a uniform `<role>_actor_id` naming that four of
#: these five tables never adopted, and every real erasure died on the first one.
_AUTHOR_COLUMNS = {
    policies.RECORD_TASK_CHECKPOINT: ("intent_checkpoints", "author"),
    policies.RECORD_EXTERNAL_SIGNAL: ("external_signals", "producer_id"),
    policies.RECORD_CONTEXT_FEEDBACK: ("context_feedback", "reporter_id"),
    policies.RECORD_CONTEXT_RECEIPT: ("context_receipts", "requested_by"),
    policies.RECORD_MEMORY_CLAIM: ("memory_claims", "author_actor_id"),
}


def _mapped_columns(table: str) -> frozenset[str]:
    """The columns the ORM model for `table` declares, or an empty set when none does.

    `memory_claims` has no mapped model — it is reached by raw SQL throughout — so
    it is covered by the integration tier instead of here. Returning empty rather
    than raising keeps that gap visible in one place instead of as a missing entry
    nobody notices.
    """
    from contextplane.context.models_receipt import ContextReceipt
    from contextplane.signals.models import ExternalSignal
    from contextplane.signals.models_feedback import ContextFeedback
    from contextplane.workspaces.models import IntentCheckpoint

    models = {
        "intent_checkpoints": IntentCheckpoint,
        "external_signals": ExternalSignal,
        "context_feedback": ContextFeedback,
        "context_receipts": ContextReceipt,
    }
    model = models.get(table)
    if model is None:
        return frozenset()
    return frozenset(model.__table__.columns.keys())


def test_every_class_the_erasure_walks_has_a_query() -> None:
    """The walk and the query map are one list, in both directions.

    A class in the walk with no query raises part-way through an erasure that has
    already deleted rows; a query for a class the walk never visits is dead code
    that reads as coverage.
    """
    assert set(context_derivatives.ACTOR_RECORD_CLASSES) == set(context_derivatives._ACTOR_SOURCES)


@pytest.mark.parametrize("record_class", list(_AUTHOR_COLUMNS))
def test_the_author_column_is_one_the_table_actually_has(record_class: str) -> None:
    """Against the mapped model, not against a second copy of the same guess."""
    table, column = _AUTHOR_COLUMNS[record_class]
    declared = _mapped_columns(table)
    if not declared:
        pytest.skip(f"{table} has no mapped model; the integration tier covers it")
    assert column in declared, f"{table} has no column {column!r}"
    assert column in context_derivatives._ACTOR_SOURCES[record_class].sql


def test_the_claim_query_scopes_by_the_authoring_tenant() -> None:
    """A claim names two tenants and only one of them is the person's.

    `owning_tenant_id` is the tenant of the claim's *subject*. Scoping there would
    miss everything this actor asserted about another tenant's subject, and sweep
    in what other people asserted about this one's.
    """
    sql = context_derivatives._ACTOR_SOURCES[policies.RECORD_MEMORY_CLAIM].sql
    assert "author_tenant_id = :tenant" in sql
    assert "owning_tenant_id" not in sql


# --- how the actor is bound, per class ----------------------------------------


@pytest.mark.asyncio
async def test_the_text_keyed_tables_are_asked_with_the_text_form() -> None:
    """asyncpg does not coerce a `UUID` into a text comparison; it refuses it."""
    session = await _run()
    for table in ("intent_checkpoints", "external_signals", "context_feedback", "context_receipts"):
        _, params = session.source_query(table)
        assert params["actor"] == str(_TARGET), f"{table} was asked with a non-text actor"
        assert isinstance(params["actor"], str)


@pytest.mark.asyncio
async def test_the_uuid_keyed_table_is_asked_with_the_uuid() -> None:
    """And the reverse: the text form against a `uuid` column is an error, not a miss."""
    _, params = (await _run()).source_query("memory_claims")
    assert params["actor"] == _TARGET
    assert isinstance(params["actor"], uuid.UUID)


@pytest.mark.asyncio
async def test_only_the_tables_that_record_an_author_kind_filter_on_it() -> None:
    """Signals and feedback name a producer that may be another system entirely.

    Matching on the id alone would delete a vendor's whole feed the first time one
    of its identifiers collided with an actor's. The other three tables have no such
    column, so binding the list there would be a parameter nothing reads.
    """
    session = await _run()
    for table in ("external_signals", "context_feedback"):
        _, params = session.source_query(table)
        assert params["origin_types"] == list(policies.ACTOR_ORIGIN_TYPES)
    for table in ("intent_checkpoints", "context_receipts", "memory_claims"):
        _, params = session.source_query(table)
        assert "origin_types" not in params


def test_an_external_producer_is_not_an_actor_of_this_system() -> None:
    """The vocabulary itself, pinned: adding `external` here would widen every
    erasure to another system's records."""
    assert "external" not in policies.ACTOR_ORIGIN_TYPES
    assert set(policies.ACTOR_ORIGIN_TYPES) == {"human", "agent"}


def test_the_two_subsystems_that_erase_by_author_agree_on_who_is_a_person() -> None:
    """One definition, read from two layers that cannot import each other.

    Signals sits above context in the import contract, so neither can take the
    tuple from the other; both take it from the policy module below them. This is
    the check that keeps that true — two copies erasing two different sets of rows
    is the failure it prevents.
    """
    from contextplane.signals import erasure as signal_erasure

    assert signal_erasure.ACTOR_ORIGIN_TYPES == policies.ACTOR_ORIGIN_TYPES


# --- the tombstone ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_authority_is_recorded_as_text() -> None:
    """`request_authority` is a TEXT column: who asked need not be an actor row.

    Bound as a `UUID` it is not coerced — the insert fails, and it fails after the
    participants ahead of this one have already deleted their rows.
    """
    params = (await _run()).tombstone_insert()
    assert params["authority"] == str(_CALLER)
    assert isinstance(params["authority"], str)


@pytest.mark.asyncio
async def test_the_tombstone_names_the_policy_version_it_was_decided_under() -> None:
    """The foreign key into `retention_policies` is on (version, class), so a
    tombstone that names neither has nothing to point at."""
    params = (await _run()).tombstone_insert()
    assert params["policy"] == policies.POLICY_VERSION
    assert params["cls"] == policies.RECORD_DERIVATIVE
    assert params["subject"] == _TARGET


@pytest.mark.asyncio
async def test_the_tombstone_and_its_work_are_one_commit() -> None:
    """An enqueue without a tombstone is work nobody authorised; a tombstone
    without an enqueue is an authorisation nobody acted on."""
    session = await _run()
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_class_the_actor_authored_nothing_in_reports_zero() -> None:
    """Zero is a real answer. Omitting the class would make "nothing to do"
    indistinguishable from "never asked"."""
    session = _FakeSession()
    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    participant = context_derivatives.ContextDerivativeErasure(factory, _Salts())

    scheduled = await participant.erase_actor(_ctx(), _TARGET)

    assert scheduled == dict.fromkeys(context_derivatives.ACTOR_RECORD_CLASSES, 0)


def test_the_participant_reports_the_subsystem_name_erasure_coverage_pins() -> None:
    """The registry's coverage list is keyed on this string."""
    assert context_derivatives.ContextDerivativeErasure.subsystem == "context_derivatives"


def test_the_erasure_reads_the_source_classes_in_a_declared_order() -> None:
    """Records whose derivatives hold verbatim text come first, so a failure
    part-way through has already scheduled the artefacts that matter most."""
    assert context_derivatives.ACTOR_RECORD_CLASSES[0] == policies.RECORD_TASK_CHECKPOINT
    assert context_derivatives.ACTOR_RECORD_CLASSES[1] == policies.RECORD_EXTERNAL_SIGNAL


def test_the_policy_covers_every_class_the_erasure_will_tombstone() -> None:
    """A class with no disposition has no verifier sentence, so the tombstone it
    writes cannot be disclosed at all."""
    for record_class in (*context_derivatives.ACTOR_RECORD_CLASSES, policies.RECORD_DERIVATIVE):
        assert policies.disposition(record_class).verifier_disclosure


@pytest.mark.asyncio
async def test_one_erasure_stamps_one_instant_everywhere() -> None:
    """The tombstone and every work item it authorises carry the same time.

    Read per statement instead, the same erasure would be recorded at several
    times, and "was this scheduled by that tombstone" stops being answerable by
    comparing them.
    """
    session = await _run()
    stamps = {params["now"] for _, params in session.executed if "now" in params}
    assert len(stamps) == 1
    assert stamps.pop().tzinfo is datetime.UTC
