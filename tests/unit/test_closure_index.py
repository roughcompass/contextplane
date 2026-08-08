"""Unit tests for the closure-outbox producer.

All DB interaction is mocked at the ``session.execute`` / ``begin_nested``
boundary — no Docker or real Postgres is required.

Coverage:
- the enqueue writes exactly one ``closure_outbox`` INSERT, bound by name, and
  does it inside a SAVEPOINT so it is atomic with the caller's edge write;
- a missing ``closure_outbox`` table (the pre-migration state) is swallowed, so
  an installation that has not run the migration yet still writes edges;
- any other database failure propagates, because reading a real connection or
  constraint error as "the migration hasn't run yet" would drop outbox rows
  silently and leave the closure cache permanently stale.
"""

from __future__ import annotations

import datetime
import uuid
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy.exc

from contextplane.service.retrieval.closure_index import enqueue_closure_refresh

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_EDGE = uuid.uuid4()


class _FakeSavepoint:
    """Async context manager standing in for ``session.begin_nested()``.

    Records whether it was entered, so a test can assert the INSERT happened
    inside the SAVEPOINT rather than merely that both occurred.
    """

    def __init__(self, raise_on_exit: BaseException | None = None) -> None:
        self.entered = False
        self.exited = False
        self._raise_on_exit = raise_on_exit

    async def __aenter__(self) -> _FakeSavepoint:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.exited = True
        if self._raise_on_exit is not None:
            raise self._raise_on_exit
        return False


def _make_session(savepoint: _FakeSavepoint, execute: AsyncMock | None = None) -> Any:
    session = MagicMock()
    session.begin_nested = MagicMock(return_value=savepoint)
    session.execute = execute or AsyncMock()
    return session


def _programming_error() -> sqlalchemy.exc.ProgrammingError:
    return sqlalchemy.exc.ProgrammingError("INSERT INTO closure_outbox", {}, Exception("no such table"))


@pytest.mark.asyncio
async def test_enqueue_inserts_one_row_inside_a_savepoint() -> None:
    savepoint = _FakeSavepoint()
    execute = AsyncMock()
    session = _make_session(savepoint, execute)

    await enqueue_closure_refresh(session, _TENANT, _EDGE, _NOW)

    assert savepoint.entered, "the insert must run inside a SAVEPOINT"
    assert execute.await_count == 1
    statement, params = execute.await_args.args
    assert "INSERT INTO closure_outbox" in str(statement)
    # Bound by name rather than interpolated — the ids reach the driver as
    # parameters, never as SQL text.
    assert params == {"tid": _TENANT, "eid": _EDGE, "now": _NOW}


@pytest.mark.asyncio
async def test_missing_outbox_table_does_not_fail_the_edge_write() -> None:
    """Before the migration that creates ``closure_outbox`` has run, the enqueue
    is a no-op rather than an error that would roll back the edge itself."""
    savepoint = _FakeSavepoint()
    session = _make_session(savepoint, AsyncMock(side_effect=_programming_error()))

    await enqueue_closure_refresh(session, _TENANT, _EDGE, _NOW)

    assert savepoint.entered


@pytest.mark.asyncio
async def test_other_database_errors_propagate() -> None:
    """A connection or constraint failure is not the pre-migration state, and
    swallowing it would leave the closure cache stale with nothing to say so."""
    savepoint = _FakeSavepoint()
    boom = sqlalchemy.exc.OperationalError("INSERT INTO closure_outbox", {}, Exception("connection reset"))
    session = _make_session(savepoint, AsyncMock(side_effect=boom))

    with pytest.raises(sqlalchemy.exc.OperationalError):
        await enqueue_closure_refresh(session, _TENANT, _EDGE, _NOW)
