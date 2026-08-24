"""An abandoned transaction must not be able to hold a lock forever.

E15b-T2. The "unexplained CI stalls" this entry was left open for have an
explanation, and it is not slowness. A session left idle inside a transaction
keeps every lock it took until its backend disconnects, and a backend nothing is
left to read from never disconnects. One orphaned `ACCESS SHARE` is enough to
stop a table: the next `DROP INDEX` queues for `ACCESS EXCLUSIVE` behind it, and
because Postgres grants locks in order, every later read and write of that table
then queues behind the `DROP`.

Observed directly rather than inferred. A local integration run sat at 0% CPU
for twenty-five minutes; `pg_stat_activity` had one backend `idle in
transaction` since the run's third second, a `DROP INDEX` blocked on a relation
lock three seconds behind it, and every later statement queued in arrival order.

Two settings, because they cover different connections and both are needed: the
application's own engine, and the whole local test cluster — the suite builds
dozens of engines with a bare `create_async_engine` that `storage/pg.py` never
sees.

What this does **not** claim: which task abandoned the transaction. Its last
statement was the reporting-obligation backlog observer, which the scheduler
fires once at startup, so a job cancelled during teardown before it could unwind
is the likely author — likely, not established. The bound is worth having either
way, because it is the difference between a leak that costs one connection and a
leak that stops everything.
"""

from __future__ import annotations

from typing import Any

import pytest

from contextplane.config import Settings
from contextplane.storage import pg
from scripts import pg_provider


@pytest.fixture
def recorded_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What `create_engine` hands SQLAlchemy, without connecting to anything.

    Asserted at the call rather than read back off the engine: SQLAlchemy keeps
    `connect_args` in a closure over the pool's creator, and a test that reached
    into it would be pinned to a version of SQLAlchemy rather than to this
    function's contract.
    """
    seen: dict[str, Any] = {}

    def _capture(url: str, **kwargs: Any) -> object:
        seen["url"] = url
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(pg, "create_async_engine", _capture)
    pg.create_engine(Settings(database_url="postgresql+asyncpg://u:p@localhost/db"))
    return seen


def test_the_application_engine_bounds_an_idle_transaction(recorded_kwargs: dict[str, Any]) -> None:
    """Through `server_settings`, so the bound travels with our connections
    rather than depending on how the database somebody points us at happens to
    be configured."""
    server_settings = recorded_kwargs["connect_args"]["server_settings"]
    assert server_settings["idle_in_transaction_session_timeout"] == pg.IDLE_IN_TRANSACTION_TIMEOUT_MS


def test_the_pgbouncer_workaround_survived_the_change(recorded_kwargs: dict[str, Any]) -> None:
    """`prepared_statement_cache_size=0` is load-bearing under PgBouncer
    transaction mode and shares a dict with the setting just added. Removing it
    breaks queries silently, which is exactly what an edit to its neighbour
    would do."""
    assert recorded_kwargs["connect_args"]["prepared_statement_cache_size"] == 0


def test_the_bound_is_long_enough_not_to_interrupt_anything_real() -> None:
    """The timeout applies only while a transaction sits *between* statements,
    so a long statement and a slow migration are both untouched. What it catches
    is a session nothing is going to speak to again."""
    assert int(pg.IDLE_IN_TRANSACTION_TIMEOUT_MS) >= 10_000


def test_the_test_cluster_bounds_an_idle_transaction_too() -> None:
    """The application engine is not enough on its own.

    The orphan that wedged the run came from the application's engine, but the
    suite builds dozens of its own with a bare `create_async_engine`, and a leak
    from any of them would wedge a table the same way. Set on the cluster, so it
    covers every connection to it regardless of who opened one.
    """
    assert "idle_in_transaction_session_timeout=30s" in pg_provider._SERVER_FLAGS


def test_the_cluster_flags_are_well_formed() -> None:
    """Every `-c` is followed by its setting. A stray one makes the server
    refuse to start, and that failure presents as a broken Docker image rather
    than as a typo in this tuple."""
    flags = pg_provider._SERVER_FLAGS
    assert len(flags) % 2 == 0
    assert all(flag == "-c" for flag in flags[::2])
    assert all("=" in setting for setting in flags[1::2])


def test_the_connection_cap_was_not_disturbed() -> None:
    """`max_connections=50` is the other measured guard on this cluster,
    deliberately just above the observed peak so a leak fails rather than is
    absorbed. Asserted here because it now shares a tuple with a setting added
    for a different leak, and the two are easy to confuse."""
    assert "max_connections=50" in pg_provider._SERVER_FLAGS
