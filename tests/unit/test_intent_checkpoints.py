"""Appending to a task chain and reading it back, against an in-memory database.

The fake here is not an assertion-free stand-in: it keeps real state, so the
tests can assert on ordering across two appends, on a retry finding its own
earlier row, and on a stored checkpoint being unchanged by everything that
happens after it. Postgres-enforced properties -- the append-only trigger, the
unique sequence index, two connections racing -- are proved against a live
database in the concurrency integration test; what is proved here is the
service's own decisions, which is the part a live database cannot check.

**What the audience tests below can and cannot prove.** They prove the service
asks the right question: that it binds the caller's actor, the moment the
checkpoint is stamped with, the recognised resolvers and the capability's role
set, and that it treats the answer correctly -- refusing an append, reporting a
read as absent. The fake decides authorization from those bound parameters.

They do **not** prove the database enforces anything. Neutering the `EXISTS`
clause in the statements leaves every test in this file passing, because the
parameters are still bound and the fake still evaluates them. That was measured,
not assumed. `tests/integration/test_intent_checkpoint_concurrency.py` is what
catches it, and the same experiment fails seven tests there. A fake cannot check
SQL semantics; pretending otherwise is how a missing predicate ships behind a
green suite, which is the defect this task exists to repair.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.context.schemas.trust import InvalidContextItem
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import TenantContext
from contextplane.workspaces.audience import AudienceDenied
from contextplane.workspaces.checkpoints import (
    ACTION_CHECKPOINT_APPENDED,
    ACTION_HEAD_SUMMARY_SET,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    IntentCheckpointService,
    checkpoint_identity,
)
from contextplane.workspaces.schemas.intent_memory import checkpoint_digest
from tests.helpers.clock import FakeClock

#: Masks any uuid in an error message, so two denials can be compared for
#: everything except the id the caller supplied.
_MASK_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_NOW = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT_A = uuid.uuid4()
_TENANT_B = uuid.uuid4()
_ACTOR_A = uuid.uuid4()
_ACTOR_B = uuid.uuid4()


def _ctx(tenant: uuid.UUID = _TENANT_A, actor: uuid.UUID = _ACTOR_A) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=actor, roles=["producer"])


def _reference(external_id: str = "42", *, source: str = "github", kind: str = "issue") -> dict[str, Any]:
    return {
        "source_system": source,
        "source_namespace": "roughcompass/contextplane",
        "kind": kind,
        "external_id": external_id,
        "classification": "internal",
        "external_authority": "repo-admin",
    }


# ---------------------------------------------------------------------------
# In-memory stand-in for the three tables the service writes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Db:
    """State the fake session reads and writes, so tests can assert on it."""

    checkpoints: dict[uuid.UUID, dict[str, Any]] = dataclasses.field(default_factory=dict)
    heads: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = dataclasses.field(default_factory=dict)
    audits: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    #: Participant grants, which every checkpoint statement now tests against.
    #: Modelled rather than stubbed true: a fake that always authorized would
    #: make every test below pass whether or not the predicate was in the SQL,
    #: which is the shape that let the missing check ship in the first place.
    grants: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Statement fingerprints in the order they were issued, so a test can
    # assert the task lock was taken before the head was read.
    calls: list[str] = dataclasses.field(default_factory=list)

    def grant(
        self,
        *,
        intent_id: uuid.UUID,
        actor_id: uuid.UUID | str,
        tenant_id: uuid.UUID = _TENANT_A,
        role: str = "owner",
        granted_at: datetime.datetime | None = None,
        expires_at: datetime.datetime | None = None,
        resolver_version: str = "explicit/v1",
    ) -> None:
        self.grants.append(
            {
                "tenant_id": tenant_id,
                "intent_id": intent_id,
                "actor_id": str(actor_id),
                "role": role,
                "granted_at": granted_at or (_NOW - datetime.timedelta(days=1)),
                "expires_at": expires_at,
                "resolver_version": resolver_version,
            }
        )

    def authorizes(self, args: dict[str, Any], *, intent_id: uuid.UUID) -> bool:
        """Evaluate the EXISTS clause the statements carry, against `grants`.

        Reads the bound parameters the query actually sent -- actor, moment,
        recognised resolvers and the capability's role set -- so a statement
        that stopped binding one of them stops being authorized here too.
        """
        moment = args["moment"]
        return any(
            grant["tenant_id"] == args["tenant_id"]
            and grant["intent_id"] == intent_id
            and grant["actor_id"] == args["actor_id"]
            and grant["granted_at"] <= moment
            and (grant["expires_at"] is None or grant["expires_at"] > moment)
            and grant["resolver_version"] in args["resolvers"]
            and grant["role"] in args["roles"]
            for grant in self.grants
        )


def _result(
    *,
    rows: list[dict[str, Any]] | None = None,
    rowcount: int = 0,
    returning: tuple[Any, ...] | None = None,
) -> MagicMock:
    """A driver result, in the two shapes the statements here actually read.

    `rows` feeds `.mappings().first()`, which is how every read in this module
    consumes its answer. `returning` feeds the positional forms a `RETURNING`
    clause is read through -- `.first()` for a row and `.scalar_one()` for a
    single column -- so a statement that stopped returning what it claims to
    fails here rather than handing back a `MagicMock` that behaves like a value.
    """
    mappings = MagicMock()
    mappings.first = MagicMock(return_value=(rows or [None])[0] if rows else None)
    result = MagicMock()
    result.mappings = MagicMock(return_value=mappings)
    result.rowcount = rowcount
    result.first = MagicMock(return_value=returning)
    result.scalar_one = MagicMock(return_value=None if returning is None else returning[0])
    # ORM-style reads consume `.scalars()`. Only the admission scan does that
    # here, and it has no tenant rows to find, so an empty iterator is the whole
    # of it -- the built-in floor it falls back to is what the tests exercise.
    result.scalars = MagicMock(return_value=iter(()))
    return result


def _make_session(db: _Db) -> AsyncMock:
    """An AsyncMock session that routes by SQL keyword and keeps real rows.

    JSONB columns are stored exactly as the service binds them -- as JSON text --
    because that is one of the two shapes a driver can hand back, and the read
    path has to survive it.
    """

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        args = params or {}

        if "pg_advisory_xact_lock" in sql:
            db.calls.append(f"lock:{args['key']}")
            return _result()

        if sql.startswith("SELECT") and "FROM intent_heads" in sql:
            db.calls.append("select_head")
            row = db.heads.get((args["tenant_id"], args["intent_id"]))
            return _result(rows=[row] if row else None)

        if sql.startswith("SELECT") and "FROM intent_checkpoints" in sql:
            db.calls.append("select_checkpoint")
            assert "intent_participant_grants" in sql, "the read must carry its own audience test"
            for row in db.checkpoints.values():
                if row["tenant_id"] != args["tenant_id"]:
                    continue
                matched = ("checkpoint_id = :cid" in sql and row["checkpoint_id"] == args["cid"]) or (
                    "digest = :digest" in sql and row["digest"] == args["digest"]
                )
                if matched:
                    # The row exists; the EXISTS decides whether this actor sees
                    # it. Returning nothing is the same answer as not found.
                    if db.authorizes(args, intent_id=row["intent_id"]):
                        return _result(rows=[row])
                    return _result()
            return _result()

        if "INSERT INTO intent_checkpoints" in sql:
            db.calls.append("insert_checkpoint")
            assert "intent_participant_grants" in sql, "the append must carry its own audience test"
            if not db.authorizes(args, intent_id=args["intent_id"]):
                # `INSERT ... SELECT ... WHERE EXISTS` inserts no row rather
                # than raising, so the caller learns from the row count.
                return _result(rowcount=0)
            if args["cid"] in db.checkpoints:
                msg = "duplicate key value violates unique constraint task_checkpoints_pkey"
                raise AssertionError(msg)
            db.checkpoints[args["cid"]] = {
                "checkpoint_id": args["cid"],
                "tenant_id": args["tenant_id"],
                "intent_id": args["intent_id"],
                "sequence": args["sequence"],
                "predecessor_id": args["pred"],
                "goal": args["goal"],
                "decisions": args["decisions"],
                "assumptions": args["assumptions"],
                "evidence": args["evidence"],
                "completed_checks": args["completed_checks"],
                "open_questions": args["open_questions"],
                "next_action": args["next_action"],
                "author": args["author"],
                "recorded_at": args["recorded_at"],
                "retention_policy": args["retention_policy"],
                "digest": args["digest"],
            }
            return _result(rowcount=1)

        if "INSERT INTO intent_heads" in sql:
            db.calls.append("upsert_head")
            key = (args["tenant_id"], args["intent_id"])
            current = db.heads.get(key)
            if current is None or current["head_sequence"] < args["sequence"]:
                db.heads[key] = {
                    "tenant_id": args["tenant_id"],
                    "intent_id": args["intent_id"],
                    "head_checkpoint_id": args["cid"],
                    "head_sequence": args["sequence"],
                    "summary": args["summary"],
                    "updated_at": args["updated_at"],
                }
            return _result()

        if "UPDATE intent_heads" in sql:
            db.calls.append("update_head_summary")
            head = db.heads.get((args["tenant_id"], args["intent_id"]))
            if head is None:
                return _result(rowcount=0)
            head["summary"] = args["summary"]
            head["updated_at"] = args["updated_at"]
            # The head checkpoint comes back from the update itself, which is what
            # the caller registers the new summary against.
            return _result(rowcount=1, returning=(head["head_checkpoint_id"],))

        if "INSERT INTO derivative_registrations" in sql:
            db.calls.append("register_summary_derivative")
            # The registration's own id, returned so the caller can link its
            # sources to it. A real value rather than a mock: the caller parses it.
            return _result(rowcount=1, returning=(uuid.uuid4(),))

        if "INSERT INTO derivative_source_links" in sql:
            db.calls.append("link_summary_source")
            return _result(rowcount=1)

        if "INSERT INTO audit_log" in sql:
            db.calls.append("insert_audit")
            db.audits.append({**args, "after": json.loads(args["after"])})
            return _result()

        # The admission scan every append now runs before storage. Routed here
        # rather than stubbed away, because the append genuinely reads this
        # table and a fake that refuses to answer would only prove the fake is
        # out of date. No tenant rows: the built-in floor is what these tests
        # exercise, and `test_content_carrying_a_prohibited_class_is_refused`
        # below asserts it still fires with the table empty.
        if "FROM pii_patterns" in sql or "FROM pii_field_policies" in sql:
            return _result()

        if "INSERT INTO pii_detection_log" in sql:
            db.calls.append("insert_pii_detection")
            return _result(rowcount=1)

        msg = f"unrouted statement: {sql}"
        raise AssertionError(msg)

    session = AsyncMock()
    session.execute = _execute
    return session


def _participating_task(db: _Db, *, actor: uuid.UUID = _ACTOR_A, role: str = "owner") -> uuid.UUID:
    """A task this actor participates in.

    Participation is now the precondition for every append and every read, so
    almost every test needs one. Spelled at the call site rather than defaulted
    inside `_Db`, so a test that means to exercise a non-participant does not
    have to remember to undo a default it never wrote.
    """
    task = uuid.uuid4()
    db.grant(intent_id=task, actor_id=actor, role=role)
    return task


def _make_service(
    db: _Db, *, retention_policy: str = "standard", clock: FakeClock | None = None
) -> IntentCheckpointService:
    session = _make_session(db)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock(return_value=cm)
    return IntentCheckpointService(
        session_factory=factory,
        clock=clock or FakeClock(_NOW),
        retention_policy=retention_policy,
    )


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_checkpoint_on_a_task_starts_the_chain() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    result = await service.append_checkpoint(
        _ctx(), intent_id=task, payload={"goal": "ship it", "next_action": "run the gates"}, idempotency_key="k1"
    )

    assert result.created is True
    assert result.checkpoint.sequence == 1
    assert result.checkpoint.predecessor_id is None
    assert db.heads[(_TENANT_A, task)]["head_checkpoint_id"] == result.checkpoint.checkpoint_id
    assert db.heads[(_TENANT_A, task)]["head_sequence"] == 1


@pytest.mark.asyncio
async def test_a_second_append_names_the_first_as_its_predecessor() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    first = await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "step one"}, idempotency_key="k1")
    second = await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "step two"}, idempotency_key="k2")

    assert second.checkpoint.sequence == 2
    assert second.checkpoint.predecessor_id == first.checkpoint.checkpoint_id
    assert db.heads[(_TENANT_A, task)]["head_checkpoint_id"] == second.checkpoint.checkpoint_id


@pytest.mark.asyncio
async def test_the_task_lock_is_taken_before_the_head_is_read() -> None:
    """Reading first and locking after would let two appends derive one successor."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

    assert db.calls[0] == f"lock:{_TENANT_A}:{task}"
    assert db.calls.index(f"lock:{_TENANT_A}:{task}") < db.calls.index("select_head")


@pytest.mark.asyncio
async def test_two_tenants_appending_to_the_same_task_id_do_not_share_a_lock() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    # The same task id in the other tenant is a different task, and needs its
    # own grant -- which is the point of the isolation being asserted below.
    db.grant(intent_id=task, actor_id=_ACTOR_A, tenant_id=_TENANT_B)

    await service.append_checkpoint(_ctx(_TENANT_A), intent_id=task, payload={"goal": "a"}, idempotency_key="k")
    await service.append_checkpoint(_ctx(_TENANT_B), intent_id=task, payload={"goal": "b"}, idempotency_key="k")

    locks = {call for call in db.calls if call.startswith("lock:")}
    assert locks == {f"lock:{_TENANT_A}:{task}", f"lock:{_TENANT_B}:{task}"}
    # Two separate chains, each starting at 1 -- one tenant's task id says
    # nothing about another's.
    assert [row["sequence"] for row in db.checkpoints.values()] == [1, 1]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_under_the_same_key_returns_the_recorded_checkpoint() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)
    payload = {"goal": "ship it", "decisions": ["use the lock"]}

    first = await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")
    replay = await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")

    assert replay.created is False
    assert replay.checkpoint == first.checkpoint
    assert len(db.checkpoints) == 1
    assert db.heads[(_TENANT_A, task)]["head_sequence"] == 1


@pytest.mark.asyncio
async def test_reusing_a_key_for_different_content_conflicts() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

    with pytest.raises(ConflictError, match="different content"):
        await service.append_checkpoint(
            _ctx(), intent_id=task, payload={"goal": "ship something else"}, idempotency_key="k1"
        )

    assert len(db.checkpoints) == 1
    assert db.checkpoints[next(iter(db.checkpoints))]["goal"] == "ship it"


@pytest.mark.asyncio
async def test_reusing_a_key_under_a_different_author_conflicts() -> None:
    """Attribution is part of the content a key names, not a field a replay may change."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)
    # Both actors participate, so what this proves is the attribution conflict
    # and not an audience refusal wearing its clothes.
    db.grant(intent_id=task, actor_id=_ACTOR_B)
    payload = {"goal": "ship it"}

    await service.append_checkpoint(_ctx(actor=_ACTOR_A), intent_id=task, payload=payload, idempotency_key="k1")

    with pytest.raises(ConflictError):
        await service.append_checkpoint(_ctx(actor=_ACTOR_B), intent_id=task, payload=payload, idempotency_key="k1")


@pytest.mark.asyncio
async def test_a_replay_keeps_its_original_recorded_time() -> None:
    db = _Db()
    clock = FakeClock(_NOW)
    service = _make_service(db, clock=clock)
    task = _participating_task(db)
    payload = {"goal": "ship it"}

    first = await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")
    clock.tick(datetime.timedelta(hours=3))
    replay = await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")

    assert replay.checkpoint.recorded_at == first.checkpoint.recorded_at


@pytest.mark.asyncio
async def test_an_append_without_an_idempotency_key_is_refused() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="idempotency key"):
        await service.append_checkpoint(
            _ctx(), intent_id=_participating_task(db), payload={"goal": "x"}, idempotency_key="   "
        )
    assert db.checkpoints == {}


@pytest.mark.asyncio
async def test_an_oversized_idempotency_key_is_refused() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="over the"):
        await service.append_checkpoint(
            _ctx(),
            intent_id=_participating_task(db),
            payload={"goal": "x"},
            idempotency_key="k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1),
        )


def test_checkpoint_identity_is_stable_and_scoped_to_its_tenant() -> None:
    # A pure derivation, with no database and no audience: identity is computed
    # before anything is authorized, and stays the same either way.
    task = uuid.uuid4()
    a = checkpoint_identity(tenant_id=_TENANT_A, intent_id=task, idempotency_key="k1")
    b = checkpoint_identity(tenant_id=_TENANT_B, intent_id=task, idempotency_key="k1")

    assert a == checkpoint_identity(tenant_id=_TENANT_A, intent_id=task, idempotency_key="k1")
    assert a != b


# ---------------------------------------------------------------------------
# Server-derived fields, evidence and retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_payload_that_supplies_its_own_author_is_refused() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="server-derived"):
        await service.append_checkpoint(
            _ctx(),
            intent_id=_participating_task(db),
            payload={"goal": "x", "author": "somebody-else"},
            idempotency_key="k1",
        )
    assert db.checkpoints == {}


@pytest.mark.asyncio
async def test_the_author_is_the_authenticated_actor() -> None:
    db = _Db()
    service = _make_service(db)

    result = await service.append_checkpoint(
        _ctx(actor=_ACTOR_B),
        intent_id=_participating_task(db, actor=_ACTOR_B),
        payload={"goal": "x"},
        idempotency_key="k1",
    )

    assert result.checkpoint.author == str(_ACTOR_B)


@pytest.mark.asyncio
async def test_retention_is_bound_from_the_deployment_and_stored_on_the_row() -> None:
    db = _Db()
    service = _make_service(db, retention_policy="regulated-7y")

    result = await service.append_checkpoint(
        _ctx(), intent_id=_participating_task(db), payload={"goal": "x"}, idempotency_key="k1"
    )

    assert result.checkpoint.retention_policy == "regulated-7y"
    assert db.checkpoints[result.checkpoint.checkpoint_id]["retention_policy"] == "regulated-7y"


def test_a_service_without_a_retention_policy_will_not_construct() -> None:
    with pytest.raises(ValueError, match="retention policy"):
        IntentCheckpointService(session_factory=MagicMock(), clock=FakeClock(_NOW), retention_policy="  ")


@pytest.mark.asyncio
async def test_evidence_is_normalized_before_the_duplicate_check() -> None:
    """Two spellings of one reference are a duplicate, not two corroborating sources."""
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="normalized"):
        await service.append_checkpoint(
            _ctx(),
            intent_id=_participating_task(db),
            payload={"goal": "x"},
            idempotency_key="k1",
            evidence=[_reference("42"), _reference("42", source="GitHub")],
        )
    assert db.checkpoints == {}


@pytest.mark.asyncio
async def test_evidence_survives_storage_and_reads_back_verifiable() -> None:
    db = _Db()
    service = _make_service(db)
    observed = datetime.datetime(2026, 5, 1, 9, 0, tzinfo=datetime.UTC)

    written = await service.append_checkpoint(
        _ctx(),
        intent_id=_participating_task(db),
        payload={"goal": "x"},
        idempotency_key="k1",
        evidence=[{**_reference("42"), "observed_at": observed, "revision": "abc123"}],
    )
    read = await service.get_checkpoint(_ctx(), checkpoint_id=written.checkpoint.checkpoint_id)

    assert read == written.checkpoint
    assert read.evidence[0].observed_at == observed
    assert read.evidence[0].revision == "abc123"


@pytest.mark.asyncio
async def test_a_malformed_evidence_reference_is_refused_as_a_bad_request() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError):
        await service.append_checkpoint(
            _ctx(),
            intent_id=_participating_task(db),
            payload={"goal": "x"},
            idempotency_key="k1",
            evidence=[{**_reference("42"), "not_a_field": "surprise"}],
        )


# ---------------------------------------------------------------------------
# Retrieval stability and tenant scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_checkpoint_reads_back_unchanged_after_later_checkpoints_and_a_new_summary() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    first = await service.append_checkpoint(
        _ctx(),
        intent_id=task,
        payload={"goal": "step one", "open_questions": ["is the lock enough"]},
        idempotency_key="k1",
    )
    await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "step two"}, idempotency_key="k2")
    await service.set_head_summary(_ctx(), intent_id=task, summary="a completely different story")

    by_id = await service.get_checkpoint(_ctx(), checkpoint_id=first.checkpoint.checkpoint_id)
    by_digest = await service.get_checkpoint_by_digest(_ctx(), digest=first.checkpoint.digest)

    assert by_id == first.checkpoint
    assert by_digest == first.checkpoint
    assert by_id.open_questions == ("is the lock enough",)


@pytest.mark.asyncio
async def test_a_summary_edit_leaves_the_head_pointing_at_the_same_checkpoint() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    written = await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "ship it"}, idempotency_key="k1")
    await service.set_head_summary(_ctx(), intent_id=task, summary="rewritten by somebody else")
    head = await service.get_head(_ctx(), intent_id=task)

    assert head["head_checkpoint_id"] == written.checkpoint.checkpoint_id
    assert head["head_sequence"] == 1
    assert head["summary"] == "rewritten by somebody else"


@pytest.mark.asyncio
async def test_summarizing_a_task_with_no_checkpoints_is_not_found() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(NotFoundError):
        await service.set_head_summary(_ctx(), intent_id=_participating_task(db), summary="nothing to summarize")


@pytest.mark.asyncio
async def test_an_empty_summary_is_refused() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)
    await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "x"}, idempotency_key="k1")

    with pytest.raises(ValidationError):
        await service.set_head_summary(_ctx(), intent_id=task, summary="   ")


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_a_checkpoint_by_id_or_digest() -> None:
    db = _Db()
    service = _make_service(db)

    written = await service.append_checkpoint(
        _ctx(_TENANT_A), intent_id=_participating_task(db), payload={"goal": "ship it"}, idempotency_key="k1"
    )

    with pytest.raises(NotFoundError):
        await service.get_checkpoint(_ctx(_TENANT_B), checkpoint_id=written.checkpoint.checkpoint_id)
    with pytest.raises(NotFoundError):
        await service.get_checkpoint_by_digest(_ctx(_TENANT_B), digest=written.checkpoint.digest)


@pytest.mark.asyncio
async def test_an_unknown_checkpoint_is_not_found() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(NotFoundError):
        await service.get_checkpoint(_ctx(), checkpoint_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        await service.get_checkpoint_by_digest(_ctx(), digest="  ")


@pytest.mark.asyncio
async def test_a_row_whose_content_no_longer_matches_its_digest_is_refused_on_read() -> None:
    """Append-only is a claim about writers; the digest check does not depend on it."""
    db = _Db()
    service = _make_service(db)
    written = await service.append_checkpoint(
        _ctx(), intent_id=_participating_task(db), payload={"goal": "ship it"}, idempotency_key="k1"
    )
    db.checkpoints[written.checkpoint.checkpoint_id]["goal"] = "ship something else"

    with pytest.raises(InvalidContextItem, match="digest does not match"):
        await service.get_checkpoint(_ctx(), checkpoint_id=written.checkpoint.checkpoint_id)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_audit_row_is_written_on_the_same_session_as_the_append() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)

    written = await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

    assert db.calls.index("insert_checkpoint") < db.calls.index("insert_audit")
    (audit,) = db.audits
    assert audit["action"] == ACTION_CHECKPOINT_APPENDED
    assert audit["target_id"] == written.checkpoint.checkpoint_id
    assert audit["actor_id"] == _ACTOR_A
    assert audit["after"]["sequence"] == 1
    assert audit["after"]["digest"] == written.checkpoint.digest


@pytest.mark.asyncio
async def test_the_audit_row_carries_no_checkpoint_content() -> None:
    """Content lives once, under the retention policy the checkpoint bound."""
    db = _Db()
    service = _make_service(db)

    await service.append_checkpoint(
        _ctx(),
        intent_id=_participating_task(db),
        payload={
            "goal": "a secret-sounding goal",
            "decisions": ["a decision nobody else should read"],
            "open_questions": ["an open question"],
            "next_action": "a next action",
        },
        idempotency_key="k1",
    )

    (audit,) = db.audits
    serialized = json.dumps(audit["after"])
    for content in (
        "a secret-sounding goal",
        "a decision nobody else should read",
        "an open question",
        "a next action",
    ):
        assert content not in serialized


@pytest.mark.asyncio
async def test_a_replay_writes_no_second_audit_row() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)
    payload = {"goal": "ship it"}

    await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")
    await service.append_checkpoint(_ctx(), intent_id=task, payload=payload, idempotency_key="k1")

    assert len(db.audits) == 1


@pytest.mark.asyncio
async def test_a_summary_edit_is_audited() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db)
    await service.append_checkpoint(_ctx(), intent_id=task, payload={"goal": "x"}, idempotency_key="k1")

    await service.set_head_summary(_ctx(), intent_id=task, summary="new summary")

    assert [audit["action"] for audit in db.audits] == [ACTION_CHECKPOINT_APPENDED, ACTION_HEAD_SUMMARY_SET]


# ---------------------------------------------------------------------------
# The audience, enforced by the service rather than by whoever called it
# ---------------------------------------------------------------------------
#
# Every test below calls the service directly, with no router and no transport
# guard in the way. That is the whole point: the surfaces had their own check
# and the service had none, so any other caller reached every task in the
# tenant. A guard that only exists at the edge is a guard the next caller does
# not inherit.


@pytest.mark.asyncio
async def test_a_non_participant_cannot_append_to_a_task() -> None:
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_B), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )

    assert db.checkpoints == {}, "the refusal must leave no row behind"


@pytest.mark.asyncio
async def test_a_refused_append_writes_no_head_and_no_audit_row() -> None:
    """A denial that still moved the head would leave the task pointing at a
    checkpoint that does not exist."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_B), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )

    assert db.heads == {}
    assert db.audits == []


@pytest.mark.asyncio
async def test_content_carrying_a_prohibited_class_is_refused() -> None:
    """With no tenant policy rows at all -- the fake answers both policy reads
    empty. The floor lives in code, so a deployment that has configured nothing
    still refuses; a scan that only fired for a configured tenant would leave
    the default deployment storing the card."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)

    with pytest.raises(ValidationError, match="prohibited class"):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A),
            intent_id=task,
            payload={"goal": "charge 4111111111111111"},
            idempotency_key="k-pii",
        )

    assert db.heads == {}, "a refused append must not move the head"
    assert "insert_checkpoint" not in db.calls, "and must not have stored the content it refused"
    actions = [audit["action"] for audit in db.audits]
    assert actions == ["context.admission_refused"], (
        "the refusal is the only thing that happened, and the audit log says so -- "
        "an append row beside it would describe a step that does not exist"
    )


@pytest.mark.asyncio
async def test_the_scan_runs_before_the_task_lock_is_taken() -> None:
    """The lock serializes every append to one task. Holding it across a detector
    sweep would make append throughput a function of scan cost, so the refusal
    has to happen before `lock_task` -- and nothing the scan decides needs task
    state, which is what makes that safe."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)

    with pytest.raises(ValidationError):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A),
            intent_id=task,
            payload={"goal": "charge 4111111111111111"},
            idempotency_key="k-pii-lock",
        )

    assert not any(
        call.startswith("lock:") for call in db.calls
    ), "a refused append must not have contended for the task lock"


@pytest.mark.asyncio
async def test_a_reader_cannot_append() -> None:
    """`reader` carries read and not extend. The capability is checked by
    membership in a role set, so a role that gains reading does not thereby
    gain writing."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A, role="reader")

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )


@pytest.mark.asyncio
async def test_an_auditor_can_read_but_cannot_append() -> None:
    """The role that no linear ordering places correctly: it reads everything
    and writes nothing, so a `>= contributor` test would either lock it out of
    reads or hand it writes."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)

    written = await service.append_checkpoint(
        _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
    )

    db.grants.clear()
    db.grant(intent_id=task, actor_id=_ACTOR_B, role="auditor")

    assert await service.get_checkpoint(_ctx(actor=_ACTOR_B), checkpoint_id=written.checkpoint.checkpoint_id)
    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_B), intent_id=task, payload={"goal": "y"}, idempotency_key="k2"
        )


@pytest.mark.asyncio
async def test_a_non_participant_reads_a_checkpoint_as_not_found() -> None:
    """Not "forbidden". A refusal distinguishable from absence tells the caller
    the checkpoint exists, and repeated across ids that enumerates the tenant."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)
    written = await service.append_checkpoint(
        _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
    )

    with pytest.raises(NotFoundError):
        await service.get_checkpoint(_ctx(actor=_ACTOR_B), checkpoint_id=written.checkpoint.checkpoint_id)


@pytest.mark.asyncio
async def test_an_unknown_checkpoint_and_a_forbidden_one_answer_identically() -> None:
    """The pair that makes the previous test mean something: if these differed,
    the difference would be the oracle."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)
    written = await service.append_checkpoint(
        _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
    )

    with pytest.raises(NotFoundError) as forbidden_error:
        await service.get_checkpoint(_ctx(actor=_ACTOR_B), checkpoint_id=written.checkpoint.checkpoint_id)
    with pytest.raises(NotFoundError) as missing_error:
        await service.get_checkpoint(_ctx(actor=_ACTOR_B), checkpoint_id=uuid.uuid4())

    # Same exception type, and neither message mentions why. Ids are masked by
    # shape, because the id a caller passed in is the one thing the two answers
    # may legitimately differ on -- everything else has to match.
    forbidden = _MASK_UUID.sub("<id>", str(forbidden_error.value))
    missing = _MASK_UUID.sub("<id>", str(missing_error.value))

    assert forbidden == missing
    for phrase in ("participant", "grant", "forbidden", "permission", "expired"):
        assert phrase not in forbidden.lower(), f"a denial that says {phrase!r} is a denial that can be probed"


@pytest.mark.asyncio
async def test_a_non_participant_cannot_read_by_digest_either() -> None:
    """The digest read is the second door into the same row, and it was the one
    a surface-level guard was most likely to miss."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)
    written = await service.append_checkpoint(
        _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
    )

    with pytest.raises(NotFoundError):
        await service.get_checkpoint_by_digest(_ctx(actor=_ACTOR_B), digest=written.checkpoint.digest)


@pytest.mark.asyncio
async def test_an_expired_grant_stops_authorizing() -> None:
    """Evaluated against the moment the checkpoint is stamped with, so a grant
    that lapsed before the append does not carry it."""
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    db.grant(intent_id=task, actor_id=_ACTOR_A, expires_at=_NOW - datetime.timedelta(minutes=1))

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )


@pytest.mark.asyncio
async def test_a_grant_that_starts_later_does_not_authorize_yet() -> None:
    """The other end of the window. A grant recorded with a future start is not
    a grant now, and reading `granted_at` as "whenever the row appeared" is how
    that turns into one."""
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    db.grant(intent_id=task, actor_id=_ACTOR_A, granted_at=_NOW + datetime.timedelta(hours=1))

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )


@pytest.mark.asyncio
async def test_a_grant_from_an_unrecognized_resolver_is_not_evidence() -> None:
    """A row is not authority on its own; it has to have come from a resolver
    this build still recognizes."""
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    db.grant(intent_id=task, actor_id=_ACTOR_A, resolver_version="retired-scheme/v0")

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )


@pytest.mark.asyncio
async def test_a_grant_on_another_task_does_not_carry_over() -> None:
    """Participation is per task. A grant that authorized every task would make
    the check look present and enforce nothing."""
    db = _Db()
    service = _make_service(db)
    _participating_task(db, actor=_ACTOR_A)
    other = uuid.uuid4()

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_A), intent_id=other, payload={"goal": "x"}, idempotency_key="k1"
        )


@pytest.mark.asyncio
async def test_a_non_participant_cannot_replay_an_existing_append() -> None:
    """The replay path reads before it writes, so it needs the same capability
    as the write. Otherwise a caller who may not append could learn a stored
    checkpoint by guessing the key that wrote it."""
    db = _Db()
    service = _make_service(db)
    task = _participating_task(db, actor=_ACTOR_A)
    await service.append_checkpoint(_ctx(actor=_ACTOR_A), intent_id=task, payload={"goal": "x"}, idempotency_key="k1")

    with pytest.raises(AudienceDenied):
        await service.append_checkpoint(
            _ctx(actor=_ACTOR_B), intent_id=task, payload={"goal": "x"}, idempotency_key="k1"
        )


# ---------------------------------------------------------------------------
# Golden regressions across the nomenclature cutover
#
# The expected values below were computed from the pre-cutover source -- the
# namespace, the uuid5 subject string and the length-prefixed sha256 as they
# were spelled before any rename -- and are pinned here as literals. They are
# not recomputed from the code under test, because a golden derived from the
# thing it checks proves only that the code agrees with itself.
#
# What they establish is narrow and worth stating exactly: the rename moved
# parameter and column names, and neither identity nor digest is derived from a
# name. Both are derived from values, in a fixed order. So a triple and a body
# that produced these bytes before the cutover must still produce them after,
# and a rename that reached into the derivation would fail here rather than in a
# database whose checkpoint chain no longer verifies.
# ---------------------------------------------------------------------------

#: One pre-cutover tenant / scoped-uuid / idempotency triple, spelled as strings
#: because that is what the derivation consumes.
_GOLDEN_TENANT = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
_GOLDEN_INTENT = uuid.UUID("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
_GOLDEN_KEY = "append-1"

#: `uuid5(_CHECKPOINT_NAMESPACE, f"{tenant}:{intent}:{key}")` under the
#: pre-cutover namespace `8d0c6f0f-52a2-5b3f-9a3d-1f3a0f9c6d21`.
_GOLDEN_CHECKPOINT_ID = uuid.UUID("24c93820-8cbf-5f44-abbf-c7efa30808d0")

#: The digest of the body below at sequence 1 with no predecessor.
_GOLDEN_DIGEST = "18dbae1a71189855862740bd6bbe26eaaacbf2e52d48126001012b3f381aa7be"

_GOLDEN_BODY: dict[str, Any] = {
    "goal": "ship the nomenclature cutover",
    "decisions": ("rename in place", "keep the index names"),
    "assumptions": ("a single head at 0048",),
    "completed_checks": ("engine check exits zero",),
    "open_questions": ("request_digest cannot be recomputed",),
    "next_action": "run the verify line",
    "author": "agent:idr-t03",
    "retention_policy": "standard",
}


def test_the_scoped_checkpoint_id_survives_the_rename() -> None:
    """The uuid5 namespace and subject ordering are unchanged.

    The subject interpolates the three *values*; the parameter that carries the
    middle one was renamed. If the rename had reached the namespace constant or
    reordered the subject, this is the assertion that goes red -- and it goes
    red here rather than as a retry that silently fails to find its own earlier
    write, which is what the derived id exists to prevent.
    """
    assert (
        checkpoint_identity(
            tenant_id=_GOLDEN_TENANT,
            intent_id=_GOLDEN_INTENT,
            idempotency_key=_GOLDEN_KEY,
        )
        == _GOLDEN_CHECKPOINT_ID
    )


def test_the_checkpoint_digest_bytes_survive_the_rename() -> None:
    """Content and value ordering digest identically across the cutover.

    Every list field carries a member, so a reordering of the parts -- the one
    change to this derivation that would still produce a well-formed digest --
    cannot pass. `recorded_at` is deliberately absent from the derivation and so
    is absent here; folding a clock in would make this golden expire.
    """
    assert (
        checkpoint_digest(
            checkpoint_id=_GOLDEN_CHECKPOINT_ID,
            intent_id=_GOLDEN_INTENT,
            sequence=1,
            predecessor_id=None,
            goal=_GOLDEN_BODY["goal"],
            decisions=_GOLDEN_BODY["decisions"],
            assumptions=_GOLDEN_BODY["assumptions"],
            evidence=(),
            completed_checks=_GOLDEN_BODY["completed_checks"],
            open_questions=_GOLDEN_BODY["open_questions"],
            next_action=_GOLDEN_BODY["next_action"],
            author=_GOLDEN_BODY["author"],
            retention_policy=_GOLDEN_BODY["retention_policy"],
        )
        == _GOLDEN_DIGEST
    )


def test_the_digest_still_distinguishes_a_reordered_body() -> None:
    """The golden above is only evidence if the digest can tell bodies apart.

    A digest that ignored its inputs would satisfy the pinned value and every
    other assertion in this file. Swapping two decisions is the smallest change
    that leaves the same parts present in a different order, so it is what
    proves the length-prefixing does the work it is documented to do.
    """
    swapped = tuple(reversed(_GOLDEN_BODY["decisions"]))
    assert (
        checkpoint_digest(
            checkpoint_id=_GOLDEN_CHECKPOINT_ID,
            intent_id=_GOLDEN_INTENT,
            sequence=1,
            predecessor_id=None,
            goal=_GOLDEN_BODY["goal"],
            decisions=swapped,
            assumptions=_GOLDEN_BODY["assumptions"],
            evidence=(),
            completed_checks=_GOLDEN_BODY["completed_checks"],
            open_questions=_GOLDEN_BODY["open_questions"],
            next_action=_GOLDEN_BODY["next_action"],
            author=_GOLDEN_BODY["author"],
            retention_policy=_GOLDEN_BODY["retention_policy"],
        )
        != _GOLDEN_DIGEST
    )
