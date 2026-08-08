"""Appending to a task chain and reading it back, against an in-memory database.

The fake here is not an assertion-free stand-in: it keeps real state, so the
tests can assert on ordering across two appends, on a retry finding its own
earlier row, and on a stored checkpoint being unchanged by everything that
happens after it. Postgres-enforced properties -- the append-only trigger, the
unique sequence index, two connections racing -- are proved against a live
database in the concurrency integration test; what is proved here is the
service's own decisions, which is the part a live database cannot check.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.context.schemas.trust import InvalidContextItem
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import TenantContext
from contextplane.workspaces.checkpoints import (
    ACTION_CHECKPOINT_APPENDED,
    ACTION_HEAD_SUMMARY_SET,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    TaskCheckpointService,
    checkpoint_identity,
)
from tests.helpers.clock import FakeClock

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
    # Statement fingerprints in the order they were issued, so a test can
    # assert the task lock was taken before the head was read.
    calls: list[str] = dataclasses.field(default_factory=list)


def _result(*, rows: list[dict[str, Any]] | None = None, rowcount: int = 0) -> MagicMock:
    mappings = MagicMock()
    mappings.first = MagicMock(return_value=(rows or [None])[0] if rows else None)
    result = MagicMock()
    result.mappings = MagicMock(return_value=mappings)
    result.rowcount = rowcount
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

        if sql.startswith("SELECT") and "FROM task_heads" in sql:
            db.calls.append("select_head")
            row = db.heads.get((args["tenant_id"], args["task_id"]))
            return _result(rows=[row] if row else None)

        if sql.startswith("SELECT") and "FROM task_checkpoints" in sql:
            db.calls.append("select_checkpoint")
            for row in db.checkpoints.values():
                if row["tenant_id"] != args["tenant_id"]:
                    continue
                if "checkpoint_id = :cid" in sql and row["checkpoint_id"] == args["cid"]:
                    return _result(rows=[row])
                if "digest = :digest" in sql and row["digest"] == args["digest"]:
                    return _result(rows=[row])
            return _result()

        if "INSERT INTO task_checkpoints" in sql:
            db.calls.append("insert_checkpoint")
            if args["cid"] in db.checkpoints:
                msg = "duplicate key value violates unique constraint task_checkpoints_pkey"
                raise AssertionError(msg)
            db.checkpoints[args["cid"]] = {
                "checkpoint_id": args["cid"],
                "tenant_id": args["tenant_id"],
                "task_id": args["task_id"],
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
            return _result()

        if "INSERT INTO task_heads" in sql:
            db.calls.append("upsert_head")
            key = (args["tenant_id"], args["task_id"])
            current = db.heads.get(key)
            if current is None or current["head_sequence"] < args["sequence"]:
                db.heads[key] = {
                    "tenant_id": args["tenant_id"],
                    "task_id": args["task_id"],
                    "head_checkpoint_id": args["cid"],
                    "head_sequence": args["sequence"],
                    "summary": args["summary"],
                    "updated_at": args["updated_at"],
                }
            return _result()

        if "UPDATE task_heads" in sql:
            db.calls.append("update_head_summary")
            head = db.heads.get((args["tenant_id"], args["task_id"]))
            if head is None:
                return _result(rowcount=0)
            head["summary"] = args["summary"]
            head["updated_at"] = args["updated_at"]
            return _result(rowcount=1)

        if "INSERT INTO audit_log" in sql:
            db.calls.append("insert_audit")
            db.audits.append({**args, "after": json.loads(args["after"])})
            return _result()

        msg = f"unrouted statement: {sql}"
        raise AssertionError(msg)

    session = AsyncMock()
    session.execute = _execute
    return session


def _make_service(
    db: _Db, *, retention_policy: str = "standard", clock: FakeClock | None = None
) -> TaskCheckpointService:
    session = _make_session(db)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    factory = MagicMock(return_value=cm)
    return TaskCheckpointService(
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
    task = uuid.uuid4()

    result = await service.append_checkpoint(
        _ctx(), task_id=task, payload={"goal": "ship it", "next_action": "run the gates"}, idempotency_key="k1"
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
    task = uuid.uuid4()

    first = await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "step one"}, idempotency_key="k1")
    second = await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "step two"}, idempotency_key="k2")

    assert second.checkpoint.sequence == 2
    assert second.checkpoint.predecessor_id == first.checkpoint.checkpoint_id
    assert db.heads[(_TENANT_A, task)]["head_checkpoint_id"] == second.checkpoint.checkpoint_id


@pytest.mark.asyncio
async def test_the_task_lock_is_taken_before_the_head_is_read() -> None:
    """Reading first and locking after would let two appends derive one successor."""
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()

    await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

    assert db.calls[0] == f"lock:{_TENANT_A}:{task}"
    assert db.calls.index(f"lock:{_TENANT_A}:{task}") < db.calls.index("select_head")


@pytest.mark.asyncio
async def test_two_tenants_appending_to_the_same_task_id_do_not_share_a_lock() -> None:
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()

    await service.append_checkpoint(_ctx(_TENANT_A), task_id=task, payload={"goal": "a"}, idempotency_key="k")
    await service.append_checkpoint(_ctx(_TENANT_B), task_id=task, payload={"goal": "b"}, idempotency_key="k")

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
    task = uuid.uuid4()
    payload = {"goal": "ship it", "decisions": ["use the lock"]}

    first = await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")
    replay = await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")

    assert replay.created is False
    assert replay.checkpoint == first.checkpoint
    assert len(db.checkpoints) == 1
    assert db.heads[(_TENANT_A, task)]["head_sequence"] == 1


@pytest.mark.asyncio
async def test_reusing_a_key_for_different_content_conflicts() -> None:
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()

    await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

    with pytest.raises(ConflictError, match="different content"):
        await service.append_checkpoint(
            _ctx(), task_id=task, payload={"goal": "ship something else"}, idempotency_key="k1"
        )

    assert len(db.checkpoints) == 1
    assert db.checkpoints[next(iter(db.checkpoints))]["goal"] == "ship it"


@pytest.mark.asyncio
async def test_reusing_a_key_under_a_different_author_conflicts() -> None:
    """Attribution is part of the content a key names, not a field a replay may change."""
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    payload = {"goal": "ship it"}

    await service.append_checkpoint(_ctx(actor=_ACTOR_A), task_id=task, payload=payload, idempotency_key="k1")

    with pytest.raises(ConflictError):
        await service.append_checkpoint(_ctx(actor=_ACTOR_B), task_id=task, payload=payload, idempotency_key="k1")


@pytest.mark.asyncio
async def test_a_replay_keeps_its_original_recorded_time() -> None:
    db = _Db()
    clock = FakeClock(_NOW)
    service = _make_service(db, clock=clock)
    task = uuid.uuid4()
    payload = {"goal": "ship it"}

    first = await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")
    clock.tick(datetime.timedelta(hours=3))
    replay = await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")

    assert replay.checkpoint.recorded_at == first.checkpoint.recorded_at


@pytest.mark.asyncio
async def test_an_append_without_an_idempotency_key_is_refused() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="idempotency key"):
        await service.append_checkpoint(_ctx(), task_id=uuid.uuid4(), payload={"goal": "x"}, idempotency_key="   ")
    assert db.checkpoints == {}


@pytest.mark.asyncio
async def test_an_oversized_idempotency_key_is_refused() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="over the"):
        await service.append_checkpoint(
            _ctx(),
            task_id=uuid.uuid4(),
            payload={"goal": "x"},
            idempotency_key="k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1),
        )


def test_checkpoint_identity_is_stable_and_scoped_to_its_tenant() -> None:
    task = uuid.uuid4()
    a = checkpoint_identity(tenant_id=_TENANT_A, task_id=task, idempotency_key="k1")
    b = checkpoint_identity(tenant_id=_TENANT_B, task_id=task, idempotency_key="k1")

    assert a == checkpoint_identity(tenant_id=_TENANT_A, task_id=task, idempotency_key="k1")
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
            _ctx(), task_id=uuid.uuid4(), payload={"goal": "x", "author": "somebody-else"}, idempotency_key="k1"
        )
    assert db.checkpoints == {}


@pytest.mark.asyncio
async def test_the_author_is_the_authenticated_actor() -> None:
    db = _Db()
    service = _make_service(db)

    result = await service.append_checkpoint(
        _ctx(actor=_ACTOR_B), task_id=uuid.uuid4(), payload={"goal": "x"}, idempotency_key="k1"
    )

    assert result.checkpoint.author == str(_ACTOR_B)


@pytest.mark.asyncio
async def test_retention_is_bound_from_the_deployment_and_stored_on_the_row() -> None:
    db = _Db()
    service = _make_service(db, retention_policy="regulated-7y")

    result = await service.append_checkpoint(_ctx(), task_id=uuid.uuid4(), payload={"goal": "x"}, idempotency_key="k1")

    assert result.checkpoint.retention_policy == "regulated-7y"
    assert db.checkpoints[result.checkpoint.checkpoint_id]["retention_policy"] == "regulated-7y"


def test_a_service_without_a_retention_policy_will_not_construct() -> None:
    with pytest.raises(ValueError, match="retention policy"):
        TaskCheckpointService(session_factory=MagicMock(), clock=FakeClock(_NOW), retention_policy="  ")


@pytest.mark.asyncio
async def test_evidence_is_normalized_before_the_duplicate_check() -> None:
    """Two spellings of one reference are a duplicate, not two corroborating sources."""
    db = _Db()
    service = _make_service(db)

    with pytest.raises(ValidationError, match="normalized"):
        await service.append_checkpoint(
            _ctx(),
            task_id=uuid.uuid4(),
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
        task_id=uuid.uuid4(),
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
            task_id=uuid.uuid4(),
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
    task = uuid.uuid4()

    first = await service.append_checkpoint(
        _ctx(),
        task_id=task,
        payload={"goal": "step one", "open_questions": ["is the lock enough"]},
        idempotency_key="k1",
    )
    await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "step two"}, idempotency_key="k2")
    await service.set_head_summary(_ctx(), task_id=task, summary="a completely different story")

    by_id = await service.get_checkpoint(_ctx(), checkpoint_id=first.checkpoint.checkpoint_id)
    by_digest = await service.get_checkpoint_by_digest(_ctx(), digest=first.checkpoint.digest)

    assert by_id == first.checkpoint
    assert by_digest == first.checkpoint
    assert by_id.open_questions == ("is the lock enough",)


@pytest.mark.asyncio
async def test_a_summary_edit_leaves_the_head_pointing_at_the_same_checkpoint() -> None:
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()

    written = await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "ship it"}, idempotency_key="k1")
    await service.set_head_summary(_ctx(), task_id=task, summary="rewritten by somebody else")
    head = await service.get_head(_ctx(), task_id=task)

    assert head["head_checkpoint_id"] == written.checkpoint.checkpoint_id
    assert head["head_sequence"] == 1
    assert head["summary"] == "rewritten by somebody else"


@pytest.mark.asyncio
async def test_summarizing_a_task_with_no_checkpoints_is_not_found() -> None:
    db = _Db()
    service = _make_service(db)

    with pytest.raises(NotFoundError):
        await service.set_head_summary(_ctx(), task_id=uuid.uuid4(), summary="nothing to summarize")


@pytest.mark.asyncio
async def test_an_empty_summary_is_refused() -> None:
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "x"}, idempotency_key="k1")

    with pytest.raises(ValidationError):
        await service.set_head_summary(_ctx(), task_id=task, summary="   ")


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_a_checkpoint_by_id_or_digest() -> None:
    db = _Db()
    service = _make_service(db)

    written = await service.append_checkpoint(
        _ctx(_TENANT_A), task_id=uuid.uuid4(), payload={"goal": "ship it"}, idempotency_key="k1"
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
        _ctx(), task_id=uuid.uuid4(), payload={"goal": "ship it"}, idempotency_key="k1"
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
    task = uuid.uuid4()

    written = await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "ship it"}, idempotency_key="k1")

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
        task_id=uuid.uuid4(),
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
    task = uuid.uuid4()
    payload = {"goal": "ship it"}

    await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")
    await service.append_checkpoint(_ctx(), task_id=task, payload=payload, idempotency_key="k1")

    assert len(db.audits) == 1


@pytest.mark.asyncio
async def test_a_summary_edit_is_audited() -> None:
    db = _Db()
    service = _make_service(db)
    task = uuid.uuid4()
    await service.append_checkpoint(_ctx(), task_id=task, payload={"goal": "x"}, idempotency_key="k1")

    await service.set_head_summary(_ctx(), task_id=task, summary="new summary")

    assert [audit["action"] for audit in db.audits] == [ACTION_CHECKPOINT_APPENDED, ACTION_HEAD_SUMMARY_SET]
