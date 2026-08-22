"""What an erasure does to a derived claim: minimize it, invalidate it, keep the shell.

A claim derived from a signal, a checkpoint or a receipt is a second copy of what
those records said. It quotes them — the evidence links carry excerpts, and so does
the claim's own provenance — and it keeps answering queries long after the record it
quoted has been erased or withdrawn. That is the artefact this handler exists to
reach.

**Delete and redact do the same thing here, and the thing is not deletion.** The
approved disposition for a memory claim is minimization: minimize excerpts,
invalidate the claim, retain the shell for audit, serve it nowhere. A handler that
implemented `delete` literally would destroy the record that the assertion was ever
made, which is the part an auditor needs precisely when somebody asks what was
believed about them and why. So both operations reduce, and the row survives with
nothing quoted in it.

**Minimization is why nothing has to be repaired.** Erasing an actor's claims
outright is a different job with a different module (`claim_erasure_writes.py`): it
deletes rows, so it has to splice supersession chains and null confirmation triples
that would otherwise dangle. Nothing dangles here. The claim keeps its id, its
supersession link and its confirmation triple; what it loses is the quotations and
its place in every serving path.

**Clearing an evidence link's excerpt is what tombstones it.** The link row stays,
so the audit answer to "what did this derivation read?" is still a list of kinds and
referents; what it no longer holds is the sentence somebody wrote. The approved
disposition for this class records no tombstone row of its own, and one here would be
a second, weaker copy of what the link already says.

**Invalidation is not claim creation, so the one-writer rule is untouched.** The
claim-table writes are not made here: they are `claim_erasure_writes.py`'s two
minimization primitives, called in this handler's transaction. That module is the
permitted writer for those tables, and the split it already describes is the one this
follows — the decision of which claim and why is a propagation decision and belongs
here; every write it implies belongs there. Nothing in either path stages a claim,
links a subject or scores one.

**The word this handler settles the attempt into is `invalidated`**, which the
derivation table's own status vocabulary has. The claim table's does not; what closes a
claim there is a different word for the same fact, spelled and explained where that
write lives.

**The claim half is reachable only once something links a claim to its derivation.**
`claim_derivations.created_claim_id` has no production writer today — the path that
stages a claim from a derivation is not built — so in the tree as shipped this handler
minimizes the derivation's own evidence links and invalidates the attempt, and the
claim it would also close does not yet exist. That is stated here rather than left to
be discovered: a reader who assumed otherwise would conclude that erasing a source
already takes derived claims out of serving.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import text

from contextplane.retention import derivatives
from contextplane.service.memory.claim_erasure_writes import (
    close_claim_for_erasure,
    minimize_claim_evidence,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

#: This handler's version, recorded on every registration the registrar writes.
#: Bumped when the reduction changes shape: a registration records which version
#: reduced an artefact, and two claims reduced differently must be distinguishable
#: without re-reading either.
HANDLER_VERSION: Final[str] = "claim-derivative.v1"

#: How a claim-derivative registration names what it covers. The derivation, not the
#: claim: the registration is written when the attempt and its evidence chain persist,
#: which is before any claim exists to name — and the attempt is what holds the links
#: back to every source, so it is the addressable thing either way.
LOCATOR_PREFIX: Final[str] = "claim_derivation:"

#: The audience a claim derivative is built for. One partition per tenant, because a
#: claim is not built for an audience: visibility is evaluated per read, against the
#: caller, on the claim and its subject both. The locator alone distinguishes
#: registrations.
AUDIENCE_PARTITION: Final[str] = "tenant"

#: The derivation status this handler settles an attempt into. Part of the closed set
#: `claim_derivations` declares, and the only member that says "this attempt's inputs
#: were withdrawn" rather than "the attempt concluded against itself".
STATUS_INVALIDATED: Final[str] = "invalidated"


def _rows_affected(result: object) -> int:
    """How many rows a DML statement touched, as an int rather than an Optional.

    One cast here instead of a suppression at each call site: `execute` is typed as
    returning a generic `Result`, which has no `rowcount`, and every caller below has
    just run an UPDATE.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def locator_for(derivation_id: uuid.UUID) -> str:
    """The storage locator a claim-derivative registration carries.

    One function so the registrar and the handler cannot disagree about the spelling.
    A locator the handler could not parse would leave the derivative unreachable while
    the registration looked complete.
    """
    return f"{LOCATOR_PREFIX}{derivation_id}"


def derivation_from_locator(locator: str) -> uuid.UUID:
    """The derivation a locator names, or a refusal.

    Refused rather than defaulted: a locator this handler cannot read was written by
    something that disagreed with `locator_for`, and guessing would reduce the wrong
    attempt, or none, while reporting success.
    """
    if not locator.startswith(LOCATOR_PREFIX):
        msg = f"claim-derivative locator {locator!r} does not name a derivation"
        raise derivatives.UnhandledDerivativeKind(msg)
    try:
        return uuid.UUID(locator.removeprefix(LOCATOR_PREFIX))
    except ValueError as exc:
        msg = f"claim-derivative locator {locator!r} does not carry a derivation id"
        raise derivatives.UnhandledDerivativeKind(msg) from exc


# Locked for the duration: the reduction reads the attempt's status and its claim and
# then writes both, and two propagation items for one derivation — an erasure and an
# expiry arriving together — would otherwise interleave those steps.
_ATTEMPT_SQL = """
SELECT status, created_claim_id
FROM claim_derivations
WHERE derivation_id = :derivation AND tenant_id = :tenant
FOR UPDATE
"""

# Only the links that still quote something. A second pass finds none and reports zero,
# which is what makes a retried propagation item free rather than a second reduction.
_MINIMIZE_EVIDENCE_SQL = """
UPDATE derivation_evidence_links
SET excerpt = NULL
WHERE derivation_id = :derivation AND excerpt IS NOT NULL
"""

_INVALIDATE_ATTEMPT_SQL = """
UPDATE claim_derivations
SET status = :invalidated
WHERE derivation_id = :derivation AND status <> :invalidated
"""


class ClaimDerivativeHandler:
    """Minimizes a derived claim's quotations and takes it out of every serving path.

    Holds nothing. The reduction is expressed entirely in the four statements above,
    keyed by the derivation the registration names, so there is no policy to configure
    and no state to carry between items.
    """

    kind = derivatives.KIND_CLAIM_DERIVATIVE
    version = HANDLER_VERSION

    async def apply(
        self,
        session: AsyncSession,
        registration: derivatives.Registration,
        operation: str,
    ) -> int:
        """Reduce the derivation this registration names. Returns artefacts touched.

        The count is evidence excerpts cleared, plus provenance excerpts cleared, plus
        the claim and the attempt when either still had to be closed. Zero is a valid
        success: an attempt already invalidated, or one whose registration outlived it,
        has nothing left to do.

        `rebuild` is refused. Rebuilding a claim derivative means re-running an
        extractor over evidence this propagation is in the middle of withdrawing, which
        this handler cannot do and must not report as done — a refusal lands the item
        in `failed`, where `pending_overdue` counts it.

        **Which reads fail closed on that count is now a list, not an assurance.** This
        sentence used to end "and reads fail closed", which described no code: the count
        had one consumer, on a workspace read, and the arm that serves claims did not
        ask. The refusal is real on the observed-claims arm and on all three workspace
        reads; it is not wired on the canonical arm or the ARC arm, and each of those is a
        deliberate answer recorded at the arms rather than an omission.

        This sentence used to include "the context-receipt read surface" in that
        deliberate list, and `arms.py` simultaneously described the same surface as a
        miss awaiting a fix. Two hand-maintained lists disagreeing about one surface is
        how it stayed unguarded: `exclusions_for` serves the `item_key` that
        `receipt_link` minimizes and is now guarded, while `get` and `arms_for` serve
        tables no blocking handler touches and are correctly not. A registration this handler makes is `blocking`, so
        every guard that asks `blocking_only` does count it.
        """
        if operation not in derivatives.OPERATIONS:
            msg = f"{operation!r} is not a propagation operation"
            raise derivatives.UnhandledDerivativeKind(msg)
        if operation == derivatives.OPERATION_REBUILD:
            msg = (
                "a claim derivative cannot be rebuilt by propagation: re-deriving it "
                "would re-read the evidence this work item is withdrawing"
            )
            raise derivatives.UnhandledDerivativeKind(msg)

        derivation_id = derivation_from_locator(registration.storage_locator)
        params = {"derivation": derivation_id, "tenant": registration.tenant_id}
        attempt = (await session.execute(text(_ATTEMPT_SQL), params)).one_or_none()
        if attempt is None:
            # The registration outlived the attempt, or this tenant never owned it.
            # Both are the retried-propagation shape, not an error.
            return 0

        touched = _rows_affected(await session.execute(text(_MINIMIZE_EVIDENCE_SQL), {"derivation": derivation_id}))

        if attempt.created_claim_id is not None:
            claim_id = uuid.UUID(str(attempt.created_claim_id))
            touched += await minimize_claim_evidence(session, claim_id=claim_id)
            touched += await close_claim_for_erasure(session, claim_id=claim_id)

        touched += _rows_affected(
            await session.execute(text(_INVALIDATE_ATTEMPT_SQL), {**params, "invalidated": STATUS_INVALIDATED})
        )

        _log.info(
            "claim_derivative.reduced: derivation=%s claim=%s operation=%s artefacts=%d",
            derivation_id,
            attempt.created_claim_id,
            operation,
            touched,
        )
        return touched


__all__ = [
    "AUDIENCE_PARTITION",
    "HANDLER_VERSION",
    "LOCATOR_PREFIX",
    "STATUS_INVALIDATED",
    "ClaimDerivativeHandler",
    "derivation_from_locator",
    "locator_for",
]
