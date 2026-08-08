"""External-reference tables: what the migration builds, and what it refuses.

The rules that matter here are the ones a service can forget. Normalization and
collision scope are enforced by constraints rather than left to the write path,
because a row that arrives by any other route — a backfill, a repair script, a
later service nobody has written yet — must land in the same shape or the
uniqueness that makes references converge stops meaning anything.

The parity test follows the task-memory precedent: a column in the migration but
not the ORM is invisible to service code, and one in the ORM but not the
database fails at query time rather than at import.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess  # noqa: S404 - alembic's CLI is the interface under test; driving it in-process would not prove the command works
import sys
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.context.schemas.trust import ExternalReferenceV1

_MODELS = (ContextExternalReference, ContextReferenceBinding)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    tid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n) ON CONFLICT DO NOTHING"),
            {"t": tid, "s": f"xr-{tid.hex[:8]}", "n": "external reference test"},
        )
    return tid


def _reference(
    conn: object,
    tenant: uuid.UUID,
    *,
    source_system: str = "github",
    source_namespace: str = "roughcompass/contextplane",
    kind: str = "pull_request",
    external_id: str = "412",
    revision: str | None = None,
    collision_key: str | None = None,
) -> uuid.UUID:
    """Insert a reference, keying it the way the service would."""
    rid = uuid.uuid4()
    key = (
        collision_key
        or ExternalReferenceV1(
            source_system=source_system,
            source_namespace=source_namespace,
            kind=kind,
            external_id=external_id,
            classification="internal",
            external_authority="platform-team",
        ).collision_key()
    )
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO context_external_references
                (reference_id, tenant_id, source_system, source_namespace, kind, external_id,
                 classification, external_authority, revision, collision_key)
            VALUES (:rid, :tid, :sys, :ns, :kind, :eid, 'internal', 'platform-team', :rev, :key)
            """
        ),
        {
            "rid": rid,
            "tid": tenant,
            "sys": source_system,
            "ns": source_namespace,
            "kind": kind,
            "eid": external_id,
            "rev": revision,
            "key": key,
        },
    )
    return rid


def _bind(
    conn: object, tenant: uuid.UUID, reference: uuid.UUID, subject: uuid.UUID, kind: str = "task_checkpoint"
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO context_reference_bindings (binding_id, tenant_id, reference_id, subject_type, subject_id)
            VALUES (:bid, :tid, :rid, :st, :sid)
            """
        ),
        {"bid": uuid.uuid4(), "tid": tenant, "rid": reference, "st": kind, "sid": subject},
    )


# --- fresh install ------------------------------------------------------------


@pytest.mark.parametrize("table", ["context_external_references", "context_reference_bindings"])
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


@pytest.mark.parametrize(
    ("table", "index"),
    [
        ("context_external_references", "uq_external_reference_collision"),
        ("context_external_references", "ix_external_reference_lookup"),
        ("context_reference_bindings", "uq_reference_binding"),
        ("context_reference_bindings", "ix_reference_binding_subject"),
        ("context_reference_bindings", "ix_reference_binding_reference"),
    ],
)
def test_the_join_paths_have_their_indexes(sync_engine: Engine, table: str, index: str) -> None:
    """Both directions are indexed on purpose. The unique index cannot serve
    "what cites this reference" because reference_id is its last column."""
    with sync_engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})
        }
    assert index in names, f"missing {index}; have {sorted(names)}"


# --- one row per external thing ------------------------------------------------


def test_one_reference_per_collision_key_per_tenant(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Two producers naming one issue must converge on one row, or a reader
    counting distinct sources over-counts every mention."""
    with sync_engine.begin() as conn:
        _reference(conn, tenant_id, external_id="collide-1")
    with pytest.raises(Exception, match="uq_external_reference_collision"), sync_engine.begin() as conn:
        _reference(conn, tenant_id, external_id="collide-1")


def test_two_tenants_may_name_the_same_external_thing(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Uniqueness is per tenant. A shared key across tenants would make one
    tenant's citation of a public issue block another's."""
    other = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": other, "s": f"xr2-{other.hex[:8]}", "n": "second tenant"},
        )
        mine = _reference(conn, tenant_id, external_id="shared-1")
        theirs = _reference(conn, other, external_id="shared-1")

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT reference_id, collision_key FROM context_external_references WHERE reference_id IN (:a, :b)"),
            {"a": mine, "b": theirs},
        ).all()
    assert {row[0] for row in rows} == {mine, theirs}
    # Same external thing, so the same key -- separated only by tenant.
    assert len({row[1] for row in rows}) == 1


def test_two_revisions_of_one_document_are_one_reference(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Revision is outside the collision scope: scoping by it would make every
    edit look like a new source."""
    first = ExternalReferenceV1(
        source_system="confluence",
        source_namespace="platform",
        kind="page",
        external_id="rev-doc",
        classification="internal",
        external_authority="platform-team",
        revision="v1",
    )
    second = dataclasses.replace(first, revision="v2")
    assert first.collision_key() == second.collision_key()

    with sync_engine.begin() as conn:
        _reference(
            conn,
            tenant_id,
            source_system="confluence",
            source_namespace="platform",
            kind="page",
            external_id="rev-doc",
            revision="v1",
        )
    with pytest.raises(Exception, match="uq_external_reference_collision"), sync_engine.begin() as conn:
        _reference(
            conn,
            tenant_id,
            source_system="confluence",
            source_namespace="platform",
            kind="page",
            external_id="rev-doc",
            revision="v2",
        )


def test_the_same_id_under_two_kinds_is_two_references(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Kind is in the collision scope: issue 412 and pull request 412 in one
    repository are different things, and merging them resolves both to one."""
    with sync_engine.begin() as conn:
        issue = _reference(conn, tenant_id, kind="issue", external_id="412-kinds")
        pull = _reference(conn, tenant_id, kind="pull_request", external_id="412-kinds")

    with sync_engine.connect() as conn:
        keys = {
            row[0]
            for row in conn.execute(
                text("SELECT collision_key FROM context_external_references WHERE reference_id IN (:a, :b)"),
                {"a": issue, "b": pull},
            )
        }
    assert len(keys) == 2, "kind is in the collision scope, so these must not converge"


# --- normalization is enforced, not assumed ------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("source_system", "GitHub"), ("source_namespace", "RoughCompass/ContextPlane"), ("kind", "Pull_Request")],
)
def test_an_unfolded_row_is_refused(sync_engine: Engine, tenant_id: uuid.UUID, field: str, value: str) -> None:
    """A row written around the service would carry an unfolded spelling and
    never collide with the folded one, which is the convergence quietly
    failing rather than erroring."""
    with pytest.raises(Exception, match="ck_external_reference_normalized"), sync_engine.begin() as conn:
        _reference(conn, tenant_id, **{field: value}, external_id=f"unfolded-{field}")  # type: ignore[arg-type]


def test_an_untrimmed_external_id_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The id keeps its case but not its whitespace: ' 412' and '412' are the
    same thing to every system that issued them."""
    with pytest.raises(Exception, match="ck_external_reference_normalized"), sync_engine.begin() as conn:
        _reference(conn, tenant_id, external_id="  padded  ")


@pytest.mark.parametrize("field", ["source_system", "source_namespace", "kind", "external_id"])
def test_an_empty_scope_part_is_refused(sync_engine: Engine, tenant_id: uuid.UUID, field: str) -> None:
    """A reference missing part of its collision scope collides with everything
    else missing the same part.

    The collision key is supplied rather than derived, because the schema
    refuses an empty scope part before any SQL runs -- and what this test is
    for is the row that arrives by some other route, where the database is the
    only thing left to refuse it.
    """
    with pytest.raises(Exception, match="ck_external_reference_scope_present"), sync_engine.begin() as conn:
        _reference(conn, tenant_id, collision_key=uuid.uuid4().hex, **{field: ""})  # type: ignore[arg-type]


def test_an_unknown_classification_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A classification nobody declared is one no policy covers."""
    with pytest.raises(Exception, match="ck_external_reference_classification"), sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO context_external_references
                    (reference_id, tenant_id, source_system, source_namespace, kind, external_id,
                     classification, external_authority, collision_key)
                VALUES (:rid, :tid, 'github', 'ns', 'issue', 'cls-1', 'top-secret', 'authority', :key)
                """
            ),
            {"rid": uuid.uuid4(), "tid": tenant_id, "key": uuid.uuid4().hex},
        )


# --- bindings ------------------------------------------------------------------


def test_a_subject_cites_one_reference_once(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """Twice is a duplicate that reads as two independent sources supporting
    one claim -- the same rule the checkpoint contract enforces in memory."""
    subject = uuid.uuid4()
    with sync_engine.begin() as conn:
        reference = _reference(conn, tenant_id, external_id="bind-once")
        _bind(conn, tenant_id, reference, subject)
    with pytest.raises(Exception, match="uq_reference_binding"), sync_engine.begin() as conn:
        _bind(conn, tenant_id, reference, subject)


def test_two_subjects_may_cite_one_reference(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with sync_engine.begin() as conn:
        reference = _reference(conn, tenant_id, external_id="bind-shared")
        _bind(conn, tenant_id, reference, uuid.uuid4())
        _bind(conn, tenant_id, reference, uuid.uuid4())

    with sync_engine.connect() as conn:
        bound = conn.execute(
            text("SELECT count(*) FROM context_reference_bindings WHERE reference_id = :r"), {"r": reference}
        ).scalar_one()
    assert bound == 2


def test_an_unknown_subject_type_is_refused(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The set is closed so a typo cannot create a binding nobody queries for."""
    with pytest.raises(Exception, match="ck_reference_binding_subject_type"), sync_engine.begin() as conn:
        reference = _reference(conn, tenant_id, external_id="bind-typo")
        _bind(conn, tenant_id, reference, uuid.uuid4(), kind="task_checkpiont")


def test_deleting_a_reference_removes_its_bindings(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """A binding has no meaning without the thing it points at, and an orphan
    would let a join return fewer rows than the subject actually cited."""
    subject = uuid.uuid4()
    with sync_engine.begin() as conn:
        reference = _reference(conn, tenant_id, external_id="cascade-1")
        _bind(conn, tenant_id, reference, subject)
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM context_external_references WHERE reference_id = :r"), {"r": reference})
    with sync_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM context_reference_bindings WHERE reference_id = :r"), {"r": reference}
        ).scalar_one()
    assert remaining == 0


def test_a_binding_needs_a_reference_that_exists(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    with pytest.raises(Exception, match="foreign key|violates"), sync_engine.begin() as conn:
        _bind(conn, tenant_id, uuid.uuid4(), uuid.uuid4())


# --- downgrade ------------------------------------------------------------------


def test_the_migration_downgrades_and_upgrades_again(pg_container: str) -> None:
    """Run against a throwaway database on the same server, for the same reason
    the task-memory suite does: downgrading the shared one would drop tables out
    from under every other integration module in the session."""
    scratch = f"xr_downgrade_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_sync_url(pg_container), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))

        scratch_url = pg_container.rsplit("/", 1)[0] + "/" + scratch
        env = {**os.environ, "DATABASE_URL": scratch_url}
        run = lambda *args: subprocess.run(  # noqa: E731
            [sys.executable, "-m", "alembic", *args],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        up = run("upgrade", "head")
        assert up.returncode == 0, f"upgrade head failed: {up.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_external_references")

        down = run("downgrade", "0030_task_memory")
        assert down.returncode == 0, f"downgrade failed: {down.stderr[-2000:]}"

        after = inspect(create_engine(_sync_url(scratch_url)))
        for table in ("context_reference_bindings", "context_external_references"):
            assert not after.has_table(table), f"{table} survived the downgrade"
        # The predecessor link is intact: downgrading this revision must not
        # take the one below it with it.
        assert after.has_table("task_checkpoints"), "the downgrade reached past its own revision"

        again = run("upgrade", "head")
        assert again.returncode == 0, f"re-upgrade failed: {again.stderr[-2000:]}"
        assert inspect(create_engine(_sync_url(scratch_url))).has_table("context_reference_bindings")
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": scratch},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        admin.dispose()
