"""Running the PII scan a write path must pass before it stores anything.

Extracted from the artifact router, which was its only caller. Further callers
now need the identical behaviour, and a hand-copied security control is how two
copies drift -- this codebase already carried three copies of one tombstone
query for exactly that reason.

**The scan is split from the transport.** `scan_for_pii` takes a session factory
and reports what it found; `run_pii_scan` is the HTTP adapter that raises 422.
The split exists because the extraction worker has no request to take a factory
from, and the alternative -- a second implementation for background callers --
is exactly the drift this module was created to prevent. A model can reproduce
PII from a source body into its output, so generated values must pass the same
scanner as submitted ones, and "the same" has to mean one code path.

The scanning behaviour itself is unchanged from the verbatim extraction: the
queries, the advisory-scanner construction, the best-effort log write, and the
block condition are the original ones.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.security.pii_scanner import build_builtin_scanner
from registry.storage.models import PiiFieldPolicyRow, PiiPatternRow
from registry.types import TenantContext


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
        except Exception:  # noqa: BLE001
            # Detection log write failure MUST NOT block the request.
            pass

    return PiiScanOutcome(
        blocked=response.action_taken == "block",
        matched_patterns=tuple(m.name for m in response.matched_patterns),
        action_taken=response.action_taken,
        categories=tuple(sorted({m.category for m in response.matched_patterns})),
    )


async def run_pii_scan(
    request: Request,
    ctx: TenantContext,
    text: str,
    field_type: str,
) -> None:
    """The HTTP adapter: scan, and raise 422 if the policy blocks.

    Kept as the routers' entry point so their behaviour is unchanged by the
    split.
    """
    outcome = await scan_for_pii(request.app.state.session_factory, ctx, text, field_type)
    if outcome.blocked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "pii_blocked",
                "message": (f"PII detected in field '{field_type}' with block policy; " "write rejected."),
                "matched_patterns": list(outcome.matched_patterns),
            },
        )
