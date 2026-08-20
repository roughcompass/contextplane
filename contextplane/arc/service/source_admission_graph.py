"""Admitting what the canonical graph already vouches for.

The two admission paths in `source_admission.py` both start from bytes this
deployment pulled in: a connector fetch or an authorized upload. This one
starts from a claim that is already on the canonical graph, promoted there by
an actor who is not the claim's author, and turns that promotion into source
evidence a governed revision may cite.

**What vouches for it.** Not a signature -- there is none, and the evidence
row records that honestly by leaving both proof columns NULL under the
`graph_promoted` verification method. The authority is
`memory_promotion_journal`: promotion is the point where a staged claim
becomes something the rest of the product treats as true, it is performed by
a second actor, and it is exactly reversible. This service re-reads that row
at admission time and refuses a promotion that was withdrawn, so a reversed
decision cannot be laundered into ARC by admitting it afterwards.

**Separation of duties is enforced here, not assumed.** The promotion service
already routes a cross-tenant claim to the owning tenant rather than writing
their graph, and this module additionally refuses a promotion whose
`promoted_by` is the claim's own `author_actor_id`. One actor asserting and
then promoting their own claim is a single point of failure wearing a
two-step process, and ARC's whole value is that the chain has no such link.

**What the bytes are.** A promoted claim has no document to hash, so the
admitted content is a canonical JSON projection of the promotion itself:
subject, predicate, value, the evidence refs behind it, and the journal
record. That projection is what a directive anchors into and what
`/v1/arc/sources/{id}/body` later returns, so the anchor a policy author
writes points at something byte-stable and re-derivable rather than at a
rendering that could drift.

**Expiry is supplied, not derived.** Every source evidence row carries an
`expires_at` and ARC refuses an expired one; a graph fact has no natural
deadline. The caller therefore states a review horizon, which is the same
shape as the review dates the artifact lifecycle already runs on -- a
governed citation of a graph fact should be revisited, and the alternative
(an evidence row that never expires) would be the only one on this surface.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from contextplane.arc.schemas.authoring_profile_shapes import SOURCE_APPROVAL_CLAIM_PROFILE
from contextplane.arc.service import source_admission_vocab as vocab
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.queries import source_admission_graph as queries
from contextplane.arc.service.source_admission import (
    ApprovalProof,
    SourceAdmissionRefused,
    SourceAdmissionService,
    SourceEvidence,
)
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.types import Clock

#: The media type of the admitted projection. A concrete type rather than a
#: bare `application/json`: what is admitted is one specific profile shape,
#: and a directive that anchors into it is entitled to know which.
GRAPH_PROMOTION_CONTENT_TYPE = "application/vnd.contextplane.graph-promotion+json"

#: Evidence kinds whose ref identifies an immutable upstream revision, in
#: preference order. A commit hash pins content exactly; a document revision
#: pins it as well as the upstream system does. The remaining kinds
#: (`session_event`, `curator`, `work_item`, `incident`, `connector_run`)
#: identify an *event*, not a revision of the governed text, so none of them
#: can serve as a source revision locator.
_LOCATOR_KINDS = ("commit", "document_revision")


@dataclasses.dataclass(frozen=True)
class GraphPromotionAdmission:
    """One request to admit a promoted claim as source evidence."""

    claim_id: uuid.UUID
    source_system: str
    review_expires_at: datetime.datetime
    idempotency_key: str


def _canonical_projection(
    claim: queries.PromotedClaimRow,
    provenance: tuple[queries.ProvenanceRow, ...],
    *,
    promoted_by_subject: str,
) -> bytes:
    """The admitted bytes: a sorted, compact JSON projection of the promotion.

    Sorted keys and `(',', ':')` separators, UTF-8, no trailing newline --
    the same determinism the rest of this surface's digests depend on. Two
    admissions of an unchanged promotion produce identical bytes and so an
    identical digest, which is what lets the idempotency payload digest mean
    what it says.
    """
    projection: dict[str, Any] = {
        "profile": "contextplane.graph_promotion.v1",
        "claim": {
            "claim_id": str(claim.claim_id),
            "subject_reference": claim.subject_reference,
            "subject_entity_id": str(claim.subject_entity_id) if claim.subject_entity_id else None,
            "predicate": claim.predicate,
            "value": claim.value_jsonb,
            "source_authority": claim.source_authority,
            "asserted_valid_from": claim.asserted_valid_from.isoformat(),
            "asserted_valid_to": (claim.asserted_valid_to.isoformat() if claim.asserted_valid_to else None),
        },
        "evidence": [
            {
                "kind": row.evidence_kind,
                "ref": row.evidence_ref,
                "excerpt": row.evidence_excerpt,
                "derivation": row.derivation,
            }
            for row in provenance
        ],
        "promotion": {
            "promotion_id": str(claim.promotion_id),
            "promoted_at": claim.promoted_at.isoformat(),
            "promoted_by": promoted_by_subject,
            "target_kind": claim.target_kind,
            "created_row_id": str(claim.created_row_id),
        },
    }
    return json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _locator(claim: queries.PromotedClaimRow, provenance: tuple[queries.ProvenanceRow, ...]) -> str:
    """The upstream revision this promotion stands on.

    Refuses rather than inventing one. A claim whose only evidence is a
    session event or a curator action records that somebody said something,
    not which revision of a governed document said it, and a source evidence
    row whose locator cannot be resolved upstream is not evidence of a
    source at all.
    """
    for kind in _LOCATOR_KINDS:
        for row in provenance:
            if row.evidence_kind == kind:
                return f"{kind}:{row.evidence_ref}"
    raise SourceAdmissionRefused(
        f"claim {claim.claim_id} has no commit or document_revision evidence to serve as a source revision locator"
    )


class GraphPromotionAdmissionService:
    """Admits a promoted canonical claim as ARC source evidence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[Any],
        *,
        admission: SourceAdmissionService,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._admission = admission
        self._authorization = authorization
        self._clock = clock

    async def admit_promoted_claim(
        self,
        ctx: ArcRequestContext,
        request: GraphPromotionAdmission,
    ) -> SourceEvidence:
        self._authorization.assert_can_write_artifact(
            ctx, ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=ctx.tenant_id)
        )

        now = self._clock.now()
        if request.review_expires_at <= now:
            raise SourceAdmissionRefused(
                f"review_expires_at {request.review_expires_at.isoformat()} is not in the future"
            )

        async with self._session_factory() as session:
            claim = await queries.load_promoted_claim(session, claim_id=request.claim_id, tenant_id=ctx.tenant_id)
            if claim is None:
                raise SourceAdmissionRefused(f"claim {request.claim_id} is not a promoted claim owned by this tenant")
            if claim.reversed_at is not None:
                raise SourceAdmissionRefused(
                    f"promotion {claim.promotion_id} was reversed at {claim.reversed_at.isoformat()}"
                )
            if claim.promoted_by is None:
                raise SourceAdmissionRefused(
                    f"promotion {claim.promotion_id} records no promoting actor to attribute the approval to"
                )
            if claim.author_actor_id is not None and claim.promoted_by == claim.author_actor_id:
                raise SourceAdmissionRefused(
                    f"promotion {claim.promotion_id} was performed by the claim's own author; "
                    "source evidence requires a second actor"
                )

            provenance = await queries.load_claim_provenance(session, claim.claim_id)
            promoted_by_subject = await queries.load_actor_subject(session, claim.promoted_by)

        subject = promoted_by_subject or str(claim.promoted_by)
        content_bytes = _canonical_projection(claim, provenance, promoted_by_subject=subject)
        locator = _locator(claim, provenance)

        arc_claim = {
            "profile": SOURCE_APPROVAL_CLAIM_PROFILE,
            "source_system": request.source_system,
            "source_revision_locator": locator,
            "source_content_digest_algorithm": "sha256",
            "source_content_digest": _sha256_hex(content_bytes),
            "source_content_type": GRAPH_PROMOTION_CONTENT_TYPE,
            "approval_locator": f"promotion:{claim.promotion_id}",
            "approving_authority_issuer": ctx.oidc_issuer,
            "approving_authority_subject": subject,
            "approval_scope": f"claim:{claim.claim_id}",
            "approved_at": _rfc3339(claim.promoted_at),
            "expires_at": _rfc3339(request.review_expires_at),
        }

        return await self._admission.finish_admission(
            ctx,
            claim=arc_claim,
            verifier_id=f"promotion:{claim.promotion_id}",
            proof=ApprovalProof(verification_method="graph_promotion"),
            idempotency_key=request.idempotency_key,
            content_bytes=content_bytes,
            content_digest=_sha256_hex(content_bytes),
            content_bytes_len=len(content_bytes),
            admission_method=vocab.GRAPH_PROMOTION,
            connector_id=None,
            policy_id=None,
            owning_scope="tenant",
            tenant_id=ctx.tenant_id,
        )


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rfc3339(moment: datetime.datetime) -> str:
    """Render *moment* in the exact shape the claim profile's pattern fixes.

    `isoformat()` emits `+00:00` for an aware UTC datetime, which that
    pattern rejects -- it requires a literal `Z` -- and emits microseconds
    only when non-zero, which the pattern permits either way.
    """
    utc = moment.astimezone(datetime.UTC).replace(tzinfo=None)
    return utc.isoformat(timespec="microseconds" if utc.microsecond else "seconds") + "Z"


__all__ = [
    "GRAPH_PROMOTION_CONTENT_TYPE",
    "GraphPromotionAdmission",
    "GraphPromotionAdmissionService",
]
