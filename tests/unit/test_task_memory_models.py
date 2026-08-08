"""The task-memory ORM declares the constraints that carry the guarantees.

The database is the enforcer — `tests/integration/test_task_memory_migrations.py`
proves the rules actually refuse rows. What this file pins is that the ORM still
*declares* them, without needing a container: a constraint quietly dropped from
the model is how the ORM and the schema start drifting, and the drift is silent
until a query fails in production.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from contextplane.workspaces.models import TaskCheckpoint, TaskHead, TaskParticipantGrant


def _constraint_names(model: type) -> set[str]:
    return {c.name for c in model.__table__.constraints if c.name} | {i.name for i in model.__table__.indexes if i.name}


def _check_clauses(model: type) -> dict[str, str]:
    return {c.name: str(c.sqltext) for c in model.__table__.constraints if isinstance(c, CheckConstraint) and c.name}


# --- grants -------------------------------------------------------------------


def test_the_self_grant_refusal_is_declared() -> None:
    """The rule that makes a grant a grant rather than a claim about oneself."""
    clauses = _check_clauses(TaskParticipantGrant)
    assert "ck_grant_not_self" in clauses
    assert "actor_id" in clauses["ck_grant_not_self"]
    assert "granted_by" in clauses["ck_grant_not_self"]


def test_the_role_vocabulary_is_closed() -> None:
    clause = _check_clauses(TaskParticipantGrant)["ck_grant_role"]
    for role in ("reader", "contributor", "owner", "auditor"):
        assert role in clause


def test_a_grant_window_cannot_close_before_it_opens() -> None:
    assert "ck_grant_window" in _check_clauses(TaskParticipantGrant)


def test_one_grant_per_actor_per_task_is_declared() -> None:
    unique = [c for c in TaskParticipantGrant.__table__.constraints if isinstance(c, UniqueConstraint)]
    columns = {tuple(col.name for col in c.columns) for c in unique}
    assert ("tenant_id", "task_id", "actor_id") in columns


def test_temporal_evidence_is_on_the_row() -> None:
    """A grant that was valid last month is not evidence today, so when it was
    made and when it stops applying travel with it."""
    columns = {c.name for c in TaskParticipantGrant.__table__.columns}
    assert {"granted_at", "expires_at", "resolver_version"} <= columns


def test_only_the_expiry_is_optional() -> None:
    """Absent expiry means the grant lasts as long as the task — a decision
    somebody made, not a gap. Everything else is required."""
    nullable = {c.name for c in TaskParticipantGrant.__table__.columns if c.nullable}
    assert nullable == {"expires_at"}


# --- checkpoints --------------------------------------------------------------


def test_the_chain_may_only_start_at_sequence_one() -> None:
    clause = _check_clauses(TaskCheckpoint)["ck_checkpoint_predecessor"]
    assert "predecessor_id IS NULL" in clause
    assert "predecessor_id IS NOT NULL" in clause


def test_two_writers_cannot_declare_the_same_step() -> None:
    unique = [c for c in TaskCheckpoint.__table__.constraints if isinstance(c, UniqueConstraint)]
    columns = {tuple(col.name for col in c.columns) for c in unique}
    assert ("tenant_id", "task_id", "sequence") in columns


def test_the_structured_fields_stay_separate_from_the_goal() -> None:
    """Resume treats them differently: an open question is work remaining, a
    completed check is work that need not repeat. Flattened into prose, both read
    as narrative and a resuming agent re-derives the distinction by guessing."""
    columns = {c.name for c in TaskCheckpoint.__table__.columns}
    assert {"decisions", "assumptions", "evidence", "completed_checks", "open_questions"} <= columns


def test_the_checkpoint_carries_its_own_retention_and_digest() -> None:
    columns = {c.name for c in TaskCheckpoint.__table__.columns}
    assert {"retention_policy", "digest"} <= columns


def test_next_action_is_nullable_because_absent_differs_from_empty() -> None:
    nullable = {c.name for c in TaskCheckpoint.__table__.columns if c.nullable}
    assert "next_action" in nullable


# --- head ---------------------------------------------------------------------


def test_the_head_is_keyed_by_tenant_and_task_so_it_can_only_be_overwritten() -> None:
    """One row per task. The chain is the history; a second copy here would be a
    second answer to what happened."""
    primary = tuple(c.name for c in TaskHead.__table__.primary_key.columns)
    assert primary == ("tenant_id", "task_id")


def test_the_head_points_at_a_real_checkpoint() -> None:
    targets = {fk.column.table.name for fk in TaskHead.__table__.foreign_keys}
    assert "task_checkpoints" in targets


def test_every_task_memory_table_is_tenant_scoped() -> None:
    for model in (TaskParticipantGrant, TaskCheckpoint, TaskHead):
        assert "tenant_id" in {c.name for c in model.__table__.columns}, model.__tablename__
        assert "tenants" in {fk.column.table.name for fk in model.__table__.foreign_keys}, model.__tablename__
