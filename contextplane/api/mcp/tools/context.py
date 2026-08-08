"""The context-resolve MCP tool: the same surface the REST router publishes.

One tool, `registry_resolve_context`, over the same `ContextResolver` the router
calls. Same arms, same assembler, same receipt write, same four blocks. Neither
transport implements the surface -- both adapt one service -- because the moment
they diverge the divergence is silent: an agent calling over MCP would receive an
answer a REST caller could not, and nothing would report that as a fault.

**The JSON shape mirrors `ContextEnvelopeResponse` field for field.** A
cross-transport parity check compares the two by field name, so a field named
differently here is a parity failure rather than a stylistic choice. Blocks are
emitted in `BLOCK_NAMES` order for the same reason the router walks that tuple.

**A blocked envelope is a successful tool call.** An envelope whose canonical arm
failed is a correct answer to "what context is available", and the answer is "not
enough to rely on". Raising `ToolError` would discard the other three blocks and
the receipt id, and would tell an agent the tool is broken when the corpus was.
Agents branch on `state` and `quality`, exactly as REST callers do.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context as mcp_context
from contextplane.context.schemas.envelope import BLOCK_NAMES
from contextplane.exceptions import ValidationError
from contextplane.types import Clock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

    from contextplane.context.resolve import ContextResolver, ResolvedContext
    from contextplane.context.schemas.envelope import ContextItemV1

#: Mirrors `MAX_ARM_LIMIT` in the REST schema. Duplicated as a literal rather
#: than imported so the tool's declared bound is visible in its own signature to
#: an agent reading the schema; the parity test pins the two together.
MAX_ARM_LIMIT = 200


def _resolver() -> ContextResolver:
    """The resolver off the app's typed container, at call time.

    Not a constructor argument to `create_contextplane_mcp_server`: that factory
    runs while the router table is being mounted, before the container exists,
    so anything bound there would be a second instance -- and a second receipt
    writer means two clocks stamping one table.
    """
    app = mcp_context._request_app.get()
    resolver = getattr(mcp_context._services(app), "context_resolver", None)
    if resolver is None:
        raise ToolError("context resolution is not configured on this deployment")
    return cast("ContextResolver", resolver)


def _item_json(item: ContextItemV1) -> dict[str, Any]:
    return {
        "receipt_item_id": {
            "value": item.receipt_item_id.value(),
            "block": item.receipt_item_id.block,
            "source": item.receipt_item_id.source,
            "item_key": item.receipt_item_id.item_key,
        },
        "payload": dict(item.payload),
        "trust": None
        if item.trust is None
        else {
            "trust": item.trust.trust,
            "source": item.trust.source,
            "assertion_kind": item.trust.assertion_kind,
            "authority": item.trust.authority,
            "freshness": item.trust.freshness.isoformat() if item.trust.freshness else None,
            "mutability": item.trust.mutability,
            "attribution": item.trust.attribution,
            "classification": item.trust.classification,
        },
    }


def _envelope_json(resolved: ResolvedContext) -> dict[str, Any]:
    envelope = resolved.envelope
    return {
        "state": envelope.state,
        "blocks": [
            {
                "name": name,
                "state": envelope.block(name).state,
                "items": [_item_json(item) for item in envelope.block(name).items],
                "reason": envelope.block(name).reason,
            }
            for name in BLOCK_NAMES
        ],
        "quality": {
            "degraded_blocks": list(envelope.quality.degraded_blocks),
            "reasons": list(envelope.quality.reasons),
            "cacheable": envelope.quality.cacheable,
        },
        "receipt_id": str(resolved.receipt_id),
        "arc_block_note": resolved.arc_block_note,
    }


async def registry_resolve_context(
    query: str,
    arc_receipt_id: str | None = None,
    subject_entity_id: str | None = None,
    task_ids: list[str] | None = None,
    workspace_term: str | None = None,
    limit: int = 25,
    max_age_s: float | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Resolve one context request into the fixed four-block envelope.

    Args:
        query: What you are asking for.
        arc_receipt_id: An attested ARC resolution to serve. Omit it and the ARC
            block comes back `empty` rather than failed, with `arc_block_note`
            saying so -- ARC context is served from an attested decision, never
            selected here.
        subject_entity_id: Optional entity the canonical arm should centre on.
        task_ids: Tasks whose workspace material may be recalled. Each is subject
            to your own participation; tasks you are not in contribute nothing
            and are not reported.
        workspace_term: Lexical term for the workspace arm.
        limit: Per-arm bound, 1..200.
        max_age_s: Treat arm results older than this as stale. Omit to accept any
            age.

    Returns:
        JSON object with `state`, four `blocks` in fixed order, `quality`,
        `receipt_id`, and `arc_block_note`. A `blocked` state is a normal
        response, not an error: read `quality.reasons` for what failed.
    """
    if limit < 1 or limit > MAX_ARM_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_ARM_LIMIT}")
    if max_age_s is not None and max_age_s <= 0:
        raise ToolError("max_age_s must be positive when given")

    ctx = await mcp_context._resolve_tenant(session_factory, clock)
    try:
        resolved = await _resolver().resolve(
            ctx,
            query=query,
            moment=clock.now(),
            arc_receipt_id=uuid.UUID(arc_receipt_id) if arc_receipt_id else None,
            subject_entity_id=uuid.UUID(subject_entity_id) if subject_entity_id else None,
            task_ids=tuple(uuid.UUID(value) for value in (task_ids or ())),
            workspace_term=workspace_term,
            limit=limit,
            max_age_s=max_age_s,
        )
    except ValueError as exc:
        # A malformed UUID from an agent is a caller error, and saying which
        # argument is wrong is safe -- it reveals nothing about the tenant.
        raise ToolError(f"invalid identifier: {exc}") from exc
    except ValidationError as exc:
        raise ToolError(str(exc)) from exc

    return json.dumps(_envelope_json(resolved))


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tool onto ``mcp_server``.

    Only what tenant resolution needs is bound. The resolver comes off the
    container at call time -- see `_resolver`.
    """
    deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    mcp_server.tool()(mcp_context._bind_tool(registry_resolve_context, **deps))


__all__ = ["MAX_ARM_LIMIT", "register", "registry_resolve_context"]
