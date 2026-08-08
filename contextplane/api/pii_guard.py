"""The HTTP adaptation of the pre-storage PII scan.

The scan itself, and the admission decision it feeds, live in
`contextplane.security.pii_guard` beside the scanner they drive -- services,
the MCP tool surface, and the extraction worker all run them and none of them
serve a request. What is left here is the part that only makes sense over
HTTP: pulling the session factory off `request.app.state`, and turning a
blocked write into a 422 the client can read.

Nothing in this module is re-exported. A caller that wants the scan or the
admission decision imports it from `contextplane.security.pii_guard` directly.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from contextplane.security.pii_guard import AdmissionRefused, admit_or_refuse, scan_for_pii
from contextplane.types import TenantContext


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


async def run_admission(
    request: Request,
    ctx: TenantContext,
    text: str,
    field_type: str,
    *,
    subject: str,
) -> None:
    """The HTTP adapter: admit, or raise 422.

    Mirrors `run_pii_scan`'s shape so a router swapping one for the other reads
    the same. The body deliberately does not name the matched value, only the
    classes -- an error that echoed the offending content back would put it in
    the response, the logs, and whatever captured them.
    """
    try:
        await admit_or_refuse(request.app.state.session_factory, ctx, text, field_type, subject=subject)
    except AdmissionRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "pii_blocked",
                "message": (
                    f"content for field '{field_type}' carries a prohibited class and was refused before storage"
                ),
                "matched_patterns": list(refused.decision.classes),
            },
        ) from refused


__all__ = ["run_admission", "run_pii_scan"]
