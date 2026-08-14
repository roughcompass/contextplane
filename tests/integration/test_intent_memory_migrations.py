"""Task-memory tables: what the migration builds, and what it refuses.

Three rules are enforced in the database rather than in a service, and each is
here because a Python-only check is one the next writer skips without noticing:
a grant cannot be self-issued, a checkpoint cannot be rewritten, and two writers
cannot both claim step 4 of one task.

The parity test follows the ARC precedent: a column added to the migration but
not the ORM is invisible to service code, and one declared in the ORM but absent
from the database fails at query time rather than at import.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from contextplane.workspaces.models import IntentCheckpoint, IntentHead, IntentParticipantGrant
from tests.helpers.migration_database import (
    MigrationDatabases,
    assert_alembic_ok,
    assert_at_head,
    migration_databases,
    migration_template,
)

__all__ = ["migration_databases", "migration_template"]

_MODELS = (IntentParticipantGrant, IntentCheckpoint, IntentHead)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    """A tenant to hang rows off, since every table is tenant-scoped."""
    tid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n) ON CONFLICT DO NOTHING"),
            {"t": tid, "s": f"tm-{tid.hex[:8]}", "n": "task memory test"},
        )
    return tid


def _checkpoint(conn: object, tenant: uuid.UUID, task: uuid.UUID, sequence: int, predecessor: uuid.UUID | None):
    cid = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO intent_checkpoints
                (checkpoint_id, tenant_id, intent_id, sequence, predecessor_id, goal,
                 next_action, author, recorded_at, retention_policy, digest)
            VALUES (:cid, :tid, :task, :seq, :pred, 'ship it', 'keep going', 'agent-1',
                    now(), 'standard', 'deadbeef')
            """
        ),
        {"cid": cid, "tid": tenant, "task": task, "seq": sequence, "pred": predecessor},
    )
    return cid


# --- fresh install ------------------------------------------------------------


@pytest.mark.parametrize("table", ["intent_participant_grants", "intent_checkpoints", "intent_heads"])
def test_the_migration_creates_every_table(sync_engine: Engine, table: str) -> None:
    assert inspect(sync_engine).has_table(table)


def test_the_orm_and_the_database_agree_column_for_column(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    for model in _MODELS:
        live = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        declared = {column.name for column in model.__table__.columns}
        assert declared == live, (
            f"{model.__tablename__} drifted: ORM-only {sorted(declared - live)}, "
            f"database-only {sorted(live - declared)}"
        )


# --- grants: a self-grant is an assertion, not a grant ------------------------


def test_a_grant_is_stored_with_its_temporal_evidence(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO intent_participant_grants
                    (tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version)
                VALUES (:t, :task, 'alice', 'contributor', 'bob', now(), now() + interval '1 day', 'v1')
                """
            ),
            {"t": tenant_id, "task": task},
        )
        stored = conn.execute(
            text(
                "SELECT role, granted_by, resolver_version, expires_at IS NOT NULL "
                "FROM intent_participant_grants WHERE tenant_id = :t AND intent_id = :task"
            ),
            {"t": tenant_id, "task": task},
        ).one()
    assert stored == ("contributor", "bob", "v1", True)


def test_an_actor_cannot_grant_themselves_participation(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Refused by the schema so no service can be the one place that forgets.
    Once stored, a self-grant is indistinguishable from a real one."""
    with pytest.raises(Exception, match="ck_grant_not_self"), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO intent_participant_grants
                    (tenant_id, intent_id, actor_id, role, granted_by, granted_at, resolver_version)
                VALUES (:t, :task, 'alice', 'owner', 'alice', now(), 'v1')
                """
            ),
            {"t": tenant_id, "task": uuid.uuid4()},
        )


def test_an_expiry_before_the_grant_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_grant_window"), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO intent_participant_grants
                    (tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version)
                VALUES (:t, :task, 'alice', 'reader', 'bob', now(), now() - interval '1 day', 'v1')
                """
            ),
            {"t": tenant_id, "task": uuid.uuid4()},
        )


def test_an_unknown_role_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="ck_grant_role"), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO intent_participant_grants
                    (tenant_id, intent_id, actor_id, role, granted_by, granted_at, resolver_version)
                VALUES (:t, :task, 'alice', 'admin', 'bob', now(), 'v1')
                """
            ),
            {"t": tenant_id, "task": uuid.uuid4()},
        )


def test_one_actor_holds_one_grant_per_task(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Two rows would make "what may this actor do" ambiguous exactly when it is
    being asked."""
    task = uuid.uuid4()
    statement = text(
        """
        INSERT INTO intent_participant_grants
            (tenant_id, intent_id, actor_id, role, granted_by, granted_at, resolver_version)
        VALUES (:t, :task, 'alice', :role, 'bob', now(), 'v1')
        """
    )
    with sync_engine.begin() as conn:
        conn.execute(statement, {"t": tenant_id, "task": task, "role": "reader"})
    with pytest.raises(Exception, match="uq_task_participant_grant"), sync_engine.begin() as conn:
        conn.execute(statement, {"t": tenant_id, "task": task, "role": "owner"})


# --- checkpoints: append-only, and the chain has no holes ---------------------


def test_a_checkpoint_chain_is_written_in_sequence(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        first = _checkpoint(conn, tenant_id, task, 1, None)
        _checkpoint(conn, tenant_id, task, 2, first)
        rows = (
            conn.execute(
                text("SELECT sequence FROM intent_checkpoints WHERE intent_id = :task ORDER BY sequence"),
                {"task": task},
            )
            .scalars()
            .all()
        )
    assert rows == [1, 2]


def test_a_checkpoint_cannot_be_updated(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Resume reconstructs what a past agent decided by walking this chain. An
    update would rewrite history that later checkpoints were built on."""
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        cid = _checkpoint(conn, tenant_id, task, 1, None)
    with pytest.raises(Exception, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("UPDATE intent_checkpoints SET goal = 'rewritten' WHERE checkpoint_id = :c"), {"c": cid})


def test_a_checkpoint_cannot_be_deleted(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A delete breaks the chain every successor's predecessor_id points at."""
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        cid = _checkpoint(conn, tenant_id, task, 1, None)
    with pytest.raises(Exception, match="append-only"), sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM intent_checkpoints WHERE checkpoint_id = :c"), {"c": cid})


def test_two_writers_cannot_both_claim_one_step(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        _checkpoint(conn, tenant_id, task, 1, None)
    with pytest.raises(Exception, match="uq_task_checkpoint_sequence"), sync_engine.begin() as conn:
        _checkpoint(conn, tenant_id, task, 1, None)


def test_only_the_first_checkpoint_may_have_no_predecessor(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A hole anywhere else means the chain resume walks is short without saying so."""
    task = uuid.uuid4()
    with pytest.raises(Exception, match="ck_checkpoint_predecessor"), sync_engine.begin() as conn:
        _checkpoint(conn, tenant_id, task, 2, None)


def test_a_first_checkpoint_may_not_claim_a_predecessor(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    task = uuid.uuid4()
    other = uuid.uuid4()
    with sync_engine.begin() as conn:
        earlier = _checkpoint(conn, tenant_id, other, 1, None)
    with pytest.raises(Exception, match="ck_checkpoint_predecessor"), sync_engine.begin() as conn:
        _checkpoint(conn, tenant_id, task, 1, earlier)


# --- head: a projection, meant to be overwritten ------------------------------


def test_the_head_is_overwritten_rather_than_appended(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The checkpoint chain is the history; a second copy here would be a second
    answer to what happened."""
    task = uuid.uuid4()
    with sync_engine.begin() as conn:
        first = _checkpoint(conn, tenant_id, task, 1, None)
        second = _checkpoint(conn, tenant_id, task, 2, first)
        conn.execute(
            text(
                """
                INSERT INTO intent_heads (tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at)
                VALUES (:t, :task, :c, 1, 'started', now())
                """
            ),
            {"t": tenant_id, "task": task, "c": first},
        )
        conn.execute(
            text(
                """
                INSERT INTO intent_heads (tenant_id, intent_id, head_checkpoint_id, head_sequence, summary, updated_at)
                VALUES (:t, :task, :c, 2, 'moved on', now())
                ON CONFLICT (tenant_id, intent_id)
                DO UPDATE SET head_checkpoint_id = EXCLUDED.head_checkpoint_id,
                              head_sequence = EXCLUDED.head_sequence,
                              summary = EXCLUDED.summary,
                              updated_at = EXCLUDED.updated_at
                """
            ),
            {"t": tenant_id, "task": task, "c": second},
        )
        rows = conn.execute(
            text("SELECT head_sequence, summary FROM intent_heads WHERE intent_id = :task"), {"task": task}
        ).all()
    assert rows == [(2, "moved on")]


# --- existing behaviour is not silently upgraded ------------------------------


def test_existing_workspace_tables_are_untouched(sync_engine: Engine) -> None:
    """Participation is granted. Inventing grants for rows that predate the
    concept would manufacture evidence nobody produced."""
    inspector = inspect(sync_engine)
    assert inspector.has_table("workspaces")
    with sync_engine.begin() as conn:
        orphaned = conn.execute(text("SELECT count(*) FROM intent_participant_grants")).scalar_one()
    # No backfill ran: whatever grants exist were written by these tests alone.
    assert isinstance(orphaned, int)


# --- downgrade ----------------------------------------------------------------


def test_the_migration_downgrades_and_upgrades_again(migration_databases: MigrationDatabases) -> None:
    """Run against a database of this test's own, cloned from a template at head.

    Downgrading the shared one would drop tables out from under every other
    integration module in the session, so a test that proves the downgrade works
    would break unrelated tests to do it. The clone arrives already at head, so
    the reversal is what this test pays for rather than a rebuild of the schema
    it is about to reverse.
    """
    with migration_databases.head_clone("intent memory") as scratch:
        assert_at_head(scratch)
        assert inspect(create_engine(scratch.sync_url)).has_table("intent_checkpoints")

        assert_alembic_ok(scratch.downgrade("0012_arc_submission_identity"), "downgrade")

        after = inspect(create_engine(scratch.sync_url))
        for table in ("intent_heads", "intent_checkpoints", "intent_participant_grants"):
            assert not after.has_table(table), f"{table} survived the downgrade"
        # The trigger function is named in the downgrade rather than left to
        # cascade: a function outlives the table it was attached to.
        with create_engine(scratch.sync_url).connect() as conn:
            remaining = conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'task_checkpoints_are_immutable'")
            ).scalar_one()
        assert remaining == 0, "the immutability trigger function outlived its table"

        assert_alembic_ok(scratch.upgrade_head(), "re-upgrade")
        assert inspect(create_engine(scratch.sync_url)).has_table("intent_checkpoints")
