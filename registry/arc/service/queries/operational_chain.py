"""Parametrized SQL for the operational event chain's three tables.

`operational_chain.py` owns the append/verify transaction shape, the
canonical-object construction, and signing; this module owns getting rows
in and out of `arc_operational_events`, `arc_operational_event_heads`, and
`arc_operational_chain_checkpoints`. Every function takes an already-open
`AsyncSession` -- none of them opens its own transaction -- so the caller
controls exactly what commits together, the same convention `queries/
source_admission.py` and `queries/proposal.py` use.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class ExistingEvent:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    sequence: int
    event_digest: str
    request_payload_digest: str


@dataclasses.dataclass(frozen=True)
class HeadRow:
    next_sequence: int
    last_event_digest: str


@dataclasses.dataclass(frozen=True)
class EventRow:
    event_id: uuid.UUID
    artifact_id: uuid.UUID
    sequence: int
    event_type: str
    event_payload: dict[str, Any]
    actor_issuer: str
    actor_subject: str
    actor_role: str
    authorization_decision_reference: str
    authority_evidence_digest: str
    idempotency_key_digest: str
    previous_event_digest: str | None
    signer_key_id: str
    event_digest: str
    signature: str
    created_at: datetime.datetime


async def find_by_idempotency(
    session: AsyncSession, revision_id: uuid.UUID, idempotency_key_digest: str
) -> ExistingEvent | None:
    row = (
        await session.execute(
            text(
                "SELECT event_id, revision_id, sequence, event_digest, request_payload_digest "
                "FROM arc_operational_events "
                "WHERE revision_id = :rid AND idempotency_key_digest = :digest"
            ),
            {"rid": revision_id, "digest": idempotency_key_digest},
        )
    ).one_or_none()
    if row is None:
        return None
    return ExistingEvent(
        event_id=row.event_id,
        revision_id=row.revision_id,
        sequence=row.sequence,
        event_digest=row.event_digest,
        request_payload_digest=row.request_payload_digest,
    )


async def lock_head(session: AsyncSession, revision_id: uuid.UUID) -> HeadRow | None:
    """`SELECT ... FOR UPDATE` -- the append concurrency control.

    Holding this lock for the rest of the caller's transaction is what
    serializes concurrent appends to one revision's chain: two appends
    cannot both read `next_sequence = n` and both write sequence `n`,
    because the second blocks until the first commits (or rolls back) and
    then re-reads `n + 1` (or the same `n`, if the first rolled back).
    """
    row = (
        await session.execute(
            text(
                "SELECT next_sequence, last_event_digest FROM arc_operational_event_heads "
                "WHERE revision_id = :rid FOR UPDATE"
            ),
            {"rid": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return HeadRow(next_sequence=row.next_sequence, last_event_digest=row.last_event_digest)


async def load_head(session: AsyncSession, revision_id: uuid.UUID) -> HeadRow | None:
    """Unlocked read, for verification -- never on the append path."""
    row = (
        await session.execute(
            text("SELECT next_sequence, last_event_digest FROM arc_operational_event_heads WHERE revision_id = :rid"),
            {"rid": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return HeadRow(next_sequence=row.next_sequence, last_event_digest=row.last_event_digest)


async def load_events(session: AsyncSession, revision_id: uuid.UUID) -> list[EventRow]:
    result = await session.execute(
        text(
            "SELECT event_id, artifact_id, sequence, event_type, event_payload, actor_issuer, actor_subject, "
            "       actor_role, authorization_decision_reference, authority_evidence_digest, "
            "       idempotency_key_digest, previous_event_digest, signer_key_id, event_digest, signature, "
            "       created_at "
            "FROM arc_operational_events WHERE revision_id = :rid ORDER BY sequence"
        ),
        {"rid": revision_id},
    )
    return [
        EventRow(
            event_id=row.event_id,
            artifact_id=row.artifact_id,
            sequence=row.sequence,
            event_type=row.event_type,
            event_payload=dict(row.event_payload),
            actor_issuer=row.actor_issuer,
            actor_subject=row.actor_subject,
            actor_role=row.actor_role,
            authorization_decision_reference=row.authorization_decision_reference,
            authority_evidence_digest=row.authority_evidence_digest,
            idempotency_key_digest=row.idempotency_key_digest,
            previous_event_digest=row.previous_event_digest,
            signer_key_id=row.signer_key_id,
            event_digest=row.event_digest,
            signature=row.signature,
            created_at=row.created_at,
        )
        for row in result
    ]


async def insert_event(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    sequence: int,
    event_id: uuid.UUID,
    artifact_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    actor_issuer: str,
    actor_subject: str,
    actor_role: str,
    authorization_decision_reference: str,
    authority_evidence_digest: str,
    idempotency_key_digest: str,
    previous_digest: str | None,
    signer_key_id: str,
    digest: str,
    signature: str,
    signature_profile: str,
    request_digest: str,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_operational_events ("
            "  revision_id, sequence, event_id, artifact_id, event_type, event_payload,"
            "  actor_issuer, actor_subject, actor_role, authorization_decision_reference,"
            "  authority_evidence_digest, idempotency_key_digest, previous_event_digest,"
            "  signer_key_id, event_digest, signature, signature_profile, request_payload_digest, created_at"
            ") VALUES ("
            "  :revision_id, :sequence, :event_id, :artifact_id, :event_type, CAST(:event_payload AS JSONB),"
            "  :actor_issuer, :actor_subject, :actor_role, :authorization_decision_reference,"
            "  :authority_evidence_digest, :idempotency_key_digest, :previous_digest,"
            "  :signer_key_id, :digest, :signature, :signature_profile, :request_digest, :created_at"
            ")"
        ),
        {
            "revision_id": revision_id,
            "sequence": sequence,
            "event_id": event_id,
            "artifact_id": artifact_id,
            "event_type": event_type,
            "event_payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "actor_issuer": actor_issuer,
            "actor_subject": actor_subject,
            "actor_role": actor_role,
            "authorization_decision_reference": authorization_decision_reference,
            "authority_evidence_digest": authority_evidence_digest,
            "idempotency_key_digest": idempotency_key_digest,
            "previous_digest": previous_digest,
            "signer_key_id": signer_key_id,
            "digest": digest,
            "signature": signature,
            "signature_profile": signature_profile,
            "request_digest": request_digest,
            "created_at": created_at,
        },
    )


async def insert_head(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    next_sequence: int,
    last_event_digest: str,
    updated_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_operational_event_heads (revision_id, next_sequence, last_event_digest, updated_at) "
            "VALUES (:revision_id, :next_sequence, :digest, :updated_at)"
        ),
        {
            "revision_id": revision_id,
            "next_sequence": next_sequence,
            "digest": last_event_digest,
            "updated_at": updated_at,
        },
    )


async def advance_head(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    expected_previous: str | None,
    next_sequence: int,
    digest: str,
    updated_at: datetime.datetime,
) -> int:
    """Guarded on the digest read under `lock_head`'s own lock -- belt and
    braces: if this ever affects zero rows, something moved the head
    between the locked read and here, and the caller must not continue as
    though it had won."""
    result = await session.execute(
        text(
            "UPDATE arc_operational_event_heads "
            "SET next_sequence = :next_sequence, last_event_digest = :digest, updated_at = :updated_at "
            "WHERE revision_id = :revision_id AND last_event_digest = :expected_previous"
        ),
        {
            "revision_id": revision_id,
            "next_sequence": next_sequence,
            "digest": digest,
            "updated_at": updated_at,
            "expected_previous": expected_previous,
        },
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def insert_checkpoint(
    session: AsyncSession,
    *,
    checkpoint_id: uuid.UUID,
    deployment_id: str,
    revision_id: uuid.UUID,
    sequence: int,
    event_id: uuid.UUID,
    head_digest: str,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_operational_chain_checkpoints ("
            "  checkpoint_id, deployment_id, revision_id, sequence, event_id, head_digest, created_at"
            ") VALUES ("
            "  :checkpoint_id, :deployment_id, :revision_id, :sequence, :event_id, :head_digest, :created_at"
            ")"
        ),
        {
            "checkpoint_id": checkpoint_id,
            "deployment_id": deployment_id,
            "revision_id": revision_id,
            "sequence": sequence,
            "event_id": event_id,
            "head_digest": head_digest,
            "created_at": created_at,
        },
    )


async def count_pending_checkpoints(session: AsyncSession, revision_id: uuid.UUID) -> int:
    count: int = (
        await session.execute(
            text(
                "SELECT count(*) FROM arc_operational_chain_checkpoints "
                "WHERE revision_id = :rid AND exported_at IS NULL"
            ),
            {"rid": revision_id},
        )
    ).scalar_one()
    return count


@dataclasses.dataclass(frozen=True)
class CheckpointRow:
    checkpoint_id: uuid.UUID
    deployment_id: str
    revision_id: uuid.UUID
    sequence: int
    head_digest: str
    exported_at: datetime.datetime | None
    sink_receipt_digest: str | None
    sink_receipt_signature: str | None


async def load_checkpoint(session: AsyncSession, checkpoint_id: uuid.UUID) -> CheckpointRow | None:
    row = (
        await session.execute(
            text(
                "SELECT checkpoint_id, deployment_id, revision_id, sequence, head_digest, exported_at, "
                "       sink_receipt_digest, sink_receipt_signature "
                "FROM arc_operational_chain_checkpoints WHERE checkpoint_id = :cid"
            ),
            {"cid": checkpoint_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return CheckpointRow(
        checkpoint_id=row.checkpoint_id,
        deployment_id=row.deployment_id,
        revision_id=row.revision_id,
        sequence=row.sequence,
        head_digest=row.head_digest,
        exported_at=row.exported_at,
        sink_receipt_digest=row.sink_receipt_digest,
        sink_receipt_signature=row.sink_receipt_signature,
    )


async def select_pending_checkpoints(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Every checkpoint still waiting on a sink acknowledgment, oldest
    first, capped at *limit*.

    A plain unlocked `SELECT`, matching `source_admission.py`'s
    `select_due_for_refresh`'s own reasoning: the actual mutation
    (`mark_exported`) is its own conditional compare-and-swap, so nothing
    here needs to hold a row lock across what may be a slow external sink
    call. Two exporter passes racing the same row is safe -- the sink's own
    identity-keyed idempotency and the compare-and-swap below both absorb
    it -- so there is nothing to lose by not locking here.
    """
    result = await session.execute(
        text(
            "SELECT checkpoint_id FROM arc_operational_chain_checkpoints "
            "WHERE exported_at IS NULL ORDER BY created_at LIMIT :limit"
        ),
        {"limit": limit},
    )
    return list(result.scalars().all())


async def mark_exported(
    session: AsyncSession,
    *,
    checkpoint_id: uuid.UUID,
    exported_at: datetime.datetime,
    sink_receipt_digest: str,
    sink_receipt_signature: str,
) -> bool:
    """Record a sink acknowledgment -- guarded so a concurrent exporter
    pass that already recorded one cannot overwrite it with a second,
    possibly different, receipt. Returns whether this call is the one that
    applied it."""
    result = await session.execute(
        text(
            "UPDATE arc_operational_chain_checkpoints "
            "SET exported_at = :exported_at, sink_receipt_digest = :digest, sink_receipt_signature = :signature "
            "WHERE checkpoint_id = :cid AND exported_at IS NULL"
        ),
        {
            "cid": checkpoint_id,
            "exported_at": exported_at,
            "digest": sink_receipt_digest,
            "signature": sink_receipt_signature,
        },
    )
    return (result.rowcount or 0) == 1  # type: ignore[attr-defined]


async def record_export_failure(
    session: AsyncSession, *, checkpoint_id: uuid.UUID, error_code: str, attempted_at: datetime.datetime
) -> None:
    await session.execute(
        text(
            "UPDATE arc_operational_chain_checkpoints "
            "SET export_attempts = export_attempts + 1, last_export_error_code = :code, "
            "    last_export_attempt_at = :attempted_at "
            "WHERE checkpoint_id = :cid"
        ),
        {"cid": checkpoint_id, "code": error_code, "attempted_at": attempted_at},
    )


__all__ = [
    "CheckpointRow",
    "EventRow",
    "ExistingEvent",
    "HeadRow",
    "advance_head",
    "count_pending_checkpoints",
    "find_by_idempotency",
    "insert_checkpoint",
    "insert_event",
    "insert_head",
    "load_checkpoint",
    "load_events",
    "load_head",
    "lock_head",
    "mark_exported",
    "record_export_failure",
    "select_pending_checkpoints",
]
