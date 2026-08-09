"""Capability listing — keyset-paginated entity enumeration.

list_capabilities:
  - Paginated entity list; keyset (cursor) pagination on (created_at DESC, entity_id DESC).
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import text

from contextplane.service.retrieval._query_primitives import _RetrievalState
from contextplane.types import EntityRef, TemporalFilter, TenantContext


class _ListingMethods(_RetrievalState):
    """``RetrievalService.list_capabilities`` — the whole of this concern."""

    async def list_capabilities(
        self,
        ctx: TenantContext,
        lifecycle: str | None,
        entity_type: str | None,
        cursor: dict[str, Any],
        page_size: int,
        temporal_filter: TemporalFilter,
    ) -> tuple[list[EntityRef], dict[str, Any] | None]:
        """Paginated entity list filtered by tenant (and optionally lifecycle/entity_type).

        Uses keyset pagination on (created_at DESC, entity_id DESC) so performance
        is constant at any depth — no OFFSET scan. Pass the opaque ``cursor`` dict
        decoded from ``pagination.py``; an empty dict starts from the first page.

        Returns a (items, next_cursor_payload) tuple. ``next_cursor_payload`` is
        None when no further pages exist; otherwise it is a dict ready to pass to
        ``encode_cursor``.

        Entities do not have bi-temporal columns (they use `is_active` as their
        lifecycle flag). Temporal filtering is applied only to the attributes
        sub-query used for the lifecycle filter, not to the entity row itself.
        """
        now = self._clock.now()

        filters = ["e.tenant_id = :tid AND e.is_active = TRUE"]
        params: dict[str, Any] = {"tid": ctx.tenant_id}

        if entity_type is not None:
            filters.append("e.entity_type = :entity_type")
            params["entity_type"] = entity_type

        if lifecycle is not None:
            # lifecycle is stored as an attribute with key='lifecycle'
            filters.append(
                """EXISTS (
                    SELECT 1 FROM attributes a
                    WHERE a.entity_id = e.entity_id
                      AND a.tenant_id = :tid
                      AND a.key = 'lifecycle'
                      AND a.value = to_jsonb(:lifecycle::text)
                      AND a.t_invalidated_at IS NULL
                      AND (a.t_valid_to IS NULL OR a.t_valid_to > :lc_now)
                )"""
            )
            params["lifecycle"] = lifecycle
            params["lc_now"] = now

        # Keyset predicate: skip rows at-or-after the cursor position.
        # The sort is (created_at DESC, entity_id DESC) so "before in the cursor
        # order" means a strictly smaller (ts, id) tuple.
        if cursor:
            filters.append("(e.created_at, e.entity_id) < (:cursor_ts, :cursor_id)")

            params["cursor_ts"] = datetime.datetime.fromisoformat(cursor["ts"])
            params["cursor_id"] = cursor["id"]

        where_clause = " AND ".join(filters)

        sql = text(
            f"""
            SELECT e.entity_id, e.tenant_id, e.entity_type, e.name,
                   e.external_id, e.is_active, e.created_at
            FROM entities e
            WHERE {where_clause}
            ORDER BY e.created_at DESC, e.entity_id DESC
            LIMIT :limit
            """
        )
        # Fetch one extra row to detect whether a next page exists.
        params["limit"] = page_size + 1

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]

        items = [
            EntityRef(
                entity_id=row["entity_id"],
                tenant_id=row["tenant_id"],
                entity_type=row["entity_type"],
                name=row["name"],
                external_id=row["external_id"],
                is_active=row["is_active"],
                created_at=row["created_at"],
            )
            for row in page_rows
            if row["tenant_id"] == ctx.tenant_id
        ]

        next_cursor_payload: dict[str, Any] | None = None
        if has_more and items:
            last = items[-1]
            next_cursor_payload = {
                "ts": last.created_at.isoformat(),
                "id": str(last.entity_id),
            }

        return items, next_cursor_payload
