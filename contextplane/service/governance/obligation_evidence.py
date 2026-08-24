"""Everything recorded about one obligation, exported as one bundle.

E4-T7. Unblocked by E4-T5d, which made the decision-named relationship
expressible: an obligation cites an incident, and a claim cites that same
incident as evidence. Three objects, one relationship — and that chain is what
makes an obligation-scoped bundle carry evidence rather than four fields its
detail route already returns.

## The scope is a join, and the join is the whole design

    obligation
      -> context_reference_bindings   (subject_type = 'reporting_obligation')
      -> context_external_references  (kind = 'incident')
      -> memory_claim_provenance      (evidence_kind = 'incident', evidence_ref = external_id)

Nothing is recomputed and nothing is inferred. An obligation that cites no
incident carries no claims, which is correct rather than empty-looking: it is a
nomination nobody has matched to a record yet, and 0076 made `summary` free text
precisely so that state is legal.

## What it does not carry, and why each is deliberate

**No claim content.** Claim ids, the incident they cite, and nothing they
assert. E4-T7b made the same choice for the same reason: an export that served
the content would be a second read path for material the rest of the system
governs carefully, and the question a bundle answers is *which* records bear on
this obligation.

**No deadline, no due date, no at-risk state, and no automatic materiality.**
Those need the ratified thresholds E4-T6 is blocked on, and a placeholder
presented as a compliance feature is worse than an absent one. The bundle
reports the materiality **as recorded**, which is `unclassified` for most
obligations most of the time — reading a row is not classifying one.

**No tamper-evidence claim.** `reporting_obligations`,
`context_external_references` and `memory_claim_provenance` carry no digest
column, so none of these rows sits on a chain. The honest statement travels in
the document, as a field, because an exported document travels away from every
docstring explaining it.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.governance.obligations import (
    SUBJECT_REPORTING_OBLIGATION,
    ReportingObligation,
    ReportingObligationService,
)
from contextplane.types import TenantContext

#: What this bundle is, stated in the bundle. Same wording as the quarantine
#: export's, and shared rather than restated would be wrong here: they are
#: different documents about different tables, and a single constant would
#: eventually be edited for one and read by the other.
BUNDLE_PROVENANCE: Final[str] = (
    "A read of mutable rows from this deployment's own database at export time. "
    "None of these rows sits on a digest chain, so this document evidences what "
    "the database held when it was produced and nothing about what it held before."
)

#: The kind of external record an obligation cites. Named here as well as in the
#: service that writes the citation, because this read must not widen if that
#: write ever does: a bundle silently including a `deployment` would report a
#: deploy as the incident.
_INCIDENT_KIND: Final = "incident"

_CITED_INCIDENTS = """
SELECT r.reference_id, r.source_system, r.source_namespace, r.external_id,
       r.authorized_uri, r.observed_at, b.bound_at
  FROM context_reference_bindings b
  JOIN context_external_references r ON r.reference_id = b.reference_id
 WHERE b.tenant_id = :tenant
   AND b.subject_type = :subject_type
   AND b.subject_id = :oid
   AND r.kind = :incident_kind
 ORDER BY b.bound_at, r.reference_id
"""

#: Claims whose provenance names one of this obligation's incidents.
#:
#: Joined through `memory_claims` for the tenant filter, which
#: `memory_claim_provenance` does not carry -- the same trap
#: `claim_quarantine_members` sets, and the same answer.
_CLAIMS_CITING = """
SELECT DISTINCT p.claim_id, p.evidence_ref
  FROM memory_claim_provenance p
  JOIN memory_claims c ON c.claim_id = p.claim_id
 WHERE p.evidence_kind = :incident_kind
   AND p.evidence_ref = ANY(:external_ids)
   AND COALESCE(c.owning_tenant_id, c.author_tenant_id) = :tenant
 ORDER BY p.claim_id
"""


@dataclasses.dataclass(frozen=True)
class CitedIncident:
    """One external incident record this obligation is about."""

    reference_id: uuid.UUID
    source_system: str
    source_namespace: str
    external_id: str
    authorized_uri: str | None
    observed_at: datetime.datetime | None
    bound_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class ObligationEvidenceBundle:
    """One obligation, the incidents it cites, and the claims citing those."""

    obligation: ReportingObligation
    incidents: tuple[CitedIncident, ...]
    #: Claim ids, paired with the incident each cites. Ids and not content --
    #: see the module docstring.
    citing_claims: tuple[dict[str, Any], ...]
    provenance: str = BUNDLE_PROVENANCE

    @property
    def is_matched(self) -> bool:
        """Whether anybody has yet said which record this obligation concerns.

        False is a nomination in progress rather than a defect, and a reader of
        an empty bundle needs to be able to tell those apart.
        """
        return bool(self.incidents)


class ObligationEvidenceService:
    """The read that makes an obligation-scoped export carry evidence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        obligations: ReportingObligationService,
    ) -> None:
        self._session_factory = session_factory
        self._obligations = obligations

    async def bundle_for(self, ctx: TenantContext, *, obligation_id: uuid.UUID) -> ObligationEvidenceBundle:
        """Everything recorded about one obligation, in one read.

        The obligation is read through its own service rather than re-queried
        here: it is the only writer of that table and the only reader that knows
        what a missing row means, and a second copy of the tenant-scoped select
        would be a second place the two could disagree about whose obligation
        this is.
        """
        obligation = await self._obligations.get(ctx, obligation_id=obligation_id)

        async with self._session_factory() as session:
            incidents = tuple(
                CitedIncident(
                    reference_id=row["reference_id"],
                    source_system=row["source_system"],
                    source_namespace=row["source_namespace"],
                    external_id=row["external_id"],
                    authorized_uri=row["authorized_uri"],
                    observed_at=row["observed_at"],
                    bound_at=row["bound_at"],
                )
                for row in (
                    await session.execute(
                        text(_CITED_INCIDENTS),
                        {
                            "tenant": ctx.tenant_id,
                            "subject_type": SUBJECT_REPORTING_OBLIGATION,
                            "oid": obligation_id,
                            "incident_kind": _INCIDENT_KIND,
                        },
                    )
                )
                .mappings()
                .all()
            )

            claims: tuple[dict[str, Any], ...] = ()
            if incidents:
                # Skipped entirely when nothing is cited. `= ANY('{}')` matches
                # no row, so the query would be correct and pointless -- and a
                # reader of the log would see an evidence scan run for an
                # obligation nobody has matched to a record.
                claims = tuple(
                    {"claim_id": str(row["claim_id"]), "incident": row["evidence_ref"]}
                    for row in (
                        await session.execute(
                            text(_CLAIMS_CITING),
                            {
                                "incident_kind": _INCIDENT_KIND,
                                "external_ids": [incident.external_id for incident in incidents],
                                "tenant": ctx.tenant_id,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )

        return ObligationEvidenceBundle(
            obligation=obligation,
            incidents=incidents,
            citing_claims=claims,
        )


__all__ = [
    "BUNDLE_PROVENANCE",
    "CitedIncident",
    "ObligationEvidenceBundle",
    "ObligationEvidenceService",
]
