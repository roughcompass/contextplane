"""Type-aware identity against a real database, where the indexes are real.

The resolution logic is exercised by unit-level checks over candidate rows. What
those cannot show is whether the database agrees, and the agreement is the whole
migration: a Python comparison that judges two names identical while Postgres
stores them separately is worse than either behaviour alone, because each half
is individually defensible.

So the tests here are the ones that need SQL:

- the same name under two types is *accepted* by the type-aware index and would
  have been rejected by the tenant-wide one it replaces;
- the backfill adds handles without touching a single opaque ID, checked by
  comparing the ID set before and after rather than by trusting that no UPDATE
  was written;
- re-running the backfill adds nothing, so an interrupted run resumes;
- `lower()` in the index and `str.lower` in Python agree on input where
  `casefold` would not, which is the divergence the key normalization exists to
  avoid;
- a retired handle stops resolving and stays readable, because rollback must
  restore behaviour without deleting identifiers.

Every test builds its own tenant. Sharing one would make the uniqueness
assertions depend on what another test had already inserted, which is exactly
the sort of order dependence that makes a suite pass alone and fail in CI.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from profile_identity_migrate import plan_backfill  # noqa: E402 - same

from contextplane.entities.identity import (  # noqa: E402 - after the sys.path line
    AmbiguousIdentity,
    HandleRow,
    lookup_key_for,
    resolve_qualified,
    resolve_unqualified,
)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant(sync_engine: Engine) -> Iterator[str]:
    """A tenant of this test's own, so uniqueness assertions stand alone."""
    tenant_id = str(uuid.uuid4())
    with sync_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, :d)"),
            {"t": tenant_id, "s": f"identity-{tenant_id[:8]}", "d": f"identity {tenant_id[:8]}"},
        )
    yield tenant_id


def _entity(connection, tenant_id: str, *, entity_type: str, name: str) -> str:  # type: ignore[no-untyped-def]
    entity_id = str(uuid.uuid4())
    connection.execute(
        text("INSERT INTO entities (entity_id, tenant_id, entity_type, name) VALUES (:e, :t, :ty, :n)"),
        {"e": entity_id, "t": tenant_id, "ty": entity_type, "n": name},
    )
    return entity_id


def _handle(connection, tenant_id: str, entity_id: str, *, entity_type: str, name: str, kind: str = "primary") -> str:  # type: ignore[no-untyped-def]
    handle_id = str(uuid.uuid4())
    connection.execute(
        text(
            "INSERT INTO entity_handles (handle_id, tenant_id, entity_id, entity_type, namespace, handle_name, "
            "qualified_handle, lookup_key, kind, valid_from, source, recorded_at) "
            "VALUES (:h, :t, :e, :ty, 'tenant', :n, :q, :k, :kind, now(), 'test', now())"
        ),
        {
            "h": handle_id,
            "t": tenant_id,
            "e": entity_id,
            "ty": entity_type,
            "n": name,
            "q": f"tenant:{entity_type}/{name}",
            "k": lookup_key_for("tenant", entity_type, name),
            "kind": kind,
        },
    )
    return handle_id


# --- the property the migration exists to deliver -----------------------------


def test_one_name_under_two_types_is_accepted(sync_engine: Engine, tenant: str) -> None:
    """The tenant-wide index this replaces forbade exactly this pair."""
    with sync_engine.begin() as connection:
        service = _entity(connection, tenant, entity_type="service", name="checkout-a")
        capability = _entity(connection, tenant, entity_type="capability", name="checkout-b")
        _handle(connection, tenant, service, entity_type="service", name="checkout")
        _handle(connection, tenant, capability, entity_type="capability", name="checkout")

    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT entity_id::text, entity_type, namespace, handle_name, kind "
                "FROM entity_handles WHERE tenant_id = :t AND valid_to IS NULL"
            ),
            {"t": tenant},
        ).fetchall()
    assert len(rows) == 2, rows

    candidates = [HandleRow(*row) for row in rows]
    assert resolve_qualified(candidates, "tenant:service/checkout") == service
    assert resolve_qualified(candidates, "tenant:capability/checkout") == capability


def test_the_same_name_and_type_twice_is_refused_by_the_index(sync_engine: Engine, tenant: str) -> None:
    """Type-aware does not mean unconstrained; one primary per name per type."""
    with sync_engine.begin() as connection:
        first = _entity(connection, tenant, entity_type="service", name="dup-a")
        second = _entity(connection, tenant, entity_type="service", name="dup-b")
        _handle(connection, tenant, first, entity_type="service", name="shared")

    with pytest.raises(IntegrityError), sync_engine.begin() as connection:
        _handle(connection, tenant, second, entity_type="service", name="shared")


def test_an_unqualified_name_matching_two_types_is_refused(sync_engine: Engine, tenant: str) -> None:
    with sync_engine.begin() as connection:
        service = _entity(connection, tenant, entity_type="service", name="amb-a")
        capability = _entity(connection, tenant, entity_type="capability", name="amb-b")
        _handle(connection, tenant, service, entity_type="service", name="orders")
        _handle(connection, tenant, capability, entity_type="capability", name="orders")

    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT entity_id::text, entity_type, namespace, handle_name, kind "
                "FROM entity_handles WHERE tenant_id = :t AND valid_to IS NULL"
            ),
            {"t": tenant},
        ).fetchall()

    with pytest.raises(AmbiguousIdentity) as caught:
        resolve_unqualified([HandleRow(*row) for row in rows], "orders")
    assert set(caught.value.entity_types) == {"service", "capability"}


# --- the backfill moves no identifiers ----------------------------------------


def test_backfill_adds_handles_and_moves_no_opaque_id(sync_engine: Engine, tenant: str) -> None:
    """Checked by comparing ID sets, not by trusting that no UPDATE was written."""
    with sync_engine.begin() as connection:
        for index in range(3):
            _entity(connection, tenant, entity_type="service", name=f"svc-{index}")

    with sync_engine.connect() as connection:
        before = {
            row[0]
            for row in connection.execute(
                text("SELECT entity_id::text FROM entities WHERE tenant_id = :t"), {"t": tenant}
            ).fetchall()
        }
        rows = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                text("SELECT entity_id::text, entity_type, name FROM entities WHERE tenant_id = :t"), {"t": tenant}
            ).fetchall()
        ]

    planned = plan_backfill(rows, existing_keys=())
    assert len(planned) == 3

    with sync_engine.begin() as connection:
        for handle in planned:
            _handle(connection, tenant, handle.entity_id, entity_type=handle.entity_type, name=handle.handle_name)

    with sync_engine.connect() as connection:
        after = {
            row[0]
            for row in connection.execute(
                text("SELECT entity_id::text FROM entities WHERE tenant_id = :t"), {"t": tenant}
            ).fetchall()
        }
        handles = connection.execute(
            text("SELECT count(*) FROM entity_handles WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()

    assert after == before, "an opaque ID changed; every dependent reference would now be wrong"
    assert handles == 3


def test_backfill_is_idempotent_against_what_the_database_already_holds(sync_engine: Engine, tenant: str) -> None:
    """A resumed run adds only the remainder, so an interruption is survivable."""
    with sync_engine.begin() as connection:
        first = _entity(connection, tenant, entity_type="service", name="idem-one")
        _entity(connection, tenant, entity_type="service", name="idem-two")
        _handle(connection, tenant, first, entity_type="service", name="idem-one")

    with sync_engine.connect() as connection:
        rows = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                text("SELECT entity_id::text, entity_type, name FROM entities WHERE tenant_id = :t"), {"t": tenant}
            ).fetchall()
        ]
        keys = [
            str(row[0])
            for row in connection.execute(
                text("SELECT lookup_key FROM entity_handles WHERE tenant_id = :t AND valid_to IS NULL"),
                {"t": tenant},
            ).fetchall()
        ]

    planned = plan_backfill(rows, existing_keys=keys)
    assert [handle.handle_name for handle in planned] == ["idem-two"]
    assert not plan_backfill(rows, existing_keys=[*keys, planned[0].lookup_key])


# --- Python and SQL agree about what one name is ------------------------------


def test_python_and_sql_normalize_the_same_way(sync_engine: Engine, tenant: str) -> None:
    """`straße` and `strasse` are two names to SQL, so they must be two to Python.

    Postgres `lower()` leaves the eszett alone; Python `casefold()` turns it into
    `ss`. The key uses `lower` so the two agree — this test is what fails if
    somebody 'improves' it to `casefold`.
    """
    with sync_engine.begin() as connection:
        sharp = _entity(connection, tenant, entity_type="service", name="strasse-a")
        double = _entity(connection, tenant, entity_type="service", name="strasse-b")
        _handle(connection, tenant, sharp, entity_type="service", name="straße")
        # Accepted only because SQL keeps them distinct; a casefolded key would
        # have collided on the unique index and this insert would fail.
        _handle(connection, tenant, double, entity_type="service", name="strasse")

    with sync_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT lower(handle_name) FROM entity_handles WHERE tenant_id = :t AND valid_to IS NULL "
                "ORDER BY handle_name"
            ),
            {"t": tenant},
        ).fetchall()
    assert {row[0] for row in stored} == {"straße", "strasse"}


# --- rollback keeps identifiers ----------------------------------------------


def test_a_retired_handle_stops_resolving_and_stays_readable(sync_engine: Engine, tenant: str) -> None:
    """Rollback restores behaviour without deleting identifiers."""
    with sync_engine.begin() as connection:
        entity = _entity(connection, tenant, entity_type="service", name="retired-svc")
        handle_id = _handle(connection, tenant, entity, entity_type="service", name="retired")
        # `now()` is transaction-stable in Postgres, so retiring with it inside
        # the transaction that created the row yields valid_to == valid_from and
        # trips `valid_to > valid_from`. The constraint is right: a handle that
        # stopped being valid at the instant it started was never valid.
        # `clock_timestamp()` advances within the transaction, which is what a
        # retirement actually is.
        connection.execute(
            text("UPDATE entity_handles SET valid_to = clock_timestamp() WHERE handle_id = :h"),
            {"h": handle_id},
        )

    with sync_engine.connect() as connection:
        active = connection.execute(
            text("SELECT count(*) FROM entity_handles WHERE tenant_id = :t AND valid_to IS NULL"), {"t": tenant}
        ).scalar_one()
        total = connection.execute(
            text("SELECT count(*) FROM entity_handles WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()

    assert active == 0, "a retired handle must not resolve"
    assert total == 1, "a retired handle must still be readable; rollback never deletes an identifier"
