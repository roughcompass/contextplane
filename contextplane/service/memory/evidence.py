"""Assembling an evidence chain, and refusing the rows that may not be in one.

Between the ledger and the extractor there has to be something that decides *what
counts as input*. This module is that something. It selects the feedback and
signals a derivation may read, validates the chain that comes back, and converts
it into the provenance shape the one claim writer accepts.

**Eligibility is a query predicate, not a caller's promise.** The derivation
service validates the evidence it is handed and cannot know where the caller got
it; the feedback table records `learning_eligible` and the signal ledger records
supersession, and both are read here in the `WHERE` clause rather than filtered in
Python afterwards. A read that loads ineligible rows and then drops them has
already loaded them, and the drop is one refactor from disappearing.

**A diagnostic observation is never eligible, and that is enforced twice.** The
schema forbids a learning-eligible diagnostic, and this module's predicate
excludes the kind outright. The duplication is deliberate: the schema stops a bad
row existing, and this stops a bad row being *selected* even if one somehow does —
they fail differently, and the second is the one that keeps an unattributable
complaint out of a derivation when the first has been amended.

**An incomplete chain is refused, not completed.** Evidence missing its authority,
or a receipt item citing no receipt, is a chain nobody can check later; inventing
the missing half would produce provenance that looks complete and is not. The
refusal names what was missing.

**Nothing here writes a claim, and nothing here decides one is worth staging.** It
answers "what may this derivation read" and "is this chain checkable". Whether the
resulting assertion becomes a staged claim is the claim path's decision, made
against its own rules.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import SOURCE_AUTHORITY_RANK
from contextplane.service.memory.claim_authority import Evidence as ProvenanceEvidence
from contextplane.service.memory.derivation import Evidence, weakest_authority

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import TenantContext

#: The feedback kind that may never be learned from. Excluded by the selection
#: predicate as well as by the schema; see the module docstring for why both.
KIND_DIAGNOSTIC: Final[str] = "diagnostic_observation"

#: How an evidence item is spelled as claim provenance. The claim writer takes a
#: `kind` and an opaque `ref`; these are the kinds a derived claim may cite.
PROVENANCE_KINDS: Final[frozenset[str]] = frozenset(
    {"signal", "receipt", "receipt_item", "external_reference", "checkpoint"}
)


#: The two eligibility reads, written out rather than assembled. Both carry the
#: same three conditions; the second adds the receipt filter.
_ELIGIBLE_FEEDBACK = text(
    "SELECT feedback_id, kind, rating, receipt_id, receipt_item_id FROM context_feedback"
    " WHERE tenant_id = :tid AND learning_eligible AND kind <> :diagnostic"
    " ORDER BY created_at DESC LIMIT :limit"
)

_ELIGIBLE_FEEDBACK_FOR_RECEIPT = text(
    "SELECT feedback_id, kind, rating, receipt_id, receipt_item_id FROM context_feedback"
    " WHERE tenant_id = :tid AND learning_eligible AND kind <> :diagnostic AND receipt_id = :rid"
    " ORDER BY created_at DESC LIMIT :limit"
)


class EvidenceRefused(ValidationError):
    """A chain that cannot be assembled or cannot be checked."""


@dataclasses.dataclass(frozen=True)
class EligibleFeedback:
    """One feedback row a derivation is allowed to read."""

    feedback_id: uuid.UUID
    kind: str
    rating: str
    receipt_id: uuid.UUID | None
    receipt_item_id: str | None


class EvidenceAssembler:
    """Selects what a derivation may read, and shapes what it produces.

    Holds only a session factory: the eligibility rules are the database's own
    columns, so there is no policy here to configure and no state to carry
    between calls.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def eligible_feedback(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> tuple[EligibleFeedback, ...]:
        """Feedback this tenant may learn from, newest first.

        Three conditions, all in the predicate: the tenant owns it, it is marked
        learning-eligible, and it is not a diagnostic observation. The third is
        redundant against a correct schema and is here anyway — a predicate that
        relies on another table's CHECK for its own correctness is one amendment
        away from being wrong.
        """
        # Two whole statements rather than one assembled from fragments. The
        # fragments would all have been literals under this module's control, and
        # the shape is still the one that goes wrong the first time a predicate is
        # built from something a caller supplied -- so the optional filter is a
        # second query, not a string that grows.
        statement = _ELIGIBLE_FEEDBACK if receipt_id is None else _ELIGIBLE_FEEDBACK_FOR_RECEIPT
        params: dict[str, object] = {"tid": ctx.tenant_id, "diagnostic": KIND_DIAGNOSTIC, "limit": limit}
        if receipt_id is not None:
            params["rid"] = receipt_id

        async with self._session_factory() as session:
            rows = (await session.execute(statement, params)).all()
        return tuple(
            EligibleFeedback(
                feedback_id=row.feedback_id,
                kind=row.kind,
                rating=row.rating,
                receipt_id=row.receipt_id,
                receipt_item_id=row.receipt_item_id,
            )
            for row in rows
        )

    async def eligible_signals(
        self,
        ctx: TenantContext,
        *,
        limit: int = 100,
    ) -> tuple[uuid.UUID, ...]:
        """Signals a derivation may read: this tenant's, not revoked, not superseded.

        Revocation and supersession are different states with the same effect
        here and different effects elsewhere — a revoked signal was withdrawn, a
        superseded one is still true but overtaken — so both are excluded and
        neither is collapsed into the other.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT signal_id FROM external_signals"
                        " WHERE tenant_id = :tid AND revoked_at IS NULL AND NOT superseded_for_learning"
                        " ORDER BY ingested_at DESC LIMIT :limit"
                    ),
                    {"tid": ctx.tenant_id, "limit": limit},
                )
            ).all()
        return tuple(row.signal_id for row in rows)


def validate_chain(evidence: Sequence[Evidence]) -> None:
    """Refuse a chain nobody could check later.

    Each item must carry an authority on the ladder and the pointers its kind
    requires. `Evidence` enforces most of that at construction; this re-checks the
    assembled chain because a chain is more than its items — an empty one is
    itself the failure, and the caller that built it should hear so here rather
    than at the insert.
    """
    if not evidence:
        message = "an evidence chain with nothing in it cannot support an assertion"
        raise EvidenceRefused(message)
    for item in evidence:
        if item.source_authority not in SOURCE_AUTHORITY_RANK:
            message = f"evidence carries an authority outside the ladder: {item.source_authority!r}"
            raise EvidenceRefused(message)
        if item.kind not in PROVENANCE_KINDS:
            message = f"evidence kind {item.kind!r} has no provenance spelling"
            raise EvidenceRefused(message)


def as_provenance(evidence: Sequence[Evidence]) -> tuple[ProvenanceEvidence, ...]:
    """The chain as the claim writer's own provenance shape.

    The claim writer takes a kind and an opaque ref; this is where a typed
    evidence item becomes one. The ref is built from the pointer the kind
    requires, so a reader can resolve it back without knowing which extractor
    produced it.
    """
    validate_chain(evidence)
    return tuple(ProvenanceEvidence(kind=item.kind, ref=_ref_for(item), excerpt=item.excerpt) for item in evidence)


def _ref_for(item: Evidence) -> str:
    """The resolvable pointer for one evidence item.

    A receipt item is spelled as the pair, not the item alone: the item id means
    nothing without the receipt it is on, and a ref that cannot be resolved back
    is provenance in name only.
    """
    if item.kind == "signal":
        return f"signal:{item.signal_id}"
    if item.kind == "receipt":
        return f"receipt:{item.receipt_id}"
    if item.kind == "receipt_item":
        return f"receipt_item:{item.receipt_id}:{item.receipt_item_id}"
    if item.kind == "external_reference":
        return f"external_reference:{item.reference_id}"
    return f"checkpoint:{item.checkpoint_id}@{item.checkpoint_digest}"


def ceiling_for(evidence: Sequence[Evidence]) -> str:
    """The strongest authority a claim staged from this chain may carry.

    Re-exported from the derivation module rather than recomputed, so the
    staging path and the extractor cannot disagree about what the evidence
    licenses — two implementations of one ceiling is how a claim ends up carrying
    an authority one of them would have refused.
    """
    return weakest_authority(evidence)


__all__ = [
    "KIND_DIAGNOSTIC",
    "PROVENANCE_KINDS",
    "EligibleFeedback",
    "EvidenceAssembler",
    "EvidenceRefused",
    "as_provenance",
    "ceiling_for",
    "validate_chain",
]
