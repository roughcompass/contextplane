"""Curator actions over staged claims, over MCP: the agent-facing twin of
``api/routers/memory_curation.py``.

Thirteen tools, one module -- a deliberate deviation from "one module per
tool" the way the rest of this package is grouped by domain (``catalog``,
``retrieval``, ``workspace``, ``memory``, ``notifications``, ``arc``): the
curation surface is one coordinated capability (queue, promotion review,
confirmation, history, capability requests, direct assertion), and splitting
it into thirteen one-function files would scatter one contract across
thirteen places that all have to stay in step with one REST router.

Every tool here mirrors its REST twin's semantics exactly -- same service
call, same exception set, same chokepoint wraps for the two lookups that are
routing checks rather than visibility filters (claim history's subject, and
a capability request's subject). Nothing here re-derives a rule the service
layer already enforces; a tool's own job is translating the service's typed
exceptions into a ``ToolError`` a caller can act on, not re-checking anything
the service would refuse on its own.

Every service this module needs (``claims``, ``curation_queue``, ``promotion``,
``confirmations``, ``claim_history``, ``visibility``, ``capability_requests``)
comes off the FastAPI app's typed service container at call time, the same
way ``context._claim_serving()``/``context._memory_service()`` already read
theirs -- these seven services are not threaded into
``create_contextplane_mcp_server`` as constructor arguments the way
``workspace_service`` is, so a per-call accessor (not a bound constructor
dependency) is how each tool reaches them.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Final, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import CatalogError, ConflictError, NotFoundError, ValidationError
from contextplane.extraction.containment import CandidateRefused
from contextplane.pagination import InvalidCursorError, decode_cursor, encode_cursor
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.memory.capability_requests import CapabilityRequestService
from contextplane.service.memory.claim_assertion import ClaimPiiBlocked, stage_claim_defended
from contextplane.service.memory.claim_authority import Evidence
from contextplane.service.memory.claim_history import ClaimHistoryService, ClaimVisibility
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confirmation import ConfirmationService
from contextplane.service.memory.curation_queue import CurationQueueService, QueueItem
from contextplane.service.memory.promotion import PromotionService, Proposal
from contextplane.types import Clock, JSONValue, TenantContext
from contextplane.usage.results import set_mcp_result_count

_DEFAULT_PAGE_SIZE: Final[int] = 100
_MAX_PAGE_SIZE: Final[int] = 500

# The evidence_kind CHECK constraint's own vocabulary. Validated here, not
# just left to the database, so a caller who sends an unknown kind gets a
# clean ToolError instead of a raw constraint-violation error surfacing from
# three calls deep in the write path.
_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "session_event",
        "document_revision",
        "commit",
        "work_item",
        "connector_run",
        "curator",
        "incident",
    }
)

_PROPOSAL_STATES: Final[frozenset[str]] = frozenset({"open", "accepted", "amended", "rejected"})

_CAPABILITY_REQUEST_STATUSES: Final[frozenset[str]] = frozenset(
    {"acknowledged", "accepted", "declined", "duplicate", "resolved"}
)

# `amended_value`'s "not supplied at all" marker. A caller-sent `null` and an
# omitted argument mean different things to `review_promotion_proposal`: an
# accept with no amendment must promote the claim's own proposed value, never
# a caller-shaped null. A plain `None` default cannot carry that distinction,
# so the default is this sentinel instead, compared by value (not identity,
# which nothing here can guarantee survives a JSON-RPC round trip) against
# whatever the caller actually sent.
_AMENDED_VALUE_UNSET: Final[str] = "\x00amended_value_unset\x00"


# ---------------------------------------------------------------------------
# Service accessors -- one per domain, reading off the app's typed container
# at call time. Mirrors `context._claim_serving()`/`context._memory_service()`:
# these seven services are not constructor arguments to
# `create_contextplane_mcp_server`, so each tool reaches its own off `app.state`
# through one of these rather than a bound dependency.
# ---------------------------------------------------------------------------


def _service(name: str, *, label: str) -> object:
    app = context._request_app.get()
    service = getattr(context._services(app), name, None)
    if service is None:
        raise ToolError(f"{label} is not configured on this deployment")
    return service


def _curation_queue() -> CurationQueueService:
    return cast(CurationQueueService, _service("curation_queue", label="the curation queue"))


def _claims() -> ClaimService:
    return cast(ClaimService, _service("claims", label="claim curation"))


def _promotion() -> PromotionService:
    return cast(PromotionService, _service("promotion", label="promotion review"))


def _confirmations() -> ConfirmationService:
    return cast(ConfirmationService, _service("confirmations", label="claim confirmation"))


def _claim_history() -> ClaimHistoryService:
    return cast(ClaimHistoryService, _service("claim_history", label="claim history"))


def _visibility() -> VisibilityService:
    return cast(VisibilityService, _service("visibility", label="visibility resolution"))


def _capability_requests() -> CapabilityRequestService:
    return cast(CapabilityRequestService, _service("capability_requests", label="capability requests"))


def _map_error(exc: Exception) -> ToolError:
    """`context._map_catalog_error` is annotated for `CatalogError` alone,
    but its isinstance ladder already handles `PermissionError` (Python's
    builtin, not a `CatalogError`) the same way the generic fallback branch
    does -- every call site here catches that alongside the typed exceptions
    a service actually raises, so this cast reflects what the function does
    at runtime, not a gap in what its signature promises.
    """
    return context._map_catalog_error(cast(CatalogError, exc))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _check_page_size(page_size: int) -> None:
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ToolError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")


def _decode_cursor_pair(
    cursor: str | None, *, created_at_key: str, id_key: str
) -> tuple[datetime.datetime, uuid.UUID] | None:
    """Decode an opaque list cursor into the keyset pair a service expects.

    There is no shared cursor helper on ``context`` yet, so this calls
    ``api/cursor.py`` directly -- the same opaque format the REST list
    routes already produce, so a cursor from one surface decodes cleanly on
    the other.
    """
    if cursor is None:
        return None
    try:
        payload = decode_cursor(cursor, strict=True)
        return (
            datetime.datetime.fromisoformat(payload[created_at_key]),
            uuid.UUID(payload[id_key]),
        )
    except (InvalidCursorError, KeyError, ValueError) as exc:
        raise ToolError("invalid cursor") from exc


def _encode_cursor_pair(created_at: datetime.datetime, id_value: uuid.UUID, *, created_at_key: str, id_key: str) -> str:
    return encode_cursor({created_at_key: created_at.isoformat(), id_key: str(id_value)})


def _parse_optional_datetime(value: str | None, *, field: str) -> datetime.datetime | None:
    if value is None:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"{field} must be an ISO-8601 datetime: {exc}") from exc


def _serialize_queue_item(item: QueueItem) -> dict[str, Any]:
    """`context._serialize` only walks dataclass fields; `available_actions`
    is a computed property, so it is added in afterward the same way the
    REST route's own view model includes it explicitly."""
    payload = cast(dict[str, Any], context._serialize(item))
    payload["available_actions"] = list(item.available_actions)
    return payload


def _serialize_proposal(proposal: Proposal) -> dict[str, Any]:
    """`high_impact` is a computed property (`bool(high_impact_reasons)`),
    not a dataclass field -- added in the same way `_serialize_queue_item`
    adds `available_actions`, so this tool's proposal shape matches the REST
    route's `ProposalResponse` field for field."""
    payload = cast(dict[str, Any], context._serialize(proposal))
    payload["high_impact"] = proposal.high_impact
    return payload


def _claim_visible(ctx: TenantContext, claim: ClaimVisibility) -> bool:
    """Whether *ctx*'s tenant may read one claim row.

    A claim's own visibility can be narrower than the subject it describes
    (an observer's private note about an otherwise public capability), so a
    claim-level check has to run in addition to a subject-level one.
    Duplicated here rather than imported from the REST surface on purpose:
    each independent enforcement site stays responsible for its own
    visibility decision, so a change to one cannot silently change the
    other without its own review and its own test.
    """
    if claim.owning_tenant_id == ctx.tenant_id:
        return True
    return claim.visibility == "public"


# ---------------------------------------------------------------------------
# Tool: assert_claim
# ---------------------------------------------------------------------------


async def assert_claim(
    subject_reference: str,
    predicate: str,
    value: JSONValue,
    evidence: list[dict[str, Any]],
    asserted_valid_from: str | None = None,
    asserted_valid_to: str | None = None,
    visibility: str | None = None,
    namespace: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Assert a claim directly, not through extraction: the agent-to-ingest path.

    Runs through ``claim_assertion.stage_claim_defended`` -- the one place
    both ingestion defenses are implemented -- before ``stage_claim`` ever
    sees the value: directive-containment over the value and every evidence
    excerpt, then a PII scan under the same field policy a model-generated
    claim value is scanned under. Neither check is re-implemented here; this
    tool calls the identical helper the REST route calls, so a refusal means
    the same thing on both surfaces.

    Never lands directly in the canonical graph: an unresolvable
    ``subject_reference`` still stages the claim ``unlinked`` rather than
    refusing the write, and only promotion -- reviewed separately, by a
    different actor -- can move a value onto the graph a search sees.

    Args:
        subject_reference: What the claim is about (slug, UUID, or external id).
        predicate: The relationship being asserted, e.g. `exposes_operation`.
        value: The asserted value. Scanned for directive content and PII when a string.
        evidence: At least one item: `{"kind": ..., "ref": ..., "excerpt": ...}`.
            `kind` is one of session_event, document_revision, commit,
            work_item, connector_run, curator, incident. `excerpt` is
            optional but is exactly as scanned as `value` when present.
        asserted_valid_from: Optional ISO-8601 datetime the fact took effect.
        asserted_valid_to: Optional ISO-8601 datetime the fact stopped holding.
        visibility: Optional `public`, `tenant-shared`, or `private`.
        namespace: Optional hierarchical namespace for retrieval scoping.

    Returns:
        JSON object for the staged claim: claim_id, subject_entity_id,
        predicate, value, status, visibility, owning_tenant_id,
        source_authority, is_contested.
    """
    ctx = await context._resolve_tenant(session_factory, clock)

    if not evidence:
        raise ToolError("evidence must include at least one item; an assertion nobody can check is not evidence")

    parsed_evidence: list[Evidence] = []
    for idx, item in enumerate(evidence):
        kind = item.get("kind")
        ref = item.get("ref")
        if kind not in _EVIDENCE_KINDS:
            raise ToolError(f"evidence[{idx}].kind must be one of {sorted(_EVIDENCE_KINDS)}")
        if not ref:
            raise ToolError(f"evidence[{idx}].ref is required")
        parsed_evidence.append(Evidence(kind=kind, ref=ref, excerpt=item.get("excerpt")))

    valid_from_dt = _parse_optional_datetime(asserted_valid_from, field="asserted_valid_from")
    valid_to_dt = _parse_optional_datetime(asserted_valid_to, field="asserted_valid_to")

    claims = _claims()
    try:
        staged = await stage_claim_defended(
            session_factory,
            claims,
            ctx,
            subject_reference=subject_reference,
            predicate=predicate,
            value=value,
            evidence=tuple(parsed_evidence),
            asserted_valid_from=valid_from_dt,
            asserted_valid_to=valid_to_dt,
            visibility=visibility,
            namespace=namespace,
        )
    except CandidateRefused as exc:
        # A clear, structured refusal -- not the generic ToolError(str(exc))
        # fallback, which would drop `trigger`. CandidateRefused is a
        # RegistryError, deliberately not a CatalogError, so it never reaches
        # `context._map_catalog_error`'s isinstance checks; it is handled
        # here instead, the same deliberate special-case the REST route
        # gives it.
        raise ToolError(
            json.dumps({"code": "containment_refused", "message": str(exc), "trigger": exc.trigger})
        ) from exc
    except ClaimPiiBlocked as exc:
        raise ToolError(
            json.dumps(
                {
                    "code": "pii_blocked",
                    "message": str(exc),
                    "matched_patterns": list(exc.matched_patterns),
                }
            )
        ) from exc
    except ValidationError as exc:
        raise _map_error(exc) from exc
    return json.dumps(context._serialize(staged))


# ---------------------------------------------------------------------------
# Tool: list_curation_queue
# ---------------------------------------------------------------------------


async def list_curation_queue(
    counts: bool = False,
    cursor: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Everything needing curator attention in the caller's tenant.

    Args:
        counts: When true, return the per-reason tally instead of the item
            list. A tally needs no cursor of its own.
        cursor: Opaque cursor from a previous response's `next_cursor`.
        page_size: Items per page (1-500, default 100). Ignored when `counts` is true.

    Returns:
        `{"counts": {...}}` when `counts` is true, else `{"items": [...],
        "next_cursor": str | null}`. Each item carries `available_actions`
        naming what a curator may do with it.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    queue = _curation_queue()
    if counts:
        tally = await queue.counts_for(ctx.tenant_id)
        return json.dumps({"counts": tally})

    _check_page_size(page_size)
    cursor_pair = _decode_cursor_pair(cursor, created_at_key="created_at", id_key="claim_id")
    items = await queue.items_for(ctx.tenant_id, cursor=cursor_pair, page_size=page_size)

    next_cursor: str | None = None
    if len(items) > page_size:
        items = items[:page_size]
        last = items[-1]
        next_cursor = _encode_cursor_pair(
            last.created_at, last.claim_id, created_at_key="created_at", id_key="claim_id"
        )

    set_mcp_result_count(len(items))
    return json.dumps({"items": [_serialize_queue_item(i) for i in items], "next_cursor": next_cursor})


# ---------------------------------------------------------------------------
# Tool: link_claim_subject
# ---------------------------------------------------------------------------


async def link_claim_subject(
    claim_id: str,
    subject_reference: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Give a subjectless (unlinked) claim a home.

    Curator-only, and the service is the one gate: role, tenancy, and the
    claim's current status are all asserted by `ClaimService.link_subject`
    itself, not re-checked here.

    Args:
        claim_id: UUID of the unlinked claim.
        subject_reference: The capability this claim is actually about.

    Returns:
        JSON object for the now-staged claim (same shape as `assert_claim`'s
        return value).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise ToolError("claim_id must be a UUID") from exc
    try:
        claim = await _claims().link_subject(ctx, claim_id=claim_uuid, subject_reference=subject_reference)
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise _map_error(exc) from exc
    return json.dumps(context._serialize(claim))


# ---------------------------------------------------------------------------
# Tool: discard_claim
# ---------------------------------------------------------------------------


async def discard_claim(
    claim_id: str,
    reason: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Refuse a claim outright: it never serves again.

    Works on a staged claim or one still unlinked -- a reference that will
    never resolve has this as its only way out of the queue.

    Args:
        claim_id: UUID of the claim to discard.
        reason: Why it is being discarded. Audited.

    Returns:
        `{"status": "discarded"}`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise ToolError("claim_id must be a UUID") from exc
    try:
        await _claims().discard(ctx, claim_id=claim_uuid, reason=reason)
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise _map_error(exc) from exc
    return json.dumps({"status": "discarded"})


# ---------------------------------------------------------------------------
# Tool: list_promotion_proposals
# ---------------------------------------------------------------------------


async def list_promotion_proposals(
    state: str = "open",
    cursor: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Proposals owned by the caller's tenant, oldest first.

    Args:
        state: One of open, accepted, amended, rejected. Defaults to `open`
            -- the review queue, not the full history.
        cursor: Opaque cursor from a previous response's `next_cursor`.
        page_size: Items per page (1-500, default 100).

    Returns:
        `{"items": [...], "next_cursor": str | null}`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if state not in _PROPOSAL_STATES:
        raise ToolError(f"state must be one of {sorted(_PROPOSAL_STATES)}")
    _check_page_size(page_size)

    cursor_pair = _decode_cursor_pair(cursor, created_at_key="created_at", id_key="proposal_id")
    proposals = await _promotion().proposals_for(ctx.tenant_id, state=state, cursor=cursor_pair, page_size=page_size)

    next_cursor: str | None = None
    if len(proposals) > page_size:
        proposals = proposals[:page_size]
        last = proposals[-1]
        if last.created_at is None:  # pragma: no cover - the read path always fills this
            raise ToolError("internal error: a listed proposal is missing created_at")
        next_cursor = _encode_cursor_pair(
            last.created_at, last.proposal_id, created_at_key="created_at", id_key="proposal_id"
        )

    set_mcp_result_count(len(proposals))
    return json.dumps({"items": [_serialize_proposal(p) for p in proposals], "next_cursor": next_cursor})


# ---------------------------------------------------------------------------
# Tool: review_promotion_proposal
# ---------------------------------------------------------------------------


async def review_promotion_proposal(
    proposal_id: str,
    state: str,
    amended_value: JSONValue = _AMENDED_VALUE_UNSET,
    reason: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Accept (optionally amending the value) or reject an open proposal.

    Authority is entirely the service's own gate:
    `PromotionService.accept`/`reject` both check owner tenant and
    `producer`/`admin` role. This tool resolves tenant context and nothing
    else.

    Args:
        proposal_id: UUID of the proposal.
        state: `accepted` or `rejected`.
        amended_value: Only valid when accepting. Omit entirely to promote
            the claim's own proposed value unchanged -- passing an explicit
            `null` is a different thing from omitting this argument, and
            only omitting it means "no amendment".
        reason: Only valid (and required) when rejecting.

    Returns:
        `{"proposal": {...}, "promotion_id": str | null}` -- `promotion_id`
        is set only when this call itself just accepted the proposal.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError as exc:
        raise ToolError("proposal_id must be a UUID") from exc
    if state not in ("accepted", "rejected"):
        raise ToolError("state must be 'accepted' or 'rejected'")

    amended_provided = amended_value != _AMENDED_VALUE_UNSET
    if state == "rejected" and amended_provided:
        raise _map_error(ValidationError("amended_value is only valid when accepting a proposal"))
    if state == "accepted" and reason is not None:
        raise _map_error(ValidationError("reason is only valid when rejecting a proposal"))

    promotion = _promotion()
    roles = frozenset(ctx.roles)
    promotion_id: uuid.UUID | None = None
    try:
        if state == "accepted":
            accept_kwargs: dict[str, Any] = {}
            if amended_provided:
                accept_kwargs["amended_value"] = amended_value
            promotion_id = await promotion.accept(
                proposal_uuid,
                actor_tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                roles=roles,
                **accept_kwargs,
            )
        else:
            if reason is None:
                raise ValidationError("rejecting a proposal requires a reason")
            await promotion.reject(
                proposal_uuid,
                actor_tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                roles=roles,
                reason=reason,
            )
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise _map_error(exc) from exc

    updated = await promotion.get_proposal(proposal_uuid)
    if updated is None:  # pragma: no cover - unreachable on the path that just decided it
        raise _map_error(NotFoundError("no such proposal"))
    return json.dumps(
        {
            "proposal": _serialize_proposal(updated),
            "promotion_id": str(promotion_id) if promotion_id is not None else None,
        }
    )


# ---------------------------------------------------------------------------
# Tool: reverse_promotion
# ---------------------------------------------------------------------------


async def reverse_promotion(
    promotion_id: str,
    reason: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Undo a promotion, restoring whatever the canonical graph said before it.

    Refuses (via a conflict error) when a later promotion has already built
    on the row this one created; that later one has to be reversed first.

    Args:
        promotion_id: UUID from the journal entry (`journal_for` on the REST
            surface, or the `promotion_id` a prior `review_promotion_proposal`
            call returned).
        reason: Why it is being reversed. Audited.

    Returns:
        `{"status": "reversed"}`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        promotion_uuid = uuid.UUID(promotion_id)
    except ValueError as exc:
        raise ToolError("promotion_id must be a UUID") from exc
    try:
        await _promotion().reverse(
            promotion_uuid,
            actor_tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            roles=frozenset(ctx.roles),
            reason=reason,
        )
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise _map_error(exc) from exc
    return json.dumps({"status": "reversed"})


# ---------------------------------------------------------------------------
# Tool: confirm_claim
# ---------------------------------------------------------------------------


async def confirm_claim(
    claim_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """A human puts their name to a claim, producing a new one that supersedes it.

    Refuses a service principal: the human tier comes from the
    authenticated actor's own kind, so a worker calling this tool gets the
    same refusal a direct call would raise.

    Args:
        claim_id: UUID of the claim being confirmed.

    Returns:
        JSON object: claim_id (the new, confirming claim), confirms_claim_id,
        source_authority, confidence, bucket, hold_until.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise ToolError("claim_id must be a UUID") from exc
    try:
        confirmation = await _confirmations().confirm(ctx, claim_id=claim_uuid)
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise _map_error(exc) from exc
    return json.dumps(context._serialize(confirmation))


# ---------------------------------------------------------------------------
# Tool: adjudicate_claim
# ---------------------------------------------------------------------------


async def adjudicate_claim(
    claim_id: str,
    verdict: str,
    observed_confidence: float,
    note: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Record whether a claim turned out to be correct.

    The only input a calibration fit is ever built from. `ConfirmationService.adjudicate`
    raises `ValidationError` for an unrecognized verdict or an out-of-range
    confidence -- caught here and translated the same way every other typed
    service error from this module is.

    Args:
        claim_id: UUID of the claim being judged.
        verdict: One of correct, incorrect, undecidable.
        observed_confidence: What the reviewer saw at judgment time, in [0, 1].
        note: Optional free-text note.

    Returns:
        `{"status": "recorded"}`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise ToolError("claim_id must be a UUID") from exc
    try:
        await _confirmations().adjudicate(
            ctx,
            claim_id=claim_uuid,
            verdict=verdict,
            observed_confidence=observed_confidence,
            note=note,
        )
    except (NotFoundError, ValidationError) as exc:
        raise _map_error(exc) from exc
    return json.dumps({"status": "recorded"})


# ---------------------------------------------------------------------------
# Tool: get_claim_history
# ---------------------------------------------------------------------------


async def get_claim_history(
    claim_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """The claim's full supersession/confirmation chain, oldest first.

    `ClaimHistoryService.chain_for` takes no tenant context by design, so
    this tool is the one place tenant enforcement happens for it -- the
    requested claim must exist, its own visibility must pass, and its
    subject must resolve as visible through the chokepoint; any of the three
    failing answers identically, so a claim id is never a cross-tenant
    existence oracle. Every entry the chain walk returns is filtered by the
    same claim-level check, because a chain can cross a supersession that
    narrowed visibility partway through.

    Args:
        claim_id: UUID of the claim to trace.

    Returns:
        `{"items": [...]}`, oldest first.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise ToolError("claim_id must be a UUID") from exc

    history = _claim_history()
    anchor_rows = await history.visibility_rows_for([claim_uuid])
    anchor = anchor_rows.get(claim_uuid)
    if anchor is None or not _claim_visible(ctx, anchor):
        raise _map_error(NotFoundError("no such claim"))
    if anchor.subject_entity_id is None or not await _visibility().filter_entities(ctx, [anchor.subject_entity_id]):
        raise _map_error(NotFoundError("no such claim"))

    chain = await history.chain_for(claim_uuid)
    chain_visibility = await history.visibility_rows_for([c.claim_id for c in chain])
    visible_chain = [
        c for c in chain if (row := chain_visibility.get(c.claim_id)) is not None and _claim_visible(ctx, row)
    ]

    set_mcp_result_count(len(visible_chain))
    return json.dumps({"items": [context._serialize(c) for c in visible_chain]})


# ---------------------------------------------------------------------------
# Tool: raise_capability_request
# ---------------------------------------------------------------------------


async def raise_capability_request(
    subject_entity_id: str,
    request_category: str,
    title: str,
    body: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Ask the tenant that owns a capability for something, routed by the subject.

    `CapabilityRequestService.raise_request`'s own subject lookup is a bare
    existence check, not a visibility filter -- called directly, that would
    make this tool a cross-tenant existence oracle (a caller could tell a
    private entity apart from a nonexistent one just by whether the request
    lands). So this tool resolves the subject through the visibility
    chokepoint first and raises the identical error the service itself
    raises for an absent subject: absent and invisible are the same answer.

    Args:
        subject_entity_id: UUID of the capability the request concerns.
        request_category: A short category tag, e.g. `add_dependency`.
        title: Short summary.
        body: Full request text.

    Returns:
        JSON object for the created request: request_id, owner_tenant_id,
        requester_tenant_id, subject_entity_id, request_category, title,
        body, status, decision_reason, resulting_promotion_id, created_at.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        subject_uuid = uuid.UUID(subject_entity_id)
    except ValueError as exc:
        raise ToolError("subject_entity_id must be a UUID") from exc

    if not await _visibility().filter_entities(ctx, [subject_uuid]):
        raise _map_error(NotFoundError("no such capability"))
    try:
        created = await _capability_requests().raise_request(
            ctx,
            subject_entity_id=subject_uuid,
            request_category=request_category,
            title=title,
            body=body,
        )
    except (NotFoundError, ValidationError) as exc:
        raise _map_error(exc) from exc
    return json.dumps(context._serialize(created))


# ---------------------------------------------------------------------------
# Tool: list_capability_requests
# ---------------------------------------------------------------------------


async def list_capability_requests(
    role: str = "owner",
    open_only: bool = True,
    cursor: str | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """What is waiting on this tenant to decide, or what it has asked for.

    Args:
        role: `owner` (default; the review queue) or `requester` (the
            tenant's own outbound history).
        open_only: For `role=owner` only, narrow to still-open requests
            (default true). Ignored for `role=requester` -- a declined or
            duplicate-marked request is exactly the signal that view exists
            to keep visible.
        cursor: Opaque cursor from a previous response's `next_cursor`.
        page_size: Items per page (1-500, default 100).

    Returns:
        `{"items": [...], "next_cursor": str | null}`.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if role not in ("owner", "requester"):
        raise ToolError("role must be 'owner' or 'requester'")
    _check_page_size(page_size)

    cursor_pair = _decode_cursor_pair(cursor, created_at_key="created_at", id_key="request_id")
    svc = _capability_requests()
    if role == "owner":
        items = await svc.for_owner(ctx, open_only=open_only, cursor=cursor_pair, page_size=page_size)
    else:
        items = await svc.raised_by(ctx, cursor=cursor_pair, page_size=page_size)

    next_cursor: str | None = None
    if len(items) > page_size:
        items = items[:page_size]
        last = items[-1]
        next_cursor = _encode_cursor_pair(
            last.created_at, last.request_id, created_at_key="created_at", id_key="request_id"
        )

    set_mcp_result_count(len(items))
    return json.dumps({"items": [context._serialize(i) for i in items], "next_cursor": next_cursor})


# ---------------------------------------------------------------------------
# Tool: triage_capability_request
# ---------------------------------------------------------------------------


async def triage_capability_request(
    request_id: str,
    to_status: str,
    reason: str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Move a capability request along its lifecycle.

    Authority is entirely `CapabilityRequestService.transition`'s own gate
    (owning tenant, `producer`/`admin` role, and the legal-transition check)
    -- this tool resolves tenant context and nothing else.

    Args:
        request_id: UUID of the request.
        to_status: One of acknowledged, accepted, declined, duplicate, resolved.
        reason: Required for declined/duplicate/resolved decisions.

    Returns:
        JSON object for the updated request (same shape as
        `raise_capability_request`'s return value).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        request_uuid = uuid.UUID(request_id)
    except ValueError as exc:
        raise ToolError("request_id must be a UUID") from exc
    if to_status not in _CAPABILITY_REQUEST_STATUSES:
        raise ToolError(f"to_status must be one of {sorted(_CAPABILITY_REQUEST_STATUSES)}")

    try:
        updated = await _capability_requests().transition(
            ctx,
            request_id=request_uuid,
            to_status=to_status,
            reason=reason,
        )
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise _map_error(exc) from exc
    return json.dumps(context._serialize(updated))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's thirteen tools onto ``mcp_server``.

    Only ``session_factory``/``clock`` are bound at registration time; the
    seven domain services are read off the app's typed container inside
    each tool body, the same way ``memory.py``'s session/claim-retrieval
    tools already do theirs.
    """
    deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    mcp_server.tool()(context._bind_tool(assert_claim, **deps))
    mcp_server.tool()(context._bind_tool(list_curation_queue, **deps))
    mcp_server.tool()(context._bind_tool(link_claim_subject, **deps))
    mcp_server.tool()(context._bind_tool(discard_claim, **deps))
    mcp_server.tool()(context._bind_tool(list_promotion_proposals, **deps))
    mcp_server.tool()(context._bind_tool(review_promotion_proposal, **deps))
    mcp_server.tool()(context._bind_tool(reverse_promotion, **deps))
    mcp_server.tool()(context._bind_tool(confirm_claim, **deps))
    mcp_server.tool()(context._bind_tool(adjudicate_claim, **deps))
    mcp_server.tool()(context._bind_tool(get_claim_history, **deps))
    mcp_server.tool()(context._bind_tool(raise_capability_request, **deps))
    mcp_server.tool()(context._bind_tool(list_capability_requests, **deps))
    mcp_server.tool()(context._bind_tool(triage_capability_request, **deps))


__all__: list[str] = [
    "assert_claim",
    "list_curation_queue",
    "link_claim_subject",
    "discard_claim",
    "list_promotion_proposals",
    "review_promotion_proposal",
    "reverse_promotion",
    "confirm_claim",
    "adjudicate_claim",
    "get_claim_history",
    "raise_capability_request",
    "list_capability_requests",
    "triage_capability_request",
    "register",
]
