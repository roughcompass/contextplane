"""Parametrized SQL for the D2 projection-approval challenge protocol.

`approval_challenge.py` owns authorization, canonicalization, and the
compare-and-swap shape; this module owns getting rows in and out of
`arc_approval_challenges`, reading the verifier a challenge names, and the
one `arc_authoring_proposal_versions` compare-and-swap this protocol
performs (`submitted -> approved`). Every function takes an already-open
`AsyncSession` -- none of them opens its own transaction -- matching every
other queries module in this package's stated convention.

**One deliberate exception.** The `arc_projection_approval_evidence` INSERT
itself is *not* here -- it lives directly in `approval_challenge.py`.
`scripts/check_arc_approval_writers.py` is an AST gate that attributes a
privileged write to the file whose own source contains the `text(...)` call
site; keeping the one write this deployment's entire trust chain rests on
visible in the orchestrating service file, rather than one indirection layer
away, is what lets that gate's allowlist name a single, exact module. Every
other write in this protocol (the challenge row, the compare-and-swap) has
no such constraint and lives here as usual.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.service.approval_challenge_verification import MAX_ATTEMPTS

# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class VerifierRow:
    """One `arc_approval_verifiers` row, with both its signing material
    (`algorithm`/`public_key`/`provider_id`) and its D1 principal-binding
    columns -- the union `approval_challenge.py`'s verification needs.
    Distinct from `queries/enrollment.py`'s own `VerifierRow`, which omits
    signing material because D1 enrollment verifies proof of possession
    against the *challenge's* committed credential, not this row's.
    """

    approval_verifier_id: str
    allowed_evidence_types: tuple[str, ...]
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    revoked_at: datetime.datetime | None
    principal_binding_kind: str | None
    principal_issuer: str | None
    principal_subject: str | None
    provider_id: str | None
    algorithm: str | None
    public_key: bytes | None
    credential_fingerprint: str | None


@dataclasses.dataclass(frozen=True)
class ChallengeRow:
    approval_challenge_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    approval_verifier_id: str
    nonce: str
    canonical_evidence_bytes: bytes
    signing_domain: str
    approved_payload_digest: str
    idempotency_scope_digest: str
    request_payload_digest: str
    requested_by_issuer: str
    requested_by_subject: str
    attempt_count: int
    state: str
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    terminalized_at: datetime.datetime | None


_CHALLENGE_COLUMNS = (
    "approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id, approval_verifier_id,"
    " nonce, canonical_evidence_bytes, signing_domain, approved_payload_digest, idempotency_scope_digest,"
    " request_payload_digest, requested_by_issuer, requested_by_subject, attempt_count, state,"
    " issued_at, expires_at, terminalized_at"
)


def _challenge_row(row: object) -> ChallengeRow:
    return ChallengeRow(
        approval_challenge_id=row.approval_challenge_id,  # type: ignore[attr-defined]
        proposal_id=row.proposal_id,  # type: ignore[attr-defined]
        proposal_version=row.proposal_version,  # type: ignore[attr-defined]
        artifact_id=row.artifact_id,  # type: ignore[attr-defined]
        revision_id=row.revision_id,  # type: ignore[attr-defined]
        approval_verifier_id=row.approval_verifier_id,  # type: ignore[attr-defined]
        nonce=row.nonce,  # type: ignore[attr-defined]
        canonical_evidence_bytes=bytes(row.canonical_evidence_bytes),  # type: ignore[attr-defined]
        signing_domain=row.signing_domain,  # type: ignore[attr-defined]
        approved_payload_digest=row.approved_payload_digest,  # type: ignore[attr-defined]
        idempotency_scope_digest=row.idempotency_scope_digest,  # type: ignore[attr-defined]
        request_payload_digest=row.request_payload_digest,  # type: ignore[attr-defined]
        requested_by_issuer=row.requested_by_issuer,  # type: ignore[attr-defined]
        requested_by_subject=row.requested_by_subject,  # type: ignore[attr-defined]
        attempt_count=row.attempt_count,  # type: ignore[attr-defined]
        state=row.state,  # type: ignore[attr-defined]
        issued_at=row.issued_at,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        terminalized_at=row.terminalized_at,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Verifier lookup
# ---------------------------------------------------------------------------


async def load_verifier_for_share(session: AsyncSession, approval_verifier_id: str) -> VerifierRow | None:
    """Read one verifier inside the caller's transaction, locked `FOR SHARE`.

    Matches `VerifierRegistry.get`'s own stated reason for the lock: a
    revocation is a plain `UPDATE`, which takes an implicit `FOR UPDATE`, so
    holding this share lock for the duration of the caller's transaction is
    what stops a challenge completion from verifying against a verifier
    whose revocation commits a moment later and still recording it as
    approved (trust-roots D4: transaction-scoped, uncached resolution).
    """
    row = (
        await session.execute(
            text(
                "SELECT approval_verifier_id, allowed_evidence_types, valid_from, valid_to, revoked_at,"
                "       principal_binding_kind, principal_issuer, principal_subject, provider_id,"
                "       algorithm, public_key, credential_fingerprint "
                "FROM arc_approval_verifiers WHERE approval_verifier_id = :vid FOR SHARE"
            ),
            {"vid": approval_verifier_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return VerifierRow(
        approval_verifier_id=row.approval_verifier_id,
        allowed_evidence_types=tuple(row.allowed_evidence_types or ()),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        revoked_at=row.revoked_at,
        principal_binding_kind=row.principal_binding_kind,
        principal_issuer=row.principal_issuer,
        principal_subject=row.principal_subject,
        provider_id=row.provider_id,
        algorithm=row.algorithm,
        public_key=bytes(row.public_key) if row.public_key is not None else None,
        credential_fingerprint=row.credential_fingerprint,
    )


# ---------------------------------------------------------------------------
# Challenge rows
# ---------------------------------------------------------------------------


async def find_challenge_by_scope_digest(session: AsyncSession, idempotency_scope_digest: str) -> ChallengeRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_CHALLENGE_COLUMNS} FROM arc_approval_challenges "  # noqa: S608 - _CHALLENGE_COLUMNS is a module constant, not caller input
                "WHERE idempotency_scope_digest = :digest"
            ),
            {"digest": idempotency_scope_digest},
        )
    ).one_or_none()
    return None if row is None else _challenge_row(row)


async def insert_challenge(
    session: AsyncSession,
    *,
    approval_challenge_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    artifact_id: uuid.UUID,
    revision_id: uuid.UUID,
    approval_verifier_id: str,
    nonce: str,
    canonical_evidence_bytes: bytes,
    signing_domain: str,
    approved_payload_digest: str,
    idempotency_scope_digest: str,
    request_payload_digest: str,
    requested_by_issuer: str,
    requested_by_subject: str,
    issued_at: datetime.datetime,
    expires_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_approval_challenges ("
            "  approval_challenge_id, proposal_id, proposal_version, artifact_id, revision_id,"
            "  approval_verifier_id, nonce, canonical_evidence_bytes, signing_domain, approved_payload_digest,"
            "  idempotency_scope_digest, request_payload_digest, requested_by_issuer, requested_by_subject,"
            "  issued_at, expires_at"
            ") VALUES ("
            "  :approval_challenge_id, :proposal_id, :proposal_version, :artifact_id, :revision_id,"
            "  :approval_verifier_id, :nonce, :canonical_evidence_bytes, :signing_domain, :approved_payload_digest,"
            "  :idempotency_scope_digest, :request_payload_digest, :requested_by_issuer, :requested_by_subject,"
            "  :issued_at, :expires_at"
            ")"
        ),
        {
            "approval_challenge_id": approval_challenge_id,
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "approval_verifier_id": approval_verifier_id,
            "nonce": nonce,
            "canonical_evidence_bytes": canonical_evidence_bytes,
            "signing_domain": signing_domain,
            "approved_payload_digest": approved_payload_digest,
            "idempotency_scope_digest": idempotency_scope_digest,
            "request_payload_digest": request_payload_digest,
            "requested_by_issuer": requested_by_issuer,
            "requested_by_subject": requested_by_subject,
            "issued_at": issued_at,
            "expires_at": expires_at,
        },
    )


async def lock_challenge(session: AsyncSession, approval_challenge_id: uuid.UUID) -> ChallengeRow | None:
    """`SELECT ... FOR UPDATE` -- held for the rest of the caller's
    transaction so the attempt-ceiling increment, the terminal-state
    transition, and the eventual evidence insert are never interleaved with
    a second completion attempt against the same challenge.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_CHALLENGE_COLUMNS} FROM arc_approval_challenges "  # noqa: S608 - _CHALLENGE_COLUMNS is a module constant, not caller input
                "WHERE approval_challenge_id = :cid FOR UPDATE"
            ),
            {"cid": approval_challenge_id},
        )
    ).one_or_none()
    return None if row is None else _challenge_row(row)


async def record_failed_attempt(
    session: AsyncSession, approval_challenge_id: uuid.UUID, *, new_attempt_count: int, now: datetime.datetime
) -> None:
    """Increment the attempt counter, and terminalize the challenge as
    `failed` exactly when the new count reaches the ceiling. One statement
    for both, so there is no window in which the count is updated but the
    terminal state is not (or vice versa).
    """
    if new_attempt_count >= MAX_ATTEMPTS:
        await session.execute(
            text(
                "UPDATE arc_approval_challenges SET attempt_count = :count, state = 'failed', "
                "terminalized_at = :now WHERE approval_challenge_id = :cid"
            ),
            {"count": new_attempt_count, "now": now, "cid": approval_challenge_id},
        )
    else:
        await session.execute(
            text("UPDATE arc_approval_challenges SET attempt_count = :count WHERE approval_challenge_id = :cid"),
            {"count": new_attempt_count, "cid": approval_challenge_id},
        )


async def mark_terminal(
    session: AsyncSession, approval_challenge_id: uuid.UUID, *, state: str, now: datetime.datetime
) -> None:
    """Move a challenge to a terminal state (`expired` or `superseded`) with
    no attempt-count change -- neither losing a race nor timing out is a
    signature failure.
    """
    await session.execute(
        text(
            "UPDATE arc_approval_challenges SET state = :state, terminalized_at = :now "
            "WHERE approval_challenge_id = :cid"
        ),
        {"state": state, "now": now, "cid": approval_challenge_id},
    )


async def mark_completed(session: AsyncSession, approval_challenge_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE arc_approval_challenges SET state = 'completed' WHERE approval_challenge_id = :cid"),
        {"cid": approval_challenge_id},
    )


async def count_live_challenges(
    session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int, now: datetime.datetime
) -> int:
    """Live = `issued` and not yet past its own expiry -- the cardinality
    D2 caps at ten per proposal version (and, separately, ten per actor).
    """
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM arc_approval_challenges "
                "WHERE proposal_id = :pid AND proposal_version = :pv AND state = 'issued' AND expires_at > :now"
            ),
            {"pid": proposal_id, "pv": proposal_version, "now": now},
        )
    ).scalar_one()
    return int(count)


async def count_live_challenges_for_actor(
    session: AsyncSession, *, requested_by_issuer: str, requested_by_subject: str, now: datetime.datetime
) -> int:
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM arc_approval_challenges "
                "WHERE requested_by_issuer = :issuer AND requested_by_subject = :subject "
                "  AND state = 'issued' AND expires_at > :now"
            ),
            {"issuer": requested_by_issuer, "subject": requested_by_subject, "now": now},
        )
    ).scalar_one()
    return int(count)


# ---------------------------------------------------------------------------
# The one proposal-version write this protocol performs.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApprovedVersion:
    proposal_id: uuid.UUID
    proposal_version: int
    revision_id: uuid.UUID


async def cas_submitted_to_approved(
    session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
) -> ApprovedVersion | None:
    """`submitted -> approved`, in the same statement that decides it.

    Mirrors `queries/proposal.py`'s `transition_version` and `queries/
    materialisation.py`'s `freeze_and_link`: a bare `UPDATE ... WHERE ...
    RETURNING`, no separate `SELECT ... FOR UPDATE` -- the `WHERE` clause's
    row lock at execution time is the whole mechanism. Returns `None` on a
    lost race (another challenge's completion already moved this version
    out of `submitted`) or a not-found row; the caller decides which.
    Deliberately does not touch `terminal_*`/`terminalized_at`: `approved`
    is not a terminal proposal state.
    """
    row = (
        await session.execute(
            text(
                "UPDATE arc_authoring_proposal_versions SET state = 'approved' "
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version AND state = 'submitted' "
                "RETURNING proposal_id, proposal_version, revision_id"
            ),
            {"proposal_id": proposal_id, "proposal_version": proposal_version},
        )
    ).one_or_none()
    if row is None or row.revision_id is None:
        return None
    return ApprovedVersion(
        proposal_id=row.proposal_id, proposal_version=row.proposal_version, revision_id=row.revision_id
    )


@dataclasses.dataclass(frozen=True)
class LiveEvidenceRow:
    """The one live (`revoked_at IS NULL`) `arc_projection_approval_evidence`
    row for a revision, with the proof material `_load_evidence`/`_load_
    evidence_by_version` in `approval_challenge.py` deliberately omit (those
    two never return `proof_bytes`/`signature_algorithm` to a router --
    Appendix B.1's "never returned by any route" rule). This row exists for
    `registry.arc.service.integrity`'s own re-verification, which runs
    inside the service layer and never crosses the wire, so the same
    omission does not apply here.
    """

    evidence_id: uuid.UUID
    approval_challenge_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    revision_id: uuid.UUID
    approved_payload_digest: str
    approval_verifier_id: str
    credential_fingerprint_at_approval: str
    verification_method: str
    signature_algorithm: str | None
    proof_bytes: bytes
    revoked_at: datetime.datetime | None


async def load_live_evidence_by_revision(session: AsyncSession, revision_id: uuid.UUID) -> LiveEvidenceRow | None:
    """The live evidence for one revision, if any -- a plain, unlocked read.

    `revision_id` is not unique on this table by schema (only `approval_
    challenge_id` is), but the `submitted -> approved` compare-and-swap this
    protocol performs at most once per proposal version, combined with the
    `revision_id` bijection, means at most one *live* row exists per
    revision in practice; `revoked_at IS NULL` narrows to that one row the
    same way `find_live_evidence_challenge` above already does.
    """
    row = (
        await session.execute(
            text(
                "SELECT evidence_id, approval_challenge_id, proposal_id, proposal_version, revision_id,"
                "       approved_payload_digest, approval_verifier_id, credential_fingerprint_at_approval,"
                "       verification_method, signature_algorithm, proof_bytes, revoked_at "
                "FROM arc_projection_approval_evidence "
                "WHERE revision_id = :revision_id AND revoked_at IS NULL"
            ),
            {"revision_id": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return LiveEvidenceRow(
        evidence_id=row.evidence_id,
        approval_challenge_id=row.approval_challenge_id,
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        revision_id=row.revision_id,
        approved_payload_digest=row.approved_payload_digest,
        approval_verifier_id=row.approval_verifier_id,
        credential_fingerprint_at_approval=row.credential_fingerprint_at_approval,
        verification_method=row.verification_method,
        signature_algorithm=row.signature_algorithm,
        proof_bytes=bytes(row.proof_bytes),
        revoked_at=row.revoked_at,
    )


async def find_live_evidence_challenge(
    session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int
) -> uuid.UUID | None:
    """The `approval_challenge_id` of the one live (`revoked_at IS NULL`)
    evidence row for this proposal version, if any -- used to tell a losing
    completion "superseded by a different challenge" apart from "this
    challenge itself already won" without disclosing the winner's evidence.
    """
    return (
        await session.execute(
            text(
                "SELECT approval_challenge_id FROM arc_projection_approval_evidence "
                "WHERE proposal_id = :pid AND proposal_version = :pv AND revoked_at IS NULL"
            ),
            {"pid": proposal_id, "pv": proposal_version},
        )
    ).scalar_one_or_none()


__all__ = [
    "ApprovedVersion",
    "ChallengeRow",
    "LiveEvidenceRow",
    "VerifierRow",
    "cas_submitted_to_approved",
    "count_live_challenges",
    "count_live_challenges_for_actor",
    "find_challenge_by_scope_digest",
    "find_live_evidence_challenge",
    "insert_challenge",
    "load_live_evidence_by_revision",
    "load_verifier_for_share",
    "lock_challenge",
    "mark_completed",
    "mark_terminal",
    "record_failed_attempt",
]
