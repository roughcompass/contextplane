"""`POST /v1/arc/sources/graph-promotions` — admit a promoted claim as source evidence.

A sibling module rather than three more handlers in `arc.py`, which is
within twenty lines of the 800-line ceiling `scripts/check_file_sizes.py`
enforces. Splitting the *existing* source routes out would be a move-only
refactor needing its own byte-identical-`openapi.json` proof; adding the new
route beside them here needs none, and the two files stay cohesive because
this one holds exactly the admission authority that reads the canonical
graph.

The five request helpers come from `arc.py` rather than being re-derived:
`_evidence_response` and `_translate_source_admission_error` in particular
decide what a caller sees, and a second copy is how two source-admission
routes would start reporting the same refusal differently.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status

from contextplane.api.container import Services
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.routers.arc import (
    _arc_context,
    _evidence_response,
    _require_idempotency_key,
    _translate_source_admission_error,
)
from contextplane.api.schemas.arc_authoring import GraphPromotionRequest, SourceEvidenceResponse
from contextplane.arc import GraphPromotionAdmission, GraphPromotionAdmissionService
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/arc", tags=["arc"])


def _graph_admission(request: Request) -> GraphPromotionAdmissionService:
    services: Services = request.app.state.services
    return services.arc_graph_source_admission


async def admit_source_graph_promotion(
    request: Request,
    body: GraphPromotionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SourceEvidenceResponse:
    """Admit a claim the canonical graph already carries.

    The caller names a promoted claim, the upstream system its evidence
    points into, and a review deadline. It supplies no proof: the approving
    authority is the promotion journal row, which this service reads and
    re-checks — including whether the promotion was since reversed, and
    whether the promoting actor was someone other than the claim's author.
    """
    arc_ctx = _arc_context(request, ctx)
    key = _require_idempotency_key(idempotency_key)
    admission = GraphPromotionAdmission(
        claim_id=body.claim_id,
        source_system=body.source_system,
        review_expires_at=body.review_expires_at,
        idempotency_key=key,
    )
    try:
        evidence = await _graph_admission(request).admit_promoted_claim(arc_ctx, admission)
    except Exception as exc:
        raise _translate_source_admission_error(exc) from exc
    return _evidence_response(evidence)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_mutation_route(
    path="/sources/graph-promotions",
    action="admit",
    handler=admit_source_graph_promotion,
    verb="POST",
    response_model=SourceEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)

__all__ = ["admit_source_graph_promotion", "router"]
