"""Projection-approval challenges and the verified evidence they produce.

Revision ID: 0009_arc_approval_challenges
Revises: 0008_arc_verifier_principal_binding
Create Date: 2026-08-06

`arc_approval_challenges` is the D2 two-call protocol's own state: the server
recomputes the approval-target digest, commits it (and every other bound
field) into `canonical_evidence_bytes`, and hands that back for the named
verifier to sign or attest over before any evidence exists. `UNIQUE (nonce)`
and `UNIQUE (idempotency_scope_digest)` back the single-use and exact-retry
rules the service enforces; `CHECK attempt_count <= 3` is the attempt
ceiling -- the third invalid signature terminalizes the challenge rather than
leaving it retryable forever.

`arc_projection_approval_evidence` is the verified output: the row activation
predicate 8 revalidates. `UNIQUE (approval_challenge_id)` gives one evidence
per winning challenge; the partial `UNIQUE (proposal_id, proposal_version)
WHERE revoked_at IS NULL` gives one *live* evidence per proposal version
while still letting a revoked row coexist with the one that superseded it --
a plain UNIQUE could not do both at once.

This table is deliberately distinct from the pre-existing `arc_approval_
evidence`, whose only remaining writable value is `exception_approval` now
that its direct `artifact_activation` write path has been removed: an
`artifact_activation`-class row belongs here instead, verified by the
challenge/proof round trip this migration adds, not by a direct
evidence-type write.
"""

from __future__ import annotations

from alembic import op

revision = "0009_arc_approval_challenges"
down_revision: str | None = "0008_arc_verifier_principal_binding"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_approval_challenges -- the two-call protocol's own state.
# ---------------------------------------------------------------------------

_CHALLENGES_DDL = """
CREATE TABLE arc_approval_challenges (
    approval_challenge_id      UUID PRIMARY KEY,
    proposal_id                UUID NOT NULL,
    proposal_version           INTEGER NOT NULL,
    artifact_id                UUID NOT NULL REFERENCES arc_artifacts(artifact_id),
    revision_id                UUID NOT NULL REFERENCES arc_revisions(revision_id),
    approval_verifier_id       TEXT NOT NULL REFERENCES arc_approval_verifiers(approval_verifier_id),
    nonce                      TEXT NOT NULL,
    canonical_evidence_bytes   BYTEA NOT NULL,
    signing_domain             TEXT NOT NULL,
    approved_payload_digest    TEXT NOT NULL,
    idempotency_scope_digest   TEXT NOT NULL,
    request_payload_digest     TEXT NOT NULL,
    requested_by_issuer        TEXT NOT NULL,
    requested_by_subject       TEXT NOT NULL,
    attempt_count               INTEGER NOT NULL DEFAULT 0,
    state                      TEXT NOT NULL DEFAULT 'issued',
    issued_at                  TIMESTAMPTZ NOT NULL,
    expires_at                 TIMESTAMPTZ NOT NULL,
    terminalized_at             TIMESTAMPTZ,
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    -- Appendix B.3, verbatim: this is the same column the primary key
    -- already makes unique, restated as its own named constraint because
    -- the TDD names it as a discrete rule to prove rather than an
    -- incidental property of the key choice.
    CONSTRAINT uq_arc_approval_challenges_id UNIQUE (approval_challenge_id),
    CONSTRAINT uq_arc_approval_challenges_nonce UNIQUE (nonce),
    CONSTRAINT uq_arc_approval_challenges_idempotency_scope UNIQUE (idempotency_scope_digest),
    CONSTRAINT ck_arc_approval_challenges_attempt_count CHECK (attempt_count <= 3),
    CONSTRAINT ck_arc_approval_challenges_state CHECK (
        state IN ('issued', 'completed', 'failed', 'expired', 'superseded')
    ),
    CONSTRAINT ck_arc_approval_challenges_window CHECK (issued_at < expires_at)
)
"""

_CHALLENGES_INDEXES = [
    "CREATE INDEX ix_arc_approval_challenges_version ON arc_approval_challenges (proposal_id, proposal_version)",
]

# ---------------------------------------------------------------------------
# arc_projection_approval_evidence -- the verified D2 output. Column set is
# named exactly, per the TDD's own Appendix B.1 table for this row.
# ---------------------------------------------------------------------------

_EVIDENCE_DDL = """
CREATE TABLE arc_projection_approval_evidence (
    evidence_id                          UUID PRIMARY KEY,
    approval_challenge_id                UUID NOT NULL REFERENCES arc_approval_challenges(approval_challenge_id),
    proposal_id                          UUID NOT NULL,
    proposal_version                     INTEGER NOT NULL,
    revision_id                          UUID NOT NULL REFERENCES arc_revisions(revision_id),
    approved_payload_digest              TEXT NOT NULL,
    approval_verifier_id                 TEXT NOT NULL REFERENCES arc_approval_verifiers(approval_verifier_id),
    approving_principal_issuer           TEXT NOT NULL,
    approving_principal_subject          TEXT NOT NULL,
    credential_fingerprint_at_approval    TEXT NOT NULL,
    verification_method                  TEXT NOT NULL,
    signature_algorithm                  TEXT,
    proof_bytes                          BYTEA NOT NULL,
    signing_domain                       TEXT NOT NULL,
    verified_at                          TIMESTAMPTZ NOT NULL,
    revoked_at                           TIMESTAMPTZ,
    revocation_reason_code               TEXT,
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT uq_arc_projection_approval_evidence_challenge UNIQUE (approval_challenge_id),
    CONSTRAINT ck_arc_projection_approval_evidence_method CHECK (
        verification_method IN ('detached_signature', 'verifier_attestation')
    ),
    CONSTRAINT ck_arc_projection_approval_evidence_revocation_pair CHECK (
        (revoked_at IS NULL) = (revocation_reason_code IS NULL)
    )
)
"""

# The one-live-evidence-per-version rule per Appendix B.1/B.2: a *partial*
# UNIQUE, not a plain one -- it must refuse a second live row for the same
# version while still letting any number of revoked rows for that same
# version coexist (a revoked-then-reapproved history). A plain UNIQUE on
# (proposal_id, proposal_version) would refuse the second case too, which is
# the wrong index; the WHERE clause is what makes the two behave differently.
_EVIDENCE_LIVE_PER_VERSION_INDEX = (
    "CREATE UNIQUE INDEX uq_arc_projection_approval_evidence_live_per_version "
    "ON arc_projection_approval_evidence (proposal_id, proposal_version) "
    "WHERE revoked_at IS NULL"
)


def upgrade() -> None:
    # Challenges first: evidence's FK references it.
    op.execute(_CHALLENGES_DDL)
    for stmt in _CHALLENGES_INDEXES:
        op.execute(stmt)
    op.execute(_EVIDENCE_DDL)
    op.execute(_EVIDENCE_LIVE_PER_VERSION_INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE arc_projection_approval_evidence")
    op.execute("DROP TABLE arc_approval_challenges")
