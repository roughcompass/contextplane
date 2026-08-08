"""The one place both ingestion defenses run before a self-asserted claim is staged.

`ClaimService.stage_claim` is the sole writer of the claim table. It validates
the ontology, resolves the subject, and derives authority and visibility --
but it runs neither of the two checks before ever reaching that write path.
Extraction refuses directive content and blocking PII before it calls
`stage_claim` (`extraction/service.py::ExtractionService._stage_one`). A
connector run is governed differently and deliberately so: its admission gate
is content-blind (it decides how much a registered source may assert, not what
the assertion says), because technical documents legitimately contain
imperative prose -- an ADR that says "run the migration before deploying" is
ordinary content, not an attack, and refusing it would reject the very
documents connectors exist to read. What protects a reader from connector
content is the read path instead: every claim served carries the
untrusted-recall trust label, structurally enforced on construction
(`claim_serving.py::ServedClaim`). An agent or curator asserting a claim
directly -- on its own say-so rather than from a transcript or a governed
source -- has neither layer in front of it: no extraction pass ran, and no
source registration bounds what it may assert. A directive in that position is
anomalous rather than expected, which is what makes refusing it correct. This
module is that layer, shared by both surfaces that let a caller stage a claim
on their own say-so (a REST route and its MCP equivalent), so the two checks
exist exactly once rather than once per surface. A second, hand-copied
implementation is how one surface ends up running a check the other quietly
skipped.

**Why containment runs first.** A claim whose value instructs rather than
describes is a stored-prompt-injection channel: once staged, it looks like an
ordinary claim, carries real (or fabricated) provenance, and reaches every
later agent through the same trusted read path every other claim does.
Nothing downstream can tell it apart from a genuine observation, so it must
never be staged. Checked before the PII scan for the same reason
`extraction/service.py` orders it first: a directive value must never be
reported as a mere PII match, and a regex check that costs no database query
should never run after one that does, only to be refused anyway.

**Why the PII scan uses the same field type extraction applies to generated
values.** A claim asserted directly by an agent is exactly as unreviewed as
one a model extracted from a transcript -- nobody has read either before it
lands -- so both are scored against the same tenant PII policy rather than a
separate, laxer one nobody configured on purpose.

**What this module does not do.** It does not change `stage_claim`, and it
does not re-validate anything `stage_claim` already validates on its own
(the ontology, the subject, authority, visibility). Both checks here run
strictly before that call; a refusal here never reaches the claim table.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.exceptions import ValidationError
from contextplane.extraction.containment import CandidateRefused, assert_not_directive
from contextplane.extraction.service import PII_FIELD_TYPE
from contextplane.security.pii_guard import AdmissionRefused, admit_or_refuse
from contextplane.service.memory.claim_authority import Evidence, StagedClaim
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.types import JSONValue, TenantContext

_log = logging.getLogger(__name__)

# The PII scanner's per-tenant field-policy key. Imported, not re-declared,
# from `extraction/service.py`: a tenant's field policy is keyed on this exact
# string (`pii_field_policies.field_type`), so a directly asserted claim value
# must be scanned under the identical key a model-generated one already is --
# a second, differently-spelled constant here would mean a policy an admin
# configured for extraction's claim values silently never applies to a
# curator's or an agent's, and the gap would only surface as "the scanner let
# something through," never as an error anywhere.


# `audit_log.target_type` for a refused attempt. Deliberately distinct from
# `'memory_claim'` (what every other claim action uses): nothing is ever
# staged for a refused candidate, so there is no claim row for that id to
# point at. Using the claim target type here would make a later reader
# querying by claim id expect a row that does not exist.
_CONTAINMENT_ATTEMPT_TARGET_TYPE = "memory_claim_attempt"


class ClaimPiiBlocked(ValidationError):
    """A direct claim assertion refused because the PII scanner's policy resolved to 'block'.

    Carries `field` (`"value"` or `"evidence[<n>].excerpt"`) and
    `matched_patterns` as attributes, the same precedent
    `WorkspacePiiBlocked` sets in `service/workspace/entries.py`: the caller
    (a REST route or an MCP tool) reconstructs the exact structured response
    body from these rather than re-deriving it from `str(exc)`.
    """

    def __init__(self, field: str, matched_patterns: tuple[str, ...]) -> None:
        patterns = ", ".join(sorted(matched_patterns)) if matched_patterns else "none"
        super().__init__(f"{field} matched a blocking PII policy ({patterns}); assertion refused.")
        self.field = field
        self.matched_patterns = matched_patterns


async def _assert_no_pii(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    candidate_text: str,
    *,
    field: str,
) -> None:
    """Admit *candidate_text* under the pilot floor, raising on a refusal.

    Runs through admission rather than a bare scan, so a claim value carrying a
    prohibited class is refused on a deployment that has configured nothing --
    which is every deployment until somebody inserts a policy row. Admission
    calls `scan_for_pii` itself, so the detection rows are still written exactly
    once per call.
    """
    try:
        await admit_or_refuse(session_factory, ctx, candidate_text, PII_FIELD_TYPE, subject=field)
    except AdmissionRefused as refused:
        raise ClaimPiiBlocked(field=field, matched_patterns=refused.decision.classes) from refused


async def _audit_containment_refusal(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    subject_reference: str,
    predicate: str,
    refused: CandidateRefused,
) -> None:
    """Best-effort record of one refused attempt.

    A metric shows a rate; an investigation of a specific attempt needs the
    row a metric cannot carry. `target_id` is a freshly minted id rather than
    a claim id -- refusing here means nothing was ever staged, so there is no
    claim row to point at. Best-effort and swallowed on failure, the same
    shape `pii_guard.scan_for_pii`'s own detection-log write uses: a refusal
    must still reach the caller as a clean 4xx whether or not this write
    succeeds, so a broken audit write must never turn a refusal into a 500.
    """
    try:
        now = datetime.datetime.now(tz=datetime.UTC)
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_log "
                    "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                    "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                    "VALUES (:audit_id, :tid, :aid, :action, :ttype, :target, NULL, "
                    "        CAST(:after AS JSONB), :now, NULL, NULL)"
                ),
                {
                    "audit_id": uuid.uuid4(),
                    "tid": ctx.tenant_id,
                    "aid": ctx.actor_id,
                    "action": actions.CLAIM_CONTAINMENT_REFUSED,
                    "ttype": _CONTAINMENT_ATTEMPT_TARGET_TYPE,
                    "target": uuid.uuid4(),
                    "after": json.dumps(
                        {
                            "trigger": refused.trigger,
                            "detail": refused.detail,
                            "predicate": predicate,
                            "subject_reference": subject_reference,
                        },
                        sort_keys=True,
                    ),
                    "now": now,
                },
            )
    except Exception:  # noqa: BLE001 - see comment above
        # Mirrors pii_guard.scan_for_pii's own detection-log write: an audit
        # write failing must never block the refusal the caller is waiting on.
        _log.warning("failed to audit a containment refusal", exc_info=True)


async def stage_claim_defended(
    session_factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    ctx: TenantContext,
    *,
    subject_reference: str,
    predicate: str,
    value: JSONValue,
    evidence: tuple[Evidence, ...],
    asserted_valid_from: datetime.datetime | None = None,
    asserted_valid_to: datetime.datetime | None = None,
    visibility: str | None = None,
    namespace: str | None = None,
) -> StagedClaim:
    """Run both ingestion defenses, then stage through the one write path.

    Called by both surfaces that let a caller assert a claim on their own
    say-so -- a REST route and its MCP equivalent -- so the defense lives
    here exactly once. Takes `session_factory` (the PII scan needs a factory,
    not a bound service) and `claims` (the `ClaimService` instance that
    actually stages) as two separate parameters rather than one combined
    object: both call sites already hold both off the same typed service
    container, and neither needs a new object constructed just to pass one
    thing in.

    Order mirrors `extraction/service.py::ExtractionService._stage_one`:
    containment first (a regex check with no query cost, and a directive
    value must never be reported as merely a PII match), the PII scan last
    (the only check here that costs a database round trip). A non-`str`
    value passes containment's own no-op path unconditionally and skips the
    PII scan explicitly -- the same `isinstance` guard extraction's own
    `_stage_one` uses, because only a string can carry an instruction or a
    reproduced secret.
    """
    try:
        assert_not_directive(value)
        for idx, item in enumerate(evidence):
            if item.excerpt:
                assert_not_directive(item.excerpt, field=f"evidence[{idx}].excerpt")
    except CandidateRefused as refused:
        await _audit_containment_refusal(
            session_factory,
            ctx,
            subject_reference=subject_reference,
            predicate=predicate,
            refused=refused,
        )
        raise

    if isinstance(value, str):
        await _assert_no_pii(session_factory, ctx, value, field="value")
    for idx, item in enumerate(evidence):
        if item.excerpt:
            await _assert_no_pii(session_factory, ctx, item.excerpt, field=f"evidence[{idx}].excerpt")

    return await claims.stage_claim(
        ctx,
        subject_reference=subject_reference,
        predicate=predicate,
        value=value,
        evidence=evidence,
        asserted_valid_from=asserted_valid_from,
        asserted_valid_to=asserted_valid_to,
        visibility=visibility,
        namespace=namespace,
    )


__all__ = [
    "PII_FIELD_TYPE",
    "ClaimPiiBlocked",
    "stage_claim_defended",
]
