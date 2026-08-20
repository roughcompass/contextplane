"""Dropping the superseded corroboration knobs, and keeping the live bounds.

Two dead columns come off `memory_confidence_policy` in `0056`. The interesting
part is not the drop -- it is `ck_memory_confidence_bounds`, a table-level CHECK
that mentioned both dead columns and four live ones. PostgreSQL drops such a
constraint when any column it mentions is dropped, so the naive migration removes
two dead knobs and silently takes the bounds on `contradiction_penalty`,
`confirmed_confidence`, `confirmation_hold_days` and `decay_multiplier` with them.

That failure is invisible to a test that only checks the columns are gone, and
invisible in Python entirely: the constraint exists only in the database. So the
live half is asserted by writing a row the bounds must refuse, in both directions
of the migration.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from tests.helpers.migration_database import (
    MigrationDatabases,
    assert_alembic_ok,
    assert_at_head,
    migration_databases,
    migration_template,
)

__all__ = ["migration_databases", "migration_template"]

_BEFORE = "0055_legal_hold_ceiling_absolute"
_DROPPED = ("corroboration_headroom", "corroboration_scale")

#: One term from each of the four bounds the drop must not take with it, paired
#: with a value the constraint has to refuse. Every one of these would insert
#: cleanly if `ck_memory_confidence_bounds` had been allowed to fall away.
_OUT_OF_BOUNDS = (
    ("contradiction_penalty", "0.900"),
    ("confirmed_confidence", "0.500"),
    ("confirmation_hold_days", "9000"),
    ("decay_multiplier", "99.0"),
)


@pytest.fixture(scope="module")
def at_head(pg_container: str) -> Iterator[Engine]:
    """The shared session database, which is already at head.

    The forward direction needs no clone: it asserts what head looks like, and
    every row it writes is a fresh tenant of its own. Only the tests that
    downgrade need a database they are allowed to destroy.
    """
    engine = create_engine(pg_container.replace("postgresql+asyncpg://", "postgresql+psycopg2://"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def cycled(migration_databases: MigrationDatabases) -> Iterator[Engine]:
    """A throwaway clone taken from head back to `0055`.

    A clone rather than the session database, because downgrading that would
    drop schema out from under every other integration module in the run — a
    test proving the reverse works would break unrelated tests to do it.

    Through `head_clone` rather than `clone`, so the database is dropped even if
    the downgrade raises. The first version called `clone` and relied on the
    fixture's end-of-test sweep to catch it, which works and holds the database
    open for longer than it needs to be — and the sweep raises rather than logs
    when a drop fails, so a lingering clone under a loaded run turns into a
    teardown error attributed to whichever test happened to be last.
    """
    with migration_databases.head_clone("confidence policy knobs cycle") as database:
        assert_at_head(database)
        assert_alembic_ok(database.downgrade(_BEFORE), f"downgrade to {_BEFORE}")
        engine = create_engine(database.sync_url)
        try:
            yield engine
        finally:
            engine.dispose()


def _tenant(engine: Engine) -> uuid.UUID:
    tid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :n)"),
            {"t": tid, "s": f"conf-{tid.hex[:8]}", "n": "confidence policy migration"},
        )
    return tid


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("memory_confidence_policy")}


def _insert(engine: Engine, tenant: uuid.UUID, column: str, value: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO memory_confidence_policy (tenant_id, {column}) VALUES (:t, {value})"),
            {"t": tenant},
        )


# --- forward ------------------------------------------------------------------


@pytest.mark.parametrize("column", _DROPPED)
def test_the_superseded_knob_is_gone(at_head: Engine, column: str) -> None:
    """A column left behind after its consumer is removed still looks
    configurable: an operator sets it, gets no error, and gets no effect."""
    assert column not in _columns(at_head)


def test_the_columns_the_scorer_still_reads_survive(at_head: Engine) -> None:
    """The positive control. A migration that dropped the whole table would pass
    every assertion above."""
    assert {"contradiction_penalty", "confirmed_confidence", "decay_multiplier"} <= _columns(at_head)


@pytest.mark.parametrize(("column", "value"), _OUT_OF_BOUNDS)
def test_the_live_bounds_still_refuse_an_out_of_range_value(at_head: Engine, column: str, value: str) -> None:
    """The property the naive drop loses. PostgreSQL removes a table-level CHECK
    along with any column it mentions, so dropping the two dead columns without
    re-adding the constraint would leave these four unbounded."""
    tenant = _tenant(at_head)
    with pytest.raises(Exception, match="ck_memory_confidence_bounds"):
        _insert(at_head, tenant, column, value)


def test_a_row_inside_the_bounds_is_still_accepted(at_head: Engine) -> None:
    """The re-added constraint is a bar and not a wall — a check that refused
    everything would satisfy all four refusals above."""
    tenant = _tenant(at_head)
    _insert(at_head, tenant, "decay_multiplier", "1.50")
    with at_head.begin() as conn:
        stored = conn.execute(
            text("SELECT decay_multiplier FROM memory_confidence_policy WHERE tenant_id = :t"),
            {"t": tenant},
        ).scalar_one()
    assert float(stored) == 1.50


# --- reverse ------------------------------------------------------------------


@pytest.mark.parametrize("column", _DROPPED)
def test_the_downgrade_restores_the_knob(cycled: Engine, column: str) -> None:
    assert column in _columns(cycled)


def test_the_restored_knob_carries_the_baseline_default(cycled: Engine) -> None:
    """Restored without them, every surviving row would be NULL and the re-added
    CHECK would be unknown rather than true — passing, while leaving the next
    write to supply values for knobs nothing sets."""
    tenant = _tenant(cycled)
    with cycled.begin() as conn:
        conn.execute(text("INSERT INTO memory_confidence_policy (tenant_id) VALUES (:t)"), {"t": tenant})
        row = conn.execute(
            text(
                "SELECT corroboration_headroom, corroboration_scale "
                "FROM memory_confidence_policy WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).one()
    assert float(row[0]) == 0.600
    assert float(row[1]) == 2.00


def test_the_downgraded_bounds_constrain_the_restored_knobs_again(cycled: Engine) -> None:
    """The constraint has to come back in its original form, not merely come
    back: a reduced check left in place after the downgrade would admit a
    headroom the pre-0056 schema refused."""
    tenant = _tenant(cycled)
    with pytest.raises(Exception, match="ck_memory_confidence_bounds"):
        _insert(cycled, tenant, "corroboration_headroom", "0.950")


def test_the_downgraded_bounds_still_hold_the_live_columns(cycled: Engine) -> None:
    """Both halves of the original constraint, so a downgrade that restored only
    the corroboration terms is caught too."""
    tenant = _tenant(cycled)
    with pytest.raises(Exception, match="ck_memory_confidence_bounds"):
        _insert(cycled, tenant, "decay_multiplier", "99.0")


def test_re_upgrading_the_reversed_clone_reaches_head_again(
    migration_databases: MigrationDatabases,
) -> None:
    """Down and back up on one database. A downgrade that leaves the schema in a
    shape the upgrade cannot re-apply is only visible from this direction."""
    with migration_databases.head_clone("confidence policy knobs round trip") as database:
        assert_at_head(database)
        assert_alembic_ok(database.downgrade(_BEFORE), f"downgrade to {_BEFORE}")
        assert_alembic_ok(database.upgrade_head(), "re-upgrade to head")
        assert_at_head(database)

        engine = create_engine(database.sync_url)
        try:
            assert not (set(_DROPPED) & _columns(engine))
        finally:
            engine.dispose()
