"""Parametrized SQL for the five source-admission tables.

`source_admission.py` owns validation, streaming, redirect-following
fetches, and the admission transaction's shape; this module owns getting
rows in and out of the five tables that transaction touches. Every function
takes an already-open `AsyncSession` — none of them opens its own
transaction — so the caller controls exactly what commits together.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Row shapes. Plain dataclasses rather than the ORM models in `arc/models.py`
# — the service reasons about exactly the columns a query selected, not
# about a lazily-loadable mapped instance tied to a session.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ConnectorRow:
    connector_id: str
    owning_scope: str
    tenant_id: uuid.UUID | None
    allowed_schemes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allowed_media_types: tuple[str, ...]
    allowed_verifier_ids: tuple[str, ...]
    max_bytes: int
    credential_ref: str | None
    registered_at: datetime.datetime
    #: Set when the grant was withdrawn. The admission path refuses on this;
    #: material admitted before it stays admitted, because it was validly
    #: admitted and rewriting that would describe a history that did not happen.
    revoked_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class UploadPolicyRow:
    policy_id: str
    owning_scope: str
    tenant_id: uuid.UUID | None
    allowed_media_types: tuple[str, ...]
    allowed_verifier_ids: tuple[str, ...]
    max_bytes: int
    registered_at: datetime.datetime
    #: See `ConnectorRow.revoked_at`. The same grant, pushed rather than pulled.
    revoked_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class EvidenceRow:
    source_evidence_id: uuid.UUID
    owning_scope: str
    tenant_id: uuid.UUID | None
    source_system: str
    source_revision_locator: str
    source_content_type: str
    source_content_digest: str
    claim: dict[str, Any]
    claim_digest: str
    verification_method: str
    verifier_id: str
    signature: str | None
    verifier_attestation: dict[str, Any] | None
    admission_method: str
    connector_id: str | None
    policy_id: str | None
    admitted_at: datetime.datetime
    admitted_by_issuer: str
    admitted_by_subject: str
    verified_at: datetime.datetime
    expires_at: datetime.datetime
    idempotency_key_digest: str
    admission_request_payload_digest: str
    idempotency_scope_digest: str


@dataclasses.dataclass(frozen=True)
class StatusRow:
    source_evidence_id: uuid.UUID
    status: str
    checked_at: datetime.datetime
    next_check_at: datetime.datetime
    status_source: str
    status_evidence_digest: str | None


@dataclasses.dataclass(frozen=True)
class BodyRow:
    source_evidence_id: uuid.UUID
    content_digest: str
    content_bytes: int
    body: bytes
    created_at: datetime.datetime


_EVIDENCE_COLUMNS = (
    "source_evidence_id, owning_scope, tenant_id, source_system, source_revision_locator, "
    "source_content_type, source_content_digest, claim, claim_digest, verification_method, "
    "verifier_id, signature, verifier_attestation, admission_method, connector_id, policy_id, "
    "admitted_at, admitted_by_issuer, admitted_by_subject, verified_at, expires_at, "
    "idempotency_key_digest, admission_request_payload_digest, idempotency_scope_digest"
)


def _evidence_row(row: Any) -> EvidenceRow:  # noqa: ANN401 - a raw SQLAlchemy Row has no narrower public type
    return EvidenceRow(
        source_evidence_id=row.source_evidence_id,
        owning_scope=row.owning_scope,
        tenant_id=row.tenant_id,
        source_system=row.source_system,
        source_revision_locator=row.source_revision_locator,
        source_content_type=row.source_content_type,
        source_content_digest=row.source_content_digest,
        claim=dict(row.claim),
        claim_digest=row.claim_digest,
        verification_method=row.verification_method,
        verifier_id=row.verifier_id,
        signature=row.signature,
        verifier_attestation=dict(row.verifier_attestation) if row.verifier_attestation is not None else None,
        admission_method=row.admission_method,
        connector_id=row.connector_id,
        policy_id=row.policy_id,
        admitted_at=row.admitted_at,
        admitted_by_issuer=row.admitted_by_issuer,
        admitted_by_subject=row.admitted_by_subject,
        verified_at=row.verified_at,
        expires_at=row.expires_at,
        idempotency_key_digest=row.idempotency_key_digest,
        admission_request_payload_digest=row.admission_request_payload_digest,
        idempotency_scope_digest=row.idempotency_scope_digest,
    )


# ---------------------------------------------------------------------------
# arc_source_connectors
# ---------------------------------------------------------------------------


async def insert_connector(
    session: AsyncSession,
    *,
    connector_id: str,
    owning_scope: str,
    tenant_id: uuid.UUID | None,
    allowed_schemes: list[str],
    allowed_hosts: list[str],
    allowed_media_types: list[str],
    allowed_verifier_ids: list[str],
    max_bytes: int,
    credential_ref: str | None,
    registered_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_source_connectors ("
            "  connector_id, owning_scope, tenant_id, allowed_schemes, allowed_hosts,"
            "  allowed_media_types, allowed_verifier_ids, max_bytes, credential_ref, registered_at"
            ") VALUES ("
            "  :connector_id, :owning_scope, :tenant_id, CAST(:allowed_schemes AS TEXT[]),"
            "  CAST(:allowed_hosts AS TEXT[]), CAST(:allowed_media_types AS TEXT[]),"
            "  CAST(:allowed_verifier_ids AS TEXT[]), :max_bytes, :credential_ref, :registered_at"
            ")"
        ),
        {
            "connector_id": connector_id,
            "owning_scope": owning_scope,
            "tenant_id": tenant_id,
            "allowed_schemes": allowed_schemes,
            "allowed_hosts": allowed_hosts,
            "allowed_media_types": allowed_media_types,
            "allowed_verifier_ids": allowed_verifier_ids,
            "max_bytes": max_bytes,
            "credential_ref": credential_ref,
            "registered_at": registered_at,
        },
    )


async def revoke_connector(
    session: AsyncSession,
    *,
    connector_id: str,
    actor_id: uuid.UUID,
    reason: str,
    now: datetime.datetime,
) -> bool:
    """Withdraw a connector. Returns False when it was already withdrawn.

    A compare-and-swap on `revoked_at IS NULL` rather than a blind UPDATE: two
    operators withdrawing the same connector must leave the first decision and
    the first reason, not the last writer's.
    """
    updated = (
        await session.execute(
            text(
                "UPDATE arc_source_connectors "
                "SET revoked_at = :now, revoked_by = :actor, revocation_reason = :reason "
                "WHERE connector_id = :cid AND revoked_at IS NULL "
                "RETURNING connector_id"
            ),
            {"cid": connector_id, "actor": actor_id, "reason": reason, "now": now},
        )
    ).one_or_none()
    return updated is not None


async def revoke_upload_policy(
    session: AsyncSession,
    *,
    policy_id: str,
    actor_id: uuid.UUID,
    reason: str,
    now: datetime.datetime,
) -> bool:
    """Withdraw an upload policy. Same compare-and-swap, same reason."""
    updated = (
        await session.execute(
            text(
                "UPDATE arc_source_upload_policies "
                "SET revoked_at = :now, revoked_by = :actor, revocation_reason = :reason "
                "WHERE policy_id = :pid AND revoked_at IS NULL "
                "RETURNING policy_id"
            ),
            {"pid": policy_id, "actor": actor_id, "reason": reason, "now": now},
        )
    ).one_or_none()
    return updated is not None


async def load_connector(session: AsyncSession, connector_id: str) -> ConnectorRow | None:
    row = (
        await session.execute(
            text(
                "SELECT connector_id, owning_scope, tenant_id, allowed_schemes, allowed_hosts,"
                "       allowed_media_types, allowed_verifier_ids, max_bytes, credential_ref,"
                "       registered_at, revoked_at "
                "FROM arc_source_connectors WHERE connector_id = :connector_id"
            ),
            {"connector_id": connector_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ConnectorRow(
        connector_id=row.connector_id,
        owning_scope=row.owning_scope,
        tenant_id=row.tenant_id,
        allowed_schemes=tuple(row.allowed_schemes),
        allowed_hosts=tuple(row.allowed_hosts),
        allowed_media_types=tuple(row.allowed_media_types),
        allowed_verifier_ids=tuple(row.allowed_verifier_ids),
        max_bytes=row.max_bytes,
        credential_ref=row.credential_ref,
        registered_at=row.registered_at,
        revoked_at=row.revoked_at,
    )


# ---------------------------------------------------------------------------
# arc_source_upload_policies
# ---------------------------------------------------------------------------


async def insert_upload_policy(
    session: AsyncSession,
    *,
    policy_id: str,
    owning_scope: str,
    tenant_id: uuid.UUID | None,
    allowed_media_types: list[str],
    allowed_verifier_ids: list[str],
    max_bytes: int,
    registered_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_source_upload_policies ("
            "  policy_id, owning_scope, tenant_id, allowed_media_types, allowed_verifier_ids,"
            "  max_bytes, registered_at"
            ") VALUES ("
            "  :policy_id, :owning_scope, :tenant_id, CAST(:allowed_media_types AS TEXT[]),"
            "  CAST(:allowed_verifier_ids AS TEXT[]), :max_bytes, :registered_at"
            ")"
        ),
        {
            "policy_id": policy_id,
            "owning_scope": owning_scope,
            "tenant_id": tenant_id,
            "allowed_media_types": allowed_media_types,
            "allowed_verifier_ids": allowed_verifier_ids,
            "max_bytes": max_bytes,
            "registered_at": registered_at,
        },
    )


async def load_upload_policy(session: AsyncSession, policy_id: str) -> UploadPolicyRow | None:
    row = (
        await session.execute(
            text(
                "SELECT policy_id, owning_scope, tenant_id, allowed_media_types, allowed_verifier_ids,"
                "       max_bytes, registered_at, revoked_at "
                "FROM arc_source_upload_policies WHERE policy_id = :policy_id"
            ),
            {"policy_id": policy_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return UploadPolicyRow(
        policy_id=row.policy_id,
        owning_scope=row.owning_scope,
        tenant_id=row.tenant_id,
        allowed_media_types=tuple(row.allowed_media_types),
        allowed_verifier_ids=tuple(row.allowed_verifier_ids),
        max_bytes=row.max_bytes,
        registered_at=row.registered_at,
        revoked_at=row.revoked_at,
    )


# ---------------------------------------------------------------------------
# Idempotency: a transaction-scoped advisory lock keyed by the scope digest,
# per ADR 039 — held only for the duration of the caller's transaction and
# released automatically at commit or rollback. `hashtextextended` reduces
# the 64-character hex digest to the bigint the lock function takes; two
# different digests hashing to the same lock key only ever costs unrelated
# admissions a moment of serialization, never a wrong answer, because the
# recheck immediately after acquiring the lock is what actually decides.
# ---------------------------------------------------------------------------


async def acquire_scope_lock(session: AsyncSession, scope_digest: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:digest, 0))"),
        {"digest": scope_digest},
    )


async def find_evidence_by_scope_digest(session: AsyncSession, scope_digest: str) -> EvidenceRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_EVIDENCE_COLUMNS} FROM arc_source_approval_evidence "  # noqa: S608 - _EVIDENCE_COLUMNS is a module constant, not caller input
                "WHERE idempotency_scope_digest = :digest"
            ),
            {"digest": scope_digest},
        )
    ).one_or_none()
    if row is None:
        return None
    return _evidence_row(row)


# ---------------------------------------------------------------------------
# arc_source_bodies / arc_source_approval_evidence / arc_source_approval_status
# ---------------------------------------------------------------------------


async def insert_body(
    session: AsyncSession,
    *,
    source_evidence_id: uuid.UUID,
    content_digest: str,
    content_bytes: int,
    body: bytes,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_source_bodies (source_evidence_id, content_digest, content_bytes, body, created_at) "
            "VALUES (:source_evidence_id, :content_digest, :content_bytes, :body, :created_at)"
        ),
        {
            "source_evidence_id": source_evidence_id,
            "content_digest": content_digest,
            "content_bytes": content_bytes,
            "body": body,
            "created_at": created_at,
        },
    )


async def load_body(session: AsyncSession, source_evidence_id: uuid.UUID) -> BodyRow | None:
    row = (
        await session.execute(
            text(
                "SELECT source_evidence_id, content_digest, content_bytes, body, created_at "
                "FROM arc_source_bodies WHERE source_evidence_id = :source_evidence_id"
            ),
            {"source_evidence_id": source_evidence_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return BodyRow(
        source_evidence_id=row.source_evidence_id,
        content_digest=row.content_digest,
        content_bytes=row.content_bytes,
        body=bytes(row.body),
        created_at=row.created_at,
    )


async def insert_evidence(
    session: AsyncSession,
    *,
    source_evidence_id: uuid.UUID,
    owning_scope: str,
    tenant_id: uuid.UUID | None,
    source_system: str,
    source_revision_locator: str,
    source_content_type: str,
    source_content_digest: str,
    claim: dict[str, Any],
    claim_digest: str,
    verification_method: str,
    verifier_id: str,
    signature: str | None,
    verifier_attestation: dict[str, Any] | None,
    admission_method: str,
    connector_id: str | None,
    policy_id: str | None,
    admitted_at: datetime.datetime,
    admitted_by_issuer: str,
    admitted_by_subject: str,
    verified_at: datetime.datetime,
    expires_at: datetime.datetime,
    idempotency_key_digest: str,
    admission_request_payload_digest: str,
    idempotency_scope_digest: str,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            f"INSERT INTO arc_source_approval_evidence ({_EVIDENCE_COLUMNS}, created_at) VALUES ("  # noqa: S608 - _EVIDENCE_COLUMNS is a module constant, not caller input
            ":source_evidence_id, :owning_scope, :tenant_id, :source_system, :source_revision_locator,"
            " :source_content_type, :source_content_digest, CAST(:claim AS JSONB), :claim_digest,"
            " :verification_method, :verifier_id, :signature, CAST(:verifier_attestation AS JSONB),"
            " :admission_method, :connector_id, :policy_id, :admitted_at, :admitted_by_issuer,"
            " :admitted_by_subject, :verified_at, :expires_at, :idempotency_key_digest,"
            " :admission_request_payload_digest, :idempotency_scope_digest, :created_at"
            ")"
        ),
        {
            "source_evidence_id": source_evidence_id,
            "owning_scope": owning_scope,
            "tenant_id": tenant_id,
            "source_system": source_system,
            "source_revision_locator": source_revision_locator,
            "source_content_type": source_content_type,
            "source_content_digest": source_content_digest,
            "claim": _json(claim),
            "claim_digest": claim_digest,
            "verification_method": verification_method,
            "verifier_id": verifier_id,
            "signature": signature,
            "verifier_attestation": _json(verifier_attestation) if verifier_attestation is not None else None,
            "admission_method": admission_method,
            "connector_id": connector_id,
            "policy_id": policy_id,
            "admitted_at": admitted_at,
            "admitted_by_issuer": admitted_by_issuer,
            "admitted_by_subject": admitted_by_subject,
            "verified_at": verified_at,
            "expires_at": expires_at,
            "idempotency_key_digest": idempotency_key_digest,
            "admission_request_payload_digest": admission_request_payload_digest,
            "idempotency_scope_digest": idempotency_scope_digest,
            "created_at": created_at,
        },
    )


async def load_evidence(session: AsyncSession, source_evidence_id: uuid.UUID) -> EvidenceRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_EVIDENCE_COLUMNS} FROM arc_source_approval_evidence "  # noqa: S608 - _EVIDENCE_COLUMNS is a module constant, not caller input
                "WHERE source_evidence_id = :source_evidence_id"
            ),
            {"source_evidence_id": source_evidence_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return _evidence_row(row)


async def insert_status(
    session: AsyncSession,
    *,
    source_evidence_id: uuid.UUID,
    status: str,
    checked_at: datetime.datetime,
    next_check_at: datetime.datetime,
    status_source: str,
    status_evidence_digest: str | None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_source_approval_status ("
            "  source_evidence_id, status, checked_at, next_check_at, status_source, status_evidence_digest"
            ") VALUES ("
            "  :source_evidence_id, :status, :checked_at, :next_check_at, :status_source, :status_evidence_digest"
            ")"
        ),
        {
            "source_evidence_id": source_evidence_id,
            "status": status,
            "checked_at": checked_at,
            "next_check_at": next_check_at,
            "status_source": status_source,
            "status_evidence_digest": status_evidence_digest,
        },
    )


async def load_status(session: AsyncSession, source_evidence_id: uuid.UUID) -> StatusRow | None:
    row = (
        await session.execute(
            text(
                "SELECT source_evidence_id, status, checked_at, next_check_at, status_source,"
                "       status_evidence_digest "
                "FROM arc_source_approval_status WHERE source_evidence_id = :source_evidence_id"
            ),
            {"source_evidence_id": source_evidence_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return StatusRow(
        source_evidence_id=row.source_evidence_id,
        status=row.status,
        checked_at=row.checked_at,
        next_check_at=row.next_check_at,
        status_source=row.status_source,
        status_evidence_digest=row.status_evidence_digest,
    )


# ---------------------------------------------------------------------------
# arc_source_approval_status refresh -- read the due set and advance the
# freshness window. `source_status.py` owns the vocabulary/freshness rules
# these two functions serve; this module only gets rows in and out.
# ---------------------------------------------------------------------------


async def select_due_for_refresh(session: AsyncSession, *, now: datetime.datetime, limit: int) -> list[uuid.UUID]:
    """Every row whose freshness window has lapsed, oldest-due first, capped
    at *limit*.

    A plain `SELECT`, not `FOR UPDATE`: the actual mutation
    (`update_status_refresh`) is its own conditional compare-and-swap, so
    nothing here needs to hold a row lock across a remote status check or
    an operational-chain write a due row might go on to trigger. Bounding by
    *limit* is what keeps one pass from spinning on an arbitrarily large
    backlog -- the caller's scheduler decides how often the next pass runs.
    """
    result = await session.execute(
        text(
            "SELECT source_evidence_id FROM arc_source_approval_status "
            "WHERE next_check_at <= :now "
            "ORDER BY next_check_at "
            "LIMIT :limit"
        ),
        {"now": now, "limit": limit},
    )
    return list(result.scalars().all())


async def update_status_refresh(
    session: AsyncSession,
    *,
    source_evidence_id: uuid.UUID,
    checked_at: datetime.datetime,
    next_check_at: datetime.datetime,
) -> bool:
    """Advance the freshness window for one still-`current` row; never
    touches `status` itself.

    The `WHERE ... AND next_check_at <= :checked_at` guard is a real
    compare-and-swap: if another pass already refreshed this row since it
    was selected, this matches zero rows instead of overwriting a fresher
    window with a stale one. That guard is what makes running the refresh
    worker twice over the same due set idempotent rather than merely
    harmless. Returns whether it actually applied, so the caller can tell a
    real refresh apart from a race it lost.
    """
    result = await session.execute(
        text(
            "UPDATE arc_source_approval_status "
            "SET checked_at = :checked_at, next_check_at = :next_check_at, status_source = 'worker' "
            "WHERE source_evidence_id = :source_evidence_id AND next_check_at <= :checked_at"
        ),
        {
            "checked_at": checked_at,
            "next_check_at": next_check_at,
            "source_evidence_id": source_evidence_id,
        },
    )
    return (result.rowcount or 0) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The revocation/expiry cascade. `source_status.py` owns the four-part
# atomic transaction (status flip, revision cascade, audit, operational
# event); this module only gets rows in and out of the two tables that
# transaction's non-operational-chain half touches.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DependentRevision:
    """One revision materialized from a proposal that cited the source
    being revoked or expired -- what the cascade has to act on."""

    artifact_id: uuid.UUID
    revision_id: uuid.UUID


async def find_active_revisions_by_source(
    session: AsyncSession, source_evidence_id: uuid.UUID
) -> list[DependentRevision]:
    """Every currently-`active` revision materialized from a proposal that
    named *source_evidence_id* as its `source_evidence_id`.

    `arc_revisions` carries no direct source-evidence column of its own --
    the only durable link is through the proposal version that
    materialized it (`arc_authoring_proposal_versions.source_evidence_id`
    -> `.revision_id`) -- so this is a join, not a lookup on
    `arc_revisions` alone. Only `active` revisions are returned: a
    superseded, revoked, or already-expired revision has nothing further
    for this cascade to do to it.
    """
    result = await session.execute(
        text(
            "SELECT r.artifact_id, r.revision_id FROM arc_revisions r "
            "JOIN arc_authoring_proposal_versions pv ON pv.revision_id = r.revision_id "
            "WHERE pv.source_evidence_id = :source_evidence_id AND r.lifecycle_state = 'active'"
        ),
        {"source_evidence_id": source_evidence_id},
    )
    return [DependentRevision(artifact_id=row.artifact_id, revision_id=row.revision_id) for row in result]


async def mark_status_terminal(
    session: AsyncSession,
    *,
    source_evidence_id: uuid.UUID,
    status: str,
    checked_at: datetime.datetime,
    next_check_at: datetime.datetime,
) -> bool:
    """Flip `arc_source_approval_status.status` to a terminal value
    (`revoked`/`expired`), guarded so a status that already reached a
    terminal value is never overwritten by a second, possibly
    contradictory, transition -- once revoked, never un-revoked by a later
    expiry check or the reverse.

    *next_check_at* is the caller's job to compute (typically `checked_at`
    plus the same freshness window fresh rows get, so a terminal status is
    still read through `check_status`'s own freshness gate rather than
    going stale itself) -- not computed here with `+ interval` inside the
    UPDATE, because asyncpg cannot always disambiguate one bind parameter
    reused in two different expression positions in the same statement
    (`checked_at = $2` and `$2 + interval '...'` both being `$2`), and
    raises `AmbiguousParameterError` rather than silently guessing.
    """
    result = await session.execute(
        text(
            "UPDATE arc_source_approval_status "
            "SET status = :status, checked_at = :checked_at, next_check_at = :next_check_at, "
            "    status_source = 'worker' "
            "WHERE source_evidence_id = :source_evidence_id AND status NOT IN ('revoked', 'expired')"
        ),
        {
            "status": status,
            "checked_at": checked_at,
            "next_check_at": next_check_at,
            "source_evidence_id": source_evidence_id,
        },
    )
    return (result.rowcount or 0) == 1  # type: ignore[attr-defined]


async def revoke_or_expire_revision(
    session: AsyncSession, *, revision_id: uuid.UUID, lifecycle_state: str, now: datetime.datetime
) -> None:
    """Move one revision to `revoked` or `expired` as part of the source
    cascade. Deliberately does not touch mandatory-obligation bookkeeping
    the way `ArtifactService.revoke`'s own human-driven path does --
    tombstoning `arc_mandatory_obligations` for a source-triggered cascade
    is a real gap this task does not close, named rather than silently
    assumed away; see this task's own report for the reasoning.
    """
    await session.execute(
        text("UPDATE arc_revisions SET lifecycle_state = :state, revoked_at = :now WHERE revision_id = :revision_id"),
        {"state": lifecycle_state, "now": now, "revision_id": revision_id},
    )


def _json(value: dict[str, Any]) -> str:
    """Encode a dict for a `CAST(:param AS JSONB)` bind.

    asyncpg does not accept a Python `dict` directly for a `jsonb` column
    through raw SQL text() the way the ORM's JSONB type adapts it — the
    parameter has to arrive as a JSON string for the cast to work.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "BodyRow",
    "ConnectorRow",
    "DependentRevision",
    "EvidenceRow",
    "StatusRow",
    "UploadPolicyRow",
    "acquire_scope_lock",
    "find_active_revisions_by_source",
    "find_evidence_by_scope_digest",
    "insert_body",
    "insert_connector",
    "insert_evidence",
    "insert_status",
    "insert_upload_policy",
    "load_body",
    "load_connector",
    "load_evidence",
    "load_status",
    "load_upload_policy",
    "mark_status_terminal",
    "revoke_or_expire_revision",
    "select_due_for_refresh",
    "update_status_refresh",
]
