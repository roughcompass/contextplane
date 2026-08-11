"""Receipt and resume MCP tools: the same surface the REST router publishes.

One pair of reads and one resume, over the same services, so an agent asking
over MCP gets the answer a REST caller would. Both transports compute the resume
status through the same helper rather than each deciding for itself -- the point
of the field is that they agree.

Services come off the app's typed container at call time, not bound at
construction: the MCP server is built while the router table is being mounted,
before the container exists.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.api.routers.receipts import compose_resume_response
from contextplane.context.resume import ResumeRequest
from contextplane.types import Clock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

    from contextplane.context.models_receipt import ContextReceipt
    from contextplane.context.receipts import ContextReceiptService
    from contextplane.context.references import ReceiptReferenceIndex


def _service(name: str, *, label: str) -> object:
    app = context._request_app.get()
    service = getattr(context._services(app), name, None)
    if service is None:
        raise ToolError(f"{label} is not configured on this deployment")
    return service


def _receipts() -> ContextReceiptService:
    return cast("ContextReceiptService", _service("context_receipts", label="context receipts"))


def _index() -> ReceiptReferenceIndex:
    return cast("ReceiptReferenceIndex", _service("context_reference_index", label="the receipt reference index"))


def _receipt_json(row: ContextReceipt) -> dict[str, Any]:
    return {
        "receipt_id": str(row.receipt_id),
        "task_id": str(row.task_id) if row.task_id else None,
        "state": row.state,
        "cacheable": row.cacheable,
        "resolved_at": row.resolved_at.isoformat(),
        "requested_by": row.requested_by,
        "request_digest": row.request_digest,
    }


async def find_receipts_by_reference(
    source_system: str,
    source_namespace: str,
    kind: str,
    external_id: str,
    limit: int = 50,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Every context resolution that cited one piece of external work.

    Args:
        source_system: e.g. `github`.
        source_namespace: e.g. `acme/app`.
        kind: e.g. `commit`, `pull_request`, `build`, `deployment`.
        external_id: the id within that system.
        limit: most receipts to return, newest first.

    Returns:
        JSON object with a `receipts` array.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    found = await _index().receipts_for_reference(
        ctx,
        source_system=source_system,
        source_namespace=source_namespace,
        kind=kind,
        external_id=external_id,
        limit=limit,
    )
    return json.dumps({"receipts": [_receipt_json(row) for row in found]})


async def get_context_receipt(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """One context resolution's receipt, by id.

    This is the id `registry_resolve_context` returns, so it is how an agent
    reads back what its own resolution was given. Without it a caller holds an
    identifier for evidence it cannot open.

    Missing and forbidden are the same answer, by construction: the tenant
    predicate is inside the SELECT, so a receipt belonging to another tenant
    returns nothing rather than being found and refused. A distinguishable
    refusal would confirm the id exists.

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object with `receipt_id`, `task_id`, `state`, `cacheable`,
        `resolved_at`, `requested_by` and `request_digest`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    row = await _receipts().get(ctx, receipt_id=uuid.UUID(receipt_id))
    if row is None:
        raise ToolError(f"no receipt {receipt_id}")
    return json.dumps(_receipt_json(row))


async def get_receipt_references(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """What one resolution claimed to be about. The read an auditor makes.

    Pairs with `find_receipts_by_reference`, which goes the other way. Both
    directions are needed: one answers "which resolutions cited this commit",
    this one answers "what was this resolution about".

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object with a `references` array carrying source_system,
        source_namespace, kind, external_id and classification.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    found = await _index().references_for_receipt(ctx, receipt_id=uuid.UUID(receipt_id))
    return json.dumps(
        {
            "references": [
                {
                    "source_system": row.source_system,
                    "source_namespace": row.source_namespace,
                    "kind": row.kind,
                    "external_id": row.external_id,
                    "classification": row.classification,
                }
                for row in found
            ]
        }
    )


async def get_receipt_exclusions(
    receipt_id: str,
    block: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """What one resolution found and deliberately did not return.

    The answer to "was there more than this". An empty list means nothing was
    withheld; it does not mean nothing was checked.

    Args:
        receipt_id: UUID of the receipt.
        block: optional block name to narrow to.

    Returns:
        JSON object with an `exclusions` array of block, item_key and reason.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    found = await _receipts().exclusions_for(ctx, receipt_id=uuid.UUID(receipt_id), block=block)
    return json.dumps({"exclusions": [{"block": r.block, "item_key": r.item_key, "reason": r.reason} for r in found]})


async def resume_context(
    references: list[list[str]],
    checkpoint_bound: int | None = None,
    receipt_bound: int | None = None,
    reference_bound: int | None = None,
    feedback_bound: int | None = None,
    learning_bound: int | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Pick up work named by external references, within bounds.

    `status` is one of `resumed`, `empty` or `ambiguous`, and they are three
    different instructions: carry on, start fresh, or disambiguate because the
    references name more than one task. Never returns a transcript.

    Args:
        references: `[system, namespace, kind, external_id]` tuples.
        checkpoint_bound: most checkpoints to look back over.
        receipt_bound: most prior receipts to name.
        reference_bound: most external references to echo back.
        feedback_bound: most feedback annotations from the last receipt.
        learning_bound: most reviewed claims newer than the last receipt.

    Returns:
        JSON object with `status`, the head, the checkpoint window, open
        questions, unresolved feedback, newer learning, the next action, and
        which arms were truncated.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    bounds = {
        name: value
        for name, value in (
            ("checkpoint_bound", checkpoint_bound),
            ("receipt_bound", receipt_bound),
            ("reference_bound", reference_bound),
            ("feedback_bound", feedback_bound),
            ("learning_bound", learning_bound),
        )
        if value is not None
    }
    # Arity is checked here because the REST body declares a four-tuple and
    # pydantic enforces it, while the request dataclass does not. Without this
    # the two transports would disagree about what a valid reference is, and a
    # three-element reference would reach a query that expects four.
    malformed = [ref for ref in references if len(ref) != 4]
    if malformed:
        return json.dumps(
            {
                "error": (
                    "each reference must be [source_system, source_namespace, kind, external_id]; "
                    f"got {malformed[0]!r}"
                )
            }
        )
    try:
        request = ResumeRequest(
            references=tuple((ref[0], ref[1], ref[2], ref[3]) for ref in references),
            **bounds,
        )
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})

    container = context._services(context._request_app.get())
    if container is None:
        raise ToolError("context resume is not configured on this deployment")
    try:
        response = await compose_resume_response(container=container, ctx=ctx, request=request)
    except PermissionError as exc:
        raise ToolError(str(exc)) from exc
    return response.model_dump_json()


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``."""
    deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    mcp_server.tool()(context._bind_tool(find_receipts_by_reference, **deps))
    mcp_server.tool()(context._bind_tool(get_context_receipt, **deps))
    mcp_server.tool()(context._bind_tool(get_receipt_references, **deps))
    mcp_server.tool()(context._bind_tool(get_receipt_exclusions, **deps))
    mcp_server.tool()(context._bind_tool(resume_context, **deps))


__all__ = [
    "find_receipts_by_reference",
    "get_context_receipt",
    "get_receipt_exclusions",
    "get_receipt_references",
    "register",
    "resume_context",
]
