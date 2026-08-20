"""Parametrized SQL for reading a promoted claim as admissible source evidence.

Separate from `source_admission.py`'s query module because it reads a
different set of tables owned by a different subdomain: `memory_claims`,
`memory_promotion_journal`, and `memory_claim_provenance` belong to the
memory service, and ARC only ever reads them. Keeping the two apart makes
that direction visible -- nothing here writes, and no ARC table is named in
this file.

Every function takes an already-open `AsyncSession` and opens no transaction
of its own, matching the sibling module's contract.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class PromotedClaimRow:
    """A claim and the promotion that moved it onto the canonical graph.

    `author_actor_id` and `promoted_by` are both carried so the caller can
    enforce separation of duties without a second query. `reversed_at` is
    carried rather than filtered in SQL so the refusal message can say that
    a promotion existed and was withdrawn, which is a different operator
    problem from one that never happened.
    """

    claim_id: uuid.UUID
    owning_tenant_id: uuid.UUID | None
    author_actor_id: uuid.UUID | None
    subject_entity_id: uuid.UUID | None
    subject_reference: str
    predicate: str
    value_jsonb: Any
    claim_status: str
    source_authority: str
    asserted_valid_from: datetime.datetime
    asserted_valid_to: datetime.datetime | None
    promotion_id: uuid.UUID
    promoted_at: datetime.datetime
    promoted_by: uuid.UUID | None
    reversed_at: datetime.datetime | None
    created_row_id: uuid.UUID
    target_kind: str


@dataclasses.dataclass(frozen=True)
class ProvenanceRow:
    evidence_kind: str
    evidence_ref: str
    evidence_excerpt: str | None
    derivation: str


async def load_promoted_claim(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> PromotedClaimRow | None:
    """Load *claim_id* with its most recent promotion, scoped to *tenant_id*.

    Scoped on `owning_tenant_id` -- the tenant that owns the claim's subject,
    not whoever authored it. A claim about another tenant's subject is that
    tenant's to govern, and this is the same boundary promotion itself draws
    when it routes a cross-tenant claim to the owner as a proposal instead of
    writing their graph.

    Most recent by `promoted_at`: a claim promoted, reversed, and promoted
    again is governed by its current promotion, and ordering by the journal's
    own timestamp is what makes "current" well defined.
    """
    row = (
        await session.execute(
            text(
                "SELECT c.claim_id, c.owning_tenant_id, c.author_actor_id, c.subject_entity_id,"
                "       c.subject_reference, c.predicate, c.value_jsonb, c.status AS claim_status,"
                "       c.source_authority, c.asserted_valid_from, c.asserted_valid_to,"
                "       j.promotion_id, j.promoted_at, j.promoted_by, j.reversed_at,"
                "       j.created_row_id, j.target_kind "
                "FROM memory_claims c "
                "JOIN memory_promotion_journal j ON j.claim_id = c.claim_id "
                "WHERE c.claim_id = :claim_id AND c.owning_tenant_id = :tenant_id "
                "ORDER BY j.promoted_at DESC "
                "LIMIT 1"
            ),
            {"claim_id": claim_id, "tenant_id": tenant_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return PromotedClaimRow(
        claim_id=row.claim_id,
        owning_tenant_id=row.owning_tenant_id,
        author_actor_id=row.author_actor_id,
        subject_entity_id=row.subject_entity_id,
        subject_reference=row.subject_reference,
        predicate=row.predicate,
        value_jsonb=row.value_jsonb,
        claim_status=row.claim_status,
        source_authority=row.source_authority,
        asserted_valid_from=row.asserted_valid_from,
        asserted_valid_to=row.asserted_valid_to,
        promotion_id=row.promotion_id,
        promoted_at=row.promoted_at,
        promoted_by=row.promoted_by,
        reversed_at=row.reversed_at,
        created_row_id=row.created_row_id,
        target_kind=row.target_kind,
    )


async def load_claim_provenance(session: AsyncSession, claim_id: uuid.UUID) -> tuple[ProvenanceRow, ...]:
    """Every provenance row for *claim_id*, in a deterministic order.

    Ordered by `(evidence_kind, evidence_ref)` rather than by `recorded_at`:
    two rows recorded in the same transaction share a timestamp, and the
    locator this feeds has to be the same on every admission of the same
    claim or the idempotency payload digest moves under it.
    """
    rows = (
        await session.execute(
            text(
                "SELECT evidence_kind, evidence_ref, evidence_excerpt, derivation "
                "FROM memory_claim_provenance WHERE claim_id = :claim_id "
                "ORDER BY evidence_kind, evidence_ref"
            ),
            {"claim_id": claim_id},
        )
    ).all()
    return tuple(
        ProvenanceRow(
            evidence_kind=row.evidence_kind,
            evidence_ref=row.evidence_ref,
            evidence_excerpt=row.evidence_excerpt,
            derivation=row.derivation,
        )
        for row in rows
    )


async def load_actor_subject(session: AsyncSession, actor_id: uuid.UUID) -> str | None:
    """The OIDC subject for *actor_id*, for the approving-authority field.

    `actors.oidc_subject` is NOT NULL, so a row always yields one; `None`
    means no actor row, which the caller renders as the actor UUID rather
    than refusing -- an internal identifier is still an identification, and
    a journal row can outlive the actor record it points at.
    """
    row = (
        await session.execute(
            text("SELECT oidc_subject FROM actors WHERE actor_id = :actor_id"),
            {"actor_id": actor_id},
        )
    ).one_or_none()
    return None if row is None else row.oidc_subject


__all__ = [
    "PromotedClaimRow",
    "ProvenanceRow",
    "load_actor_subject",
    "load_claim_provenance",
    "load_promoted_claim",
]
