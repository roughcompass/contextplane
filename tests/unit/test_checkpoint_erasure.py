"""Erasing task memory: what the checkpoint participant writes, and what the summary handler refuses.

The statements themselves are proved against a real database in
`tests/integration/test_checkpoint_erasure.py` -- the immutability trigger admits
exactly one UPDATE shape, and only Postgres can say whether this module produces
it. What is proved here is the part a database cannot check: the decisions this
code makes before it issues anything.

Two of those decisions are refusals, and each is asserted twice -- that it was
refused, *and* that nothing was written. A refusal that rolls back after writing
is not the same promise as one that never writes, and only the second is safe to
retry.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import TenantContext
from contextplane.workspaces import derivative_handlers, queries_checkpoint

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACTOR = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OPERATOR = uuid.UUID("33333333-3333-3333-3333-333333333333")
_TASK = uuid.UUID("44444444-4444-4444-4444-444444444444")

#: Key material a test can configure, so the keyed path is exercised as well as
#: the refusal a deployment with no key gets.
_KEY_ID = "k1"
_KEY_HEX = "00112233445566778899aabbccddeeff"


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_OPERATOR, roles=["operator"])


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


@dataclasses.dataclass
class _Statement:
    """One statement the code under test issued, normalized for matching."""

    sql: str
    params: dict[str, Any]


class _Result:
    """The two shapes the code under test reads a result through."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, rowcount: int = 0, scalar: Any = None) -> None:
        self._rows = rows or []
        self.rowcount = rowcount
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> tuple[Any, ...] | None:
        return None if not self._rows else tuple(self._rows[0].values())

    def scalar_one(self) -> Any:
        return self._scalar


def _session(*, rows: list[dict[str, Any]] | None = None, rowcount: int = 1) -> AsyncMock:
    """A session that records every statement and answers with fixed shapes.

    Deliberately not a state machine. What these tests assert is which statements
    are issued and what they bind; whether Postgres accepts them is the integration
    tier's question, and a fake that answered it would only agree with itself.
    """
    issued: list[_Statement] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(stmt).split())
        issued.append(_Statement(sql=sql, params=dict(params or {})))
        if "FROM intent_checkpoints" in sql:
            return _Result(rows=rows or [])
        if "INSERT INTO derivative_registrations" in sql:
            return _Result(scalar=uuid.uuid4())
        return _Result(rowcount=rowcount)

    session = AsyncMock()
    session.execute = _execute
    session.issued = issued
    return session


def _factory(session: AsyncMock) -> Any:
    """A session factory yielding one session, in the shape the participant opens.

    A plain callable returning an async context manager, because that is what
    `async_sessionmaker` is: calling it is synchronous, and only entering the
    result awaits.
    """

    @contextlib.asynccontextmanager
    async def _open() -> AsyncIterator[AsyncMock]:
        yield session

    return _open


def _checkpoint_row(digest: str = "sha256:aaaa") -> dict[str, Any]:
    return {"checkpoint_id": uuid.uuid4(), "digest": digest}


# --- the summary handler ------------------------------------------------------


def test_the_summary_handler_owns_the_kind_the_schema_stores() -> None:
    """A handler registered under a kind the schema does not store would be a
    handler nothing ever dispatches to, and the coverage gate would read it as
    covering something."""
    handler = derivative_handlers.SummaryDerivativeHandler()
    assert handler.kind == derivatives.KIND_SUMMARY
    assert handler.kind in derivatives.DERIVATIVE_KINDS
    assert handler.version, "a derivative built by this handler could not be identified later"

    registry = derivatives.HandlerRegistry()
    registry.register(handler)
    assert registry.handler_for(derivatives.KIND_SUMMARY) is handler


@pytest.mark.parametrize("operation", [derivatives.OPERATION_DELETE, derivatives.OPERATION_REDACT])
@pytest.mark.asyncio
async def test_deleting_and_redacting_a_summary_both_replace_its_prose(operation: str) -> None:
    """`delete` cannot mean the row. It carries the head pointer and sequence the
    chain is read through, so deleting it would destroy structure the erasure has
    no mandate over -- and leave a task whose head is gone rather than redacted."""
    session = _session(rowcount=1)
    touched = await derivative_handlers.SummaryDerivativeHandler().apply(session, _registration(), operation)

    assert touched == 1
    (statement,) = session.issued
    assert statement.sql.startswith("UPDATE intent_heads SET summary")
    assert statement.params["erased"] == derivative_handlers.ERASED_SUMMARY
    assert statement.params["tenant"] == _TENANT
    assert statement.params["task"] == _TASK
    # The row survives the redaction: only the prose column is written.
    assert "head_checkpoint_id" not in statement.sql and "head_sequence" not in statement.sql


@pytest.mark.asyncio
async def test_a_summary_already_redacted_is_reported_as_nothing_left_to_do() -> None:
    """Zero touched is a successful answer, not a failure. A retry after a partly
    applied propagation is the normal recovery path, and the statement's own
    predicate is what makes the second run a no-op."""
    session = _session(rowcount=0)
    touched = await derivative_handlers.SummaryDerivativeHandler().apply(
        session, _registration(), derivatives.OPERATION_DELETE
    )

    assert touched == 0
    (statement,) = session.issued
    assert "summary <> :erased" in statement.sql, "a second redaction would rewrite the same marker every run"


@pytest.mark.asyncio
async def test_rebuilding_a_summary_is_refused_and_writes_nothing() -> None:
    """A head summary is prose somebody wrote, not a projection of the chain, so
    there is nothing to recompute it from. Blanking it and calling that a rebuild
    would be a deletion reported as a refresh."""
    session = _session()
    with pytest.raises(derivative_handlers.SummaryCannotBeRebuilt, match="cannot be rebuilt"):
        await derivative_handlers.SummaryDerivativeHandler().apply(
            session, _registration(), derivatives.OPERATION_REBUILD
        )

    assert session.issued == [], "the refusal ran after touching the artefact"


@pytest.mark.asyncio
async def test_a_locator_this_handler_cannot_address_is_refused_and_writes_nothing() -> None:
    """Loud rather than "nothing to do": an unrecognised locator names an artefact
    somebody registered and nobody can reach, and reporting zero touched would
    mark the propagation done while the content stays where it is."""
    session = _session()
    for locator in (
        str(_TASK),
        f"pgvector://chunks/{_TASK}",
        "intent_heads://not-a-uuid/summary",
        f"intent_heads://{_TASK}",
    ):
        with pytest.raises(derivative_handlers.UnknownDerivativeLocator, match="task head summary"):
            await derivative_handlers.SummaryDerivativeHandler().apply(
                session, _registration(locator=locator), derivatives.OPERATION_DELETE
            )

    assert session.issued == []


def test_a_summary_locator_and_audience_name_the_task_they_belong_to() -> None:
    """The locator is the derivative's identity, so two tasks may never produce
    one; the audience is the task's participants, so a summary built for one task
    can never be served as another's."""
    assert derivative_handlers.summary_locator(_TASK) != derivative_handlers.summary_locator(uuid.uuid4())
    assert str(_TASK) in derivative_handlers.summary_locator(_TASK)
    assert derivative_handlers.summary_audience(_TASK) == f"task:{_TASK}"


# --- the checkpoint participant -----------------------------------------------


@pytest.mark.asyncio
async def test_erasing_an_actor_minimizes_each_checkpoint_and_tombstones_it() -> None:
    """One tombstone per erased checkpoint, holding no part of the body -- which is
    what lets a verifier say the record existed and was erased without becoming an
    oracle for what it said."""
    rows = [_checkpoint_row("sha256:aaaa"), _checkpoint_row("sha256:bbbb")]
    session = _session(rows=rows, rowcount=1)
    participant = derivative_handlers.CheckpointErasure(_factory(session), _salts())

    counts = await participant.erase_actor(_ctx(), _ACTOR)

    assert counts == {"checkpoints": 2, "tombstones": 2}
    updates = [s for s in session.issued if s.sql.startswith("UPDATE intent_checkpoints")]
    tombstones_written = [s for s in session.issued if "INSERT INTO source_tombstones" in s.sql]
    assert len(updates) == 2 and len(tombstones_written) == 2

    for statement in tombstones_written:
        assert statement.params["cls"] == policies.RECORD_TASK_CHECKPOINT
        assert statement.params["policy"] == policies.POLICY_VERSION
        # Accountability, not content: who asked and why, never what was erased.
        assert statement.params["authority"] == str(_OPERATOR)
        assert statement.params["reason"] == derivatives.TRIGGER_ERASURE
    proofs = {statement.params["proof"] for statement in tombstones_written}
    assert len(proofs) == 2, "two checkpoints with different content produced one proof"
    assert not any("sha256:" in str(statement.params["proof"]) for statement in tombstones_written)


@pytest.mark.asyncio
async def test_minimization_writes_the_body_and_leaves_every_immutable_column_alone() -> None:
    """The list the database enforces and the list a post-erasure verifier reads
    are the same list. An UPDATE that moved any of it would be a rewrite wearing
    an erasure's clothes, and the trigger would refuse the whole erasure."""
    session = _session(rows=[_checkpoint_row()], rowcount=1)
    await derivative_handlers.CheckpointErasure(_factory(session), _salts()).erase_actor(_ctx(), _ACTOR)

    (update,) = [s for s in session.issued if s.sql.startswith("UPDATE intent_checkpoints")]
    assert update.params["erased"] == derivative_handlers.ERASED_CHECKPOINT_GOAL
    for cleared in ("decisions", "assumptions", "evidence", "completed_checks", "open_questions"):
        assert f"{cleared} = '[]'::jsonb" in update.sql
    assert "next_action = NULL" in update.sql
    for immutable in ("digest", "sequence", "predecessor_id", "recorded_at", "author", "retention_policy"):
        assert f"{immutable} =" not in update.sql.split("WHERE")[0], f"minimization writes {immutable}"


@pytest.mark.asyncio
async def test_a_checkpoint_already_minimized_is_not_erased_a_second_time() -> None:
    """Idempotence lives in the selection, so a retried erasure is free rather than
    a second pass that rewrites the same marker and mints a second proof under a
    later instant."""
    session = _session(rows=[], rowcount=0)
    counts = await derivative_handlers.CheckpointErasure(_factory(session), _salts()).erase_actor(_ctx(), _ACTOR)

    assert counts == {"checkpoints": 0, "tombstones": 0}
    (selection,) = [s for s in session.issued if s.sql.startswith("SELECT")]
    assert selection.params["erased"] == derivative_handlers.ERASED_CHECKPOINT_GOAL
    assert "goal <> :erased" in selection.sql
    assert selection.params["actor"] == str(_ACTOR), "the author column stores the actor as text"


@pytest.mark.asyncio
async def test_the_selection_is_scoped_to_the_tenant_asking() -> None:
    """A checkpoint id is a UUID a caller can hold. Without the tenant predicate,
    erasing an actor in one tenant would reach rows in another."""
    session = _session(rows=[_checkpoint_row()], rowcount=1)
    await derivative_handlers.CheckpointErasure(_factory(session), _salts()).erase_actor(_ctx(), _ACTOR)

    for statement in session.issued:
        assert statement.params.get("tenant") == _TENANT, f"unscoped statement: {statement.sql}"


@pytest.mark.asyncio
async def test_with_no_key_material_the_erasure_refuses_before_it_writes_anything() -> None:
    """Refused *and* nothing written. A minimization committed without the
    tombstone accounting for it is indistinguishable from data loss, and an
    unkeyed proof would be a tombstone that verifies against nothing while looking
    exactly like one that does."""
    session = _session(rows=[_checkpoint_row()], rowcount=1)
    unkeyed = tombstones.KeyedTenantSalt({}, active_key_id=None)

    with pytest.raises(tombstones.TenantSaltUnavailable, match="no active retention key"):
        await derivative_handlers.CheckpointErasure(_factory(session), unkeyed).erase_actor(_ctx(), _ACTOR)

    assert session.issued == [], "the erasure read or wrote before it knew it could prove itself"


@pytest.mark.asyncio
async def test_the_participant_does_not_schedule_derivative_work_of_its_own() -> None:
    """The context subsystem's participant already walks checkpoint sources and
    enqueues one item per derivative under its own tombstone. A second enqueue
    under a second tombstone is a second cause, so the outbox would accept it and
    every summary redaction would be scheduled twice."""
    session = _session(rows=[_checkpoint_row()], rowcount=1)
    await derivative_handlers.CheckpointErasure(_factory(session), _salts()).erase_actor(_ctx(), _ACTOR)

    assert not [s for s in session.issued if "derivative_work_outbox" in s.sql]


# --- the registrar ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_head_summary_is_registered_against_the_checkpoint_it_describes() -> None:
    """The source link is what makes the summary reachable from an erasure of the
    checkpoint whose words are in it. Registering the artefact without one is the
    same as not registering it: nothing joins to it."""
    session = _session()
    head = uuid.uuid4()

    await queries_checkpoint.register_summary_derivative(
        session, tenant_id=_TENANT, intent_id=_TASK, head_checkpoint_id=head
    )

    (registration,) = [s for s in session.issued if "INSERT INTO derivative_registrations" in s.sql]
    (link,) = [s for s in session.issued if "INSERT INTO derivative_source_links" in s.sql]
    assert registration.params["kind"] == derivatives.KIND_SUMMARY
    assert registration.params["locator"] == derivative_handlers.summary_locator(_TASK)
    assert registration.params["audience"] == derivative_handlers.summary_audience(_TASK)
    assert registration.params["handler_version"] == derivative_handlers.SUMMARY_HANDLER_VERSION
    assert link.params["cls"] == policies.RECORD_TASK_CHECKPOINT
    assert link.params["sid"] == head


@pytest.mark.asyncio
async def test_an_event_bounded_summary_is_registered_with_no_reachable_clock() -> None:
    """A checkpoint is bounded by tenant deletion rather than by a duration, so it
    contributes no expiry. The column is NOT NULL, so the event bound is written
    as an instant no sweep reaches -- a plausible-looking date would read as "this
    expires then" and would be wrong."""
    session = _session()
    await queries_checkpoint.register_summary_derivative(
        session, tenant_id=_TENANT, intent_id=_TASK, head_checkpoint_id=uuid.uuid4()
    )

    (registration,) = [s for s in session.issued if "INSERT INTO derivative_registrations" in s.sql]
    assert registration.params["expires_at"] == derivative_handlers.EVENT_BOUNDED_HORIZON
    assert registration.params["expires_at"].year == 9999
    # Only ever earlier: the moment this summary reads a source with a real
    # duration, the registry's own LEAST takes that duration instead.
    assert "LEAST" in registration.sql


def test_the_erased_markers_are_recognisable_and_carry_nothing() -> None:
    """Recognisable is what makes a second pass a no-op; carrying nothing is what
    makes it an erasure. Both markers share the prefix every minimized value in
    this system carries, so one predicate identifies all of them."""
    for marker in (derivative_handlers.ERASED_CHECKPOINT_GOAL, derivative_handlers.ERASED_SUMMARY):
        assert tombstones.is_erased_key(marker)
        assert marker.strip(), "a blank marker would be refused by the column's own NOT NULL shape"
    assert derivative_handlers.ERASED_CHECKPOINT_GOAL != derivative_handlers.ERASED_SUMMARY


def _registration(locator: str | None = None) -> derivatives.Registration:
    return derivatives.Registration(
        derivative_id=uuid.uuid4(),
        tenant_id=_TENANT,
        derivative_kind=derivatives.KIND_SUMMARY,
        storage_locator=derivative_handlers.summary_locator(_TASK) if locator is None else locator,
        audience_partition=derivative_handlers.summary_audience(_TASK),
        classification=derivative_handlers.SUMMARY_CLASSIFICATION,
        expires_at=_NOW,
        blocking=False,
    )
