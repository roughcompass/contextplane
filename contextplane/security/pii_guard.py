"""Running the PII scan a write path must pass before it stores anything.

Extracted from the artifact router, which was its only caller. Further callers
now need the identical behaviour, and a hand-copied security control is how two
copies drift -- this codebase already carried three copies of one tombstone
query for exactly that reason.

**The scan is split from the transport.** This module takes a session factory
and reports what it found; the HTTP adapter that turns a block into a 422 lives
in `contextplane.api.pii_guard`. The split exists because the extraction worker
has no request to take a factory from, and the alternative -- a second
implementation for background callers -- is exactly the drift this module was
created to prevent. A model can reproduce PII from a source body into its
output, so generated values must pass the same scanner as submitted ones, and
"the same" has to mean one code path.

The control lives beside the scanner it drives rather than in the API package:
services, the MCP tool surface, and the extraction worker all run it, and none
of them serve an HTTP request.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.context.admission import AdmissionDecision, RefusalRecord, admit
from contextplane.security.pii_scanner import build_builtin_scanner
from contextplane.storage.models import PiiFieldPolicyRow, PiiPatternRow
from contextplane.types import TenantContext

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PiiScanOutcome:
    """What the scan found, without deciding what the caller should do.

    The HTTP path turns a block into a 422; the extraction path turns it into a
    categorized rejection and a dead-lettered candidate. Same finding, different
    consequences, so the decision belongs to the caller.

    action_taken and categories are the resolved-policy view of the same scan
    (one of 'advisory'/'warn'/'block', and the matched patterns' categories,
    deduplicated and sorted) -- added for callers that need the three-outcome
    dispatch without recomputing it from matched_patterns themselves.
    """

    blocked: bool
    matched_patterns: tuple[str, ...]
    action_taken: str
    categories: tuple[str, ...]


async def scan_for_pii(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    text: str,
    field_type: str,
) -> PiiScanOutcome:
    """Scan *text* for *field_type* and report the outcome.

    Queries tenant pii_patterns.policy_override and pii_field_policies, builds
    the scanner, and always writes detection rows to pii_detection_log.
    """

    # --- Load tenant pattern overrides, and every enabled pattern's id->name.
    # pii_field_policies stores pattern_id (the row's UUID); _resolve_policy's
    # per-field lookup key is name-based ("field_type:pattern_name"). This is
    # the only place that id is translated to a name, so the query loads every
    # enabled pattern -- not just the ones carrying a policy_override -- and
    # keeps both structures in the same pass.
    pattern_overrides: dict[str, str] = {}
    pattern_id_to_name: dict[uuid.UUID, str] = {}
    async with factory() as session:
        pat_rows = await session.execute(
            select(PiiPatternRow).where(
                PiiPatternRow.tenant_id == ctx.tenant_id,
                PiiPatternRow.is_enabled.is_(True),
            )
        )
        for pattern in pat_rows.scalars():
            pattern_id_to_name[pattern.pattern_id] = pattern.name
            if pattern.policy_override:
                pattern_overrides[pattern.name] = pattern.policy_override

    # --- Load per-field policies ---
    field_policies: dict[str, str] = {}
    async with factory() as session:
        fp_rows = await session.execute(
            select(PiiFieldPolicyRow).where(
                PiiFieldPolicyRow.tenant_id == ctx.tenant_id,
                PiiFieldPolicyRow.field_type == field_type,
            )
        )
        for policy in fp_rows.scalars():
            if policy.pattern_id is None:
                field_policies[f"{field_type}:*"] = policy.policy
                continue
            pattern_name = pattern_id_to_name.get(policy.pattern_id)
            if pattern_name is None:
                # The pattern this policy targeted was deleted or disabled
                # since the policy row was created. Keying on an id that
                # resolves to nothing would silently never match, so skip
                # it rather than write a key no lookup can ever hit.
                continue
            field_policies[f"{field_type}:{pattern_name}"] = policy.policy

    scanner = build_builtin_scanner(tenant_policy="advisory")

    # Collect detection log rows for writing.
    detection_rows: list[dict[str, Any]] = []

    def _log_sink(row: dict[str, Any]) -> None:
        row["tenant_id"] = str(ctx.tenant_id)
        row["actor_id"] = str(ctx.actor_id) if ctx.actor_id else None
        detection_rows.append(row)

    response = scanner.scan(
        text,
        field_type=field_type,
        pattern_overrides=pattern_overrides,
        field_policies=field_policies,
        log_sink=_log_sink,
    )

    # Persist detection log rows (best-effort; must not block 422 raise).
    if detection_rows:
        try:
            now = datetime.datetime.now(tz=datetime.UTC)
            async with factory() as session, session.begin():
                for dr in detection_rows:
                    await session.execute(
                        sql_text(
                            "INSERT INTO pii_detection_log "
                            "(tenant_id, actor_id, target_type, target_id, "
                            " pattern_name, category, match_offset, match_length, "
                            " action_taken, ts) "
                            "VALUES (:tid, :aid, :ttype, NULL, :pname, :cat, "
                            "        :moffset, :mlen, :action, :now)"
                        ),
                        {
                            "tid": ctx.tenant_id,
                            "aid": ctx.actor_id,
                            "ttype": dr.get("target_type", field_type),
                            "pname": dr["pattern_name"],
                            "cat": dr["category"],
                            "moffset": dr.get("match_offset"),
                            "mlen": dr.get("match_length"),
                            "action": dr["action_taken"],
                            "now": now,
                        },
                    )
        except Exception:  # noqa: BLE001 - detection log write must never block the request
            _log.warning(
                "scan_for_pii: pii_detection_log write failed tenant=%s field_type=%s",
                ctx.tenant_id,
                field_type,
                exc_info=True,
            )

    return PiiScanOutcome(
        blocked=response.action_taken == "block",
        matched_patterns=tuple(m.name for m in response.matched_patterns),
        action_taken=response.action_taken,
        categories=tuple(sorted({m.category for m in response.matched_patterns})),
    )


class AdmissionRefused(Exception):
    """Content carrying a prohibited class was refused before storage.

    A distinct type rather than a `ValueError`, because every caller has to
    treat it as terminal: the write does not happen, and there is no repair the
    service layer can make on the caller's behalf.
    """

    def __init__(self, decision: AdmissionDecision) -> None:
        self.decision = decision
        super().__init__(f"content carries a prohibited class: {', '.join(decision.classes)}")


#: Namespace for deriving an audit target from a subject that is not a UUID.
#: Fixed forever: the derivation has to be reproducible, or an auditor cannot
#: recompute the id for a session and find its refusals.
_ADMISSION_TARGET_NAMESPACE = uuid.UUID("6f8f7a1e-5b3d-4d2a-9c14-0e2f9a7b6c31")


def admission_target_id(*, tenant_id: uuid.UUID, field_type: str, subject: str) -> uuid.UUID:
    """A stable audit target for a write that never happened.

    `audit_log.target_id` is a `NOT NULL` UUID, and the subjects a pilot write
    names are not UUIDs -- a session id is a caller-chosen string and an entity
    may be addressed by slug. Nor is there a row id to point at: admission runs
    before storage, so the thing being written does not exist yet.

    So the id is derived from the subject rather than invented. The same session
    always maps to the same target, which is what lets an auditor recompute it
    and find every refusal against that session; a random id per refusal would
    satisfy the column and answer no question. The readable subject travels in
    the payload beside it, so nobody has to recompute anything to read one row.
    """
    return uuid.uuid5(_ADMISSION_TARGET_NAMESPACE, f"{tenant_id}:{field_type}:{subject}")


async def _record_refusals(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    refusals: Sequence[RefusalRecord],
    *,
    subject: str,
) -> None:
    """Write one audit row per refusal.

    Failures here are logged and swallowed, the same way the detection log is
    handled above: the refusal itself has already been decided, and an audit
    write that could veto it would turn a storage hiccup into an admitted
    write.
    """
    try:
        now = datetime.datetime.now(tz=datetime.UTC)
        async with factory() as session, session.begin():
            for refusal in refusals:
                await session.execute(
                    sql_text(
                        "INSERT INTO audit_log "
                        "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                        "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                        "VALUES (:audit_id, :tid, :aid, :action, :ttype, :target, NULL, "
                        "        CAST(:after AS JSONB), :ts, NULL, :error_code)"
                    ),
                    {
                        "audit_id": uuid.uuid4(),
                        "tid": refusal.tenant_id,
                        "aid": ctx.actor_id,
                        "action": actions.CONTEXT_ADMISSION_REFUSED,
                        "ttype": refusal.target_type,
                        "target": admission_target_id(
                            tenant_id=refusal.tenant_id, field_type=refusal.target_type, subject=subject
                        ),
                        # The subject is merged in here rather than carried on
                        # the record: admission does not know what a caller was
                        # writing to, and should not have to.
                        "after": json.dumps(
                            {**refusal.as_audit_payload(), "subject": subject}, sort_keys=True, default=str
                        ),
                        "ts": now,
                        "error_code": refusal.trigger,
                    },
                )
    except Exception:  # noqa: BLE001 - an audit write must never turn a refusal into an admission
        _log.warning(
            "admission: refusal audit write failed tenant=%s field_type=%s",
            ctx.tenant_id,
            refusals[0].target_type if refusals else "unknown",
            exc_info=True,
        )


async def admit_or_refuse(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    text: str,
    field_type: str,
    *,
    subject: str,
    strategy_id: str | None = None,
) -> PiiScanOutcome:
    """Run admission before storage, and refuse audibly.

    Returns the tenant-policy scan outcome so a caller that also surfaces
    warnings can reuse it. Two policy sets are genuinely in play -- the pilot
    floor, which refuses, and the tenant's own, which may additionally warn --
    and a caller that wants both should not pay for a third pass over the text.

    `subject` is what the caller was writing to -- a session id, an entity, a
    task. It is a string because that is what the surfaces hold; see
    `admission_target_id` for how it becomes the audit row's target.

    The single entry point every pilot write goes through. It is here rather
    than in the admission module because admission decides and holds no session:
    persisting the refusal needs one, and this module already owns the pattern
    of scanning and writing what it found.

    Detection logging still happens through `scan_for_pii`, so a caller gets
    both records: the detection rows describing what was seen, and the audit row
    describing what was refused.
    """
    outcome = await scan_for_pii(factory, ctx, text, field_type)

    decision = admit(
        text,
        field_type=field_type,
        tenant_id=ctx.tenant_id,
        now=datetime.datetime.now(tz=datetime.UTC),
        actor_id=ctx.actor_id,
        target_id=admission_target_id(tenant_id=ctx.tenant_id, field_type=field_type, subject=subject),
        strategy_id=strategy_id,
    )
    if decision.admitted:
        return outcome

    await _record_refusals(factory, ctx, decision.refusals, subject=subject)
    raise AdmissionRefused(decision)


__all__ = [
    "AdmissionRefused",
    "PiiScanOutcome",
    "admission_target_id",
    "admit_or_refuse",
    "scan_for_pii",
]
