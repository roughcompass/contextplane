"""Just-in-time detail retrieval: one page, one transaction, one receipt event.

The bundle an agent receives at resolution carries identity and citation but
not prose. Full source text arrives only here, on request, for one item at a
time. Two reasons, and they pull the same way: putting every directive's text
in every bundle would blow the byte budget, and it would hand every matched
actor content that only some of them are cleared to read.

Because detail is served *after* resolution, the world may have moved. So
every page re-checks rather than trusting the receipt:

- The receipt must still be usable -- a chain that failed verification cannot
  keep serving the content it once authorized.
- The item must have been selected *by that receipt*. Detail cannot widen
  scope; a handle is a pointer into what was already granted, never a key to
  something new.
- Lifecycle and audience are re-evaluated now, not as of resolution. A
  revision revoked in between must stop being readable immediately.

Every attempt that gets as far as a current authorization decision appends a
receipt event -- granted or denied. An attempt that never got that far,
because its token was invalid or replayed, must *not* touch the chain: it was
not a well-bound request, and letting it advance the chain would let anyone
holding a receipt ID append to someone else's audit record.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.continuation import (
    MAX_CHAIN_BYTES,
    TOKEN_TTL,
    ContinuationTokenError,
    ContinuationTokenProvider,
    PageBinding,
    PageState,
    issue,
    open_token,
    token_digest,
)
from registry.arc.service.receipt import EVENT_SOURCE_HOST, ReceiptService
from registry.arc.types import ArcRequestContext, DetailAudience
from registry.audit import actions
from registry.types import Clock

DETAIL_REQUEST_PROFILE = "arc_detail_request_v1"
DETAIL_RESPONSE_PROFILE = "arc_detail_response_page_v1"

EVENT_JIT_RETRIEVAL = "jit_retrieval"
EVENT_JIT_DENIED = "jit_denied"

# Per page, and the ceiling a caller may ask for. The chain-wide cap in
# `continuation` is the one that actually bounds total exposure; this only
# bounds a single response.
DEFAULT_PAGE_BYTES = 16 * 1024
MAX_PAGE_BYTES = 32 * 1024

DENIED_REVOKED = "detail_revoked"
DENIED_AUDIENCE = "detail_audience_denied"
DENIED_NOT_SELECTED = "detail_not_selected"
DENIED_RECEIPT_UNUSABLE = "detail_receipt_unusable"
DENIED_CHAIN_BUDGET = "detail_chain_budget_exhausted"


class DetailDenied(Exception):
    """The caller may not read this detail, now.

    Carries a bounded reason code for the receipt event and the audit row.
    The code is deliberately coarse: a caller learning precisely *why* they
    were denied learns the shape of what they cannot see.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DetailIdempotencyConflict(Exception):
    """One idempotency key, two different requests."""


@dataclasses.dataclass(frozen=True)
class DetailRequest:
    """One page request, already shape-validated.

    `continuation_token` and `idempotency_key` are transport state and are
    deliberately excluded from the base digest below: the base digest
    identifies *what was asked for*, and it must stay identical across the
    pages of one chain.
    """

    receipt_id: uuid.UUID
    context_handle: str
    request_kind: str
    selector: dict[str, object]
    idempotency_key: str
    max_response_bytes: int = DEFAULT_PAGE_BYTES
    continuation_token: str | None = None

    def base_digest(self) -> str:
        canonical = json.dumps(
            {
                "profile": DETAIL_REQUEST_PROFILE,
                "receipt_id": str(self.receipt_id),
                "context_handle": self.context_handle,
                "request_kind": self.request_kind,
                "selector": self.selector,
                "max_response_bytes": self.max_response_bytes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class DetailItem:
    """One unit of returned detail, with the citation that makes it checkable.

    An item the caller's audience does not permit is returned *redacted*
    rather than dropped. Silently omitting it would leave the caller unable
    to tell "there is nothing here" from "there is something here you may
    not see" -- and those are different facts, one of which an agent needs
    in order to know it should escalate rather than proceed.

    Redaction removes the payload, not the pointer: identity survives so the
    omission is nameable, while the prose, the locators, and the digest --
    everything that carries or fingerprints content -- do not.
    """

    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    directive_id: uuid.UUID | None
    source_anchor: str
    source_system: str
    source_canonical_locator: str
    source_revision_locator: str
    content_digest: str
    excerpt: str
    audience_redacted: bool = False

    def redacted(self) -> DetailItem:
        """The same item with every audience-gated field emptied.

        A new object rather than a flag consulted at render time: a redacted
        item that still carries its excerpt in memory is one accidental
        serialization away from leaking it.
        """
        return dataclasses.replace(
            self,
            source_anchor="",
            source_system="",
            source_canonical_locator="",
            source_revision_locator="",
            content_digest="",
            excerpt="",
            audience_redacted=True,
        )

    def as_content(self) -> dict[str, object]:
        if self.audience_redacted:
            return {
                "artifact_id": str(self.artifact_id),
                "revision_id": str(self.revision_id),
                "directive_id": str(self.directive_id) if self.directive_id else None,
                "source_anchor": None,
                "citation": None,
                "trust_label": "source_detail",
                "excerpt": None,
                # No excerpt digest either: a digest of withheld content is
                # still an oracle for guessing it.
                "excerpt_digest": None,
                "audience_redacted": True,
            }
        return {
            "artifact_id": str(self.artifact_id),
            "revision_id": str(self.revision_id),
            "directive_id": str(self.directive_id) if self.directive_id else None,
            "source_anchor": self.source_anchor,
            "citation": {
                "source_system": self.source_system,
                "source_canonical_locator": self.source_canonical_locator,
                "source_revision_locator": self.source_revision_locator,
                "content_digest": self.content_digest,
            },
            # Labelled so a model reading the bundle can tell retrieved source
            # material from a directive it must obey. They are different kinds
            # of claim on its behaviour.
            "trust_label": "source_detail",
            "excerpt": self.excerpt,
            "excerpt_digest": hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest(),
            "audience_redacted": False,
        }


@dataclasses.dataclass(frozen=True)
class DetailPage:
    """One page, plus the token that reaches the next."""

    receipt_id: uuid.UUID
    request_digest: str
    page_number: int
    items: tuple[dict[str, object], ...]
    returned_bytes: int
    complete: bool
    continuation_token: str | None = None
    reason_codes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SelectedDetail:
    """A row the receipt actually selected, as the detail path needs it."""

    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    directive_id: uuid.UUID | None
    source_anchor: str
    source_system: str
    source_canonical_locator: str
    source_revision_locator: str
    content_digest: str
    body: str
    detail_audience: DetailAudience
    lifecycle_state: str


class JitService:
    """Serves one authorized page of detail per transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        receipts: ReceiptService,
        tokens: ContinuationTokenProvider,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._receipts = receipts
        self._tokens = tokens
        self._clock = clock

    async def retrieve(self, ctx: ArcRequestContext, request: DetailRequest) -> DetailPage:
        """Serve one page, or refuse and record why.

        A refusal that reached an authorization decision still commits: the
        receipt event recording the denial is evidence, and evidence of a
        denial is exactly as important as evidence of a grant.
        """
        now = self._clock.now()
        base_digest = request.base_digest()
        handle_digest = hashlib.sha256(request.context_handle.encode("utf-8")).hexdigest()
        key_digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()

        binding = PageBinding(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            host_id=ctx.host_id or "",
            receipt_id=request.receipt_id,
            context_handle_digest=handle_digest,
            base_request_digest=base_digest,
        )

        # Token validation happens before the transaction that would append
        # to the chain. An invalid or replayed token is not a well-bound
        # request, and must leave the receipt's audit chain untouched --
        # otherwise anyone knowing a receipt ID could append to it.
        incoming_state: PageState | None = None
        consumed_digest: str | None = None
        if request.continuation_token is not None:
            try:
                incoming_state = open_token(self._tokens, request.continuation_token, binding=binding, now=now)
            except ContinuationTokenError as exc:
                await self._audit_rejected(ctx, request, base_digest, reason="invalid_continuation")
                raise DetailDenied("invalid_continuation", str(exc)) from exc
            consumed_digest = token_digest(request.continuation_token)

        page_number = incoming_state.page_number + 1 if incoming_state else 1
        cumulative_bytes = incoming_state.cumulative_bytes if incoming_state else 0
        cumulative_results = incoming_state.cumulative_results if incoming_state else 0
        position = incoming_state.next_position if incoming_state else 0

        async with self._session_factory() as session, session.begin():
            await self._assert_replay_consistent(session, request, key_digest, base_digest)

            if not await self._receipts.is_usable(session, request.receipt_id):
                await self._deny(
                    session, ctx, request, base_digest, key_digest, consumed_digest, DENIED_RECEIPT_UNUSABLE
                )

            rows = await self._load_selected(session, ctx, request)
            if not rows:
                await self._deny(
                    session, ctx, request, base_digest, key_digest, consumed_digest, DENIED_NOT_SELECTED
                )

            # Revocation removes an item outright: a revoked revision is not
            # something the caller may see a redacted stub of, it is
            # something that should no longer be served at all.
            live = [r for r in rows if r.lifecycle_state in ("active", "expired")]
            if not live:
                await self._deny(session, ctx, request, base_digest, key_digest, consumed_digest, DENIED_REVOKED)

            # Audience, by contrast, redacts rather than removes -- but a
            # page where the caller may read *nothing* is a denial, not a
            # page of empty stubs.
            permitted = [self._audience_permits(ctx, r) for r in live]
            if not any(permitted):
                await self._deny(session, ctx, request, base_digest, key_digest, consumed_digest, DENIED_AUDIENCE)

            if cumulative_bytes >= MAX_CHAIN_BYTES:
                await self._deny(
                    session, ctx, request, base_digest, key_digest, consumed_digest, DENIED_CHAIN_BUDGET
                )

            page_limit = min(request.max_response_bytes, MAX_PAGE_BYTES, MAX_CHAIN_BYTES - cumulative_bytes)
            items, next_position, returned_bytes = _fill_page(live, permitted, position, page_limit)
            complete = next_position >= len(live)
            redacted_count = sum(1 for i in items if i.audience_redacted)

            token: str | None = None
            if not complete:
                token = issue(
                    self._tokens,
                    binding=binding,
                    state=PageState(
                        page_number=page_number,
                        next_position=next_position,
                        cumulative_bytes=cumulative_bytes + returned_bytes,
                        cumulative_results=cumulative_results + len(items),
                        # Binding the artifact state means a revocation
                        # mid-chain invalidates the token rather than
                        # silently changing what the next page returns.
                        artifact_state_digest=_artifact_state_digest(live),
                        issued_at=now,
                        expires_at=now + TOKEN_TTL,
                    ),
                )

            await self._append_event(
                session,
                ctx,
                request,
                event_type=EVENT_JIT_RETRIEVAL,
                base_digest=base_digest,
                key_digest=key_digest,
                consumed_digest=consumed_digest,
                payload={
                    "page_number": page_number,
                    "returned_bytes": returned_bytes,
                    "complete": complete,
                    "item_count": len(items),
                    # Recorded so an auditor can see that some of what was
                    # selected was withheld from this actor, without the
                    # event itself naming what.
                    "redacted_count": redacted_count,
                    "reason_codes": [DENIED_AUDIENCE] if redacted_count else [],
                },
            )
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_JIT_GRANTED,
                payload={
                    "receipt_id": str(request.receipt_id),
                    "request_digest": base_digest,
                    "page_number": page_number,
                    "returned_bytes": returned_bytes,
                    "complete": complete,
                },
            )

        return DetailPage(
            receipt_id=request.receipt_id,
            request_digest=base_digest,
            page_number=page_number,
            items=tuple(i.as_content() for i in items),
            returned_bytes=returned_bytes,
            complete=complete,
            continuation_token=token,
            reason_codes=(DENIED_AUDIENCE,) if redacted_count else (),
        )

    def _audience_permits(self, ctx: ArcRequestContext, row: SelectedDetail) -> bool:
        """Audience, evaluated now rather than as of resolution.

        A caller whose roles changed, or an artifact whose audience was
        narrowed, must take effect on the next page -- not on the next
        resolution.
        """
        if row.detail_audience is DetailAudience.ALL_MATCHED_ACTORS:
            return True
        if row.detail_audience is DetailAudience.TENANT_ADMIN_AUDITOR:
            return "admin" in ctx.roles or "auditor" in ctx.roles
        if row.detail_audience is DetailAudience.REGISTERED_GATEWAY_ONLY:
            return ctx.is_mcp_session
        return False

    async def _assert_replay_consistent(
        self, session: AsyncSession, request: DetailRequest, key_digest: str, base_digest: str
    ) -> None:
        """One idempotency key must always mean one request.

        A key reused with a different base digest is the caller conflating
        two requests, which is exactly what an idempotency key exists to
        catch.
        """
        row = (
            await session.execute(
                text(
                    "SELECT request_payload_digest FROM arc_receipt_events "
                    "WHERE receipt_id = :rid AND event_source = :src AND idempotency_key_digest = :kd"
                ),
                {"rid": request.receipt_id, "src": EVENT_SOURCE_HOST, "kd": key_digest},
            )
        ).one_or_none()
        if row is not None and row.request_payload_digest != base_digest:
            msg = "idempotency key already identifies a different detail request"
            raise DetailIdempotencyConflict(msg)

    async def _load_selected(
        self, session: AsyncSession, ctx: ArcRequestContext, request: DetailRequest
    ) -> list[SelectedDetail]:
        """Read only what this receipt selected, scoped to this tenant.

        Two shapes, and the difference is enforced by the schema rather than
        chosen here. A context handle is unique per receipt -- one handle
        resolves to exactly one selection row, so JIT authorization is never
        ambiguous -- which means a handle lookup can only ever return a
        single item. Paging therefore exists for the query shape, which
        ranges over everything the receipt selected.

        Either way the join through `arc_receipt_selected_directives` is what
        stops detail widening scope: a row that was never selected is simply
        not reachable from here, rather than reachable and then rejected.
        """
        by_handle = request.request_kind != "query"
        sql = (
            "SELECT sd.artifact_id, sd.revision_id, sd.directive_id, d.source_anchor, "
            "       r.source_system, r.source_canonical_locator, r.source_revision_locator, "
            "       r.content_digest, coalesce(r.source_body_plaintext, '') AS body, "
            "       r.detail_audience, r.lifecycle_state "
            "FROM arc_receipt_selected_directives sd "
            "JOIN arc_revisions r ON r.revision_id = sd.revision_id "
            "JOIN arc_directives d ON d.revision_id = sd.revision_id "
            "                     AND d.directive_id = sd.directive_id "
            "JOIN arc_receipts rc ON rc.receipt_id = sd.receipt_id "
            "WHERE sd.receipt_id = :rid AND rc.tenant_id = :tid AND rc.actor_id = :aid "
        )
        params: dict[str, object] = {
            "rid": request.receipt_id,
            "tid": ctx.tenant_id,
            "aid": ctx.actor_id,
        }
        if by_handle:
            sql += "  AND sd.context_handle_digest = :handle "
            params["handle"] = hashlib.sha256(request.context_handle.encode("utf-8")).hexdigest()
        # Ordered by directive id, not by relevance: paging must be stable
        # across calls, and a rank that shifted between pages would silently
        # skip or repeat items.
        sql += "ORDER BY sd.directive_id"

        rows = (await session.execute(text(sql), params)).all()
        return [
            SelectedDetail(
                artifact_id=r.artifact_id,
                revision_id=r.revision_id,
                directive_id=r.directive_id,
                source_anchor=r.source_anchor,
                source_system=r.source_system,
                source_canonical_locator=r.source_canonical_locator,
                source_revision_locator=r.source_revision_locator,
                content_digest=r.content_digest,
                body=r.body,
                detail_audience=DetailAudience(r.detail_audience),
                lifecycle_state=r.lifecycle_state,
            )
            for r in rows
        ]

    async def _deny(
        self,
        session: AsyncSession,
        ctx: ArcRequestContext,
        request: DetailRequest,
        base_digest: str,
        key_digest: str,
        consumed_digest: str | None,
        reason_code: str,
    ) -> None:
        """Record the denial on the chain, then raise.

        The event is appended rather than skipped because a denial is
        evidence too: an auditor asking what an agent was refused, and when,
        needs the same record as what it was granted.
        """
        await self._append_event(
            session,
            ctx,
            request,
            event_type=EVENT_JIT_DENIED,
            base_digest=base_digest,
            key_digest=key_digest,
            consumed_digest=consumed_digest,
            payload={"reason_codes": [reason_code]},
        )
        await audit_outbox.emit(
            session,
            tenant_id=ctx.tenant_id,
            event_type=actions.ARC_JIT_DENIED,
            payload={
                "receipt_id": str(request.receipt_id),
                "request_digest": base_digest,
                "reason_code": reason_code,
            },
        )
        await session.commit()
        msg = f"detail denied: {reason_code}"
        raise DetailDenied(reason_code, msg)

    async def _append_event(
        self,
        session: AsyncSession,
        ctx: ArcRequestContext,
        request: DetailRequest,
        *,
        event_type: str,
        base_digest: str,
        key_digest: str,
        consumed_digest: str | None,
        payload: dict[str, object],
    ) -> None:
        await self._receipts.append_event(
            session,
            receipt_id=request.receipt_id,
            tenant_id=ctx.tenant_id,
            event_type=event_type,
            event_source=EVENT_SOURCE_HOST,
            request_payload_digest=base_digest,
            payload=payload,
            actor_id=ctx.actor_id,
            idempotency_key_digest=key_digest,
            consumed_continuation_token_digest=consumed_digest,
        )

    async def _audit_rejected(
        self, ctx: ArcRequestContext, request: DetailRequest, base_digest: str, *, reason: str
    ) -> None:
        """A rejected attempt, recorded without touching the receipt chain.

        Its own transaction, because there is no legitimate chain append to
        ride on -- and an unbound attempt must not be able to advance
        someone else's audit record.
        """
        async with self._session_factory() as session, session.begin():
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_JIT_ATTEMPT_REJECTED,
                payload={
                    "receipt_id": str(request.receipt_id),
                    "request_digest": base_digest,
                    "reason_code": reason,
                },
            )


def _fill_page(
    rows: list[SelectedDetail], permitted: list[bool], position: int, limit_bytes: int
) -> tuple[list[DetailItem], int, int]:
    """Take whole items until the next would exceed the limit.

    Never a partial item: truncating an excerpt mid-way would hand back
    content whose digest does not match anything, and a caller could not
    tell a truncated obligation from a complete one.

    Redaction is applied here, before measurement, so a redacted stub is
    charged its real (small) size against the budget rather than the size of
    the content the caller never received.
    """
    items: list[DetailItem] = []
    used = 0
    index = position
    while index < len(rows):
        row = rows[index]
        item = DetailItem(
            artifact_id=row.artifact_id,
            revision_id=row.revision_id,
            directive_id=row.directive_id,
            source_anchor=row.source_anchor,
            source_system=row.source_system,
            source_canonical_locator=row.source_canonical_locator,
            source_revision_locator=row.source_revision_locator,
            content_digest=row.content_digest,
            excerpt=row.body,
        )
        if not permitted[index]:
            item = item.redacted()
        size = len(json.dumps(item.as_content(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if items and used + size > limit_bytes:
            break
        # An item larger than the whole page limit is still served alone:
        # refusing it would make that item permanently unreachable.
        items.append(item)
        used += size
        index += 1
    return items, index, used


def _artifact_state_digest(rows: list[SelectedDetail]) -> str:
    """A digest over what is being paged, so a change invalidates the token."""
    material = "|".join(f"{r.revision_id}:{r.lifecycle_state}:{r.content_digest}" for r in rows)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_PAGE_BYTES",
    "DENIED_AUDIENCE",
    "DENIED_CHAIN_BUDGET",
    "DENIED_NOT_SELECTED",
    "DENIED_RECEIPT_UNUSABLE",
    "DENIED_REVOKED",
    "DETAIL_REQUEST_PROFILE",
    "DETAIL_RESPONSE_PROFILE",
    "EVENT_JIT_DENIED",
    "EVENT_JIT_RETRIEVAL",
    "MAX_PAGE_BYTES",
    "DetailDenied",
    "DetailIdempotencyConflict",
    "DetailItem",
    "DetailPage",
    "DetailRequest",
    "JitService",
    "SelectedDetail",
]
