"""Signal ingestion over HTTP: one route, one contract.

    POST /v1/signals → SignalIngestResponse (201 created | 200 recognised)

This router adapts and does not decide. Every rule -- which sources may write,
what authority their observations carry, whether a resubmission is a replay or a
conflict, what bounds one submission may not exceed -- lives in
`signals/ingest.py`, because the MCP surface answers the same question and a rule
enforced in two adapters is a rule that will eventually be enforced differently
in one of them.

**A plain resource `POST`, not a tunneled action, and not run through
`HttpMethodRouter`.** Reporting an observation is a collection create, the same
shape `POST /v1/capabilities` already is here: there is no alternate HTTP verb
for it, so there is nothing for the method-mode switch to switch between.

**201 or 200, and the difference is real.** A submission this call stored answers
201; one it recognised as already stored answers 200 with the same body. That is
what lets a client retrying a dropped response tell that its retry found the
first write rather than making a second.

**Idempotency lives in the envelope, not in a header.** Every other create in
this codebase reads `Idempotency-Key` off the request, and this one deliberately
does not: a signal's submission key is part of what the producer reports and part
of what the ledger enforces a unique index on, so it is a body field a replay
carries verbatim rather than transport metadata a proxy might not forward.
Threading a second key through a header would give one submission two identities
that could disagree.

**Refusals are distinguished by what a caller should do about them.**
An unregistered source and another tenant's source both answer 404 with the same
message -- a distinguishable refusal would turn a source id into a cross-tenant
existence oracle. A source over its declared ceiling answers 429, because the
same bytes will be accepted later; a submission that changed what it reports
under a key already used answers 409, because nothing the caller can retry will
make both true.

**The ingest service is built per request from the container's own session
factory and clock.** It holds no construction-time policy -- no retention
decision, no clock of its own -- so a single instance would carry nothing a fresh
one does not, and building it here keeps the two transports reading the same
governance service the container already publishes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from contextplane.api.auth.context import require_roles
from contextplane.api.container import Services, services
from contextplane.api.errors import build_error, map_catalog_error
from contextplane.api.schemas.signals import SignalIngestRequest, SignalIngestResponse
from contextplane.auth.roles import ROLE_ADMIN, ROLE_CONSUMER, ROLE_PRODUCER
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.signals.ingest import (
    ExternalSignalEnvelopeV1,
    SignalIngestRefused,
    SignalIngestService,
    normalize_references,
)
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1", tags=["signals"])

# Reporting an observation is a write, so it needs a write role. `consumer` is
# included because a human reporting an outcome about work they consumed is the
# ordinary case this surface exists for, and requiring `producer` for it would
# leave the direct-feedback path reachable only by service accounts.
_ingest_required = require_roles([ROLE_CONSUMER, ROLE_PRODUCER, ROLE_ADMIN])


def _ingest_service(container: Services) -> SignalIngestService:
    """The one ingest service the app constructed, off the typed container.

    Read rather than built: a route that assembled its own from container
    primitives would be a second construction of a service the app already
    declares, and the two could drift a collaborator apart without either
    call site changing.
    """
    return container.signal_ingest


@router.post("/signals", response_model=SignalIngestResponse)
async def ingest_signal(
    body: SignalIngestRequest,
    ctx: Annotated[TenantContext, Depends(_ingest_required)],
    container: Annotated[Services, Depends(services)],
    response: Response,
) -> SignalIngestResponse:
    """Report one observation from a registered source.

    Records what the source said, at the three times involved, under the
    authority that source declared. It concludes nothing: no success, no failure,
    no causal link, and no learning eligibility is derived from the payload here.

    Returns the stored signal's id, the server-assigned ingestion time, the
    derived authority, the content digest, whether this call recognised an
    existing submission rather than storing a new one, and each reference
    normalized with its collision key.
    """
    try:
        envelope = ExternalSignalEnvelopeV1(
            source_id=body.source_id,
            source_system=body.source_system,
            source_event_id=body.source_event_id,
            producer_id=body.producer_id,
            producer_type=body.producer_type,
            idempotency_key=body.idempotency_key,
            classification=body.classification,
            schema_version=body.schema_version,
            event_time=body.event_time,
            observed_time=body.observed_time,
            references=normalize_references([reference.model_dump() for reference in body.references]),
            team_key=body.team_key,
            project_key=body.project_key,
            expires_at=body.expires_at,
            payload=body.payload,
            evidence_handle=body.evidence_handle,
        )
        ingested = await _ingest_service(container).ingest(ctx, envelope)
    except SignalIngestRefused as exc:
        # 429, not 403: nothing about the submission is wrong, and the same bytes
        # will be accepted once the window rolls or the circuit closes. A
        # `forbidden` here would send a client looking for a permission problem
        # it does not have.
        raise build_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            code="source_ingest_ceiling",
            message=str(exc),
        ) from exc
    except ConflictError as exc:
        # Special-cased ahead of the generic translator, the same way other
        # routers here special-case an exception whose code a caller has to
        # branch on: `map_catalog_error` would give this the right status (409)
        # under the generic `conflict` code, and a client cannot tell that apart
        # from any other 409 on any other surface. A reused submission key
        # carrying different content is the one conflict this route can produce,
        # and naming it is what lets a client stop retrying.
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message=str(exc),
        ) from exc
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc

    response.status_code = status.HTTP_200_OK if ingested.replayed else status.HTTP_201_CREATED
    return SignalIngestResponse.of(ingested)


__all__ = ["router"]
