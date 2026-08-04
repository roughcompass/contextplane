"""The only two functions that build a usage event.

Everything else in the system either stashes identity or calls one of these. That
is the shape the requirement asks for: a second construction site is how the
closed-vocabulary and no-content rules get bypassed, because the second site is
always written in a hurry by someone who does not know the rules exist.

A conformance gate asserts nothing outside this module inserts into the table.

**Both functions are silent on failure, and that is not laziness.** They are called
from the metrics middleware and the MCP tool wrapper, on the way out of a request
that has already succeeded. An exception here would turn a served request into a
500 caused by the attempt to measure it — the one outcome worse than not measuring.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from structlog.contextvars import get_contextvars

from registry.usage.identity import read_mcp_identity, read_request_identity
from registry.usage.vocabularies import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    SURFACE_MCP,
    SURFACE_REST,
)
from registry.usage.writer import UsageEvent, UsageWriter

__all__ = ["outcome_for", "record_mcp_usage", "record_rest_usage"]

_log = logging.getLogger(__name__)

# Status classes that mean the service did its job. A 3xx is a served response;
# only a refusal or a fault is an error. "Found nothing" is `result_count = 0`,
# not an outcome — see `vocabularies`.
_OK_STATUS_CLASSES = frozenset({"2xx", "3xx"})


def outcome_for(status_class: str) -> str:
    """Coarsen a status class to the served/failed split."""
    return OUTCOME_OK if status_class in _OK_STATUS_CLASSES else OUTCOME_ERROR


def _writer(app: Any) -> UsageWriter | None:
    writer = getattr(getattr(app, "state", None), "usage_writer", None)
    return writer if isinstance(writer, UsageWriter) else None


def record_rest_usage(
    scope: dict[str, Any],
    *,
    operation: str,
    status_class: str,
    seconds: float,
    now: datetime.datetime | None = None,
) -> None:
    """Record one REST call, from the metrics middleware.

    Reads the identity the tenant-context dependency stashed. A request with no
    stashed identity is **skipped, not recorded with a null tenant** — and the
    distinction matters: `actor_id` is nullable because an unauthenticated caller
    is still a caller, but `tenant_id` is not, because a row attributed to no
    tenant cannot be read back by anyone and would only inflate global counts.
    Unauthenticated traffic is already visible in the operational tier, which
    needs no identity at all.
    """
    try:
        identity = read_request_identity(scope)
        if identity is None:
            return
        writer = _writer(scope.get("app"))
        if writer is None:
            return
        writer.record(
            UsageEvent(
                occurred_at=now or datetime.datetime.now(tz=datetime.UTC),
                tenant_id=identity.tenant_id,
                actor_id=identity.actor_id,
                surface=SURFACE_REST,
                operation=operation,
                outcome=outcome_for(status_class),
                status_class=status_class,
                latency_ms=max(int(seconds * 1000), 0),
                request_id=_request_id(),
                subject_entity_ids=_subject_entities(scope),
            )
        )
    except Exception:  # pragma: no cover - measurement never breaks a request
        _log.debug("usage: rest recording failed", exc_info=True)


def record_mcp_usage(
    app: Any,
    *,
    tool: str,
    status_class: str,
    seconds: float,
    now: datetime.datetime | None = None,
) -> None:
    """Record one MCP tool invocation, from the tool wrapper.

    Identity comes from the ContextVar the tenant-resolution helper set. A tool
    that never resolved a tenant — one that failed before authentication — is
    skipped for the same reason as above.
    """
    try:
        identity = read_mcp_identity()
        if identity is None:
            return
        writer = _writer(app)
        if writer is None:
            return
        writer.record(
            UsageEvent(
                occurred_at=now or datetime.datetime.now(tz=datetime.UTC),
                tenant_id=identity.tenant_id,
                actor_id=identity.actor_id,
                surface=SURFACE_MCP,
                # The tool name, which is a closed set by construction: it comes
                # from the registered catalog and changes only when an engineer
                # adds a decorator.
                operation=tool,
                outcome=outcome_for(status_class),
                status_class=status_class,
                latency_ms=max(int(seconds * 1000), 0),
                request_id=_request_id(),
            )
        )
    except Exception:  # pragma: no cover - measurement never breaks a tool call
        _log.debug("usage: mcp recording failed", exc_info=True)


def _subject_entities(scope: dict[str, Any]) -> tuple[uuid.UUID, ...]:
    """Which entities this call concerned, taken from the resolved path params.

    A route matched as `/v1/capabilities/{entity_id}` leaves the *resolved* value in
    `scope["path_params"]`, so the middleware can read the entity the caller asked
    about without any per-route wiring. That is what makes a per-capability rollup
    possible at all — the `operation` column deliberately holds the template, so
    the id has to come from somewhere else.

    Only UUID-valued params are taken. A slug or a page cursor is not an entity
    reference, and putting one here would make the capability rollup group by
    something that is not a capability. Bounded by construction: a route has a
    handful of path params, not an unbounded list.
    """
    params = scope.get("path_params")
    if not isinstance(params, dict):
        return ()
    found: list[uuid.UUID] = []
    for value in params.values():
        if isinstance(value, uuid.UUID):
            found.append(value)
        elif isinstance(value, str):
            try:
                found.append(uuid.UUID(value))
            except (ValueError, AttributeError, TypeError):
                continue
    return tuple(found)


def _request_id() -> str | None:
    """The correlation id, from the context the request-id middleware bound.

    Not from the request headers. The middleware *generates* an id when the client
    sent none and echoes it on the response, so reading the inbound headers would
    capture it only for the callers who supplied their own — a correlation column
    that is populated exactly when it is least needed. It binds the id as a
    structlog context variable for the log lines, and that is the single source of
    truth for the value.
    """
    try:
        value = get_contextvars().get("request_id")
        return value[:128] if isinstance(value, str) else None
    except Exception:  # pragma: no cover
        return None
