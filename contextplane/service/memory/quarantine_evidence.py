"""What one quarantine withheld, exported as one bundle.

E4-T7b. Carved out of E4-T7 the way E4-T5b was carved out of E4-T6, and for the
same reason: the blocked half needs a classification nobody here can make, and
this half needs none.

**Scoped by `quarantine_id`, not by an obligation.** E4-T7 says "one case", and
grounding it found that the case object holding evidence in this tree is the
quarantine ledger, not `reporting_obligations`. That table carries **no
reference column to anything** — its `summary` is free text precisely so an
obligation can be nominated before anybody knows which record it concerns — so
an obligation-scoped bundle would contain the four fields its own detail route
already returns and no evidence at all. That is a compliance-shaped rename, and
the export that answers "what did you withhold, when, on whose authority, and
which rows" is quarantine-scoped.

**The scope predicate is in the query, and the boundary is where the test is.**
A bundle that quietly includes rows outside the quarantine is a disclosure; one
that quietly omits rows inside it is an incomplete filing. Neither is visible in
the output, so `claim_quarantine_members` is the spine — the set recorded at
apply time, never recomputed — and the integration tests seed a second tenant's
identically shaped quarantine and assert absence.

`claim_quarantine_members` has **no `tenant_id` column**, which is the trap.
Isolation comes from the join to `claim_quarantines`; a query that read members
by id alone would serve another tenant's set to anyone who guessed a UUID.

**A reverted quarantine still exports.** The ledger keeps its row and its
members after revert by design — *"the fact that content was withheld for a
period survives the withholding"* — and filtering on "currently withheld" would
omit exactly the period somebody is asking about.

**What this bundle says, and what it deliberately does not.** It says *which*
claims were withheld, not *what they said*. The member list is ids. Serving the
withheld content back through a new export would be a way around the
withholding, which is the one thing the mechanism exists to do.

**No tamper-evidence claim of any kind, and here that is not a caution but a
fact about these tables.** The digest chains in this system run over
`arc_receipt_events` and `arc_operational_event_heads`. Neither
`claim_quarantines` nor `context_receipts` carries a digest column, so none of
the rows exported here sits on either chain — bounded-exposure tamper-evidence
is not merely the ceiling for this document, it is unavailable. The honest
statement is the one in `BUNDLE_PROVENANCE` below: a read of mutable rows from
this deployment's own database at export time.

An export like this is where somebody reaches for the strongest word available,
and the strongest word available is wrong: a chain the party holding the storage
also holds proves nothing against that party. So this surface makes no claim
that anyone is unable to deny anything, and a test checks the served strings for
the phrase rather than trusting this paragraph. See
[the external-anchor decision](../../../.develop/adr/0012-external-anchor-for-the-digest-chains.md).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import NotFoundError
from contextplane.service.memory.quarantine import OPERATOR_ROLES
from contextplane.types import TenantContext

#: Who may export. The roles that may *apply* a quarantine, plus the auditor.
#:
#: Reading what was withheld is an audit function, and an auditor who cannot see
#: the ledger cannot check the operator who wrote it. Widened here rather than
#: by loosening `OPERATOR_ROLES`, because applying a quarantine and reading one
#: are different acts and an auditor must not be able to withhold anything.
EVIDENCE_ROLES: Final[frozenset[str]] = OPERATOR_ROLES | {"auditor"}

#: What this bundle is, stated in the bundle rather than left to a reader's
#: assumption. Returned as a field because a document exported for somebody
#: outside this system travels away from every docstring explaining it.
BUNDLE_PROVENANCE: Final[str] = (
    "A read of mutable rows from this deployment's own database at export time. "
    "None of these rows sits on a digest chain, so this document evidences what "
    "the database held when it was produced and nothing about what it held before."
)


@dataclasses.dataclass(frozen=True)
class WithheldReceipt:
    """One context receipt this quarantine withheld, and when."""

    receipt_id: uuid.UUID
    withheld_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class EvidenceBundle:
    """One quarantine, its recorded member set, and the receipts it withheld."""

    quarantine_id: uuid.UUID
    #: The operator's predicate as they stated it, structured rather than
    #: rendered SQL — the ledger stores it that way so revert cannot depend on
    #: reproducing an evaluation.
    predicate: dict[str, Any]
    reason: str
    #: What the predicate matched **at apply time**, from the ledger. Kept beside
    #: `members` rather than derived from it: if the two ever disagree, that
    #: disagreement is itself the finding, and a bundle that recomputed one from
    #: the other could not show it.
    matched_count: int
    applied_by: uuid.UUID
    applied_at: datetime.datetime
    reverted_by: uuid.UUID | None
    reverted_at: datetime.datetime | None
    #: The recorded membership. Ids, not content — see the module docstring.
    members: tuple[uuid.UUID, ...]
    withheld_receipts: tuple[WithheldReceipt, ...]
    provenance: str = BUNDLE_PROVENANCE

    @property
    def is_reverted(self) -> bool:
        """Whether the withholding has been undone. The bundle exports either
        way: the period it covers is what somebody is asking about."""
        return self.reverted_at is not None


_LEDGER = """
SELECT quarantine_id, predicate, matched_count, reason,
       applied_by, applied_at, reverted_by, reverted_at
  FROM claim_quarantines
 WHERE quarantine_id = :qid AND tenant_id = :tenant
"""

#: The join to the ledger is the tenant filter. `claim_quarantine_members` has no
#: `tenant_id` of its own, so reading it by `quarantine_id` alone would serve
#: another tenant's recorded set to anybody who guessed a UUID.
_MEMBERS = """
SELECT m.claim_id
  FROM claim_quarantine_members m
  JOIN claim_quarantines q ON q.quarantine_id = m.quarantine_id
 WHERE m.quarantine_id = :qid AND q.tenant_id = :tenant
 ORDER BY m.claim_id
"""

#: `context_receipts` carries its own `tenant_id`, and both are applied: the
#: quarantine's tenant is already established above, so a receipt in a different
#: tenant pointing at this quarantine would be a corruption rather than a
#: permitted row, and it should be absent from the bundle rather than exported.
_RECEIPTS = """
SELECT receipt_id, withheld_at
  FROM context_receipts
 WHERE withheld_by = :qid AND tenant_id = :tenant
 ORDER BY withheld_at, receipt_id
"""


class QuarantineEvidenceService:
    """The read side of the quarantine ledger, which had none.

    Separate from `QuarantineService` rather than a method on it: that class is
    the write path — preview, apply, revert — and nothing in the tree read
    `claim_quarantines` outside its own apply and revert statements. A read
    concern with a different role set and a different failure mode is its own
    seam, the same split `curation_ranking.py` took out of `curation_queue.py`.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def bundle_for(self, ctx: TenantContext, *, quarantine_id: uuid.UUID) -> EvidenceBundle:
        """Everything recorded about one quarantine, in one read.

        Refuses an unknown id with `NotFoundError` rather than an empty bundle.
        An empty bundle is indistinguishable from a quarantine that withheld
        nothing, and "we withheld nothing" is a very different answer to give
        somebody than "no such quarantine".
        """
        if not (set(ctx.roles) & EVIDENCE_ROLES):
            # `PermissionError`, the same refusal `QuarantineService` raises for
            # the write side, which `map_catalog_error` turns into a 403. A
            # validation error here would tell a caller their request was
            # malformed when it was their credential that was wrong.
            msg = f"exporting quarantine evidence requires one of {sorted(EVIDENCE_ROLES)}"
            raise PermissionError(msg)

        async with self._session_factory() as session:
            ledger = (
                (await session.execute(text(_LEDGER), {"qid": quarantine_id, "tenant": ctx.tenant_id}))
                .mappings()
                .one_or_none()
            )
            if ledger is None:
                msg = f"no quarantine {quarantine_id} in this tenant"
                raise NotFoundError(msg)
            members = (await session.execute(text(_MEMBERS), {"qid": quarantine_id, "tenant": ctx.tenant_id})).all()
            receipts = (await session.execute(text(_RECEIPTS), {"qid": quarantine_id, "tenant": ctx.tenant_id})).all()

        return EvidenceBundle(
            quarantine_id=ledger["quarantine_id"],
            predicate=_as_predicate(ledger["predicate"]),
            reason=ledger["reason"],
            matched_count=int(ledger["matched_count"]),
            applied_by=ledger["applied_by"],
            applied_at=ledger["applied_at"],
            reverted_by=ledger["reverted_by"],
            reverted_at=ledger["reverted_at"],
            members=tuple(row.claim_id for row in members),
            withheld_receipts=tuple(
                WithheldReceipt(receipt_id=row.receipt_id, withheld_at=row.withheld_at) for row in receipts
            ),
        )


def _as_predicate(stored: object) -> dict[str, Any]:
    """The ledger's JSONB, whatever the driver handed back.

    asyncpg returns JSONB as a string unless a codec is registered, and the
    write path stores it with `json.dumps`. Normalised here so the bundle's
    shape does not depend on driver configuration.
    """
    if isinstance(stored, str):
        loaded = json.loads(stored)
        return loaded if isinstance(loaded, dict) else {"predicate": loaded}
    return dict(stored) if isinstance(stored, dict) else {"predicate": stored}


__all__ = [
    "BUNDLE_PROVENANCE",
    "EVIDENCE_ROLES",
    "EvidenceBundle",
    "QuarantineEvidenceService",
    "WithheldReceipt",
]
