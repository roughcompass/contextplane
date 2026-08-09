"""Signal ingestion over MCP: the agent-facing twin of ``api/routers/signals.py``.

One tool, over the same service, so an agent reporting an observation gets the
same answer a REST caller would -- including the same refusals. Nothing here
re-derives a rule ``signals/ingest.py`` already enforces; this module's job is
translating the service's typed exceptions into a ``ToolError`` a caller can act
on, and turning the result into JSON.

**Three fields the REST body has to refuse, this tool cannot receive.** Ingestion
time, authority, and the content digest are server-derived, and they are simply
not parameters here -- an MCP tool's argument schema is closed by construction,
so there is no open-body case to guard. The REST surface needs an explicit
refusal for exactly that reason: a JSON body is open unless something closes it.
Both transports end up prohibiting the same three things; only one of them has to
say so out loud.

**The governance service comes off the app's typed container at call time.** The
MCP server is built while the router table is being mounted, before the container
exists, so it cannot be a construction-time argument the way ``session_factory``
and ``clock`` are -- the same per-call accessor shape the other tool modules use.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import CatalogError
from contextplane.signals.ingest import (
    ExternalSignalEnvelopeV1,
    IngestedSignal,
    SignalIngestRefused,
    SignalIngestService,
    normalize_references,
)
from contextplane.types import Clock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

    from contextplane.service.memory.source_governance import SourceGovernanceService


def _governance() -> SourceGovernanceService:
    app = context._request_app.get()
    service = getattr(context._services(app), "source_governance", None)
    if service is None:
        raise ToolError("source governance is not configured on this deployment")
    return cast("SourceGovernanceService", service)


def _parse_moment(name: str, raw: str) -> datetime.datetime:
    """Parse a required ISO-8601 instant, refusing a naive one.

    Refused rather than assumed UTC: a timestamp read as local time by whoever
    renders it makes the lag between two of these three instants unreadable,
    which is the one thing keeping them in separate fields is for.
    """
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ToolError(f"{name} must be an ISO-8601 datetime: {exc}") from exc
    if parsed.tzinfo is None:
        raise ToolError(f"{name} must be timezone-aware; a naive timestamp is unreadable across zones")
    return parsed


def _signal_json(ingested: IngestedSignal) -> dict[str, Any]:
    return {
        "signal_id": str(ingested.signal_id),
        "ingested_at": ingested.ingested_at.isoformat(),
        "authority": ingested.authority,
        "content_digest": ingested.content_digest,
        "replayed": ingested.replayed,
        "references": [
            {
                "collision_key": reference.collision_key(),
                "source_system": reference.source_system,
                "source_namespace": reference.source_namespace,
                "kind": reference.kind,
                "external_id": reference.external_id,
                "classification": reference.classification,
                "external_authority": reference.external_authority,
                "revision": reference.revision,
                "authorized_uri": reference.authorized_uri,
                "observed_at": None if reference.observed_at is None else reference.observed_at.isoformat(),
            }
            for reference in ingested.references
        ],
    }


async def ingest_signal(
    source_id: str,
    source_system: str,
    source_event_id: str,
    producer_id: str,
    producer_type: str,
    idempotency_key: str,
    classification: str,
    event_time: str,
    observed_time: str,
    references: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    evidence_handle: str | None = None,
    team_key: str | None = None,
    project_key: str | None = None,
    expires_at: str | None = None,
    schema_version: str = "external_signal.v1",
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Report one observation from a registered source, exactly once.

    Records what the source said, at the three times involved, under the authority
    that source declared for itself. It concludes nothing: no success, no failure,
    no causal link, and no learning eligibility is derived from what you send.
    The ingestion time is this server's own and cannot be supplied; the authority
    comes from the source's declared policy and cannot be supplied either.

    Resubmitting the same observation is safe. An exact redelivery -- whether
    under the same submission key or a fresh one -- finds the stored signal and
    reports `replayed: true` instead of storing a second row. Reusing a
    submission key with *different* content is refused rather than either
    overwriting the stored observation or storing a second one beside it.

    Args:
        source_id: UUID of the registered source this arrives through. The
            registration carries the declared authority and the ingest ceiling.
        source_system: The external system's own name, e.g. `github`.
        source_event_id: That system's identifier for this occurrence.
        producer_id: Who produced the observation, in the source's id space.
        producer_type: One of `human`, `agent`, `external`.
        idempotency_key: This submission's key. Distinct from source_event_id.
        classification: One of `public`, `internal`, `confidential`, `restricted`.
        event_time: ISO-8601, timezone-aware. When the source says it happened.
        observed_time: ISO-8601, timezone-aware. When the producer learned of it.
        references: The external work this is about. Each needs `source_system`,
            `source_namespace`, `kind`, `external_id`, `classification`,
            `external_authority`; `revision`, `authorized_uri` and `observed_at`
            are optional. Empty is legal for a diagnostic observation.
        payload: The allowlisted projection. Exactly one of payload or
            evidence_handle.
        evidence_handle: A handle to authorized evidence held elsewhere. Exactly
            one of payload or evidence_handle.
        team_key: Team scope, when the producer knows one.
        project_key: Project scope, when the producer knows one.
        expires_at: ISO-8601. When this stops being usable as current.
        schema_version: The envelope contract version.

    Returns:
        JSON object with signal_id, ingested_at, authority, content_digest,
        replayed, and each reference normalized with its collision_key.
    """
    ctx = await context._resolve_tenant(session_factory, clock)

    try:
        source_uuid = uuid.UUID(source_id)
    except ValueError as exc:
        raise ToolError(f"source_id must be a UUID: {exc}") from exc

    try:
        envelope = ExternalSignalEnvelopeV1(
            source_id=source_uuid,
            source_system=source_system,
            source_event_id=source_event_id,
            producer_id=producer_id,
            producer_type=producer_type,
            idempotency_key=idempotency_key,
            classification=classification,
            schema_version=schema_version,
            event_time=_parse_moment("event_time", event_time),
            observed_time=_parse_moment("observed_time", observed_time),
            references=normalize_references(references or []),
            team_key=team_key,
            project_key=project_key,
            expires_at=None if expires_at is None else _parse_moment("expires_at", expires_at),
            payload=payload,
            evidence_handle=evidence_handle,
        )
        ingested = await SignalIngestService(
            session_factory,
            clock=clock,
            governance=_governance(),
        ).ingest(ctx, envelope)
    except SignalIngestRefused as exc:
        raise ToolError(f"the source may not write right now: {exc}") from exc
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc

    return json.dumps(_signal_json(ingested))


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tool onto ``mcp_server``."""
    mcp_server.tool()(context._bind_tool(ingest_signal, session_factory=session_factory, clock=clock))


__all__ = ["ingest_signal", "register"]
