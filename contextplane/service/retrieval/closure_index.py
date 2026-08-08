"""The write side of the closure index — one outbox row per edge mutation.

`closure_cache` has three parts and they live in three places on purpose:

- this module enqueues, inside the caller's edge-write transaction;
- ``graph_closure_cache`` reads, cache-first, with a CTE fallback;
- the closure-refresh worker drains the outbox and recomputes the cache.

The producer belongs beside the read path it keeps warm rather than inside the
worker that drains it. It runs in the caller's transaction on the request path
and never in the worker process, so filing it with the drain would put a
request-path function in a module whose whole subject is a background loop --
and would make the catalog service, which is the only caller, import the
worker layer to write one row. This mirrors ``embedding_index``, which is the
same producer/drain split for the embedding outbox.
"""

from __future__ import annotations

import datetime
import logging
import uuid

import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


async def enqueue_closure_refresh(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID,
    now: datetime.datetime,
) -> None:
    """Insert one ``closure_outbox`` row in the caller's active transaction.

    Wrapped in a SAVEPOINT so that if the ``closure_outbox`` table is absent
    (e.g., before migration 0008 is applied) the outer transaction is not
    poisoned.  Once the migration is applied, the insert succeeds atomically
    with the edge write.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO closure_outbox "
                    "(outbox_id, tenant_id, edge_id, enqueued_at, attempts) "
                    "VALUES (gen_random_uuid(), :tid, :eid, :now, 0)"
                ),
                {"tid": tenant_id, "eid": edge_id, "now": now},
            )
    # A missing table surfaces as ProgrammingError (matches the same
    # pre-migration guard in service/catalog/facts.py's own outbox enqueue).
    # Anything else -- a real connection or constraint failure -- should
    # propagate rather than be read as "the migration hasn't run yet".
    except sqlalchemy.exc.ProgrammingError:
        _log.debug(
            "closure_outbox not present yet (migration 0008 creates it); "
            "skipping closure_refresh enqueue for edge_id=%s",
            edge_id,
        )


__all__ = ["enqueue_closure_refresh"]
