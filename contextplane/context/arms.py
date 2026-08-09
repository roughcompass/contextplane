"""Composing the four arms one context resolution reads, over the services that own them.

The assembler decides what four blocks *mean*. It takes arms as injected
callables and deliberately knows nothing about where their rows come from, which
is what lets every one of its failure paths be reachable from a fixture. This
module is the other half of that split: it owns which service answers each
block, and what trust each answer carries. Those are the only two decisions made
here, and this is the only place either is made.

**Every request gets all four arms.** An arm missing from the mapping and an arm
that ran and broke are the same value to the assembler -- a failed block -- and
only one of them is ever true. So this module never omits a key, and never
substitutes an empty answer for a broken one. Where a source genuinely has
nothing to say, the arm says so; where it could not be asked, it raises and the
block fails with a reason.

**The ARC arm serves an attested resolution; it does not perform one.** Which
governance directives apply is decided by the ARC resolution path, which attests
its answer and records a receipt naming the exact revisions it served. This arm
reads that receipt back. Deciding applicability again here would make context
resolution a second governance authority whose answer could disagree with the
attested one -- and the disagreement would be invisible, because both would look
like ARC. A request naming no receipt gets an *empty* ARC block: no resolution
was named, which is a complete answer rather than a failure.

**Withheld is not missing.** A directive the resolution omitted, or one whose
source this caller's audience does not permit, is recorded as an exclusion rather
than dropped. The assembler degrades a block that withheld something, so a reader
can tell "there is nothing" from "there is something you may not see" -- the
second is the one that tells them to go and ask for access.

**Trust labels are chosen per source, each for a stated reason.** Every
non-canonical item carries all eight. Canonical items carry none: the canonical
block is the registry's own answer, and attributing it would invite the question
of whether some other authority could have supplied it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Any, cast

from contextplane.context import queries as context_queries
from contextplane.context.assembler import (
    ArmOutcome,
    Exclusion,
    canonical_item,
    contextual_item,
    ordered_items,
)
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_WORKSPACE,
)
from contextplane.context.schemas.trust import (
    TRUST_ASSERTED,
    TRUST_ATTESTED,
    TRUST_OBSERVED,
    AssertionKind,
    Classification,
    TrustMetadataV1,
)
from contextplane.service.memory.claim_serving import ClaimQuery
from contextplane.types import TemporalFilter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.arc import ArcRequestContext, ReceiptReader
    from contextplane.context.assembler import ContextArm
    from contextplane.context.schemas.trust import ExternalReferenceV1
    from contextplane.service.memory.claim_serving import ClaimServingService, ServedClaim
    from contextplane.service.retrieval import RetrievalService
    from contextplane.types import SearchResult, TenantContext
    from contextplane.workspaces.recall import WorkspaceRecall

#: The most items any one arm returns before reporting itself truncated. The
#: assembler applies its own cap on top; this one bounds the *read*, so a caller
#: cannot make one arm decide how much work every other request queues behind.
DEFAULT_ARM_LIMIT = 25

# Where each arm's rows come from, as stable system identifiers. These appear in
# every receipt item id, so renaming one silently renames every item a receipt
# ever pointed at -- they are part of the contract, not labels.
_CANONICAL_SOURCE = "catalog"
_ARC_SOURCE = "arc-receipt"
_CLAIMS_SOURCE = "living-memory"

#: Who stands behind an ARC directive. Distinct from `_ARC_SOURCE`, which names
#: the read path: the receipt relays a directive whose weight comes from the
#: attestation over the resolution, and conflating relay with endorsement is
#: exactly what the two fields exist to keep apart.
_ARC_AUTHORITY_PREFIX = "arc-attestation"

#: A resolution that reports anything other than `ready` answered incompletely.
#: The arm still returns what the receipt holds -- the directives it names were
#: really selected -- but the block must not read as whole.
_ARC_STATUS_READY = "ready"

# What kind of statement each claim category makes. An agent that cannot tell a
# measurement from an intention will plan against a wish, so the mapping is
# explicit rather than defaulted for all categories at once.
#
# The four descriptive categories state something that is the case about an
# entity, which is `fact`. A decision rationale states why a choice was made,
# which is closer to `intent` than to a measurement of anything. A session
# summary is a note *about* work rather than a statement about the entity, which
# is `annotation`.
_ASSERTION_KIND_BY_CATEGORY: dict[str, AssertionKind] = {
    "interface_contract": "fact",
    "dependency": "fact",
    "ownership_stewardship": "fact",
    "operational_lifecycle": "fact",
    "decision_rationale": "intent",
    "session_summary": "annotation",
}

#: An unrecognised category is not promoted to a fact. `annotation` is the
#: weakest kind, and reading an unknown statement as weaker than it might be is
#: the error that costs least.
_ASSERTION_KIND_FALLBACK: AssertionKind = "annotation"

#: Handling class for governed operational content that is neither published nor
#: legally controlled. Both the ARC and claim arms label at this level: their
#: content is tenant-internal, and anything the caller may not read has already
#: been removed by the owning service rather than downgraded by a label here.
_INTERNAL: Classification = "internal"


class ContextArms:
    """Builds the four arms for one resolution, over the services that own them.

    One instance per deployment, holding the collaborators rather than the
    request: every method takes the caller's identity and bounds, and returns a
    zero-argument callable, because the assembler runs arms concurrently under
    its own timeout and must not need to know what any of them takes.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        retrieval: RetrievalService,
        claims: ClaimServingService,
        arc_receipts: ReceiptReader,
        recall: WorkspaceRecall,
    ) -> None:
        self._session_factory = session_factory
        self._retrieval = retrieval
        self._claims = claims
        self._arc_receipts = arc_receipts
        self._recall = recall

    def for_request(
        self,
        ctx: TenantContext,
        *,
        query: str,
        moment: datetime.datetime,
        arc: ArcRequestContext | None = None,
        arc_receipt_id: uuid.UUID | None = None,
        subject_entity_id: uuid.UUID | None = None,
        task_ids: tuple[uuid.UUID, ...] = (),
        workspace_term: str | None = None,
        workspace_reference: ExternalReferenceV1 | None = None,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> dict[str, ContextArm]:
        """All four arms for one request, keyed by block name.

        Returns a mapping the assembler can use directly. It is a plain dict
        rather than four positional returns so a caller cannot reorder the
        blocks by accident, and so the keys are the same constants the envelope
        validates against.
        """
        return {
            BLOCK_CANONICAL: self.canonical_arm(ctx, query=query, moment=moment, limit=limit),
            BLOCK_ARC: self.arc_arm(arc, receipt_id=arc_receipt_id),
            BLOCK_OBSERVED_CLAIMS: self.observed_claims_arm(
                ctx, subject_entity_id=subject_entity_id, moment=moment, limit=limit
            ),
            BLOCK_WORKSPACE: self.workspace_arm(
                ctx,
                term=workspace_term if workspace_term is not None else query,
                reference=workspace_reference,
                task_ids=task_ids,
                moment=moment,
                limit=limit,
            ),
        }

    # -- canonical ---------------------------------------------------------

    def canonical_arm(
        self,
        ctx: TenantContext,
        *,
        query: str,
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> ContextArm:
        """The registry's own answer, from the catalog it owns.

        A search failure is deliberately not caught. The canonical arm failing
        blocks the whole response, and that is the intended outcome: the
        surrounding context without the thing it surrounds reads as the whole
        picture rather than as a gap.
        """

        async def arm() -> ArmOutcome:
            # One more than the bound, so "there is more" is answered by the
            # read rather than inferred from a page that happens to be full.
            results = await self._retrieval.search(ctx, query, limit + 1, TemporalFilter(as_of=moment))
            kept = results[:limit]
            items = [
                canonical_item(
                    source=_CANONICAL_SOURCE,
                    item_key=str(result.entity.entity_id),
                    payload=_canonical_payload(result),
                )
                for result in kept
            ]
            return ArmOutcome(
                items=ordered_items(items),
                truncated=len(results) > limit,
                # The catalog is the registry's own live state, so this answer is
                # exactly as fresh as the instant it was read at. `None` would
                # claim the arm cannot report freshness, which is not true here
                # and would suppress a staleness bound the caller asked for.
                fresh_as_of=moment,
            )

        return arm

    # -- ARC ---------------------------------------------------------------

    def arc_arm(self, arc: ArcRequestContext | None, *, receipt_id: uuid.UUID | None) -> ContextArm:
        """The directives an attested resolution already served to this caller.

        Both arguments are optional and mean one thing together: a request that
        names no ARC resolution has no ARC context, which is an empty block. It
        is not a failure -- nothing was asked of ARC and nothing broke -- and it
        is not silently dropped either, because the block is still present and
        still says which of the four states it is in.
        """

        async def arm() -> ArmOutcome:
            if arc is None or receipt_id is None:
                return ArmOutcome()
            return _arc_outcome(await self._arc_receipts.get_receipt(arc, receipt_id))

        return arm

    # -- observed claims ---------------------------------------------------

    def observed_claims_arm(
        self,
        ctx: TenantContext,
        *,
        subject_entity_id: uuid.UUID | None = None,
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> ContextArm:
        """Claims recalled from living memory, weighed but not promoted.

        A structural read rather than a ranked one. Ranking would make an
        answer's contents depend on a similarity score nobody asked for, and a
        receipt that cannot be reproduced from its own inputs is decorative.
        """
        # One more than the bound for the same reason the canonical arm asks for
        # it, clamped to what the query type accepts so an over-large caller
        # bound is refused by this arm rather than by the service underneath it.
        bounded = min(limit + 1, ClaimQuery.MAX_LIMIT)

        async def arm() -> ArmOutcome:
            served = await self._claims.query(
                ctx,
                ClaimQuery(subject_entity_id=subject_entity_id, as_of=moment, limit=bounded),
            )
            kept = served[:limit]
            items = [
                contextual_item(
                    block=BLOCK_OBSERVED_CLAIMS,
                    source=_CLAIMS_SOURCE,
                    item_key=str(claim.claim_id),
                    payload=_claim_payload(claim),
                    trust=_claim_trust(claim),
                )
                for claim in kept
            ]
            return ArmOutcome(
                items=ordered_items(items),
                truncated=len(served) > limit,
                # Same reasoning as the canonical arm: a live read is as fresh as
                # its own instant. Each claim additionally carries its own
                # `as_of` in trust metadata, which is the age of the *statement*
                # rather than of the read.
                fresh_as_of=moment,
            )

        return arm

    # -- workspace ---------------------------------------------------------

    def workspace_arm(
        self,
        ctx: TenantContext,
        *,
        term: str | None = None,
        reference: ExternalReferenceV1 | None = None,
        task_ids: tuple[uuid.UUID, ...] = (),
        moment: datetime.datetime,
        limit: int = DEFAULT_ARM_LIMIT,
    ) -> ContextArm:
        """Checkpoints from tasks this caller participates in.

        Three reads, one block. A named external reference is the most specific
        thing a caller can supply, so it wins; a search term narrows lexically;
        with neither, the block is every checkpoint the caller's grants reach.
        All three resolve the audience inside the query rather than filtering
        after it, which is why none of them is assembled here.
        """
        actor = str(ctx.actor_id)

        if reference is not None:
            return self._recall.reference_arm(
                tenant_id=ctx.tenant_id,
                actor_id=actor,
                source_system=reference.source_system,
                source_namespace=reference.source_namespace,
                kind=reference.kind,
                external_id=reference.external_id,
                moment=moment,
                limit=limit,
            )

        if term is not None and term.strip():
            return self._recall.lexical_arm(
                tenant_id=ctx.tenant_id,
                actor_id=actor,
                term=term,
                moment=moment,
                limit=limit,
            )

        async def arm() -> ArmOutcome:
            async with self._session_factory() as session:
                return await context_queries.workspace_arm(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=actor,
                    moment=moment,
                    task_ids=task_ids,
                    limit=limit,
                )

        return arm


# -- payloads and trust ----------------------------------------------------


def _canonical_payload(result: SearchResult) -> dict[str, object]:
    """One catalog entity, with the facts that matched.

    The matching facts travel with the entity rather than as items of their own:
    a fact is evidence for why this entity is in the answer, and promoting it to
    a peer item would make one entity look like several.
    """
    return {
        "entity_id": str(result.entity.entity_id),
        "entity_type": result.entity.entity_type,
        "name": result.entity.name,
        "external_id": result.entity.external_id,
        "is_active": result.entity.is_active,
        "score": result.score,
        "matching_facts": [
            {"fact_id": str(fact.fact_id), "category": fact.category, "body": fact.body}
            for fact in result.matching_facts
        ],
    }


def _arc_outcome(receipt: dict[str, object]) -> ArmOutcome:
    """One receipt, read as the ARC block.

    Three things come out of the same record and must not be conflated: the
    directives that were served, the ones that were withheld and why, and
    whether the resolution itself was whole.
    """
    evaluated_at = datetime.datetime.fromisoformat(cast("str", receipt["evaluated_at"]))
    status = cast("str", receipt["resolution_status"])
    attestation_id = cast("str", receipt["attestation_id"])
    integrity_state = cast("str", receipt["integrity_state"])
    selected = cast("list[dict[str, Any]]", receipt.get("selected") or [])

    trust = _arc_trust(attestation_id=attestation_id, integrity_state=integrity_state, evaluated_at=evaluated_at)

    items = []
    exclusions = []
    for row in selected:
        directive_id = str(row["directive_id"])

        if row.get("was_omitted"):
            exclusions.append(
                Exclusion(
                    item_key=directive_id,
                    reason=str(
                        row.get("omission_reason") or "the resolution omitted this directive without recording a reason"
                    ),
                )
            )
            continue

        # Redaction is the owning service's decision, already applied to the row
        # by the time it arrives here. Recording it as an exclusion rather than
        # returning a hollow item keeps "you may not see this" distinguishable
        # from "this directive has no source".
        if row.get("audience_redacted"):
            exclusions.append(
                Exclusion(
                    item_key=directive_id,
                    reason="this caller's audience does not permit the directive's source",
                )
            )
            continue

        items.append(
            contextual_item(
                block=BLOCK_ARC,
                source=_ARC_SOURCE,
                item_key=directive_id,
                payload=_arc_payload(row),
                trust=trust,
            )
        )

    return ArmOutcome(
        items=ordered_items(items),
        exclusions=tuple(exclusions),
        # The receipt's own instant, not the request's. An attested resolution
        # can be minutes or days old, and reporting it as fresh-now would defeat
        # every staleness bound a caller sets.
        fresh_as_of=evaluated_at,
        degraded_reason=_arc_degraded_reason(receipt, status),
    )


def _arc_degraded_reason(receipt: dict[str, object], status: str) -> str | None:
    """Why the attested resolution was less than whole, if it was.

    Taken from the receipt rather than recomputed. An explanation that
    contradicts the record it explains is worse than no explanation.
    """
    if status == _ARC_STATUS_READY:
        return None
    reasons = [
        *(cast("list[str]", receipt.get("blocked_reasons") or [])),
        *(cast("list[str]", receipt.get("degraded_reasons") or [])),
    ]
    detail = "; ".join(reasons) if reasons else "the receipt records no reason"
    return f"the attested resolution was {status}: {detail}"


def _arc_payload(row: dict[str, Any]) -> dict[str, object]:
    """One selected directive. Identity and locators, never prose.

    Full directive text is served by the ARC detail path, one item at a time,
    against a fresh authorization check. Inlining it here would hand every
    matched actor content only some of them are cleared to read, and would put
    the whole corpus in every envelope.
    """
    return {
        "directive_id": str(row["directive_id"]),
        "artifact_id": str(row["artifact_id"]),
        "revision_id": str(row["revision_id"]),
        "is_mandatory": bool(row.get("is_mandatory")),
        "source_locator": row.get("source_locator"),
        "source_revision_locator": row.get("source_revision_locator"),
        "content_digest": row.get("content_digest"),
    }


def _arc_trust(*, attestation_id: str, integrity_state: str, evaluated_at: datetime.datetime) -> TrustMetadataV1:
    """Trust metadata for one served directive.

    `attested` only when the receipt still carries both an attestation and a
    verifying integrity state. A resolution whose chain no longer verifies did
    still happen -- the directives it names were really selected -- but nothing
    stands behind it any more, so it drops to `asserted` rather than being
    withheld or silently kept at full weight.

    `immutable` because the selected row pins a revision id and a content
    digest: whatever the artifact says today, this is what was served.

    Attribution is absent, and that is a statement rather than a gap. The
    receipt names the actor who *resolved*, not the authority who authored or
    approved the directive, and attributing a policy to the agent that read it
    would be worse than admitting the read cannot say.
    """
    attested = bool(attestation_id.strip()) and integrity_state == "valid"
    return TrustMetadataV1(
        trust=TRUST_ATTESTED if attested else TRUST_ASSERTED,
        source=_ARC_SOURCE,
        assertion_kind="policy",
        authority=f"{_ARC_AUTHORITY_PREFIX}:{attestation_id}" if attested else _ARC_AUTHORITY_PREFIX,
        freshness=evaluated_at,
        mutability="immutable",
        attribution=None,
        classification=_INTERNAL,
    )


def _claim_payload(claim: ServedClaim) -> dict[str, object]:
    """One recalled claim, with the handles its evidence resolves through.

    Citations carry kind and ref rather than inlined excerpts: the handle is
    what the provenance table is keyed by, and an excerpt in the envelope would
    grow with every claim while answering a question the caller has not asked
    yet.
    """
    return {
        "claim_id": str(claim.claim_id),
        "subject_entity_id": str(claim.subject_entity_id),
        "predicate": claim.predicate,
        "value": claim.value,
        "category": claim.claim_category,
        "confidence": claim.confidence,
        "valid_from": claim.valid_from.isoformat(),
        "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
        "human_confirmed": claim.human_confirmed,
        "label": claim.label,
        "citations": [{"kind": citation.kind, "ref": citation.ref} for citation in claim.citations],
    }


def _claim_trust(claim: ServedClaim) -> TrustMetadataV1:
    """Trust metadata for one recalled claim.

    `asserted` once a person has confirmed it, `observed` until then. The
    difference is the whole point of confirmation: an unconfirmed claim is
    something the system noticed, and a confirmed one is something somebody
    stands behind. Neither is `attested` -- recall is not an attestation path,
    and promoting it to one here would let an observation reach an agent
    wearing the weight of a governed artifact.

    `mutable` because a claim can be confirmed, superseded, or decayed after
    this read. An agent caching it would outlive the confidence it cached.

    Attribution is absent: a served claim names the authority tier that scored
    it, not an actor who said it. Its citations are evidence handles rather
    than authorship, and reading one as an author would attribute a claim to
    whichever commit or session happened to be cited first.
    """
    return TrustMetadataV1(
        trust=TRUST_ASSERTED if claim.human_confirmed else TRUST_OBSERVED,
        source=_CLAIMS_SOURCE,
        assertion_kind=_ASSERTION_KIND_BY_CATEGORY.get(claim.claim_category, _ASSERTION_KIND_FALLBACK),
        authority=claim.authority,
        freshness=claim.as_of,
        mutability="mutable",
        attribution=None,
        classification=_INTERNAL,
    )


__all__ = [
    "BLOCK_NAMES",
    "DEFAULT_ARM_LIMIT",
    "ContextArms",
]
