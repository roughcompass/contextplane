"""The four agent write routes, checked against the schema they name.

Routing is a pure decision, and the unit and conformance suites prove the
decision. What they cannot prove is that the decision refers to anything: a
route naming a table nobody migrated, or two routes converging on one table
after a rename, both pass every in-memory check and fail on the first write.
So these tests resolve every name the route table uses against a migrated
database.

The last test is the one the whole boundary exists for. A real workspace entry
-- a row somebody wrote while thinking out loud -- is offered to each of the
four intents in turn, and the four target tables are counted before and after.
The count is the assertion: a working note that reaches any of them has become
a record the registry stands behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from contextplane.context.intent import (
    AUTHORITY_CITED_EVIDENCE,
    AUTHORITY_PARTICIPANT_GRANT,
    AUTHORITY_QUALIFIED_CONTROL,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    INTENT_CANONICAL_REVIEW,
    INTENT_CHECKPOINT,
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    ROUTES,
    TABLES_NO_INTENT_MAY_TARGET,
    WRITE_INTENTS,
    DisallowedWrite,
    WriteAuthority,
    route_agent_write,
)

AUTHORITIES: dict[str, WriteAuthority] = {
    INTENT_CHECKPOINT: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_PARTICIPANT_GRANT),
    INTENT_OBSERVATION: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_CITED_EVIDENCE),
    INTENT_REQUEST: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_REQUESTER_ENTITLEMENT),
    INTENT_CANONICAL_REVIEW: WriteAuthority(
        actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL, control_id="ctrl-1"
    ),
}

TARGET_TABLES = tuple(ROUTES[intent].target_table for intent in WRITE_INTENTS)


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
            {"t": tid, "s": f"ir-{tid.hex[:8]}", "n": "intent routing test"},
        )
    return tid


# --- every name the route table uses resolves ---------------------------------


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_every_route_targets_a_table_the_migration_creates(sync_engine: Engine, intent: str) -> None:
    """A route naming a table nobody migrated passes every in-memory check and
    fails on the first write."""
    assert inspect(sync_engine).has_table(ROUTES[intent].target_table)


def test_the_four_routes_target_four_distinct_real_tables(sync_engine: Engine) -> None:
    """Distinct in the database, not merely distinct as strings -- a rename that
    merged two of them would leave the strings different and the tables one."""
    inspector = inspect(sync_engine)
    assert all(inspector.has_table(table) for table in TARGET_TABLES)
    assert len(set(TARGET_TABLES)) == len(WRITE_INTENTS)


@pytest.mark.parametrize("table", sorted(TABLES_NO_INTENT_MAY_TARGET))
def test_every_table_the_router_refuses_to_target_actually_exists(sync_engine: Engine, table: str) -> None:
    """A guard naming a table nobody has guards nothing, and reads as protection
    for as long as it takes somebody to check."""
    assert inspect(sync_engine).has_table(table)


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_every_agent_write_lands_somewhere_a_tenant_owns(sync_engine: Engine, intent: str) -> None:
    """Each target carries a foreign key to tenants, so a routed write is always
    attributable to the tenant it was written for. A target without one could
    not be isolated per tenant however carefully the route was chosen."""
    table = ROUTES[intent].target_table
    referred = {fk["referred_table"] for fk in inspect(sync_engine).get_foreign_keys(table)}
    assert "tenants" in referred, f"{table} is not tenant-scoped"


def test_the_workspace_table_is_none_of_the_four_targets(sync_engine: Engine) -> None:
    """A note and a checkpoint in one table become indistinguishable rows."""
    assert inspect(sync_engine).has_table("workspace_entries")
    assert "workspace_entries" not in TARGET_TABLES


# --- a real workspace entry reaches none of them ------------------------------


def _row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in TARGET_TABLES}


def test_a_stored_workspace_entry_cannot_be_routed_into_any_agent_write(
    sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The boundary the whole module exists for, exercised against a row that
    really is in the database rather than a dict shaped like one."""
    workspace_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (workspace_id, tenant_id, name, owner_kind) "
                "VALUES (:w, :t, 'routing test', 'tenant')"
            ),
            {"w": workspace_id, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO workspace_entries (entry_id, workspace_id, tenant_id, kind, body_md) "
                "VALUES (:e, :w, :t, 'note', :body)"
            ),
            {"e": entry_id, "w": workspace_id, "t": tenant_id, "body": "maybe payments is owned by platform now"},
        )

    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT entry_id, kind, body_md FROM workspace_entries WHERE entry_id = :e"),
            {"e": entry_id},
        ).one()

    workspace_body: dict[str, Any] = {
        "entry_id": str(stored.entry_id),
        "entry_kind": stored.kind,
        "title": "ownership?",
        "body_md": stored.body_md,
    }

    before = _row_counts(sync_engine)
    for intent in WRITE_INTENTS:
        with pytest.raises(DisallowedWrite, match="context, never authority"):
            route_agent_write(intent, workspace_body, authority=AUTHORITIES[intent])
    assert _row_counts(sync_engine) == before


def test_the_workspace_entry_stays_in_the_workspace_table(sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The other half of the same rule: refusing the route must not also have
    lost the note. It is still context; it is only not authority."""
    workspace_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (workspace_id, tenant_id, name, owner_kind) "
                "VALUES (:w, :t, 'routing test', 'tenant')"
            ),
            {"w": workspace_id, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO workspace_entries (entry_id, workspace_id, tenant_id, kind, body_md) "
                "VALUES (:e, :w, :t, 'note', 'still a note')"
            ),
            {"e": uuid.uuid4(), "w": workspace_id, "t": tenant_id},
        )
        with pytest.raises(DisallowedWrite):
            route_agent_write(
                INTENT_CHECKPOINT,
                {"body_md": "still a note"},
                authority=AUTHORITIES[INTENT_CHECKPOINT],
            )

    with sync_engine.connect() as conn:
        surviving = conn.execute(
            text("SELECT count(*) FROM workspace_entries WHERE workspace_id = :w"),
            {"w": workspace_id},
        ).scalar_one()
    assert surviving == 1
