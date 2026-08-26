"""What each arm's rows look like on the wire, and what trust each carries.

`arms.py` states that it makes exactly two decisions: which service answers each
block, and what trust each answer carries. This is the second one, and it is
here rather than there because the two have nothing to say to each other -- a
payload shape is a statement about one source's rows, and it changes when that
source changes, not when the composition does.

Nothing here reads. Every function takes a row a service already returned and
turns it into an item body or a trust record, so each one is reachable from a
literal and every branch is testable without a database.

**Trust labels are chosen per source, each for a stated reason**, and the
reasons are in the functions rather than in a table: a table of eight labels by
four sources is a thing nobody reads, and each of these choices is the kind that
gets quietly widened by whoever needs one more source to look trustworthy.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, cast

from contextplane.context.assembler import ArmOutcome, Exclusion, contextual_item, ordered_items
from contextplane.context.schemas.envelope import BLOCK_ARC
from contextplane.context.schemas.trust import (
    TRUST_ASSERTED,
    TRUST_ATTESTED,
    TRUST_OBSERVED,
    AssertionKind,
    Classification,
    TrustMetadataV1,
)

if TYPE_CHECKING:
    from contextplane.service.memory.claim_serving import ServedClaim
    from contextplane.types import SearchResult

# Where each arm's rows come from, as stable system identifiers. These appear in
# every receipt item id, so renaming one silently renames every item a receipt
# ever pointed at -- they are part of the contract, not labels.
CANONICAL_SOURCE = "catalog"
ARC_SOURCE = "arc-receipt"
CLAIMS_SOURCE = "living-memory"

#: Who stands behind an ARC directive. Distinct from `ARC_SOURCE`, which names
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


def canonical_payload(result: SearchResult) -> dict[str, object]:
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
        "score": result.fused_rank_score,
        "matching_facts": [
            {"fact_id": str(fact.fact_id), "category": fact.category, "body": fact.body}
            for fact in result.matching_facts
        ],
    }


def arc_outcome(receipt: dict[str, object]) -> ArmOutcome:
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
                source=ARC_SOURCE,
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
        source=ARC_SOURCE,
        assertion_kind="policy",
        authority=f"{_ARC_AUTHORITY_PREFIX}:{attestation_id}" if attested else _ARC_AUTHORITY_PREFIX,
        freshness=evaluated_at,
        mutability="immutable",
        attribution=None,
        classification=_INTERNAL,
    )


def claim_payload(claim: ServedClaim) -> dict[str, object]:
    """One recalled claim, with the handles its evidence resolves through.

    Citations carry kind and ref rather than inlined excerpts: the handle is
    what the provenance table is keyed by, and an excerpt in the envelope would
    grow with every claim while answering a question the caller has not asked
    yet.
    """
    return {
        "claim_id": str(claim.claim_id),
        "subject_entity_id": str(claim.subject_entity_id),
        # The name beside the id, because a reader of this block — an agent, most
        # of the time — cannot resolve a UUID and should not have to compare two
        # of them by eye to decide whether a claim is about the thing they asked
        # about. `None` where the reference has not resolved, which is a state
        # rather than a gap.
        "subject_name": claim.subject_name,
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


def claim_trust(claim: ServedClaim) -> TrustMetadataV1:
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
        source=CLAIMS_SOURCE,
        assertion_kind=_ASSERTION_KIND_BY_CATEGORY.get(claim.claim_category, _ASSERTION_KIND_FALLBACK),
        authority=claim.authority,
        freshness=claim.as_of,
        mutability="mutable",
        attribution=None,
        classification=_INTERNAL,
    )


__all__ = [
    "ARC_SOURCE",
    "CANONICAL_SOURCE",
    "CLAIMS_SOURCE",
    "arc_outcome",
    "canonical_payload",
    "claim_payload",
    "claim_trust",
]
