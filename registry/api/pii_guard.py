"""Running the PII scan a write path must pass before it stores anything.

Extracted from the artifact router, which was its only caller. A second caller
now needs the identical behaviour, and a hand-copied security control is how two
copies drift -- this codebase already carried three copies of one tombstone
query for exactly that reason.

Moved verbatim rather than improved. An extraction that also changes behaviour
makes a later regression impossible to attribute: the diff would show both a
move and a fix, and nobody could tell which one broke something.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy import text as sql_text

from registry.security.pii_scanner import build_builtin_scanner
from registry.storage.models import PiiFieldPolicyRow, PiiPatternRow
from registry.types import TenantContext


async def run_pii_scan(
    request: Request,
    ctx: TenantContext,
    text: str,
    field_type: str,
) -> None:
    """Run PII scan on *text* for *field_type*.

    Queries tenant pii_patterns.policy_override and pii_field_policies, builds
    the scanner, and raises HTTP 422 if action_taken == 'block'.
    Always writes detection rows to pii_detection_log.
    """
    factory = request.app.state.session_factory

    # --- Load tenant pattern overrides ---
    pattern_overrides: dict[str, str] = {}
    async with factory() as session:
        pat_rows = await session.execute(
            select(PiiPatternRow).where(
                PiiPatternRow.tenant_id == ctx.tenant_id,
                PiiPatternRow.policy_override.isnot(None),
                PiiPatternRow.is_enabled.is_(True),
            )
        )
        for row in pat_rows.scalars():
            if row.policy_override:
                pattern_overrides[row.name] = row.policy_override

    # --- Load per-field policies ---
    field_policies: dict[str, str] = {}
    async with factory() as session:
        fp_rows = await session.execute(
            select(PiiFieldPolicyRow).where(
                PiiFieldPolicyRow.tenant_id == ctx.tenant_id,
                PiiFieldPolicyRow.field_type == field_type,
            )
        )
        for row in fp_rows.scalars():
            if row.pattern_id is None:
                field_policies[f"{field_type}:*"] = row.policy
            else:
                # Resolve pattern name from loaded pattern_overrides keys (best-effort)
                # Field-policy lookup uses field_type:pattern_name key format
                field_policies[f"{field_type}:{row.pattern_id}"] = row.policy

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

    if response.action_taken == "block":
        matched = [m.name for m in response.matched_patterns]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "pii_blocked",
                "message": (f"PII detected in field '{field_type}' with block policy; " "write rejected."),
                "matched_patterns": matched,
            },
        )
