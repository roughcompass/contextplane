"""The ARC ORM and the live schema must agree, column for column.

Migration `0023_arc_phase1` is the authoritative DDL and `registry/arc/models.py`
is a hand-written mirror of it. Nothing but a test stops the two drifting, and
the drift that matters is silent: a column added to the migration but not the ORM
is invisible to service code, and a column declared in the ORM but absent from
the database fails at query time rather than at import.

This is also, per ARC-T01's findings, the first place in this codebase where ORM
and schema are asserted to agree for a whole subsystem. Several existing tables
are raw DDL with no ORM class at all, so there is no prior art to copy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from registry.arc.models import ARC_MODELS

# Columns the migration adds that the ORM deliberately does not mirror. Empty on
# purpose: an entry here is a decision that needs a comment, not a convenience.
_ORM_EXEMPT: dict[str, set[str]] = {}


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Engine:
    """Synchronous engine — `inspect()` reflection has no async equivalent."""
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


def test_every_arc_table_exists_in_the_database(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    present = set(inspector.get_table_names())
    for model in ARC_MODELS:
        assert (
            model.__tablename__ in present
        ), f"{model.__name__} maps {model.__tablename__}, which the migration did not create"


def test_no_arc_table_lacks_a_mapped_class(sync_engine: Engine) -> None:
    """The reverse direction: a new table without an ORM class is invisible."""
    inspector = inspect(sync_engine)
    in_db = {t for t in inspector.get_table_names() if t.startswith("arc_")}
    mapped = {m.__tablename__ for m in ARC_MODELS}
    assert in_db - mapped == set(), f"arc_ tables with no mapped class: {sorted(in_db - mapped)}"
    assert mapped - in_db == set(), f"mapped classes with no table: {sorted(mapped - in_db)}"


def test_columns_match_in_both_directions(sync_engine: Engine) -> None:
    inspector = inspect(sync_engine)
    problems: list[str] = []

    for model in ARC_MODELS:
        table = model.__tablename__
        db_columns = {c["name"] for c in inspector.get_columns(table)}
        orm_columns = {c.name for c in inspect(model).columns}
        exempt = _ORM_EXEMPT.get(table, set())

        missing_in_orm = db_columns - orm_columns - exempt
        missing_in_db = orm_columns - db_columns
        if missing_in_orm:
            problems.append(f"{table}: in database but not in ORM: {sorted(missing_in_orm)}")
        if missing_in_db:
            problems.append(f"{table}: in ORM but not in database: {sorted(missing_in_db)}")

    assert not problems, "ORM/schema drift:\n" + "\n".join(problems)


def test_nullability_matches(sync_engine: Engine) -> None:
    """Nullability disagreements are the subtle half of drift.

    A column the ORM thinks is optional but the database requires fails only on
    the insert path that omits it — often in production rather than in a test.
    """
    inspector = inspect(sync_engine)
    problems: list[str] = []

    for model in ARC_MODELS:
        table = model.__tablename__
        db_nullable = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
        for column in inspect(model).columns:
            if column.name not in db_nullable:
                continue
            # A primary key is NOT NULL in the database regardless of how the
            # ORM spells it, so comparing those adds noise without signal.
            if column.primary_key:
                continue
            if column.nullable != db_nullable[column.name]:
                problems.append(
                    f"{table}.{column.name}: ORM nullable={column.nullable}, "
                    f"database nullable={db_nullable[column.name]}"
                )

    assert not problems, "nullability drift:\n" + "\n".join(problems)


def test_primary_keys_match(sync_engine: Engine) -> None:
    """Composite keys are where a hand-written mirror goes wrong quietly."""
    inspector = inspect(sync_engine)
    problems: list[str] = []

    for model in ARC_MODELS:
        table = model.__tablename__
        db_pk = set(inspector.get_pk_constraint(table)["constrained_columns"])
        orm_pk = {c.name for c in inspect(model).primary_key}
        if db_pk != orm_pk:
            problems.append(f"{table}: database PK {sorted(db_pk)} vs ORM PK {sorted(orm_pk)}")

    assert not problems, "primary-key drift:\n" + "\n".join(problems)


def test_reserved_deployment_tenant_exists_and_is_disabled(pg_container: str) -> None:
    """The sentinel must be present and unusable as a real tenant.

    `disabled_at` is the guard the JIT materialization path checks; `is_active`
    is declared but gates nothing, so asserting only the latter would prove
    nothing.
    """
    engine = create_engine(_sync_url(pg_container))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT slug, is_active, provider, disabled_at IS NOT NULL AS disabled "
                    "FROM tenants WHERE tenant_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff'"
                )
            ).one()
            assert row.slug == "_deployment"
            assert row.disabled is True, "sentinel must be disabled via disabled_at"
            assert row.provider == "system"
            assert row.is_active is False

            # And the seed tenant it once collided with is untouched.
            default_slug = conn.execute(
                text("SELECT slug FROM tenants " "WHERE tenant_id = '00000000-0000-0000-0000-000000000000'")
            ).scalar_one()
            assert default_slug == "default"
    finally:
        engine.dispose()
