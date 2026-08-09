"""Feedback over MCP: the agent-facing twin of ``api/routers/context_feedback.py``.

One tool, over the same service, so an agent reporting that an answer was wrong
gets the same answer a REST caller would — including the same refusals. Nothing
here re-derives a rule ``signals/feedback.py`` already enforces; this module
translates the service's typed exceptions into a ``ToolError`` a caller can act on
and turns the result into JSON.

**An agent is the caller most likely to be reporting on its own retrieved
context**, which is exactly the case the item-specific shape exists for: it read a
receipt, one item on it was stale, and saying so about that item is worth more
than saying it about the whole answer. The tool takes `receipt_id` and
`receipt_item_id` as plain strings and parses them here, because an MCP argument
that fails to parse should say which argument rather than surfacing a UUID error
from three layers down.

**Nothing is inferred from an outcome.** An agent that observed a failed build
reports that through signal ingestion; it is an observation about the world, not a
verdict on an answer this system served. This tool accepts no signal id so the two
cannot be joined into a rating nobody stated.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import CatalogError
from contextplane.signals.feedback import (
    FeedbackService,
    FeedbackSubmissionV1,
    feedback_json,
)
from contextplane.types import Clock

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from mcp.server.fastmcp import FastMCP


def _parse_uuid(name: str, raw: str | None) -> uuid.UUID | None:
    """Parse an optional id, naming the argument when it does not parse."""
    if raw is None or raw == "":
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ToolError(f"{name} must be a UUID: {exc}") from exc


async def record_context_feedback(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    kind: str,
    rating: str,
    reporter_id: str,
    reporter_type: str,
    idempotency_key: str,
    receipt_id: str | None = None,
    receipt_item_id: str | None = None,
    note: str | None = None,
    learning_eligible: bool = True,
) -> str:
    """Report feedback about a served answer, bound to exactly what it is about.

    Three shapes, and which one you use decides what may be learned from it:

    - `item_specific` — cites a receipt *and* an exact item on it. Use this when
      one retrieved item was the problem; it is the only shape that can become
      evidence about that item.
    - `receipt_level` — cites a receipt and must not cite an item. Use this when
      the answer as a whole was wrong and no single line explains why.
    - `diagnostic_observation` — cites neither, and is never learning-eligible.
      Use this to report something wrong without attributing it to any served
      item.

    The binding is checked before anything is stored: a receipt that is not yours
    and a receipt that does not exist answer identically, and an item that is not
    on the receipt you named is refused rather than filed against it.

    Resubmitting is safe. The same report under the same key finds the stored row
    and reports `replayed: true`. Reusing a key for *different* content is refused
    rather than overwriting the first report or filing a second beside it.

    Args:
        kind: One of `item_specific`, `receipt_level`, `diagnostic_observation`.
        rating: One of `relevant`, `irrelevant`, `missing`, `stale`, `incorrect`,
            `contradicted`, `unsafe`, `selected`, `ignored`, `succeeded`,
            `failed`, `rolled_back`, `needs_human_review`.
        reporter_id: Who is reporting. Must be you unless `reporter_type` is
            `external`.
        reporter_type: One of `human`, `agent`, `external`.
        idempotency_key: This submission's key. A replay carries it verbatim.
        receipt_id: The resolution this is about. Required for `item_specific`
            and `receipt_level`; forbidden for `diagnostic_observation`.
        receipt_item_id: The exact item on that receipt. Required for
            `item_specific`; forbidden for the other two.
        note: Free text. Minimized before the structured fields expire, so put
            anything that must survive into the structured fields.
        learning_eligible: Whether this may be used as learning or evaluation
            evidence. You may lower it; a diagnostic observation is always false.

    Returns:
        JSON with feedback_id, kind, rating, learning_eligible, receipt_id,
        receipt_item_id, content_digest, created_at, and replayed.
    """
    ctx = await context._resolve_tenant(session_factory, clock)

    submission = FeedbackSubmissionV1(
        kind=kind,
        rating=rating,
        reporter_id=reporter_id,
        reporter_type=reporter_type,
        idempotency_key=idempotency_key,
        receipt_id=_parse_uuid("receipt_id", receipt_id),
        receipt_item_id=receipt_item_id or None,
        note=note,
        learning_eligible=learning_eligible,
    )
    try:
        recorded = await FeedbackService(session_factory, clock=clock).record(ctx, submission)
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc

    payload: dict[str, Any] = feedback_json(recorded)
    return json.dumps(payload)


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tool onto ``mcp_server``."""
    mcp_server.tool()(context._bind_tool(record_context_feedback, session_factory=session_factory, clock=clock))


__all__ = ["record_context_feedback", "register"]
