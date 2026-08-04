"""Unit tests for registry/service/retrieval/listing.py.

All DB interactions are mocked — no Postgres or Docker required.

Coverage:
  - list_capabilities keyset pagination: cursor encoding/decoding, single-page
    no-cursor, multi-page cursor emission, cursor chaining, empty result.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from registry.config import Settings
from registry.service.retrieval import RetrievalService
from registry.types import TemporalFilter, TenantContext
from tests.helpers.clock import FakeClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


def _ctx(tenant_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id or _TENANT_ID,
        actor_id=_ACTOR_ID,
        roles=["reader"],
    )


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
    )


def _stub_embedder() -> MagicMock:
    emb = MagicMock()
    emb.model_version = "stub-v1"
    emb.encode = MagicMock(side_effect=lambda texts: np.ones((len(texts), 4), dtype=np.float32))
    return emb


def _tf() -> TemporalFilter:
    return TemporalFilter(as_of=None)


def _make_list_session_factory(rows: list[dict]) -> MagicMock:
    """Build a session factory that returns ``rows`` from the entities SELECT."""

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM entities" in sql:
            result.mappings.return_value.all.return_value = rows
        else:
            result.mappings.return_value.all.return_value = []
        return result

    session = MagicMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory


def _list_entity_row(
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    created_at: datetime.datetime | None = None,
) -> dict:
    return {
        "entity_id": entity_id or uuid.uuid4(),
        "tenant_id": tenant_id or _TENANT_ID,
        "entity_type": "capability",
        "name": "test-cap",
        "external_id": None,
        "is_active": True,
        "created_at": created_at or _NOW,
    }


def _make_list_service(rows: list[dict]) -> RetrievalService:
    factory = _make_list_session_factory(rows)
    return RetrievalService(
        session_factory=factory,
        clock=FakeClock(_NOW),
        embedder=_stub_embedder(),
        settings=_settings(),
    )


# ---------------------------------------------------------------------------
# list_capabilities — keyset pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_capabilities_no_cursor_single_page() -> None:
    """Single page result: next_cursor is None."""
    rows = [_list_entity_row() for _ in range(5)]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=20,
        temporal_filter=_tf(),
    )

    assert len(items) == 5
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_capabilities_emits_cursor_when_more_rows() -> None:
    """page_size+1 rows returned → trim to page_size and emit next_cursor."""
    page_size = 3
    rows = [_list_entity_row() for _ in range(page_size + 1)]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert len(items) == page_size
    assert next_cursor is not None
    assert "ts" in next_cursor and "id" in next_cursor


@pytest.mark.asyncio
async def test_list_capabilities_cursor_points_to_last_item() -> None:
    """next_cursor encodes the last returned item's (created_at, entity_id)."""
    last_id = uuid.uuid4()
    last_ts = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    page_size = 2
    rows = [
        _list_entity_row(),
        _list_entity_row(entity_id=last_id, created_at=last_ts),
        _list_entity_row(),  # extra row signals has_more
    ]
    svc = _make_list_service(rows)

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert next_cursor is not None
    assert next_cursor["id"] == str(last_id)
    assert datetime.datetime.fromisoformat(next_cursor["ts"]) == last_ts


@pytest.mark.asyncio
async def test_list_capabilities_empty_result() -> None:
    """Empty DB result returns empty items list and no cursor."""
    svc = _make_list_service([])

    items, next_cursor = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=20,
        temporal_filter=_tf(),
    )

    assert items == []
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_capabilities_cursor_payload_round_trips() -> None:
    """The cursor payload produced by list_capabilities survives encode/decode."""
    from registry.api.cursor import decode_cursor, encode_cursor

    page_size = 1
    rows = [_list_entity_row(), _list_entity_row()]  # second row triggers has_more
    svc = _make_list_service(rows)

    _, next_cursor_payload = await svc.list_capabilities(
        _ctx(),
        lifecycle=None,
        entity_type=None,
        cursor={},
        page_size=page_size,
        temporal_filter=_tf(),
    )

    assert next_cursor_payload is not None
    token = encode_cursor(next_cursor_payload)
    decoded = decode_cursor(token)
    assert decoded == next_cursor_payload
