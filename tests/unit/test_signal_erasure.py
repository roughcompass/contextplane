"""Signal and feedback erasure: what each clock touches, and what it refuses to.

The statements are asserted here rather than only their effects, for the reason the
module exists: the binding table has no foreign key to its subject, so a delete that
named the wrong subject type would remove nothing and report success. An effects-only
test against a fake store cannot tell those apart — it would agree with the bug.

Refusals are asserted twice each, as the contract requires: that the call refused, and
that nothing was written. A refusal that raises after a partial write is worse than no
refusal, because the caller believes nothing happened.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.retention import derivatives, holds, policies, tombstones
from contextplane.signals import erasure
from contextplane.types import TenantContext

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACTOR = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SIGNAL = uuid.UUID("33333333-3333-3333-3333-333333333333")
_TOMBSTONE = uuid.UUID("44444444-4444-4444-4444-444444444444")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

_KEY_ID = "k1"


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: b"\x01" * 32}, active_key_id=_KEY_ID)


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=["admin"], oidc_subject="ops")


class _FrozenClock:
    def now(self) -> datetime.datetime:
        return _NOW


class _AsyncCM:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Answers each statement by its leading verb and records everything sent.

    `rowcounts` lets a test say what a given DML statement affected, keyed by a
    substring of the statement, so the refusal paths (which turn on an UPDATE matching
    no rows) are reachable without a database.
    """

    def __init__(
        self,
        *,
        signal_ids: list[uuid.UUID] | None = None,
        rowcounts: dict[str, int] | None = None,
    ) -> None:
        self._signal_ids = signal_ids if signal_ids is not None else []
        self._rowcounts = rowcounts or {}
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))

        if sql.startswith("SELECT tombstone_id"):
            return MagicMock(scalar_one=MagicMock(return_value=_TOMBSTONE))
        if sql.startswith("SELECT signal_id") or sql.startswith("SELECT feedback_id"):
            return MagicMock(all=MagicMock(return_value=[(sid,) for sid in self._signal_ids]))
        if sql.startswith("SELECT"):
            return MagicMock(all=MagicMock(return_value=[]))

        affected = next((count for fragment, count in self._rowcounts.items() if fragment in sql), 1)
        return MagicMock(rowcount=affected)

    async def commit(self) -> None:
        self.commits += 1

    def statements(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return [(sql, params) for sql, params in self.executed if sql.startswith(prefix)]

    def wrote_anything(self) -> bool:
        """Whether any statement that changes data was sent.

        The check every refusal test needs. A SELECT is not a write, and the revoke
        UPDATE that matched zero rows is not one either — it is the probe that
        discovered the refusal, and it changed nothing by definition.
        """
        return any(
            sql.startswith(("INSERT", "DELETE")) or (sql.startswith("UPDATE") and "SET revoked_at" not in sql)
            for sql, _ in self.executed
        )


def _factory(session: _FakeSession) -> MagicMock:
    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    return factory


# --- Which signals belong to an actor -------------------------------------------


@pytest.mark.asyncio
async def test_only_human_and_agent_producers_are_treated_as_actors() -> None:
    """An `external` producer is a system, not a person. Erasing an actor must not
    delete a vendor's whole feed because one producer id happened to look like theirs,
    so the predicate is the id *and* the type."""
    session = _FakeSession(signal_ids=[])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    sql, params = session.statements("SELECT signal_id")[0]
    assert "producer_type = ANY(:origin_types)" in sql
    assert params["origin_types"] == ["human", "agent"]
    assert "external" not in params["origin_types"]
    # The actor is matched as text, because the column is the producer's own id space.
    assert params["actor"] == str(_ACTOR)
    assert params["tenant"] == _TENANT


@pytest.mark.asyncio
async def test_an_actor_who_produced_no_signals_reports_zero_rather_than_skipping() -> None:
    """Zero is a real answer and distinguishing it from "never asked" is what makes the
    participant's report readable after the fact."""
    session = _FakeSession(signal_ids=[])
    counts = await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    assert counts["signals"] == 0
    assert session.statements("DELETE FROM external_signals") == []
    # Feedback minimization still runs: an actor with no signals may still have written
    # feedback, and skipping it because the signal set was empty would leave the note.
    assert session.statements("UPDATE context_feedback")


# --- The binding delete, which nothing cascades ---------------------------------


@pytest.mark.asyncio
async def test_reference_bindings_are_deleted_before_the_signals_that_own_them() -> None:
    """Order matters and nothing enforces it. `context_reference_bindings` has no
    foreign key to its polymorphic subject, so deleting the signal first leaves a row
    that a reverse lookup resurrects — the reference then reads as still-cited by an
    id that no longer exists."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    order = [sql for sql, _ in session.executed if sql.startswith("DELETE")]
    assert len(order) == 2
    assert "context_reference_bindings" in order[0]
    assert "external_signals" in order[1]

    _, params = session.statements("DELETE FROM context_reference_bindings")[0]
    assert params["subject_type"] == erasure.SUBJECT_TYPE_EXTERNAL_SIGNAL
    assert params["ids"] == [_SIGNAL]
    assert params["tenant"] == _TENANT


def test_the_subject_type_is_the_one_the_writer_uses() -> None:
    """Not a second literal. The binding table is polymorphic, so a spelling that
    drifted by one character would delete nothing and report success — the erasure
    would pass its own tests while leaving every binding in place."""
    from contextplane.signals import ingest

    assert erasure.SUBJECT_TYPE_EXTERNAL_SIGNAL is ingest.SUBJECT_EXTERNAL_SIGNAL


@pytest.mark.asyncio
async def test_the_whole_erasure_is_one_transaction() -> None:
    """The tombstone authorises the work, the enqueue schedules it, the bindings and
    rows go. A partial commit that deleted signals but left the propagation unscheduled
    would leave their vectors searchable with no record of what was owed."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    assert session.commits == 1


# --- Scheduling propagation before the source row goes --------------------------


@pytest.mark.asyncio
async def test_propagation_is_enqueued_while_the_source_row_still_exists() -> None:
    """The enqueue reads the derivative links, which are keyed by the source id. Moving
    it after the delete would schedule nothing and the erasure would report success."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    order = [sql for sql, _ in session.executed]
    enqueue_at = next(i for i, sql in enumerate(order) if sql.startswith("INSERT INTO derivative_work_outbox"))
    delete_at = next(i for i, sql in enumerate(order) if sql.startswith("DELETE FROM external_signals"))
    assert enqueue_at < delete_at

    # And the tombstone that authorises it comes before the enqueue, because the outbox
    # row names it: enqueueing first would have nothing to point at.
    tombstone_at = next(i for i, sql in enumerate(order) if sql.startswith("INSERT INTO source_tombstones"))
    assert tombstone_at < enqueue_at


@pytest.mark.asyncio
async def test_each_erased_signal_gets_its_own_tombstone() -> None:
    """One per signal, so a dependent can be invalidated by cause. A single actor-wide
    marker would say only that somebody in the set was erased, which no dependent can
    act on."""
    second = uuid.uuid4()
    session = _FakeSession(signal_ids=[_SIGNAL, second])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    tombstoned = session.statements("INSERT INTO source_tombstones")
    assert len(tombstoned) == 2
    assert {params["subject"] for _, params in tombstoned} == {_SIGNAL, second}
    for _, params in tombstoned:
        assert params["cls"] == policies.RECORD_EXTERNAL_SIGNAL
        assert params["policy"] == policies.POLICY_VERSION
        assert params["reason"] == derivatives.TRIGGER_ERASURE


@pytest.mark.asyncio
async def test_a_tombstone_conflict_is_re_read_rather_than_re_generated() -> None:
    """A retry must land on the tombstone the first attempt wrote: the outbox is unique
    per tombstone, so a second id would let the same work be enqueued twice under two
    authorisations."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    inserted, _ = session.statements("INSERT INTO source_tombstones")[0]
    assert "ON CONFLICT (tenant_id, record_class, subject_id) DO NOTHING" in inserted
    assert session.statements("SELECT tombstone_id")


# --- Feedback is minimized, never deleted --------------------------------------


@pytest.mark.asyncio
async def test_feedback_notes_are_cleared_and_the_rows_survive() -> None:
    """The note is what somebody wrote; the discriminant, rating and receipt linkage are
    what every aggregate counts. Deleting rows would change those answers retroactively
    while looking like data that was never there."""
    session = _FakeSession(signal_ids=[])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    sql, params = session.statements("UPDATE context_feedback")[0]
    assert "SET note = NULL" in sql
    # Feedback records who reported by the reporter's own id, as text, with a type
    # saying what kind of reporter that was. There is no actor_id column on this
    # table, and matching one would erase nothing while reporting success.
    assert "reporter_id = :actor" in sql
    assert "reporter_type = ANY(:origin_types)" in sql
    assert params["actor"] == str(_ACTOR)
    assert params["origin_types"] == ["human", "agent"]
    # Nothing else is touched, and no feedback row is removed anywhere in the module.
    assert "rating" not in sql and "kind" not in sql
    assert not [s for s, _ in session.executed if s.startswith("DELETE FROM context_feedback")]


@pytest.mark.asyncio
async def test_already_minimized_feedback_is_not_rewritten() -> None:
    """`note IS NOT NULL` keeps a repeated erasure from reporting work it did not do,
    which is what makes the count meaningful on a retry."""
    session = _FakeSession(signal_ids=[])
    await erasure.SignalErasure(_factory(session), _salts(), clock=_FrozenClock()).erase_actor(_ctx(), _ACTOR)

    sql, _ = session.statements("UPDATE context_feedback")[0]
    assert "note IS NOT NULL" in sql


# --- Revocation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_stamps_the_signal_and_schedules_its_derivatives_together() -> None:
    """One transaction because the two halves are one fact: a stamp without an enqueue
    is a signal marked withdrawn whose summaries still answer queries."""
    session = _FakeSession()
    await erasure.revoke_signal(_factory(session), _salts(), ctx=_ctx(), signal_id=_SIGNAL, now=_NOW)

    stamp, params = session.statements("UPDATE external_signals")[0]
    assert "SET revoked_at = :now" in stamp
    # Only an unrevoked signal is stamped, so a repeat cannot move the instant.
    assert "revoked_at IS NULL" in stamp
    assert params["id"] == _SIGNAL

    _, tombstone = session.statements("INSERT INTO source_tombstones")[0]
    assert tombstone["reason"] == derivatives.TRIGGER_REVOCATION
    assert session.commits == 1


@pytest.mark.asyncio
async def test_revoking_a_foreign_or_already_revoked_signal_refuses_and_writes_nothing() -> None:
    """Both refusals, asserted twice each. Not distinguished in the message on purpose:
    telling a caller which of the two it was would confirm the existence of another
    tenant's row."""
    session = _FakeSession(rowcounts={"SET revoked_at": 0})

    with pytest.raises(erasure.SignalErasureRefused) as refused:
        await erasure.revoke_signal(_factory(session), _salts(), ctx=_ctx(), signal_id=_SIGNAL, now=_NOW)

    assert "already revoked" in str(refused.value)
    # Nothing written, and nothing committed: no tombstone, no propagation item.
    assert session.wrote_anything() is False
    assert session.statements("INSERT INTO source_tombstones") == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_revoking_without_key_material_refuses_and_writes_nothing() -> None:
    """The tombstone's proof is a keyed HMAC, so an unkeyed deployment cannot record a
    revocation it can later prove. Refusing beats stamping the row and leaving an
    unprovable marker behind."""
    session = _FakeSession()
    unkeyed = tombstones.KeyedTenantSalt({}, active_key_id=None)

    with pytest.raises(tombstones.TenantSaltUnavailable):
        await erasure.revoke_signal(_factory(session), unkeyed, ctx=_ctx(), signal_id=_SIGNAL, now=_NOW)

    assert session.statements("INSERT INTO source_tombstones") == []
    assert session.commits == 0


# --- Expiry batches ------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_expiry_clears_the_observation_and_keeps_the_envelope() -> None:
    """The payload holds a person's words; the envelope is what makes every derived
    claim auditable. Two clocks, and this is the earlier one."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    expiry = erasure.SignalExpiry(_factory(session), holds.NoHoldStorage())

    assert await expiry.minimize_signal_payloads(_ctx(), now=_NOW) == 1

    sql, params = session.statements("UPDATE external_signals")[0]
    # A marker, not NULL: the schema requires exactly one of payload/evidence_handle to
    # be present on every row, so clearing both would need a migration. The marker holds
    # no observation and no producer text.
    assert "SET payload = CAST(:marker AS jsonb), evidence_handle = NULL" in sql
    assert json.loads(str(params["marker"])) == erasure.MINIMIZED_PAYLOAD
    assert erasure.MINIMIZED_PAYLOAD == {"minimized": True, "policy_version": policies.POLICY_VERSION}
    assert not [s for s, _ in session.executed if s.startswith("DELETE FROM external_signals")]


@pytest.mark.asyncio
async def test_envelope_expiry_deletes_bindings_with_the_rows() -> None:
    """The later clock. The bindings go in the same transaction for the same reason they
    do on erasure: nothing cascades, and a surviving binding resurrects the id."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    expiry = erasure.SignalExpiry(_factory(session), holds.NoHoldStorage())

    assert await expiry.delete_expired_signals(_ctx(), now=_NOW) == 1

    deletes = [sql for sql, _ in session.executed if sql.startswith("DELETE")]
    assert "context_reference_bindings" in deletes[0]
    assert "external_signals" in deletes[1]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_held_record_is_not_expired() -> None:
    """A legal hold suspends the clock. The batch consults the seam rather than each
    sweep implementing the consult, so a path that forgot to ask cannot exist."""

    class _AllHeld:
        async def active_holds(
            self, tenant_id: uuid.UUID, record_class: str, subject_ids: Any, *, now: Any
        ) -> dict[uuid.UUID, Any]:
            return {sid: MagicMock() for sid in subject_ids}

        async def held_overdue(self, tenant_id: uuid.UUID, *, now: Any) -> tuple[Any, ...]:
            return ()

    session = _FakeSession(signal_ids=[_SIGNAL])
    expiry = erasure.SignalExpiry(_factory(session), _AllHeld())  # type: ignore[arg-type]

    assert await expiry.delete_expired_signals(_ctx(), now=_NOW) == 0
    assert not [s for s, _ in session.executed if s.startswith("DELETE")]
    assert session.commits == 0


@pytest.mark.asyncio
async def test_feedback_expiry_minimizes_and_never_deletes() -> None:
    """The same rule as actor erasure, on the payload clock instead of a request."""
    session = _FakeSession(signal_ids=[uuid.uuid4()])
    expiry = erasure.SignalExpiry(_factory(session), holds.NoHoldStorage())

    assert await expiry.minimize_feedback_notes(_ctx(), now=_NOW) == 1

    sql, _ = session.statements("UPDATE context_feedback")[0]
    assert "SET note = NULL" in sql
    assert not [s for s, _ in session.executed if s.startswith("DELETE FROM context_feedback")]


@pytest.mark.asyncio
async def test_an_empty_due_batch_writes_nothing() -> None:
    """A quiet sweep must not open a transaction and commit an empty statement, which
    would make an idle tick indistinguishable from a working one in the logs."""
    session = _FakeSession(signal_ids=[])
    expiry = erasure.SignalExpiry(_factory(session), holds.NoHoldStorage())

    assert await expiry.minimize_signal_payloads(_ctx(), now=_NOW) == 0
    assert await expiry.minimize_feedback_notes(_ctx(), now=_NOW) == 0
    assert await expiry.delete_expired_signals(_ctx(), now=_NOW) == 0
    assert session.commits == 0


@pytest.mark.asyncio
async def test_the_batch_is_bounded_and_ordered_oldest_first() -> None:
    """Bounded so a tick cannot hold a transaction open across the table; oldest first
    so the longest-overdue rows are the ones that go."""
    session = _FakeSession(signal_ids=[_SIGNAL])
    expiry = erasure.SignalExpiry(_factory(session), holds.NoHoldStorage(), batch_size=7)

    await expiry.minimize_signal_payloads(_ctx(), now=_NOW)

    sql, params = session.statements("SELECT signal_id")[0]
    assert "ORDER BY ingested_at" in sql and "LIMIT :limit" in sql
    assert params["limit"] == 7


def test_the_expiry_deadline_comes_from_the_approved_policy() -> None:
    """Not a number in this module. The policy owns the period, so a change to it moves
    both clocks without an edit here."""
    payload_due = erasure.SignalExpiry._deadline(policies.RECORD_EXTERNAL_SIGNAL, _NOW, payload=True)
    record_due = erasure.SignalExpiry._deadline(policies.RECORD_EXTERNAL_SIGNAL, _NOW, payload=False)

    # The payload clock is the earlier one, so its cutoff is the more recent instant.
    assert record_due < payload_due < _NOW
    days = policies.disposition(policies.RECORD_EXTERNAL_SIGNAL).payload_retention_days
    assert days is not None
    assert payload_due == _NOW - datetime.timedelta(days=days)


def test_an_event_bounded_class_selects_nothing_rather_than_guessing_a_period() -> None:
    """A class with no clock is bounded by tenant or workspace deletion. A deadline of
    `now` selects nothing, where an invented period would delete on a date no policy
    chose."""
    workspace_entry = policies.RECORD_WORKSPACE_ENTRY
    assert policies.disposition(workspace_entry).retention_days is None
    assert erasure.SignalExpiry._deadline(workspace_entry, _NOW, payload=False) == _NOW
