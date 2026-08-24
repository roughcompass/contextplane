"""The context-resolve REST surface.

    POST /v1/context/resolve          → ContextEnvelopeResponse
    POST /v1/context/instruction-sets → InstructionSetResponse

The first is the phase's headline contract: exactly five blocks, the quality
that describes them, and the id of the receipt that recorded them. The second is
the one-time half of the instruction channel -- a caller submits the content of
its instruction set once per distinct set, and every later resolve carries only
the digest this returns.

This router adapts and does not decide. It turns a request body into the
resolver's arguments and a `ResolvedContext` into a response body. Which items
a block contains, what trust each carries, whether the envelope is complete,
degraded or blocked, and what the stored receipt looks like are all decided
before control returns here -- by the arms, the assembler and the receipt
service. The MCP tool answers the identical question, and a rule enforced in an
adapter is a rule the other adapter will eventually enforce differently.

**`blocked` is a 200, not a 5xx.** An envelope whose canonical arm failed is a
complete, well-formed, correct answer to the question "what context is available
right now", and the answer is "not enough to rely on". Returning 503 would tell
a caller the service is broken when the service worked and the corpus did not,
and it would throw away the three other blocks and the receipt along with it.
The distinction lives in `state` and `quality`, which is where a caller can
branch on it. This is what the Contract means by empty, degraded, failed and
blocked remaining distinct across the transport boundary.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import map_catalog_error
from contextplane.api.schemas.context import (
    ContextEnvelopeResponse,
    ContextResolveRequest,
    InstructionSetRequest,
    InstructionSetResponse,
)
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1", tags=["context"])

# Resolving context is a read. Every arm applies its own authorization beneath
# this -- the workspace arm resolves participation per task, the visibility
# chokepoint governs entity reads -- so tenant role here is the outer gate and
# never the only one.
_resolve_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN, ROLE_AUDITOR])


@router.post("/context/resolve", response_model=ContextEnvelopeResponse)
async def resolve_context(
    body: ContextResolveRequest,
    ctx: Annotated[TenantContext, Depends(_resolve_required)],
    container: Annotated[Services, Depends(services)],
) -> ContextEnvelopeResponse:
    """Resolve one context request into the block envelope.

    The clock is read once, here, and passed down. Every arm evaluates "active
    now" against that one moment, so a request cannot authorize on one side of a
    grant's expiry and read on the other -- and the receipt records the same
    instant the answer was resolved at.
    """
    moment = container.clock.now()
    try:
        resolved = await container.context_resolver.resolve(
            ctx,
            query=body.query,
            moment=moment,
            arc_receipt_id=body.arc_receipt_id,
            subject_entity_id=body.subject_entity_id,
            intent_ids=tuple(body.intent_ids),
            workspace_term=body.workspace_term,
            workspace_reference=body.workspace_reference.to_contract() if body.workspace_reference else None,
            lifecycle_references=tuple(ref.to_contract() for ref in body.lifecycle_references),
            instruction_digest=body.instruction_digest,
            limit=body.limit,
            max_age_s=body.max_age_s,
        )
    except ValidationError as exc:
        # A malformed external reference is the realistic case: the frozen
        # contract refuses it rather than repairing it, and that refusal is the
        # caller's to fix, so it must not read as a server fault.
        raise map_catalog_error(exc) from exc

    return ContextEnvelopeResponse.of(
        resolved.envelope,
        receipt_id=resolved.receipt_id,
        arc_block_note=resolved.arc_block_note,
        instruction_disposition=str(resolved.instruction_disposition),
        instruction_block_note=resolved.instruction_block_note,
    )


@router.post("/context/instruction-sets", response_model=InstructionSetResponse)
async def declare_instruction_set(
    body: InstructionSetRequest,
    ctx: Annotated[TenantContext, Depends(_resolve_required)],
    container: Annotated[Services, Depends(services)],
) -> InstructionSetResponse:
    """Submit the content of one instruction set, once, and get back its digest.

    Idempotent, and idempotent without an `Idempotency-Key`: the digest *is* the
    content, so a repeat submission of the same set is the same row and there is
    no key a caller could get wrong. That is why this is not a create in the
    sense the rest of the surface means -- it registers content under a name
    derived from the content.

    Returns the digest rather than taking one. A caller that sent both would be
    asserting a hash of its own bytes, and the service would have to either trust
    it or recompute it; recomputing makes the sent value decorative, and trusting
    it lets a caller file one instruction set under another's name.
    """
    try:
        digest = await container.instruction_channel.submit(ctx, content=body.content, now=container.clock.now())
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return InstructionSetResponse(digest=digest)
