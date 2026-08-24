"""Async engine + session factory.

PgBouncer in transaction mode does not support named prepared statements
across connections. asyncpg uses prepared statements by default, so we
disable its cache here. Removing the `prepared_statement_cache_size=0`
arg silently breaks queries under PgBouncer transaction mode.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from contextplane.config import Settings

#: How long a connection of ours may sit inside a transaction with nothing
#: happening before Postgres reclaims it.
#:
#: **This is a bound on a bug, not a tuning knob.** No request this service
#: serves opens a transaction and then waits thirty seconds for its own next
#: statement; a session in that state has been abandoned by whatever opened it.
#: The timeout applies only while a transaction is *idle* between statements, so
#: a long-running statement and a slow migration are both unaffected.
#:
#: The reason it is set at all is what an abandoned session costs. It keeps
#: every lock it took until its backend disconnects, and a backend nothing is
#: left to read from never disconnects. One `ACCESS SHARE` lock held that way is
#: enough to wedge a whole table: the next `ALTER`/`DROP` queues for
#: `ACCESS EXCLUSIVE` behind it, and because Postgres grants locks in order,
#: every ordinary read and write of that table then queues behind the `DROP`.
#: A test run met exactly that and spent twenty-five minutes making no progress
#: at all, with one orphaned `SELECT` at the head of the queue.
#:
#: Thirty seconds is long enough that nothing legitimate is interrupted and
#: short enough that an orphan is gone before the next thing needs the table.
IDLE_IN_TRANSACTION_TIMEOUT_MS: Final[str] = "30000"


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async SQLAlchemy engine bound to the configured database URL."""
    return create_async_engine(
        settings.database_url,
        connect_args={
            "prepared_statement_cache_size": 0,  # required for PgBouncer transaction mode
            # Set on the connection rather than on the server, so it travels
            # with this application's sessions and does not depend on how the
            # database somebody points us at happens to be configured.
            "server_settings": {"idle_in_transaction_session_timeout": IDLE_IN_TRANSACTION_TIMEOUT_MS},
        },
        pool_size=10,
        max_overflow=20,
    )


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to the given engine. No module-level singleton."""
    return async_sessionmaker(engine, expire_on_commit=False)
