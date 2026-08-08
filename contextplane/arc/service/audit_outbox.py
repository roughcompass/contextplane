"""ARC's audit path: an outbox row, written in the same transaction as the state.

The rest of this codebase writes audit rows inline -- `INSERT INTO audit_log`
alongside the change being audited. ARC does not, and the difference is
deliberate.

An ARC resolution is one `REPEATABLE READ` transaction that can be retried on
a serialization failure. Inline audit writes would either be rolled back with
the attempt (losing the record of what happened) or, if written on a separate
connection to survive, would record attempts that never committed. Neither is
acceptable for a subsystem whose entire product is trustworthy evidence.

An outbox row is written in the same transaction as the domain state, so the
two are atomic: an ARC write that committed always has its audit row, and one
that rolled back has neither. A drain worker later moves those rows into
`audit_log`, and is ARC's *only* writer to that table -- if ARC also wrote
inline anywhere, ordering between the two paths would be undefined and the
audit log could show effect before cause.

Global-scope events attribute to the reserved deployment tenant. They are not
any tenant's business, and filing them under whichever tenant happened to
trigger them would both mislead that tenant's auditor and leak the existence
of deployment-wide activity.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.models import DEPLOYMENT_TENANT_ID
from contextplane.exceptions import RegistryError

# `arc_audit_outbox.event_payload` is JSONB, so a column-type scan cannot see
# inside it. Bounded here instead: an audit row is a record of what happened,
# not a place to park a response body.
MAX_PAYLOAD_BYTES = 8 * 1024


class AuditPayloadTooLarge(RegistryError):
    """A payload exceeded the outbox bound. Never truncated silently.

    Truncating would produce an audit row that looks complete and is not,
    which is worse than a loud failure: an auditor cannot tell that
    something was dropped.
    """


def _serialize(payload: dict[str, Any]) -> str:
    """Deterministic JSON, so two identical events serialize identically.

    Sorted keys matter here for the same reason they matter in the
    canonicalization profiles: an audit row that differs only in key order
    would defeat any downstream deduplication or comparison.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    size = len(encoded.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        msg = f"ARC audit payload is {size} bytes, over the {MAX_PAYLOAD_BYTES}-byte bound"
        raise AuditPayloadTooLarge(msg)
    return encoded


async def emit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Write one outbox row on the caller's session, without committing.

    Not committing is the entire point. This row must land or not land
    together with the state change it describes, so it belongs to the
    caller's transaction.
    """
    outbox_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO arc_audit_outbox (outbox_id, tenant_id, event_type, event_payload) "
            "VALUES (:outbox_id, :tenant_id, :event_type, CAST(:payload AS JSONB))"
        ),
        {
            "outbox_id": outbox_id,
            "tenant_id": tenant_id,
            "event_type": event_type,
            "payload": _serialize(payload),
        },
    )
    return outbox_id


async def emit_global(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Emit a deployment-scope event against the reserved sentinel tenant.

    Separate function rather than a nullable `tenant_id`: the column is NOT
    NULL with a foreign key, and more importantly "which tenant" is a
    decision that should be made explicitly at the call site rather than by
    forgetting to pass one.
    """
    return await emit(session, tenant_id=DEPLOYMENT_TENANT_ID, event_type=event_type, payload=payload)


__all__ = ["MAX_PAYLOAD_BYTES", "AuditPayloadTooLarge", "emit", "emit_global"]
